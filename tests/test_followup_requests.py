"""User follow-ups keep intent/results, not the previous task's live protocol or stale screen."""

import json
from unittest.mock import Mock

import pytest

from tests.conftest import ScriptedLLM, native_tool_message
from winagent.agent import Agent, AgentEvents
from winagent.backends import BackendError
from winagent.protocol import ToolCall


def turn(protocol, *calls):
    if protocol == "native":
        return native_tool_message(*calls)
    return json.dumps({"actions": [{"tool": name, "args": args} for name, args in calls]}, ensure_ascii=False)


def text(message):
    content = message.get("content")
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if part.get("type") == "text")
    return content or ""


def images(messages):
    return [part for message in messages if isinstance(message.get("content"), list)
            for part in message["content"] if part.get("type") == "image_url"]


def assert_native_pairs(messages):
    pending = set()
    for message in messages:
        if message.get("role") == "tool":
            assert message["tool_call_id"] in pending, "orphan tool result"
            pending.remove(message["tool_call_id"])
            continue
        assert not pending, "an assistant tool batch lost one or more matching results"
        if message.get("role") == "assistant":
            pending = {call["id"] for call in message.get("tool_calls", [])}
    assert not pending


def assert_followup_is_clean(messages, previous_task, current_task, summary, old_frames=()):
    assert text(messages[-1]).startswith(f"[Current user request]\n{current_task}")
    assert messages[-2]["role"] == "assistant"
    assert json.loads(messages[-2]["content"])["message"] == summary
    assert any(message.get("role") == "user" and text(message) == previous_task for message in messages)
    for message in messages[1:-1]:
        assert message["role"] in ("user", "assistant")
        assert not message.get("tool_calls")
        if message["role"] == "assistant":
            data = json.loads(message["content"])
            assert "actions" not in data and "tool_results" not in data
    all_text = "\n".join(text(message) for message in messages)
    assert all(frame.frame_id not in all_text for frame in old_frames)
    assert "tool work in progress" not in text(messages[0])
    assert_native_pairs(messages)


@pytest.mark.parametrize("protocol", ["native", "json"])
@pytest.mark.parametrize("vision", [False, True])
def test_followup_keeps_file_context_and_uses_only_the_new_screen(config, backend, tmp_path, protocol, vision):
    config.tool_protocol = protocol
    config.vision_enabled = vision
    path = tmp_path / "گزارش.txt"
    first_task = "یک گزارش بساز"
    second_task = "حالا به همان فایل یک خط دیگر اضافه کن"
    shots = []
    old_frames = []

    def followup(messages):
        assert_followup_is_clean(messages, first_task, second_task, "گزارش ساخته شد.", old_frames)
        assert len(images(messages)) == int(vision)
        previous = json.loads(messages[-2]["content"])["previous_request"]
        assert previous["status"] == "completed"
        # The filename was chosen by the model, not present in the user's task or final summary.
        assert str(path) in json.dumps(previous, ensure_ascii=False)
        assert "coordinate_mapping" not in json.dumps(previous)
        return turn(protocol, ("write_file", {"path": str(path), "content": "دوم\n", "append": True}))

    llm = ScriptedLLM([
        turn(protocol, ("write_file", {"path": str(path), "content": "اول\n"}), ("get_screen_info", {})),
        turn(protocol, ("task_complete", {"summary": "گزارش ساخته شد."})),
        followup,
        turn(protocol, ("task_complete", {"summary": "خط دوم اضافه شد."})),
    ])
    agent = Agent(config, backend, llm, AgentEvents(on_screenshot=shots.append))
    assert agent.run(first_task).status == "completed"
    old_frames[:] = shots
    assert agent.run(second_task).status == "completed"
    assert path.read_text() == "اول\nدوم\n"
    assert len(llm.calls) == 4
    if vision:
        assert agent._model_frame is shots[-1] and shots[-1] not in old_frames


