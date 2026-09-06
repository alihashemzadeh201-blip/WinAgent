"""Agent <-> LLM message protocol.

Two wire formats are supported on top of the OpenAI-compatible
``/chat/completions`` endpoint:

``native``
    Standard function calling: tool schemas go in the ``tools`` field, the
    model replies with ``tool_calls`` and we answer with ``role: tool``
    messages.

``json``
    For models/back-ends without function calling.  The tools are described in
    the system prompt and the model replies with a JSON object::

        {"thought": "...", "actions": [{"tool": "click", "args": {"x": 10, "y": 20}}]}

    or with ``{"message": "..."}`` for a plain answer.  Tool results are sent
    back as a ``user`` message containing a JSON document (plus the screenshot
    image when vision is enabled).

Both formats produce the same internal :class:`ToolCall` objects.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from .tools.definitions import TOOLS_BY_NAME


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = field(default_factory=lambda: f"call_{uuid.uuid4().hex[:12]}")
    raw_arguments: str = ""  # the original argument string (native protocol)

    def to_openai(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": json.dumps(self.arguments, ensure_ascii=False)},
        }


@dataclass
class AssistantTurn:
    """Parsed model output: free text and/or tool calls."""

    text: str = ""
    thought: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_content: str = ""
    finish_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    parse_error: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class ProtocolError(ValueError):
    pass


# --------------------------------------------------------------------------
# JSON extraction helpers
# --------------------------------------------------------------------------
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def _candidate_json_blobs(text: str) -> list[str]:
    """Return possible JSON documents contained in ``text`` (best first)."""
    text = text.strip()
    if not text:
        return []
    cands: list[str] = []
    for m in _FENCE_RE.finditer(text):
        cands.append(m.group(1).strip())
    cands.append(text)
    # balanced-brace scan for the first top-level object
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    cands.append(text[start:i + 1])
                    start = -1
    # de-duplicate, keep order
    seen: set[str] = set()
    out = []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _loads_lenient(blob: str) -> Any:
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        pass
    # common LLM slips: trailing commas, single quotes around keys, python literals
    fixed = re.sub(r",\s*([}\]])", r"\1", blob)
    fixed = fixed.replace("\u201c", '"').replace("\u201d", '"')
    fixed = re.sub(r"\bTrue\b", "true", fixed)
    fixed = re.sub(r"\bFalse\b", "false", fixed)
    fixed = re.sub(r"\bNone\b", "null", fixed)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass
    # last resort: json5-like single quotes
    if "'" in fixed and '"' not in fixed:
        try:
            return json.loads(fixed.replace("'", '"'))
        except json.JSONDecodeError:
            pass
    raise ProtocolError("not JSON")


def parse_json_arguments(raw: Any) -> dict[str, Any]:
    """Parse function-call arguments that may be a dict, a JSON string, or garbage."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        for blob in _candidate_json_blobs(raw):
            try:
                val = _loads_lenient(blob)
            except ProtocolError:
                continue
            if isinstance(val, dict):
                return val
            if isinstance(val, str):
                # double encoded JSON
                try:
                    inner = json.loads(val)
                    if isinstance(inner, dict):
                        return inner
                except json.JSONDecodeError:
                    pass
        raise ProtocolError(f"Could not parse tool arguments: {raw[:200]!r}")
    raise ProtocolError(f"Unsupported argument type {type(raw).__name__}")


def _normalise_action(item: Any) -> Optional[ToolCall]:
    """Turn one of the many shapes LLMs produce into a ToolCall."""
    if not isinstance(item, dict):
        return None
    name = item.get("tool") or item.get("name") or item.get("action") or item.get("function") or item.get("tool_name")
    if isinstance(name, dict):  # {"function": {"name":..., "arguments":...}}
        inner = name
        name = inner.get("name")
        args = inner.get("arguments", inner.get("args", {}))
    else:
        args = item.get("args", item.get("arguments", item.get("parameters", item.get("input", item.get("params")))))
    if not isinstance(name, str) or not name:
        return None
    if args is None:
        # arguments may be inline: {"tool": "click", "x": 1, "y": 2}
        args = {k: v for k, v in item.items() if k not in ("tool", "name", "action", "function", "tool_name", "id", "thought", "reasoning")}
    if isinstance(args, str):
        try:
            args = parse_json_arguments(args)
        except ProtocolError:
            args = {"value": args}
    if not isinstance(args, dict):
        args = {"value": args}
    call_id = item.get("id") if isinstance(item.get("id"), str) else None
    tc = ToolCall(name=name.strip(), arguments=args)
    if call_id:
        tc.id = call_id
    return tc


