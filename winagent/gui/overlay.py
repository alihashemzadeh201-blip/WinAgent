"""Small always-on-top status overlay + the Qt implementation of :class:`ScreenGuard`.

While the agent works, the main window gets in the way: it covers the desktop
the agent has to see and click on.  Instead of simply minimising it (and
leaving the user staring at a screen that seems to do things on its own),
the GUI can

* show a compact, frameless, always-on-top **overlay** in a screen corner that
  tells the user what the agent is doing right now (step, current action,
  elapsed time) and offers a Stop button, and/or
* keep the main window on screen and hide it **only for the instant** of a
  screenshot or of a click/keystroke that would land on it.

The overlay never takes keyboard focus (``WindowDoesNotAcceptFocus`` +
``WA_ShowWithoutActivating``) so the agent's keystrokes keep going to the
application it is operating, and on Windows 10 2004+ it is excluded from
screen capture with ``SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`` so it
never shows up in the model's screenshots.  (That API rejects per-pixel-alpha
layered windows, which is why the overlay is an opaque, masked window rather
than a translucent one.)  Where the API is not available (older Windows, other
platforms) the overlay is hidden for the duration of the capture like any
other window of ours.

:class:`QtScreenGuard` bridges the worker thread (tool executor) and the Qt
main thread: hiding happens through a ``BlockingQueuedConnection`` so the
executor can wait until the windows are really gone before it captures.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from typing import Callable, Optional

from PySide6.QtCore import QCoreApplication, QObject, QPoint, QRectF, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication, QMouseEvent, QPainterPath, QRegion
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..screenguard import Point, Rect, ScreenGuard, rects_overlap
from .theme import PALETTE

log = logging.getLogger(__name__)

WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011

CORNERS = ("bottom-right", "bottom-left", "top-right", "top-left")


# ---------------------------------------------------------------- helpers
def exclude_from_capture(widget: QWidget, exclude: bool = True) -> bool:
    """Windows 10 2004+: keep ``widget``'s top-level window out of screen captures.

    Returns True when the OS confirmed the request.  On other platforms or
    older Windows versions this is a no-op returning False and callers fall
    back to hiding the window while capturing.
    """
    if sys.platform != "win32":
        return False
    if exclude and getattr(sys.getwindowsversion(), "build", 0) < 19041:
        return False   # before Windows 10 2004 the flag degrades to WDA_MONITOR (black box in screenshots)
    try:
        hwnd = int(widget.winId())
        user32 = ctypes.windll.user32
        user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.SetWindowDisplayAffinity.restype = ctypes.c_int
        ok = bool(user32.SetWindowDisplayAffinity(ctypes.c_void_p(hwnd), WDA_EXCLUDEFROMCAPTURE if exclude else WDA_NONE))
        if ok and exclude:
            # Pre-2004 Windows silently downgrades to WDA_MONITOR, which paints a black box into screenshots
            # instead of nothing – verify the flag really stuck, otherwise undo it.
            value = ctypes.c_uint(0)
            user32.GetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
            user32.GetWindowDisplayAffinity.restype = ctypes.c_int
            if user32.GetWindowDisplayAffinity(ctypes.c_void_p(hwnd), ctypes.byref(value)):
                ok = value.value == WDA_EXCLUDEFROMCAPTURE
            if not ok:
                user32.SetWindowDisplayAffinity(ctypes.c_void_p(hwnd), WDA_NONE)
        log.debug("exclude_from_capture(%s) -> %s", hwnd, ok)
        return ok
    except Exception as exc:  # pragma: no cover - depends on the OS
        log.debug("SetWindowDisplayAffinity failed: %s", exc)
        return False


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def wait_for_compositor() -> None:
    """Block until the Desktop Window Manager has composed a frame without the windows we just hid."""
    if sys.platform != "win32":
        return
    try:
        dwm = ctypes.windll.dwmapi
        dwm.DwmFlush()
        dwm.DwmFlush()
    except Exception:  # pragma: no cover - no DWM (remote session / very old Windows)
        pass


def physical_frame_rect(widget: QWidget) -> Rect:
    """Frame rectangle of a top-level widget in *physical* screen pixels (the backend's coordinate system)."""
    if sys.platform == "win32":
        try:
            rect = _RECT()
            user32 = ctypes.windll.user32
            user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
            user32.GetWindowRect.restype = ctypes.c_int
            if user32.GetWindowRect(ctypes.c_void_p(int(widget.winId())), ctypes.byref(rect)):
                return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
        except Exception as exc:  # pragma: no cover
            log.debug("GetWindowRect failed: %s", exc)
    fg = widget.frameGeometry()
    ratio = float(widget.devicePixelRatioF() or 1.0)
    screen = widget.screen() or QGuiApplication.primaryScreen()
    if screen is not None and ratio != 1.0:
        # logical -> physical relative to the screen origin (each screen is scaled around its own origin)
        sg = screen.geometry()
        ox, oy = int(round(sg.left() * ratio)), int(round(sg.top() * ratio))
        left = ox + int(round((fg.left() - sg.left()) * ratio))
        top = oy + int(round((fg.top() - sg.top()) * ratio))
        return left, top, left + int(round(fg.width() * ratio)), top + int(round(fg.height() * ratio))
    return fg.left(), fg.top(), fg.right() + 1, fg.bottom() + 1


def on_gui_thread() -> bool:
    app = QCoreApplication.instance()
    return app is not None and QThread.currentThread() == app.thread()


# ---------------------------------------------------------------- overlay
class StatusOverlay(QWidget):
    """Compact always-on-top status panel shown in a screen corner while the agent works.

    It is created *without* a parent on purpose: an owned tool window would be
    hidden by Windows together with its (minimised) owner.
    """

    stop_requested = Signal()
    show_main_requested = Signal()

    WIDTH = 330

    def __init__(self, corner: str = "bottom-right", exclude_from_capture: bool = True):
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.WindowDoesNotAcceptFocus)
        super().__init__(None, flags)
        self.setObjectName("StatusOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        # NOTE: no WA_TranslucentBackground on purpose – a per-pixel-alpha layered window cannot be excluded
        # from screen capture on Windows; rounded corners come from a window mask instead (see resizeEvent).
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWindowTitle("WinAgent status")
        self.corner = corner if corner in CORNERS else "bottom-right"
        self.want_capture_exclusion = exclude_from_capture
        self.capture_excluded = False      # True once the OS keeps the overlay out of screenshots
        self._drag_offset: Optional[QPoint] = None
        self._user_moved = False
        self._started = 0.0
        self._pulse = False
        self._paused = False
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(600)
        self._timer.timeout.connect(self._tick)

    # .............................................................. UI
    def _build(self) -> None:
        p = PALETTE
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.card = QFrame()
        self.card.setObjectName("OverlayCard")
        self.card.setFixedWidth(self.WIDTH)
        self.card.setStyleSheet(
            f"QFrame#OverlayCard {{ background: {p['panel']}; border: 1px solid {p['accent']}; border-radius: 12px; }}"
            f"QLabel {{ background: transparent; color: {p['text']}; }}"
            f"QLabel#OverlayTitle {{ font-weight: 600; color: {p['accent']}; }}"
            f"QLabel#OverlayMuted {{ color: {p['muted']}; font-size: 9pt; }}"
            f"QLabel#OverlayTool {{ color: {p['success']}; font-family: Consolas, 'Cascadia Mono', monospace; font-size: 9pt; }}"
            f"QPushButton {{ background: {p['panel2']}; border: 1px solid {p['border']}; border-radius: 7px; color: {p['text']};"
            " padding: 3px 9px; min-height: 14px; font-size: 9pt; }"
            f"QPushButton:hover {{ border-color: {p['accent']}; }}"
            f"QPushButton#OverlayStop {{ background: {p['error']}; border-color: {p['error']}; color: white; font-weight: 600; }}"
        )
        outer.addWidget(self.card)
        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.dot = QLabel("●")
        head.addWidget(self.dot)
        self.title = QLabel("WinAgent is working")
        self.title.setObjectName("OverlayTitle")
        head.addWidget(self.title, 1)
        self.elapsed = QLabel("0:00")
        self.elapsed.setObjectName("OverlayMuted")
        head.addWidget(self.elapsed)
        lay.addLayout(head)

        self.status = QLabel("Starting…")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.tool = QLabel("")
        self.tool.setObjectName("OverlayTool")
        self.tool.setWordWrap(True)
        self.tool.hide()
        lay.addWidget(self.tool)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.step = QLabel("")
        self.step.setObjectName("OverlayMuted")
        row.addWidget(self.step, 1)
        self.btn_show = QPushButton("Show window")
        self.btn_show.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_show.clicked.connect(self.show_main_requested.emit)
        row.addWidget(self.btn_show)
        self.btn_stop = QPushButton("■ Stop")
        self.btn_stop.setObjectName("OverlayStop")
        self.btn_stop.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_stop.clicked.connect(self.stop_requested.emit)
        row.addWidget(self.btn_stop)
        lay.addLayout(row)
        self.setToolTip("WinAgent is controlling the computer. Drag to move, double-click to open the main window, Stop to interrupt.")
        self._apply_dot()

    # ...................................................... life cycle
    def start(self) -> None:
        """Show the overlay for a new task."""
        self._started = time.time()
        self.set_paused(False)
        self.set_status("Starting…")
        self.set_tool(None)
        self.set_step(0, 0)
        self.elapsed.setText("0:00")
        self.show()
        if not self._user_moved:
            self.move_to_corner()
        self.raise_()
        if self.want_capture_exclusion and not self.capture_excluded:
            self.capture_excluded = exclude_from_capture(self)
        elif not self.want_capture_exclusion and self.capture_excluded:
            exclude_from_capture(self, False)
            self.capture_excluded = False
        self._timer.start()

    def configure(self, corner: str, exclude_from_capture: bool) -> None:
        """Apply new settings (takes effect the next time the overlay is shown)."""
        if corner in CORNERS and corner != self.corner:
            self.corner = corner
            self._user_moved = False
        self.want_capture_exclusion = bool(exclude_from_capture)

    def finish(self) -> None:
        self._timer.stop()
        self.hide()

    def move_to_corner(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        self.adjustSize()
        area = screen.availableGeometry()   # excludes the taskbar
        margin = 16
        w, h = self.width(), self.height()
        if self.corner == "bottom-right":
            x, y = area.right() - w - margin, area.bottom() - h - margin
        elif self.corner == "bottom-left":
            x, y = area.left() + margin, area.bottom() - h - margin
        elif self.corner == "top-right":
            x, y = area.right() - w - margin, area.top() + margin
        else:
            x, y = area.left() + margin, area.top() + margin
        self.move(x, y)

    # ......................................................... updates
    @Slot(str)
    def set_status(self, text: str) -> None:
        self.status.setText(text)
        self._fit()

    def set_tool(self, text: Optional[str]) -> None:
        if text:
            self.tool.setText(text)
            self.tool.show()
        else:
            self.tool.clear()
            self.tool.hide()
        self._fit()

    def set_step(self, step: int, max_steps: int) -> None:
        self.step.setText(f"step {step}/{max_steps}" if step else "")

    def set_paused(self, paused: bool, text: str = "") -> None:
        self._paused = paused
        self.title.setText(text or ("WinAgent is waiting for you" if paused else "WinAgent is working"))
        self._apply_dot()

    def _apply_dot(self) -> None:
        p = PALETTE
        colour = p["warn"] if self._paused else (p["success"] if not self._pulse else p["accent"])
        self.dot.setStyleSheet(f"color: {colour}; font-size: 11pt; background: transparent;")

    def _fit(self) -> None:
        self.adjustSize()
        if self.isVisible() and not self._user_moved:
            self.move_to_corner()

    def _tick(self) -> None:
        if self._started:
            secs = int(time.time() - self._started)
            self.elapsed.setText(f"{secs // 60}:{secs % 60:02d}")
        self._pulse = not self._pulse
        self._apply_dot()

    def resizeEvent(self, event) -> None:  # noqa: D401 - Qt override
        super().resizeEvent(event)
        if self._dwm_rounded_corners():
            self.clearMask()
            return
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 12.0, 12.0)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def _dwm_rounded_corners(self) -> bool:
        """Windows 11: let the compositor round the corners (anti-aliased) instead of a pixel mask."""
        if sys.platform != "win32" or getattr(sys.getwindowsversion(), "build", 0) < 22000:
            return False
        try:
            DWMWA_WINDOW_CORNER_PREFERENCE = 33
            DWMWCP_ROUND = 2
            pref = ctypes.c_int(DWMWCP_ROUND)
            hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(int(self.winId())), DWMWA_WINDOW_CORNER_PREFERENCE,
                                                            ctypes.byref(pref), ctypes.sizeof(pref))
            return hr == 0
        except Exception:  # pragma: no cover
            return False

    # ........................................................ dragging
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            self._user_moved = True
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.show_main_requested.emit()
        super().mouseDoubleClickEvent(event)


