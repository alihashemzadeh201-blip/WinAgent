"""Gemini-style normalized positions are an explicit, end-to-end contract, never a name/value heuristic."""

from copy import deepcopy
import json

import pytest
from PIL import Image

from tests.conftest import ScriptedLLM, native_tool_message
from winagent.agent import Agent, AgentEvents
from winagent.backends import FakeBackend, ScreenGeometry
from winagent.config import Config
from winagent.prompts import build_system_prompt
from winagent.protocol import ToolCall, parse_native_response
from winagent.screenshot import draw_grid, prepare_screenshot
from winagent.tools import ToolExecutor
from winagent.tools.definitions import TOOLS_BY_NAME, openai_tool_schemas, tools_markdown

NORM = "normalized_1000"
PIXELS = "image_pixels"


def turn(protocol, *calls):
    return native_tool_message(*calls) if protocol == "native" else json.dumps({"actions": [{"tool": n, "args": a} for n, a in calls]})


@pytest.mark.parametrize("max_width", [640, 1280, None])
@pytest.mark.parametrize("origin", [(0, 0), (-1920, -1080)])
def test_normalized_center_is_independent_of_image_resize_and_honors_origin(max_width, origin):
    shot = prepare_screenshot(Image.new("RGB", (1920, 1080)), max_width=max_width, grid=False,
                              geometry=ScreenGeometry(*origin, 1920, 1080), coordinate_space=NORM)
    expected = (origin[0] + 960, origin[1] + 540)
    assert shot.model_to_physical(500, 500) == expected
    assert shot.to_model(*expected) == (500, 500)
    assert shot.model_limits == (1000, 1000)
    assert "normalized_1000, NOT image pixels" in shot.describe()
    assert parse_native_response({"content": shot.describe()}).parse_error  # metadata echo still isn't an answer


def test_pixel_and_normalized_values_are_not_interchangeable():
    raw = Image.new("RGB", (1920, 1080))
    pixels = prepare_screenshot(raw, grid=False)
    normalized = prepare_screenshot(raw, grid=False, coordinate_space=NORM)
    assert pixels.model_to_physical(500, 500) == (750, 750)
    assert normalized.model_to_physical(500, 500) == (960, 540)
    assert pixels.model_limits == (1279, 719)


@pytest.mark.parametrize("point,expected", [((0, 0), (0, 0)), ((999, 999), (1918, 1078)), ((1000, 1000), (1919, 1079))])
def test_normalized_uses_denominator_1000_and_keeps_pointer_on_screen(point, expected):
    shot = prepare_screenshot(Image.new("RGB", (1920, 1080)), coordinate_space=NORM, grid=False)
    assert shot.model_to_physical(*point) == expected
    assert shot.model_to_physical(1000, 1000, edge=True) == (1920, 1080)


@pytest.mark.parametrize("point", [(-1, 500), (1001, 500), (500, -1), (500, 1001)])
def test_normalized_out_of_range_is_rejected_not_clamped(point):
    cfg = Config(coordinate_space=NORM, action_delay=0)
    backend = FakeBackend(width=1920, height=1080)
    executor = ToolExecutor(backend, cfg)
    executor.take_screenshot()
    result = executor.execute(ToolCall("click", {"x": point[0], "y": point[1]}))
    assert not result.ok and "normalized_1000 bounds" in result.error
    assert not any(e["kind"] == "click" for e in backend.events)


def test_normalized_grid_positions_and_labels_use_both_axis_extents(monkeypatch):
    import winagent.screenshot as module
    labels = []
    draw_factory = module.ImageDraw.Draw
    def recording_draw(*args, **kwargs):
        draw = draw_factory(*args, **kwargs)
        original = draw.text
        def text(xy, value, **kw):
            labels.append((xy, value))
            return original(xy, value, **kw)
        draw.text = text
        return draw
    monkeypatch.setattr(module.ImageDraw, "Draw", recording_draw)
    image = draw_grid(Image.new("RGB", (1200, 600)), spacing=100, coordinate_space=NORM)
    assert len(labels) == 18
    assert ((604, 2), "500") in labels  # x=500 is at image x=600
    assert ((4, 302), "500") in labels  # y=500 is at image y=300, NOT image y=500
    assert image.getpixel((200, 60))[0] > 0 and image.getpixel((200, 100)) == (0, 0, 0)


