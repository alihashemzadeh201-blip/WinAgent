"""Headless (offscreen) checks of the status overlay and the Qt screen guard.

Skipped automatically when PySide6 or an offscreen Qt platform is not available.
"""

import os
import time
from unittest.mock import Mock

import pytest
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
try:  # pragma: no cover - depends on the machine
    from PySide6.QtWidgets import QApplication, QDialog

    _app = QApplication.instance() or QApplication([])
except Exception as exc:  # pragma: no cover
    pytest.skip(f"Qt cannot start here: {exc}", allow_module_level=True)

from tests.mock_server import start_server  # noqa: E402
from winagent.backends import BackendError, FakeBackend  # noqa: E402
from winagent.config import Config, load_config  # noqa: E402
from winagent.gui import main_window as main_window_module  # noqa: E402
from winagent.gui.main_window import MainWindow  # noqa: E402
from winagent.gui.overlay import QtScreenGuard, StatusOverlay, physical_frame_rect  # noqa: E402
from winagent.gui.settings_dialog import GUI_MODE_LABELS, SettingsDialog  # noqa: E402


@pytest.fixture(autouse=True)
def dispose_test_windows():
    """Dispose native widgets on the Qt thread, not during Python's unordered interpreter shutdown."""
    yield
    from PySide6.QtCore import QCoreApplication, QEvent

    for widget in _app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    _app.processEvents()


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


# ---------------------------------------------------------------- capture source / backend recovery
@pytest.fixture
def demo_window(tmp_path):
    win = make_window("http://localhost/v1")
    win.config_path = tmp_path / "config.json"
    yield win
    win.close()


def accept_settings(monkeypatch, *, expected_backend, backend=None, model=None, hotkey=None):
    """Exercise the real SettingsDialog collection/save path without a modal event loop."""
    def accept(dlg):
        assert dlg.backend.currentText() == expected_backend
        if backend is not None:
            dlg.backend.setCurrentText(backend)
        if model is not None:
            dlg.model.setCurrentText(model)
        if hotkey is not None:
            dlg.stop_hotkey.setText(hotkey)
        dlg._accept()
        assert dlg.result() == QDialog.DialogCode.Accepted
        return dlg.result()

    monkeypatch.setattr(SettingsDialog, "exec", accept)


def test_demo_source_stays_visible_after_new_chat_and_status_updates(demo_window):
    win = demo_window
    win.manual_screenshot()
    assert win._last_shot is not None and win.btn_open_shot.isEnabled()
    win.new_chat()
    win._on_status("Ready")
    assert win.backend_label.isVisible()
    assert "DEMO" in win.backend_label.text() and "not your screen" in win.backend_label.text()
    assert "DEMO" in win.windowTitle()


@pytest.mark.parametrize("selected", ["auto", "windows"])
def test_settings_can_leave_launch_demo_and_clear_simulated_context(demo_window, monkeypatch, selected):
    win = demo_window
    win.manual_screenshot()
    win.agent.history.append({"role": "user", "content": "old simulated desktop"})
    old = win.backend
    old.close = Mock()
    # Stand-in for a Windows desktop result, with odd RGB row width to exercise the Qt image conversion too.
    real = FakeBackend(width=401, height=201)
    real.name = "windows"
    raw = Image.new("RGB", (401, 201), (17, 153, 61))
    raw.putpixel((1, 2), (200, 40, 50))
    real.capture = Mock(return_value=raw)
    factory = Mock(return_value=real)
    monkeypatch.setattr(main_window_module, "create_backend", factory)
    accept_settings(monkeypatch, expected_backend="fake", backend=selected)

    win.open_settings()
    assert win.backend_override is None
    assert win.backend is real and win.agent.backend is real
    assert win.config.backend == selected and load_config(win.config_path, env=False).backend == selected
    old.close.assert_called_once()
    factory.assert_called_once_with(selected, stop_hotkey=win.config.stop_hotkey, on_emergency_stop=win._emergency_stop)
    assert win.agent.history == [] and win.agent.executor.last_screenshot is None
    assert win._last_shot is None and win.shot_view.pixmap_full() is None
    assert not win.btn_open_shot.isEnabled() and win.shot_label.text() == ""
    assert "real desktop" in win.backend_label.text() and "DEMO" not in win.windowTitle()

    win.manual_screenshot()
    real.capture.assert_called_once()
    assert win.shot_view.pixmap_full().toImage().pixelColor(1, 2).getRgb() == (200, 40, 50, 255)
    assert win.btn_open_shot.isEnabled()


@pytest.mark.parametrize("change_hotkey", [False, True])
def test_saving_other_settings_does_not_persist_temporary_demo(demo_window, monkeypatch, change_hotkey):
    win = demo_window
    old = win.backend
    factory = Mock(return_value=FakeBackend())
    monkeypatch.setattr(main_window_module, "create_backend", factory)
    hotkey = "ctrl+shift+esc" if change_hotkey else None
    accept_settings(monkeypatch, expected_backend="fake", model="new-model", hotkey=hotkey)

    win.open_settings()
    assert win.backend_override == "fake"
    assert win.config.backend == "auto"
    assert load_config(win.config_path, env=False).backend == "auto"
    assert win.config.model == "new-model"
    if change_hotkey:
        factory.assert_called_once_with("fake", stop_hotkey=hotkey, on_emergency_stop=win._emergency_stop)
    else:
        factory.assert_not_called()
        assert win.backend is old


