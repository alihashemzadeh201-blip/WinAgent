"""Bad model output is recoverable; no malformed/partial tool batch may have side effects."""

import json
from unittest.mock import Mock

import pytest

from tests.conftest import ScriptedLLM, native_tool_message
from winagent.agent import Agent, AgentEvents
from winagent.config import Config
from winagent.llm import ChatResponse, LLMClient, LLMError
from winagent.protocol import parse_json_arguments, parse_json_protocol, parse_native_response

ECHO = "document, y from 0 (top) to 719 (bottom).]"


def response(message, finish="stop"):
    return ChatResponse(message, finish, {}, "mock", {}, 0.01)


@pytest.mark.parametrize("content", [
    ECHO, "", "  ", "{}", "[]", '{"actions": [', '{"actions":[{"tool":"click","args":{"x":10,"y":20}}]',
    '{"thought":"still thinking"}', '{"actions":[]}', '{"actions":null}',
    '{"actions":{"tool":"click","args":{}}}', '{"actions":"click"}',
    '{"actions":[null]}', '{"actions":[{}]}', '{"actions":[{"tool":"  ","args":{}}]}',
    '{"actions":[{"tool":"click","args":"broken"}]}', '{"actions":[{"tool":"click","args":[]}]}',
    '{"actions":[{"tool":"click","args":{"x":50}}]}', '{"message":null}', '{"message":{}}',
    '{"message":"done","actions":[{"tool":"mouse_move","args":{"x":10}}]}',
])
def test_malformed_json_is_marked_unusable_in_both_protocols(content):
    for turn in (parse_json_protocol(content), parse_native_response({"content": content})):
        assert turn.parse_error, content
        assert not turn.tool_calls and not turn.text


@pytest.mark.parametrize("message", [
    {"content": {}}, {"content": ["wrong part"]}, {"content": [{"text": 123}]},
    {"tool_calls": "bad"}, {"tool_calls": {}}, {"tool_calls": [None]},
    {"tool_calls": [{"function": []}]}, {"tool_calls": [{"function": {"arguments": "{}"}}]},
    {"tool_calls": [{"function": {"name": "click", "arguments": "{bad"}}]},
    {"tool_calls": [{"function": {"name": "click", "arguments": None}}]},
    {"tool_calls": [{"function": {"name": "click", "arguments": ""}}]},
    {"tool_calls": [{"function": {"name": "click", "arguments": "  "}}]},
    {"reasoning_content": "I am thinking", "content": None},
])
def test_malformed_native_shapes_do_not_crash_or_invent_empty_arguments(message):
    turn = parse_native_response(message)
    assert turn.parse_error and not turn.tool_calls and not turn.text


@pytest.mark.parametrize("text", ["سلام!", "The y coordinate starts at the top of the image.",
                                  "The label says y from 0 (top) to 719 (bottom).", '{"result":42}', '[1,2,3]'])
def test_genuine_native_answers_are_not_mistaken_for_bad_output(text):
    turn = parse_native_response({"content": text})
    assert turn.text == text and not turn.parse_error


def test_native_json_message_and_content_parts_are_unwrapped():
    turn = parse_native_response({"content": [{"type": "text", "text": '{"message":"سلام"}'}]})
    assert turn.text == "سلام" and not turn.parse_error


@pytest.mark.parametrize("protocol", ["native", "json"])
def test_user_reported_metadata_fragment_is_retried_without_replaying_actions(config, backend, protocol):
    config.tool_protocol = protocol
    def tool(name, args):
        return native_tool_message((name, args)) if protocol == "native" else json.dumps({"actions": [{"tool": name, "args": args}]})
    llm = ScriptedLLM([tool("type_text", {"text": "only once"}), ECHO, tool("task_complete", {"summary": "done"})])
    texts, statuses = [], []
    agent = Agent(config, backend, llm, AgentEvents(on_assistant_text=texts.append, on_status=statuses.append))
    outcome = agent.run("type the requested text")
    assert outcome.status == "completed" and outcome.tool_calls == 2 and outcome.steps == 2
    assert backend.typed == ["only once"]
    assert ECHO not in texts
    assert len(llm.calls) == 3 and any("requesting correction (1/3)" in s for s in statuses)
    assert llm.calls[2]["messages"][:-1] == llm.calls[1]["messages"]  # same screenshot/results, no new side effects
    repair = llm.calls[2]["messages"][-1]
    assert repair["role"] == "user" and "NO actions" in repair["content"]
    assert "earlier successful actions" in repair["content"]
    assert not any("previous response was invalid" in str(m) for m in agent.history)


