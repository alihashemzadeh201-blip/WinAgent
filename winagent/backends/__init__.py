"""Desktop backends: real Windows (Win32 API) and a simulated one for tests/demos."""

from __future__ import annotations

import sys
from typing import Callable, Optional

from .base import BackendError, CommandResult, ControlInfo, DesktopBackend, EmergencyStop, ScreenGeometry, WindowInfo  # noqa: F401
from .fake import FakeBackend  # noqa: F401


def create_backend(kind: str = "auto", *, stop_hotkey: Optional[str] = "ctrl+alt+esc",
                   on_emergency_stop: Optional[Callable[[], None]] = None) -> DesktopBackend:
    """Instantiate the desktop backend.

    ``auto`` picks the real Windows backend on Windows and the simulated one
    elsewhere (so the GUI can still be explored on other operating systems).
    """
    kind = (kind or "auto").lower()
    if kind == "auto":
        kind = "windows" if sys.platform == "win32" else "fake"
    if kind == "windows":
        from .windows import WindowsBackend

        return WindowsBackend(stop_hotkey=stop_hotkey or "", on_emergency_stop=on_emergency_stop)
    if kind == "fake":
        return FakeBackend()
    raise ValueError(f"Unknown backend {kind!r} (expected auto, windows or fake)")