def test_cursor_is_still_drawn_in_image_pixels_not_normalized_units():
    shot = prepare_screenshot(Image.new("RGB", (1920, 1080)), coordinate_space=NORM,
                              cursor=(960, 540), grid=False)
    assert shot.to_model(960, 540) == (500, 500)
    assert shot.image.getpixel((640, 360)) == (255, 255, 0)
    assert shot.image.getpixel((500, 500)) == (0, 0, 0)


def test_crop_edges_and_reported_window_positions_share_the_contract():
    cfg = Config(coordinate_space=NORM, action_delay=0, auto_screenshot_after_action=False)
    backend = FakeBackend(width=1920, height=1080)
    executor = ToolExecutor(backend, cfg)
    assert executor.execute(ToolCall("open_app", {"name": "notepad", "wait": 0})).ok
    full = executor.take_screenshot()
    backend.mouse = (960, 540)
    info = executor.execute(ToolCall("get_screen_info", {}))
    assert info.data["mouse_screenshot"] == [500, 500] and info.data["mouse_physical"] == [960, 540]
    window = backend.active_window()
    assert info.data["active_window"]["screenshot_rect"] == [*full.to_model(window.left, window.top),
                                                             *full.to_model(window.right, window.bottom)]
    controls = executor.execute(ToolCall("get_window_controls", {}))
    raw_controls = backend.window_controls(window.hwnd)
    for control, raw in zip(controls.data["controls"], raw_controls, strict=True):
        assert control["coordinate_space"] == NORM
        assert control["screenshot_center"] == list(full.to_model((raw.left + raw.right) // 2, (raw.top + raw.bottom) // 2))
    region = executor.execute(ToolCall("screenshot", {"region": [250, 250, 750, 750]}))
    assert region.ok and region.screenshot.origin == (480, 270) and region.screenshot.raw_size == (960, 540)
    whole = executor.execute(ToolCall("screenshot", {"region": [0, 0, 1000, 1000]}))
    assert whole.ok and whole.screenshot.raw_size == (1920, 1080)
    assert executor.last_screenshot is full


@pytest.mark.parametrize("tool,args", [
    ("mouse_move", {"x": 500, "y": 500}), ("click", {"x": 500, "y": 500}),
    ("double_click", {"x": 500, "y": 500}), ("right_click", {"x": 500, "y": 500}),
    ("click_at", {"x": 500, "y": 500}), ("scroll", {"amount": -1, "x": 500, "y": 500}),
    ("drag", {"x1": 250, "y1": 250, "x2": 500, "y2": 500}),
])
def test_every_pointer_path_converts_model_units_once(tool, args):
    cfg = Config(coordinate_space=NORM, action_delay=0, auto_screenshot_after_action=False)
    backend = FakeBackend(width=1920, height=1080)
    executor = ToolExecutor(backend, cfg)
    executor.take_screenshot()
    result = executor.execute(ToolCall(tool, args))
    assert result.ok, result.error
    mapping = result.data["coordinate_mapping"][-1]
    assert mapping["model_point"] == [500, 500]
    assert mapping["physical_target"] == [960, 540] and mapping["image_point"] == [640, 360]
    assert mapping["coordinate_space"] == NORM
    if tool == "drag":
        assert result.data["coordinate_mapping"][0]["physical_target"] == [480, 270]


def test_default_and_aliases_never_auto_enable_normalization():
    for model in ("ag/gemini-pro-agent", "google/gemini-3.1-pro-preview", "some-private-alias"):
        assert Config(model=model).coordinate_space == PIXELS
    cfg = Config(coordinate_space=NORM)
    assert Config.from_dict(cfg.to_dict()).coordinate_space == NORM
    assert any("coordinate_space" in e for e in Config(coordinate_space="auto").validate())


def test_native_schemas_and_json_docs_are_consistent_without_mutating_registry():
    original = deepcopy(TOOLS_BY_NAME["click"].parameters)
    norm = {e["function"]["name"]: e["function"] for e in openai_tool_schemas(NORM)}
    for name in ("mouse_move", "click", "double_click", "right_click", "scroll"):
        xy = norm[name]["parameters"]["properties"]
        assert xy["x"]["maximum"] == xy["y"]["maximum"] == 1000
        assert xy["x"]["minimum"] == xy["y"]["minimum"] == 0
    assert norm["drag"]["parameters"]["properties"]["y2"]["maximum"] == 1000
    assert norm["screenshot"]["parameters"]["properties"]["region"]["items"]["maximum"] == 1000
    assert "maximum" not in norm["window_action"]["parameters"]["properties"]["x"]
    assert TOOLS_BY_NAME["click"].parameters == original
    pixel = next(e["function"] for e in openai_tool_schemas() if e["function"]["name"] == "click")
    assert pixel["parameters"] == original
    assert "integer[0..1000]" in tools_markdown(NORM)
    assert "region uses normalized 0-1000" in tools_markdown(NORM)
    assert "normalized 0-1000 units on BOTH axes" in norm["click"]["description"]


@pytest.mark.parametrize("protocol", ["native", "json"])
def test_mode_is_pinned_for_batch_and_changes_only_with_the_next_observed_frame(monkeypatch, protocol):
    cfg = Config(coordinate_space=NORM, tool_protocol=protocol, action_delay=0, verify_on_completion=False)
    backend = FakeBackend(width=1920, height=1080)
    move = backend.mouse_move
    def change_mode(*args, **kwargs):
        move(*args, **kwargs)
        cfg.coordinate_space = PIXELS
    monkeypatch.setattr(backend, "mouse_move", change_mode)
    llm = ScriptedLLM([
        turn(protocol, ("mouse_move", {"x": 500, "y": 500}), ("click", {"x": 500, "y": 500})),
        turn(protocol, ("click", {"x": 500, "y": 500})),
        turn(protocol, ("task_complete", {"summary": "done"})),
    ])
    results = []
    agent = Agent(cfg, backend, llm, AgentEvents(on_tool_end=results.append))
    assert agent.run("test coordinate frames").status == "completed"
    click_positions = [(e["x"], e["y"]) for e in backend.events if e["kind"] == "click"]
    assert click_positions == [(960, 540), (750, 750)]
    assert results[1].data["coordinate_mapping"][0]["coordinate_space"] == NORM
    assert f"Coordinate convention: {NORM}" in llm.calls[0]["messages"][0]["content"]
    assert f"Coordinate convention: {PIXELS}" in llm.calls[1]["messages"][0]["content"]
    if protocol == "native":
        first_click = next(e["function"] for e in llm.calls[0]["tools"] if e["function"]["name"] == "click")
        second_click = next(e["function"] for e in llm.calls[1]["tools"] if e["function"]["name"] == "click")
        assert first_click["parameters"]["properties"]["y"]["maximum"] == 1000
        assert "maximum" not in second_click["parameters"]["properties"]["y"]


def test_retry_keeps_the_requested_coordinate_contract_even_if_settings_change():
    cfg = Config(coordinate_space=NORM, action_delay=0, verify_on_completion=False)
    backend = FakeBackend(width=1920, height=1080)
    def broken(messages):
        cfg.coordinate_space = PIXELS
        return "document, y from 0 (top) to 719 (bottom).]"
    llm = ScriptedLLM([broken, native_tool_message(("mouse_move", {"x": 500, "y": 500})), native_tool_message(("task_complete", {"summary": "done"}))])
    agent = Agent(cfg, backend, llm)
    assert agent.run("move to the centre").status == "completed"
    assert backend.mouse == (960, 540)
    assert f"Coordinate convention: {NORM}" in llm.calls[1]["messages"][0]["content"]


def test_normalized_without_a_frame_fails_closed_then_requests_a_full_image():
    cfg = Config(coordinate_space=NORM, action_delay=0, verify_on_completion=False)
    backend = FakeBackend(width=1920, height=1080)
    executor = ToolExecutor(backend, cfg)
    result = executor.execute(ToolCall("click", {"x": 500, "y": 500}))
    assert not result.ok and result.needs_new_screenshot and not backend.events
    llm = ScriptedLLM([native_tool_message(("mouse_move", {"x": 500, "y": 500}))] * 2 + [native_tool_message(("task_complete", {"summary": "done"}))])
    agent = Agent(cfg, backend, llm)
    assert agent.run("move", initial_screenshot=False).status == "completed"
    assert backend.mouse == (960, 540)
    assert sum(e["kind"] == "move" for e in backend.events) == 1


def test_prompts_explicitly_disallow_mixing_units_and_keep_window_positions_physical():
    for protocol in ("native", "json"):
        prompt = build_system_prompt(protocol=protocol, vision=True, system_info={}, coordinate_space=NORM)
        assert "centre is (500,500)" in prompt
        assert "NOT image pixels" in prompt
        assert "window_action positioning/sizing are still physical pixels" in prompt
    pixel = build_system_prompt(protocol="native", vision=True, system_info={})
    assert "Do NOT return normalized 0-1000 units" in pixel


def test_small_numeric_values_never_trigger_unit_auto_detection():
    for mode, expected in ((PIXELS, (750, 750)), (NORM, (960, 540))):
        backend = FakeBackend(width=1920, height=1080)
        executor = ToolExecutor(backend, Config(coordinate_space=mode, auto_screenshot_after_action=False))
        executor.take_screenshot()
        assert executor.execute(ToolCall("mouse_move", {"x": 500, "y": 500})).ok
        assert backend.mouse == expected


def test_tool_cannot_override_its_frame_units_with_a_conflicting_extra_argument():
    backend = FakeBackend(width=1920, height=1080)
    executor = ToolExecutor(backend, Config(coordinate_space=NORM))
    executor.take_screenshot()
    result = executor.execute(ToolCall("click", {"x": 500, "y": 500, "coordinate_space": PIXELS}))
    assert not result.ok and "No input was sent" in result.error
    assert not backend.events


def test_window_action_positions_remain_physical_pixels_in_normalized_mode():
    backend = FakeBackend(width=1920, height=1080)
    executor = ToolExecutor(backend, Config(coordinate_space=NORM, auto_screenshot_after_action=False))
    assert executor.execute(ToolCall("open_app", {"name": "notepad", "wait": 0})).ok
    executor.take_screenshot()
    assert executor.execute(ToolCall("window_action", {"action": "move", "x": 100, "y": 200})).ok
    window = backend.active_window()
    assert (window.left, window.top) == (100, 200)


@pytest.mark.parametrize("cursor,visible", [((1919, 1079), True), ((1920, 1080), False)])
def test_edge_cursor_annotation_is_clamped_only_for_a_position_inside_capture(cursor, visible):
    shot = prepare_screenshot(Image.new("RGB", (1920, 1080)), max_width=640, grid=False,
                              coordinate_space=NORM, cursor=cursor)
    assert (shot.image.getpixel((639, 359)) == (255, 255, 0)) is visible


@pytest.mark.parametrize("value", [0.5, -0.1, 1000.1, "500px", True, float("nan"), float("inf")])
def test_normalized_mode_does_not_round_other_units_into_valid_clicks(value):
    backend = FakeBackend(width=1920, height=1080)
    executor = ToolExecutor(backend, Config(coordinate_space=NORM))
    executor.take_screenshot()
    result = executor.execute(ToolCall("click", {"x": value, "y": 500}))
    assert not result.ok and not backend.events
    assert "normalized 0-1000 value" in result.error


def test_integer_valued_coordinate_strings_remain_compatible():
    backend = FakeBackend(width=1920, height=1080)
    executor = ToolExecutor(backend, Config(coordinate_space=NORM, auto_screenshot_after_action=False))
    executor.take_screenshot()
    assert executor.execute(ToolCall("mouse_move", {"x": "500", "y": 500.0})).ok
    assert backend.mouse == (960, 540)
