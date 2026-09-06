"""Real Windows desktop backend (Win32 API through ``ctypes``).

Everything here is deliberately dependency-light: mouse/keyboard input goes
through ``SendInput``/``SetCursorPos``, screenshots through Pillow's
``ImageGrab`` (which uses ``BitBlt``), windows through ``EnumWindows`` and
friends, the clipboard through the user32 clipboard API.  No pywin32 /
pyautogui required.
"""

from __future__ import annotations

import base64
import ctypes
import difflib
import json
import logging
import os
import platform
import re
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable, Optional

from PIL import Image, ImageGrab

from ..keys import MODIFIERS, VK_CODES, hotkey_to_vk, normalize_key
from .base import (
    BackendError,
    CommandResult,
    ControlInfo,
    DesktopBackend,
    ScreenGeometry,
    WindowInfo,
)

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# ----------------------------------------------------------------------------
# Win32 constants
# ----------------------------------------------------------------------------
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
WHEEL_DELTA = 120
XBUTTON1 = 0x0001
XBUTTON2 = 0x0002

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

MAPVK_VK_TO_VSC = 0

SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CMONITORS = 80

SW_SHOWNORMAL = 1
SW_MAXIMIZE = 3
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9

WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WM_HOTKEY = 0x0312
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_APPWINDOW = 0x00040000

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

DWMWA_CLOAKED = 14
DWMWA_EXTENDED_FRAME_BOUNDS = 9

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MOD_NOREPEAT = 0x4000

# Keys for which the "extended" flag must be set in keyboard input.
EXTENDED_VKS = {
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,  # pgup pgdn end home arrows
    0x2C, 0x2D, 0x2E,  # printscreen insert delete
    0x5B, 0x5C, 0x5D,  # win keys, apps
    0x6F,  # numpad divide
    0x90,  # numlock
    0xA3, 0xA5,  # right ctrl, right alt
    0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3,  # media keys
    0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xAB, 0xAC,  # browser keys
}

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
SEE_MASK_FLAG_NO_UI = 0x00000400

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM) if IS_WINDOWS else None

# Friendly aliases -> what ShellExecute / Start menu understands.
APP_ALIASES: dict[str, str] = {
    "notepad": "notepad.exe", "دفترچه یادداشت": "notepad.exe", "نوت پد": "notepad.exe",
    "calculator": "calc.exe", "calc": "calc.exe", "ماشین حساب": "calc.exe",
    "paint": "mspaint.exe", "mspaint": "mspaint.exe", "نقاشی": "mspaint.exe",
    "wordpad": "write.exe",
    "cmd": "cmd.exe", "command prompt": "cmd.exe", "terminal": "wt.exe", "windows terminal": "wt.exe",
    "powershell": "powershell.exe", "pwsh": "pwsh.exe",
    "explorer": "explorer.exe", "file explorer": "explorer.exe", "files": "explorer.exe",
    "this pc": "explorer.exe shell:MyComputerFolder", "my computer": "explorer.exe shell:MyComputerFolder",
    "downloads": "explorer.exe shell:Downloads", "documents": "explorer.exe shell:Personal",
    "desktop": "explorer.exe shell:Desktop", "recycle bin": "explorer.exe shell:RecycleBinFolder",
    "task manager": "taskmgr.exe", "taskmgr": "taskmgr.exe",
    "control panel": "control.exe", "control": "control.exe",
    "settings": "ms-settings:", "windows settings": "ms-settings:", "تنظیمات": "ms-settings:",
    "store": "ms-windows-store:", "microsoft store": "ms-windows-store:",
    "edge": "msedge.exe", "microsoft edge": "msedge.exe",
    "chrome": "chrome.exe", "google chrome": "chrome.exe", "کروم": "chrome.exe",
    "firefox": "firefox.exe", "mozilla firefox": "firefox.exe", "فایرفاکس": "firefox.exe",
    "brave": "brave.exe", "opera": "opera.exe",
    "word": "winword.exe", "microsoft word": "winword.exe", "ورد": "winword.exe",
    "excel": "excel.exe", "microsoft excel": "excel.exe", "اکسل": "excel.exe",
    "powerpoint": "powerpnt.exe", "microsoft powerpoint": "powerpnt.exe", "پاورپوینت": "powerpnt.exe",
    "outlook": "outlook.exe", "onenote": "onenote.exe", "teams": "ms-teams.exe",
    "vscode": "code", "vs code": "code", "visual studio code": "code", "code": "code",
    "snipping tool": "ms-screenclip:", "snip": "ms-screenclip:",
    "camera": "microsoft.windows.camera:", "photos": "ms-photos:",
    "mail": "outlookmail:", "calendar": "outlookcal:", "maps": "bingmaps:",
    "registry editor": "regedit.exe", "regedit": "regedit.exe",
    "device manager": "devmgmt.msc", "services": "services.msc", "event viewer": "eventvwr.msc",
    "disk management": "diskmgmt.msc", "system information": "msinfo32.exe",
    "character map": "charmap.exe", "on-screen keyboard": "osk.exe", "magnifier": "magnify.exe",
    "remote desktop": "mstsc.exe", "run": "explorer.exe shell:::{2559a1f3-21d7-11d4-bdaf-00c04f60b9f0}",
    "media player": "wmplayer.exe", "windows media player": "wmplayer.exe",
    "spotify": "spotify.exe", "telegram": "telegram.exe", "discord": "discord.exe", "whatsapp": "whatsapp:",
    "zoom": "zoom", "skype": "skype.exe", "vlc": "vlc.exe", "steam": "steam.exe",
    "python": "python.exe", "cursor": "cursor.exe", "pycharm": "pycharm64.exe",
}

