"""Ordinary pointer positioning does not require another observation or calibration step."""

import json
from unittest.mock import Mock

import pytest

from tests.conftest import ScriptedLLM, native_tool_message
from winagent.agent import Agent, AgentEvents
from winagent.prompts import build_system_prompt
from winagent.protocol import ToolCall
from winagent.tools import ToolExecutor
from winagent.tools import executor as executor_module
from winagent.tools.definitions import openai_tool_schemas, tools_markdown


POINTER_ACTIONS = [
    ("mouse_move", {"x": 400, "y": 225}),
    ("click", {"x": 400, "y": 225}),
    ("double_click", {"x": 400, "y": 225}),
    ("right_click", {"x": 400, "y": 225}),
    ("drag", {"x1": 100, "y1": 100, "x2": 400, "y2": 225}),
    ("scroll", {"x": 400, "y": 225, "amount": -1}),
]


def turn(protocol, *calls):
    if protocol == "native":
        return native_tool_message(*calls)
    return json.dumps({"actions": [{"tool": name, "args": args} for name, args in calls]})


@pytest.mark.parametrize("name,args", POINTER_ACTIONS)
def test_executor_no_longer_reads_or_reports_post_action_cursor(config, backend, monkeypatch, caplog, name, args):
    config.auto_screenshot_after_action = False
    config.mouse_failsafe = False  # isolate the removed readback from the retained emergency-stop check
    executor = ToolExecutor(backend, config)
    executor.take_screenshot()
    position = Mock(wraps=backend.mouse_position)
    monkeypatch.setattr(backend, "mouse_position", position)
    with caplog.at_level("INFO", logger="winagent.tools.executor"):
        result = executor.execute(ToolCall(name, args))
    assert result.ok, result.error
    position.assert_not_called()
    assert backend.mouse == (800, 450)
    assert "pointer_after_action" not in result.data
    assert "Pointer after action" not in caplog.text
    assert result.data["coordinate_mapping"][-1]["physical_target"] == [800, 450]


@pytest.mark.parametrize("space,xy", [("image_pixels", (400, 225)), ("normalized_1000", (500, 500))])
@pytest.mark.parametrize("name", ["mouse_move", "move_mouse", "hover"])
def test_move_has_no_auto_capture_or_capture_delay_and_keeps_its_reference_frame(config, backend, monkeypatch, space, xy, name):
    config.coordinate_space = space
    config.auto_screenshot_after_action = True
    config.action_delay = 0.6
    config.mouse_failsafe = False
    executor = ToolExecutor(backend, config)
    frame = executor.take_screenshot()
    capture = Mock(wraps=backend.capture)
    position = Mock(wraps=backend.mouse_position)
    delay = Mock()
    monkeypatch.setattr(backend, "capture", capture)
    monkeypatch.setattr(backend, "mouse_position", position)
    monkeypatch.setattr(executor_module.time, "sleep", delay)
    result = executor.execute(ToolCall(name, {"x": xy[0], "y": xy[1]}))
    assert result.ok and result.screenshot is None
    assert backend.mouse == (800, 450)
    assert executor.screenshot_count == 1 and executor.last_screenshot is frame
    capture.assert_not_called()
    position.assert_not_called()
    delay.assert_not_called()


@pytest.mark.parametrize("name,args", POINTER_ACTIONS[1:] + [("type_text", {"text": "test"})])
def test_actions_that_change_ui_still_capture_their_result(config, backend, name, args):
    assert config.auto_screenshot_after_action
    executor = ToolExecutor(backend, config)
    frame = executor.take_screenshot()
    result = executor.execute(ToolCall(name, args))
    assert result.ok, result.error
    # The auto-capture still runs after the action. On the simulated desktop these actions only
    # move the cursor (no window under the pointer), so the perceptually unchanged frame is
    # reused instead of being re-sent as a duplicate image.
    assert result.screenshot is not None
    if result.screenshot is frame:
        assert result.data.get("screen_unchanged") is True
        assert "no new image" in result.data.get("note", "")
    assert executor.screenshot_count == 2


def test_ui_change_after_action_yields_a_fresh_frame(config, backend):
    assert config.auto_screenshot_after_action
    executor = ToolExecutor(backend, config)
    frame = executor.take_screenshot()
    # clicking the fake Start button opens the large Start-menu window: a real, large screen change
    result = executor.execute(ToolCall("click", {"x": 20, "y": 440}))
    assert result.ok, result.error
    assert result.screenshot is not None and result.screenshot is not frame
    assert result.data.get("screen_unchanged") is not True
    assert executor.screenshot_count == 2


@pytest.mark.parametrize("protocol", ["native", "json"])
@pytest.mark.parametrize("observe_hover", [False, True])
def test_agent_captures_after_moving_only_when_explicitly_requested(config, backend, protocol, observe_hover):
    config.tool_protocol = protocol
    calls = [("mouse_move", {"x": 400, "y": 225})]
    if observe_hover:
        calls.append(("screenshot", {}))
    llm = ScriptedLLM([
        turn(protocol, *calls),
        turn(protocol, ("task_complete", {"summary": "Finished."})),
    ])
    images, results = [], []
    agent = Agent(config, backend, llm, AgentEvents(on_screenshot=images.append, on_tool_end=results.append))
    outcome = agent.run("hover and inspect the menu" if observe_hover else "move to the centre; do not click")
    assert outcome.status == "completed" and len(llm.calls) == 2
    assert backend.mouse == (800, 450)
    assert [event["kind"] for event in backend.events] == ["move"]
    assert results[0].screenshot is None
    assert agent.executor.screenshot_count == len(images) == (2 if observe_hover else 1)
    assert agent._model_frame is images[-1]
    if observe_hover:
        assert results[1].screenshot is images[1]


@pytest.mark.parametrize("protocol", ["native", "json"])
@pytest.mark.parametrize("space", ["image_pixels", "normalized_1000"])
def test_model_instructions_avoid_move_check_click_but_allow_hover_observation(protocol, space):
    prompt = build_system_prompt(protocol=protocol, vision=True, system_info={}, coordinate_space=space)
    assert "Mere pointer movement needs no position-validation step" in prompt
    assert "do not insert a move-and-check step first" in prompt
    assert "If hover opens a menu/tooltip you need to inspect, explicitly request screenshot" in prompt
    assert "verifying the result of each action" not in prompt
    schemas = {tool["function"]["name"]: tool["function"] for tool in openai_tool_schemas(space)}
    assert "No automatic screenshot is taken" in schemas["mouse_move"]["description"]
    assert "No automatic screenshot is taken" in tools_markdown(space)
    assert "no separate mouse_move is needed" in schemas["click"]["description"]