def parse_json_protocol(content: str) -> AssistantTurn:
    """Parse the fallback JSON protocol out of a plain assistant message."""
    turn = AssistantTurn(raw_content=content or "")
    if not content or not content.strip():
        return turn
    for blob in _candidate_json_blobs(content):
        try:
            data = _loads_lenient(blob)
        except ProtocolError:
            continue
        if isinstance(data, list):
            data = {"actions": data}
        if not isinstance(data, dict):
            continue
        # single action object at top level
        if any(k in data for k in ("tool", "name", "action", "function")) and "actions" not in data:
            single = _normalise_action(data)
            if single and single.name in TOOLS_BY_NAME:
                turn.tool_calls = [single]
                turn.thought = str(data.get("thought") or data.get("reasoning") or "")
                turn.text = str(data.get("message") or "")
                return turn
        if "actions" in data or "tool_calls" in data or "message" in data or "final_answer" in data:
            turn.thought = str(data.get("thought") or data.get("reasoning") or "")
            turn.text = str(data.get("message") or data.get("final_answer") or data.get("response") or "")
            for item in data.get("actions") or data.get("tool_calls") or []:
                tc = _normalise_action(item)
                if tc:
                    turn.tool_calls.append(tc)
            if data.get("done") is True and not turn.tool_calls and turn.text:
                turn.tool_calls.append(ToolCall("task_complete", {"summary": turn.text, "success": True}))
            return turn
    # not JSON -> plain text answer
    turn.text = content.strip()
    return turn


def parse_native_response(message: dict[str, Any]) -> AssistantTurn:
    """Parse an OpenAI ``message`` object that may contain ``tool_calls``."""
    content = message.get("content")
    if isinstance(content, list):  # some servers return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    content = content or ""
    turn = AssistantTurn(raw_content=content)
    reasoning = message.get("reasoning_content") or message.get("reasoning")
    if isinstance(reasoning, str):
        turn.thought = reasoning.strip()
    calls = message.get("tool_calls") or []
    if not calls and message.get("function_call"):  # legacy single function call
        fc = message["function_call"]
        calls = [{"id": None, "function": fc}]
    for c in calls:
        fn = c.get("function") or {}
        name = fn.get("name") or c.get("name")
        if not name:
            continue
        raw_args = fn.get("arguments", c.get("arguments", "{}"))
        try:
            args = parse_json_arguments(raw_args)
        except ProtocolError as exc:
            turn.parse_error = str(exc)
            args = {}
        tc = ToolCall(name=name, arguments=args, raw_arguments=raw_args if isinstance(raw_args, str) else json.dumps(raw_args))
        if c.get("id"):
            tc.id = c["id"]
        turn.tool_calls.append(tc)
    if turn.tool_calls:
        turn.text = content.strip()
        return turn
    # Some models with native support still answer with the JSON protocol in text.
    parsed = parse_json_protocol(content)
    if parsed.tool_calls:
        parsed.thought = parsed.thought or turn.thought
        return parsed
    turn.text = content.strip()
    return turn


# --------------------------------------------------------------------------
# Building messages
# --------------------------------------------------------------------------
def image_part(data_url: str, detail: str = "auto") -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": data_url, "detail": detail}}


def text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def tool_result_message_native(call: ToolCall, result_text: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": result_text}


def assistant_message_native(turn: AssistantTurn) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": turn.text or ""}
    if turn.tool_calls:
        msg["tool_calls"] = [tc.to_openai() for tc in turn.tool_calls]
    return msg


def assistant_message_json(turn: AssistantTurn) -> dict[str, Any]:
    """Re-serialise a parsed turn in the canonical JSON protocol (keeps history tidy)."""
    if turn.tool_calls:
        payload = {
            "thought": turn.thought,
            "actions": [{"tool": tc.name, "args": tc.arguments} for tc in turn.tool_calls],
        }
        if turn.text:
            payload["message"] = turn.text
        return {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)}
    return {"role": "assistant", "content": turn.text or turn.raw_content}


def tool_results_message_json(results: list[tuple[ToolCall, str]]) -> dict[str, Any]:
    payload = {
        "tool_results": [
            {"tool": call.name, "id": call.id, "result": _maybe_json(text)} for call, text in results
        ]
    }
    return {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}


def _maybe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
