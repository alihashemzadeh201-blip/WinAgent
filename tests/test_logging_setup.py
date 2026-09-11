"""Tests for central logging: rotating file log, redaction, per-task JSONL traces, log bundle."""

from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path

import pytest

import winagent.logging_setup as ls
from winagent.agent import Agent
from winagent.logbundle import make_log_bundle
from winagent.llm import ChatResponse, LLMError

SECRET = "sk-test-secret-0123456789"


# ------------------------------------------------------------------- fixtures
@pytest.fixture
def logdir(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    monkeypatch.setenv("WINAGENT_LOGS_DIR", str(d))
    return d


@pytest.fixture
def restore_root_logging():
    """setup_logging mutates the root logger; keep it hermetic across tests."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_marker = getattr(root, ls._SETUP_MARKER, False)
    yield
    for h in list(root.handlers):
        if h not in saved_handlers:
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
    root.handlers = saved_handlers
    root.setLevel(saved_level)
    if saved_marker:
        setattr(root, ls._SETUP_MARKER, True)
    else:
        root.__dict__.pop(ls._SETUP_MARKER, None)


class MiniLLM:
    """Scripted stand-in for LLMClient: one successful task_complete."""

    supports_tools = None
    supports_vision = None
    total_prompt_tokens = 0
    total_completion_tokens = 0

    def cancel(self):
        pass

    def reset_cancel(self):
        pass

    def chat(self, messages, tools=None, **kw):
        msg = {"role": "assistant", "content": None,
               "tool_calls": [{"id": "c1", "type": "function",
                               "function": {"name": "task_complete",
                                            "arguments": '{"success": true, "summary": "done"}'}}]}
        return ChatResponse(message=msg, finish_reason="tool_calls",
                            usage={"prompt_tokens": 7, "completion_tokens": 3},
                            model="fake-model", raw={"id": "chatcmpl-x"}, latency=0.05)


# ----------------------------------------------------------------- setup/log
def test_setup_logging_rotating_and_idempotent(logdir, restore_root_logging):
    d = ls.setup_logging("INFO", console=False)
    assert d == logdir
    root = logging.getLogger()
    rotating = [h for h in root.handlers if isinstance(h, ls.RotatingFileHandler)]
    assert len(rotating) == 1
    assert rotating[0].maxBytes == ls.MAX_BYTES
    assert rotating[0].backupCount == ls.BACKUP_COUNT
    # idempotent: a second call must not stack another handler
    ls.setup_logging("DEBUG", console=False)
    assert len([h for h in root.handlers if isinstance(h, ls.RotatingFileHandler)]) == 1
    # a message actually lands in the file
    logging.getLogger("winagent.test").info("hello-rotation")
    for h in root.handlers:
        h.flush()
    log_file = logdir / ls.LOG_BASENAME
    assert log_file.exists()
    assert "hello-rotation" in log_file.read_text(encoding="utf-8")


# ----------------------------------------------------------------- redaction
def test_redact_masks_credentials_but_not_token_usage():
    out = ls.redact({"api_key": "abc", "model": "m",
                     "extra_headers": {"Authorization": "Bearer abc"},
                     "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                     "nested": ["abc", {"client_secret": "x", "note": "keep me abc"}]}, ["abc"])
    assert out["api_key"] == "<redacted>"
    assert out["extra_headers"]["Authorization"] == "<redacted>"
    assert out["usage"] == {"prompt_tokens": 7, "completion_tokens": 3}   # NOT redacted
    assert out["nested"][0] == "<redacted>"
    assert out["nested"][1]["client_secret"] == "<redacted>"
    assert out["nested"][1]["note"] == "keep me <redacted>"


def test_redact_empty_secret_list_is_harmless():
    data = {"api_key": "abc", "prompt_tokens": 1}
    assert ls.redact(data) == {"api_key": "<redacted>", "prompt_tokens": 1}


# ---------------------------------------------------------------- sanitising
def test_sanitize_messages_strips_base64_images():
    b64 = "A" * 5000
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}]
    out = ls.sanitize_messages(msgs)
    url = out[0]["content"][1]["image_url"]["url"]
    assert b64[:100] not in url and "omitted" in url
    assert out[0]["content"][0] == {"type": "text", "text": "hi"}
    # original untouched (no in-place mutation)
    assert msgs[0]["content"][1]["image_url"]["url"].endswith(b64[-10:])


def test_sanitize_messages_clips_long_text():
    out = ls.sanitize_messages([{"role": "system", "content": "x" * (ls.MAX_TEXT_CHARS + 500)}])
    assert len(out[0]["content"]) < ls.MAX_TEXT_CHARS + 100
    assert "truncated" in out[0]["content"]


# ----------------------------------------------------------------- trace file
def test_trace_writer_jsonl_redacts_and_caps_size(logdir):
    path = logdir / "sessions" / "t.jsonl"
    w = ls.LLMTraceWriter(path, secrets=[SECRET], max_file_bytes=120)
    w.record(type="a", value=SECRET)
    w.record(type="b", value=2)
    w.record(type="c", value="y" * 50)
    w.record(type="d", value=4)
    w.close()
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["type"] == "a" and lines[0]["value"] == "<redacted>"
    assert "ts" in lines[0]
    assert SECRET not in path.read_text(encoding="utf-8")
    assert w.exceeded is True                       # cap kicked in
    assert all(l["type"] != "d" for l in lines)     # later records dropped


# ----------------------------------------------------------------- agent run
def test_agent_writes_full_session_trace(config, backend, logdir):
    config.api_key = SECRET
    agent = Agent(config, backend, llm=MiniLLM())
    outcome = agent.run("Rename the Cube object to Hero")
    assert outcome.status == "completed"
    assert outcome.trace_file
    files = list((logdir / "sessions").glob("*.jsonl"))
    assert len(files) == 1
    lines = [json.loads(l) for l in files[0].read_text(encoding="utf-8").splitlines()]
    types = [l["type"] for l in lines]
    assert types[0] == "session_start" and types[-1] == "session_end"
    assert "llm_request" in types and "llm_response" in types and "tool_results" in types
    start = lines[0]
    assert start["task"].startswith("Rename the Cube")
    assert start["config"]["api_key"] == "<redacted>"
    req = next(l for l in lines if l["type"] == "llm_request")
    assert req["seq"] == 1 and req["retry"] == 0 and req["temperature"] is None
    resp = next(l for l in lines if l["type"] == "llm_response")
    assert resp["ok"] is True and resp["usage"]["prompt_tokens"] == 7
    tools = next(r for r in next(l for l in lines if l["type"] == "tool_results")["results"])
    assert tools["tool"] == "task_complete" and tools["ok"] is True
    assert lines[-1]["status"] == "completed"
    text = files[0].read_text(encoding="utf-8")
    assert SECRET not in text                        # no credential leak
    assert "iVBOR" not in text                       # no PNG payload leaked


def test_agent_trace_disabled(config, backend, logdir):
    config.log_llm_trace = False
    agent = Agent(config, backend, llm=MiniLLM())
    outcome = agent.run("do something")
    assert outcome.status == "completed"
    assert outcome.trace_file is None
    assert not (logdir / "sessions").exists() or not list((logdir / "sessions").glob("*.jsonl"))


def test_agent_trace_captures_llm_error(config, backend, logdir):
    class BoomLLM(MiniLLM):
        def chat(self, *a, **k):
            raise LLMError("provider exploded", status=500, body="trace id 42")

    agent = Agent(config, backend, llm=BoomLLM())
    outcome = agent.run("do something")
    assert outcome.status == "error"
    lines = [json.loads(l) for l in Path(outcome.trace_file).read_text(encoding="utf-8").splitlines()]
    err = next(l for l in lines if l["type"] == "llm_response" and not l["ok"])
    assert err["status"] == 500 and err["body"] == "trace id 42"
    assert "provider exploded" in err["error"]
    assert lines[-1]["type"] == "session_end" and lines[-1]["status"] == "error"


def test_agent_trace_retries_are_separate_records(config, backend, logdir):
    """One invalid (prose) response + one good one:
    raw HTTP response stays ok=True, the rejection gets its own record, retry rises 0 -> 1."""
    class TwoShotLLM(MiniLLM):
        def __init__(self):
            self.n = 0

        def chat(self, messages, tools=None, **kw):
            self.n += 1
            if self.n == 1:
                return ChatResponse(message={"role": "assistant", "content": "no tools, just prose"},
                                    finish_reason="stop", usage={}, model="fake-model", raw={},
                                    latency=0.01)
            return super().chat(messages, tools=tools, **kw)

    agent = Agent(config, backend, llm=TwoShotLLM())
    outcome = agent.run("do something")
    assert outcome.status == "completed"
    lines = [json.loads(l) for l in Path(outcome.trace_file).read_text(encoding="utf-8").splitlines()]
    rejs = [l for l in lines if l["type"] == "llm_rejected"]
    assert len(rejs) == 1 and rejs[0]["retry"] == 0
    assert rejs[0]["reason"]                       # carries the parse/protocol error text
    resps = [l for l in lines if l["type"] == "llm_response"]
    assert [r["retry"] for r in resps] == [0, 1]   # raw round-trips, both HTTP-ok
    assert all(r["ok"] for r in resps)
    # the second request (same seq) carries the appended repair message
    reqs = [l for l in lines if l["type"] == "llm_request"]
    assert len(reqs) == 2 and reqs[1]["retry"] == 1
    assert len(reqs[1]["messages"]) == len(reqs[0]["messages"]) + 1
    assert reqs[1]["messages"][-1]["role"] == "user"


# --------------------------------------------------------------------- bundle
def test_log_bundle_contains_logs_and_hides_secrets(logdir, config):
    logdir.mkdir(parents=True, exist_ok=True)
    (logdir / "winagent.log").write_text("app log line\n", encoding="utf-8")
    sess = logdir / "sessions"
    sess.mkdir()
    (sess / "20260911-000000-task-a.jsonl").write_text('{"type": "session_start"}\n', encoding="utf-8")
    (sess / "20260911-000001-task-b.jsonl").write_text('{"type": "session_start"}\n', encoding="utf-8")
    config.api_key = SECRET
    bundle = make_log_bundle(config=config)
    assert bundle.exists() and bundle.name.startswith("WinAgent-log-bundle-")
    zf = zipfile.ZipFile(bundle)
    names = zf.namelist()
    assert "meta.json" in names
    assert "logs/winagent.log" in names
    assert "logs/20260911-000000-task-a.jsonl" in names
    meta = json.loads(zf.read("meta.json"))
    assert meta["version"]
    assert meta["config"]["api_key"] == "<set>"
    alltext = "".join(zf.read(n).decode("utf-8", "replace") for n in names)
    assert SECRET not in alltext
