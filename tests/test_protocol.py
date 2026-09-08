import json

import pytest

from winagent.keys import hotkey_to_vk, normalize_key, parse_key_combo
from winagent.protocol import (
    ProtocolError,
    ToolCall,
    assistant_message_json,
    parse_json_arguments,
    parse_json_protocol,
    parse_native_response,
    tool_results_message_json,
)


def test_parse_native_tool_calls():
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "abc", "type": "function", "function": {"name": "click", "arguments": '{"x": 10, "y": 20}'}},
            {"id": "def", "type": "function", "function": {"name": "type_text", "arguments": {"text": "سلام"}}},
        ],
    }
    turn = parse_native_response(msg)
    assert [c.name for c in turn.tool_calls] == ["click", "type_text"]
    assert turn.tool_calls[0].arguments == {"x": 10, "y": 20}
    assert turn.tool_calls[0].id == "abc"
    assert turn.tool_calls[1].arguments == {"text": "سلام"}


def test_parse_native_plain_text():
    turn = parse_native_response({"role": "assistant", "content": "Hello there"})
    assert turn.text == "Hello there"
    assert not turn.has_tool_calls


def test_parse_native_falls_back_to_json_in_content():
    content = '{"thought": "open it", "actions": [{"tool": "open_app", "args": {"name": "notepad"}}]}'
    turn = parse_native_response({"role": "assistant", "content": content})
    assert turn.has_tool_calls
    assert turn.tool_calls[0].name == "open_app"
    assert turn.thought == "open it"


def test_parse_json_protocol_with_fence_and_trailing_comma():
    content = 'Sure!\n```json\n{"thought": "t", "actions": [{"tool": "click", "args": {"x": 1, "y": 2,}},]}\n```'
    turn = parse_json_protocol(content)
    assert turn.tool_calls[0].name == "click"
    assert turn.tool_calls[0].arguments == {"x": 1, "y": 2}


def test_parse_json_protocol_message_only():
    turn = parse_json_protocol('{"message": "کار انجام شد"}')
    assert turn.text == "کار انجام شد"
    assert not turn.has_tool_calls


def test_parse_json_protocol_single_action_and_inline_args():
    turn = parse_json_protocol('{"tool": "press_keys", "keys": "ctrl+s"}')
    assert turn.tool_calls[0].name == "press_keys"
    assert turn.tool_calls[0].arguments == {"keys": "ctrl+s"}


def test_parse_json_protocol_list_and_name_arguments_shapes():
    turn = parse_json_protocol('[{"name": "wait", "arguments": "{\\"seconds\\": 2}"}]')
    assert turn.tool_calls[0].name == "wait"
    assert turn.tool_calls[0].arguments == {"seconds": 2}


def test_parse_json_protocol_plain_text_passthrough():
    turn = parse_json_protocol("I cannot do that, sorry.")
    assert turn.text == "I cannot do that, sorry."
    assert not turn.has_tool_calls


def test_parse_json_protocol_done_flag_becomes_task_complete():
    turn = parse_json_protocol('{"message": "finished", "done": true}')
    assert turn.tool_calls[0].name == "task_complete"
    assert turn.tool_calls[0].arguments["summary"] == "finished"


def test_parse_json_arguments_variants():
    assert parse_json_arguments("") == {}
    assert parse_json_arguments(None) == {}
    assert parse_json_arguments({"a": 1}) == {"a": 1}
    assert parse_json_arguments('"{\\"a\\": 1}"') == {"a": 1}
    with pytest.raises(ProtocolError):
        parse_json_arguments("not json at all")


def test_json_history_roundtrip():
    turn = parse_json_protocol('{"thought": "x", "actions": [{"tool": "click", "args": {"x": 5, "y": 6}}]}')
    msg = assistant_message_json(turn)
    data = json.loads(msg["content"])
    assert data["actions"][0] == {"tool": "click", "args": {"x": 5, "y": 6}}
    results = tool_results_message_json([(turn.tool_calls[0], '{"ok": true}')])
    assert results["role"] == "user"
    assert json.loads(results["content"])["tool_results"][0]["result"] == {"ok": True}


def test_toolcall_to_openai_serialises_unicode():
    tc = ToolCall("type_text", {"text": "سلام دنیا"}, id="c1")
    out = tc.to_openai()
    assert out["id"] == "c1"
    assert json.loads(out["function"]["arguments"]) == {"text": "سلام دنیا"}


# ---------------------------------------------------------------- key names
@pytest.mark.parametrize("raw,expected", [
    ("Control", "ctrl"), ("CTRL", "ctrl"), ("Return", "enter"), ("Escape", "esc"), ("ArrowDown", "down"),
    ("Windows", "win"), ("super", "win"), ("Cmd", "win"), ("PageDown", "pagedown"), ("Del", "delete"),
    ("F5", "f5"), ("a", "a"), ("A", "a"), ("+", "+"), ("space", "space"), (" ", "space"), ("Print Screen", "printscreen"),
])
def test_normalize_key(raw, expected):
    assert normalize_key(raw) == expected


def test_parse_key_combo_variants():
    assert parse_key_combo("ctrl+shift+s") == ["ctrl", "shift", "s"]
    assert parse_key_combo("Ctrl + C") == ["ctrl", "c"]
    assert parse_key_combo(["Shift", "Tab"]) == ["shift", "tab"]
    assert parse_key_combo("alt-f4") == ["alt", "f4"]
    assert parse_key_combo("ctrl++") == ["ctrl", "+"]
    assert parse_key_combo("enter") == ["enter"]
    assert parse_key_combo("s+ctrl") == ["ctrl", "s"]  # modifiers are moved first
    with pytest.raises(ValueError):
        parse_key_combo("ctrl+notakey")


def test_hotkey_to_vk():
    mods, vk = hotkey_to_vk("ctrl+alt+esc")
    assert mods == 0x0002 | 0x0001
    assert vk == 0x1B
    with pytest.raises(ValueError):
        hotkey_to_vk("ctrl+alt")
