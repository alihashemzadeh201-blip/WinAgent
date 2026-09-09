"""An image resize is not a desktop resize; neither may silently reinterpret pending clicks."""

import contextlib
import io
import json

import pytest
from PIL import Image

from tests.conftest import ScriptedLLM, native_tool_message
from winagent.agent import Agent, AgentEvents
from winagent.backends import ScreenGeometry
from winagent.config import Config
from winagent.protocol import ToolCall
from winagent.screenguard import ScreenGuard
from winagent.tools.executor import ToolExecutor


def clicks(backend):
    return [(e["x"], e["y"]) for e in backend.events if e["kind"] == "click"]


def tools(protocol, *calls):
    return native_tool_message(*calls) if protocol == "native" else json.dumps({"actions": [{"tool": n, "args": a} for n, a in calls]})


@pytest.mark.parametrize("protocol", ["native", "json"])
@pytest.mark.parametrize("first_tool", ["mouse_move", "click"])
def test_batch_uses_the_image_seen_before_auto_capture_changes_width(config, backend, monkeypatch, protocol, first_tool):
    config.tool_protocol = protocol
    first_action = getattr(backend, "mouse_move" if first_tool == "mouse_move" else "mouse_click")
    def change_width(*args, **kwargs):
        first_action(*args, **kwargs)
        config.screenshot_max_width = 768  # used to shift the next click 17 physical pixels down
    monkeypatch.setattr(backend, "mouse_move" if first_tool == "mouse_move" else "mouse_click", change_width)
    results, images = [], []
    llm = ScriptedLLM([
        tools(protocol, (first_tool, {"x": 100, "y": 100}), ("click", {"x": 300, "y": 200})),
        tools(protocol, ("click", {"x": 300, "y": 200})),
        tools(protocol, ("task_complete", {"summary": "done"})),
    ])
    agent = Agent(config, backend, llm, AgentEvents(on_tool_end=results.append, on_screenshot=images.append))
    assert agent.run("move then click twice").status == "completed"
    prefix = [(200, 200)] if first_tool == "click" else []
    assert clicks(backend) == prefix + [(600, 400), (625, 417)]  # old batch frame, new frame for the NEXT response
    assert (results[0].screenshot is not None) == (first_tool == "click")
    assert images[0].size == (800, 450) and images[1].size == (768, 432)
    mapping = results[1].data["coordinate_mapping"][0]
    assert mapping["frame_id"] == images[0].frame_id
    assert mapping["image_point"] == [300, 200] and mapping["physical_target"] == [600, 400]
    assert "pointer_after_action" not in results[1].data


@pytest.mark.parametrize("protocol", ["native", "json"])
@pytest.mark.parametrize("first_tool", ["mouse_move", "click"])
def test_unseen_full_capture_before_a_crop_does_not_become_model_input(config, backend, monkeypatch, protocol, first_tool):
    config.tool_protocol = protocol
    first_action = getattr(backend, "mouse_move" if first_tool == "mouse_move" else "mouse_click")
    def change_width(*args, **kwargs):
        first_action(*args, **kwargs)
        config.screenshot_max_width = 768
    monkeypatch.setattr(backend, "mouse_move" if first_tool == "mouse_move" else "mouse_click", change_width)
    images, results = [], []
    llm = ScriptedLLM([
        tools(protocol, (first_tool, {"x": 100, "y": 100}), ("screenshot", {"region": [100, 100, 300, 200]})),
        tools(protocol, ("get_screen_info", {}), ("click", {"x": 300, "y": 200})),
        tools(protocol, ("task_complete", {"summary": "done"})),
    ])
    agent = Agent(config, backend, llm, AgentEvents(on_screenshot=images.append, on_tool_end=results.append))
    assert agent.run("inspect the region").status == "completed"
    crop = images[2] if first_tool == "click" else images[1]
    assert crop.is_region and crop.raw_size == (400, 200)  # original batch coordinates too
    assert (results[0].screenshot is not None) == (first_tool == "click")
    assert clicks(backend) == ([(200, 200)] if first_tool == "click" else []) + [(600, 400)]
    assert images[0].frame_id in results[2].data["screenshot_frame"]
    assert results[3].data["coordinate_mapping"][0]["frame_id"] == images[0].frame_id


