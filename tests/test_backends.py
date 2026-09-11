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


# ---------------------------------------------------------------- ctypes hygiene

def test_windows_backend_never_passes_byref_to_pointer_argtypes():
    """Regression: Python 3.12 rejects ctypes.byref() for POINTER-declared argtypes.

    It raised ``ArgumentError: expected LP__RECT instance instead of pointer to RECT`` in
    production (crashing ``window_action``), and it silently zeroed the mouse/active-window
    screenshot data through a swallowed exception in ``mouse_position``.  Every call to a
    function whose argtypes declare POINTER(...) must pass the ctypes instance (or an array),
    never byref(); functions declared with c_void_p may keep byref.
    """
    import ast
    import re
    from pathlib import Path

    source = (Path(windows.__file__).with_suffix(".py")).read_text(encoding="utf-8")
    tree = ast.parse(source)

    # 1) collect the functions declared with a POINTER(...) argtype
    pointer_funcs: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Attribute)):
            continue
        target = node.targets[0]
        if not (isinstance(target.value, ast.Attribute) and target.attr == "argtypes"):
            continue
        func_name = target.value.attr
        if not isinstance(node.value, (ast.List, ast.Tuple)):
            continue
        for item in node.value.elts:
            if not isinstance(item, ast.Call):
                continue
            ptr_name = (item.func.id if isinstance(item.func, ast.Name)
                        else item.func.attr if isinstance(item.func, ast.Attribute) else None)
            if ptr_name == "POINTER":
                pointer_funcs.add(func_name)
    assert {"GetWindowRect", "GetPhysicalCursorPos"} <= pointer_funcs  # sanity: the audit found these

    # 2) no call site of those functions may pass a byref(...) argument
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None)
        if name not in pointer_funcs:
            continue
        for arg in node.args:
            if (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
                    and arg.func.id in ("byref", "pointer")):
                line = re.sub(r"\s+", " ", source.splitlines()[node.lineno - 1].strip())
                offenders.append(f"line {node.lineno}: {name}({arg.func.id}(...)): {line}")
    assert not offenders, "byref/pointer passed to a POINTER-argtypes function:\n" + "\n".join(offenders)
