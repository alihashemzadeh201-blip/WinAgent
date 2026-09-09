"""Screenshot post-processing.

The model sees a *scaled* copy of the screen (to keep token cost low) with an
optional coordinate grid drawn on it, so that it can reason about positions.
:class:`Screenshot` records the actual size ratio on each axis so the tool
executor can map model coordinates back to physical pixels without height-rounding drift.
"""

from __future__ import annotations

import base64
import io
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from .backends.base import ScreenGeometry
from .config import COORDINATE_SPACES


@dataclass
class Screenshot:
    image: Image.Image             # the (possibly annotated, scaled) image sent to the model
    raw_size: tuple[int, int]      # physical capture size (w, h)
    scale: float                   # nominal width scale (compatibility); mappings use actual per-axis ratios
    origin: tuple[int, int] = (0, 0)   # physical offset of the capture (multi-monitor / region)
    taken_at: float = field(default_factory=time.time)
    fmt: str = "jpeg"
    quality: int = 70
    is_region: bool = False        # zoomed detail does not replace the executor's full-screen click frame
    _encoded: Optional[bytes] = field(default=None, repr=False)
    frame_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    all_screens: bool = False
    desktop_bounds: Optional[tuple[int, int, int, int]] = None  # left, top, width, height AT CAPTURE
    coordinate_space: str = "image_pixels"  # frozen with the image, not read from mutable settings at click time

    def __post_init__(self) -> None:
        if self.coordinate_space not in COORDINATE_SPACES:
            raise ValueError(f"Unknown coordinate space: {self.coordinate_space}")

    @property
    def size(self) -> tuple[int, int]:
        return self.image.size

    @property
    def scale_x(self) -> float:
        return self.image.width / self.raw_size[0]

    @property
    def scale_y(self) -> float:
        # Resized height is rounded independently; reusing the width scale introduces vertical drift.
        return self.image.height / self.raw_size[1]

    def to_physical(self, x: float, y: float) -> tuple[int, int]:
        """Convert IMAGE PIXELS using the actual encoded dimensions; model units use model_to_physical."""
        px = int(round(x * self.raw_size[0] / self.image.width)) + self.origin[0]
        py = int(round(y * self.raw_size[1] / self.image.height)) + self.origin[1]
        return px, py

    def to_image(self, x: float, y: float) -> tuple[int, int]:
        """Convert physical coordinates to image pixels (the inverse of to_physical)."""
        ix = int(round((x - self.origin[0]) * self.image.width / self.raw_size[0]))
        iy = int(round((y - self.origin[1]) * self.image.height / self.raw_size[1]))
        return ix, iy

    @property
    def model_limits(self) -> tuple[int, int]:
        return (1000, 1000) if self.coordinate_space == "normalized_1000" else (self.image.width - 1, self.image.height - 1)

    def model_to_physical(self, x: float, y: float, *, edge: bool = False) -> tuple[int, int]:
        """Convert declared model units ONCE, directly into the raw capture's physical pixel space.

        Normalized coordinates use /1000 * size (not /999). 1000 denotes the far boundary;
        pointer targets use its last pixel, while crop edges are exclusive and may equal size.
        """
        if self.coordinate_space == "image_pixels":
            return self.to_physical(x, y)
        if not (0 <= x <= 1000 and 0 <= y <= 1000):
            raise ValueError("Normalized coordinates must be between 0 and 1000 on BOTH axes.")
        px = int(x * self.raw_size[0] / 1000)
        py = int(y * self.raw_size[1] / 1000)
        if not edge:
            px, py = min(px, self.raw_size[0] - 1), min(py, self.raw_size[1] - 1)
        return px + self.origin[0], py + self.origin[1]

    def to_model(self, x: float, y: float) -> tuple[int, int]:
        """Physical window/control/cursor position in the same units advertised to the model."""
        if self.coordinate_space == "image_pixels":
            return self.to_image(x, y)
        return (round((x - self.origin[0]) * 1000 / self.raw_size[0]),
                round((y - self.origin[1]) * 1000 / self.raw_size[1]))

    def encode(self) -> bytes:
        if self._encoded is None:
            buf = io.BytesIO()
            if self.fmt == "png":
                self.image.save(buf, format="PNG", optimize=True)
            else:
                self.image.convert("RGB").save(buf, format="JPEG", quality=self.quality, optimize=True)
            self._encoded = buf.getvalue()
        return self._encoded

    def data_url(self) -> str:
        mime = "image/png" if self.fmt == "png" else "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(self.encode()).decode('ascii')}"

    def describe(self) -> str:
        w, h = self.image.size
        description = (f"Screenshot {w}x{h} px (physical capture {self.raw_size[0]}x{self.raw_size[1]}; "
                       f"scale x={self.scale_x:.6f}, y={self.scale_y:.6f}; frame {self.frame_id}). ")
        if self.is_region:
            return description + ("ZOOMED detail for inspection only; pointer actions still use the LAST FULL screenshot. "
                                  "Take a full screenshot before clicking this area; do not use this crop's local coordinates.")
        if self.coordinate_space == "normalized_1000":
            return description + (
                "Coordinate space: normalized_1000, NOT image pixels. Both axes use a 0-1000 grid independent of "
                "image size. Centre=(500,500); 1000 denotes the far boundary (last pixel for mouse targets). "
                "DPI, image resizing and monitor origin are handled by the executor; do not convert again. "
                "Coordinates you send: x from 0 (left) to 1000 (right), y from 0 (top) to 1000 (bottom).")
        return description + (
            "Coordinate space: image_pixels. Use image pixels as given: display DPI and scaling are already handled; do not add window/title-bar/taskbar offsets. "
            f"Coordinates you send must be in this {w}x{h} space: x from 0 (left) to {w - 1} (right), "
            f"y from 0 (top) to {h - 1} (bottom).")


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except Exception:  # pragma: no cover
        return ImageFont.load_default()


