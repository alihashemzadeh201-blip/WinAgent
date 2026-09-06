"""Settings dialog: connection, agent behaviour, screenshots, safety."""

from __future__ import annotations

import threading
from typing import Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import BACKENDS, SCREENSHOT_FORMATS, TOOL_PROTOCOLS, Config
from ..llm import LLMClient, LLMError

PRESETS: list[tuple[str, str, str]] = [
    ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
    ("OpenRouter", "https://openrouter.ai/api/v1", "openai/gpt-4o-mini"),
    ("Groq", "https://api.groq.com/openai/v1", "meta-llama/llama-4-scout-17b-16e-instruct"),
    ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("Together", "https://api.together.xyz/v1", "meta-llama/Llama-4-Scout-17B-16E-Instruct"),
    ("Google (OpenAI compat)", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash"),
    ("Ollama (local)", "http://localhost:11434/v1", "llama3.2-vision"),
    ("LM Studio (local)", "http://localhost:1234/v1", "local-model"),
    ("vLLM / custom", "http://localhost:8000/v1", "model-name"),
]


class _Worker(QObject):
    finished = Signal(bool, str)
    models = Signal(bool, object)


class SettingsDialog(QDialog):
    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("WinAgent – Settings")
        self.setMinimumWidth(640)
        self.config = config.copy()
        self._worker = _Worker()
        self._worker.finished.connect(self._on_test_done)
        self._worker.models.connect(self._on_models)
        self._build()
        self._load(self.config)

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        root = QVBoxLayout(self)
        tabs = QTabWidget()
        root.addWidget(tabs)

        # --- Connection ------------------------------------------------------
        conn = QWidget()
        form = QFormLayout(conn)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.preset = QComboBox()
        self.preset.addItem("— choose a preset —")
        for name, _url, _model in PRESETS:
            self.preset.addItem(name)
        self.preset.currentIndexChanged.connect(self._apply_preset)
        form.addRow("Provider preset", self.preset)
        self.api_base_url = QLineEdit()
        self.api_base_url.setPlaceholderText("https://api.openai.com/v1")
        form.addRow("API base URL", self.api_base_url)
        key_row = QHBoxLayout()
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("sk-…  (or set WINAGENT_API_KEY)")
        self.show_key = QPushButton("Show")
        self.show_key.setObjectName("Flat")
        self.show_key.setCheckable(True)
        self.show_key.setToolTip("Show / hide the API key")
        self.show_key.toggled.connect(lambda on: self.api_key.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        key_row.addWidget(self.api_key, 1)
        key_row.addWidget(self.show_key)
        form.addRow("API key", key_row)
        model_row = QHBoxLayout()
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.fetch_models = QPushButton("Fetch models")
        self.fetch_models.clicked.connect(self._fetch_models)
        model_row.addWidget(self.model, 1)
        model_row.addWidget(self.fetch_models)
        form.addRow("Model", model_row)
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0.0, 2.0)
        self.temperature.setSingleStep(0.1)
        form.addRow("Temperature", self.temperature)
        self.max_tokens = QSpinBox()
        self.max_tokens.setRange(0, 200000)
        self.max_tokens.setSpecialValueText("(server default)")
        form.addRow("Max tokens", self.max_tokens)
        self.request_timeout = QSpinBox()
        self.request_timeout.setRange(10, 900)
        self.request_timeout.setSuffix(" s")
        form.addRow("Request timeout", self.request_timeout)
        self.tool_protocol = QComboBox()
        self.tool_protocol.addItems(TOOL_PROTOCOLS)
        self.tool_protocol.setToolTip("auto: try native function calling, fall back to JSON-in-text.\n"
                                      "native: OpenAI tools API.\njson: model answers with a JSON object (works with any model).")
        form.addRow("Tool protocol", self.tool_protocol)
        self.extra_headers = QLineEdit()
        self.extra_headers.setPlaceholderText('optional JSON, e.g. {"HTTP-Referer": "https://myapp"}')
        form.addRow("Extra headers", self.extra_headers)
        test_row = QHBoxLayout()
        self.test_btn = QPushButton("Test connection")
        self.test_btn.clicked.connect(self._test_connection)
        self.test_label = QLabel("")
        self.test_label.setObjectName("Muted")
        self.test_label.setWordWrap(True)
        test_row.addWidget(self.test_btn)
        test_row.addWidget(self.test_label, 1)
        form.addRow("", test_row)
        tabs.addTab(conn, "Connection")

        # --- Agent ------------------------------------------------------------
        agent = QWidget()
        aform = QFormLayout(agent)
        self.max_steps = QSpinBox()
        self.max_steps.setRange(1, 500)
        aform.addRow("Max steps per task", self.max_steps)
        self.vision_enabled = QCheckBox("Send screenshots to the model (vision)")
        aform.addRow("", self.vision_enabled)
        self.auto_screenshot = QCheckBox("Automatically capture the screen after each action")
        aform.addRow("", self.auto_screenshot)
        self.action_delay = QDoubleSpinBox()
        self.action_delay.setRange(0.0, 10.0)
        self.action_delay.setSingleStep(0.1)
        self.action_delay.setSuffix(" s")
        aform.addRow("Delay before screenshot", self.action_delay)
        self.max_images = QSpinBox()
        self.max_images.setRange(1, 20)
        aform.addRow("Screenshots kept in context", self.max_images)
        self.max_history = QSpinBox()
        self.max_history.setRange(10, 1000)
        aform.addRow("Max history messages", self.max_history)
        self.response_language = QComboBox()
        self.response_language.setEditable(True)
        self.response_language.addItems(["auto", "فارسی (Persian)", "English", "العربية", "Türkçe", "Deutsch", "Français", "Español"])
        aform.addRow("Response language", self.response_language)
        self.minimize_gui = QCheckBox("Minimize this window while the agent is working")
        aform.addRow("", self.minimize_gui)
        self.extra_prompt = QPlainTextEdit()
        self.extra_prompt.setPlaceholderText("Additional instructions for the agent (optional)…")
        self.extra_prompt.setFixedHeight(90)
        aform.addRow("Extra system prompt", self.extra_prompt)
        tabs.addTab(agent, "Agent")

        # --- Screenshots -----------------------------------------------------
        shots = QWidget()
        sform = QFormLayout(shots)
        self.shot_width = QSpinBox()
        self.shot_width.setRange(320, 4096)
        self.shot_width.setSingleStep(64)
        self.shot_width.setSuffix(" px")
        sform.addRow("Max screenshot width", self.shot_width)
        self.shot_grid = QCheckBox("Draw coordinate grid")
        sform.addRow("", self.shot_grid)
        self.shot_grid_spacing = QSpinBox()
        self.shot_grid_spacing.setRange(25, 500)
        self.shot_grid_spacing.setSuffix(" px")
        sform.addRow("Grid spacing", self.shot_grid_spacing)
        self.shot_cursor = QCheckBox("Mark the mouse cursor")
        sform.addRow("", self.shot_cursor)
        self.shot_format = QComboBox()
        self.shot_format.addItems(SCREENSHOT_FORMATS)
        sform.addRow("Format", self.shot_format)
        self.shot_quality = QSpinBox()
        self.shot_quality.setRange(30, 100)
        sform.addRow("JPEG quality", self.shot_quality)
        tabs.addTab(shots, "Screenshots")

        # --- Safety ------------------------------------------------------------
        safety = QWidget()
        fform = QFormLayout(safety)
        self.confirm_dangerous = QCheckBox("Ask before destructive commands / overwriting files")
        fform.addRow("", self.confirm_dangerous)
        self.allow_shell = QCheckBox("Allow shell commands (PowerShell / cmd)")
        fform.addRow("", self.allow_shell)
        self.allow_file_write = QCheckBox("Allow writing files")
        fform.addRow("", self.allow_file_write)
        self.stop_hotkey = QLineEdit()
        self.stop_hotkey.setPlaceholderText("ctrl+alt+esc")
        fform.addRow("Emergency-stop hotkey", self.stop_hotkey)
        self.mouse_failsafe = QCheckBox("Abort when the mouse is moved to the top-left corner")
        fform.addRow("", self.mouse_failsafe)
        self.backend = QComboBox()
        self.backend.addItems(BACKENDS)
        self.backend.setToolTip("auto: real Windows desktop on Windows, simulated elsewhere.\nfake: simulated desktop for testing.")
        fform.addRow("Desktop backend", self.backend)
        self.log_level = QComboBox()
        self.log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        fform.addRow("Log level", self.log_level)
        note = QLabel("Changing the hotkey or the backend takes effect after saving.")
        note.setWordWrap(True)
        note.setObjectName("Muted")
        fform.addRow("", note)
        tabs.addTab(safety, "Safety")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # -------------------------------------------------------------- helpers
    def _apply_preset(self, index: int) -> None:
        if index <= 0:
            return
        name, url, model = PRESETS[index - 1]
        self.api_base_url.setText(url)
        if not self.model.currentText().strip() or self.model.currentText() != model:
            self.model.setEditText(model)

    def _load(self, cfg: Config) -> None:
        import json

        self.api_base_url.setText(cfg.api_base_url)
        self.api_key.setText(cfg.api_key)
        self.model.setEditText(cfg.model)
        self.temperature.setValue(cfg.temperature)
        self.max_tokens.setValue(cfg.max_tokens)
        self.request_timeout.setValue(int(cfg.request_timeout))
        self.tool_protocol.setCurrentText(cfg.tool_protocol)
        self.extra_headers.setText(json.dumps(cfg.extra_headers, ensure_ascii=False) if cfg.extra_headers else "")
        self.max_steps.setValue(cfg.max_steps)
        self.vision_enabled.setChecked(cfg.vision_enabled)
        self.auto_screenshot.setChecked(cfg.auto_screenshot_after_action)
        self.action_delay.setValue(cfg.action_delay)
        self.max_images.setValue(cfg.max_images_in_context)
        self.max_history.setValue(cfg.max_history_messages)
        self.response_language.setEditText(cfg.response_language)
        self.minimize_gui.setChecked(cfg.minimize_gui_while_running)
        self.extra_prompt.setPlainText(cfg.extra_system_prompt)
        self.shot_width.setValue(cfg.screenshot_max_width)
        self.shot_grid.setChecked(cfg.screenshot_grid)
        self.shot_grid_spacing.setValue(cfg.screenshot_grid_spacing)
        self.shot_cursor.setChecked(cfg.screenshot_show_cursor)
        self.shot_format.setCurrentText(cfg.screenshot_format)
        self.shot_quality.setValue(cfg.screenshot_jpeg_quality)
        self.confirm_dangerous.setChecked(cfg.confirm_dangerous_actions)
        self.allow_shell.setChecked(cfg.allow_shell_commands)
        self.allow_file_write.setChecked(cfg.allow_file_write)
        self.stop_hotkey.setText(cfg.stop_hotkey)
        self.mouse_failsafe.setChecked(cfg.mouse_failsafe)
        self.backend.setCurrentText(cfg.backend)
        self.log_level.setCurrentText(cfg.log_level)

    def _collect(self) -> Optional[Config]:
        import json

        cfg = self.config.copy()
        cfg.api_base_url = self.api_base_url.text().strip()
        cfg.api_key = self.api_key.text().strip()
        cfg.model = self.model.currentText().strip()
        cfg.temperature = float(self.temperature.value())
        cfg.max_tokens = int(self.max_tokens.value())
        cfg.request_timeout = float(self.request_timeout.value())
        cfg.tool_protocol = self.tool_protocol.currentText()
        headers_text = self.extra_headers.text().strip()
        if headers_text:
            try:
                headers = json.loads(headers_text)
                if not isinstance(headers, dict):
                    raise ValueError("must be a JSON object")
                cfg.extra_headers = {str(k): str(v) for k, v in headers.items()}
            except (ValueError, TypeError) as exc:
                QMessageBox.warning(self, "Invalid headers", f"Extra headers must be a JSON object: {exc}")
                return None
        else:
            cfg.extra_headers = {}
        cfg.max_steps = int(self.max_steps.value())
        cfg.vision_enabled = self.vision_enabled.isChecked()
        cfg.auto_screenshot_after_action = self.auto_screenshot.isChecked()
        cfg.action_delay = float(self.action_delay.value())
        cfg.max_images_in_context = int(self.max_images.value())
        cfg.max_history_messages = int(self.max_history.value())
        cfg.response_language = self.response_language.currentText().strip() or "auto"
        cfg.minimize_gui_while_running = self.minimize_gui.isChecked()
        cfg.extra_system_prompt = self.extra_prompt.toPlainText()
        cfg.screenshot_max_width = int(self.shot_width.value())
        cfg.screenshot_grid = self.shot_grid.isChecked()
        cfg.screenshot_grid_spacing = int(self.shot_grid_spacing.value())
        cfg.screenshot_show_cursor = self.shot_cursor.isChecked()
        cfg.screenshot_format = self.shot_format.currentText()
        cfg.screenshot_jpeg_quality = int(self.shot_quality.value())
        cfg.confirm_dangerous_actions = self.confirm_dangerous.isChecked()
        cfg.allow_shell_commands = self.allow_shell.isChecked()
        cfg.allow_file_write = self.allow_file_write.isChecked()
        cfg.stop_hotkey = self.stop_hotkey.text().strip() or "ctrl+alt+esc"
        cfg.mouse_failsafe = self.mouse_failsafe.isChecked()
        cfg.backend = self.backend.currentText()
        cfg.log_level = self.log_level.currentText()
        try:
            from ..keys import hotkey_to_vk

            hotkey_to_vk(cfg.stop_hotkey)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid hotkey", f"Emergency-stop hotkey is invalid: {exc}")
            return None
        problems = cfg.validate()
        if problems:
            QMessageBox.warning(self, "Invalid settings", "\n".join(problems))
            return None
        return cfg

    def _accept(self) -> None:
        cfg = self._collect()
        if cfg is None:
            return
        self.config = cfg
        self.accept()

    def result_config(self) -> Config:
        return self.config

    # ---------------------------------------------------------- connection
    def _test_connection(self) -> None:
        cfg = self._collect()
        if cfg is None:
            return
        self.test_btn.setEnabled(False)
        self.test_label.setText("Testing…")

        def work() -> None:
            try:
                msg = LLMClient(cfg).test_connection()
                self._worker.finished.emit(True, msg)
            except LLMError as exc:
                self._worker.finished.emit(False, str(exc))
            except Exception as exc:  # pragma: no cover
                self._worker.finished.emit(False, f"{type(exc).__name__}: {exc}")

        threading.Thread(target=work, daemon=True).start()

    def _on_test_done(self, ok: bool, msg: str) -> None:
        self.test_btn.setEnabled(True)
        color = "#3ddc97" if ok else "#ff5c5c"
        self.test_label.setStyleSheet(f"color: {color};")
        self.test_label.setText(msg)

    def _fetch_models(self) -> None:
        cfg = self._collect()
        if cfg is None:
            return
        self.fetch_models.setEnabled(False)

        def work() -> None:
            try:
                self._worker.models.emit(True, LLMClient(cfg).list_models())
            except Exception as exc:
                self._worker.models.emit(False, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_models(self, ok: bool, payload) -> None:
        self.fetch_models.setEnabled(True)
        if not ok:
            self.test_label.setStyleSheet("color: #ff5c5c;")
            self.test_label.setText(f"Could not fetch models: {payload}")
            return
        current = self.model.currentText()
        self.model.clear()
        self.model.addItems(payload)
        self.model.setEditText(current)
        self.test_label.setStyleSheet("color: #8b93a7;")
        self.test_label.setText(f"{len(payload)} models available.")
