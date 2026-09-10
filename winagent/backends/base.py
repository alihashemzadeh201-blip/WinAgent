"""Abstract desktop backend.

A *backend* is the thin layer that actually touches the operating system:
mouse, keyboard, screen capture, windows, processes, clipboard and files.
The agent, the tool executor and the GUI never call OS APIs directly; they go
through this interface so that the whole stack can be tested (or demoed on a
non-Windows machine) with :class:`winagent.backends.fake.FakeBackend`.

All coordinates handled by a backend are **physical screen pixels**.  The
conversion from the (scaled) screenshot coordinate system used by the LLM is
done in :mod:`winagent.tools.executor`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from PIL import Image


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    class_name: str = ""
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0
    pid: int = 0
    process_name: str = ""
    is_minimized: bool = False
    is_maximized: bool = False
    is_active: bool = False

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["width"] = self.width
        d["height"] = self.height
        return d


@dataclass
class ControlInfo:
    hwnd: int
    class_name: str
    text: str
    left: int
    top: int
    right: int
    bottom: int
    visible: bool = True
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CommandResult:
    command: str
    shell: str
    exit_code: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool = False
    duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScreenGeometry:
    """Physical geometry of the captured area (virtual screen or primary)."""

    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BackendError(RuntimeError):
    """Raised when an OS level operation fails in a way the LLM should know about."""


class EmergencyStop(RuntimeError):
    """Raised when the user triggered the fail-safe (mouse corner / hotkey)."""


class DesktopBackend(ABC):
    """Interface implemented by :class:`WindowsBackend` and :class:`FakeBackend`."""

    name: str = "abstract"

    # ------------------------------------------------------------------ screen
    @abstractmethod
    def screen_geometry(self, all_screens: bool = False) -> ScreenGeometry: ...

    @abstractmethod
    def capture(self, all_screens: bool = False, region: Optional[tuple[int, int, int, int]] = None) -> Image.Image:
        """Capture the screen (or ``region`` = (left, top, right, bottom) in physical px)."""

    # ------------------------------------------------------------------- mouse
    @abstractmethod
    def mouse_position(self) -> tuple[int, int]: ...

    @abstractmethod
    def mouse_move(self, x: int, y: int, duration: float = 0.2) -> None: ...

    @abstractmethod
    def mouse_click(
        self,
        x: Optional[int],
        y: Optional[int],
        button: str = "left",
        clicks: int = 1,
        modifiers: Optional[list[str]] = None,
    ) -> None: ...

    @abstractmethod
    def mouse_down(self, x: Optional[int], y: Optional[int], button: str = "left") -> None: ...

    @abstractmethod
    def mouse_up(self, x: Optional[int], y: Optional[int], button: str = "left") -> None: ...

    @abstractmethod
    def mouse_drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left", duration: float = 0.5) -> None: ...

    @abstractmethod
    def mouse_scroll(self, x: Optional[int], y: Optional[int], dx: int = 0, dy: int = 0) -> None:
        """Scroll ``dy`` clicks vertically (positive = up) and ``dx`` horizontally (positive = right)."""

    # ---------------------------------------------------------------- keyboard
    @abstractmethod
    def type_text(self, text: str, interval: float = 0.0) -> None: ...

    @abstractmethod
    def press_keys(self, keys: list[str], repeat: int = 1) -> None:
        """Press a key combination such as ``["ctrl", "c"]`` (all held together)."""

    # ---------------------------------------------------------------- programs
    @abstractmethod
    def open_application(self, name: str, args: str = "") -> str:
        """Launch a program by name/path.  Returns a human readable description."""

    @abstractmethod
    def open_url(self, url: str) -> str: ...

    @abstractmethod
    def run_command(self, command: str, shell: str = "powershell", timeout: float = 60.0, cwd: Optional[str] = None) -> CommandResult: ...

    # ----------------------------------------------------------------- windows
    @abstractmethod
    def list_windows(self) -> list[WindowInfo]: ...

    @abstractmethod
    def active_window(self) -> Optional[WindowInfo]: ...

    @abstractmethod
    def focus_window(self, hwnd: int) -> bool: ...

    @abstractmethod
    def window_action(self, hwnd: int, action: str, x: Optional[int] = None, y: Optional[int] = None,
                      width: Optional[int] = None, height: Optional[int] = None) -> str: ...

    @abstractmethod
    def window_controls(self, hwnd: int, limit: int = 200) -> list[ControlInfo]: ...

    # --------------------------------------------------------------- clipboard
    @abstractmethod
    def clipboard_get(self) -> str: ...

    @abstractmethod
    def clipboard_set(self, text: str) -> None: ...

    # ------------------------------------------------- keyboard layout / input
    def active_keyboard_layout(self) -> str:
        """Name of the input language currently active for the foreground window (e.g. 'en-US')."""
        return "unknown"

    def set_keyboard_layout(self, name: str) -> str:
        """Activate keyboard layout ``name`` for the foreground window; return the active layout."""
        return self.active_keyboard_layout()

    # ------------------------------------------------------------------- menus
    def menu_structure(self, hwnd: Optional[int] = None) -> Optional[list[dict[str, Any]]]:
        """Enumerable menu-bar tree of ``hwnd`` (or the active window), or None when the window
        has no (classic) menu. Nodes: {"text", "mnemonic", "enabled", "items":[...]} /
        {"separator": True}."""
        return None

    def open_menu_items(self) -> list[dict[str, Any]]:
        """Items of the popup menu (context menu / submenu) currently open on screen, or []."""
        return []

    # -------------------------------------------------------------------- misc
    @abstractmethod
    def system_info(self) -> dict[str, Any]: ...

    def find_window(self, title: Optional[str] = None, hwnd: Optional[int] = None) -> Optional[WindowInfo]:
        """Locate a window by handle or by case-insensitive title substring."""
        windows = self.list_windows()
        if hwnd:
            for w in windows:
                if w.hwnd == int(hwnd):
                    return w
            return None
        if title:
            needle = title.strip().lower()
            # exact (case-insensitive) match first, then substring, then process name
            for w in windows:
                if w.title.lower() == needle:
                    return w
            for w in windows:
                if needle in w.title.lower():
                    return w
            for w in windows:
                if needle and needle in (w.process_name or "").lower():
                    return w
        return None

    def close(self) -> None:  # noqa: B027 - default no-op
        """Release resources (hotkeys, threads)."""
        return None