@pytest.mark.parametrize("call", [
    ToolCall("mouse_move", {"x": 100, "y": 100}), ToolCall("click", {"x": 100, "y": 100}),
    ToolCall("double_click", {"x": 100, "y": 100}), ToolCall("right_click", {"x": 100, "y": 100}),
    ToolCall("click", {}), ToolCall("scroll", {"amount": -1}),
    ToolCall("scroll", {"amount": -1, "x": 100, "y": 100}),
    ToolCall("drag", {"x1": 100, "y1": 100, "x2": 200, "y2": 200}),
    ToolCall("screenshot", {"region": [100, 100, 300, 200]}),
])
def test_display_resize_blocks_stale_input_before_any_mouse_event(config, backend, call):
    executor = ToolExecutor(backend, config)
    executor.take_screenshot()
    backend.height += 32
    result = executor.execute(call)
    assert not result.ok and result.needs_new_screenshot
    assert "geometry changed" in result.error
    assert not backend.events


def test_monitor_origin_change_is_not_just_a_scale_factor(config, backend, monkeypatch):
    executor = ToolExecutor(backend, config)
    frame = executor.take_screenshot(all_screens=True)
    monkeypatch.setattr(backend, "screen_geometry", lambda all_screens=False: ScreenGeometry(-1600, 0, 1600, 900))
    result = executor.execute(ToolCall("click", {"x": 100, "y": 100}), coordinate_frame=frame)
    assert result.needs_new_screenshot and not clicks(backend)


def test_display_change_while_hiding_the_gui_is_checked_at_input_boundary(config, backend):
    class ChangingGuard(ScreenGuard):
        @contextlib.contextmanager
        def shield(self, **kwargs):
            if not kwargs.get("for_capture"):
                backend.height += 32
            yield
    executor = ToolExecutor(backend, config, guard=ChangingGuard())
    executor.take_screenshot()
    result = executor.execute(ToolCall("click", {"x": 100, "y": 100}))
    assert result.needs_new_screenshot and not clicks(backend)


@pytest.mark.parametrize("changed_during_capture", [False, True])
def test_inconsistent_capture_does_not_publish_an_invalid_frame(config, backend, monkeypatch, changed_during_capture):
    executor = ToolExecutor(backend, config)
    previous = executor.take_screenshot()
    def capture(**kwargs):
        if changed_during_capture:
            backend.height = 932
        return Image.new("RGB", (1600, 932))
    monkeypatch.setattr(backend, "capture", capture)
    result = executor.execute(ToolCall("screenshot", {}))
    assert not result.ok and result.needs_new_screenshot
    assert "inconsistent screen capture" in result.error
    assert executor.last_screenshot is previous


@pytest.mark.parametrize("protocol", ["native", "json"])
def test_display_change_during_http_refreshes_and_replans_without_replaying(config, backend, protocol):
    config.tool_protocol = protocol
    def change_display(messages):
        backend.width = 1920
        return tools(protocol, ("click", {"x": 300, "y": 200}), ("type_text", {"text": "must be skipped"}),
                     ("task_complete", {"summary": "must not report a false success"}))
    results, states = [], []
    llm = ScriptedLLM([
        tools(protocol, ("type_text", {"text": "only once"})),
        change_display,
        tools(protocol, ("click", {"x": 200, "y": 150})),
        tools(protocol, ("task_complete", {"summary": "done"})),
    ])
    agent = Agent(config, backend, llm, AgentEvents(on_tool_end=results.append, on_status=states.append))
    assert agent.run("continue the task").status == "completed"
    assert backend.typed == ["only once"]
    assert clicks(backend) == [(480, 360)]
    assert results[1].needs_new_screenshot and results[1].screenshot.raw_size == (1920, 900)
    assert "This action was NOT executed" in json.dumps(llm.calls[2]["messages"])
    assert any("Display changed" in s for s in states)


def test_automatic_capture_preserves_all_monitor_scope(config, backend, monkeypatch):
    scopes = []
    def geometry(all_screens=False):
        return ScreenGeometry(-1600, -100, 3200, 1000) if all_screens else ScreenGeometry(0, 0, 1600, 900)
    def capture(all_screens=False, region=None):
        scopes.append(all_screens)
        geo = geometry(all_screens)
        return Image.new("RGB", (geo.width, geo.height))
    monkeypatch.setattr(backend, "screen_geometry", geometry)
    monkeypatch.setattr(backend, "capture", capture)
    executor = ToolExecutor(backend, config)
    original = executor.take_screenshot(all_screens=True)
    result = executor.execute(ToolCall("click", {"x": 400, "y": 200}))
    assert result.ok and result.screenshot.all_screens
    assert result.screenshot.raw_size == original.raw_size and result.screenshot.origin == original.origin
    assert scopes == [True, True]