# ----------------------------------------------------------- Qt guard
class QtScreenGuard(QObject, ScreenGuard):
    """ScreenGuard that hides / re-shows the GUI's top-level windows on the Qt thread.

    ``windows()`` returns the widgets that must never appear in a screenshot or
    under the agent's mouse: the main window and its dialogs, plus the overlay
    when the OS could not exclude it from capture.
    """

    _hide_req = Signal(object, object, bool, object)   # rect, point, for_capture, result holder
    _show_req = Signal()

    def __init__(self, windows: Callable[[], list[QWidget]], parent: Optional[QObject] = None):
        QObject.__init__(self, parent)
        ScreenGuard.__init__(self)
        self._windows = windows
        self.settle_time = 0.15        # seconds to wait after hiding before capturing (DWM needs a frame or two)
        self.enabled = True
        self.reactivate = True         # give the focus back to a window that was active when we hid it (idle mode)
        self._hidden_widgets: list[tuple[QWidget, bool]] = []
        self._app_active = False
        self._hide_req.connect(self._do_hide, Qt.ConnectionType.BlockingQueuedConnection)
        self._show_req.connect(self._do_show, Qt.ConnectionType.QueuedConnection)
        app = QGuiApplication.instance()
        if app is not None:
            self._app_active = app.applicationState() == Qt.ApplicationState.ApplicationActive
            app.applicationStateChanged.connect(self._on_app_state)

    # worker-thread API ---------------------------------------------------
    def hide_windows(self, rect: Optional[Rect], point: Optional[Point], for_capture: bool = False) -> float:
        if not self.enabled:
            return -1.0
        holder: dict[str, float] = {}
        if on_gui_thread():
            self._do_hide(rect, point, for_capture, holder)
        else:
            self._hide_req.emit(rect, point, for_capture, holder)   # blocks until the GUI thread has hidden the windows
        settle = holder.get("settle", -1.0)
        if settle >= 0:
            wait_for_compositor()
        return settle

    def show_windows(self) -> None:
        if on_gui_thread():
            self._do_show()
        else:
            self._show_req.emit()

    def ui_has_focus(self) -> bool:
        """True while one of our windows is the active (foreground) window – keystrokes would land in it."""
        return self.enabled and self._app_active

    # GUI-thread slots ----------------------------------------------------
    @Slot(Qt.ApplicationState)
    def _on_app_state(self, state) -> None:
        self._app_active = state == Qt.ApplicationState.ApplicationActive

    @Slot(object, object, bool, object)
    def _do_hide(self, rect: Optional[Rect], point: Optional[Point], for_capture: bool, holder: dict) -> None:
        hidden: list[tuple[QWidget, bool]] = []
        focus_case = rect is None and point is None and not for_capture   # "get out of the keyboard's way"
        for w in self._windows():
            try:
                if not w.isVisible() or w.isMinimized():
                    continue
                if for_capture and getattr(w, "capture_excluded", False):
                    continue   # the OS already keeps it out of screenshots
                if focus_case and w.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus:
                    continue   # cannot hold the keyboard focus anyway (the overlay)
                if not self._covers(w, rect, point):
                    continue
                was_active = w.isActiveWindow()
                w.hide()
                hidden.append((w, was_active))
            except RuntimeError:   # widget already deleted
                continue
        if hidden:
            self._hidden_widgets.extend(hidden)
            QApplication.processEvents()   # flush the hide to the window system before the capture
            holder["settle"] = self.settle_time
        else:
            holder["settle"] = -1.0

    @Slot()
    def _do_show(self) -> None:
        widgets, self._hidden_widgets = self._hidden_widgets, []
        for w, was_active in widgets:
            try:
                self._show_without_activating(w)
                if was_active and self.reactivate:
                    w.raise_()
                    w.activateWindow()
            except RuntimeError:
                continue

    @staticmethod
    def _show_without_activating(w: QWidget) -> None:
        """Bring a window back exactly where it was WITHOUT stealing focus from the app the agent is using."""
        handle = w.windowHandle()
        already = w.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        if handle is not None:
            # the platform plugin reads this QWindow property (the widget attribute is only copied on creation)
            handle.setProperty("_q_showWithoutActivating", True)
        w.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        w.show()
        if not already:
            w.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
            if handle is not None:
                handle.setProperty("_q_showWithoutActivating", False)

    @staticmethod
    def _covers(w: QWidget, rect: Optional[Rect], point: Optional[Point]) -> bool:
        if rect is None and point is None:
            return True
        try:
            frame = physical_frame_rect(w)
        except Exception:   # pragma: no cover
            return True
        if point is not None and frame[0] <= point[0] < frame[2] and frame[1] <= point[1] < frame[3]:
            return True
        if rect is not None and rects_overlap(frame, rect):
            return True
        return False
