"""The tool protocol exposed to the language model.

Every capability of the agent is a *tool* with a name, a description and a
JSON-schema for its arguments.  The same definitions are used for

* the native OpenAI ``tools`` / ``tool_calls`` protocol, and
* the fallback "JSON in the assistant message" protocol for models without
  function calling (see :mod:`winagent.protocol`).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from ..config import COORDINATE_SPACES


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    dangerous: bool = False          # requires confirmation when confirm_dangerous_actions is on
    screenshot_after: bool = True    # attach a fresh screenshot to the result
    category: str = "general"
    tags: tuple[str, ...] = field(default_factory=tuple)

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


_XY = {
    "x": {"type": "integer", "description": "X coordinate in pixels of the last FULL screenshot supplied before this response, not a crop or GUI preview."},
    "y": {"type": "integer", "description": "Y coordinate in pixels of the last FULL screenshot supplied before this response, not a crop or GUI preview."},
}

TOOLS: list[ToolSpec] = [
    # ------------------------------------------------------------- perception
    ToolSpec(
        name="screenshot",
        description=(
            "Capture the screen and look at it. Returns an image (when vision is enabled) plus "
            "information about the active window. Call this before interacting with the UI, and again "
            "after actions when you need to verify the result. Use `region` to zoom into a part of the screen "
            "for small text."
        ),
        parameters=_obj({
            "region": {
                "type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4,
                "description": "Optional [left, top, right, bottom] in last FULL screenshot coordinates to capture a zoomed region.",
            },
            "all_screens": {"type": "boolean", "description": "Capture all monitors instead of the primary one."},
            "grid": {"type": "boolean", "description": "Overlay a labelled coordinate grid (default from settings)."},
        }),
        screenshot_after=False, category="perception",
    ),
    ToolSpec(
        name="get_screen_info",
        description="Return screen size, DPI scaling, mouse position, active window and the list of open windows (no image).",
        parameters=_obj({}), screenshot_after=False, category="perception",
    ),
    # ------------------------------------------------------------------ mouse
    ToolSpec(
        name="mouse_move",
        description="Move the mouse pointer to (x, y) without clicking (useful for hover menus/tooltips).",
        parameters=_obj(_XY, ["x", "y"]), category="mouse",
    ),
    ToolSpec(
        name="click",
        description=(
            "Click at (x, y) in screenshot coordinates. If x/y are omitted, click at the current pointer position. "
            "Use button='right' for context menus, clicks=2 for double-click. Hold modifier keys with `modifiers`."
        ),
        parameters=_obj({
            **_XY,
            "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Mouse button (default left)."},
            "clicks": {"type": "integer", "minimum": 1, "maximum": 3, "description": "1=single, 2=double, 3=triple."},
            "modifiers": {"type": "array", "items": {"type": "string"}, "description": "Keys held during the click, e.g. ['ctrl'] or ['shift']."},
        }), category="mouse",
    ),
    ToolSpec(
        name="double_click",
        description="Double-click with the left button at (x, y). Shortcut for click with clicks=2.",
        parameters=_obj(_XY, ["x", "y"]), category="mouse",
    ),
    ToolSpec(
        name="right_click",
        description="Right-click at (x, y) to open a context menu.",
        parameters=_obj(_XY, ["x", "y"]), category="mouse",
    ),
    ToolSpec(
        name="drag",
        description="Press the mouse button at (x1, y1), move to (x2, y2) and release. Use for selecting text, moving windows, drag-and-drop, drawing.",
        parameters=_obj({
            "x1": {"type": "integer"}, "y1": {"type": "integer"}, "x2": {"type": "integer"}, "y2": {"type": "integer"},
            "button": {"type": "string", "enum": ["left", "right", "middle"]},
            "duration": {"type": "number", "description": "Seconds for the drag motion (default 0.5)."},
        }, ["x1", "y1", "x2", "y2"]), category="mouse",
    ),
    ToolSpec(
        name="scroll",
        description=(
            "Scroll the mouse wheel at (x, y) (or at the current pointer position). "
            "`amount` is in wheel clicks: positive = scroll up / left, negative = scroll down / right. "
            "Typical values: -5 to scroll down a bit, 10 to scroll up a page."
        ),
        parameters=_obj({
            **_XY,
            "amount": {"type": "integer", "description": "Number of wheel clicks. Negative scrolls down."},
            "direction": {"type": "string", "enum": ["vertical", "horizontal"], "description": "Default vertical."},
        }, ["amount"]), category="mouse",
    ),
    # --------------------------------------------------------------- keyboard
    ToolSpec(
        name="type_text",
        description=(
            "Type text into the focused control exactly as given (supports Unicode/Persian/emoji, '\\n' presses Enter, "
            "'\\t' presses Tab). Click on the target field first. Set press_enter=true to hit Enter afterwards."
        ),
        parameters=_obj({
            "text": {"type": "string", "description": "The text to type."},
            "press_enter": {"type": "boolean", "description": "Press Enter after typing."},
            "interval": {"type": "number", "description": "Delay between characters in seconds (default 0 = fast)."},
        }, ["text"]), category="keyboard",
    ),
    ToolSpec(
        name="press_keys",
        description=(
            "Press a key or key combination, e.g. 'enter', 'ctrl+c', 'alt+tab', 'win+r', 'ctrl+shift+esc', 'f5', "
            "'down', 'pagedown'. Keys in a combination are held together. Use `repeat` to press it several times "
            "(e.g. 'tab' x3)."
        ),
        parameters=_obj({
            "keys": {"type": "string", "description": "Key or combination joined with '+', e.g. 'ctrl+s'."},
            "repeat": {"type": "integer", "minimum": 1, "maximum": 50, "description": "How many times to press (default 1)."},
        }, ["keys"]), category="keyboard",
    ),
    ToolSpec(
        name="hotkey_sequence",
        description="Press several key combinations one after another, e.g. menu navigation ['alt', 'f', 's']. Never use it to launch programs (win+r / Start search) – call open_app instead.",
        parameters=_obj({
            "sequence": {"type": "array", "items": {"type": "string"}, "description": "List of key combinations pressed in order."},
            "delay": {"type": "number", "description": "Seconds between combinations (default 0.3)."},
        }, ["sequence"]), category="keyboard",
    ),
    # --------------------------------------------------------------- programs
    ToolSpec(
        name="open_app",
        description=(
            "Launch a program (the ONLY reliable way – do not use the Start menu or win+r for this). Accepts a friendly "
            "name ('paint', 'calculator', 'settings', 'ماشین حساب'), an executable name (mspaint.exe, notepad.exe, calc.exe, "
            "msedge.exe, winword.exe), a full path, a document/folder path (opened with its default program), a Start-menu "
            "or Store app name, or a URI (ms-settings:display, https://...). Resolution uses an alias table, the App Paths "
            "registry, PATH, Start-menu shortcuts and Store apps; several launch methods are tried. The result tells you the "
            "resolved target, whether a new window appeared and the active window title – verify it is the intended program. "
            "On failure the error explains why (not installed, blocked by policy/antivirus, needs admin) – do not retry blindly."
        ),
        parameters=_obj({
            "name": {"type": "string", "description": "Program name, executable, path, Start-menu entry, file/folder path or URI."},
            "args": {"type": "string", "description": "Optional command line arguments, e.g. a file to open or '--incognito'."},
            "wait": {"type": "number", "description": "Max seconds to wait for the window to appear (default 3; returns earlier once it shows up)."},
        }, ["name"]), category="programs",
    ),
    ToolSpec(
        name="open_url",
        description="Open a URL in the default web browser.",
        parameters=_obj({"url": {"type": "string"}}, ["url"]), category="programs",
    ),
    ToolSpec(
        name="run_command",
        description=(
            "Run a shell command (PowerShell by default, or cmd) and return stdout/stderr/exit code. Ideal for file "
            "operations, system queries, starting processes, or anything faster than clicking through a UI. "
            "Do not run interactive programs. Commands that delete data or change system settings need user confirmation."
        ),
        parameters=_obj({
            "command": {"type": "string"},
            "shell": {"type": "string", "enum": ["powershell", "cmd"], "description": "Default powershell."},
            "timeout": {"type": "number", "description": "Seconds before the command is killed (default 60)."},
            "cwd": {"type": "string", "description": "Working directory."},
        }, ["command"]), dangerous=True, screenshot_after=False, category="programs",
    ),
    # ---------------------------------------------------------------- windows
    ToolSpec(
        name="list_windows",
        description="List open top-level windows with handle, title, process and position.",
        parameters=_obj({}), screenshot_after=False, category="windows",
    ),
    ToolSpec(
        name="focus_window",
        description="Bring a window to the foreground, by title substring or handle.",
        parameters=_obj({
            "title": {"type": "string", "description": "Case-insensitive substring of the window title (or process name)."},
            "hwnd": {"type": "integer", "description": "Window handle from list_windows."},
        }), category="windows",
    ),
    ToolSpec(
        name="window_action",
        description="Minimize, maximize, restore, close, move or resize a window (by title substring or handle).",
        parameters=_obj({
            "action": {"type": "string", "enum": ["minimize", "maximize", "restore", "close", "move", "resize", "move_resize"]},
            "title": {"type": "string"},
            "hwnd": {"type": "integer"},
            "x": {"type": "integer", "description": "New left position in PHYSICAL pixels (move)."},
            "y": {"type": "integer", "description": "New top position in PHYSICAL pixels (move)."},
            "width": {"type": "integer", "description": "New width in PHYSICAL pixels (resize)."},
            "height": {"type": "integer", "description": "New height in PHYSICAL pixels (resize)."},
        }, ["action"]), category="windows",
    ),
    ToolSpec(
        name="get_window_controls",
        description="List child controls (buttons, edits, ...) of a window with their text and screenshot coordinates. Works for classic Win32 apps; modern apps may expose nothing.",
        parameters=_obj({"title": {"type": "string"}, "hwnd": {"type": "integer"}, "limit": {"type": "integer"}}),
        screenshot_after=False, category="windows",
    ),
    # -------------------------------------------------------------- clipboard
    ToolSpec(
        name="clipboard",
        description="Read or write the clipboard. Writing then pressing ctrl+v is the most reliable way to enter long or non-Latin text.",
        parameters=_obj({
            "action": {"type": "string", "enum": ["get", "set"]},
            "text": {"type": "string", "description": "Text to place on the clipboard (for set)."},
        }, ["action"]), screenshot_after=False, category="clipboard",
    ),
    # ------------------------------------------------------------------ files
    ToolSpec(
        name="read_file",
        description="Read a text file (up to max_chars) so you can analyse or quote its content.",
        parameters=_obj({
            "path": {"type": "string"},
            "max_chars": {"type": "integer", "description": "Default 20000."},
            "encoding": {"type": "string", "description": "Default utf-8."},
        }, ["path"]), screenshot_after=False, category="files",
    ),
    ToolSpec(
        name="write_file",
        description="Write (or append) text to a file, creating parent folders. Overwriting an existing file needs confirmation.",
        parameters=_obj({
            "path": {"type": "string"},
            "content": {"type": "string"},
            "append": {"type": "boolean"},
        }, ["path", "content"]), dangerous=True, screenshot_after=False, category="files",
    ),
    ToolSpec(
        name="list_directory",
        description="List files and folders of a directory (name, size, modified time).",
        parameters=_obj({"path": {"type": "string"}, "limit": {"type": "integer"}}, ["path"]),
        screenshot_after=False, category="files",
    ),
    # ------------------------------------------------------------------- flow
    ToolSpec(
        name="wait",
        description="Pause for a number of seconds (e.g. while an application loads), then take a screenshot.",
        parameters=_obj({"seconds": {"type": "number", "minimum": 0, "maximum": 60}}, ["seconds"]), category="flow",
    ),
    ToolSpec(
        name="ask_user",
        description=(
            "Pause and ask the user a question when you need information (a password, a choice, a file name) or "
            "permission for something risky. The task continues with the user's answer."
        ),
        parameters=_obj({
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}, "description": "Optional suggested answers."},
        }, ["question"]), screenshot_after=False, category="flow",
    ),
    ToolSpec(
        name="task_complete",
        description=(
            "Call this when the user's request has been fully accomplished (or cannot be done). Provide a concise "
            "summary of what was done and the final result. After this call, no more actions are executed."
        ),
        parameters=_obj({
            "summary": {"type": "string", "description": "What was done and the result, in the user's language."},
            "success": {"type": "boolean", "description": "False if the task could not be completed."},
        }, ["summary"]), screenshot_after=False, category="flow",
    ),
]

TOOLS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOLS}


_POINTER_TOOLS = {"mouse_move", "click", "double_click", "right_click", "drag", "scroll"}


def openai_tool_schemas(coordinate_space: str = "image_pixels") -> list[dict[str, Any]]:
    """Return isolated schemas. Coordinate-mode changes must not mutate the shared tool registry."""
    if coordinate_space not in COORDINATE_SPACES:
        raise ValueError(f"Unknown coordinate space: {coordinate_space}")
    schemas = [deepcopy(t.to_openai()) for t in TOOLS]
    if coordinate_space == "normalized_1000":
        for entry in schemas:
            fn = entry["function"]
            props = fn["parameters"].get("properties", {})
            if fn["name"] in _POINTER_TOOLS:
                fn["description"] += (" Positions use normalized 0-1000 units on BOTH axes of the last FULL image "
                                      "provided before this response, NOT image pixels. Centre=(500,500).")
                for key in ("x", "y", "x1", "y1", "x2", "y2"):
                    if key in props:
                        props[key] = {"type": "integer", "minimum": 0, "maximum": 1000,
                                      "description": f"{key}: normalized 0-1000, NOT pixels; 0 is left/top, 1000 is the far edge."}
            elif fn["name"] == "screenshot":
                props["region"]["items"] = {"type": "integer", "minimum": 0, "maximum": 1000}
                props["region"]["description"] = "[left, top, right, bottom] in normalized 0-1000 units of the last FULL image."
                fn["description"] += " The region uses normalized 0-1000 units on both axes, not image pixels."
            elif fn["name"] == "window_action":
                fn["description"] += " Window positioning/sizing remains in physical desktop pixels, NOT normalized units."
    return schemas


def tools_markdown(coordinate_space: str = "image_pixels") -> str:
    """Human/LLM description using the SAME coordinate contract as the native function schemas."""
    lines: list[str] = []
    for entry in openai_tool_schemas(coordinate_space):
        fn = entry["function"]
        props = fn["parameters"].get("properties", {})
        required = set(fn["parameters"].get("required", []))
        arg_desc = []
        for name, schema in props.items():
            typ = schema.get("type", "any")
            if "enum" in schema:
                typ = "|".join(map(str, schema["enum"]))
            if "minimum" in schema and "maximum" in schema:
                typ += f"[{schema['minimum']}..{schema['maximum']}]"
            flag = "" if name in required else "?"
            arg_desc.append(f"{name}{flag}: {typ}")
        lines.append(f"- **{fn['name']}**({', '.join(arg_desc)}) — {fn['description']}")
    return "\n".join(lines)
