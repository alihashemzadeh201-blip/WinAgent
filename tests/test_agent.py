import json

from tests.conftest import ScriptedLLM, native_tool_message
from winagent.agent import Agent, AgentEvents
from winagent.llm import LLMError


def make_agent(config, backend, llm, **events):
    ev = AgentEvents(**events)
    return Agent(config, backend, llm=llm, events=ev)


def test_native_flow_completes_task(config, backend):
    llm = ScriptedLLM([
        native_tool_message(("open_app", {"name": "notepad", "wait": 0})),
        native_tool_message(("click", {"x": 300, "y": 200}), ("type_text", {"text": "hello"})),
        native_tool_message(("task_complete", {"summary": "Notepad opened and text typed."})),
    ])
    texts = []
    tools = []
    agent = make_agent(config, backend, llm, on_assistant_text=texts.append, on_tool_end=lambda r: tools.append(r.call.name))
    outcome = agent.run("open notepad and type hello")
    assert outcome.status == "completed"
    assert outcome.message == "Notepad opened and text typed."
    assert tools == ["open_app", "click", "type_text", "task_complete"]
    assert "hello" in "".join(backend.typed)
    # history bookkeeping: system prompt is not in history; tool messages have ids
    roles = [m["role"] for m in agent.history]
    assert roles[0] == "user" and "tool" in roles
    # the second request carried the tool results of the first
    second = llm.calls[1]["messages"]
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "call_0" for m in second)
    assert second[0]["role"] == "system" and "WinAgent" in second[0]["content"]
    assert llm.calls[0]["tools"] is not None


def test_explicit_message_answer_ends_turn(config, backend):
    llm = ScriptedLLM([json.dumps({"message": "Hi! I'm WinAgent."})])
    agent = make_agent(config, backend, llm)
    outcome = agent.run("hi")
    assert outcome.status == "answered"
    assert outcome.message == "Hi! I'm WinAgent."
    assert json.loads(agent.history[-1]["content"]) == {"message": "Hi! I'm WinAgent."}


def test_json_protocol_flow(config, backend):
    config.tool_protocol = "json"
    llm = ScriptedLLM([
        '{"thought": "launch", "actions": [{"tool": "open_app", "args": {"name": "calc", "wait": 0}}]}',
        '{"actions": [{"tool": "task_complete", "args": {"summary": "opened calculator"}}]}',
    ])
    agent = make_agent(config, backend, llm)
    outcome = agent.run("open calculator")
    assert outcome.status == "completed"
    assert llm.calls[0]["tools"] is None
    system = llm.calls[0]["messages"][0]["content"]
    assert "Response protocol" in system and "open_app" in system
    # tool results were sent back as a JSON user message with an image
    follow = llm.calls[1]["messages"]
    last_user = [m for m in follow if m["role"] == "user"][-1]
    assert isinstance(last_user["content"], list)
    assert last_user["content"][0]["type"] == "text" and "tool_results" in last_user["content"][0]["text"]
    assert last_user["content"][1]["type"] == "image_url"


def test_auto_fallback_to_json_when_tools_unsupported(config, backend):
    def fail(messages):
        raise LLMError("tools is not supported by this model", status=400)

    llm = ScriptedLLM([
        fail,
        '{"actions": [{"tool": "task_complete", "args": {"summary": "ok"}}]}',
    ])
    statuses = []
    agent = make_agent(config, backend, llm, on_status=statuses.append)

    # emulate LLMClient's capability detection
    original_chat = llm.chat

    def chat(messages, tools=None, **kw):
        try:
            return original_chat(messages, tools=tools, **kw)
        except LLMError as exc:
            if tools is not None:
                llm.supports_tools = False
            raise exc

    llm.chat = chat
    outcome = agent.run("do it")
    assert outcome.status == "completed"
    assert agent.protocol == "json"
    assert any("JSON protocol" in s for s in statuses)


def test_vision_fallback_strips_images(config, backend):
    def fail(messages):
        raise LLMError("model does not support image_url content", status=400)

    llm = ScriptedLLM([fail, native_tool_message(("task_complete", {"summary": "ok"}))])
    agent = make_agent(config, backend, llm)
    original_chat = llm.chat

    def chat(messages, tools=None, **kw):
        try:
            return original_chat(messages, tools=tools, **kw)
        except LLMError:
            llm.supports_vision = False
            raise

    llm.chat = chat
    outcome = agent.run("hello")
    assert outcome.status == "completed"
    assert agent.vision is False
    msgs = llm.calls[1]["messages"]
    assert all(not isinstance(m.get("content"), list) for m in msgs)
    assert "no image input" in msgs[0]["content"]


