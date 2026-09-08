"""Headless (offscreen) checks of the status overlay and the Qt screen guard.

Skipped automatically when PySide6 or an offscreen Qt platform is not available.
"""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
try:  # pragma: no cover - depends on the machine
    from PySide6.QtWidgets import QApplication, QDialog

    _app = QApplication.instance() or QApplication([])
except Exception as exc:  # pragma: no cover
    pytest.skip(f"Qt cannot start here: {exc}", allow_module_level=True)

from tests.mock_server import start_server  # noqa: E402
from winagent.config import Config  # noqa: E402
from winagent.gui.main_window import MainWindow  # noqa: E402
from winagent.gui.overlay import QtScreenGuard, StatusOverlay, physical_frame_rect  # noqa: E402
from winagent.gui.settings_dialog import GUI_MODE_LABELS, SettingsDialog  # noqa: E402


def pump(seconds: float = 0.05) -> None:
    end = time.time() + seconds
    while time.time() < end:
        _app.processEvents()
        time.sleep(0.005)


def run_task(win: MainWindow, text: str, timeout: float = 30.0):
    """Send a message and pump the event loop until the worker finished; returns overlay samples."""
    samples = []
    win.input.submitted.emit(text)
    deadline = time.time() + timeout
    started = False
    while time.time() < deadline:
        _app.processEvents()
        running = win.is_running()
        if running:
            started = True
            samples.append((win.overlay.isVisible(), win.overlay.status.text(), win.overlay.tool.text(), win.isMinimized()))
        elif started:
            break
        time.sleep(0.003)
    pump(0.2)
    assert started and not win.is_running(), "task did not run/finish"
    return samples


@pytest.fixture
def server():
    srv, url = start_server()
    yield url
    srv.shutdown()


def make_window(url: str, **overrides) -> MainWindow:
    cfg = Config(api_base_url=url, api_key="t", model="mock", action_delay=0.0, screenshot_max_width=640, **overrides)
    win = MainWindow(cfg, backend_override="fake")
    win.resize(1000, 700)
    win.show()
    pump(0.05)
    return win


def test_overlay_widget_updates_and_never_wants_focus():
    ov = StatusOverlay(corner="top-left")
    ov.start()
    pump()
    assert ov.isVisible()
    ov.set_status("Running click…")
    ov.set_tool("click {\"x\": 1}")
    ov.set_step(2, 40)
    assert ov.status.text() == "Running click…" and ov.tool.isVisible() and ov.step.text() == "step 2/40"
    ov.set_paused(True)
    assert "waiting" in ov.title.text()
    from PySide6.QtCore import Qt

    assert ov.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert ov.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert ov.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert ov.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert not ov.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground), "layered windows cannot be excluded from capture"
    left, top, right, bottom = physical_frame_rect(ov)
    assert right > left and bottom > top
    ov.finish()
    pump()
    assert not ov.isVisible()


def test_qt_guard_hides_only_windows_covering_the_area():
    a = QDialog()
    a.setGeometry(0, 0, 200, 100)
    a.show()
    b = QDialog()
    b.setGeometry(500, 500, 200, 100)
    b.show()
    pump()
    guard = QtScreenGuard(lambda: [a, b])
    with guard.shield(point=(50, 50)):
        pump()
        assert not a.isVisible() and b.isVisible()
    pump()
    assert a.isVisible() and b.isVisible()
    with guard.shield(rect=(0, 0, 2000, 2000), for_capture=True):
        pump()
        assert not a.isVisible() and not b.isVisible()
    pump()
    assert a.isVisible() and b.isVisible()
    # a window the OS already excludes from capture is left alone for captures, but not for clicks
    b.capture_excluded = True
    with guard.shield(rect=(0, 0, 2000, 2000), for_capture=True):
        pump()
        assert not a.isVisible() and b.isVisible()
    with guard.shield(point=(550, 550)):
        pump()
        assert not b.isVisible()
    pump()
    guard.enabled = False
    with guard.shield():
        assert a.isVisible() and b.isVisible()
    a.close()
    b.close()


def test_overlay_mode_shows_status_during_task_and_hides_after(server):
    win = make_window(server, gui_mode_while_running="overlay")
    assert win.gui_mode == "overlay"
    samples = run_task(win, "open notepad and write hello")
    assert any(v for v, *_ in samples), "overlay never became visible"
    statuses = {s for _, s, _, _ in samples}
    assert any(s.startswith("Running ") for s in statuses), statuses
    tools = {t for _, _, t, _ in samples if t}
    assert any(t.startswith("open_app") for t in tools), tools
    assert not win.overlay.isVisible()
    assert win.guard.hide_count > 0, "the main window was never hidden for a screenshot"
    assert not win.guard.hidden and win.isVisible()
    assert "completed" in win.status_label.text().lower()
    win.close()


def test_visible_mode_keeps_window_and_hides_it_only_for_captures(server):
    win = make_window(server, gui_mode_while_running="visible")
    # on the simulated backend "visible" degrades to "none" (nothing to protect); force the real behaviour
    win.force_gui_mode = True
    assert win.gui_mode == "visible"
    samples = run_task(win, "take a screenshot and describe the screen")
    assert not any(v for v, *_ in samples), "no overlay in 'visible' mode"
    assert not any(m for *_, m in samples), "the main window must not be minimised"
    assert win.guard.hide_count >= 1
    assert win.isVisible() and not win.guard.hidden
    win.close()


def test_none_mode_leaves_everything_alone(server):
    win = make_window(server, gui_mode_while_running="none")
    samples = run_task(win, "take a screenshot and describe the screen")
    assert not any(v for v, *_ in samples)
    assert win.guard.hide_count == 0
    win.close()


def test_question_restores_window_and_pauses_overlay(server):
    win = make_window(server, gui_mode_while_running="overlay")
    win.input.submitted.emit("please ask me something")
    deadline = time.time() + 30
    while time.time() < deadline and not win._pending_question:
        _app.processEvents()
        time.sleep(0.003)
    assert win._pending_question
    pump(0.05)
    assert not win.isMinimized()
    assert "waiting" in win.overlay.title.text()
    win.input.submitted.emit("Desktop")
    deadline = time.time() + 30
    while time.time() < deadline and win.is_running():
        _app.processEvents()
        time.sleep(0.003)
    pump(0.2)
    assert not win.overlay.isVisible()
    win.close()


def test_settings_dialog_round_trips_gui_mode(server):
    cfg = Config(api_base_url=server, api_key="t", model="mock", gui_mode_while_running="visible")
    dlg = SettingsDialog(cfg)
    assert dlg.gui_mode.currentData() == "visible"
    assert dlg.gui_mode.count() == len(GUI_MODE_LABELS)
    dlg.gui_mode.setCurrentIndex(dlg.gui_mode.findData("minimize"))
    out = dlg._collect()
    assert out is not None and out.gui_mode_while_running == "minimize"
    assert out.validate() == []
