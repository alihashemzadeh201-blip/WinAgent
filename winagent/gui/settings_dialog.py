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

from ..config import BACKENDS, COORDINATE_SPACES, GUI_MODES, OVERLAY_CORNERS, TOOL_PROTOCOLS, Config
from ..llm import LLMClient, LLMError

GUI_MODE_LABELS = {
    "overlay": "Minimise + small status overlay in a corner (recommended)",
    "visible": "Keep window visible; hide it only while a screenshot is taken",
    "minimize": "Minimise the window, show nothing",
    "none": "Leave the window where it is",
}
assert tuple(GUI_MODE_LABELS) == GUI_MODES

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
        self.setMinimumWidth(720)
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
        self.response_retries = QSpinBox()
        self.response_retries.setRange(0, 10)
        self.response_retries.setToolTip("Additional requests when model output is malformed, empty or truncated. "
                                        "Rejected responses execute no actions; successful actions are not repeated.")
        aform.addRow("Malformed-response retries", self.response_retries)
        self.vision_enabled = QCheckBox("Send screenshots to the model (vision)")
        aform.addRow("", self.vision_enabled)
        self.auto_screenshot = QCheckBox("Automatically capture the screen after each action")
        aform.addRow("", self.auto_screenshot)
        self.verify_completion = QCheckBox("Verify the result before accepting task completion")
        self.verify_completion.setToolTip(
            "Before accepting a successful completion, the agent takes a fresh screenshot and asks the model "
            "to check that the original request is really satisfied (one round: the model either confirms "
            "with task_complete or continues the work). Honest failure reports are never re-verified.")
        aform.addRow("", self.verify_completion)
        self._skills_label = QLabel("(none installed)")
        self._skills_label.setToolTip("")
        self._refresh_skills_label()
        install_btn = QPushButton("Install skill…")
        install_btn.setToolTip("Create or edit a skill: a .md/.txt procedure file the agent loads automatically "
                               "(skills/ next to WinAgent, or %APPDATA%\\WinAgent\\skills). New skills take effect "
                               "from the next request, no restart needed.")
        install_btn.clicked.connect(self._edit_skill)
        skills_row = QHBoxLayout()
        skills_row.addWidget(self._skills_label, 1)
        skills_row.addWidget(install_btn)
        aform.addRow("Installed skills", skills_row)
        self.action_delay = QDoubleSpinBox()
        self.action_delay.setRange(0.0, 10.0)
        self.action_delay.setSingleStep(0.1)
        self.action_delay.setSuffix(" s")
        aform.addRow("Delay before screenshot", self.action_delay)
        self.max_images = QSpinBox()
        self.max_images.setRange(1, 20)
        aform.addRow("Screenshots kept in context", self.max_images)
        self.preferred_layout = QComboBox()
        self.preferred_layout.setEditable(True)
        self.preferred_layout.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.preferred_layout.addItems(["en-US", "en-GB", "fa-IR", "de-DE", "fr-FR", "ar-SA", "tr-TR",
                                        "ru-RU", "it-IT", "es-ES", "pt-BR", "ja-JP", "zh-CN", "ko-KR"])
        self.preferred_layout.setToolTip("Input language the agent switches to before keyboard input (needed for "
                                         "letter shortcuts and menu mnemonics). The active layout is checked "
                                         "before every key press.")
        aform.addRow("Preferred keyboard layout", self.preferred_layout)
        self.auto_fix_layout = QCheckBox("Check and auto-correct the keyboard layout before key input")
        self.auto_fix_layout.setToolTip("Before pressing keys, the agent checks the active input language of the "
                                        "foreground window and switches it to the preferred layout when it differs "
                                        "(e.g. Persian -> English). type_text is unaffected (Unicode injection).")
        aform.addRow("", self.auto_fix_layout)
        self.max_history = QSpinBox()
        self.max_history.setRange(10, 1000)
        aform.addRow("Max history messages", self.max_history)
        self.response_language = QComboBox()
        self.response_language.setEditable(True)
        self.response_language.addItems(["auto", "فارسی (Persian)", "English", "العربية", "Türkçe", "Deutsch", "Français", "Español"])
        aform.addRow("Response language", self.response_language)
        self.gui_mode = QComboBox()
        for mode, label in GUI_MODE_LABELS.items():
            self.gui_mode.addItem(label, mode)
        self.gui_mode.setToolTip(
            "overlay: the main window is minimised and a small always-on-top status panel shows what the agent is doing.\n"
            "visible: the main window stays on screen and disappears only for the instant of a screenshot or of a click "
            "underneath it.\n"
            "minimize: minimise the window and show nothing.\n"
            "none: leave the window alone (it will appear in the agent's screenshots)."
        )
        aform.addRow("While the agent works", self.gui_mode)
        self.overlay_corner = QComboBox()
        self.overlay_corner.addItems(list(OVERLAY_CORNERS))
        aform.addRow("Status overlay corner", self.overlay_corner)
        self.overlay_exclude = QCheckBox("Exclude the overlay from screen capture (Windows 10 2004+)")
        self.overlay_exclude.setToolTip("Uses SetWindowDisplayAffinity so the overlay never appears in the agent's screenshots "
                                        "(nor in any other screen recording). When off, or on older Windows, the overlay is "
                                        "hidden for the instant of each screenshot instead.")
        aform.addRow("", self.overlay_exclude)
        self.gui_mode.currentIndexChanged.connect(self._sync_overlay_widgets)
        self.extra_prompt = QPlainTextEdit()
        self.extra_prompt.setPlaceholderText("Additional instructions for the agent (optional)…")
        self.extra_prompt.setFixedHeight(90)
        aform.addRow("Extra system prompt", self.extra_prompt)
        tabs.addTab(agent, "Agent")

        # --- Screenshots -----------------------------------------------------
        shots = QWidget()
        sform = QFormLayout(shots)
        self.shot_space = QComboBox()
        self.shot_space.addItem("Image pixels (default)", "image_pixels")
        self.shot_space.addItem("Normalized 0–1000 (Gemini-style)", "normalized_1000")
        assert tuple(self.shot_space.itemData(i) for i in range(self.shot_space.count())) == COORDINATE_SPACES
        self.shot_space.setToolTip("Explicit coordinate contract for mouse tools and screenshot regions. "
                                   "Changes the prompt, tool schemas, grid labels and conversion together. "
                                   "Model names (including Gemini aliases) never auto-select a mode. "
                                   "Test mouse_move without clicking before using a new mode.")
        sform.addRow("Coordinate space", self.shot_space)
        self.shot_width = QSpinBox()
        self.shot_width.setRange(320, 4096)
        self.shot_width.setSingleStep(64)
        self.shot_width.setSuffix(" px")
        sform.addRow("Max screenshot width", self.shot_width)
        self.shot_native = QCheckBox("Native resolution (1:1 pixels, no resizing)")
        self.shot_native.setToolTip("Sends the original capture dimensions; does NOT change Windows display resolution. "
                                   "Useful for coordinate diagnostics, but may increase image/token cost. "
                                   "The model provider may still resize images internally.")
        self.shot_native.toggled.connect(self.shot_width.setDisabled)
        sform.addRow("", self.shot_native)
        self.shot_grid = QCheckBox("Draw coordinate grid")
        sform.addRow("", self.shot_grid)
        self.shot_grid_spacing = QSpinBox()
        self.shot_grid_spacing.setRange(25, 500)
        self.shot_grid_spacing.setSuffix(" px")
        sform.addRow("Grid spacing", self.shot_grid_spacing)
        self.shot_space.currentIndexChanged.connect(self._sync_coordinate_widgets)
        self.shot_cursor = QCheckBox("Mark the mouse cursor")
        sform.addRow("", self.shot_cursor)
        self.shot_format = QComboBox()
        self.shot_format.addItem("Auto – lossless PNG for flat UI screens, JPEG for photo/3D content", "auto")
        self.shot_format.addItem("JPEG", "jpeg")
        self.shot_format.addItem("PNG (lossless)", "png")
        self.shot_format.setToolTip("Auto keeps the same quality and picks whichever encoding is best for the "
                                    "current screen: flat UI screens are sent as lossless PNG (crisp text, usually "
                                    "smaller), photo/3D content as JPEG. Quality is never lowered automatically.")
        sform.addRow("Format", self.shot_format)
        self.shot_quality = QSpinBox()
        self.shot_quality.setRange(30, 100)
        sform.addRow("JPEG quality", self.shot_quality)
        self.shot_subsampling = QComboBox()
        self.shot_subsampling.addItem("4:4:4 – best quality (default)", 0)
        self.shot_subsampling.addItem("4:2:2 – smaller, imperceptible loss", 1)
        self.shot_subsampling.addItem("4:2:0 – smallest (still no blur)", 2)
        self.shot_subsampling.setToolTip("JPEG chroma subsampling. 4:4:4 keeps full colour accuracy; 4:2:2/4:2:0 "
                                         "shrink the file with no visible quality loss for screenshots. "
                                         "Quality (above) is never lowered automatically.")
        sform.addRow("JPEG chroma subsampling", self.shot_subsampling)
        self.shot_dedupe = QCheckBox("Skip re-sending unchanged screenshots (saves tokens & time)")
        self.shot_dedupe.setToolTip("When the screen is identical to the previous full screenshot (a blinking cursor "
                                    "or clock tick is still 'unchanged'), the duplicate image is not sent to the "
                                    "model again; the tool result says the screen is unchanged instead.")
        sform.addRow("", self.shot_dedupe)
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
        self.backend.setToolTip("auto / windows: real desktop capture and control (Windows only).\n"
                                "fake: explicit demo; images and desktop actions are simulated, not your real screen.")
        fform.addRow("Desktop backend", self.backend)
        backend_note = QLabel("برای اسکرین‌شات واقعی روی ویندوز، windows یا auto را انتخاب کنید.\n"
                              "fake فقط حالت آزمایشی است و تصویر واقعی صفحهٔ شما را نمی‌گیرد.")
        backend_note.setWordWrap(True)
        fform.addRow("", backend_note)
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
        self.response_retries.setValue(cfg.max_response_retries)
        self.vision_enabled.setChecked(cfg.vision_enabled)
        self.auto_screenshot.setChecked(cfg.auto_screenshot_after_action)
        self.verify_completion.setChecked(cfg.verify_on_completion)
        self.action_delay.setValue(cfg.action_delay)
        self.max_images.setValue(cfg.max_images_in_context)
        self.preferred_layout.setEditText(cfg.preferred_keyboard_layout or "en-US")
        self.auto_fix_layout.setChecked(cfg.auto_fix_keyboard_layout)
        self.max_history.setValue(cfg.max_history_messages)
        self.response_language.setEditText(cfg.response_language)
        idx = self.gui_mode.findData(cfg.gui_mode_while_running)
        self.gui_mode.setCurrentIndex(idx if idx >= 0 else 0)
        self.overlay_corner.setCurrentText(cfg.overlay_corner)
        self.overlay_exclude.setChecked(cfg.overlay_exclude_from_capture)
        self._sync_overlay_widgets()
        self.extra_prompt.setPlainText(cfg.extra_system_prompt)
        self.shot_space.setCurrentIndex(self.shot_space.findData(cfg.coordinate_space))
        self._sync_coordinate_widgets()
        self.shot_width.setValue(cfg.screenshot_max_width)
        self.shot_native.setChecked(cfg.screenshot_native_resolution)
        self.shot_grid.setChecked(cfg.screenshot_grid)
        self.shot_grid_spacing.setValue(cfg.screenshot_grid_spacing)
        self.shot_cursor.setChecked(cfg.screenshot_show_cursor)
        self.shot_format.setCurrentIndex(max(0, self.shot_format.findData(cfg.screenshot_format)))
        self.shot_quality.setValue(cfg.screenshot_jpeg_quality)
        self.shot_subsampling.setCurrentIndex(max(0, self.shot_subsampling.findData(cfg.screenshot_jpeg_subsampling)))
        self.shot_dedupe.setChecked(cfg.dedupe_screenshots)
        self.confirm_dangerous.setChecked(cfg.confirm_dangerous_actions)
        self.allow_shell.setChecked(cfg.allow_shell_commands)
        self.allow_file_write.setChecked(cfg.allow_file_write)
        self.stop_hotkey.setText(cfg.stop_hotkey)
        self.mouse_failsafe.setChecked(cfg.mouse_failsafe)
        self.backend.setCurrentText(cfg.backend)
        self.log_level.setCurrentText(cfg.log_level)

    def _refresh_skills_label(self) -> None:
        try:
            from ..skills import load_skills
            installed = load_skills()
        except Exception:
            installed = []
        self._skills_label.setText(", ".join(s.name for s in installed) if installed else "(none installed)")
        self._skills_label.setToolTip(
            "\n".join(f"{s.name} → {s.path}" for s in installed)
            or "No skills installed. Use 'Install skill…' or drop a .md/.txt file into the skills folder "
               "(next to WinAgent, or %APPDATA%\\WinAgent\\skills). Files starting with '_' are ignored.")

    def _edit_skill(self) -> None:
        try:
            from .skill_dialog import SkillDialog
        except Exception as exc:  # pragma: no cover - Qt import issue
            QMessageBox.warning(self, "Skills unavailable", f"Could not open the skill editor: {exc}")
            return
        dlg = SkillDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh_skills_label()

    def _sync_coordinate_widgets(self) -> None:
        normalized = self.shot_space.currentData() == "normalized_1000"
        self.shot_grid_spacing.setSuffix(" /1000" if normalized else " px")

    def _sync_overlay_widgets(self) -> None:
        on = self.gui_mode.currentData() == "overlay"
        self.overlay_corner.setEnabled(on)
        self.overlay_exclude.setEnabled(on)

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
        cfg.max_response_retries = int(self.response_retries.value())
        cfg.vision_enabled = self.vision_enabled.isChecked()
        cfg.auto_screenshot_after_action = self.auto_screenshot.isChecked()
        cfg.verify_on_completion = self.verify_completion.isChecked()
        cfg.action_delay = float(self.action_delay.value())
        cfg.max_images_in_context = int(self.max_images.value())
        cfg.preferred_keyboard_layout = (self.preferred_layout.currentText() or "en-US").strip()
        cfg.auto_fix_keyboard_layout = self.auto_fix_layout.isChecked()
        cfg.max_history_messages = int(self.max_history.value())
        cfg.response_language = self.response_language.currentText().strip() or "auto"
        cfg.gui_mode_while_running = str(self.gui_mode.currentData() or GUI_MODES[0])
        cfg.overlay_corner = self.overlay_corner.currentText()
        cfg.overlay_exclude_from_capture = self.overlay_exclude.isChecked()
        cfg.extra_system_prompt = self.extra_prompt.toPlainText()
        cfg.coordinate_space = str(self.shot_space.currentData() or "image_pixels")
        cfg.screenshot_max_width = int(self.shot_width.value())
        cfg.screenshot_native_resolution = self.shot_native.isChecked()
        cfg.screenshot_grid = self.shot_grid.isChecked()
        cfg.screenshot_grid_spacing = int(self.shot_grid_spacing.value())
        cfg.screenshot_show_cursor = self.shot_cursor.isChecked()
        cfg.screenshot_format = str(self.shot_format.currentData() or "auto")
        cfg.screenshot_jpeg_quality = int(self.shot_quality.value())
        cfg.screenshot_jpeg_subsampling = int(self.shot_subsampling.currentData() or 0)
        cfg.dedupe_screenshots = self.shot_dedupe.isChecked()
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