@pytest.mark.parametrize("protocol", ["native", "json"])
def test_entire_malformed_batch_is_discarded_before_any_execution(config, backend, protocol):
    config.tool_protocol = protocol
    valid_prefix = {"tool": "type_text", "args": {"text": "do not type twice"}}
    bad_suffix = {"tool": "click", "args": '{"x": 10, "y":'}
    if protocol == "native":
        broken = native_tool_message((valid_prefix["tool"], valid_prefix["args"]), ("click", {}))
        broken["tool_calls"][1]["function"]["arguments"] = bad_suffix["args"]
        fixed = native_tool_message(("task_complete", {"summary": "fixed"}))
    else:
        broken = json.dumps({"message": "not a final answer", "actions": [valid_prefix, bad_suffix]})
        fixed = '{"actions":[{"tool":"task_complete","args":{"summary":"fixed"}}]}'
    texts = []
    llm = ScriptedLLM([broken, fixed])
    agent = Agent(config, backend, llm, AgentEvents(on_assistant_text=texts.append))
    outcome = agent.run("work")
    assert outcome.status == "completed" and outcome.tool_calls == 1
    assert backend.typed == [] and not any(e["kind"] == "click" for e in backend.events)
    assert texts == ["fixed"]
    assert not any(m.get("role") == "tool" for m in llm.calls[1]["messages"])
    assert not any(m.get("tool_calls") for m in llm.calls[1]["messages"])


@pytest.mark.parametrize("finish", ["length", "max_tokens", "max_output_tokens"])
def test_truncated_but_parseable_tool_response_is_not_executed(config, backend, finish):
    partial = native_tool_message(("type_text", {"text": "must not execute a prefix"}))
    llm = ScriptedLLM([response(partial, finish), "Please increase the output token limit."])
    agent = Agent(config, backend, llm)
    assert agent.run("work").status == "answered"
    assert len(llm.calls) == 2 and not backend.typed
    assert "truncated" in llm.calls[1]["messages"][-1]["content"]


@pytest.mark.parametrize("budget", [0, 1, 3])
def test_recovery_is_bounded_and_next_run_starts_with_a_fresh_budget(config, backend, budget):
    config.max_response_retries = budget
    errors, texts = [], []
    llm = ScriptedLLM([ECHO] * (budget + 1) + ["A valid new answer."])
    agent = Agent(config, backend, llm, AgentEvents(on_error=errors.append, on_assistant_text=texts.append))
    outcome = agent.run("first task", initial_screenshot=False)
    assert outcome.status == "error" and outcome.tool_calls == 0
    assert len(llm.calls) == budget + 1 and errors and not texts
    assert "invalid" in errors[0] and f"{budget + 1} attempts" in errors[0]
    assert agent.run("new task", initial_screenshot=False).status == "answered"
    assert "previous response was invalid" not in str(llm.calls[-1]["messages"])


def test_stop_during_repair_status_prevents_another_request(config, backend):
    llm = ScriptedLLM([ECHO, "must not request"])
    agent = Agent(config, backend, llm)
    agent.events.on_status = lambda s: agent.stop() if "requesting correction" in s else None
    assert agent.run("work", initial_screenshot=False).status == "stopped"
    assert len(llm.calls) == 1 and not backend.events


@pytest.mark.parametrize("protocol", ["native", "json"])
def test_refusal_is_not_retried_as_a_format_error(config, backend, protocol):
    config.tool_protocol = protocol
    llm = ScriptedLLM([{"content": None, "refusal": "I cannot help with that."}])
    outcome = Agent(config, backend, llm).run("question", initial_screenshot=False)
    assert outcome.status == "answered" and outcome.message == "I cannot help with that."
    assert len(llm.calls) == 1


