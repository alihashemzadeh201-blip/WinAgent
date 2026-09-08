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
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from .backends.base import ScreenGeometry


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
        """Convert model coordinates using the ACTUAL encoded image dimensions on each axis."""
        px = int(round(x * self.raw_size[0] / self.image.width)) + self.origin[0]
        py = int(round(y * self.raw_size[1] / self.image.height)) + self.origin[1]
        return px, py

    def to_image(self, x: float, y: float) -> tuple[int, int]:
        """Convert physical coordinates to image pixels (the inverse of to_physical)."""
        ix = int(round((x - self.origin[0]) * self.image.width / self.raw_size[0]))
        iy = int(round((y - self.origin[1]) * self.image.height / self.raw_size[1]))
        return ix, iy

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
                       f"scale x={self.scale_x:.6f}, y={self.scale_y:.6f}). ")
        if self.is_region:
            return description + ("ZOOMED detail for inspection only; pointer actions still use the LAST FULL screenshot. "
                                  "Take a full screenshot before clicking this area; do not use this crop's local coordinates.")
        return description + (
            "Use image pixels as given: display DPI and scaling are already handled; do not add window/title-bar/taskbar offsets. "
            f"Coordinates you send must be in this {w}x{h} space: x from 0 (left) to {w - 1} (right), "
            f"y from 0 (top) to {h - 1} (bottom).")


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except Exception:  # pragma: no cover
        return ImageFont.load_default()


def draw_grid(img: Image.Image, spacing: int = 100, color=(255, 0, 0)) -> Image.Image:
    """Overlay a labelled coordinate grid (in *image* coordinates)."""
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
    for x in range(spacing, w, spacing):
        draw.line([(x, 0), (x, h)], fill=line, width=1)
        txt = str(x)
        tw = draw.textlength(txt, font=font)
        draw.rectangle([x + 2, 2, x + 6 + tw, 16], fill=label_bg)
        draw.text((x + 4, 2), txt, fill=label_fg, font=font)
    for y in range(spacing, h, spacing):
        draw.line([(0, y), (w, y)], fill=line, width=1)
        txt = str(y)
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
    max_width: int = 1280,
    grid: bool = True,
    grid_spacing: int = 100,
    cursor: Optional[tuple[int, int]] = None,
    fmt: str = "jpeg",
    quality: int = 70,
) -> Screenshot:
    """Scale, annotate and wrap a raw capture."""
    raw_w, raw_h = raw.size
    scale = 1.0
    img = raw
    if raw_w > max_width:
        scale = max_width / raw_w
        img = raw.resize((max_width, max(1, int(round(raw_h * scale)))), Image.LANCZOS)
    if img.mode != "RGB":
        img = img.convert("RGB")
    origin = (geometry.left, geometry.top) if geometry else (0, 0)
    shot = Screenshot(image=img, raw_size=(raw_w, raw_h), scale=scale, origin=origin, fmt=fmt, quality=quality)
    if cursor is not None:
        cx, cy = shot.to_image(*cursor)
        if 0 <= cx < img.width and 0 <= cy < img.height:
            shot.image = draw_cursor(shot.image, cx, cy)
    if grid:
        shot.image = draw_grid(shot.image, spacing=grid_spacing)
    return shot
