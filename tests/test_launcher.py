"""Program-launch resolution, fallback chain and error reporting (platform independent)."""

import pytest

from winagent.backends.base import BackendError
from winagent.backends.fake import FakeBackend
from winagent.backends.launcher import (
    APP_ALIASES,
    Attempt,
    Launcher,
    LaunchEnv,
    best_match,
    describe_win32_error,
    lookup_alias,
    normalize_app_name,
    split_program_args,
)
from winagent.prompts import build_system_prompt, environment_section
from winagent.protocol import ToolCall
from winagent.tools import ToolExecutor


# --------------------------------------------------------------------------- aliases
@pytest.mark.parametrize("query, expected", [
    ("paint", "mspaint.exe"),
    ("Microsoft Paint", "mspaint.exe"),
    ("pbrush", "mspaint.exe"),                # the legacy name the model guessed in the field
    ("mspaint.exe", "mspaint.exe"),
    ("برنامه نقاشی", "mspaint.exe"),
    ("نقاشی را باز کن", "mspaint.exe"),
    ("ماشین\u200cحساب", "calc.exe | calculator:"),  # ZWNJ variant
    ("the calculator app", "calc.exe | calculator:"),
    ("Google Chrome browser", "chrome.exe"),
    ("notepad++", "notepad++.exe"),
    ("vs code", "code.exe | code"),
    ("Settings", "ms-settings:"),
    ("wifi settings", "ms-settings:network-wifi"),
    ("notepd", "notepad.exe"),                # fuzzy
    ("telegram desktop", "telegram.exe"),
    ("browser", "@browser"),
])
def test_lookup_alias(query, expected):
    assert lookup_alias(query) == expected


def test_lookup_alias_ignores_paths_and_unknown_names():
    assert lookup_alias(r"C:\x.exe") is None
    assert lookup_alias("xyzzy") is None
    assert lookup_alias("") is None


def test_alias_table_is_normalised():
    for key in APP_ALIASES:
        assert key == normalize_app_name(key)
        assert "\u200c" not in key


@pytest.mark.parametrize("raw, expected", [
    ("code .", ("code", ".")),
    (r"notepad C:\x.txt", ("notepad", r"C:\x.txt")),
    ("chrome --incognito", ("chrome", "--incognito")),
    ("explorer.exe shell:Downloads", ("explorer.exe", "shell:Downloads")),
    ("Microsoft Edge", None),
    ("visual studio code", None),
    ("Paint 3D", None),
])
def test_split_program_args(raw, expected):
    assert split_program_args(raw) == expected


# ------------------------------------------------------------------------ best_match
def test_best_match_prefers_whole_words_and_penalises_uninstallers():
    assert best_match("word", ["WordPad", "Microsoft Word", "Word 2016"]) == (1, 1)
    assert best_match("chrome", ["Google Chrome", "Chrome Remote Desktop", "Uninstall Google Chrome"]) == (1, 0)
    assert best_match("edge", ["Edge Beta", "Microsoft Edge"]) == (1, 1)
    assert best_match("telegram", ["Uninstall Telegram", "Telegram Desktop"]) == (1, 1)
    assert best_match("paint", ["Paint 3D", "Paint"]) == (0, 1)
    assert best_match("visual studio code", ["Visual Studio 2022", "Visual Studio Code"]) == (0, 1)
    assert best_match("xyz", ["Google Chrome"]) is None
    assert best_match("", ["Google Chrome"]) is None


# ------------------------------------------------------------------- fallback chain
class RecordingEnv(LaunchEnv):
    def __init__(self, programs):
        self.programs = programs

    def which(self, name):
        n = name.lower()
        n = n if n.endswith(".exe") else n + ".exe"
        return rf"C:\Programs\{n}" if n in self.programs else None

    def system_file(self, name):
        return rf"C:\Windows\System32\{name}" if name.lower() in self.programs else None

    def shortcuts(self):
        return [("Uninstall Foo", r"C:\Start\Uninstall Foo.lnk"), ("Foo Editor", r"C:\Start\Foo Editor.lnk")]

    def store_apps(self):
        return [("Bar Player", "BarPublisher.Bar_abc!App")]

    def uri_registered(self, scheme):
        return scheme in ("https", "ms-settings")


def make_launcher(programs=("notepad.exe",), failing=(), blocked=()):
    calls = []

    def strategy(name):
        def run(cand, cwd):
            target = cand.path or cand.target
            calls.append((name, target, cand.args))
            if name in failing:
                return Attempt(name, target, False, "error 5: access is denied", code=5)
            if target.lower() in blocked:
                return Attempt(name, target, False, "error 1260: blocked by policy", code=1260)
            if cand.kind in ("uri", "storeapp") or cand.path or target.lower() in programs:
                return Attempt(name, target, True, "ok", pid=42)
            return Attempt(name, target, False, "error 2: not found", code=2, not_found=True)
        return run

    strategies = {n: strategy(n) for n in ("createprocess", "shellexecute", "cmd_start", "startfile", "explorer", "powershell")}
    return Launcher(RecordingEnv(set(programs)), strategies), calls