@pytest.mark.parametrize("kind", ["auto", "windows"])
def test_backend_init_failure_disables_actions_without_fake_fallback(monkeypatch, kind):
    factory = Mock(side_effect=BackendError("Win32 initialisation failed"))
    monkeypatch.setattr(main_window_module, "create_backend", factory)
    critical, warning = Mock(), Mock()
    monkeypatch.setattr(main_window_module.QMessageBox, "critical", critical)
    monkeypatch.setattr(main_window_module.QMessageBox, "warning", warning)
    win = MainWindow(Config(backend=kind, api_base_url="http://localhost/v1"))
    try:
        assert factory.call_count == 1 and factory.call_args.args[0] == kind
        assert win.backend is None and win.agent is None
        assert not win.btn_screenshot.isEnabled() and not win.btn_send.isEnabled()
        assert not win.btn_open_shot.isEnabled() and win.btn_settings.isEnabled()
        assert win.shot_view.pixmap_full() is None and not win.guard.enabled
        assert "unavailable" in win.backend_label.text()
        assert "Win32 initialisation failed" in critical.call_args.args[2]
        win.input.submitted.emit("take a screenshot")  # Enter must be blocked, not just the Send button.
        assert win.worker is None and warning.call_count == 1
    finally:
        win.close()


def test_failed_settings_switch_can_retry_same_backend(demo_window, monkeypatch):
    win = demo_window
    win.manual_screenshot()
    real = FakeBackend()
    real.name = "windows"
    factory = Mock(side_effect=[BackendError("Windows unavailable"), real])
    monkeypatch.setattr(main_window_module, "create_backend", factory)
    monkeypatch.setattr(main_window_module.QMessageBox, "critical", Mock())
    accept_settings(monkeypatch, expected_backend="fake", backend="windows")
    win.open_settings()
    assert win.backend is None and win.agent is None
    assert win.shot_view.pixmap_full() is None and not win.btn_open_shot.isEnabled()
    assert not win.btn_send.isEnabled() and not win.btn_screenshot.isEnabled()
    assert win.windows_tree.topLevelItemCount() == 0

    accept_settings(monkeypatch, expected_backend="windows")
    win.open_settings()
    assert [call.args[0] for call in factory.call_args_list] == ["windows", "windows"]
    assert win.backend is real and win.agent is not None
    assert win.btn_send.isEnabled() and win.btn_screenshot.isEnabled()
    assert "real desktop" in win.backend_label.text()


def test_failed_manual_capture_clears_stale_preview(demo_window, monkeypatch):
    win = demo_window
    win.manual_screenshot()
    assert win.shot_view.pixmap_full() is not None
    monkeypatch.setattr(win.backend, "capture", Mock(side_effect=BackendError("screen grab failed")))
    warning = Mock()
    monkeypatch.setattr(main_window_module.QMessageBox, "warning", warning)
    win.manual_screenshot()
    assert win._last_shot is None and win.shot_view.pixmap_full() is None
    assert not win.btn_open_shot.isEnabled()
    assert "screen grab failed" in warning.call_args.args[2]
    assert "screen grab failed" in win.chat.transcript()


def test_manual_screenshot_button_disabled_while_worker_uses_frame(demo_window):
    demo_window._set_running(True)
    assert not demo_window.btn_screenshot.isEnabled()
    demo_window._set_running(False)
    assert demo_window.btn_screenshot.isEnabled()


def test_response_retry_setting_round_trips():
    dlg = SettingsDialog(Config(max_response_retries=0))
    assert dlg.response_retries.value() == 0
    dlg.response_retries.setValue(5)
    cfg = dlg._collect()
    assert cfg is not None and cfg.max_response_retries == 5
    assert not cfg.validate()
    dlg.close()


def test_gui_sets_process_dpi_awareness_before_creating_qt_app(monkeypatch):
    from winagent.backends import windows

    order = []
    app = Mock()
    app.exec.return_value = 0
    factory = Mock(side_effect=lambda *args: order.append("qt") or app)
    factory.instance.return_value = None
    monkeypatch.setattr(main_window_module, "QApplication", factory)
    monkeypatch.setattr(main_window_module, "MainWindow", Mock())
    monkeypatch.setattr(windows, "make_dpi_aware", lambda: order.append("dpi"))
    assert main_window_module.run_gui(Config(), backend_override="fake") == 0
    assert order == ["dpi", "qt"]


