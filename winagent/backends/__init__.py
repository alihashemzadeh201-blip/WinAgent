"""Desktop backends: real Windows (Win32 API) and a simulated one for tests/demos."""

from __future__ import annotations

import logging
import sys
from typing import Callable, Optional

from .base import BackendError, CommandResult, ControlInfo, DesktopBackend, EmergencyStop, ScreenGeometry, WindowInfo  # noqa: F401
from .fake import FakeBackend  # noqa: F401


def create_backend(kind: str = "auto", *, stop_hotkey: Optional[str] = "ctrl+alt+esc",
                   on_emergency_stop: Optional[Callable[[], None]] = None) -> DesktopBackend:
    """Instantiate the desktop backend.

    ``auto`` always means a real desktop, currently supported only on Windows.
    Simulation must be requested explicitly (``fake`` / ``--demo``); it is
    never a fallback for an unsupported platform or a failed Windows backend.
    """
    kind = (kind or "auto").strip().lower()
    if kind == "auto":
        if sys.platform != "win32":
            raise BackendError("Real desktop capture/control is supported only on Windows. "
                               "Run WinAgent with Windows Python (not WSL, Docker or a remote Linux server). "
                               "For a simulated demo only, use --demo or --backend fake; it cannot capture your screen.")
        kind = "windows"
    if kind == "windows":
        from .windows import WindowsBackend

        return WindowsBackend(stop_hotkey=stop_hotkey or "", on_emergency_stop=on_emergency_stop)
    if kind == "fake":
        logging.getLogger(__name__).warning("DEMO backend: screenshots and desktop actions are simulated, not your real screen. "
                                            "Use --backend windows on Windows for real capture/control.")
        return FakeBackend()
    raise ValueError(f"Unknown backend {kind!r} (expected auto, windows or fake)")
