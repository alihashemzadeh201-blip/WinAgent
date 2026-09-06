"""Execute :class:`ToolCall` objects against a desktop backend.

Responsibilities:

* validate / coerce arguments coming from the model,
* convert screenshot coordinates to physical pixels,
* enforce safety policies (dangerous command confirmation, disabled features),
* attach a fresh screenshot to results when appropriate,
* produce compact JSON results for the model.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..backends.base import BackendError, DesktopBackend, EmergencyStop
from ..config import Config
from ..keys import parse_key_combo
from ..protocol import ToolCall
from ..screenshot import Screenshot, prepare_screenshot
from .definitions import TOOLS_BY_NAME

log = logging.getLogger(__name__)

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

    def to_text(self, include_screenshot_note: bool = True) -> str:
        payload: dict[str, Any] = {"ok": self.ok}
        if self.error:
            payload["error"] = self.error
        payload.update(self.data)
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
                 stop_event=None):
        self.backend = backend
        self.config = config
        self.confirm = confirm
        self.stop_event = stop_event
        self.last_screenshot: Optional[Screenshot] = None
        self.screenshot_count = 0

    # ------------------------------------------------------------ screenshot
    def take_screenshot(self, *, region: Optional[list[int]] = None, all_screens: bool = False,
                        grid: Optional[bool] = None) -> Screenshot:
        cfg = self.config
        geometry = self.backend.screen_geometry(all_screens=all_screens)
        phys_region = None
        if region:
            if self.last_screenshot is None:
                raise BackendError("Take a full screenshot before requesting a region.")
            l, t = self.last_screenshot.to_physical(region[0], region[1])
            r, b = self.last_screenshot.to_physical(region[2], region[3])
            l, r = sorted((l, r))
            t, b = sorted((t, b))
            if r - l < 8 or b - t < 8:
                raise BackendError("Region is too small.")
            phys_region = (l, t, r, b)
        # a region is expressed in absolute coordinates -> grab the whole virtual screen and crop
        raw = self.backend.capture(all_screens=all_screens or phys_region is not None, region=phys_region)
        cursor = None
        if cfg.screenshot_show_cursor:
            try:
                cursor = self.backend.mouse_position()
            except Exception:
                cursor = None
        if phys_region:
            from ..backends.base import ScreenGeometry

            geometry = ScreenGeometry(phys_region[0], phys_region[1], phys_region[2] - phys_region[0], phys_region[3] - phys_region[1])
            max_width = cfg.screenshot_max_width  # regions are zoomed: never downscale below raw
        else:
            max_width = cfg.screenshot_max_width
        shot = prepare_screenshot(
            raw, geometry=geometry, max_width=max_width,
            grid=cfg.screenshot_grid if grid is None else bool(grid),
            grid_spacing=cfg.screenshot_grid_spacing, cursor=cursor,
            fmt=cfg.screenshot_format, quality=cfg.screenshot_jpeg_quality,
        )
        if phys_region is None:
            self.last_screenshot = shot  # regions don't replace the coordinate frame
        self.screenshot_count += 1
        return shot

    # --------------------------------------------------------------- helpers
    def _phys(self, x: Any, y: Any) -> tuple[int, int]:
        xi, yi = _to_int(x, "x"), _to_int(y, "y")
        if self.last_screenshot is None:
            # no screenshot yet: treat as physical coordinates
            return xi, yi
        w, h = self.last_screenshot.size
        if not (-50 <= xi <= w + 50 and -50 <= yi <= h + 50):
            raise BackendError(f"Coordinates ({xi},{yi}) are outside the screenshot ({w}x{h}). "
                               "Use coordinates from the latest screenshot.")
        xi = min(max(xi, 0), w - 1)
        yi = min(max(yi, 0), h - 1)
        return self.last_screenshot.to_physical(xi, yi)

    def _check_stop(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise EmergencyStop("Stopped by user.")
        if self.config.mouse_failsafe:
            try:
                x, y = self.backend.mouse_position()
            except Exception:
                return
            if x <= 0 and y <= 0:
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

    def _window_dict(self, w) -> dict[str, Any]:
        d = w.to_dict()
        if self.last_screenshot is not None:
            d["screenshot_rect"] = [*self.last_screenshot.to_image(w.left, w.top), *self.last_screenshot.to_image(w.right, w.bottom)]
        return d

    def _require_confirm(self, call: ToolCall, reason: str) -> bool:
        if not self.config.confirm_dangerous_actions:
            return True
        if self.confirm is None:
            return False
        return bool(self.confirm(call, reason))

    # ---------------------------------------------------------------- execute
    def execute(self, call: ToolCall) -> ToolResult:
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
            handler = getattr(self, f"_t_{call.name}")
            result = handler(call, call.arguments or {})
        except EmergencyStop:
            raise
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
        # automatic screenshot after UI actions
        if (spec.screenshot_after and self.config.auto_screenshot_after_action and result.ok
                and result.screenshot is None and not result.task_complete and result.ask_user is None):
            try:
                time.sleep(max(0.0, self.config.action_delay))
                result.screenshot = self.take_screenshot()
            except Exception as exc:  # pragma: no cover
                result.data["screenshot_error"] = str(exc)
        result.duration = time.time() - start
        return result

    # ------------------------------------------------------------ perception
    def _t_screenshot(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        region = a.get("region")
        if region is not None:
            if not (isinstance(region, (list, tuple)) and len(region) == 4):
                raise ValueError("region must be [left, top, right, bottom]")
            region = [_to_int(v, "region") for v in region]
        shot = self.take_screenshot(region=region, all_screens=bool(a.get("all_screens", False)), grid=a.get("grid"))
        data: dict[str, Any] = {"message": "Screenshot captured."}
        try:
            win = self.backend.active_window()
            if win:
                data["active_window"] = self._window_dict(win)
            data["mouse"] = list(self.last_screenshot.to_image(*self.backend.mouse_position())) if self.last_screenshot else None
        except Exception:
            pass
        if region is not None:
            data["note"] = ("This is a ZOOMED region; its coordinates are NOT the global frame. To click, convert back "
                            f"using region origin ({region[0]},{region[1]}) and scale, or take a full screenshot.")
            data["region"] = region
        return ToolResult(call, True, data, screenshot=shot)

    def _t_get_screen_info(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        info = self.backend.system_info()
        active = self.backend.active_window()
        windows = [self._window_dict(w) for w in self.backend.list_windows()[:40]]
        mouse = self.backend.mouse_position()
        data = {
            "system": info,
            "mouse_physical": list(mouse),
            "mouse_screenshot": list(self.last_screenshot.to_image(*mouse)) if self.last_screenshot else None,
            "screenshot_frame": self.last_screenshot.describe() if self.last_screenshot else "no screenshot taken yet",
            "active_window": self._window_dict(active) if active else None,
            "windows": windows,
        }
        return ToolResult(call, True, data)

    # ----------------------------------------------------------------- mouse
    def _t_mouse_move(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        x, y = self._phys(a.get("x"), a.get("y"))
        self.backend.mouse_move(x, y, duration=0.25)
        return ToolResult(call, True, {"message": f"Mouse moved to physical ({x},{y})."})

    def _t_click(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        button = str(a.get("button") or "left").lower()
        clicks = _to_int(a.get("clicks", 1), "clicks")
        clicks = min(max(clicks, 1), 3)
        mods = a.get("modifiers") or []
        if isinstance(mods, str):
            mods = parse_key_combo(mods)
        if a.get("x") is None or a.get("y") is None:
            self.backend.mouse_click(None, None, button=button, clicks=clicks, modifiers=list(mods))
            where = "at current pointer position"
        else:
            x, y = self._phys(a.get("x"), a.get("y"))
            self.backend.mouse_click(x, y, button=button, clicks=clicks, modifiers=list(mods))
            where = f"at physical ({x},{y})"
        kind = {1: "Clicked", 2: "Double-clicked", 3: "Triple-clicked"}[clicks]
        return ToolResult(call, True, {"message": f"{kind} {button} {where}."})

    def _t_double_click(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        return self._t_click(call, {**a, "clicks": 2, "button": "left"})

    def _t_right_click(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        return self._t_click(call, {**a, "clicks": 1, "button": "right"})

    def _t_drag(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        x1, y1 = self._phys(a.get("x1"), a.get("y1"))
        x2, y2 = self._phys(a.get("x2"), a.get("y2"))
        duration = float(a.get("duration") or 0.5)
        self.backend.mouse_drag(x1, y1, x2, y2, button=str(a.get("button") or "left"), duration=min(max(duration, 0.1), 5.0))
        return ToolResult(call, True, {"message": f"Dragged from ({x1},{y1}) to ({x2},{y2}) physical px."})

    def _t_scroll(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        amount = _to_int(a.get("amount", a.get("clicks", -3)), "amount")
        amount = max(-50, min(50, amount))
        direction = str(a.get("direction") or "vertical").lower()
        x = y = None
        if a.get("x") is not None and a.get("y") is not None:
            x, y = self._phys(a.get("x"), a.get("y"))
        if direction.startswith("h"):
            self.backend.mouse_scroll(x, y, dx=amount, dy=0)
        else:
            self.backend.mouse_scroll(x, y, dx=0, dy=amount)
        return ToolResult(call, True, {"message": f"Scrolled {direction} by {amount} clicks."})

    # -------------------------------------------------------------- keyboard
    def _t_type_text(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        text = a.get("text")
        if text is None:
            text = a.get("value", "")
        text = str(text)
        interval = float(a.get("interval") or 0.0)
        self.backend.type_text(text, interval=min(max(interval, 0.0), 0.5))
        if a.get("press_enter"):
            time.sleep(0.1)
            self.backend.press_keys(["enter"])
        return ToolResult(call, True, {"message": f"Typed {len(text)} characters" + (" and pressed Enter." if a.get("press_enter") else ".")})

    def _t_press_keys(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        keys = a.get("keys") or a.get("key") or a.get("combination") or a.get("value")
        if keys is None:
            raise ValueError("'keys' is required")
        combo = parse_key_combo(keys)
        repeat = min(max(_to_int(a.get("repeat", 1), "repeat"), 1), 50)
        self.backend.press_keys(combo, repeat=repeat)
        return ToolResult(call, True, {"message": f"Pressed {'+'.join(combo)}" + (f" x{repeat}" if repeat > 1 else "") + "."})

    def _t_hotkey_sequence(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        seq = a.get("sequence") or a.get("keys") or []
        if isinstance(seq, str):
            seq = [s.strip() for s in re.split(r"[,;]|\bthen\b", seq) if s.strip()]
        delay = min(max(float(a.get("delay") or 0.3), 0.0), 5.0)
        done = []
        for item in seq:
            self._check_stop()
            combo = parse_key_combo(item)
            self.backend.press_keys(combo)
            done.append("+".join(combo))
            time.sleep(delay)
        return ToolResult(call, True, {"message": f"Pressed sequence: {', '.join(done)}."})

    # -------------------------------------------------------------- programs
    def _t_open_app(self, call: ToolCall, a: dict[str, Any]) -> ToolResult:
        name = a.get("name") or a.get("app") or a.get("path") or a.get("value")
        if not name:
            raise ValueError("'name' is required")
        msg = self.backend.open_application(str(name), str(a.get("args") or ""))
        wait = min(max(float(a.get("wait") or 2.0), 0.0), 30.0)
        time.sleep(wait)
        data = {"message": msg}
        try:
            win = self.backend.active_window()
            if win:
                data["active_window"] = self._window_dict(win)
        except Exception:
            pass
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
        out = []
        for c in controls:
            d = c.to_dict()
            if self.last_screenshot is not None:
                cx, cy = (c.left + c.right) // 2, (c.top + c.bottom) // 2
                d["screenshot_center"] = list(self.last_screenshot.to_image(cx, cy))
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
