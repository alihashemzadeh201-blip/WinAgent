"""Headless checks of the skill installer dialog (Skipped when Qt cannot start)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
try:  # pragma: no cover - depends on the machine
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
except Exception as exc:  # pragma: no cover
    pytest.skip(f"Qt cannot start here: {exc}", allow_module_level=True)

from winagent.gui.skill_dialog import SkillDialog  # noqa: E402


def _point_at(dlg: SkillDialog, tmp_path):
    idx = dlg.dir_combo.findData(str(tmp_path))
    if idx < 0:
        dlg.dir_combo.addItem(str(tmp_path), str(tmp_path))
        idx = dlg.dir_combo.count() - 1
    dlg.dir_combo.setCurrentIndex(idx)


def test_skill_dialog_installs_file(tmp_path):
    dlg = SkillDialog()
    dlg.name_edit.setText("My CRM Export!")
    dlg.body_edit.setPlainText("### Steps\n1. open the CRM\n")
    _point_at(dlg, tmp_path)
    dlg._save()
    assert dlg.saved_path == str(tmp_path / "my-crm-export.md")
    assert "### Steps" in (tmp_path / "my-crm-export.md").read_text(encoding="utf-8")


def test_skill_dialog_rejects_hidden_name(tmp_path, monkeypatch):
    import winagent.gui.skill_dialog as mod

    warns = []
    monkeypatch.setattr(mod.QMessageBox, "warning", staticmethod(lambda *a, **k: warns.append(a)))
    dlg = SkillDialog()
    dlg.name_edit.setText("_hidden")
    dlg.body_edit.setPlainText("body")
    _point_at(dlg, tmp_path)
    dlg._save()
    assert warns
    assert not (tmp_path / "_hidden.md").exists()


def test_skill_dialog_refuses_empty_content(tmp_path, monkeypatch):
    import winagent.gui.skill_dialog as mod

    warns = []
    monkeypatch.setattr(mod.QMessageBox, "warning", staticmethod(lambda *a, **k: warns.append(a)))
    dlg = SkillDialog()
    dlg.name_edit.setText("ok-name")
    dlg.body_edit.setPlainText("   ")
    _point_at(dlg, tmp_path)
    dlg._save()
    assert warns
    assert list(tmp_path.iterdir()) == []


def test_skill_dialog_deletes_loaded_file(tmp_path, monkeypatch):
    import winagent.gui.skill_dialog as mod

    (tmp_path / "victim.md").write_text("old skill", encoding="utf-8")
    monkeypatch.setattr(mod.QMessageBox, "question", staticmethod(lambda *a, **k: mod.QMessageBox.StandardButton.Yes))
    dlg = SkillDialog(existing_path=str(tmp_path / "victim.md"))
    assert dlg.name_edit.text() == "victim"
    assert "old skill" in dlg.body_edit.toPlainText()
    dlg._delete_existing()
    assert not (tmp_path / "victim.md").exists()
