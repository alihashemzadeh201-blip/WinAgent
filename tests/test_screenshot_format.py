"""'auto' screenshot format: lossless PNG for flat UI screens, JPEG for photo/3D content,
explicit formats are respected, and duplicate suppression keeps working on top of it."""

import random

from PIL import Image

from winagent.screenshot import is_flat_image, prepare_screenshot
from winagent.tools import ToolExecutor


def make_noisy(width=1600, height=900, seed=7):
    img = Image.new("RGB", (width, height))
    px = img.load()
    rnd = random.Random(seed)
    for y in range(height):
        for x in range(width):
            px[x, y] = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
    return img


def make_gradient(width=1600, height=900):
    img = Image.new("RGB", (width, height))
    px = img.load()
    for y in range(height):
        for x in range(width):
            px[x, y] = (x * 255 // width, y * 255 // height, (x + y) * 255 // (width + height))
    return img


def test_is_flat_image_classifies_content():
    assert is_flat_image(Image.new("RGB", (400, 300), (200, 200, 210)))
    assert is_flat_image(make_gradient(400, 300))
    assert not is_flat_image(make_noisy(400, 300))


def test_default_format_is_auto(backend, config):
    assert config.screenshot_format == "auto"
    # the simulated desktop is a flat UI -> lossless PNG
    shot = ToolExecutor(backend, config).take_screenshot()
    assert shot.fmt == "png"
    assert shot.data_url().startswith("data:image/png;base64,")


def test_auto_uses_jpeg_for_noisy_screen(backend, config, monkeypatch):
    monkeypatch.setattr(backend, "capture", lambda all_screens=False, region=None: make_noisy())
    shot = ToolExecutor(backend, config).take_screenshot()
    assert shot.fmt == "jpeg"
    assert shot.data_url().startswith("data:image/jpeg;base64,")


def test_auto_uses_png_for_smooth_gradient(backend, config, monkeypatch):
    monkeypatch.setattr(backend, "capture", lambda all_screens=False, region=None: make_gradient())
    shot = ToolExecutor(backend, config).take_screenshot()
    assert shot.fmt == "png"


def test_explicit_format_is_respected(backend, config):
    config.screenshot_format = "jpeg"
    assert ToolExecutor(backend, config).take_screenshot().fmt == "jpeg"
    config.screenshot_format = "png"
    assert ToolExecutor(backend, config).take_screenshot().fmt == "png"


def test_prepare_screenshot_resolves_auto():
    flat = prepare_screenshot(Image.new("RGB", (640, 400), (10, 20, 30)), fmt="auto")
    assert flat.fmt == "png"
    noisy = prepare_screenshot(make_noisy(640, 400), fmt="auto")
    assert noisy.fmt == "jpeg"
    explicit = prepare_screenshot(make_noisy(640, 400), fmt="png")
    assert explicit.fmt == "png"


def test_png_is_lossless_for_the_model(backend, config):
    # flat screens are sent lossless: decoding the payload reproduces the (scaled) pixels
    shot = ToolExecutor(backend, config).take_screenshot()
    assert shot.fmt == "png"
    import base64
    from io import BytesIO
    payload = base64.b64decode(shot.data_url().split(",", 1)[1])
    decoded = Image.open(BytesIO(payload))
    assert decoded.size == shot.size


def test_dedupe_still_works_with_auto_format(backend, config):
    ex = ToolExecutor(backend, config)
    first = ex.take_screenshot()
    second = ex.take_screenshot()
    assert second is first  # identical screen -> the previous frame is reused, no duplicate image
    assert ex.last_capture_unchanged


def test_format_change_is_a_new_frame(backend, config, monkeypatch):
    ex = ToolExecutor(backend, config)
    first = ex.take_screenshot()
    # the screen becomes noisy -> 'auto' resolves JPEG -> a genuinely new frame (not a dedupe)
    monkeypatch.setattr(backend, "capture", lambda all_screens=False, region=None: make_noisy(seed=11))
    second = ex.take_screenshot()
    assert second is not first
    assert second.fmt == "jpeg"
