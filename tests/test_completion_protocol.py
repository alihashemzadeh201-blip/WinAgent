"""Terminal responses are explicit; arbitrary prose fragments must never end desktop work."""

import json

import pytest

from tests.conftest import ScriptedLLM, native_tool_message
from winagent.agent import Agent, AgentEvents
from winagent.prompts import build_system_prompt
from winagent.protocol import parse_native_response


def tool_turn(protocol, *calls):
    if protocol == "native":
        return native_tool_message(*calls)
    return json.dumps({"actions": [{"tool": name, "args": args} for name, args in calls]})


@pytest.mark.parametrize("protocol", ["native", "json"])
@pytest.mark.parametrize("fragment", ["b sideways.", "unexpected tail)", "... و سپس", "Done."])
def test_unstructured_first_reply_is_regenerated_not_displayed_as_an_answer(config, backend, protocol, fragment):
    config.tool_protocol = protocol
    texts = []
    llm = ScriptedLLM([fragment, '{"message":"سلام!"}'])
    agent = Agent(config, backend, llm, AgentEvents(on_assistant_text=texts.append))
    outcome = agent.run("سلام")
    assert outcome.status == "answered" and texts == ["سلام!"]
    assert len(llm.calls) == 2 and outcome.tool_calls == 0
    assert agent.executor.screenshot_count == 1
    assert "Do not simply quote or wrap" in llm.calls[1]["messages"][-1]["content"]
    assert not any(fragment in str(m) for m in agent.history)
    assert json.loads(agent.history[-1]["content"]) == {"message": "سلام!"}


@pytest.mark.parametrize("protocol", ["native", "json"])
@pytest.mark.parametrize("bad_reply", ["b sideways.", '{"message":"b sideways."}', '{"message":"Done."}'])
def test_active_task_requires_explicit_completion_without_replaying_tools(config, backend, protocol, bad_reply):
    config.tool_protocol = protocol
    texts, statuses = [], []
    llm = ScriptedLLM([
        tool_turn(protocol, ("type_text", {"text": "only once"})),
        bad_reply,
        tool_turn(protocol, ("task_complete", {"summary": "finished", "success": True})),
    ])
    agent = Agent(config, backend, llm, AgentEvents(on_assistant_text=texts.append, on_status=statuses.append))
    outcome = agent.run("type the text")
    assert outcome.status == "completed" and outcome.steps == 2 and outcome.tool_calls == 2
    assert backend.typed == ["only once"] and texts == ["finished"]
    assert len(llm.calls) == 3 and any("requesting correction" in s for s in statuses)
    assert "tool work in progress" in llm.calls[1]["messages"][0]["content"]
    assert llm.calls[2]["messages"][:-1] == llm.calls[1]["messages"]
    correction = llm.calls[2]["messages"][-1]["content"]
    assert "task_complete" in correction and "success=false" in correction
    assert "do NOT repeat earlier successful actions" in correction
    assert "b sideways." not in str(agent.history)
    assert "previous response was invalid" not in str(agent.history)


@pytest.mark.parametrize("budget", [0, 1, 3])
def test_unstructured_replies_after_actions_have_a_bounded_budget(config, backend, budget):
    config.max_response_retries = budget
    errors, texts = [], []
    llm = ScriptedLLM([native_tool_message(("type_text", {"text": "once"}))] + ["b sideways."] * (budget + 1))
    agent = Agent(config, backend, llm, AgentEvents(on_error=errors.append, on_assistant_text=texts.append))
    outcome = agent.run("type once")
    assert outcome.status == "error" and outcome.tool_calls == 1
    assert backend.typed == ["once"] and not texts
    assert len(llm.calls) == budget + 2
    assert len(errors) == 1 and "invalid" in errors[0]
    assert "No actions from the rejected response were executed" in errors[0]


@pytest.mark.parametrize("protocol", ["native", "json"])
@pytest.mark.parametrize("summary", ["", "  ", None, 7])
def test_empty_or_nontext_completion_is_rejected_atomically(config, backend, protocol, summary):
    config.tool_protocol = protocol
    llm = ScriptedLLM([
        tool_turn(protocol, ("type_text", {"text": "do not execute this prefix"}), ("task_complete", {"summary": summary})),
        tool_turn(protocol, ("task_complete", {"summary": "No changes made.", "success": False})),
    ])
    outcome = Agent(config, backend, llm).run("work")
    assert outcome.status == "answered" and outcome.message == "No changes made."
    assert len(llm.calls) == 2 and not backend.typed
    assert outcome.tool_calls == 1


