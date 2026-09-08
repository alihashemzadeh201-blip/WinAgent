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

import ast
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any


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
    # Repair only tokens OUTSIDE strings. A trailing comma elsewhere must not rewrite typed text
    # such as "True, } None" or curly quotes inside a filename/document.
    tokens = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|,(?=\s*[}\]])|\b(?:True|False|None)\b|[\u201c\u201d]''')

    def repair(match):
        token = match.group(0)
        return {",": "", "True": "true", "False": "false", "None": "null", "\u201c": '"', "\u201d": '"'}.get(token, token)

    fixed = tokens.sub(repair, blob)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass
    if "'" in blob:
        try:
            value = ast.literal_eval(blob)  # parse literals only; never eval() model output
            json.dumps(value, allow_nan=False)  # disallow non-JSON Python objects (sets, bytes, ...)
            return value
        except (ValueError, SyntaxError, TypeError):
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
        raise ProtocolError("Tool arguments are not a complete JSON object.")
    raise ProtocolError(f"Unsupported argument type {type(raw).__name__}")


def response_text(content: Any) -> str:
    """Read text content without coercing malformed objects into plausible assistant answers."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                raise ProtocolError("Invalid assistant content part: expected text.")
            texts.append(part["text"])
        return "".join(texts)
    raise ProtocolError("Invalid assistant content: expected a string or text parts.")


def _normalise_action(item: Any) -> ToolCall:
    """Normalise common tool-call shapes, but NEVER invent arguments or skip a broken action."""
    from .tools.definitions import TOOLS_BY_NAME  # lazy: the executor also imports this module

    if not isinstance(item, dict):
        raise ProtocolError("Each action must be an object.")
    name = item.get("tool") or item.get("name") or item.get("action") or item.get("function") or item.get("tool_name")
    if isinstance(name, dict):
        inner = name
        name = inner.get("name")
        args = inner.get("arguments", inner.get("args", {}))
    else:
        arg_key = next((k for k in ("args", "arguments", "parameters", "input", "params") if k in item), None)
        args = item[arg_key] if arg_key else {
            k: v for k, v in item.items()
            if k not in ("tool", "name", "action", "function", "tool_name", "id", "thought", "reasoning", "message")
        }
    if not isinstance(name, str) or not name.strip():
        raise ProtocolError("A tool call is missing its name.")
    name = name.strip()
    if isinstance(args, str):
        if not args.strip():
            raise ProtocolError(f"Arguments for {name} are empty; use an explicit JSON object.")
        args = parse_json_arguments(args)
    if not isinstance(args, dict):
        raise ProtocolError(f"Arguments for {name} must be a JSON object.")
    spec = TOOLS_BY_NAME.get(name)
    if spec:
        missing = [k for k in spec.parameters.get("required", []) if k not in args]
        if missing:
            raise ProtocolError(f"Missing required arguments for {name}: {', '.join(missing)}.")
    if name in ("click", "scroll") and ((args.get("x") is None) != (args.get("y") is None)):
        raise ProtocolError(f"{name} needs both x and y, or neither for the current pointer position.")
    call_id = item.get("id")
    if call_id is not None and (not isinstance(call_id, str) or not call_id.strip()):
        raise ProtocolError("A tool-call id must be a non-empty string.")
    tc = ToolCall(name=name, arguments=args)
    if call_id:
        tc.id = call_id
    return tc


_COORDINATE_TAIL = r"y\s+from\s+0\s*\(top\)\s+to\s+\d+\s*\(bottom\)"


def _is_screenshot_echo(text: str) -> bool:
    """Recognise standalone capture metadata, not ordinary answers discussing screen coordinates."""
    text = text.strip().strip("`").strip()
    if re.fullmatch(r"(?:document\s*,\s*)?" + _COORDINATE_TAIL + r"[.\]\s]*", text, re.I):
        return True
    return bool(re.match(r"\[?(?:Screenshot \d+[x×]\d+ px|Current screen attached\.|"
                         r"Screenshot after the actions above\.|Coordinates you send must be)", text, re.I)
                and re.search(_COORDINATE_TAIL + r"[.\]\s]*$", text, re.I))


def _bad_turn(content: str, error: str) -> AssistantTurn:
    # Reject the whole batch: the caller must not execute a valid prefix and then retry it.
    return AssistantTurn(raw_content=content, parse_error=error)


def _field_text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise ProtocolError(f"{key} must be text.")
        if value and value.strip():
            return value.strip()
    return ""


def parse_json_protocol(content: str, *, allow_plain_text: bool = False) -> AssistantTurn:
    """Parse JSON-in-text. Plain prose is allowed only for the native protocol's fallback parser."""
    from .tools.definitions import TOOLS_BY_NAME

    if not isinstance(content, str) or not content.strip():
        return _bad_turn(content if isinstance(content, str) else "", "Empty or non-text model response.")
    if _is_screenshot_echo(content):
        return _bad_turn(content, "The response only echoes screenshot coordinate metadata, not an answer or action.")
    for blob in _candidate_json_blobs(content):
        try:
            data = _loads_lenient(blob)
        except ProtocolError:
            continue
        if isinstance(data, list):
            if allow_plain_text and data and not any(
                isinstance(item, dict) and any(k in item for k in ("tool", "name", "action", "function")) for item in data
            ):
                continue  # a native answer may be an ordinary JSON data array
            data = {"actions": data}
        if not isinstance(data, dict):
            continue
        single = any(k in data for k in ("tool", "action", "function", "tool_name")) or (
            "name" in data and ((isinstance(data["name"], str) and data["name"] in TOOLS_BY_NAME)
                               or any(k in data for k in ("args", "arguments", "parameters"))))
        if not single and not any(k in data for k in ("actions", "tool_calls", "message", "final_answer", "response",
                                                      "thought", "reasoning", "done")):
            continue
        try:
            turn = AssistantTurn(raw_content=content, text=_field_text(data, "message", "final_answer", "response"),
                                 thought=_field_text(data, "thought", "reasoning"))
            if single and "actions" not in data and "tool_calls" not in data:
                turn.tool_calls = [_normalise_action(data)]
            else:
                actions = data.get("actions", data.get("tool_calls", []))
                if not isinstance(actions, list):
                    raise ProtocolError("actions/tool_calls must be an array.")
                if data.get("actions") and data.get("tool_calls"):
                    raise ProtocolError("Use one action list, not both actions and tool_calls.")
                turn.tool_calls = [_normalise_action(item) for item in actions]
            ids = [tc.id for tc in turn.tool_calls]
            if len(ids) != len(set(ids)):
                raise ProtocolError("Duplicate tool-call ids.")
            if not turn.tool_calls and _is_screenshot_echo(turn.text):
                raise ProtocolError("The response only echoes screenshot coordinate metadata.")
            if data.get("done") is True and not turn.tool_calls and turn.text:
                turn.tool_calls.append(ToolCall("task_complete", {"summary": turn.text, "success": True}))
            if not turn.tool_calls and not turn.text:
                raise ProtocolError("The response contains no actions or user-facing answer (reasoning alone is incomplete).")
            return turn
        except ProtocolError as exc:
            return _bad_turn(content, str(exc))
    # Don't disguise truncated JSON/function calls as a successful plain-text answer.
    looks_structured = bool(re.match(r"\s*(?:\{|\[\s*(?:\{|\[|\"|\d|\]))", content)
                            or re.search(r'```\s*json\b|["\'](?:actions|tool_calls)["\']\s*:', content, re.I))
    if allow_plain_text and not looks_structured:
        return AssistantTurn(text=content.strip(), raw_content=content)
    # Native models may legitimately answer a data question with a complete, non-protocol JSON document.
    if allow_plain_text:
        try:
            data = json.loads(content)
        except (ValueError, TypeError):
            pass
        else:
            if data != {}:
                return AssistantTurn(text=content.strip(), raw_content=content)
    return _bad_turn(content, "Expected a complete JSON object with actions or a non-empty message.")


def parse_native_response(message: dict[str, Any]) -> AssistantTurn:
    """Parse OpenAI content/tool calls atomically; malformed arguments never turn into an empty click."""
    content = ""
    try:
        if not isinstance(message, dict):
            raise ProtocolError("The assistant message must be an object.")
        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            return AssistantTurn(text=refusal.strip())
        content = response_text(message.get("content"))
        thought = next((message[k].strip() for k in ("reasoning_content", "reasoning")
                        if isinstance(message.get(k), str) and message[k].strip()), "")
        calls = message.get("tool_calls")
        if calls is None:
            calls = []
        if not isinstance(calls, list):
            raise ProtocolError("tool_calls must be an array.")
        if not calls and message.get("function_call") is not None:
            calls = [{"function": message["function_call"]}]
        parsed_calls = []
        for c in calls:
            if not isinstance(c, dict):
                raise ProtocolError("Each tool call must be an object.")
            fn = c.get("function", c)
            if not isinstance(fn, dict):
                raise ProtocolError("A tool-call function must be an object.")
            raw_args = fn.get("arguments", {})
            tc = _normalise_action({"name": fn.get("name"), "args": raw_args, "id": c.get("id")})
            tc.raw_arguments = raw_args if isinstance(raw_args, str) else json.dumps(raw_args)
            parsed_calls.append(tc)
        ids = [tc.id for tc in parsed_calls]
        if len(ids) != len(set(ids)):
            raise ProtocolError("Duplicate tool-call ids.")
        if parsed_calls:
            return AssistantTurn(text=content.strip(), thought=thought, tool_calls=parsed_calls, raw_content=content)
        parsed = parse_json_protocol(content, allow_plain_text=True)
        parsed.thought = parsed.thought or thought
        return parsed
    except ProtocolError as exc:
        return _bad_turn(content, str(exc))


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
