"""Test the Windows capture boundary with ImageGrab stubbed (no live Windows desktop required)."""

import base64
import io
from unittest.mock import Mock

import pytest
from PIL import Image, ImageDraw

from winagent.backends import BackendError, ScreenGeometry
from winagent.backends import windows
from winagent.config import Config
from winagent.tools.executor import ToolExecutor


@pytest.fixture
def windows_backend():
    # Do not initialise Win32 DLLs/hotkeys on the test host; capture() only calls Pillow.
    return object.__new__(windows.WindowsBackend)


@pytest.mark.parametrize("all_screens,region", [
    (False, None),
    (True, None),
    (False, (10, 20, 110, 80)),
    (False, (-1280, -200, -640, 160)),
    (True, (-1280, 0, 0, 720)),
])
def test_capture_includes_layered_windows_and_absolute_regions(monkeypatch, windows_backend, all_screens, region):
    image = Image.new("RGB", (100, 60), (10, 20, 30))
    grab = Mock(return_value=image)
    monkeypatch.setattr(windows.ImageGrab, "grab", grab)

    assert windows_backend.capture(all_screens=all_screens, region=region) is image
    grab.assert_called_once_with(bbox=region, all_screens=all_screens or region is not None, include_layered_windows=True)


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L"])
def test_capture_returns_rgb_without_replacing_pixels(monkeypatch, windows_backend, mode):
    image = Image.new(mode, (101, 61), 80)
    monkeypatch.setattr(windows.ImageGrab, "grab", Mock(return_value=image))
    captured = windows_backend.capture()
    assert captured.mode == "RGB"
    assert captured.size == image.size
    assert captured.tobytes() == image.convert("RGB").tobytes()


def test_capture_error_is_actionable_not_a_generated_image(monkeypatch, windows_backend):
    cause = OSError("screen grab failed")
    grab = Mock(side_effect=cause)
    monkeypatch.setattr(windows.ImageGrab, "grab", grab)

    with pytest.raises(BackendError, match="unlocked, interactive Windows desktop") as error:
        windows_backend.capture()
    assert error.value.__cause__ is cause
    assert "Remote Desktop" in str(error.value)
    grab.assert_called_once()


def test_real_capture_pixels_reach_the_model_image(monkeypatch, windows_backend):
    # A real ImageGrab result passes through scaling/encoding, never through FakeBackend.rendering.
    raw = Image.new("RGB", (640, 360), (24, 40, 56))
    ImageDraw.Draw(raw).rectangle((0, 0, 319, 359), fill=(210, 30, 40))
    monkeypatch.setattr(windows.ImageGrab, "grab", Mock(return_value=raw))
    windows_backend.screen_geometry = Mock(return_value=ScreenGeometry(0, 0, 640, 360))
    cfg = Config(screenshot_max_width=320, screenshot_grid=False, screenshot_show_cursor=False, screenshot_format="png")
    shot = ToolExecutor(windows_backend, cfg).take_screenshot()

    encoded = base64.b64decode(shot.data_url().split(",", 1)[1])
    received = Image.open(io.BytesIO(encoded))
    assert received.size == (320, 180) and shot.raw_size == (640, 360)
    assert received.getpixel((50, 50)) == (210, 30, 40)
    assert received.getpixel((250, 50)) == (24, 40, 56)
    assert shot.to_physical(100, 100) == (200, 200)


def test_valid_blue_desktop_is_not_rejected_by_colour(monkeypatch, windows_backend):
    raw = Image.new("RGB", (320, 180), (30, 90, 160))
    monkeypatch.setattr(windows.ImageGrab, "grab", Mock(return_value=raw))
    # A genuinely empty blue wallpaper is still a valid capture; provenance, not a colour heuristic, distinguishes demo.
    assert windows_backend.capture() is raw
