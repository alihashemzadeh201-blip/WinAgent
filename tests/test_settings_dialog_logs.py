"""Headless checks of the Settings dialog's log tools (Skipped when Qt cannot start)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
try:  # pragma: no cover - depends on the machine
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
except Exception as exc:  # pragma: no cover
    pytest.skip(f"Qt cannot start here: {exc}", allow_module_level=True)

from winagent.gui.settings_dialog import SettingsDialog  # noqa: E402


def test_settings_dialog_export_bundle(config, tmp_path, monkeypatch):
    import winagent.gui.settings_dialog as mod

    monkeypatch.setenv("WINAGENT_LOGS_DIR", str(tmp_path / "logs"))
    # make the app log + one session trace exist so the bundle has real content
    (tmp_path / "logs" / "sessions").mkdir(parents=True)
    (tmp_path / "logs" / "winagent.log").write_text("app log\n", encoding="utf-8")
    (tmp_path / "logs" / "sessions" / "20260101-task.jsonl").write_text(
        '{"type": "session_start"}\n', encoding="utf-8")

    infos: list = []
    monkeypatch.setattr(mod.QMessageBox, "information",
                        staticmethod(lambda *a, **k: infos.append(a)))
    monkeypatch.setattr(mod.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: pytest.fail(f"warning: {a}")))

    dlg = SettingsDialog(config)
    dlg._export_log_bundle()

    assert infos, "the success dialog must be shown"
    message = " ".join(str(x) for x in infos[0])
    bundles = list((tmp_path / "logs").glob("WinAgent-log-bundle-*.zip"))
    assert len(bundles) == 1
    assert bundles[0].name in message