def test_gui_keeps_working_after_bad_model_response(server, monkeypatch):
    from tests import mock_server

    original_plan = mock_server.plan
    requests = []
    broken = "document, y from 0 (top) to 719 (bottom).]"
    def flaky_plan(messages):
        requests.append(messages)
        if len(requests) == 1:
            return broken, []
        return original_plan(messages)
    monkeypatch.setattr(mock_server, "plan", flaky_plan)
    win = make_window(server)
    try:
        run_task(win, "open notepad and write hello")
        assert "completed" in win.status_label.text().lower()
        assert broken not in win.chat.transcript()
        assert not win.btn_stop.isEnabled()
        assert sum(e["kind"] == "open_app" for e in win.backend.events) == 1
        assert sum(e["kind"] == "type" for e in win.backend.events) == 1
        assert "NO actions" in requests[1][-1]["content"]
    finally:
        win.close()


def test_native_screenshot_setting_round_trips_and_disables_resize_width():
    dlg = SettingsDialog(Config(screenshot_native_resolution=True, screenshot_max_width=1024))
    assert dlg.shot_native.isChecked() and not dlg.shot_width.isEnabled()
    cfg = dlg._collect()
    assert cfg.screenshot_native_resolution and cfg.screenshot_max_width == 1024
    dlg.shot_native.setChecked(False)
    assert dlg.shot_width.isEnabled() and not dlg._collect().screenshot_native_resolution
    dlg.close()


def test_screenshot_ui_displays_native_mode_and_frame_details(demo_window):
    demo_window.config.screenshot_native_resolution = True
    demo_window.agent.config.screenshot_native_resolution = True
    demo_window.agent.executor.config.screenshot_native_resolution = True
    demo_window.manual_screenshot()
    shot = demo_window._last_shot
    assert shot.size == shot.raw_size
    assert "1:1" in demo_window.shot_label.text()
    assert shot.frame_id in demo_window.shot_label.toolTip()


def test_coordinate_space_setting_round_trips_without_model_name_guessing():
    cfg = Config(model="ag/gemini-pro-agent", coordinate_space="image_pixels")
    dlg = SettingsDialog(cfg)
    assert dlg.shot_space.currentData() == "image_pixels"
    dlg.shot_space.setCurrentIndex(dlg.shot_space.findData("normalized_1000"))
    assert dlg.shot_grid_spacing.suffix() == " /1000"
    saved = dlg._collect()
    assert saved.coordinate_space == "normalized_1000" and saved.model == cfg.model
    assert not saved.screenshot_native_resolution  # unit changes do not silently raise image resolution/cost
    restored = SettingsDialog(saved)
    assert restored.shot_space.currentData() == "normalized_1000"
    restored.shot_space.setCurrentIndex(restored.shot_space.findData("image_pixels"))
    assert restored.shot_grid_spacing.suffix() == " px"
    restored.close()
    dlg.close()


def test_screenshot_preview_identifies_normalized_units(demo_window):
    demo_window.config.coordinate_space = "normalized_1000"
    demo_window.agent.executor.config.coordinate_space = "normalized_1000"
    demo_window.manual_screenshot()
    assert "coords 0–1000" in demo_window.shot_label.text()
    assert "normalized_1000" in demo_window.shot_label.toolTip()


def test_gui_does_not_finish_on_unstructured_fragment_after_an_action(server, monkeypatch):
    from tests import mock_server

    original_plan = mock_server.plan
    requests = []
    def flaky_plan(messages):
        requests.append(messages)
        if len(requests) == 2:  # application launch already succeeded; don't repeat it
            return "b sideways.", []
        return original_plan(messages)
    monkeypatch.setattr(mock_server, "plan", flaky_plan)
    win = make_window(server)
    try:
        run_task(win, "open notepad and write hello")
        assert "completed" in win.status_label.text().lower()
        assert "b sideways." not in win.chat.transcript()
        assert sum(e["kind"] == "open_app" for e in win.backend.events) == 1
        assert sum(e["kind"] == "type" for e in win.backend.events) == 1
        assert "task_complete" in requests[2][-1]["content"]
    finally:
        win.close()


@pytest.mark.parametrize("protocol", ["native", "json"])
@pytest.mark.parametrize("rebuild", [False, True])
def test_same_window_can_run_followup_tasks_with_or_without_agent_rebuild(server, protocol, rebuild):
    win = make_window(server, tool_protocol=protocol)
    try:
        run_task(win, "open notepad and write hello")
        assert "Task completed" in win.status_label.text()
        typed = list(win.backend.typed)
        old_agent = win.agent
        old_frame = win.agent._model_frame
        if rebuild:
            win._rebuild_agent()  # same path as saving settings, with the same desktop backend
            assert win.agent is not old_agent
            assert not any(m.get("tool_calls") or m.get("role") == "tool" for m in win.agent.history)
        run_task(win, "take a screenshot and describe it")
        assert "Task completed" in win.status_label.text()
        assert win.backend.typed == typed
        assert win.agent._model_frame.frame_id != old_frame.frame_id
        assert win.agent.protocol == protocol
        transcript = win.chat.transcript()
        assert "open notepad and write hello" in transcript and "take a screenshot and describe it" in transcript
        assert win.btn_send.isEnabled() and not win.btn_stop.isEnabled()
        assert not win._pending_question
    finally:
        win.close()