LANGID_NAMES = {
    0x0409: "en-US", 0x0809: "en-GB", 0x0429: "fa-IR", 0x0401: "ar-SA", 0x041F: "tr-TR",
    0x0407: "de-DE", 0x040C: "fr-FR", 0x0410: "it-IT", 0x0C0A: "es-ES", 0x0419: "ru-RU",
    0x0411: "ja-JP", 0x0804: "zh-CN", 0x0412: "ko-KR", 0x0416: "pt-BR", 0x0413: "nl-NL",
    0x0420: "ur-PK", 0x0439: "hi-IN", 0x040D: "he-IL", 0x0415: "pl-PL", 0x0422: "uk-UA",
}


def _norm_name(s: str) -> str:
    return re.sub(r"[^0-9a-z\u0600-\u06ff]+", "", s.lower())


def make_dpi_aware() -> None:
    """Make the current process per-monitor DPI aware (physical pixel coordinates)."""
    if not IS_WINDOWS:
        return
    try:
        user32 = ctypes.windll.user32
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4
        if hasattr(user32, "SetProcessDpiAwarenessContext"):
            user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
            user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass
        user32.SetProcessDPIAware()
    except Exception as exc:  # pragma: no cover - depends on Windows version
        log.debug("DPI awareness could not be set: %s", exc)


class _HotkeyListener(threading.Thread):
    """Registers a global hot-key in its own thread (hot-keys are thread bound)."""

    def __init__(self, combo: str, callback: Callable[[], None]):
        super().__init__(name="winagent-hotkey", daemon=True)
        self.combo = combo
        self.callback = callback
        self.thread_id: Optional[int] = None
        self.registered = False
        self._ready = threading.Event()

    def run(self) -> None:  # pragma: no cover - needs Windows
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self.thread_id = kernel32.GetCurrentThreadId()
        try:
            mods, vk = hotkey_to_vk(self.combo)
        except ValueError as exc:
            log.warning("Invalid stop hotkey %r: %s", self.combo, exc)
            self._ready.set()
            return
        self.registered = bool(user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk))
        if not self.registered:
            log.warning("Could not register global hotkey %r (already in use?)", self.combo)
        self._ready.set()
        if not self.registered:
            return
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self.callback()
                    except Exception:
                        log.exception("hotkey callback failed")
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnregisterHotKey(None, 1)

    def wait_ready(self, timeout: float = 2.0) -> bool:
        self._ready.wait(timeout)
        return self.registered

    def stop(self) -> None:  # pragma: no cover - needs Windows
        if self.thread_id and IS_WINDOWS:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)