def test_launch_alias_resolves_to_executable_path_and_uses_createprocess_first():
    launcher, calls = make_launcher()
    report = launcher.launch("paint", "")
    assert not report.ok  # mspaint not "installed" in this env -> falls through
    launcher, calls = make_launcher(programs=("notepad.exe", "mspaint.exe"))
    report = launcher.launch("Paint")
    assert report.ok and report.candidate.path.endswith("mspaint.exe") and report.method == "createprocess"
    assert "mspaint.exe" in report.message() and "alias 'Paint'" in report.message()


def test_launch_falls_back_through_strategies():
    launcher, calls = make_launcher(programs=("notepad.exe",), failing=("createprocess", "shellexecute"))
    report = launcher.launch("notepad")
    assert report.ok and report.method == "cmd_start"
    assert [c[0] for c in calls[:3]] == ["createprocess", "shellexecute", "cmd_start"]
    assert len([a for a in report.attempts if not a.ok]) == 2


def test_launch_start_menu_and_store_fallbacks():
    launcher, calls = make_launcher(programs=())
    report = launcher.launch("foo")
    assert report.ok and report.candidate.kind == "file" and "Foo Editor" in report.candidate.label
    report = launcher.launch("bar")
    assert report.ok and report.candidate.kind == "storeapp" and report.candidate.target == "BarPublisher.Bar_abc!App"


def test_launch_program_with_arguments_and_uris():
    launcher, calls = make_launcher(programs=("notepad.exe",))
    report = launcher.launch(r"notepad C:\x.txt")
    assert report.ok and report.candidate.args == r"C:\x.txt" and report.candidate.path.endswith("notepad.exe")
    report = launcher.launch("ms-settings:display")
    assert report.ok and report.candidate.kind == "uri"
    report = launcher.launch("spotify:")
    assert not report.ok and "no application is registered" in report.error_message()
    assert "not installed" in report.diagnosis()


def test_launch_policy_block_is_fatal_and_diagnosed():
    launcher, calls = make_launcher(programs=("regedit.exe",), blocked=(r"c:\programs\regedit.exe", "regedit.exe", "regedit"))
    report = launcher.launch("regedit")
    assert not report.ok and report.fatal
    assert len(report.attempts) == 1  # no pointless retries
    msg = report.error_message()
    assert "blocked" in msg.lower() and "policy" in msg.lower()


def test_launch_not_found_message_guides_the_model():
    launcher, calls = make_launcher(programs=())
    report = launcher.launch("xyzzy")
    assert not report.ok
    msg = report.error_message()
    assert "No program with this name is installed" in msg
    assert "mspaint.exe" in msg and "pbrush" in msg
    with pytest.raises(BackendError):
        launcher.launch("")


def test_describe_win32_error():
    assert "access is denied" in describe_win32_error(5)
    assert describe_win32_error(None) == "unknown error"
    assert "1260" in describe_win32_error(1260)


# --------------------------------------------------------------------- fake backend
def test_fake_backend_launches_by_friendly_names(backend: FakeBackend):
    for name, title in [("paint", "Paint"), ("pbrush", "Paint"), ("ماشین حساب", "Calculator"), ("settings", "Settings"),
                        ("Telegram", "Telegram Desktop"), ("https://example.com", "example.com")]:
        backend.open_application(name)
        assert title in backend.active_window().title, name
    assert backend.last_launch.ok


def test_fake_backend_reports_failures(backend: FakeBackend):
    with pytest.raises(BackendError) as exc:
        backend.open_application("xyzzy")
    assert "No program with this name is installed" in str(exc.value)
    backend.blocked_programs.add("regedit.exe")
    with pytest.raises(BackendError) as exc:
        backend.open_application("regedit")
    assert "policy" in str(exc.value)
    assert backend.resolve_application("paint")[0]["target"] == "mspaint.exe"


# ------------------------------------------------------------------------ executor
def test_open_app_result_verifies_new_window(backend, config):
    ex = ToolExecutor(backend, config)
    res = ex.execute(ToolCall("open_app", {"name": "برنامه نقاشی", "wait": 0}))
    assert res.ok and res.data["launched"] is True
    assert res.data["new_windows"][0]["title"] == "Untitled - Paint"
    assert res.data["active_window"]["process_name"] == "mspaint.exe"
    assert "mspaint.exe" in res.data["message"]
    res = ex.execute(ToolCall("open_app", {"name": "pbrush2000", "wait": 0}))
    assert not res.ok and "Could not launch" in res.error and "mspaint.exe" in res.error


# -------------------------------------------------------------------------- prompt
def test_system_prompt_introduces_the_os(backend):
    info = backend.system_info()
    prompt = build_system_prompt(protocol="native", vision=True, system_info=info)
    assert "Windows 11" in prompt and "keyboard_layouts" in prompt
    assert "mspaint.exe (NOT pbrush)" in prompt
    assert "Google Chrome (chrome.exe)" in prompt
    assert "WITHOUT administrator rights" in prompt
    assert "Do NOT launch programs by pressing win" in prompt


def test_environment_section_windows_10_and_scaling():
    section = environment_section({"os": "Windows 10 Pro 22H2 build 19045", "os_build": 19045, "scale_percent": 150,
                                   "admin": True, "installed_apps": {}})
    assert "Windows 10" in section and "bottom-left" in section
    assert "150%" in section
    assert "WITHOUT administrator" not in section
    assert "built-in Windows apps" in section
    section = environment_section({"os": "Windows 11 Pro 24H2 build 26100", "os_build": 26100})
    assert "WordPad is removed" in section