def test_native_resolution_is_opt_in_and_preserves_encoded_dimensions(config, backend, monkeypatch):
    assert not Config().screenshot_native_resolution
    config.screenshot_native_resolution = True
    config.screenshot_grid = False
    config.screenshot_show_cursor = False
    config.screenshot_format = "png"
    raw = Image.new("RGB", (backend.width, backend.height), (41, 73, 91))
    monkeypatch.setattr(backend, "capture", lambda **kwargs: raw.copy())
    executor = ToolExecutor(backend, config)
    native = executor.take_screenshot()
    assert native.size == native.raw_size == (1600, 900)
    assert native.to_physical(811, 679) == (811, 679)
    encoded = Image.open(io.BytesIO(native.encode()))
    assert encoded.size == raw.size and encoded.tobytes() == raw.tobytes()
    assert Config.from_dict(config.to_dict()).screenshot_native_resolution
    config.screenshot_native_resolution = False
    assert executor.take_screenshot().size == (800, 450)


def test_explicit_unframed_input_does_not_pick_up_an_unseen_capture(config, backend):
    executor = ToolExecutor(backend, config)
    executor.take_screenshot()
    result = executor.execute(ToolCall("mouse_move", {"x": 300, "y": 200}), coordinate_frame=None)
    assert result.ok and backend.mouse == (300, 200)


def test_agent_reset_discards_the_model_coordinate_frame(config, backend):
    agent = Agent(config, backend, ScriptedLLM(['{"message":"hello"}']))
    agent.run("hello")
    assert agent._model_frame is not None
    agent.reset()
    assert agent._model_frame is None and agent.executor.last_screenshot is None


def test_resolution_change_between_batch_actions_does_not_replay_successful_prefix(config, backend, monkeypatch):
    move = backend.mouse_move
    def change_resolution(*args, **kwargs):
        move(*args, **kwargs)
        backend.height = 932
    monkeypatch.setattr(backend, "mouse_move", change_resolution)
    results = []
    llm = ScriptedLLM([
        native_tool_message(("mouse_move", {"x": 100, "y": 100}), ("click", {"x": 300, "y": 200}),
                            ("type_text", {"text": "should be skipped"})),
        native_tool_message(("click", {"x": 300, "y": 200})),
        native_tool_message(("task_complete", {"summary": "done"})),
    ])
    agent = Agent(config, backend, llm, AgentEvents(on_tool_end=results.append))
    assert agent.run("move and click").status == "completed"
    assert results[0].ok and results[1].needs_new_screenshot
    assert not backend.typed and clicks(backend) == [(600, 400)]
    assert sum(e["kind"] == "move" for e in backend.events) == 1


def test_failed_post_action_capture_does_not_mark_an_executed_click_as_unexecuted(config, backend, monkeypatch):
    capture = backend.capture
    calls = []
    def unstable_capture(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:  # the click has already been executed
            return Image.new("RGB", (1599, 900))
        return capture(**kwargs)
    monkeypatch.setattr(backend, "capture", unstable_capture)
    results = []
    llm = ScriptedLLM([
        native_tool_message(("click", {"x": 300, "y": 200}), ("type_text", {"text": "should be skipped"})),
        native_tool_message(("task_complete", {"summary": "done"})),
    ])
    agent = Agent(config, backend, llm, AgentEvents(on_tool_end=results.append))
    assert agent.run("click once").status == "completed"
    assert results[0].ok and results[0].needs_new_screenshot
    assert results[0].screenshot is not None
    assert clicks(backend) == [(600, 400)] and not backend.typed


def test_emergency_stop_at_input_boundary_closes_pending_native_tool_messages(config, backend):
    class StopGuard(ScreenGuard):
        @contextlib.contextmanager
        def shield(self, **kwargs):
            if not kwargs.get("for_capture"):
                backend.mouse = (0, 0)
            yield
    llm = ScriptedLLM([native_tool_message(("click", {"x": 100, "y": 100}), ("type_text", {"text": "skip"}))])
    agent = Agent(config, backend, llm, guard=StopGuard())
    assert agent.run("work").status == "stopped"
    assert not clicks(backend) and not backend.typed
    replies = [m for m in agent.history if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in replies] == ["call_0", "call_1"]
    assert all(not json.loads(m["content"])["ok"] for m in replies)


def test_stop_at_resolution_recovery_prevents_capture_and_another_model_request(config, backend):
    def change_display(messages):
        backend.height = 932
        return native_tool_message(("click", {"x": 100, "y": 100}))
    llm = ScriptedLLM([change_display])
    agent = Agent(config, backend, llm)
    agent.events.on_status = lambda text: agent.stop() if "Display changed" in text else None
    assert agent.run("work").status == "stopped"
    assert agent.executor.screenshot_count == 1
    assert len(llm.calls) == 1 and not clicks(backend)