class WindowsBackend(DesktopBackend):
    name = "windows"

    def __init__(self, stop_hotkey: str = "ctrl+alt+esc", on_emergency_stop: Optional[Callable[[], None]] = None):
        if not IS_WINDOWS:
            raise BackendError("WindowsBackend can only be used on Windows.")
        make_dpi_aware()
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.shell32 = ctypes.windll.shell32
        try:
            self.dwmapi = ctypes.windll.dwmapi
        except OSError:  # pragma: no cover
            self.dwmapi = None
        self._setup_prototypes()
        self._last_move: Optional[tuple[int, int]] = None
        self._start_apps_cache: Optional[list[dict[str, str]]] = None
        self._start_apps_time = 0.0
        self._shortcut_cache: Optional[list[tuple[str, str]]] = None
        self._hotkey: Optional[_HotkeyListener] = None
        self.stop_hotkey = stop_hotkey
        self.hotkey_registered = False
        if stop_hotkey and on_emergency_stop:
            self._hotkey = _HotkeyListener(stop_hotkey, on_emergency_stop)
            self._hotkey.start()
            self.hotkey_registered = self._hotkey.wait_ready()

    # ------------------------------------------------------------------ setup
    def _setup_prototypes(self) -> None:
        u = self.user32
        u.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        u.SendInput.restype = wintypes.UINT
        u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        u.SetCursorPos.restype = wintypes.BOOL
        u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        u.GetCursorPos.restype = wintypes.BOOL
        u.GetSystemMetrics.argtypes = [ctypes.c_int]
        u.GetSystemMetrics.restype = ctypes.c_int
        u.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        u.MapVirtualKeyW.restype = wintypes.UINT
        u.VkKeyScanW.argtypes = [wintypes.WCHAR]
        u.VkKeyScanW.restype = ctypes.c_short
        u.GetForegroundWindow.argtypes = []
        u.GetForegroundWindow.restype = wintypes.HWND
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.SetForegroundWindow.restype = wintypes.BOOL
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetWindowRect.restype = wintypes.BOOL
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL
        u.IsWindowEnabled.argtypes = [wintypes.HWND]
        u.IsWindowEnabled.restype = wintypes.BOOL
        u.IsWindow.argtypes = [wintypes.HWND]
        u.IsWindow.restype = wintypes.BOOL
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsIconic.restype = wintypes.BOOL
        u.IsZoomed.argtypes = [wintypes.HWND]
        u.IsZoomed.restype = wintypes.BOOL
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.ShowWindow.restype = wintypes.BOOL
        u.BringWindowToTop.argtypes = [wintypes.HWND]
        u.BringWindowToTop.restype = wintypes.BOOL
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.SetWindowPos.restype = wintypes.BOOL
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.PostMessageW.restype = wintypes.BOOL
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        u.AttachThreadInput.restype = wintypes.BOOL
        u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindowLongW.restype = wintypes.LONG
        u.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
        u.EnumWindows.restype = wintypes.BOOL
        u.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
        u.EnumChildWindows.restype = wintypes.BOOL
        u.GetKeyboardLayout.argtypes = [wintypes.DWORD]
        u.GetKeyboardLayout.restype = wintypes.HKL
        u.OpenClipboard.argtypes = [wintypes.HWND]
        u.OpenClipboard.restype = wintypes.BOOL
        u.CloseClipboard.argtypes = []
        u.CloseClipboard.restype = wintypes.BOOL
        u.EmptyClipboard.argtypes = []
        u.EmptyClipboard.restype = wintypes.BOOL
        u.GetClipboardData.argtypes = [wintypes.UINT]
        u.GetClipboardData.restype = wintypes.HANDLE
        u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        u.SetClipboardData.restype = wintypes.HANDLE
        u.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        u.IsClipboardFormatAvailable.restype = wintypes.BOOL
        u.GetDoubleClickTime.restype = wintypes.UINT
        k = self.kernel32
        k.GetCurrentThreadId.argtypes = []
        k.GetCurrentThreadId.restype = wintypes.DWORD
        k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k.GlobalAlloc.restype = wintypes.HGLOBAL
        k.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k.GlobalLock.restype = wintypes.LPVOID
        k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        k.GlobalUnlock.restype = wintypes.BOOL
        k.GlobalFree.argtypes = [wintypes.HGLOBAL]
        k.GlobalFree.restype = wintypes.HGLOBAL
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
        self.shell32.ShellExecuteExW.restype = wintypes.BOOL
        if self.dwmapi is not None:
            self.dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            self.dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

    # ----------------------------------------------------------------- screen
    def screen_geometry(self, all_screens: bool = False) -> ScreenGeometry:
        gm = self.user32.GetSystemMetrics
        monitors = gm(SM_CMONITORS)
        if all_screens:
            geo = ScreenGeometry(gm(SM_XVIRTUALSCREEN), gm(SM_YVIRTUALSCREEN), gm(SM_CXVIRTUALSCREEN), gm(SM_CYVIRTUALSCREEN))
        else:
            geo = ScreenGeometry(0, 0, gm(SM_CXSCREEN), gm(SM_CYSCREEN))
        geo.extra = {"monitors": monitors}
        return geo

    def capture(self, all_screens: bool = False, region: Optional[tuple[int, int, int, int]] = None) -> Image.Image:
        try:
            img = ImageGrab.grab(bbox=region, all_screens=all_screens)
        except Exception as exc:
            raise BackendError(f"Screen capture failed: {exc}") from exc
        if img.mode != "RGB":
            img = img.convert("RGB")
        return img

    # ------------------------------------------------------------------ mouse
    def mouse_position(self) -> tuple[int, int]:
        pt = wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(pt))
        return int(pt.x), int(pt.y)

    def _send(self, inputs: list[INPUT]) -> None:
        if not inputs:
            return
        arr = (INPUT * len(inputs))(*inputs)
        sent = self.user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
        if sent != len(inputs):
            err = ctypes.GetLastError() if hasattr(ctypes, "GetLastError") else 0
            raise BackendError(f"SendInput failed ({sent}/{len(inputs)} events sent, error {err}). "
                               "Is an elevated (admin) window in the foreground?")

    @staticmethod
    def _mouse_input(flags: int, data: int = 0) -> INPUT:
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.mi = MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0)
        return inp

    def mouse_move(self, x: int, y: int, duration: float = 0.2) -> None:
        x, y = int(x), int(y)
        sx, sy = self.mouse_position()
        steps = max(1, min(40, int(duration / 0.01))) if duration > 0 else 1
        for i in range(1, steps + 1):
            t = i / steps
            # ease-out for a slightly more natural motion
            t = 1 - (1 - t) ** 2
            cx = round(sx + (x - sx) * t)
            cy = round(sy + (y - sy) * t)
            self.user32.SetCursorPos(cx, cy)
            if steps > 1:
                time.sleep(duration / steps)
        self.user32.SetCursorPos(x, y)
        self._last_move = (x, y)
        # a zero-delta MOVE event makes hover states update in some apps
        self._send([self._mouse_input(MOUSEEVENTF_MOVE)])

    @staticmethod
    def _button_flags(button: str) -> tuple[int, int, int]:
        b = (button or "left").lower()
        if b in ("left", "primary", "l"):
            return MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 0
        if b in ("right", "secondary", "r"):
            return MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, 0
        if b in ("middle", "wheel", "m"):
            return MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, 0
        if b in ("x1", "back", "xbutton1"):
            return MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON1
        if b in ("x2", "forward", "xbutton2"):
            return MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON2
        raise BackendError(f"Unknown mouse button {button!r}")

    def mouse_click(self, x: Optional[int], y: Optional[int], button: str = "left", clicks: int = 1,
                    modifiers: Optional[list[str]] = None) -> None:
        if x is not None and y is not None:
            self.mouse_move(x, y, duration=0.15)
            time.sleep(0.05)
        down, up, data = self._button_flags(button)
        mods = [normalize_key(m) for m in (modifiers or [])]
        try:
            for m in mods:
                self._key(m, up=False)
            events: list[INPUT] = []
            for _ in range(max(1, int(clicks))):
                events.append(self._mouse_input(down, data))
                events.append(self._mouse_input(up, data))
            self._send(events)
        finally:
            for m in reversed(mods):
                self._key(m, up=True)

    def mouse_down(self, x: Optional[int], y: Optional[int], button: str = "left") -> None:
        if x is not None and y is not None:
            self.mouse_move(x, y, duration=0.15)
        down, _, data = self._button_flags(button)
        self._send([self._mouse_input(down, data)])

    def mouse_up(self, x: Optional[int], y: Optional[int], button: str = "left") -> None:
        if x is not None and y is not None:
            self.mouse_move(x, y, duration=0.15)
        _, up, data = self._button_flags(button)
        self._send([self._mouse_input(up, data)])

    def mouse_drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left", duration: float = 0.5) -> None:
        down, up, data = self._button_flags(button)
        self.mouse_move(x1, y1, duration=0.15)
        time.sleep(0.08)
        self._send([self._mouse_input(down, data)])
        time.sleep(0.12)
        try:
            self.mouse_move(x2, y2, duration=max(0.25, duration))
            time.sleep(0.1)
        finally:
            self._send([self._mouse_input(up, data)])

    def mouse_scroll(self, x: Optional[int], y: Optional[int], dx: int = 0, dy: int = 0) -> None:
        if x is not None and y is not None:
            self.mouse_move(x, y, duration=0.1)
            time.sleep(0.03)
        events: list[INPUT] = []
        # send one wheel notch per event; apps such as browsers behave better that way
        for _ in range(abs(int(dy))):
            events.append(self._mouse_input(MOUSEEVENTF_WHEEL, WHEEL_DELTA if dy > 0 else -WHEEL_DELTA))
        for _ in range(abs(int(dx))):
            events.append(self._mouse_input(MOUSEEVENTF_HWHEEL, WHEEL_DELTA if dx > 0 else -WHEEL_DELTA))
        for i in range(0, len(events), 5):
            self._send(events[i:i + 5])
            time.sleep(0.02)

    # --------------------------------------------------------------- keyboard
    def _vk_for_key(self, key: str) -> tuple[int, list[int]]:
        """Return (vk, extra_modifier_vks) for a canonical key name."""
        if key in VK_CODES and (len(key) > 1 or key.isalnum()):
            return VK_CODES[key], []
        if len(key) == 1:
            res = self.user32.VkKeyScanW(key)
            if res != -1:
                vk = res & 0xFF
                shift_state = (res >> 8) & 0xFF
                mods = []
                if shift_state & 1:
                    mods.append(0x10)  # shift
                if shift_state & 2:
                    mods.append(0x11)  # ctrl
                if shift_state & 4:
                    mods.append(0x12)  # alt
                return vk, mods
            if key.isalpha() and key.upper() in VK_CODES:
                return VK_CODES[key.upper()], []
            raise BackendError(f"Character {key!r} cannot be pressed on the current keyboard layout "
                               "(use type_text instead).")
        raise BackendError(f"Unknown key {key!r}")

    def _key_input(self, vk: int, up: bool) -> INPUT:
        scan = self.user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
        flags = KEYEVENTF_KEYUP if up else 0
        if vk in EXTENDED_VKS:
            flags |= KEYEVENTF_EXTENDEDKEY
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        return inp

    def _key(self, key: str, up: bool) -> None:
        vk, _ = self._vk_for_key(key)
        self._send([self._key_input(vk, up)])

    def press_keys(self, keys: list[str], repeat: int = 1) -> None:
        canonical = [normalize_key(k) for k in keys]
        mods = [k for k in canonical if k in MODIFIERS]
        others = [k for k in canonical if k not in MODIFIERS]
        for _ in range(max(1, int(repeat))):
            pressed: list[int] = []
            try:
                for m in mods:
                    vk, _ = self._vk_for_key(m)
                    self._send([self._key_input(vk, False)])
                    pressed.append(vk)
                    time.sleep(0.02)
                for k in others:
                    vk, extra = self._vk_for_key(k)
                    # e.g. '+' on a US layout needs shift; only add it if not already held
                    extra = [e for e in extra if e not in pressed and not (e == 0x10 and any(p in (0x10, 0xA0, 0xA1) for p in pressed))]
                    for e in extra:
                        self._send([self._key_input(e, False)])
                    self._send([self._key_input(vk, False), self._key_input(vk, True)])
                    for e in reversed(extra):
                        self._send([self._key_input(e, True)])
                    time.sleep(0.03)
                if not others and mods:
                    # a bare modifier tap (e.g. "win" to open the start menu)
                    pass
            finally:
                for vk in reversed(pressed):
                    self._send([self._key_input(vk, True)])
                    time.sleep(0.01)
            if repeat > 1:
                time.sleep(0.05)

    def type_text(self, text: str, interval: float = 0.0) -> None:
        if not text:
            return
        events: list[INPUT] = []

        def flush() -> None:
            nonlocal events
            if events:
                self._send(events)
                events = []

        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "\r":
                if i + 1 < len(text) and text[i + 1] == "\n":
                    i += 1
                    continue
                ch = "\n"
            if ch == "\n":
                flush()
                self.press_keys(["enter"])
            elif ch == "\t":
                flush()
                self.press_keys(["tab"])
            else:
                for unit in _utf16_units(ch):
                    down = INPUT()
                    down.type = INPUT_KEYBOARD
                    down.ki = KEYBDINPUT(0, unit, KEYEVENTF_UNICODE, 0, 0)
                    up = INPUT()
                    up.type = INPUT_KEYBOARD
                    up.ki = KEYBDINPUT(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)
                    events.extend([down, up])
                if interval > 0:
                    flush()
                    time.sleep(interval)
                elif len(events) >= 64:
                    flush()
                    time.sleep(0.01)
            i += 1
        flush()

    # --------------------------------------------------------------- programs
    def _shell_execute(self, target: str, args: str = "", cwd: Optional[str] = None) -> bool:
        """ShellExecuteEx without error dialogs; True when the shell accepted the request."""
        sei = SHELLEXECUTEINFOW()
        sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
        sei.fMask = SEE_MASK_FLAG_NO_UI | SEE_MASK_NOASYNC | SEE_MASK_NOCLOSEPROCESS
        sei.hwnd = None
        sei.lpVerb = "open"
        sei.lpFile = target
        sei.lpParameters = args or None
        sei.lpDirectory = cwd or None
        sei.nShow = SW_SHOWNORMAL
        ok = bool(self.shell32.ShellExecuteExW(ctypes.byref(sei)))
        if sei.hProcess:
            self.kernel32.CloseHandle(sei.hProcess)
        if not ok:
            log.debug("ShellExecuteEx(%r, %r) failed: hInstApp=%s", target, args, sei.hInstApp)
        return ok

    def _start_menu_shortcuts(self) -> list[tuple[str, str]]:
        if self._shortcut_cache is not None:
            return self._shortcut_cache
        roots = [
            Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
            Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
            Path(os.environ.get("USERPROFILE", "")) / "Desktop",
            Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop",
        ]
        found: list[tuple[str, str]] = []
        for root in roots:
            if not root.is_dir():
                continue
            for dirpath, _dirs, files in os.walk(root):
                for f in files:
                    if f.lower().endswith((".lnk", ".url", ".appref-ms")):
                        found.append((Path(f).stem, str(Path(dirpath) / f)))
        self._shortcut_cache = found
        return found

    def _start_apps(self) -> list[dict[str, str]]:
        if self._start_apps_cache is not None and time.time() - self._start_apps_time < 600:
            return self._start_apps_cache
        apps: list[dict[str, str]] = []
        try:
            res = self.run_command("Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress",
                                   shell="powershell", timeout=25)
            if res.exit_code == 0 and res.stdout.strip():
                data = json.loads(res.stdout)
                if isinstance(data, dict):
                    data = [data]
                apps = [{"name": str(d.get("Name", "")), "appid": str(d.get("AppID", ""))} for d in data]
        except Exception as exc:
            log.debug("Get-StartApps failed: %s", exc)
        self._start_apps_cache = apps
        self._start_apps_time = time.time()
        return apps

    @staticmethod
    def _best_match(query: str, candidates: list[str]) -> Optional[int]:
        q = _norm_name(query)
        if not q:
            return None
        norm = [_norm_name(c) for c in candidates]
        for i, n in enumerate(norm):
            if n == q:
                return i
        starts = [i for i, n in enumerate(norm) if n.startswith(q)]
        if starts:
            return min(starts, key=lambda i: len(norm[i]))
        contains = [i for i, n in enumerate(norm) if q in n]
        if contains:
            return min(contains, key=lambda i: len(norm[i]))
        close = difflib.get_close_matches(q, norm, n=1, cutoff=0.8)
        if close:
            return norm.index(close[0])
        return None

    def open_application(self, name: str, args: str = "") -> str:
        target = (name or "").strip().strip('"')
        if not target:
            raise BackendError("Application name is empty.")
        args = (args or "").strip()

        # 1. explicit path / URL / document
        expanded = os.path.expandvars(os.path.expanduser(target))
        if os.path.exists(expanded):
            if self._shell_execute(expanded, args):
                return f"Opened path {expanded!r}."
        if re.match(r"^[a-z][a-z0-9+.-]+:", target, re.I) and not re.match(r"^[a-z]:[\\/]", target, re.I):
            if self._shell_execute(target, args):
                return f"Opened URI {target!r}."

        # 2. alias table
        alias = APP_ALIASES.get(target.lower())
        if alias:
            exe, _, alias_args = alias.partition(" ")
            if self._shell_execute(exe, (alias_args + " " + args).strip()):
                return f"Launched {target!r} via alias -> {alias!r}."

        # 3. ShellExecute directly (PATH, App Paths registry, e.g. "chrome", "winword")
        for candidate in dict.fromkeys([target, target + ".exe", target.replace(" ", "") + ".exe"]):
            if self._shell_execute(candidate, args):
                return f"Launched {candidate!r} via ShellExecute."

        # 4. Start menu shortcuts (.lnk)
        shortcuts = self._start_menu_shortcuts()
        idx = self._best_match(target, [s[0] for s in shortcuts])
        if idx is not None:
            stem, path = shortcuts[idx]
            if self._shell_execute(path, args):
                return f"Launched Start-menu shortcut {stem!r} ({path})."

        # 5. Start apps incl. UWP (Get-StartApps -> shell:AppsFolder)
        apps = self._start_apps()
        idx = self._best_match(target, [a["name"] for a in apps])
        if idx is not None:
            app = apps[idx]
            if self._shell_execute("explorer.exe", f"shell:AppsFolder\\{app['appid']}"):
                return f"Launched Start app {app['name']!r} (AppID {app['appid']})."

        raise BackendError(
            f"Could not find an application matching {target!r}. Try the exact executable name "
            "(e.g. 'notepad.exe'), the full path, or open the Start menu (press 'win') and search."
        )

    def open_url(self, url: str) -> str:
        url = (url or "").strip()
        if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I) and not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
            url = "https://" + url
        if self._shell_execute(url):
            return f"Opened {url} in the default handler."
        raise BackendError(f"Could not open URL {url!r}.")

    def run_command(self, command: str, shell: str = "powershell", timeout: float = 60.0,
                    cwd: Optional[str] = None) -> CommandResult:
        shell = (shell or "powershell").lower()
        if shell in ("powershell", "ps", "pwsh"):
            exe = "pwsh.exe" if shell == "pwsh" else "powershell.exe"
            prefix = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
                      "$OutputEncoding=[System.Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; ")
            # -EncodedCommand side-steps every command-line quoting problem.
            encoded = base64.b64encode((prefix + command).encode("utf-16-le")).decode("ascii")
            argv: str | list[str] = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                     "-OutputFormat", "Text", "-EncodedCommand", encoded]
        elif shell in ("cmd", "bat"):
            # Pass a single string so Python does not mangle the quotes; /s strips the outer pair.
            argv = f'cmd.exe /d /s /c "chcp 65001>nul & {command}"'
        else:
            raise BackendError(f"Unsupported shell {shell!r} (use 'powershell' or 'cmd').")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        start = time.time()
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=timeout, cwd=cwd or None,
                                  creationflags=flags, stdin=subprocess.DEVNULL)
            return CommandResult(command, shell, proc.returncode, _decode(proc.stdout), _decode(proc.stderr),
                                 False, time.time() - start)
        except subprocess.TimeoutExpired as exc:
            return CommandResult(command, shell, None, _decode(exc.stdout or b""), _decode(exc.stderr or b""),
                                 True, time.time() - start)
        except FileNotFoundError as exc:
            raise BackendError(f"Shell executable not found: {exc}") from exc

    # ---------------------------------------------------------------- windows
    def _window_text(self, hwnd: int) -> str:
        length = self.user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    def _class_name(self, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def _rect(self, hwnd: int) -> tuple[int, int, int, int]:
        rect = wintypes.RECT()
        if self.dwmapi is not None:
            hr = self.dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect))
            if hr == 0 and (rect.right - rect.left) > 0:
                return rect.left, rect.top, rect.right, rect.bottom
        self.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return rect.left, rect.top, rect.right, rect.bottom

    def _is_cloaked(self, hwnd: int) -> bool:
        if self.dwmapi is None:
            return False
        val = wintypes.DWORD(0)
        hr = self.dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val))
        return hr == 0 and val.value != 0

    def _pid_of(self, hwnd: int) -> int:
        pid = wintypes.DWORD(0)
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def _process_name(self, pid: int, cache: dict[int, str]) -> str:
        if pid in cache:
            return cache[pid]
        name = ""
        h = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if h:
            try:
                size = wintypes.DWORD(1024)
                buf = ctypes.create_unicode_buffer(size.value)
                if self.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    name = os.path.basename(buf.value)
            finally:
                self.kernel32.CloseHandle(h)
        cache[pid] = name
        return name

    def list_windows(self) -> list[WindowInfo]:
        handles: list[int] = []

        @WNDENUMPROC
        def _cb(hwnd, _lparam):
            handles.append(int(hwnd))
            return True

        self.user32.EnumWindows(_cb, 0)
        active = int(self.user32.GetForegroundWindow() or 0)
        result: list[WindowInfo] = []
        cache: dict[int, str] = {}
        skip_classes = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Windows.UI.Core.CoreWindow",
                        "IME", "MSCTFIME UI", "Button", "tooltips_class32", "ForegroundStaging", "XamlExplorerHostIslandWindow"}
        for hwnd in handles:
            if not self.user32.IsWindowVisible(hwnd):
                continue
            title = self._window_text(hwnd)
            if not title:
                continue
            cls = self._class_name(hwnd)
            if cls in skip_classes:
                continue
            ex = self.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if ex & WS_EX_TOOLWINDOW and not ex & WS_EX_APPWINDOW:
                continue
            if self._is_cloaked(hwnd):
                continue
            left, top, right, bottom = self._rect(hwnd)
            if right - left <= 0 or bottom - top <= 0:
                if not self.user32.IsIconic(hwnd):
                    continue
            pid = self._pid_of(hwnd)
            result.append(WindowInfo(
                hwnd=hwnd, title=title, class_name=cls, left=left, top=top, right=right, bottom=bottom,
                pid=pid, process_name=self._process_name(pid, cache),
                is_minimized=bool(self.user32.IsIconic(hwnd)), is_maximized=bool(self.user32.IsZoomed(hwnd)),
                is_active=(hwnd == active),
            ))
        # active window first, then z-order as enumerated
        result.sort(key=lambda w: (not w.is_active,))
        return result

    def active_window(self) -> Optional[WindowInfo]:
        hwnd = int(self.user32.GetForegroundWindow() or 0)
        if not hwnd:
            return None
        cache: dict[int, str] = {}
        left, top, right, bottom = self._rect(hwnd)
        pid = self._pid_of(hwnd)
        return WindowInfo(hwnd=hwnd, title=self._window_text(hwnd), class_name=self._class_name(hwnd),
                          left=left, top=top, right=right, bottom=bottom, pid=pid,
                          process_name=self._process_name(pid, cache),
                          is_minimized=bool(self.user32.IsIconic(hwnd)),
                          is_maximized=bool(self.user32.IsZoomed(hwnd)), is_active=True)

    def focus_window(self, hwnd: int) -> bool:
        hwnd = int(hwnd)
        if not self.user32.IsWindow(hwnd):
            raise BackendError(f"Window handle {hwnd} is not valid anymore.")
        if self.user32.IsIconic(hwnd):
            self.user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.15)
        # Windows refuses SetForegroundWindow unless our thread "owns" input;
        # a synthetic ALT tap is the classic, well-behaved workaround.
        alt = self._key_input(0x12, False), self._key_input(0x12, True)
        self._send(list(alt))
        self.user32.SetForegroundWindow(hwnd)
        self.user32.BringWindowToTop(hwnd)
        time.sleep(0.1)
        if int(self.user32.GetForegroundWindow() or 0) == hwnd:
            return True
        # fallback: attach to the foreground thread's input queue
        fg = self.user32.GetForegroundWindow()
        cur = self.kernel32.GetCurrentThreadId()
        fg_thread = self.user32.GetWindowThreadProcessId(fg, None) if fg else 0
        if fg_thread and fg_thread != cur:
            self.user32.AttachThreadInput(cur, fg_thread, True)
            try:
                self.user32.SetForegroundWindow(hwnd)
                self.user32.BringWindowToTop(hwnd)
            finally:
                self.user32.AttachThreadInput(cur, fg_thread, False)
            time.sleep(0.1)
        return int(self.user32.GetForegroundWindow() or 0) == hwnd

    def window_action(self, hwnd: int, action: str, x: Optional[int] = None, y: Optional[int] = None,
                      width: Optional[int] = None, height: Optional[int] = None) -> str:
        hwnd = int(hwnd)
        if not self.user32.IsWindow(hwnd):
            raise BackendError(f"Window handle {hwnd} is not valid anymore.")
        action = (action or "").lower()
        if action == "minimize":
            self.user32.ShowWindow(hwnd, SW_MINIMIZE)
        elif action == "maximize":
            self.user32.ShowWindow(hwnd, SW_MAXIMIZE)
        elif action in ("restore", "show"):
            self.user32.ShowWindow(hwnd, SW_RESTORE)
        elif action == "close":
            self.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        elif action in ("move", "resize", "move_resize"):
            flags = SWP_NOZORDER | SWP_NOACTIVATE
            if x is None or y is None:
                flags |= SWP_NOMOVE
            if width is None or height is None:
                flags |= SWP_NOSIZE
            if self.user32.IsZoomed(hwnd):
                self.user32.ShowWindow(hwnd, SW_RESTORE)
            ok = self.user32.SetWindowPos(hwnd, None, int(x or 0), int(y or 0), int(width or 0), int(height or 0), flags)
            if not ok:
                raise BackendError("SetWindowPos failed (window may belong to an elevated process).")
        elif action in ("focus", "activate"):
            if not self.focus_window(hwnd):
                return "Focus requested but Windows did not switch the foreground window (try clicking it)."
        else:
            raise BackendError(f"Unknown window action {action!r}.")
        time.sleep(0.2)
        left, top, right, bottom = self._rect(hwnd)
        return f"Window {hwnd}: {action} done. New rect=({left},{top})-({right},{bottom})."

    def window_controls(self, hwnd: int, limit: int = 200) -> list[ControlInfo]:
        hwnd = int(hwnd)
        controls: list[ControlInfo] = []

        @WNDENUMPROC
        def _cb(child, _lparam):
            if len(controls) >= limit:
                return False
            visible = bool(self.user32.IsWindowVisible(child))
            if not visible:
                return True
            cls = self._class_name(child)
            text = self._window_text(child)
            rect = wintypes.RECT()
            self.user32.GetWindowRect(child, ctypes.byref(rect))
            if rect.right - rect.left <= 0 or rect.bottom - rect.top <= 0:
                return True
            controls.append(ControlInfo(int(child), cls, text[:200], rect.left, rect.top, rect.right, rect.bottom,
                                        visible, bool(self.user32.IsWindowEnabled(child))))
            return True

        self.user32.EnumChildWindows(hwnd, _cb, 0)
        return controls

    # -------------------------------------------------------------- clipboard
    def _open_clipboard(self) -> None:
        for _ in range(10):
            if self.user32.OpenClipboard(None):
                return
            time.sleep(0.05)
        raise BackendError("Clipboard is busy (another application holds it).")

    def clipboard_get(self) -> str:
        self._open_clipboard()
        try:
            if not self.user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return ""
            handle = self.user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = self.kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                self.kernel32.GlobalUnlock(handle)
        finally:
            self.user32.CloseClipboard()

    def clipboard_set(self, text: str) -> None:
        data = (text or "").encode("utf-16-le") + b"\x00\x00"
        self._open_clipboard()
        try:
            self.user32.EmptyClipboard()
            handle = self.kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                raise BackendError("GlobalAlloc failed.")
            ptr = self.kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, data, len(data))
            self.kernel32.GlobalUnlock(handle)
            if not self.user32.SetClipboardData(CF_UNICODETEXT, handle):
                self.kernel32.GlobalFree(handle)
                raise BackendError("SetClipboardData failed.")
        finally:
            self.user32.CloseClipboard()

    # ------------------------------------------------------------------- misc
    def system_info(self) -> dict[str, Any]:
        geo = self.screen_geometry(all_screens=True)
        primary = self.screen_geometry(all_screens=False)
        try:
            hkl = int(self.user32.GetKeyboardLayout(0) or 0) & 0xFFFF
        except Exception:
            hkl = 0
        try:
            self.user32.GetDpiForSystem.argtypes = []
            self.user32.GetDpiForSystem.restype = wintypes.UINT
            dpi = int(self.user32.GetDpiForSystem())
        except Exception:
            dpi = 96
        return {
            "os": f"{platform.system()} {platform.release()} (build {platform.version()})",
            "machine": platform.machine(),
            "user": os.environ.get("USERNAME", ""),
            "computer": os.environ.get("COMPUTERNAME", ""),
            "primary_screen": {"width": primary.width, "height": primary.height},
            "virtual_screen": geo.to_dict(),
            "monitors": geo.extra.get("monitors", 1),
            "system_dpi": dpi,
            "scale_percent": round(dpi / 96 * 100),
            "keyboard_layout": LANGID_NAMES.get(hkl, hex(hkl)),
            "local_time": time.strftime("%Y-%m-%d %H:%M"),
            "python": platform.python_version(),
            "stop_hotkey": self.stop_hotkey,
        }

    def close(self) -> None:
        if self._hotkey:
            self._hotkey.stop()
            self._hotkey = None


def _utf16_units(ch: str) -> list[int]:
    raw = ch.encode("utf-16-le")
    return [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]


def _decode(data: bytes) -> str:
    if not data:
        return ""
    for enc in ("utf-8", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
