"""Execute :class:`ToolCall` objects against a desktop backend.

Responsibilities:

* validate / coerce arguments coming from the model,
* convert screenshot coordinates to physical pixels,
* enforce safety policies (dangerous command confirmation, disabled features),
* attach a fresh screenshot to results when appropriate,
* produce compact JSON results for the model.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..backends.base import BackendError, DesktopBackend, EmergencyStop, ScreenGeometry
from ..config import COORDINATE_SPACES, Config
from ..keys import parse_key_combo
from ..protocol import ToolCall
from ..screenshot import (DEDUPE_HAMMING_LIMIT, Screenshot, average_hash, hash_distance, prepare_screenshot,
                          render_signature, resolve_format)
from ..screenguard import ScreenGuard, rect_from_points
from .definitions import TOOLS_BY_NAME

log = logging.getLogger(__name__)
_CURRENT_FRAME = object()


class CoordinateFrameChanged(BackendError):
    """Screen geometry no longer matches the image used to choose coordinates."""


def _bounds(geometry: ScreenGeometry) -> tuple[int, int, int, int]:
    return geometry.left, geometry.top, geometry.width, geometry.height

# Patterns that mark a shell command as destructive -> confirmation required.
DANGEROUS_COMMAND_PATTERNS = [
    r"\brm\b", r"\bdel\b", r"\berase\b", r"\brmdir\b", r"\brd\b", r"remove-item", r"\bri\b",
    r"format(-volume)?\b", r"\bdiskpart\b", r"\bcipher\b\s+/w", r"\bshutdown\b", r"\brestart-computer\b",
    r"stop-computer", r"\bbcdedit\b", r"\breg\s+(delete|add)\b", r"remove-itemproperty", r"set-itemproperty",
    r"new-itemproperty", r"\bnet\s+user\b", r"\bnet\s+localgroup\b", r"\btakeown\b", r"\bicacls\b",
    r"\bsfc\b", r"\bdism\b", r"stop-process", r"\btaskkill\b", r"\bkill\b", r"stop-service", r"\bsc\s+(delete|config)\b",
    r"set-executionpolicy", r"invoke-expression", r"\biex\b", r"invoke-webrequest.*\|\s*iex", r"\bcurl\b.*\|\s*(iex|sh)",
    r"clear-recyclebin", r"remove-appx", r"uninstall", r"\bwmic\b.*delete", r"disable-", r"\bschtasks\b.*/delete",
    r"\bnetsh\b", r"new-netfirewallrule", r"remove-netfirewallrule", r"\bmove-item\b", r"\bmv\b", r"\bmove\b",
    r"\brename-item\b", r"\bren\b", r">\s*[a-z]:\\", r"out-file", r"set-content", r"add-content",
]
_DANGEROUS_RE = re.compile("|".join(DANGEROUS_COMMAND_PATTERNS), re.I)


@dataclass
class ToolResult:
    call: ToolCall
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    screenshot: Optional[Screenshot] = None
    duration: float = 0.0
    task_complete: bool = False
    ask_user: Optional[dict[str, Any]] = None
    denied: bool = False
    needs_new_screenshot: bool = False

    def to_text(self, include_screenshot_note: bool = True) -> str:
        payload: dict[str, Any] = {"ok": self.ok}
        if self.error:
            payload["error"] = self.error
        payload.update(self.data)
        if self.needs_new_screenshot:
            payload["needs_new_screenshot"] = True
        if self.screenshot is not None and include_screenshot_note:
            payload["screenshot"] = self.screenshot.describe()
        return json.dumps(payload, ensure_ascii=False, default=str)

    def summary(self) -> str:
        """One-line summary for the GUI log."""
        if self.denied:
            return "denied by user"
        if not self.ok:
            return f"error: {self.error}"[:300]
        for key in ("message", "summary", "result", "status"):
            if key in self.data:
                return str(self.data[key])[:300]
        return "ok"


ConfirmCallback = Callable[[ToolCall, str], bool]
AskUserCallback = Callable[[str, list[str]], Optional[str]]


class ToolExecutor:
    def __init__(self, backend: DesktopBackend, config: Config, *, confirm: Optional[ConfirmCallback] = None,
                 stop_event=None, guard: Optional[ScreenGuard] = None):
        self.backend = backend
        self.config = config
        self.confirm = confirm
        self.stop_event = stop_event
        # keeps the agent's own GUI out of screenshots / from under the mouse (no-op outside the GUI)
        self.guard: ScreenGuard = guard or ScreenGuard()
        self.last_screenshot: Optional[Screenshot] = None
        self.screenshot_count = 0
        self.last_capture_unchanged = False   # set by take_screenshot(): the capture was a duplicate
        self._bound_frame = _CURRENT_FRAME
        self._bound_coordinate_space: Optional[str] = None
        self._coordinate_mappings: list[dict[str, Any]] = []

    @property
    def input_screenshot(self) -> Optional[Screenshot]:
        # An explicit None is important: an unseen capture must not invent an input frame mid-batch.
        return self.last_screenshot if self._bound_frame is _CURRENT_FRAME else self._bound_frame

    @property
    def input_coordinate_space(self) -> str:
        frame = self.input_screenshot
        space = frame.coordinate_space if frame is not None else (self._bound_coordinate_space or self.config.coordinate_space)
        if space not in COORDINATE_SPACES:
            raise BackendError(f"Invalid coordinate_space: {space}. Choose an explicit supported convention.")
        return space

    def _model_coord_int(self, value: Any, name: str) -> int:
        if self.input_coordinate_space != "normalized_1000":
            return _to_int(value, name)
        # Do not quietly turn a 0-1 coordinate (0.5) or an explicit "500px" into normalized units.
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a whole normalized 0-1000 value, without a px suffix.") from exc
        if isinstance(value, bool) or not number.is_integer():
            raise ValueError(f"{name} must be a whole normalized 0-1000 value, not fractional/0-1 coordinates.")
        return int(number)

    def _check_frame(self) -> None:
        frame = self.input_screenshot
        if frame is None or frame.desktop_bounds is None:
            return
        current = _bounds(self.backend.screen_geometry(all_screens=frame.all_screens))
        if current != frame.desktop_bounds:
            raise CoordinateFrameChanged(
                f"Display geometry changed since frame {frame.frame_id}: {frame.desktop_bounds} -> {current}. "
                "No pointer input was sent. Take a fresh full screenshot and choose new coordinates; "
                "do not rescale coordinates from the old screen layout.")

    @contextlib.contextmanager
    def _pointer_guard(self, **kwargs):
        with self.guard.shield(**kwargs):
            # The GUI/desktop may change while the guard hides windows. Check at the input boundary.
            self._check_stop()
            self._check_frame()
            yield

    def _menu_open_guard(self) -> None:
        """Block pointer input while a menu is open (menu bar / submenu / context menu).

        Moving the pointer over an open menu – or clicking into it – DISMISSES the menu instead of
        selecting anything; that is why menus "close by themselves" when the model tries the mouse.
        Refuse the action and show the keyboard route plus the currently visible items.
        """
        if not self.backend.menu_open():
            return
        try:
            items = self.backend.open_menu_items()
        except Exception:
            items = []
        labels = [str(i.get("text")) for i in items if i.get("text")][:12]
        raise BackendError(
            "A menu is currently open on screen, so pointer input is blocked: moving or clicking with "
            "the mouse while a menu is open dismisses the menu WITHOUT selecting the item. Choose it "
            "with the keyboard instead – the `menu` tool (action=\"select\", item=\"<name>\") or press_keys "
            "(Down/Up = move, Right = open a submenu, Enter = confirm) – or press Esc to close the menu "
            f"without choosing. Visible items: {', '.join(labels)}."
            " No pointer input was sent.")

    # ------------------------------------------------------------ screenshot
    def take_screenshot(self, *, region: Optional[list[int]] = None, all_screens: bool = False,
                        grid: Optional[bool] = None) -> Screenshot:
        cfg = self.config
        frame = self.input_screenshot
        capture_all = frame.all_screens if region and frame is not None else all_screens
        geometry = self.backend.screen_geometry(all_screens=capture_all)
        before = _bounds(geometry)
        phys_region = None
        if region:
            if frame is None:
                raise BackendError("Take a full screenshot before requesting a region.")
            self._check_frame()
            l, t = frame.model_to_physical(region[0], region[1], edge=True)
            r, b = frame.model_to_physical(region[2], region[3], edge=True)
            l, r = sorted((l, r))
            t, b = sorted((t, b))
            if r - l < 8 or b - t < 8:
                raise BackendError("Region is too small.")
            phys_region = (l, t, r, b)
        # a region is expressed in absolute coordinates -> grab the whole virtual screen and crop
        capture_rect = phys_region or (geometry.left, geometry.top, geometry.left + geometry.width, geometry.top + geometry.height)
        with self.guard.shield(rect=capture_rect, for_capture=True):   # our own GUI must not appear in the picture
            raw = self.backend.capture(all_screens=capture_all or phys_region is not None, region=phys_region)
            after = _bounds(self.backend.screen_geometry(all_screens=capture_all))
        expected_size = ((phys_region[2] - phys_region[0], phys_region[3] - phys_region[1])
                         if phys_region else (geometry.width, geometry.height))
        if before != after or raw.size != expected_size:
            raise CoordinateFrameChanged(
                f"Unstable or inconsistent screen capture: before={before}, after={after}, "
                f"expected pixels={expected_size}, captured pixels={raw.size}. "
                "Take a new full screenshot after the display settles; do not guess a scale or offset.")
        effective_grid = cfg.screenshot_grid if grid is None else bool(grid)
        max_width = None if cfg.screenshot_native_resolution else cfg.screenshot_max_width
        # 'auto' picks lossless PNG for flat UI screens and JPEG for photo/3D content; the CONCRETE
        # format feeds both the duplicate signature and the encoder so a deduped frame matches exactly.
        fmt = resolve_format(raw, cfg.screenshot_format)
        # Duplicate suppression: if the desktop is perceptually unchanged (same scope/bounds AND same
        # rendering settings), reuse the previous frame so the agent does not re-send an identical image.
        prev = self.last_screenshot
        if (phys_region is None and self.config.dedupe_screenshots and prev is not None
                and prev.desktop_bounds == before and prev.all_screens == capture_all and prev.phash):
            sig = render_signature(raw.size, max_width=max_width, fmt=fmt,
                                   quality=cfg.screenshot_jpeg_quality, subsampling=cfg.screenshot_jpeg_subsampling,
                                   grid=effective_grid, grid_spacing=cfg.screenshot_grid_spacing,
                                   cursor=cfg.screenshot_show_cursor, coordinate_space=cfg.coordinate_space)
            distance = hash_distance(prev.phash, average_hash(raw))
            if prev.render_sig == sig and distance <= DEDUPE_HAMMING_LIMIT:
                self.last_capture_unchanged = True
                self.screenshot_count += 1  # a capture was still taken; only the re-send is skipped
                log.info("Screenshot identical to frame %s (hash distance %d) – reusing previous frame.",
                         prev.frame_id, distance)
                return prev
        self.last_capture_unchanged = False
        cursor = None
        if cfg.screenshot_show_cursor:
            try:
                cursor = self.backend.mouse_position()
            except Exception:
                cursor = None
        if phys_region:
            geometry = ScreenGeometry(phys_region[0], phys_region[1], phys_region[2] - phys_region[0], phys_region[3] - phys_region[1])
        shot = prepare_screenshot(
            raw, geometry=geometry, max_width=max_width,
            grid=effective_grid,
            grid_spacing=cfg.screenshot_grid_spacing, cursor=cursor,
            fmt=fmt, quality=cfg.screenshot_jpeg_quality,
            subsampling=cfg.screenshot_jpeg_subsampling, coordinate_space=cfg.coordinate_space,
        )
        shot.is_region = phys_region is not None
        shot.all_screens = capture_all
        shot.desktop_bounds = before
        if phys_region is None:
            self.last_screenshot = shot  # regions don't replace the coordinate frame
        self.screenshot_count += 1
        log.info("Captured frame=%s coordinate_space=%s scope=%s origin=%s physical=%s image=%s scale=(%.6f,%.6f)",
                 shot.frame_id, shot.coordinate_space, "region" if shot.is_region else ("all monitors" if shot.all_screens else "primary"),
                 shot.origin, shot.raw_size, shot.size, shot.scale_x, shot.scale_y)
        return shot

    # --------------------------------------------------------------- helpers
    def _phys(self, x: Any, y: Any) -> tuple[int, int]:
        xi, yi = self._model_coord_int(x, "x"), self._model_coord_int(y, "y")
        frame = self.input_screenshot
        if frame is None:
            if self.input_coordinate_space == "normalized_1000":
                raise CoordinateFrameChanged("Normalized coordinates require a full screenshot with known dimensions. "
                                             "No pointer input was sent; take a full screenshot first.")
            return xi, yi  # explicitly physical coordinates without an image, never guess a normalization factor
        self._check_frame()
        w, h = frame.size
        max_x, max_y = frame.model_limits
        if not (0 <= xi <= max_x and 0 <= yi <= max_y):
            raise BackendError(f"Coordinates ({xi},{yi}) are outside {frame.coordinate_space} bounds "
                               f"(x=0..{max_x}, y=0..{max_y}) for screenshot {w}x{h}. "
                               "Use the declared coordinate space; do not mix pixels and normalized units.")
        px, py = frame.model_to_physical(xi, yi)
        image_point = [xi, yi]
        if frame.coordinate_space == "normalized_1000":
            ix, iy = frame.to_image(px, py)
            image_point = [min(w - 1, max(0, ix)), min(h - 1, max(0, iy))]
        mapping = {"frame_id": frame.frame_id, "coordinate_space": frame.coordinate_space, "model_point": [xi, yi],
                   "image_point": image_point, "image_size": list(frame.size),
                   "capture_size": list(frame.raw_size), "origin": list(frame.origin), "physical_target": [px, py]}
        self._coordinate_mappings.append(mapping)
        log.info("Pointer mapping: %s", mapping)
        return px, py

    def _pointer(self) -> Optional[tuple[int, int]]:
        try:
            x, y = self.backend.mouse_position()
            return int(x), int(y)
        except Exception:
            return None

    def _keyboard_guard(self):
        """Hide our GUI while typing when it holds the keyboard focus (otherwise the text would land in the chat box)."""
        if self.guard.ui_has_focus():
            return self.guard.shield()
        return contextlib.nullcontext()

    def _ensure_keyboard_layout(self) -> Optional[str]:
        """Check the active input language and correct it when needed.

        Always called before any keyboard input (and once at the start of every task), so the
        machine is in the preferred language state BEFORE anything happens. The mismatch is
        reported to the model even when auto-correction is disabled; the switch itself only
        happens when the setting is on. Returns a note (None when the layout is already correct).
        Letter shortcuts, menu mnemonics and type-ahead depend on the layout; type_text does not (Unicode).
        """
        cfg = self.config
        preferred = (cfg.preferred_keyboard_layout or "").strip()
        if not preferred or preferred.lower() in ("any", "auto", "off"):
            return None
        try:
            active = self.backend.active_keyboard_layout()
        except Exception as exc:  # pragma: no cover - backend without layout support
            log.debug("active_keyboard_layout unavailable: %s", exc)
            return None
        if not active or active == "unknown" or active.lower() == preferred.lower():
            return None
        if not cfg.auto_fix_keyboard_layout:
            return (f"Keyboard layout is '{active}' but the preferred layout is '{preferred}' and auto-correction "
                    "is disabled. Letter shortcuts and menu type-ahead may produce unexpected characters; "
                    "prefer type_text/clipboard for text and press only physical-key shortcuts.")
        try:
            now = self.backend.set_keyboard_layout(preferred)
        except Exception as exc:
            return (f"Keyboard layout is '{active}' but switching to the preferred '{preferred}' failed ({exc}). "
                    "Letter keys and menu shortcuts may produce unexpected characters; prefer type_text/clipboard for text.")
        if now and now.lower() == preferred.lower():
            return f"Keyboard layout was '{active}'; switched to '{now}'."
        return (f"Keyboard layout is '{active}' and the preferred '{preferred}' could not be activated "
                f"(now: {now or 'unknown'}); letter keys may produce unexpected characters "
                "(prefer type_text/clipboard for text).")

    def _check_stop(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise EmergencyStop("Stopped by user.")
        if self.config.mouse_failsafe:
            try:
                x, y = self.backend.mouse_position()
            except Exception:
                return
            # Negative coordinates are valid on monitors above/left of the primary, not a whole fail-safe quadrant.
            at_corner = (x, y) == (0, 0)
            if not at_corner and (x < 0 or y < 0):
                try:
                    virtual = self.backend.screen_geometry(all_screens=True)
                    at_corner = (x, y) == (virtual.left, virtual.top)
                except Exception:
                    pass
            if at_corner:
                raise EmergencyStop("Fail-safe triggered: the mouse was moved to the top-left corner of the screen.")

    def _find_window(self, args: dict[str, Any]):
        hwnd = args.get("hwnd")
        title = args.get("title") or args.get("name") or args.get("window")
        if hwnd in ("", None) and not title:
            win = self.backend.active_window()
            if win is None:
                raise BackendError("No active window and no title/hwnd given.")
            return win
        win = self.backend.find_window(title=title, hwnd=int(hwnd) if hwnd not in ("", None) else None)
        if win is None:
            titles = [w.title for w in self.backend.list_windows()][:15]
            raise BackendError(f"No window matching title={title!r} hwnd={hwnd!r}. Open windows: {titles}")
        return win

    def _window_dict(self, w, *, frame=_CURRENT_FRAME) -> dict[str, Any]:
        d = w.to_dict()
        frame = self.input_screenshot if frame is _CURRENT_FRAME else frame
        if frame is not None:
            d["screenshot_rect"] = [*frame.to_model(w.left, w.top), *frame.to_model(w.right, w.bottom)]
            d["screenshot_frame_id"] = frame.frame_id
            d["coordinate_space"] = frame.coordinate_space
        return d

    def _require_confirm(self, call: ToolCall, reason: str) -> bool:
        if not self.config.confirm_dangerous_actions:
            return True
        if self.confirm is None:
            return False
        return bool(self.confirm(call, reason))

    # ---------------------------------------------------------------- execute
    def execute(self, call: ToolCall, *, coordinate_frame=_CURRENT_FRAME, coordinate_space: Optional[str] = None) -> ToolResult:
        """Bind every call in a model response to the image supplied BEFORE that response.

        Captures still update last_screenshot for output, but can never reinterpret this call's input.
        Direct users of the executor retain the default of using the latest full capture.
        """
        previous, mappings, previous_space = self._bound_frame, self._coordinate_mappings, self._bound_coordinate_space
        self._bound_coordinate_space = coordinate_space or self.config.coordinate_space
        self._bound_frame = self.last_screenshot if coordinate_frame is _CURRENT_FRAME else coordinate_frame
        self._coordinate_mappings = []
        try:
            result = self._execute(call)
            if self._coordinate_mappings:
                result.data["coordinate_mapping"] = list(self._coordinate_mappings)
            return result
        finally:
            self._bound_frame, self._coordinate_mappings, self._bound_coordinate_space = previous, mappings, previous_space

    def _execute(self, call: ToolCall) -> ToolResult:
        start = time.time()
        spec = TOOLS_BY_NAME.get(call.name)
        if spec is None:
            # tolerate a few common aliases
            alias = _ALIASES.get(call.name.lower().replace("-", "_"))
            if alias:
                call = ToolCall(name=alias, arguments=call.arguments, id=call.id)
                spec = TOOLS_BY_NAME[alias]
            else:
                return ToolResult(call, False, error=f"Unknown tool '{call.name}'. Available: {sorted(TOOLS_BY_NAME)}",
                                  duration=time.time() - start)
        try:
            self._check_stop()
            args = call.arguments or {}
            declares_space = args.get("coordinate_space")
            if (declares_space is not None and (spec.category == "mouse" or (call.name == "screenshot" and args.get("region")))
                    and declares_space != self.input_coordinate_space):
                raise BackendError(f"The tool arguments declare {declares_space!r} coordinates, but this response uses "
                                   f"{self.input_coordinate_space}. No input was sent; follow the declared tool schema.")
            handler = getattr(self, f"_t_{call.name}")
            with self.guard.scope():   # hide our GUI at most once per tool call, restore when the call is over
                result = handler(call, call.arguments or {})
                # automatic screenshot after UI actions
                if (spec.screenshot_after and self.config.auto_screenshot_after_action and result.ok
                        and result.screenshot is None and not result.task_complete and result.ask_user is None):
                    try:
                        time.sleep(max(0.0, self.config.action_delay))
                        frame = self.input_screenshot
                        result.screenshot = self.take_screenshot(all_screens=frame.all_screens if frame else False)
                        if self.last_capture_unchanged and frame is not None:
                            result.data["screen_unchanged"] = True
                            if spec.category == "keyboard":
                                # Keyboard input often changes only a few pixels (typed characters) that can fall
                                # below the duplicate-detection threshold, so "unchanged" is NOT proof of failure.
                                result.data["note"] = (f"The screen looks unchanged (identical to full frame {frame.frame_id} "
                                                       "within tolerance); no new image is attached. Small changes (a few "
                                                       "typed characters, a caret) can fall below this detection threshold – "
                                                       "verify the exact state (zoomed region screenshot or get_window_controls) "
                                                       "before repeating the input.")
                            else:
                                result.data["note"] = (f"The screen is unchanged (identical to full frame {frame.frame_id} "
                                                       "within tolerance); no new image is attached. The action had no "
                                                       "visible effect – do not repeat it, change approach or skip the step.")
                    except CoordinateFrameChanged as exc:
                        # The action already succeeded. Keep ok=True; don't invite its replay just because capture failed.
                        result.data["screenshot_error"] = str(exc)
                        result.needs_new_screenshot = True
                    except Exception as exc:  # pragma: no cover
                        result.data["screenshot_error"] = str(exc)
        except EmergencyStop:
            raise
        except CoordinateFrameChanged as exc:
            result = ToolResult(call, False, error=str(exc), needs_new_screenshot=True)
        except BackendError as exc:
            result = ToolResult(call, False, error=str(exc))
        except (ValueError, TypeError, KeyError) as exc:
            result = ToolResult(call, False, error=f"Invalid arguments for {call.name}: {exc}")
        except PermissionError as exc:
            result = ToolResult(call, False, error=f"Permission denied: {exc}")
        except OSError as exc:
            result = ToolResult(call, False, error=f"OS error: {exc}")
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("Tool %s crashed", call.name)
            result = ToolResult(call, False, error=f"{type(exc).__name__}: {exc}")
        result.duration = time.time() - start
        return result

    # ------------------------------------------------------------ perception
    def _t_screenshot(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        region = a.get("region")
        if region is not None:
            if not (isinstance(region, (list, tuple)) and len(region) == 4):
                raise ValueError("region must be [left, top, right, bottom]")
            region = [self._model_coord_int(v, "region") for v in region]
        shot = self.take_screenshot(region=region, all_screens=bool(a.get("all_screens", False)), grid=a.get("grid"))
        data: dict[str, Any] = {"message": "Screenshot captured."}
        if self.last_capture_unchanged:
            data["screen_unchanged"] = True
            data["note"] = (f"The screen is unchanged since full frame {shot.frame_id} (already in the context); "
                            "the identical image was not re-sent. Treat this as strong evidence that nothing changed "
                            "(only tiny changes can fall below the detection threshold).")
        frame = self.input_screenshot if shot.is_region else shot
        try:
            win = self.backend.active_window()
            if win:
                data["active_window"] = self._window_dict(win, frame=frame)
            data["mouse"] = list(frame.to_model(*self.backend.mouse_position())) if frame else None
            data["screenshot_frame_id"] = frame.frame_id if frame else None
            data["coordinate_space"] = frame.coordinate_space if frame else self.input_coordinate_space
        except Exception:
            pass
        if region is not None:
            data["note"] = ("This is a ZOOMED detail for inspection, not a new click coordinate frame. "
                            "Pointer actions still use the last full screenshot. Take screenshot without region "
                            "before clicking an element found in this crop.")
            data["region"] = region
        return ToolResult(call, True, data, screenshot=shot)

    def _t_get_screen_info(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        frame = self.input_screenshot
        info = self.backend.system_info()
        active = self.backend.active_window()
        windows = [self._window_dict(w) for w in self.backend.list_windows()[:40]]
        mouse = self.backend.mouse_position()
        data = {
            "system": info,
            "coordinate_space": self.input_coordinate_space,
            "mouse_physical": list(mouse),
            "mouse_screenshot": list(frame.to_model(*mouse)) if frame else None,
            "screenshot_frame": frame.describe() if frame else "no screenshot taken yet",
            "active_window": self._window_dict(active) if active else None,
            "windows": windows,
        }
        return ToolResult(call, True, data)

    # ----------------------------------------------------------------- mouse
    def _t_mouse_move(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        self._menu_open_guard()
        x, y = self._phys(a.get("x"), a.get("y"))
        with self._pointer_guard(point=(x, y)):
            self.backend.mouse_move(x, y, duration=0.25)
        return ToolResult(call, True, {"message": f"Mouse moved to physical ({x},{y})."})

    def _t_click(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        self._menu_open_guard()
        button = str(a.get("button") or "left").lower()
        clicks = _to_int(a.get("clicks", 1), "clicks")
        clicks = min(max(clicks, 1), 3)
        mods = a.get("modifiers") or []
        if isinstance(mods, str):
            mods = parse_key_combo(mods)
        if a.get("x") is None or a.get("y") is None:
            with self._pointer_guard(point=self._pointer()):
                self.backend.mouse_click(None, None, button=button, clicks=clicks, modifiers=list(mods))
            where = "at current pointer position"
        else:
            x, y = self._phys(a.get("x"), a.get("y"))
            with self._pointer_guard(point=(x, y)):   # never click on our own window
                self.backend.mouse_click(x, y, button=button, clicks=clicks, modifiers=list(mods))
            where = f"at physical ({x},{y})"
        kind = {1: "Clicked", 2: "Double-clicked", 3: "Triple-clicked"}[clicks]
        return ToolResult(call, True, {"message": f"{kind} {button} {where}."})

    def _t_double_click(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        return self._t_click(call, {**a, "clicks": 2, "button": "left"})

    def _t_right_click(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        return self._t_click(call, {**a, "clicks": 1, "button": "right"})

    def _t_drag(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        self._menu_open_guard()
        x1, y1 = self._phys(a.get("x1"), a.get("y1"))
        x2, y2 = self._phys(a.get("x2"), a.get("y2"))
        duration = float(a.get("duration") or 0.5)
        with self._pointer_guard(rect=rect_from_points((x1, y1), (x2, y2), margin=4)):
            self.backend.mouse_drag(x1, y1, x2, y2, button=str(a.get("button") or "left"), duration=min(max(duration, 0.1), 5.0))
        return ToolResult(call, True, {"message": f"Dragged from ({x1},{y1}) to ({x2},{y2}) physical px."})

    def _t_scroll(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        self._menu_open_guard()
        amount = _to_int(a.get("amount", a.get("clicks", -3)), "amount")
        amount = max(-50, min(50, amount))
        direction = str(a.get("direction") or "vertical").lower()
        x = y = None
        if a.get("x") is not None and a.get("y") is not None:
            x, y = self._phys(a.get("x"), a.get("y"))
        with self._pointer_guard(point=(x, y) if x is not None else self._pointer()):
            if direction.startswith("h"):
                self.backend.mouse_scroll(x, y, dx=amount, dy=0)
            else:
                self.backend.mouse_scroll(x, y, dx=0, dy=amount)
        return ToolResult(call, True, {"message": f"Scrolled {direction} by {amount} clicks."})

    # -------------------------------------------------------------- keyboard
    _START_SEARCH_PROCS = {"searchhost.exe", "searchapp.exe", "searchui.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe"}

    def _start_search_note(self) -> Optional[str]:
        """Warn the model when it is typing into Start search / the Run box instead of using open_app."""
        try:
            win = self.backend.active_window()
        except Exception:
            return None
        if win is None:
            return None
        proc = (win.process_name or "").lower()
        title = (win.title or "").lower()
        if proc in self._START_SEARCH_PROCS or win.class_name == "StartMenu" or (proc == "explorer.exe" and title == "run"):
            return ("You are typing into Windows Start search / the Run box. This is unreliable for launching programs "
                    "(the highlighted result may be a different app or a web search). Prefer `open_app`. If you continue, "
                    "take a screenshot and read the highlighted result BEFORE pressing Enter, then verify the window title.")
        return None

    def _t_type_text(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        text = a.get("text")
        if text is None:
            text = a.get("value", "")
        text = str(text)
        interval = float(a.get("interval") or 0.0)
        note = self._start_search_note()
        layout_note = self._ensure_keyboard_layout()
        with self._keyboard_guard():
            self.backend.type_text(text, interval=min(max(interval, 0.0), 0.5))
            if a.get("press_enter"):
                time.sleep(0.1)
                self.backend.press_keys(["enter"])
        data: dict[str, Any] = {"message": f"Typed {len(text)} characters" + (" and pressed Enter." if a.get("press_enter") else ".")}
        if note:
            data["note"] = note
        if layout_note:
            data["layout"] = layout_note
        return ToolResult(call, True, data)

    def _t_press_keys(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        keys = a.get("keys") or a.get("key") or a.get("combination") or a.get("value")
        if keys is None:
            raise ValueError("'keys' is required")
        combo = parse_key_combo(keys)
        repeat = min(max(_to_int(a.get("repeat", 1), "repeat"), 1), 50)
        layout_note = self._ensure_keyboard_layout()
        with self._keyboard_guard():
            self.backend.press_keys(combo, repeat=repeat)
        data: dict[str, Any] = {"message": f"Pressed {'+'.join(combo)}" + (f" x{repeat}" if repeat > 1 else "") + "."}
        if [k for k in combo if k not in ("winleft", "winright")] in (["win"], ["win", "r"], ["win", "s"], ["win", "q"]):
            data["note"] = ("Start menu / Run / Search opened. If your goal is to launch a program, use `open_app` instead – "
                            "it is far more reliable. If you continue here, read the highlighted result on a screenshot "
                            "before pressing Enter and verify the resulting window title.")
        if layout_note:
            data["layout"] = layout_note
        return ToolResult(call, True, data)

    def _t_hotkey_sequence(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        seq = a.get("sequence") or a.get("keys") or []
        if isinstance(seq, str):
            seq = [s.strip() for s in re.split(r"[,;]|\bthen\b", seq) if s.strip()]
        delay = min(max(float(a.get("delay") or 0.3), 0.0), 5.0)
        layout_note = self._ensure_keyboard_layout()
        done = []
        with self._keyboard_guard():
            for item in seq:
                self._check_stop()
                combo = parse_key_combo(item)
                self.backend.press_keys(combo)
                done.append("+".join(combo))
                time.sleep(delay)
        data: dict[str, Any] = {"message": f"Pressed sequence: {', '.join(done)}."}
        if layout_note:
            data["layout"] = layout_note
        return ToolResult(call, True, data)

    # ------------------------------------------------------------------ menus
    @staticmethod
    def _menu_name(item: dict[str, Any]) -> str:
        return re.sub(r"&", "", str(item.get("text") or "")).strip().lower()

    def _menu_find(self, items: list[dict[str, Any]], name: str) -> Optional[dict[str, Any]]:
        """Case/''-insensitive exact match first, then unique prefix, then unique substring."""
        want = re.sub(r"&", "", str(name or "")).strip().lower()
        if not want:
            return None
        cands = [i for i in items if not i.get("separator") and self._menu_name(i)]
        for i in cands:
            if self._menu_name(i) == want:
                return i
        prefix = [i for i in cands if self._menu_name(i).startswith(want)]
        if len(prefix) == 1:
            return prefix[0]
        sub = [i for i in cands if want in self._menu_name(i)]
        if len(sub) == 1:
            return sub[0]
        return None

    @staticmethod
    def _typeahead_prefix(target: str, labels: list[str]) -> str:
        """Shortest prefix of ``target`` that uniquely identifies it among ``labels``."""
        want = re.sub(r"&", "", str(target or "")).strip().lower()
        pool = [l for l in labels if l]
        for i in range(1, len(want) + 1):
            p = want[:i]
            if sum(1 for l in pool if l.startswith(p)) == 1:
                return str(target).strip()[:i]
        return str(target).strip()

    def _menu_typeahead(self, prefix: str) -> None:
        """Type characters one-by-one (Unicode) so any layout/menu language works."""
        for ch in prefix:
            self._check_stop()
            self.backend.type_text(ch)
            time.sleep(0.12)

    def _wait_popup(self, timeout: float = 1.0) -> list[dict[str, Any]]:
        """Poll until a popup menu (menu-bar dropdown / submenu) is open; return its items.

        Returns [] when the timeout passes with no popup – the caller then falls back to the
        pre-enumerated menu tree (the same structure the model saw with action='list').
        """
        deadline = time.time() + max(0.1, timeout)
        while True:
            self._check_stop()
            try:
                items = self.backend.open_menu_items()
            except Exception:
                items = []
            if items:
                return items
            if time.time() >= deadline:
                return []
            time.sleep(0.05)

    @staticmethod
    def _menu_labels(items: list[dict[str, Any]]) -> list[str]:
        return [ToolExecutor._menu_name(i) for i in items if not i.get("separator") and i.get("text")]

    def _t_menu(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        action = str(a.get("action") or ("select" if (a.get("path") or a.get("item")) else "list")).strip().lower()
        layout_note = self._ensure_keyboard_layout()
        if action == "close":
            with self._keyboard_guard():
                self.backend.press_keys(["esc"])
            data = {"message": "Menu closed (Esc)."}
            if layout_note:
                data["layout"] = layout_note
            return ToolResult(call, True, data)
        if action == "list":
            data: dict[str, Any] = {}
            popup = self.backend.open_menu_items()
            if popup:
                data["open_popup"] = popup
            win = None
            if a.get("window") or a.get("title") or a.get("hwnd"):
                win = self._find_window(a)
            else:
                try:
                    win = self.backend.active_window()
                except Exception:
                    win = None
            bar = self.backend.menu_structure(win.hwnd if win else None)
            if bar:
                data["menu_bar"] = bar
                if win:
                    data["menu_bar_window"] = self._window_dict(win)
            if not bar and not popup:
                data["note"] = ("No enumerable menu found for this window (modern/custom UI or no menu bar). "
                                "If a context menu is open, use action='select' with item=...; otherwise navigate with "
                                "press_keys (F10 then arrow keys) and a screenshot, or use the app's search command "
                                "(e.g. F3 in Blender).")
            return ToolResult(call, True, data)
        if action == "select":
            return self._menu_select(call, a, layout_note)
        raise ValueError("action must be 'list', 'select' or 'close'")

    def _menu_select(self, call: ToolCall, a: dict[str, Any], layout_note: Optional[str]) -> ToolResult:
        """Select a menu item using ONLY the keyboard.

        Keyboard sequence (never the mouse – hovering the pointer over an open menu dismisses it):
        open the top item with Alt+mnemonic (or F10 + type-ahead + Enter), then for each deeper
        level type the unique prefix of the matched item (type-ahead highlights it), press Right
        (arrow key) to open its submenu, or Enter to confirm the last item.
        """
        path_ = [str(p).strip() for p in (a.get("path") or []) if str(p).strip()]
        item_name = str(a.get("item") or "").strip()
        if not path_ and not item_name:
            raise ValueError("action='select' needs 'path' (menu bar) or 'item' (already-open popup).")
        steps: list[str] = []
        notes: list[str] = []

        if item_name and not path_:
            popup = self.backend.open_menu_items()
            if not popup:
                raise BackendError("No open menu/popup was detected. Right-click first, or use path=[...] to "
                                   "navigate from the menu bar.")
            target = self._menu_find(popup, item_name)
            if target is None:
                raise BackendError(f"Item {item_name!r} not found in the open menu. "
                                   f"Available: {self._menu_labels(popup)}")
            # Type the unique prefix of the MATCHED item's own name -> deterministic highlight.
            prefix = self._typeahead_prefix(str(target.get("text") or ""), self._menu_labels(popup))
            with self._keyboard_guard():
                self._menu_typeahead(prefix)
                time.sleep(0.1)
                self.backend.press_keys(["enter"])
            steps.append(f"type-ahead '{prefix}' + Enter")
            return ToolResult(call, True, {
                "message": f"Selected '{target.get('text')}' from the open menu (keyboard only).",
                "steps": steps, "layout": layout_note,
            })

        # menu-bar navigation: open the first item, then walk the rest with type-ahead + arrow keys
        win = None
        if a.get("window") or a.get("title") or a.get("hwnd"):
            win = self._find_window(a)
        else:
            try:
                win = self.backend.active_window()
            except Exception:
                win = None
        if win is None:
            raise BackendError("No active window to open a menu in.")
        structure = self.backend.menu_structure(win.hwnd)
        if not structure:
            raise BackendError("This window has no enumerable menu bar. If a popup is already open use item=...; "
                               "otherwise navigate with press_keys (F10, arrows) instead.")
        if not self.backend.focus_window(win.hwnd):
            notes.append(f"Could not bring '{win.title}' to the front; the menu may open in another window.")
        target = self._menu_find(structure, path_[0])
        if target is None:
            raise BackendError(f"Menu-bar item {path_[0]!r} not found. Available: {self._menu_labels(structure)}")
        if len(path_) > 1 and not target.get("items"):
            raise BackendError(f"Menu-bar item '{target.get('text')}' has no submenu, so the path "
                               f"'{' > '.join(path_)}' cannot continue. Available at level 1: {self._menu_labels(structure)}.")
        with self._keyboard_guard():
            if target.get("mnemonic"):
                self.backend.press_keys(["alt", target["mnemonic"]])
                steps.append(f"alt+{target['mnemonic']}")
            else:
                self.backend.press_keys(["f10"])
                steps.append("f10")
                prefix0 = self._typeahead_prefix(str(target.get("text") or ""), self._menu_labels(structure))
                self._menu_typeahead(prefix0)
                steps.append(f"type-ahead '{prefix0}'")
                self.backend.press_keys(["enter"])
                steps.append("enter")
            # wait until the top menu's popup is actually open, then walk the rest of the path
            current = self._wait_popup(timeout=1.0) or (target.get("items") or [])
            for depth in range(1, len(path_)):
                self._check_stop()
                labels = self._menu_labels(current)
                target = self._menu_find(current, path_[depth])
                if target is None:
                    raise BackendError(f"Item {path_[depth]!r} not found in the open menu at level {depth}. "
                                       f"Available: {labels}")
                # deterministic highlight: type the unique prefix of the matched item's own name
                prefix = self._typeahead_prefix(str(target.get("text") or ""), labels)
                self._menu_typeahead(prefix)
                steps.append(f"type-ahead '{prefix}'")
                if depth < len(path_) - 1:
                    if not target.get("items"):
                        raise BackendError(f"Item {path_[depth]!r} has no submenu, so the path cannot continue "
                                           f"to level {depth + 1}. Available at this level: {labels}.")
                    # ARROW KEY: open the highlighted item's submenu (never the mouse – hovering an
                    # open menu dismisses it). Wait until the submenu popup is actually open.
                    self.backend.press_keys(["right"])
                    steps.append("right (open submenu)")
                    time.sleep(0.15)
                    current = self._wait_popup(timeout=0.8) or (target.get("items") or [])
                else:
                    self.backend.press_keys(["enter"])
                    steps.append("enter")
        return ToolResult(call, True, {
            "message": f"Selected menu path {' > '.join(path_)} (keyboard only: type-ahead + arrow keys, no mouse).",
            "window": self._window_dict(win), "steps": steps,
            **({"layout": layout_note} if layout_note else {}),
            **({"note": " ".join(notes)} if notes else {}),
        })

    # -------------------------------------------------------------- programs
    def _t_open_app(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        name = a.get("name") or a.get("app") or a.get("path") or a.get("program") or a.get("value")
        if not name:
            raise ValueError("'name' is required")
        name = str(name).strip()
        args = str(a.get("args") or a.get("arguments") or "")
        try:
            before = {w.hwnd for w in self.backend.list_windows()}
            fg_before = self.backend.active_window()
        except Exception:
            before, fg_before = set(), None
        msg = self.backend.open_application(name, args)
        wait = min(max(float(3.0 if a.get("wait") in (None, "") else a.get("wait")), 0.0), 30.0)
        # poll instead of sleeping blindly: stop as soon as a new window has shown up (after >= 1s so it can paint)
        start, deadline = time.time(), time.time() + wait
        min_wait = min(1.0, wait)
        new_windows: list[Any] = []
        active = None
        while True:
            self._check_stop()
            try:
                active = self.backend.active_window()
                new_windows = [w for w in self.backend.list_windows() if w.hwnd not in before and not w.is_minimized]
            except Exception:
                new_windows = []
            now = time.time()
            if now >= deadline or (new_windows and now - start >= min_wait):
                break
            time.sleep(min(0.25, max(0.0, deadline - now)))
        data: dict[str, Any] = {"message": msg}
        if active:
            data["active_window"] = self._window_dict(active)
        fg_changed = bool(active) and (fg_before is None or active.hwnd != fg_before.hwnd)
        if new_windows:
            data["new_windows"] = [self._window_dict(w) for w in new_windows[:5]]
            data["launched"] = True
            if active and active.hwnd not in {w.hwnd for w in new_windows}:
                data["note"] = (f"A new window opened but the foreground window is still {active.title!r}; use "
                                "`focus_window` with the new window's hwnd if you need it in front.")
        elif fg_changed:
            data["launched"] = True
            data["note"] = (f"No new top-level window, but focus moved to {active.title!r} – most likely an already "
                            "running instance was activated. Confirm on the screenshot that it is the intended program.")
        else:
            data["launched"] = None
            data["note"] = ("No new window appeared within the wait time. The program may still be starting (call `wait` "
                            "then `screenshot`), or it may have failed silently (e.g. blocked by security software). "
                            "Verify with the screenshot before continuing; do not retry the launch blindly.")
        return ToolResult(call, True, data)

    def _t_open_url(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        url = a.get("url") or a.get("value")
        if not url:
            raise ValueError("'url' is required")
        msg = self.backend.open_url(str(url))
        time.sleep(2.0)
        return ToolResult(call, True, {"message": msg})

    def _t_run_command(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        if not self.config.allow_shell_commands:
            return ToolResult(call, False, error="Shell commands are disabled in settings.")
        command = a.get("command") or a.get("cmd") or a.get("value")
        if not command:
            raise ValueError("'command' is required")
        command = str(command)
        shell = str(a.get("shell") or "powershell")
        timeout = min(max(float(a.get("timeout") or 60.0), 1.0), 600.0)
        if _DANGEROUS_RE.search(command):
            if not self._require_confirm(call, f"Potentially destructive {shell} command:\n{command}"):
                return ToolResult(call, False, error="The user denied running this command.", denied=True)
        res = self.backend.run_command(command, shell=shell, timeout=timeout, cwd=a.get("cwd") or None)
        data = res.to_dict()
        data["stdout"] = _truncate(data["stdout"], 12000)
        data["stderr"] = _truncate(data["stderr"], 4000)
        ok = not res.timed_out
        return ToolResult(call, ok, data, error="Command timed out." if res.timed_out else "")

    # --------------------------------------------------------------- windows
    def _t_list_windows(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        wins = [self._window_dict(w) for w in self.backend.list_windows()[:60]]
        return ToolResult(call, True, {"count": len(wins), "windows": wins})

    def _t_focus_window(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        win = self._find_window(a)
        ok = self.backend.focus_window(win.hwnd)
        msg = f"Focused window {win.hwnd} '{win.title}'." if ok else f"Requested focus for '{win.title}' but Windows kept another window in front; try clicking it."
        return ToolResult(call, True, {"message": msg, "window": self._window_dict(win)})

    def _t_window_action(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        action = str(a.get("action") or "").lower()
        if not action:
            raise ValueError("'action' is required")
        win = self._find_window(a)
        msg = self.backend.window_action(win.hwnd, action, x=_opt_int(a.get("x")), y=_opt_int(a.get("y")),
                                         width=_opt_int(a.get("width")), height=_opt_int(a.get("height")))
        return ToolResult(call, True, {"message": msg})

    def _t_get_window_controls(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        win = self._find_window(a)
        limit = min(max(_to_int(a.get("limit", 150), "limit"), 1), 500)
        controls = self.backend.window_controls(win.hwnd, limit=limit)
        frame = self.input_screenshot
        out = []
        for c in controls:
            d = c.to_dict()
            if frame is not None:
                cx, cy = (c.left + c.right) // 2, (c.top + c.bottom) // 2
                d["screenshot_center"] = list(frame.to_model(cx, cy))
                d["screenshot_frame_id"] = frame.frame_id
                d["coordinate_space"] = frame.coordinate_space
            out.append(d)
        return ToolResult(call, True, {"window": self._window_dict(win), "count": len(out), "controls": out})

    # ------------------------------------------------------------- clipboard
    def _t_clipboard(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        action = str(a.get("action") or ("set" if "text" in a else "get")).lower()
        if action == "get":
            text = self.backend.clipboard_get()
            return ToolResult(call, True, {"text": _truncate(text, 20000), "length": len(text)})
        if action == "set":
            text = str(a.get("text") if a.get("text") is not None else a.get("value", ""))
            self.backend.clipboard_set(text)
            return ToolResult(call, True, {"message": f"Clipboard set ({len(text)} chars). Press ctrl+v to paste."})
        raise ValueError("action must be 'get' or 'set'")

    # ----------------------------------------------------------------- files
    def _t_read_file(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        path = _expand(a.get("path"))
        max_chars = min(max(_to_int(a.get("max_chars", 20000), "max_chars"), 100), 200000)
        p = Path(path)
        if not p.exists():
            raise BackendError(f"File not found: {path}")
        if p.is_dir():
            raise BackendError(f"{path} is a directory; use list_directory.")
        size = p.stat().st_size
        enc = a.get("encoding") or "utf-8"
        with open(p, "r", encoding=enc, errors="replace") as fh:
            content = fh.read(max_chars + 1)
        truncated = len(content) > max_chars
        return ToolResult(call, True, {"path": str(p), "size": size, "truncated": truncated, "content": content[:max_chars]})

    def _t_write_file(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        if not self.config.allow_file_write:
            return ToolResult(call, False, error="File writing is disabled in settings.")
        path = _expand(a.get("path"))
        content = str(a.get("content") if a.get("content") is not None else a.get("text", ""))
        append = bool(a.get("append", False))
        p = Path(path)
        if p.exists() and not append:
            if not self._require_confirm(call, f"Overwrite existing file?\n{p}"):
                return ToolResult(call, False, error="The user denied overwriting the file.", denied=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a" if append else "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        return ToolResult(call, True, {"message": f"{'Appended' if append else 'Wrote'} {len(content)} chars to {p}."})

    def _t_list_directory(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        path = _expand(a.get("path") or ".")
        limit = min(max(_to_int(a.get("limit", 200), "limit"), 1), 2000)
        p = Path(path)
        if not p.is_dir():
            raise BackendError(f"Not a directory: {path}")
        entries = []
        with os.scandir(p) as it:
            for e in sorted(it, key=lambda e: (not e.is_dir(), e.name.lower())):
                try:
                    st = e.stat()
                    entries.append({"name": e.name, "type": "dir" if e.is_dir() else "file",
                                    "size": None if e.is_dir() else st.st_size,
                                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
                except OSError:
                    entries.append({"name": e.name, "type": "unknown"})
                if len(entries) >= limit:
                    break
        return ToolResult(call, True, {"path": str(p), "count": len(entries), "entries": entries})

    # ------------------------------------------------------------------ flow
    def _t_wait(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        secs = min(max(float(a.get("seconds", a.get("value", 1.0)) or 0), 0.0), 60.0)
        end = time.time() + secs
        while time.time() < end:
            self._check_stop()
            time.sleep(min(0.2, end - time.time()))
        return ToolResult(call, True, {"message": f"Waited {secs:.1f}s."})

    def _t_ask_user(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        question = str(a.get("question") or a.get("text") or a.get("value") or "").strip()
        if not question:
            raise ValueError("'question' is required")
        options = a.get("options") or []
        if not isinstance(options, list):
            options = [str(options)]
        return ToolResult(call, True, {"message": "Waiting for the user's answer."},
                          ask_user={"question": question, "options": [str(o) for o in options]})

    def _t_task_complete(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        summary = str(a.get("summary") or a.get("message") or a.get("result") or "Done.")
        success = a.get("success", True)
        if isinstance(success, str):
            success = success.strip().lower() not in ("false", "0", "no")
        return ToolResult(call, True, {"summary": summary, "success": bool(success)}, task_complete=True)


_ALIASES = {
    "take_screenshot": "screenshot", "screen_capture": "screenshot", "capture_screen": "screenshot", "look": "screenshot",
    "mouse_click": "click", "left_click": "click", "click_at": "click", "tap": "click",
    "move_mouse": "mouse_move", "hover": "mouse_move", "mouse_drag": "drag", "drag_and_drop": "drag",
    "mouse_scroll": "scroll", "scroll_down": "scroll", "scroll_up": "scroll",
    "type": "type_text", "keyboard_type": "type_text", "write": "type_text", "input_text": "type_text", "typewrite": "type_text",
    "press": "press_keys", "hotkey": "press_keys", "key": "press_keys", "keypress": "press_keys", "press_key": "press_keys",
    "send_keys": "press_keys", "key_press": "press_keys", "shortcut": "press_keys",
    "menu_navigate": "menu", "navigate_menu": "menu", "select_menu_item": "menu", "open_menu": "menu", "menu_item": "menu",
    "open_application": "open_app", "launch_app": "open_app", "launch": "open_app", "start_app": "open_app", "open_program": "open_app",
    "open": "open_app", "run_app": "open_app", "open_file": "open_app",
    "browse": "open_url", "open_browser": "open_url", "navigate": "open_url",
    "shell": "run_command", "powershell": "run_command", "cmd": "run_command", "execute_command": "run_command",
    "run_shell": "run_command", "bash": "run_command", "exec": "run_command", "run": "run_command",
    "get_windows": "list_windows", "windows": "list_windows", "activate_window": "focus_window", "switch_window": "focus_window",
    "set_clipboard": "clipboard", "get_clipboard": "clipboard", "copy_to_clipboard": "clipboard", "paste_text": "clipboard",
    "sleep": "wait", "delay": "wait", "pause": "wait",
    "finish": "task_complete", "done": "task_complete", "complete": "task_complete", "final_answer": "task_complete",
    "finish_task": "task_complete", "end_task": "task_complete", "terminate": "task_complete",
    "question": "ask_user", "ask": "ask_user", "request_input": "ask_user", "ask_question": "ask_user",
    "read": "read_file", "cat": "read_file", "ls": "list_directory", "dir": "list_directory", "list_files": "list_directory",
    "save_file": "write_file", "create_file": "write_file",
}


def _to_int(value: Any, name: str) -> int:
    if value is None:
        raise ValueError(f"'{name}' is required")
    if isinstance(value, bool):
        raise ValueError(f"'{name}' must be a number")
    if isinstance(value, (int, float)):
        return int(round(value))
    if isinstance(value, str):
        v = value.strip().rstrip("px")
        try:
            return int(round(float(v)))
        except ValueError as exc:
            raise ValueError(f"'{name}' must be a number, got {value!r}") from exc
    raise ValueError(f"'{name}' must be a number, got {type(value).__name__}")


def _opt_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    return _to_int(value, "value")


def _expand(path: Any) -> str:
    if not path:
        raise ValueError("'path' is required")
    return os.path.expandvars(os.path.expanduser(str(path)))


def _truncate(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"