def test_ask_user_roundtrip(config, backend):
    llm = ScriptedLLM([
        native_tool_message(("ask_user", {"question": "Which folder?", "options": ["Desktop", "Documents"]})),
        native_tool_message(("task_complete", {"summary": "saved to Desktop"})),
    ])
    asked = []

    def ask(q, opts):
        asked.append((q, opts))
        return "Desktop"

    agent = make_agent(config, backend, llm, ask_user=ask)
    outcome = agent.run("save the file")
    assert outcome.status == "completed"
    assert asked == [("Which folder?", ["Desktop", "Documents"])]
    tool_msgs = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"]
    assert json.loads(tool_msgs[-1]["content"])["user_answer"] == "Desktop"


def test_ask_user_cancelled_pauses_run(config, backend):
    llm = ScriptedLLM([native_tool_message(("ask_user", {"question": "Password?"}))])
    agent = make_agent(config, backend, llm, ask_user=lambda q, o: None)
    outcome = agent.run("login")
    assert outcome.status == "waiting_user"
    assert outcome.message == "Password?"


def test_confirmation_denied_is_reported_to_model(config, backend):
    llm = ScriptedLLM([
        native_tool_message(("run_command", {"command": "Remove-Item C:\\x"})),
        native_tool_message(("task_complete", {"summary": "cancelled", "success": False})),
    ])
    agent = make_agent(config, backend, llm, confirm=lambda call, reason: False)
    outcome = agent.run("delete x")
    assert outcome.status == "answered"
    tool_msgs = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"]
    assert "denied" in tool_msgs[-1]["content"]
    assert backend.commands == []


def test_max_steps_limit(config, backend):
    config.max_steps = 2
    llm = ScriptedLLM([native_tool_message(("wait", {"seconds": 0}))] * 5)
    agent = make_agent(config, backend, llm)
    outcome = agent.run("loop forever")
    assert outcome.status == "max_steps"
    assert outcome.steps == 2


def test_stop_event_interrupts(config, backend):
    agent_holder = {}

    def first(messages):
        agent_holder["agent"].stop()
        return native_tool_message(("wait", {"seconds": 5}))

    llm = ScriptedLLM([first])
    agent = make_agent(config, backend, llm)
    agent_holder["agent"] = agent
    outcome = agent.run("something long")
    assert outcome.status == "stopped"


def test_llm_error_is_reported(config, backend):
    llm = ScriptedLLM([LLMError("Authentication failed (401)")])
    errors = []
    agent = make_agent(config, backend, llm, on_error=errors.append)
    outcome = agent.run("hi")
    assert outcome.status == "error"
    assert "401" in outcome.message and errors


def test_image_trimming_keeps_only_recent_screenshots(config, backend):
    config.max_images_in_context = 2
    llm = ScriptedLLM([
        native_tool_message(("click", {"x": 1, "y": 1})),
        native_tool_message(("click", {"x": 2, "y": 2})),
        native_tool_message(("click", {"x": 3, "y": 3})),
        native_tool_message(("task_complete", {"summary": "ok"})),
    ])
    agent = make_agent(config, backend, llm)
    agent.run("click around")
    image_msgs = [m for m in agent.history if isinstance(m.get("content"), list)
                  and any(p.get("type") == "image_url" for p in m["content"])]
    assert len(image_msgs) == 2
    stripped = [m for m in agent.history if isinstance(m.get("content"), str) and "older screenshot removed" in m["content"]]
    assert stripped


def test_conversation_persists_between_runs(config, backend):
    llm = ScriptedLLM([json.dumps({"message": text}) for text in ("first answer", "second answer")])
    agent = make_agent(config, backend, llm)
    agent.run("q1")
    agent.run("q2")
    msgs = llm.calls[1]["messages"]
    assert any(m["role"] == "assistant" and json.loads(m["content"])["message"] == "first answer" for m in msgs)
    agent.reset()
    assert agent.history == []
