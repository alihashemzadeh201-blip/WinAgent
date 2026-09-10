"""Menu navigation is keyboard-only and survives submenus (the fake backend simulates the
open popup stack, type-ahead highlighting, Right = open submenu, Enter = select, Esc = close)."""

import pytest

from winagent.protocol import ToolCall
from winagent.tools import ToolExecutor
from winagent.tools import executor as executor_module


def make(backend, config, **args):
    return ToolExecutor(backend, config).execute(ToolCall("menu", args))


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    """The type-ahead / popup-wait sleeps are real time in production; skip them in tests."""
    monkeypatch.setattr(executor_module.time, "sleep", lambda *_a, **_k: None)


@pytest.fixture
def app(backend):
    # an app window has an enumerable menu bar; the bare desktop does not
    backend.open_application("notepad.exe")
    return backend


def test_multi_level_path_selects_submenu_item(app, config):
    result = make(app, config, action="select", path=["File", "Export", "Export as PDF…"])
    assert result.ok, result.error
    assert app.menu_selections == ["Export as PDF…"]
    assert "right (open submenu)" in result.data["steps"]
    assert "enter" in result.data["steps"]
    assert app.fake_menu_stack == []  # the menu closed after the selection


def test_menu_navigation_never_moves_the_mouse(app, config):
    make(app, config, action="select", path=["File", "Export", "Export as Image…"])
    make(app, config, action="select", path=["File", "Save"])
    mouse_events = [e for e in app.events if e["kind"] in ("move", "click", "drag", "down", "up")]
    assert mouse_events == []


def test_top_level_leaf_selection(app, config):
    result = make(app, config, action="select", path=["File", "Save"])
    assert result.ok, result.error
    assert app.menu_selections == ["Save"]


def test_three_level_path(app, config):
    result = make(app, config, action="select", path=["View", "Toolbars", "Show Toolbar"])
    assert result.ok, result.error
    assert app.menu_selections == ["Show Toolbar"]
    # one Right arrow: for the intermediate level (Toolbars); the last level is confirmed with Enter
    assert result.data["steps"].count("right (open submenu)") == 1


def test_select_item_in_already_open_popup(app, config):
    app.fake_menu_stack = [[{"text": "Cut", "mnemonic": "t"}, {"text": "Copy", "mnemonic": "c"},
                            {"text": "Paste", "mnemonic": "p"}]]
    app.fake_menu_typed = [""]
    result = make(app, config, action="select", item="Copy")
    assert result.ok, result.error
    assert app.menu_selections == ["Copy"]


def test_close_presses_esc(app, config):
    app.fake_menu_stack = [[{"text": "New", "mnemonic": "n"}]]
    app.fake_menu_typed = [""]
    result = make(app, config, action="close")
    assert result.ok
    assert app.fake_menu_stack == []
    assert app.pressed[-1] == ["esc"]


def test_missing_item_lists_what_is_available(app, config):
    result = make(app, config, action="select", path=["File", "Delete…"])
    assert not result.ok
    assert "not found" in result.error
    assert "save" in result.error.lower()
    assert app.menu_selections == []


def test_path_cannot_continue_beyond_leaf(app, config):
    result = make(app, config, action="select", path=["File", "Save", "Something"])
    assert not result.ok
    assert "no submenu" in result.error
    assert app.menu_selections == []


def test_top_item_without_submenu_rejected_early(app, config):
    result = make(app, config, action="select", path=["Help", "About", "X"])
    assert not result.ok
    assert "About" in result.error and "no submenu" in result.error


def test_no_menu_bar_window_errors(app, config, backend):
    backend._add_window("Desktop2", "Progman-fake", 0, 0, 100, 100, "explorer.exe")
    result = make(backend, config, action="select", path=["File", "Save"])
    assert not result.ok
    assert "no enumerable menu bar" in result.error


def test_select_without_open_menu_errors(app, config):
    result = make(app, config, action="select", item="Open")
    assert not result.ok
    assert "No open menu/popup" in result.error


def test_list_shows_structure_and_open_popup(app, config):
    result = make(app, config, action="list")
    assert result.ok
    names = [m["text"] for m in result.data["menu_bar"]]
    assert names == ["File", "Edit", "View", "Help"]
    app.fake_menu_stack = [[{"text": "Copy", "mnemonic": "c"}]]
    app.fake_menu_typed = [""]
    result = make(app, config, action="list")
    assert [m["text"] for m in result.data["open_popup"]] == ["Copy"]


def test_wrong_layout_is_fixed_and_reported(app, config):
    app.fake_keyboard_layout = "fa-IR"
    result = make(app, config, action="select", path=["File", "Save"])
    assert result.ok, result.error
    assert app.layout_changes == ["en-US"]
    assert "fa-IR" in result.data["layout"] and "en-US" in result.data["layout"]


def test_correct_layout_adds_no_note(app, config):
    result = make(app, config, action="select", path=["File", "Save"])
    assert result.ok
    assert result.data.get("layout") in (None, "")
    assert app.layout_changes == []


def test_pointer_input_blocked_while_menu_open(app, config):
    # a menu is open; every pointer tool must be refused (and nothing must reach the desktop)
    app.fake_menu_stack = [[{"text": "New", "mnemonic": "n"}, {"text": "Save", "mnemonic": "s"}]]
    app.fake_menu_typed = [""]
    ex = ToolExecutor(app, config)
    res = ex.execute(ToolCall("click", {"x": 100, "y": 200}))
    assert not res.ok
    assert "menu is currently open" in res.error
    assert "New" in res.error and "Save" in res.error          # the visible items are offered
    assert "`menu`" in res.error                                 # the keyboard route is shown
    for name, args in (("mouse_move", {"x": 100, "y": 200}), ("scroll", {"amount": -3}),
                       ("right_click", {"x": 100, "y": 200}),
                       ("drag", {"x1": 10, "y1": 10, "x2": 50, "y2": 50})):
        r2 = ex.execute(ToolCall(name, args))
        assert not r2.ok and "menu is currently open" in r2.error
    mouse_events = [e for e in app.events if e["kind"] in ("move", "click", "drag", "down", "up", "scroll")]
    assert mouse_events == []


def test_keyboard_selection_unblocks_pointer(app, config):
    # the menu tool (keyboard) works while the menu is open and unblocks the mouse afterwards
    app.fake_menu_stack = [[{"text": "New", "mnemonic": "n"}, {"text": "Save", "mnemonic": "s"}]]
    app.fake_menu_typed = [""]
    ex = ToolExecutor(app, config)
    res = ex.execute(ToolCall("menu", {"action": "select", "item": "Save"}))
    assert res.ok, res.error
    assert app.menu_selections == ["Save"]
    assert app.fake_menu_stack == []
    res_click = ex.execute(ToolCall("click", {"x": 300, "y": 400}))
    assert res_click.ok, res_click.error
    assert any(e["kind"] == "click" for e in app.events)


def test_closed_menu_via_esc_unblocks_pointer(app, config):
    app.fake_menu_stack = [[{"text": "New", "mnemonic": "n"}]]
    app.fake_menu_typed = [""]
    ex = ToolExecutor(app, config)
    res = ex.execute(ToolCall("menu", {"action": "close"}))
    assert res.ok
    res_click = ex.execute(ToolCall("click", {"x": 300, "y": 400}))
    assert res_click.ok, res_click.error
