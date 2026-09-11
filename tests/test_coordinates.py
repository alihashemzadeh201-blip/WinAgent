"""Regression checks for image/physical pixels, fractional resize rounding and Windows DPI."""

import ctypes
import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from winagent.backends import BackendError, EmergencyStop, FakeBackend, ScreenGeometry
from winagent.backends import windows
from winagent.protocol import ToolCall
from winagent.screenshot import prepare_screenshot
from winagent.tools import ToolExecutor


@pytest.mark.parametrize("size,max_width,origin", [
    ((1600, 899), 800, (0, 0)),
    ((1920, 1080), 1280, (0, 0)),
    ((1537, 901), 800, (-1537, -160)),
    ((1080, 1920), 640, (1920, -1080)),
    ((3840, 2160), 1280, (-3840, 0)),
])
@pytest.mark.parametrize("grid", [False, True])
def test_axis_mapping_matches_the_actual_encoded_image(size, max_width, origin, grid):
    shot = prepare_screenshot(Image.new("RGB", size), max_width=max_width, grid=grid, fmt="png",
                              geometry=ScreenGeometry(*origin, *size))
    encoded = Image.open(io.BytesIO(shot.encode()))
    w, h = encoded.size
    for x, y in ((0, 0), (w // 2, h // 2), (w - 1, h - 1)):
        expected = (round(x * size[0] / w) + origin[0], round(y * size[1] / h) + origin[1])
        assert shot.to_physical(x, y) == expected
        ix, iy = shot.to_image(*expected)
        assert (ix, iy) == (x, y)


def test_vertical_rounding_does_not_reuse_width_scale(config):
    backend = FakeBackend(width=1600, height=899)
    config.screenshot_max_width = 800
    config.auto_screenshot_after_action = False
    executor = ToolExecutor(backend, config)
    shot = executor.take_screenshot()
    assert shot.size == (800, 450)
    assert shot.scale_x != shot.scale_y
    assert round(449 / shot.scale) == 898  # old implementation landed one physical pixel too low
    result = executor.execute(ToolCall("click", {"x": 400, "y": 449}))
    assert result.ok
    assert backend.mouse == (800, 897)


@pytest.mark.parametrize("x,y", [(-1, 100), (800, 100), (100, -1), (100, 450)])
def test_out_of_frame_click_is_rejected_not_silently_moved_to_an_edge(backend, config, x, y):
    executor = ToolExecutor(backend, config)
    executor.take_screenshot()
    result = executor.execute(ToolCall("click", {"x": x, "y": y}))
    assert not result.ok and "outside" in result.error
    assert not any(e["kind"] == "click" for e in backend.events)


def test_negative_monitor_origin_maps_click_and_drag_consistently(config, monkeypatch):
    backend = FakeBackend(width=1600, height=899)
    monkeypatch.setattr(backend, "screen_geometry", lambda all_screens=False: ScreenGeometry(-1600, -899, 1600, 899))
    config.auto_screenshot_after_action = False
    executor = ToolExecutor(backend, config)
    shot = executor.take_screenshot(all_screens=True)
    assert executor.execute(ToolCall("click", {"x": 400, "y": 400})).ok
    assert backend.mouse == shot.to_physical(400, 400)
    assert executor.execute(ToolCall("drag", {"x1": 100, "y1": 100, "x2": 500, "y2": 400})).ok
    event = next(e for e in backend.events if e["kind"] == "drag")
    assert (event["x1"], event["y1"]) == shot.to_physical(100, 100)
    assert (event["x2"], event["y2"]) == shot.to_physical(500, 400)


def test_zoom_description_does_not_claim_its_pixels_are_the_click_frame(backend, config):
    executor = ToolExecutor(backend, config)
    full = executor.take_screenshot()
    region = executor.take_screenshot(region=[100, 100, 300, 200])
    assert executor.last_screenshot is full and region.is_region
    assert "LAST FULL screenshot" in region.describe()
    assert "Coordinates you send must be in this" not in region.describe()
    assert "DPI and scaling are already handled" in full.describe()


@pytest.fixture
def physical_backend(monkeypatch):
    """A physical-pixel Win32 stand-in; a logical API deliberately returns the wrong coordinates."""
    backend = object.__new__(windows.WindowsBackend)
    pointer = [300, 200]

    def get_physical(point):
        # the backend passes the POINT instance (ctypes takes its address for the POINTER argtype);
        # tolerate a byref wrapper too so a regression back to byref() still updates the mock state
        obj = point._obj if hasattr(point, "_obj") else point
        obj.x, obj.y = pointer
        return True

    def set_physical(x, y):
        pointer[:] = [x, y]
        return True

    backend.user32 = SimpleNamespace(
        GetPhysicalCursorPos=Mock(side_effect=get_physical), SetPhysicalCursorPos=Mock(side_effect=set_physical),
        GetCursorPos=Mock(side_effect=AssertionError("must not use DPI-virtualised cursor coordinates")),
        SetCursorPos=Mock(side_effect=AssertionError("must not use DPI-virtualised cursor coordinates")),
    )
    backend._send = Mock()
    monkeypatch.setattr(windows.time, "sleep", lambda seconds: None)
    return backend, pointer


@pytest.mark.parametrize("target", [(800, 450), (-800, -450), (3839, 2159)])
def test_windows_mouse_targets_physical_pixels_without_an_extra_dpi_factor(physical_backend, target):
    backend, pointer = physical_backend
    backend.mouse_click(*target)
    assert tuple(pointer) == target and backend.mouse_position() == target
    backend.user32.SetPhysicalCursorPos.assert_called_with(*target)
    backend.user32.SetCursorPos.assert_not_called()
    backend.user32.GetCursorPos.assert_not_called()
    events = backend._send.call_args.args[0]
    assert events[0].mi.dwFlags == windows.MOUSEEVENTF_LEFTDOWN
    assert events[1].mi.dwFlags == windows.MOUSEEVENTF_LEFTUP


@pytest.mark.parametrize("method,args,reads,target", [
    ("mouse_move", (800, 450), 1, (800, 450)),
    ("mouse_click", (800, 450), 1, (800, 450)),
    ("mouse_down", (800, 450), 1, (800, 450)),
    ("mouse_up", (800, 450), 1, (800, 450)),
    ("mouse_drag", (100, 200, 800, 450), 2, (800, 450)),
    ("mouse_scroll", (800, 450, 0, -1), 1, (800, 450)),
    ("mouse_click", (None, None), 0, (300, 200)),
    ("mouse_scroll", (None, None, 0, -1), 0, (300, 200)),
])
def test_windows_pointer_input_reads_only_the_motion_start_not_target_verification(physical_backend, method, args, reads, target):
    backend, pointer = physical_backend
    getattr(backend, method)(*args)
    assert tuple(pointer) == target
    # One initial read per interpolated motion; none at the destination or before button input.
    assert backend.user32.GetPhysicalCursorPos.call_count == reads
    backend.user32.GetCursorPos.assert_not_called()
    backend.user32.SetCursorPos.assert_not_called()
    assert backend._send.called


def test_failed_mouse_api_does_not_click_or_fake_a_position(physical_backend):
    backend, _ = physical_backend
    backend.user32.SetPhysicalCursorPos.side_effect = None
    backend.user32.SetPhysicalCursorPos.return_value = False
    with pytest.raises(BackendError, match="SetPhysicalCursorPos failed"):
        backend.mouse_click(800, 450)
    backend._send.assert_not_called()
    backend.user32.GetPhysicalCursorPos.side_effect = None
    backend.user32.GetPhysicalCursorPos.return_value = False
    with pytest.raises(BackendError, match="GetPhysicalCursorPos failed"):
        backend.mouse_position()


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("fail_capture", [False, True])
def test_capture_thread_dpi_context_is_restored(monkeypatch, fallback, fail_capture):
    state = {"context": 123}
    calls = []
    def set_context(value):
        value = ctypes.c_ssize_t(getattr(value, "value", value)).value
        calls.append(value)
        if value == -4 and fallback:
            return None
        previous = state["context"]
        state["context"] = value
        return previous
    monkeypatch.setattr(windows, "IS_WINDOWS", True)
    monkeypatch.setattr(windows.ctypes, "windll", SimpleNamespace(user32=SimpleNamespace(
        SetThreadDpiAwarenessContext=Mock(side_effect=set_context))), raising=False)

    raw = Image.new("RGB", (640, 360))
    def grab(**kwargs):
        assert state["context"] == (-3 if fallback else -4)
        if fail_capture:
            raise OSError("test capture failed")
        return raw
    monkeypatch.setattr(windows.ImageGrab, "grab", grab)
    backend = object.__new__(windows.WindowsBackend)
    if fail_capture:
        with pytest.raises(BackendError) as error:
            backend.capture()
        assert isinstance(error.value.__cause__, OSError)
    else:
        assert backend.capture() is raw
    assert calls == ([-4, -3, 123] if fallback else [-4, 123])
    assert state["context"] == 123


def test_failed_process_dpi_hresult_does_not_skip_legacy_fallback(monkeypatch):
    user32 = SimpleNamespace(SetProcessDpiAwarenessContext=Mock(return_value=False), SetProcessDPIAware=Mock())
    shcore = SimpleNamespace(SetProcessDpiAwareness=Mock(return_value=-2147024891))
    monkeypatch.setattr(windows, "IS_WINDOWS", True)
    monkeypatch.setattr(windows.ctypes, "windll", SimpleNamespace(user32=user32, shcore=shcore), raising=False)
    windows.make_dpi_aware()
    shcore.SetProcessDpiAwareness.assert_called_once_with(2)
    user32.SetProcessDPIAware.assert_called_once()


@pytest.mark.parametrize("corner", [(0, 0), (-1600, -899)])
def test_primary_and_virtual_corners_still_trigger_failsafe(backend, config, monkeypatch, corner):
    backend.mouse = corner
    monkeypatch.setattr(backend, "screen_geometry", lambda all_screens=False: ScreenGeometry(-1600, -899, 1600, 899))
    with pytest.raises(EmergencyStop, match="Fail-safe"):
        ToolExecutor(backend, config).execute(ToolCall("click", {}))
    assert not any(e["kind"] == "click" for e in backend.events)


def test_virtual_screen_geometry_uses_physical_thread_coordinates(monkeypatch):
    current = {"dpi": 123}
    def set_context(value):
        value = ctypes.c_ssize_t(getattr(value, "value", value)).value
        previous, current["dpi"] = current["dpi"], value
        return previous
    def metrics(index):
        assert current["dpi"] == -4
        return {windows.SM_CMONITORS: 2, windows.SM_XVIRTUALSCREEN: -1920, windows.SM_YVIRTUALSCREEN: -1080,
                windows.SM_CXVIRTUALSCREEN: 3840, windows.SM_CYVIRTUALSCREEN: 2160}[index]
    user32 = SimpleNamespace(SetThreadDpiAwarenessContext=Mock(side_effect=set_context), GetSystemMetrics=metrics)
    monkeypatch.setattr(windows, "IS_WINDOWS", True)
    monkeypatch.setattr(windows.ctypes, "windll", SimpleNamespace(user32=user32), raising=False)
    backend = object.__new__(windows.WindowsBackend)
    backend.user32 = user32
    geometry = backend.screen_geometry(all_screens=True)
    assert (geometry.left, geometry.top, geometry.width, geometry.height) == (-1920, -1080, 3840, 2160)
    assert current["dpi"] == 123
