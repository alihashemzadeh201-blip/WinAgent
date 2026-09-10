import json

from winagent.protocol import ToolCall
from winagent.tools import ToolExecutor
from winagent.tools.definitions import TOOLS, TOOLS_BY_NAME, openai_tool_schemas, tools_markdown


def make_executor(backend, config, confirm=None):
    return ToolExecutor(backend, config, confirm=confirm)


def test_tool_schemas_are_valid_openai_shape():
    schemas = openai_tool_schemas()
    assert len(schemas) == len(TOOLS)
    for s in schemas:
        assert s["type"] == "function"
        fn = s["function"]
        assert fn["name"] and fn["description"]
        assert fn["parameters"]["type"] == "object"
        json.dumps(s)  # serialisable
    md = tools_markdown()
    for t in TOOLS:
        assert t.name in md
    assert len(set(t.name for t in TOOLS)) == len(TOOLS)


def test_executor_has_handler_for_every_tool(backend, config):
    ex = make_executor(backend, config)
    for name in TOOLS_BY_NAME:
        assert hasattr(ex, f"_t_{name}"), name


def test_screenshot_scaling_and_coordinate_mapping(backend, config):
    config.screenshot_format = "jpeg"  # explicit format: the mapping test must not depend on auto-detection
    ex = make_executor(backend, config)
    res = ex.execute(ToolCall("screenshot", {}))
    assert res.ok
    shot = res.screenshot
    assert shot.size[0] == 800  # scaled from 1600
    assert abs(shot.scale - 0.5) < 1e-6
    assert shot.to_physical(400, 225) == (800, 450)
    assert shot.to_image(800, 450) == (400, 225)
    assert "800x450" in shot.describe()
    assert shot.data_url().startswith("data:image/jpeg;base64,")


def test_click_converts_screenshot_coordinates(backend, config):
    ex = make_executor(backend, config)
    ex.execute(ToolCall("screenshot", {}))
    res = ex.execute(ToolCall("click", {"x": 100, "y": 50, "button": "right"}))
    assert res.ok, res.error
    ev = [e for e in backend.events if e["kind"] == "click"][-1]
    assert (ev["x"], ev["y"]) == (200, 100)
    assert ev["button"] == "right"
    assert res.screenshot is not None  # auto screenshot after action


def test_click_out_of_bounds_is_rejected(backend, config):
    ex = make_executor(backend, config)
    ex.execute(ToolCall("screenshot", {}))
    res = ex.execute(ToolCall("click", {"x": 5000, "y": 10}))
    assert not res.ok
    assert "outside" in res.error


def test_double_and_right_click_aliases(backend, config):
    ex = make_executor(backend, config)
    ex.execute(ToolCall("screenshot", {}))
    assert ex.execute(ToolCall("double_click", {"x": 10, "y": 10})).ok
    assert backend.events[-1]["clicks"] == 2 or [e for e in backend.events if e["kind"] == "click"][-1]["clicks"] == 2
    assert ex.execute(ToolCall("right_click", {"x": 10, "y": 10})).ok
    assert [e for e in backend.events if e["kind"] == "click"][-1]["button"] == "right"


def test_type_text_and_keys(backend, config):
    ex = make_executor(backend, config)
    res = ex.execute(ToolCall("type_text", {"text": "سلام دنیا", "press_enter": True}))
    assert res.ok
    assert "سلام دنیا" in "".join(backend.typed)
    assert backend.pressed[-1] == ["enter"]
    res = ex.execute(ToolCall("press_keys", {"keys": "Ctrl+Shift+S", "repeat": 2}))
    assert res.ok
    assert backend.pressed[-1] == ["ctrl", "shift", "s"]
    assert backend.pressed[-2] == ["ctrl", "shift", "s"]
    res = ex.execute(ToolCall("press_keys", {"keys": "ctrl+notakey"}))
    assert not res.ok and "Invalid arguments" in res.error
    res = ex.execute(ToolCall("hotkey_sequence", {"sequence": ["alt", "f", "s"], "delay": 0}))
    assert res.ok


def test_tool_name_aliases(backend, config):
    ex = make_executor(backend, config)
    res = ex.execute(ToolCall("take_screenshot", {}))
    assert res.ok and res.call.name == "screenshot"
    res = ex.execute(ToolCall("hotkey", {"keys": "alt+tab"}))
    assert res.ok and res.call.name == "press_keys"
    res = ex.execute(ToolCall("no_such_tool", {}))
    assert not res.ok and "Unknown tool" in res.error


def test_open_app_and_windows(backend, config):
    ex = make_executor(backend, config)
    res = ex.execute(ToolCall("open_app", {"name": "notepad", "wait": 0}))
    assert res.ok
    assert "Notepad" in res.data["active_window"]["title"]
    res = ex.execute(ToolCall("list_windows", {}))
    assert res.ok and res.data["count"] >= 1
    hwnd = res.data["windows"][0]["hwnd"]
    res = ex.execute(ToolCall("window_action", {"action": "maximize", "hwnd": hwnd}))
    assert res.ok
    res = ex.execute(ToolCall("focus_window", {"title": "notepad"}))
    assert res.ok
    res = ex.execute(ToolCall("focus_window", {"title": "does-not-exist"}))
    assert not res.ok and "No window matching" in res.error
    res = ex.execute(ToolCall("get_window_controls", {"title": "notepad"}))
    assert res.ok and res.data["count"] == 2
    res = ex.execute(ToolCall("window_action", {"action": "close", "title": "notepad"}))
    assert res.ok