def test_content_filter_and_authentication_failures_are_not_response_retries(config, backend):
    for fault in (response({"content": ""}, "content_filter"), LLMError("Authentication failed (401)", 401)):
        llm = ScriptedLLM([fault])
        assert Agent(config, backend, llm).run("question", initial_screenshot=False).status == "error"
        assert len(llm.calls) == 1


@pytest.mark.parametrize("envelope", [None, [], {}, {"choices": []}, {"choices": [None]},
                                       {"choices": [{"message": []}]}, {"choices": [{"message": "bad"}]}])
def test_http_200_invalid_envelope_is_recoverable(monkeypatch, config, backend, envelope):
    client = LLMClient(config)
    good = {"choices": [{"message": {"content": "good answer"}, "finish_reason": "stop"}]}
    post = Mock(side_effect=[Mock(status_code=200, json=Mock(return_value=envelope)),
                             Mock(status_code=200, json=Mock(return_value=good))])
    monkeypatch.setattr(client.session, "post", post)
    assert Agent(config, backend, client).run("question", initial_screenshot=False).message == "good answer"
    assert post.call_count == 2


def test_http_200_non_json_is_recoverable(monkeypatch, config, backend):
    client = LLMClient(config)
    good = {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}
    post = Mock(side_effect=[Mock(status_code=200, json=Mock(side_effect=ValueError("bad JSON")), text="broken"),
                             Mock(status_code=200, json=Mock(return_value=good))])
    monkeypatch.setattr(client.session, "post", post)
    assert Agent(config, backend, client).run("question", initial_screenshot=False).message == "OK"
    assert post.call_count == 2


def test_recovery_config_defaults_validation_and_round_trip():
    assert Config().max_response_retries == 3
    assert Config.from_dict({"max_response_retries": "0"}).max_response_retries == 0
    for invalid in (-1, 11):
        assert any("max_response_retries" in e for e in Config(max_response_retries=invalid).validate())
    cfg = Config(max_response_retries=5)
    assert cfg.copy().max_response_retries == 5 and not cfg.validate()


def test_lenient_repair_preserves_string_literals_exactly():
    arguments = '{"text":"True, } None False, ] and “quoted” سلام", "press_enter": True,}'
    parsed = parse_json_arguments(arguments)
    assert parsed == {"text": "True, } None False, ] and “quoted” سلام", "press_enter": True}
    assert parse_json_arguments("{'text': \"it's True\",}") == {"text": "it's True"}


def test_duplicate_native_ids_reject_the_whole_batch():
    message = native_tool_message(("click", {"x": 20, "y": 40}), ("click", {"x": 50, "y": 60}))
    message["tool_calls"][1]["id"] = message["tool_calls"][0]["id"]
    turn = parse_native_response(message)
    assert "Duplicate" in turn.parse_error and not turn.tool_calls


def test_optional_non_text_reasoning_does_not_break_valid_native_calls():
    message = native_tool_message(("screenshot", {}))
    message["reasoning"] = {"provider_specific": "metadata"}
    assert not parse_native_response(message).parse_error


@pytest.mark.parametrize("capability", ["tools", "vision"])
def test_feature_negotiation_during_recovery_has_a_separate_budget(config, backend, capability):
    config.max_response_retries = 1
    def unsupported(messages):
        setattr(llm, f"supports_{capability}", False)
        raise LLMError(f"{capability} unsupported", status=400)
    final = '{"message":"fixed"}' if capability == "tools" else "fixed"
    llm = ScriptedLLM([ECHO, unsupported, final])
    agent = Agent(config, backend, llm)
    assert agent.run("question").message == "fixed"
    assert len(llm.calls) == 3
    if capability == "tools":
        assert agent.protocol == "json" and llm.calls[-1]["tools"] is None
        assert "ONE complete JSON object" in llm.calls[-1]["messages"][-1]["content"]
    else:
        assert not agent.vision
        assert all(not isinstance(m.get("content"), list) for m in llm.calls[-1]["messages"])
