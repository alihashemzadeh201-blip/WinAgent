"""Loop protection (stalled actions) and the pre-task input-language check at the agent level."""

import json

from tests.conftest import ScriptedLLM, native_tool_message
from winagent.agent import Agent, AgentEvents
from winagent.protocol import ToolCall
from winagent.tools import ToolExecutor


def turn(name, args):
    return native_tool_message((name, args))


def stall_messages(agent):
    return [m for m in agent.history
            if isinstance(m.get("content"), str) and "Stall detected" in m["content"]]


def first_user_text(llm):
    msgs = llm.calls[0]["messages"]
    content = msgs[-1]["content"]
    if isinstance(content, str):
        return content
    return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")


# ---------------------------------------------------------------- stall / loops
def test_jittered_repeated_clicks_trigger_one_stall_warning(config, backend):
    config.tool_protocol = "native"
    llm = ScriptedLLM([
        turn("click", {"x": 400, "y": 225}),   # a "disabled" spot that never reacts
        turn("click", {"x": 404, "y": 228}),   # a few px of jitter must NOT reset the counter
        turn("click", {"x": 410, "y": 232}),
        turn("task_complete", {"summary": "Giving up on this step."}),
    ])
    agent = Agent(config, backend, llm)
    outcome = agent.run("click that combo box")
    assert outcome.status == "completed"
    assert len(stall_messages(agent)) == 1
    # the model actually saw the warning in its next request
    last_request = json.dumps(llm.calls[-1]["messages"], ensure_ascii=False, default=str)
    assert "Stall detected" in last_request
    assert "disabled" in stall_messages(agent)[0]["content"]


def test_same_tool_different_targets_still_stalls(config, backend):
    config.tool_protocol = "native"
    llm = ScriptedLLM([
        turn("click", {"x": 100, "y": 100}),
        turn("click", {"x": 300, "y": 100}),   # different spots, same dead tool
        turn("click", {"x": 500, "y": 100}),
        turn("click", {"x": 700, "y": 100}),
        turn("task_complete", {"summary": "Moving on."}),
    ])
    agent = Agent(config, backend, llm)
    agent.run("open the dropdown")
    assert len(stall_messages(agent)) == 1


def test_progress_resets_and_prevents_false_stalls(config, backend):
    # two unchanged clicks, then a REAL screen change (Start menu opens), then two more
    # unchanged clicks: the progress round resets the counter, so no stall is ever reported
    config.tool_protocol = "native"
    llm = ScriptedLLM([
        turn("click", {"x": 100, "y": 100}),
        turn("click", {"x": 100, "y": 100}),
        turn("click", {"x": 20, "y": 440}),    # opens the Start menu -> visible change
        turn("click", {"x": 100, "y": 100}),
        turn("click", {"x": 100, "y": 100}),
        turn("task_complete", {"summary": "Done."}),
    ])
    agent = Agent(config, backend, llm)
    agent.run("poke the desktop")
    assert stall_messages(agent) == []


def test_failed_calls_also_count_as_no_progress(config, backend):
    config.tool_protocol = "native"
    # click far outside the screen bounds -> validation error every round (no input sent)
    llm = ScriptedLLM([
        turn("click", {"x": 9000, "y": 9000}),
        turn("click", {"x": 9001, "y": 9001}),
        turn("click", {"x": 9002, "y": 9002}),
        turn("task_complete", {"summary": "Coordinates were invalid."}),
    ])
    agent = Agent(config, backend, llm)
    agent.run("click there")
    assert len(stall_messages(agent)) == 1


# ----------------------------------------------------- pre-task language check
def test_task_start_fixes_wrong_layout_and_tells_the_model(config, backend):
    config.tool_protocol = "native"
    backend.open_application("notepad.exe")
    backend.fake_keyboard_layout = "fa-IR"
    llm = ScriptedLLM([turn("task_complete", {"summary": "ok"})])
    agent = Agent(config, backend, llm)
    agent.run("hello")
    text = first_user_text(llm)
    assert "Input language check before any action" in text
    assert "fa-IR" in text and "en-US" in text
    assert backend.layout_changes == ["en-US"]  # corrected BEFORE any action


def test_task_start_is_silent_when_layout_is_correct(config, backend):
    config.tool_protocol = "native"
    llm = ScriptedLLM([turn("task_complete", {"summary": "ok"})])
    agent = Agent(config, backend, llm)
    agent.run("hello")
    assert "Input language check" not in first_user_text(llm)
    assert backend.layout_changes == []


def test_wrong_layout_without_autofix_is_only_reported(config, backend):
    config.tool_protocol = "native"
    config.auto_fix_keyboard_layout = False
    backend.open_application("notepad.exe")
    backend.fake_keyboard_layout = "fa-IR"
    llm = ScriptedLLM([turn("task_complete", {"summary": "ok"})])
    agent = Agent(config, backend, llm)
    agent.run("hello")
    text = first_user_text(llm)
    assert "Input language check before any action" in text
    assert "disabled" in text
    assert backend.layout_changes == []  # nothing switched


def test_key_input_reports_layout_mismatch_when_autofix_disabled(config, backend):
    config.auto_fix_keyboard_layout = False
    backend.fake_keyboard_layout = "fa-IR"
    result = ToolExecutor(backend, config).execute(ToolCall("press_keys", {"keys": "ctrl+s"}))
    assert result.ok
    assert "fa-IR" in result.data["layout"] and "disabled" in result.data["layout"]
    assert backend.layout_changes == []


def test_keyboard_input_unchanged_note_is_not_a_failure(config, backend):
    # typing on the bare desktop changes nothing visible -> deduped frame, but the note must
    # warn that small changes can be below the detection threshold (do NOT re-type blindly)
    executor = ToolExecutor(backend, config)
    executor.take_screenshot()
    result = executor.execute(ToolCall("type_text", {"text": "ab"}))
    assert result.ok
    assert result.data.get("screen_unchanged") is True
    assert "detection threshold" in result.data["note"]
    assert "before repeating" in result.data["note"]


def test_pointer_unchanged_note_stays_strong(config, backend):
    executor = ToolExecutor(backend, config)
    executor.take_screenshot()
    result = executor.execute(ToolCall("click", {"x": 400, "y": 225}))
    assert result.ok
    assert result.data.get("screen_unchanged") is True
    assert "no visible effect" in result.data["note"]
