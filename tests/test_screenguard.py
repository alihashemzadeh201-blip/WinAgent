"""ScreenGuard: the agent's own GUI must stay out of screenshots and from under the mouse."""

import json

from winagent.config import GUI_MODES, Config
from winagent.tools import ToolExecutor  # noqa: I001 - must precede winagent.protocol (import cycle)
from winagent.protocol import ToolCall
from winagent.screenguard import RecordingGuard, ScreenGuard, rect_from_points, rects_overlap


# ------------------------------------------------------------------ helpers
def test_rect_helpers():
    assert rect_from_points((10, 20), (5, 40), margin=2) == (3, 18, 12, 42)
    assert rects_overlap((0, 0, 10, 10), (5, 5, 20, 20))
    assert not rects_overlap((0, 0, 10, 10), (10, 0, 20, 10))   # touching edges do not overlap


def test_noop_guard_is_transparent():
    guard = ScreenGuard()
    with guard.scope():
        with guard.shield(point=(1, 2)) as hidden:
            assert hidden is False
        assert guard.hidden is False
    assert guard.hide_count == 0


def test_nested_scopes_hide_once_and_restore_once():
    guard = RecordingGuard()
    with guard.scope():
        with guard.shield(point=(1, 2)):
            assert not guard.visible
        assert not guard.visible, "windows stay hidden until the outermost scope ends"
        with guard.shield(rect=(0, 0, 10, 10), for_capture=True):
            assert not guard.visible
        assert guard.shown == 0
    assert guard.visible and guard.shown == 1
    assert guard.hide_count == 1
    assert [r[2] for r in guard.requests] == [False, True]   # the second request was flagged as a capture


def test_shield_without_scope_restores_immediately():
    guard = RecordingGuard()
    with guard.shield():
        assert not guard.visible
    assert guard.visible and guard.shown == 1


def test_failing_ui_never_breaks_the_tool():
    class Broken(ScreenGuard):
        def hide_windows(self, rect, point, for_capture=False):
            raise RuntimeError("boom")

        def show_windows(self):
            raise RuntimeError("boom")

    guard = Broken()
    with guard.shield(point=(0, 0)) as hidden:
        assert hidden is False


# ---------------------------------------------------------------- executor
def test_screenshot_hides_ui_for_the_capture_area(config, backend):
    guard = RecordingGuard()
    ex = ToolExecutor(backend, config, guard=guard)
    shot = ex.take_screenshot()
    assert shot is not None
    rect, point, for_capture = guard.requests[-1]
    assert for_capture and point is None
    assert rect == (0, 0, backend.width, backend.height)
    assert guard.visible and guard.shown == 1


def test_click_hides_ui_only_around_the_target_and_once_per_call(config, backend):
    guard = RecordingGuard()
    ex = ToolExecutor(backend, config, guard=guard)
    ex.take_screenshot()
    guard.requests.clear()
    guard.shown = 0
    res = ex.execute(ToolCall(name="click", arguments={"x": 100, "y": 50}))
    assert res.ok, res.error
    kinds = [(r[1] is not None, r[2]) for r in guard.requests]
    assert kinds[0] == (True, False), "first request protects the click point"
    assert (False, True) in kinds, "the automatic screenshot after the click is a capture request"
    # both happened inside ONE execute() scope: the UI was restored exactly once at the end
    assert guard.shown == 1
    px, py = guard.requests[0][1]
    assert px > 100 and py > 50, "the point is converted to physical pixels before shielding"


def test_drag_shields_the_whole_path(config, backend):
    guard = RecordingGuard()
    ex = ToolExecutor(backend, config, guard=guard)
    ex.take_screenshot()
    guard.requests.clear()
    res = ex.execute(ToolCall(name="drag", arguments={"x1": 10, "y1": 10, "x2": 200, "y2": 100, "duration": 0.1}))
    assert res.ok, res.error
    rect = guard.requests[0][0]
    assert rect is not None and rect[0] < rect[2] and rect[1] < rect[3]


def test_keyboard_hides_ui_only_when_it_has_focus(config, backend):
    class FocusGuard(RecordingGuard):
        focus = False

        def ui_has_focus(self):
            return self.focus

    guard = FocusGuard()
    ex = ToolExecutor(backend, config, guard=guard)
    res = ex.execute(ToolCall(name="press_keys", arguments={"keys": "ctrl+s"}))
    assert res.ok
    assert all(r[2] for r in guard.requests), "without focus only the auto-screenshot capture request is made"
    guard.requests.clear()
    guard.focus = True
    res = ex.execute(ToolCall(name="type_text", arguments={"text": "hi"}))
    assert res.ok
    assert guard.requests[0] == (None, None, False), "with focus everything is hidden before typing"
    assert "hi" in "".join(backend.typed)


def test_non_ui_tools_do_not_touch_the_guard(config, backend):
    guard = RecordingGuard()
    ex = ToolExecutor(backend, config, guard=guard)
    res = ex.execute(ToolCall(name="list_windows", arguments={}))
    assert res.ok
    assert guard.requests == []
    assert guard.shown == 0


# ------------------------------------------------------------------ config
def test_gui_mode_defaults_and_validation():
    cfg = Config(api_base_url="http://x/v1", model="m")
    assert cfg.gui_mode_while_running == "overlay"
    assert cfg.validate() == []
    cfg.gui_mode_while_running = "sideways"
    assert any("gui_mode_while_running" in p for p in cfg.validate())
    assert set(GUI_MODES) == {"overlay", "visible", "minimize", "none"}


def test_legacy_minimize_flag_is_migrated():
    old_true = Config.from_dict({"api_base_url": "http://x/v1", "model": "m", "minimize_gui_while_running": True})
    old_false = Config.from_dict({"api_base_url": "http://x/v1", "model": "m", "minimize_gui_while_running": False})
    assert old_true.gui_mode_while_running == "overlay"
    assert old_false.gui_mode_while_running == "none"
    # the new key wins when both are present
    both = Config.from_dict({"minimize_gui_while_running": True, "gui_mode_while_running": "visible"})
    assert both.gui_mode_while_running == "visible"
    assert Config.from_dict({"gui_mode_while_running": " Visible "}).gui_mode_while_running == "visible"


def test_gui_mode_round_trips_through_json(tmp_path):
    cfg = Config(api_base_url="http://x/v1", model="m", gui_mode_while_running="visible")
    data = json.loads(json.dumps(cfg.to_dict()))
    assert "minimize_gui_while_running" not in data
    assert Config.from_dict(data).gui_mode_while_running == "visible"
