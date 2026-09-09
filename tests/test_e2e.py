"""End-to-end: real HTTP client + mock OpenAI server + fake desktop."""

import json

import pytest

from tests.mock_server import start_server
from winagent.agent import Agent, AgentEvents
from winagent.backends.fake import FakeBackend
from winagent.config import Config
from winagent.llm import LLMClient, LLMError


@pytest.fixture(params=[True, False], ids=["native-tools", "json-fallback"])
def server(request):
    srv, url = start_server(native_tools=request.param)
    yield srv, url, request.param
    srv.shutdown()


def make(url: str, **overrides):
    cfg = Config(api_base_url=url, api_key="test-key", model="mock-vision", action_delay=0.0, screenshot_max_width=640, **overrides)
    backend = FakeBackend()
    return cfg, backend


def test_notepad_task_over_http(server):
    srv, url, native = server
    cfg, backend = make(url)
    texts = []
    agent = Agent(cfg, backend, llm=LLMClient(cfg), events=AgentEvents(on_assistant_text=texts.append))
    outcome = agent.run("open notepad and write hello")
    assert outcome.status == "completed", outcome.message
    assert "Notepad" in outcome.message or "نوشته" in outcome.message
    assert "Hello from WinAgent" in "".join(backend.typed)
    assert agent.protocol == ("native" if native else "json")
    # requests carried images (vision) in both protocols
    reqs = srv.RequestHandlerClass.requests_log
    assert any(isinstance(m.get("content"), list) for r in reqs for m in r["messages"])
    assert outcome.usage["prompt_tokens"] > 0


def test_screenshot_task_reports_image(server):
    srv, url, native = server
    cfg, backend = make(url)
    agent = Agent(cfg, backend, llm=LLMClient(cfg))
    outcome = agent.run("take a screenshot and describe it")
    assert outcome.status == "completed"
    assert "image received" in outcome.message


def test_confirmation_flow_over_http(server):
    srv, url, native = server
    cfg, backend = make(url)
    decisions = []
    agent = Agent(cfg, backend, llm=LLMClient(cfg), events=AgentEvents(confirm=lambda c, r: decisions.append(r) or False))
    outcome = agent.run("delete the old log")
    assert outcome.status == "completed"
    assert decisions and "Remove-Item" in decisions[0]
    assert backend.commands == []


def test_ask_user_flow_over_http(server):
    srv, url, native = server
    cfg, backend = make(url)
    agent = Agent(cfg, backend, llm=LLMClient(cfg), events=AgentEvents(ask_user=lambda q, o: "Documents"))
    outcome = agent.run("ask me something")
    assert outcome.status == "completed"
    reqs = srv.RequestHandlerClass.requests_log
    last = reqs[-1]["messages"]
    assert "Documents" in json.dumps(last, ensure_ascii=False)


def test_wrong_api_key_gives_clear_error():
    srv, url = start_server()
    try:
        cfg = Config(api_base_url=url, api_key="", model="mock")
        with pytest.raises(LLMError) as exc:
            LLMClient(cfg).test_connection()
        assert exc.value.status == 401
        assert "API key" in str(exc.value)
    finally:
        srv.shutdown()


def test_list_models_and_test_connection():
    srv, url = start_server()
    try:
        cfg = Config(api_base_url=url, api_key="k", model="mock")
        client = LLMClient(cfg)
        assert client.list_models() == ["mock-text", "mock-vision"]
        assert client.test_connection().startswith("OK")
    finally:
        srv.shutdown()


def test_connection_refused_is_reported_quickly():
    cfg = Config(api_base_url="http://127.0.0.1:9/v1", api_key="k", model="m", max_retries=0)
    with pytest.raises(LLMError) as exc:
        LLMClient(cfg).chat([{"role": "user", "content": "hi"}])
    assert "Cannot connect" in str(exc.value)


def test_arbitrary_fragments_do_not_end_tool_work_over_http(server, monkeypatch):
    from tests import mock_server

    srv, url, native = server
    replies = [
        (None, [("type_text", {"text": "written once"})]),
        ("b sideways.", []),
        ('{"message":"b sideways."}', []),
        (None, [("task_complete", {"summary": "Recovered safely.", "success": True})]),
    ]
    monkeypatch.setattr(mock_server, "plan", lambda messages: replies.pop(0))
    cfg, backend = make(url)
    texts = []
    agent = Agent(cfg, backend, llm=LLMClient(cfg), events=AgentEvents(on_assistant_text=texts.append))
    result = agent.run("write the text exactly once")
    assert result.status == "completed" and result.message == "Recovered safely."
    assert result.steps == 2 and backend.typed == ["written once"]
    assert texts == ["Recovered safely."]
    assert len(srv.RequestHandlerClass.requests_log) == (4 if native else 5)  # optional native->JSON negotiation
    assert "b sideways." not in str(agent.history)
