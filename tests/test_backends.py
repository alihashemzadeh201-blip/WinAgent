"""Real capture must never silently turn into the blue simulated desktop."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from winagent import __main__ as entrypoint
from winagent import backends
from winagent.backends import BackendError, FakeBackend
from winagent.backends import windows
from winagent.config import Config


@pytest.mark.parametrize("kind", ["auto", "windows", " AUTO "])
def test_windows_selection_uses_real_backend(monkeypatch, kind):
    monkeypatch.setattr(backends, "sys", SimpleNamespace(platform="win32"))
    factory = Mock(return_value=object())
    monkeypatch.setattr(windows, "WindowsBackend", factory)
    fake = Mock(side_effect=AssertionError("unexpected simulation"))
    monkeypatch.setattr(backends, "FakeBackend", fake)
    stop = Mock()

    assert backends.create_backend(kind, stop_hotkey="ctrl+shift+esc", on_emergency_stop=stop) is factory.return_value
    factory.assert_called_once_with(stop_hotkey="ctrl+shift+esc", on_emergency_stop=stop)
    fake.assert_not_called()


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_auto_on_unsupported_os_requires_explicit_demo(monkeypatch, platform):
    monkeypatch.setattr(backends, "sys", SimpleNamespace(platform=platform))
    fake = Mock(side_effect=AssertionError("unexpected simulation"))
    monkeypatch.setattr(backends, "FakeBackend", fake)

    with pytest.raises(BackendError, match="only on Windows") as error:
        backends.create_backend("auto")
    assert "--demo" in str(error.value) and "cannot capture your screen" in str(error.value)
    fake.assert_not_called()


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_explicit_demo_is_available_and_warns(monkeypatch, caplog, platform):
    monkeypatch.setattr(backends, "sys", SimpleNamespace(platform=platform))
    backend = backends.create_backend("fake")
    assert isinstance(backend, FakeBackend)
    assert "DEMO" in caplog.text and "not your real screen" in caplog.text
    # Raw/saved demo images carry a visible banner too, not just an unlabelled blue wallpaper.
    image = backend.capture()
    assert image.getpixel((0, 0)) != image.getpixel((0, 100))
    assert len(image.crop((0, 0, image.width, 56)).getcolors(maxcolors=100_000)) > 1
    assert backend.capture(region=(100, 100, 300, 200)).size == (200, 100)


def test_windows_initialisation_failure_does_not_use_fake(monkeypatch):
    monkeypatch.setattr(backends, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(windows, "WindowsBackend", Mock(side_effect=BackendError("Win32 unavailable")))
    fake = Mock(side_effect=AssertionError("unexpected simulation"))
    monkeypatch.setattr(backends, "FakeBackend", fake)

    with pytest.raises(BackendError, match="Win32 unavailable"):
        backends.create_backend("auto")
    fake.assert_not_called()


def test_cli_reports_backend_error_without_running_agent(monkeypatch, capsys):
    factory = Mock(side_effect=BackendError("Real desktop is unavailable; use --demo for simulation."))
    monkeypatch.setattr(backends, "create_backend", factory)
    cfg = Config(stop_hotkey="ctrl+shift+esc")

    assert entrypoint._cli_loop(cfg, "auto", "take a screenshot") == 4
    assert "Desktop backend error:" in capsys.readouterr().err
    assert factory.call_args.kwargs["stop_hotkey"] == cfg.stop_hotkey
    assert factory.call_count == 1


@pytest.mark.parametrize("args,configured,expected", [
    ([], "auto", "auto"),
    (["--demo"], "windows", "fake"),
    (["--backend", "windows"], "fake", "windows"),
    (["--demo", "--backend", "windows"], "fake", "windows"),
])
def test_cli_backend_flag_precedence(monkeypatch, args, configured, expected):
    cfg = Config(backend=configured)
    monkeypatch.setattr(entrypoint, "load_config", lambda path: cfg)
    monkeypatch.setattr(entrypoint, "_setup_logging", lambda *a: None)
    loop = Mock(return_value=0)
    monkeypatch.setattr(entrypoint, "_cli_loop", loop)

    assert entrypoint.main(["--cli", *args]) == 0
    loop.assert_called_once_with(cfg, expected, None)
