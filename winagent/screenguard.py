"""Keep the agent's own user interface out of its screenshots and clicks.

The GUI lives on the very desktop the agent operates.  Whenever a screenshot
is taken, or the agent clicks at a point that lies under one of our windows,
the UI has to get out of the way for a moment.  :class:`ScreenGuard` is the
tiny protocol the tool executor talks to.  It is deliberately free of any Qt
dependency: the executor and the CLI use this no-op base class, the GUI
installs a subclass that hides / re-shows its windows on the Qt thread.

Typical use inside the executor (all calls happen in the worker thread)::

    with guard.scope():                       # one tool call
        with guard.shield(point=(x, y)):
            backend.mouse_click(x, y)         # UI hidden only if it covers (x, y)
        with guard.shield(rect=capture_rect, for_capture=True):
            backend.capture()                 # UI hidden only if it overlaps the capture
    # -> the UI is restored once, when the outermost scope ends
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

log = logging.getLogger(__name__)

Rect = tuple[int, int, int, int]   # left, top, right, bottom (physical pixels)
Point = tuple[int, int]            # x, y (physical pixels)


def rect_from_points(*points: Point, margin: int = 0) -> Rect:
    """Bounding box of ``points`` grown by ``margin`` pixels."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin


def rects_overlap(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


class ScreenGuard:
    """No-op base class; subclasses override :meth:`hide_windows` / :meth:`show_windows`.

    The base class implements the bookkeeping: nested scopes, "hide once,
    restore once", and never letting a UI problem break a tool call.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._depth = 0
        self._hidden = False
        self.hide_count = 0        # how often the UI had to be hidden (diagnostics / tests)

    # ------------------------------------------------------ override points
    def hide_windows(self, rect: Optional[Rect], point: Optional[Point], for_capture: bool = False) -> float:
        """Hide UI windows that overlap ``rect`` or contain ``point`` (both physical px).

        ``rect is None and point is None`` means "hide everything".
        ``for_capture`` tells the implementation that a screenshot is about to
        be taken (windows the OS already excludes from capture may stay).
        Windows that are already hidden stay hidden.  Returns the number of
        seconds the caller should wait for the screen to settle, or a negative
        number when nothing (new) had to be hidden.
        """
        return -1.0

    def show_windows(self) -> None:
        """Bring back whatever :meth:`hide_windows` hid."""

    def ui_has_focus(self) -> bool:
        """True when one of our own windows currently has keyboard focus."""
        return False

    # ------------------------------------------------------------------ API
    @property
    def hidden(self) -> bool:
        return self._hidden

    @contextmanager
    def scope(self) -> Iterator[None]:
        """Group several ``shield`` requests: the UI comes back when the outermost scope ends."""
        with self._lock:
            self._depth += 1
        try:
            yield
        finally:
            self._leave()

    @contextmanager
    def shield(self, *, rect: Optional[Rect] = None, point: Optional[Point] = None,
               for_capture: bool = False) -> Iterator[bool]:
        """Hide the UI while the body runs if it overlaps ``rect`` / contains ``point``.

        Yields ``True`` when the UI is hidden during the body.
        """
        with self._lock:
            self._depth += 1
        settle = -1.0
        try:
            # every request is forwarded: windows hidden earlier in this scope stay hidden, additional
            # windows that cover the new area are hidden now; the implementation reports whether it had to act
            settle = float(self.hide_windows(rect, point, for_capture))
        except Exception:  # pragma: no cover - a UI hiccup must never break a tool
            log.exception("ScreenGuard.hide_windows failed")
        if settle >= 0:
            with self._lock:
                self._hidden = True
                self.hide_count += 1
            if settle > 0:
                time.sleep(settle)
        try:
            yield self._hidden
        finally:
            self._leave()

    def _leave(self) -> None:
        restore = False
        with self._lock:
            self._depth -= 1
            if self._depth <= 0:
                self._depth = 0
                if self._hidden:
                    self._hidden = False
                    restore = True
        if restore:
            try:
                self.show_windows()
            except Exception:  # pragma: no cover
                log.exception("ScreenGuard.show_windows failed")


class RecordingGuard(ScreenGuard):
    """Test helper: pretends to hide the UI and records every request."""

    def __init__(self, settle: float = 0.0) -> None:
        super().__init__()
        self.settle = settle
        self.requests: list[tuple[Optional[Rect], Optional[Point], bool]] = []
        self.shown = 0
        self.visible = True

    def hide_windows(self, rect, point, for_capture=False):
        self.requests.append((rect, point, for_capture))
        if not self.visible:
            return -1.0          # already hidden: nothing new to do
        self.visible = False
        return self.settle

    def show_windows(self):
        self.visible = True
        self.shown += 1