def test_dangerous_command_requires_confirmation(backend, config):
    answers = []

    def confirm(call, reason):
        answers.append(reason)
        return False

    ex = make_executor(backend, config, confirm=confirm)
    res = ex.execute(ToolCall("run_command", {"command": "Remove-Item C:\\temp\\x -Recurse"}))
    assert not res.ok and res.denied
    assert answers and "Remove-Item" in answers[0]
    # safe command runs without confirmation
    res = ex.execute(ToolCall("run_command", {"command": "Get-Date"}))
    assert res.ok and "simulated" in res.data["stdout"]
    assert len(answers) == 1
    # confirmation disabled -> runs directly
    config.confirm_dangerous_actions = False
    res = ex.execute(ToolCall("run_command", {"command": "del C:\\x"}))
    assert res.ok


def test_shell_commands_can_be_disabled(backend, config):
    config.allow_shell_commands = False
    ex = make_executor(backend, config)
    res = ex.execute(ToolCall("run_command", {"command": "Get-Date"}))
    assert not res.ok and "disabled" in res.error


def test_file_tools(tmp_path, backend, config):
    ex = make_executor(backend, config, confirm=lambda c, r: True)
    target = tmp_path / "sub" / "note.txt"
    res = ex.execute(ToolCall("write_file", {"path": str(target), "content": "hello\nسلام"}))
    assert res.ok, res.error
    res = ex.execute(ToolCall("read_file", {"path": str(target)}))
    assert res.ok and res.data["content"] == "hello\nسلام"
    res = ex.execute(ToolCall("write_file", {"path": str(target), "content": "!", "append": True}))
    assert res.ok
    assert target.read_text(encoding="utf-8") == "hello\nسلام!"
    res = ex.execute(ToolCall("list_directory", {"path": str(tmp_path)}))
    assert res.ok and res.data["entries"][0]["name"] == "sub"
    res = ex.execute(ToolCall("read_file", {"path": str(tmp_path / "missing.txt")}))
    assert not res.ok and "not found" in res.error


def test_write_file_overwrite_denied(tmp_path, backend, config):
    ex = make_executor(backend, config, confirm=lambda c, r: False)
    target = tmp_path / "a.txt"
    target.write_text("orig")
    res = ex.execute(ToolCall("write_file", {"path": str(target), "content": "new"}))
    assert not res.ok and res.denied
    assert target.read_text() == "orig"


def test_clipboard_wait_ask_user_task_complete(backend, config):
    ex = make_executor(backend, config)
    assert ex.execute(ToolCall("clipboard", {"action": "set", "text": "abc"})).ok
    res = ex.execute(ToolCall("clipboard", {"action": "get"}))
    assert res.data["text"] == "abc"
    res = ex.execute(ToolCall("wait", {"seconds": 0.01}))
    assert res.ok
    res = ex.execute(ToolCall("ask_user", {"question": "Which file?", "options": ["a", "b"]}))
    assert res.ok and res.ask_user["question"] == "Which file?" and res.screenshot is None
    res = ex.execute(ToolCall("task_complete", {"summary": "done", "success": "false"}))
    assert res.task_complete and res.data["success"] is False


def test_scroll_and_drag(backend, config):
    ex = make_executor(backend, config)
    ex.execute(ToolCall("screenshot", {}))
    res = ex.execute(ToolCall("scroll", {"x": 100, "y": 100, "amount": -5}))
    assert res.ok and backend.events[-1]["dy"] == -5 or [e for e in backend.events if e["kind"] == "scroll"][-1]["dy"] == -5
    res = ex.execute(ToolCall("drag", {"x1": 10, "y1": 10, "x2": 100, "y2": 100}))
    assert res.ok
    ev = [e for e in backend.events if e["kind"] == "drag"][-1]
    assert (ev["x2"], ev["y2"]) == (200, 200)


def test_get_screen_info(backend, config):
    ex = make_executor(backend, config)
    res = ex.execute(ToolCall("get_screen_info", {}))
    assert res.ok
    assert res.data["system"]["primary_screen"]["width"] == 1600
    text = res.to_text()
    json.loads(text)


def test_region_screenshot_keeps_global_frame(backend, config):
    ex = make_executor(backend, config)
    ex.execute(ToolCall("screenshot", {}))
    full = ex.last_screenshot
    res = ex.execute(ToolCall("screenshot", {"region": [100, 100, 300, 200]}))
    assert res.ok and "ZOOMED" in res.data["note"]
    assert ex.last_screenshot is full
    assert res.screenshot.raw_size == (400, 200)
