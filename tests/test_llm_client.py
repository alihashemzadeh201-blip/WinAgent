"""LLMClient error handling: deterministic provider rejections must fail fast, transient ones retry."""

import json

import pytest

from winagent.config import Config
from winagent.llm import LLMClient, LLMError


class FakeResponse:
    def __init__(self, status: int, body: str = "", headers: dict | None = None):
        self.status_code = status
        self.text = body
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


def make_client(monkeypatch, responses: list, max_retries: int = 3) -> tuple[LLMClient, list]:
    config = Config(api_base_url="http://fake.local/v1", api_key="k", model="m",
                    max_retries=max_retries)
    client = LLMClient(config)
    calls: list = []

    def fake_post(url, headers=None, data=None, timeout=None):
        calls.append(json.loads(data))
        return responses[min(len(calls) - 1, len(responses) - 1)]

    monkeypatch.setattr(client.session, "post", fake_post)
    monkeypatch.setattr("winagent.llm.time.sleep", lambda _s: None)
    return client, calls


def test_503_invalid_argument_fails_fast(monkeypatch):
    """Antigravity wraps deterministic 400-class rejections in a 503 INVALID_ARGUMENT."""
    body = json.dumps({"error": {"code": 503, "status": "INVALID_ARGUMENT",
                                 "message": "Requests ending with a model turn are not supported."}})
    client, calls = make_client(monkeypatch, [FakeResponse(503, body)])
    with pytest.raises(LLMError) as ei:
        client.chat([{"role": "user", "content": "hi"}])
    assert "model turn" in str(ei.value)
    assert len(calls) == 1  # no wasted 30-second retries


def test_503_location_policy_fails_fast(monkeypatch):
    body = json.dumps({"error": {"code": 503, "status": "FAILED_PRECONDITION",
                                 "message": "User location is not supported for the API use."}})
    client, calls = make_client(monkeypatch, [FakeResponse(503, body)])
    with pytest.raises(LLMError) as ei:
        client.chat([{"role": "user", "content": "hi"}])
    assert "location" in str(ei.value)
    assert len(calls) == 1


def test_503_transient_still_retries(monkeypatch):
    body = json.dumps({"error": {"code": 503, "message": "upstream overloaded, please retry"}})
    client, calls = make_client(monkeypatch, [FakeResponse(503, body)] * 4)
    with pytest.raises(LLMError) as ei:
        client.chat([{"role": "user", "content": "hi"}])
    assert "overloaded" in str(ei.value)
    assert len(calls) == 4  # full retry budget for a genuinely transient error


def test_context_overflow_400_is_friendly_and_fails_fast(monkeypatch):
    """Ollama's exact phrasing must become an actionable message, not four wasted retries."""
    body = json.dumps({"error": "request (6722 tokens) exceeds the available context size (4096 tokens)"})
    client, calls = make_client(monkeypatch, [FakeResponse(400, body)])
    with pytest.raises(LLMError) as ei:
        client.chat([{"role": "user", "content": "hi"}])
    msg = str(ei.value)
    assert "too long for this model's context window" in msg
    assert "new chat" in msg
    assert "6722 tokens" in msg
    assert len(calls) == 1


def test_openai_style_context_overflow_400(monkeypatch):
    body = json.dumps({"error": {"message":
        "This model's maximum context length is 4096 tokens. However, your messages resulted in 5120 tokens."}})
    client, calls = make_client(monkeypatch, [FakeResponse(400, body)])
    with pytest.raises(LLMError) as ei:
        client.chat([{"role": "user", "content": "hi"}])
    assert "context window" in str(ei.value)
    assert len(calls) == 1


def test_ollama_image_500_downgrades_vision(monkeypatch):
    body = json.dumps({"error": "image input is not supported - could not find mmproj file"})
    client, calls = make_client(monkeypatch, [FakeResponse(500, body)])
    with pytest.raises(LLMError) as ei:
        client.chat([{"role": "user", "content": "hi"}])
    assert client.supports_vision is False
    assert "does not accept images" in str(ei.value)
    assert len(calls) == 1


def test_auth_and_endpoint_errors_still_fail_fast(monkeypatch):
    client, calls = make_client(monkeypatch, [FakeResponse(401, json.dumps({"error": "bad key"}))])
    with pytest.raises(LLMError) as ei:
        client.chat([{"role": "user", "content": "hi"}])
    assert "Authentication failed" in str(ei.value)
    assert len(calls) == 1