def draw_grid(img: Image.Image, spacing: int = 100, color=(255, 0, 0), *, coordinate_space: str = "image_pixels") -> Image.Image:
    """Grid labels use MODEL units; draw positions and cursor annotation always use image pixels."""
    if spacing < 20:
        return img
    out = img.convert("RGBA")
    overlay = Image.new("RGBA", out.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = out.size
    font = _font(max(10, min(14, spacing // 7)))
    line = (*color, 90)
    label_bg = (0, 0, 0, 150)
    label_fg = (255, 255, 255, 255)
    x_limit, y_limit = (1000, 1000) if coordinate_space == "normalized_1000" else (w, h)
    for value in range(spacing, x_limit, spacing):
        x = round(value * w / 1000) if coordinate_space == "normalized_1000" else value
        draw.line([(x, 0), (x, h)], fill=line, width=1)
        txt = str(value)
        tw = draw.textlength(txt, font=font)
        draw.rectangle([x + 2, 2, x + 6 + tw, 16], fill=label_bg)
        draw.text((x + 4, 2), txt, fill=label_fg, font=font)
    for value in range(spacing, y_limit, spacing):
        y = round(value * h / 1000) if coordinate_space == "normalized_1000" else value
        draw.line([(0, y), (w, y)], fill=line, width=1)
        txt = str(value)
        tw = draw.textlength(txt, font=font)
        draw.rectangle([2, y + 2, 6 + tw, y + 16], fill=label_bg)
        draw.text((4, y + 2), txt, fill=label_fg, font=font)
    return Image.alpha_composite(out, overlay).convert("RGB")


def draw_cursor(img: Image.Image, x: int, y: int, color=(255, 255, 0)) -> Image.Image:
    """Draw a small crosshair marking the physical cursor (given in image coords)."""
    out = img.copy()
    draw = ImageDraw.Draw(out)
    r = 9
    draw.ellipse([x - r, y - r, x + r, y + r], outline=color, width=2)
    draw.line([(x - r - 4, y), (x + r + 4, y)], fill=color, width=2)
    draw.line([(x, y - r - 4), (x, y + r + 4)], fill=color, width=2)
    return out


def prepare_screenshot(
    raw: Image.Image,
    *,
    geometry: Optional[ScreenGeometry] = None,
    max_width: Optional[int] = 1280,
    grid: bool = True,
    grid_spacing: int = 100,
    cursor: Optional[tuple[int, int]] = None,
    fmt: str = "jpeg",
    quality: int = 70,
    coordinate_space: str = "image_pixels",
) -> Screenshot:
    """Scale, annotate and wrap a raw capture."""
    raw_w, raw_h = raw.size
    scale = 1.0
    img = raw
    if max_width is not None and raw_w > max_width:
        scale = max_width / raw_w
        img = raw.resize((max_width, max(1, int(round(raw_h * scale)))), Image.LANCZOS)
    if img.mode != "RGB":
        img = img.convert("RGB")
    origin = (geometry.left, geometry.top) if geometry else (0, 0)
    shot = Screenshot(image=img, raw_size=(raw_w, raw_h), scale=scale, origin=origin, fmt=fmt, quality=quality,
                      coordinate_space=coordinate_space)
    if cursor is not None and (origin[0] <= cursor[0] < origin[0] + raw_w and origin[1] <= cursor[1] < origin[1] + raw_h):
        cx, cy = shot.to_image(*cursor)
        # Rounding a physical edge pixel can produce image.width/height after downscaling.
        # Only clamp when the physical cursor is genuinely inside this capture.
        cx, cy = min(img.width - 1, max(0, cx)), min(img.height - 1, max(0, cy))
        shot.image = draw_cursor(shot.image, cx, cy)
    if grid:
        shot.image = draw_grid(shot.image, spacing=grid_spacing, coordinate_space=coordinate_space)
    return shot