@pytest.mark.parametrize("text", ["بله", "نه", "OK", "۴", "[1, 2, 3]"])
def test_short_legitimate_chat_answers_need_no_length_or_language_heuristic(config, backend, text):
    llm = ScriptedLLM([json.dumps({"message": text}, ensure_ascii=False)])
    outcome = Agent(config, backend, llm).run("a simple question", initial_screenshot=False)
    assert outcome.status == "answered" and outcome.message == text
    assert len(llm.calls) == 1 and not backend.events


@pytest.mark.parametrize("protocol", ["native", "json"])
def test_provider_refusal_after_tool_work_is_terminal_without_format_retries(config, backend, protocol):
    config.tool_protocol = protocol
    llm = ScriptedLLM([
        tool_turn(protocol, ("get_screen_info", {})),
        {"content": None, "refusal": "I cannot help with that."},
    ])
    outcome = Agent(config, backend, llm).run("question")
    assert outcome.status == "answered" and outcome.message == "I cannot help with that."
    assert len(llm.calls) == 2


def test_stop_during_completion_repair_keeps_successful_work_and_sends_no_more_requests(config, backend):
    llm = ScriptedLLM([native_tool_message(("type_text", {"text": "once"})), "b sideways."])
    agent = Agent(config, backend, llm)
    agent.events.on_status = lambda text: agent.stop() if "requesting correction" in text else None
    assert agent.run("work").status == "stopped"
    assert len(llm.calls) == 2 and backend.typed == ["once"]


def test_native_parser_exposes_strict_mode_for_agent_without_breaking_legacy_readers():
    strict = parse_native_response({"content": "b sideways."}, allow_plain_text=False)
    assert strict.parse_error and not strict.text and not strict.tool_calls
    legacy = parse_native_response({"content": "A plain answer."}, allow_plain_text=True)
    assert not legacy.parse_error and legacy.text == "A plain answer."
    explicit = parse_native_response({"content": '{"message":"A complete answer."}'}, allow_plain_text=False)
    assert not explicit.parse_error and explicit.text == "A complete answer."


@pytest.mark.parametrize("protocol", ["native", "json"])
def test_protocol_prompts_require_chat_envelopes_and_explicit_tool_completion(protocol):
    prompt = build_system_prompt(protocol=protocol, vision=True, system_info={})
    assert '{"message":"your full answer"}' in prompt
    assert 'finish ONLY with `task_complete`' in prompt
    assert "success=false" in prompt
    assert "just answer in text" not in prompt


def test_new_chat_request_after_completed_work_does_not_require_tool_completion(config, backend):
    llm = ScriptedLLM([
        native_tool_message(("get_screen_info", {})),
        native_tool_message(("task_complete", {"summary": "Checked."})),
        '{"message":"You are welcome."}',
    ])
    agent = Agent(config, backend, llm)
    assert agent.run("check the screen").status == "completed"
    assert agent.run("thanks").message == "You are welcome."
    assert "Current task state: tool work in progress" not in llm.calls[-1]["messages"][0]["content"]


@pytest.mark.parametrize("truncated", [False, True])
def test_quoted_action_examples_are_not_executed_even_inside_a_truncated_message(config, backend, truncated):
    answer = "Example: ```json {'actions': [{'tool': 'click', 'args': {}}]} ```"
    envelope = json.dumps({"message": answer})
    responses = [envelope[:-1], '{"message":"Recovered answer."}'] if truncated else [envelope]
    llm = ScriptedLLM(responses)
    outcome = Agent(config, backend, llm).run("show an example; do not execute anything")
    assert outcome.status == "answered" and outcome.tool_calls == 0
    assert outcome.message == ("Recovered answer." if truncated else answer)
    assert len(llm.calls) == (2 if truncated else 1)
    assert not backend.events


def test_excessively_nested_model_json_is_a_recoverable_format_error(config, backend):
    broken = '{"actions":' + '[' * 1500 + '0' + ']' * 1500 + '}'
    llm = ScriptedLLM([broken, '{"message":"Recovered."}'])
    outcome = Agent(config, backend, llm).run("question", initial_screenshot=False)
    assert outcome.status == "answered" and outcome.message == "Recovered."
    assert len(llm.calls) == 2 and not backend.events