@pytest.mark.parametrize("protocol", ["native", "json"])
def test_second_task_repair_uses_current_request_and_never_replays_first_task(config, backend, protocol):
    config.tool_protocol = protocol
    llm = ScriptedLLM([
        turn(protocol, ("type_text", {"text": "first"})),
        turn(protocol, ("task_complete", {"summary": "First finished."})),
        "b sideways.",
        turn(protocol, ("type_text", {"text": "second"})),
        '{"message":"b sideways."}',
        turn(protocol, ("task_complete", {"summary": "Second finished."})),
    ])
    statuses = []
    agent = Agent(config, backend, llm, AgentEvents(on_status=statuses.append))
    assert agent.run("type first").status == "completed"
    assert agent.run("now type second").status == "completed"
    assert backend.typed == ["first", "second"]
    assert len(llm.calls) == 6
    assert sum("requesting correction" in status for status in statuses) == 2
    assert llm.calls[3]["messages"][:-1] == llm.calls[2]["messages"]
    assert "CURRENT user request" in llm.calls[3]["messages"][-1]["content"]
    assert "b sideways." not in str(agent.followup_history())
    for call in llm.calls:
        assert_native_pairs(call["messages"])


@pytest.mark.parametrize("protocol", ["native", "json"])
def test_large_atomic_batch_cannot_trim_away_the_followup_or_orphan_tool_results(config, backend, protocol):
    config.tool_protocol = protocol
    config.max_history_messages = 10
    config.auto_screenshot_after_action = False
    task = "SECOND request: type each item once"

    def finish(messages):
        assert any(text(message).startswith(f"[Current user request]\n{task}") for message in messages)
        assert_native_pairs(messages)
        if protocol == "native":
            # The soft message cap must yield to an intact current batch, not cut it in half.
            assert len([message for message in messages if message["role"] == "tool"]) == 15
        return turn(protocol, ("task_complete", {"summary": "Finished."}))

    llm = ScriptedLLM(['{"message":"First reply."}',
                       turn(protocol, *[("type_text", {"text": str(i)}) for i in range(15)]), finish])
    agent = Agent(config, backend, llm)
    assert agent.run("FIRST request", initial_screenshot=False).status == "answered"
    assert agent.run(task, initial_screenshot=False).status == "completed"
    assert backend.typed == [str(i) for i in range(15)]
    assert_native_pairs(agent.history)


@pytest.mark.parametrize("protocol", ["native", "json"])
def test_long_followup_pins_goal_and_records_work_when_old_rounds_are_trimmed(config, backend, protocol):
    config.tool_protocol = protocol
    config.max_history_messages = 10
    config.auto_screenshot_after_action = False
    task = "ادامه: این دوازده خط را فقط یک بار بنویس"

    def finish(messages):
        assert any(text(message).startswith(f"[Current user request]\n{task}") for message in messages)
        notes = [message for message in messages if text(message).startswith("[Earlier work in THIS request.")]
        assert len(notes) == 1
        observations = json.loads(text(notes[0]).split("\n", 1)[1])["observed_work"]
        assert [record["arguments"]["text"] for record in observations] == [f"line {i}" for i in range(12)]
        assert all(record["ok"] for record in observations)
        assert_native_pairs(messages)
        return turn(protocol, ("task_complete", {"summary": "Twelve lines written."}))

    llm = ScriptedLLM(['{"message":"Previous answer."}']
                      + [turn(protocol, ("type_text", {"text": f"line {i}"})) for i in range(12)] + [finish])
    agent = Agent(config, backend, llm)
    agent.run("old request", initial_screenshot=False)
    assert agent.run(task, initial_screenshot=False).status == "completed"
    assert backend.typed == [f"line {i}" for i in range(12)]
    for call in llm.calls:
        assert_native_pairs(call["messages"])


def test_trimming_does_not_drop_the_image_that_defines_pending_coordinates(config, backend):
    config.max_history_messages = 10
    config.max_images_in_context = 2
    shots = []

    def finish(messages):
        assert len(shots) == 2
        latest = shots[-1]
        image_messages = [message for message in messages if images([message])]
        assert any(latest.frame_id in text(message) for message in image_messages)
        assert agent._model_frame is latest
        assert_native_pairs(messages)
        return native_tool_message(("task_complete", {"summary": "Checked."}))

    llm = ScriptedLLM([native_tool_message(("type_text", {"text": "one UI change"}))]
                      + [native_tool_message(("get_screen_info", {}))] * 12 + [finish])
    agent = Agent(config, backend, llm, AgentEvents(on_screenshot=shots.append))
    assert agent.run("check the current window").status == "completed"
    assert backend.typed == ["one UI change"]


