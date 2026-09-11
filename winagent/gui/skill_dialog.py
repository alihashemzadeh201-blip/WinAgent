"""Skill installer: create/edit/delete the procedure files the agent loads as "skills"."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ..skills import MAX_SKILL_CHARS, save_skill, skills_dirs, slugify_name, user_skills_dir

_TEMPLATE = """### When to use
<when this skill applies>

### Steps
1. <concrete step with exact control names / shortcuts>
2. <next step>
3. Verify on the screenshot: <what must be visible>

### Notes
- <gotchas, disabled controls, dialogs to avoid>
"""


class SkillDialog(QDialog):
    """Editor for a single skill file (name + content + target directory).

    The dialog validates and writes the file itself (``save_skill``); the caller
    only needs to refresh its display afterwards.
    """

    def __init__(self, parent=None, existing_path: str = ""):
        super().__init__(parent)
        self.setWindowTitle("WinAgent – skill" + (f": {existing_path}" if existing_path else " (new)"))
        self.resize(680, 520)
        self.saved_path: str = ""
        self._existing_path = existing_path

        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. crm-export  (file name, letters/digits/-/_)")
        self.name_edit.setToolTip("File name of the skill (without extension). Files starting with '_' or '.' "
                                  "are ignored by the loader, so they are rejected here.")
        form.addRow("Name", self.name_edit)

        self.dir_combo = QComboBox()
        try:
            dirs = skills_dirs()
        except Exception:
            dirs = []
        for d in dirs:
            p = Path(d)
            label = f"{p}" + ("  (user)" if p == user_skills_dir() else "  (project)")
            self.dir_combo.addItem(label, str(p))
        if not dirs:
            self.dir_combo.addItem(str(user_skills_dir()), str(user_skills_dir()))
        self.dir_combo.setToolTip("Skills in the user directory override same-named project skills.")
        form.addRow("Save into", self.dir_combo)
        layout.addLayout(form)

        self.body_edit = QPlainTextEdit()
        self.body_edit.setPlaceholderText(_TEMPLATE)
        self.body_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = self.body_edit.font()
        font.setFamily("Consolas")
        self.body_edit.setFont(font)
        layout.addWidget(QLabel(f"Markdown / plain text (max {MAX_SKILL_CHARS} characters; longer text is kept on "
                                "disk but truncated in the prompt)."))
        layout.addWidget(self.body_edit, 1)

        if existing_path:
            p = Path(existing_path)
            if p.is_file():
                try:
                    raw = p.read_text(encoding="utf-8", errors="replace")
                    self.name_edit.setText(p.stem)
                    self.body_edit.setPlainText(raw)
                    idx = self.dir_combo.findText(p.parent.as_posix())
                    if idx < 0:
                        idx = self.dir_combo.findData(str(p.parent))
                    if idx >= 0:
                        self.dir_combo.setCurrentIndex(idx)
                except OSError:
                    pass

        buttons_row = QVBoxLayout()
        self.load_btn = QPushButton("Load existing skill…")
        self.load_btn.clicked.connect(self._load_existing)
        self.delete_btn = QPushButton("Delete this skill file")
        self.delete_btn.clicked.connect(self._delete_existing)
        self.folder_btn = QPushButton("Open skills folder")
        self.folder_btn.clicked.connect(self._open_folder)
        hrow = QVBoxLayout()
        hrow.addWidget(self.load_btn)
        hrow.addWidget(self.delete_btn)
        hrow.addWidget(self.folder_btn)
        layout.addLayout(hrow, 0)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("Install skill")
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    # ------------------------------------------------------------------ actions
    def _load_existing(self) -> None:
        start = str(user_skills_dir())
        path, _ = QFileDialog.getOpenFileName(
            self, "Load a skill", start, "Skill files (*.md *.markdown *.txt);;All files (*)")
        if not path:
            return
        try:
            p = Path(path)
            self.name_edit.setText(p.stem)
            self.body_edit.setPlainText(p.read_text(encoding="utf-8", errors="replace"))
            idx = self.dir_combo.findData(str(p.parent))
            if idx < 0:
                self.dir_combo.addItem(str(p.parent), str(p.parent))
                idx = self.dir_combo.count() - 1
            self.dir_combo.setCurrentIndex(idx)
        except OSError as exc:
            QMessageBox.warning(self, "Could not load skill", str(exc))

    def _delete_existing(self) -> None:
        p = Path(self._existing_path) if self._existing_path else None
        if p is None or not p.is_file():
            QMessageBox.information(self, "Nothing to delete", "Load a skill file first.")
            return
        if QMessageBox.question(self, "Delete skill", f"Delete {p} ?") != QMessageBox.StandardButton.Yes:
            return
        try:
            p.unlink()
            self._existing_path = ""
            self.accept()
        except OSError as exc:
            QMessageBox.warning(self, "Delete failed", str(exc))

    def _open_folder(self) -> None:
        folder = Path(self.dir_combo.currentData() or str(user_skills_dir()))
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _save(self) -> None:
        name = self.name_edit.text().strip()
        body = self.body_edit.toPlainText()
        if not name:
            QMessageBox.warning(self, "Name required", "Give the skill a name (it becomes the file name).")
            return
        if not body.strip():
            QMessageBox.warning(self, "Content required", "The skill text is empty – nothing to install.")
            return
        dest = Path(self.dir_combo.currentData() or str(user_skills_dir()))
        try:
            path = save_skill(name, body, dest)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot install skill", str(exc))
            return
        except OSError as exc:
            QMessageBox.warning(self, "Cannot install skill", f"Writing failed: {exc}")
            return
        if slugify_name(name) != path.stem:
            self.name_edit.setText(path.stem)
        self.saved_path = str(path)
        self._existing_path = str(path)
        self.accept()
