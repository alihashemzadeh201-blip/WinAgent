"""A simulated desktop backend.

Used for unit tests, for running the GUI on macOS/Linux while developing, and
for the ``--demo`` mode.  It keeps a tiny model of a desktop (windows, mouse
position, typed text, clipboard) and can *render* it to an image so the whole
screenshot -> LLM -> action loop can be exercised without a real Windows box.
"""

from __future__ import annotations

import platform
import subprocess
import sys
import time
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont

from ..keys import normalize_key
from .base import BackendError, CommandResult, ControlInfo, DesktopBackend, ScreenGeometry, WindowInfo


class FakeBackend(DesktopBackend):
    name = "fake"

    def __init__(self, width: int = 1600, height: int = 900, allow_real_commands: bool = False):
        self.width = width
        self.height = height
        self.allow_real_commands = allow_real_commands
        self.mouse = (width // 2, height // 2)
        self.clipboard = ""
        self.windows: list[WindowInfo] = []
        self.typed: list[str] = []
        self.pressed: list[list[str]] = []
        self.events: list[dict[str, Any]] = []
        self.commands: list[str] = []
        self._next_hwnd = 1000
        self._font = self._load_font(18)
        self._font_small = self._load_font(13)
        self.command_hook = None  # optional callable(command, shell) -> CommandResult
        self._add_window("Desktop", "Progman-fake", 0, 0, width, height - 40, "explorer.exe")

    @staticmethod
    def _load_font(size: int):
        try:
            return ImageFont.load_default(size=size)
        except Exception:  # pragma: no cover
            return ImageFont.load_default()

    # ---------------------------------------------------------------- helpers
    def _log(self, kind: str, **data: Any) -> None:
        self.events.append({"t": time.time(), "kind": kind, **data})

    def _add_window(self, title: str, cls: str, left: int, top: int, right: int, bottom: int, proc: str) -> WindowInfo:
        for w in self.windows:
            w.is_active = False
        self._next_hwnd += 1
        win = WindowInfo(hwnd=self._next_hwnd, title=title, class_name=cls, left=left, top=top, right=right,
                         bottom=bottom, pid=4000 + self._next_hwnd, process_name=proc, is_active=True)
        self.windows.insert(0, win)
        return win

    def _get(self, hwnd: int) -> WindowInfo:
        for w in self.windows:
            if w.hwnd == int(hwnd):
                return w
        raise BackendError(f"Window handle {hwnd} is not valid anymore.")

    # ----------------------------------------------------------------- screen
    def screen_geometry(self, all_screens: bool = False) -> ScreenGeometry:
        return ScreenGeometry(0, 0, self.width, self.height, {"monitors": 1})

    def capture(self, all_screens: bool = False, region: Optional[tuple[int, int, int, int]] = None) -> Image.Image:
        img = Image.new("RGB", (self.width, self.height), (30, 90, 160))
        draw = ImageDraw.Draw(img)
        # taskbar
        draw.rectangle([0, self.height - 40, self.width, self.height], fill=(25, 25, 30))
        draw.rectangle([8, self.height - 34, 36, self.height - 6], fill=(0, 120, 215))
        draw.text((50, self.height - 30), "Start", fill=(230, 230, 230), font=self._font_small)
        draw.text((self.width - 150, self.height - 30), time.strftime("%H:%M  %Y-%m-%d"), fill=(230, 230, 230), font=self._font_small)
        # windows, back to front
        for w in reversed([w for w in self.windows if w.class_name != "Progman-fake" and not w.is_minimized]):
            draw.rectangle([w.left, w.top, w.right, w.bottom], fill=(245, 245, 245), outline=(80, 80, 80))
            title_col = (0, 120, 215) if w.is_active else (160, 160, 160)
            draw.rectangle([w.left, w.top, w.right, w.top + 32], fill=title_col)
            draw.text((w.left + 10, w.top + 6), w.title, fill=(255, 255, 255), font=self._font)
            draw.text((w.right - 30, w.top + 4), "x", fill=(255, 255, 255), font=self._font)
            # menu bar
            draw.rectangle([w.left + 1, w.top + 33, w.right - 1, w.top + 58], fill=(235, 235, 235))
            draw.text((w.left + 12, w.top + 37), "File   Edit   View   Help", fill=(30, 30, 30), font=self._font_small)
            text = "".join(self.typed)[-2000:] if w.is_active else ""
            y = w.top + 70
            for line in text.split("\n")[-20:]:
                draw.text((w.left + 12, y), line[:120], fill=(20, 20, 20), font=self._font)
                y += 24
        # mouse cursor
        mx, my = self.mouse
        draw.polygon([(mx, my), (mx, my + 18), (mx + 5, my + 14), (mx + 12, my + 12)], fill=(0, 0, 0), outline=(255, 255, 255))
        if region:
            img = img.crop(region)
        return img

    # ------------------------------------------------------------------ mouse
    def mouse_position(self) -> tuple[int, int]:
        return self.mouse

    def mouse_move(self, x: int, y: int, duration: float = 0.2) -> None:
        self.mouse = (int(x), int(y))
        self._log("move", x=x, y=y)

    def mouse_click(self, x, y, button="left", clicks=1, modifiers=None) -> None:
        if x is not None and y is not None:
            self.mouse = (int(x), int(y))
        mx, my = self.mouse
        self._log("click", x=mx, y=my, button=button, clicks=clicks, modifiers=modifiers or [])
        # hit-test windows (front to back) to change focus / close
        for w in self.windows:
            if w.is_minimized or w.class_name == "Progman-fake":
                continue
            if w.left <= mx <= w.right and w.top <= my <= w.bottom:
                if my <= w.top + 32 and mx >= w.right - 40:
                    self.windows.remove(w)
                    if self.windows:
                        self.windows[0].is_active = True
                else:
                    self._activate(w)
                break
        else:
            if my >= self.height - 40 and mx < 45:
                self._add_window("Start menu", "StartMenu", 0, self.height - 540, 500, self.height - 40, "StartMenuExperienceHost.exe")

    def _activate(self, win: WindowInfo) -> None:
        for w in self.windows:
            w.is_active = False
        win.is_active = True
        win.is_minimized = False
        self.windows.remove(win)
        self.windows.insert(0, win)

    def mouse_down(self, x, y, button="left") -> None:
        if x is not None and y is not None:
            self.mouse = (int(x), int(y))
        self._log("down", button=button)

    def mouse_up(self, x, y, button="left") -> None:
        if x is not None and y is not None:
            self.mouse = (int(x), int(y))
        self._log("up", button=button)

    def mouse_drag(self, x1, y1, x2, y2, button="left", duration=0.5) -> None:
        self.mouse = (int(x2), int(y2))
        self._log("drag", x1=x1, y1=y1, x2=x2, y2=y2, button=button)

    def mouse_scroll(self, x, y, dx=0, dy=0) -> None:
        if x is not None and y is not None:
            self.mouse = (int(x), int(y))
        self._log("scroll", dx=dx, dy=dy)

    # --------------------------------------------------------------- keyboard
    def type_text(self, text: str, interval: float = 0.0) -> None:
        self.typed.append(text)
        self._log("type", text=text)

    def press_keys(self, keys: list[str], repeat: int = 1) -> None:
        canonical = [normalize_key(k) for k in keys]
        for _ in range(max(1, repeat)):
            self.pressed.append(canonical)
        self._log("keys", keys=canonical, repeat=repeat)
        if canonical == ["enter"]:
            self.typed.append("\n")
        elif canonical == ["backspace"] and self.typed:
            last = self.typed[-1]
            self.typed[-1] = last[:-1]
        elif canonical == ["ctrl", "a"]:
            pass
        elif canonical in (["win"], ["winleft"]):
            self._add_window("Start menu", "StartMenu", 0, self.height - 540, 500, self.height - 40, "StartMenuExperienceHost.exe")
        elif canonical == ["esc"]:
            for w in list(self.windows):
                if w.class_name == "StartMenu":
                    self.windows.remove(w)
        elif canonical == ["alt", "f4"]:
            active = self.active_window()
            if active and active.class_name != "Progman-fake":
                self.windows.remove(active)
                if self.windows:
                    self.windows[0].is_active = True

    # --------------------------------------------------------------- programs
    def open_application(self, name: str, args: str = "") -> str:
        title = name.strip()
        pretty = {"notepad": "Untitled - Notepad", "calc": "Calculator", "calculator": "Calculator",
                  "chrome": "New Tab - Google Chrome", "mspaint": "Untitled - Paint", "explorer": "File Explorer"}
        title = pretty.get(title.lower().removesuffix(".exe"), title)
        n = len([w for w in self.windows if w.class_name != "Progman-fake"])
        off = 40 + n * 30
        self._add_window(title, "FakeApp", 200 + off, 100 + off, 200 + off + 900, 100 + off + 600, name.lower().removesuffix(".exe") + ".exe")
        self.typed = []
        self._log("open_app", name=name, args=args)
        return f"Launched {name!r} (simulated)."

    def open_url(self, url: str) -> str:
        self._add_window(f"{url} - Browser", "FakeBrowser", 150, 80, 1350, 780, "browser.exe")
        self._log("open_url", url=url)
        return f"Opened {url} (simulated)."

    def run_command(self, command: str, shell: str = "powershell", timeout: float = 60.0, cwd: Optional[str] = None) -> CommandResult:
        self.commands.append(command)
        self._log("command", command=command, shell=shell)
        if self.command_hook is not None:
            return self.command_hook(command, shell)
        if self.allow_real_commands and sys.platform != "win32":
            start = time.time()
            try:
                proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout, cwd=cwd)
                return CommandResult(command, "sh", proc.returncode, proc.stdout, proc.stderr, False, time.time() - start)
            except subprocess.TimeoutExpired as exc:
                return CommandResult(command, "sh", None, exc.stdout or "", exc.stderr or "", True, time.time() - start)
        return CommandResult(command, shell, 0, f"(simulated output of: {command})", "", False, 0.01)

    # ---------------------------------------------------------------- windows
    def list_windows(self) -> list[WindowInfo]:
        return [w for w in self.windows if w.class_name != "Progman-fake"]

    def active_window(self) -> Optional[WindowInfo]:
        for w in self.windows:
            if w.is_active:
                return w
        return None

    def focus_window(self, hwnd: int) -> bool:
        self._activate(self._get(hwnd))
        self._log("focus", hwnd=hwnd)
        return True

    def window_action(self, hwnd, action, x=None, y=None, width=None, height=None) -> str:
        w = self._get(hwnd)
        action = action.lower()
        if action == "minimize":
            w.is_minimized = True
            w.is_active = False
        elif action == "maximize":
            w.left, w.top, w.right, w.bottom = 0, 0, self.width, self.height - 40
            w.is_maximized = True
        elif action in ("restore", "show"):
            w.is_minimized = False
            w.is_maximized = False
        elif action == "close":
            self.windows.remove(w)
            if self.windows:
                self.windows[0].is_active = True
        elif action in ("move", "resize", "move_resize"):
            nw = width if width is not None else w.width
            nh = height if height is not None else w.height
            nx = x if x is not None else w.left
            ny = y if y is not None else w.top
            w.left, w.top, w.right, w.bottom = int(nx), int(ny), int(nx) + int(nw), int(ny) + int(nh)
        elif action in ("focus", "activate"):
            self._activate(w)
        else:
            raise BackendError(f"Unknown window action {action!r}.")
        self._log("window_action", hwnd=hwnd, action=action)
        return f"Window {hwnd}: {action} done (simulated)."

    def window_controls(self, hwnd: int, limit: int = 200) -> list[ControlInfo]:
        w = self._get(hwnd)
        return [
            ControlInfo(w.hwnd * 10 + 1, "Edit", "".join(self.typed)[:100], w.left + 1, w.top + 60, w.right - 1, w.bottom - 1),
            ControlInfo(w.hwnd * 10 + 2, "Button", "Close", w.right - 40, w.top, w.right, w.top + 32),
        ][:limit]

    # -------------------------------------------------------------- clipboard
    def clipboard_get(self) -> str:
        return self.clipboard

    def clipboard_set(self, text: str) -> None:
        self.clipboard = text
        self._log("clipboard", text=text)

    # ------------------------------------------------------------------- misc
    def system_info(self) -> dict[str, Any]:
        return {
            "os": f"Simulated Windows 11 (host: {platform.system()} {platform.release()})",
            "user": "demo",
            "computer": "FAKE-PC",
            "primary_screen": {"width": self.width, "height": self.height},
            "monitors": 1,
            "system_dpi": 96,
            "scale_percent": 100,
            "keyboard_layout": "en-US",
            "local_time": time.strftime("%Y-%m-%d %H:%M"),
            "note": "This is a simulated desktop backend; actions do not affect a real machine.",
        }