@pytest.mark.parametrize("initial_screenshot", [False, True])
def test_followup_without_a_new_image_cannot_reuse_old_frame(config, backend, monkeypatch, initial_screenshot):
    config.coordinate_space = "normalized_1000"

    def followup(messages):
        assert not images(messages)
        assert agent._model_frame is None and agent.executor.last_screenshot is None
        result = agent.executor.execute(ToolCall("click", {"x": 500, "y": 500}))
        assert not result.ok and result.needs_new_screenshot
        return '{"message":"A fresh screen is needed."}'

    llm = ScriptedLLM([native_tool_message(("task_complete", {"summary": "First finished."})), followup])
    agent = Agent(config, backend, llm)
    agent.run("first request")
    old = agent._model_frame
    assert old is not None
    monkeypatch.setattr(backend, "capture", Mock(side_effect=BackendError("capture unavailable")))
    assert agent.run("continue", initial_screenshot=initial_screenshot).status == "answered"
    assert old.frame_id not in str(llm.calls[-1]["messages"])
    assert not backend.events
    if initial_screenshot:
        assert "No current screenshot is available" in text(llm.calls[-1]["messages"][-1])


@pytest.mark.parametrize("end", ["error", "stopped", "max_steps", "waiting_user"])
def test_followup_records_partial_work_without_claiming_previous_success(config, backend, end):
    config.max_response_retries = 0
    config.max_steps = 2 if end == "max_steps" else 40
    if end == "error":
        tail = ["b sideways."]
    elif end == "max_steps":
        tail = [native_tool_message(("get_screen_info", {}))]
    elif end == "waiting_user":
        tail = [native_tool_message(("ask_user", {"question": "Which file?"}))]
    else:
        def cancel(messages):
            agent.stop()
            return '{"message":"discard after stop"}'
        tail = [cancel]

    def continue_task(messages):
        previous = json.loads(messages[-2]["content"])["previous_request"]
        assert previous["status"] == end
        assert previous["observations"][0]["arguments"]["text"] == "only once"
        assert previous["observations"][0]["ok"] is True
        assert "b sideways." not in str(messages)
        assert not agent.stop_event.is_set()
        assert_native_pairs(messages)
        return native_tool_message(("task_complete", {"summary": "Continued without replay."}))

    llm = ScriptedLLM([native_tool_message(("type_text", {"text": "only once"}))] + tail + [continue_task])
    agent = Agent(config, backend, llm)
    assert agent.run("work").status == end
    assert agent.run("continue from there").status == "completed"
    assert backend.typed == ["only once"]


def test_reset_removes_handoffs_and_export_is_independent_of_protocol(config, backend):
    llm = ScriptedLLM([
        native_tool_message(("type_text", {"text": "remember this"})),
        native_tool_message(("task_complete", {"summary": "First done."})),
    ])
    agent = Agent(config, backend, llm)
    agent.run("first task")
    handoff = agent.followup_history()
    assert len(handoff) == 2 and not images(handoff)
    assert not any(message.get("tool_calls") or message["role"] == "tool" for message in handoff)
    handoff[0]["content"] = "do not mutate the original"
    assert agent.followup_history()[0]["content"] == "first task"
    config.tool_protocol = "json"
    llm = ScriptedLLM([turn("json", ("task_complete", {"summary": "Next done."}))])
    rebuilt = Agent(config, backend, llm)
    rebuilt.history = agent.followup_history()
    assert rebuilt.run("then continue").status == "completed"
    assert "First done." in str(llm.calls[0]["messages"])
    assert not any(message.get("tool_calls") for message in llm.calls[0]["messages"])
    rebuilt.reset()
    assert rebuilt.history == rebuilt.followup_history() == []
    assert rebuilt._model_frame is None and rebuilt.executor.last_screenshot is None


def test_many_followups_evict_complete_old_pairs_not_current_request(config, backend):
    config.max_history_messages = 10
    llm = ScriptedLLM([json.dumps({"message": f"answer {i}"}) for i in range(12)])
    agent = Agent(config, backend, llm)
    for i in range(12):
        assert agent.run(f"task {i}", initial_screenshot=False).status == "answered"
        assert text(llm.calls[-1]["messages"][-1]) == f"[Current user request]\ntask {i}"
    handoff = agent.followup_history()
    assert len(handoff) <= config.max_history_messages and len(handoff) % 2 == 0
    assert [message["role"] for message in handoff] == ["user", "assistant"] * (len(handoff) // 2)
    assert handoff[-2]["content"] == "task 11"
    assert "task 0" not in str(handoff)
