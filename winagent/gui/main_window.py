"""Main application window: chat, live screenshot, action log, controls."""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import __app_name__, __version__
from ..agent import Agent, AgentEvents, RunOutcome
from ..backends import DesktopBackend, create_backend
from ..config import Config, default_config_dir, save_config
from ..protocol import ToolCall
from ..screenshot import Screenshot
from ..tools import ToolResult
from .overlay import QtScreenGuard, StatusOverlay
from .settings_dialog import SettingsDialog
from .theme import PALETTE, STYLESHEET
from .widgets import ChatInput, ChatView, KeyValueLabel, ScreenshotView

log = logging.getLogger(__name__)

WELCOME = (
    "سلام! من **WinAgent** هستم؛ یک ایجنت هوش مصنوعی که می‌تواند کامپیوتر ویندوزی شما را کنترل کند: "
    "باز کردن برنامه‌ها، کار با ماوس و کیبورد، دیدن و تحلیل صفحه، اجرای دستورات و … \n\n"
    "چند نمونه دستور:\n"
    "• «Notepad را باز کن و متن سلام دنیا را بنویس»\n"
    "• «مرورگر را باز کن و قیمت دلار را جستجو کن»\n"
    "• «همهٔ پنجره‌ها را کوچک کن»\n"
    "• «یک اسکرین‌شات بگیر و بگو روی صفحه چه چیزی می‌بینی»\n\n"
    "⚠ برای توقف اضطراری هر زمان **Ctrl+Alt+Esc** را بزنید یا دکمهٔ Stop را فشار دهید."
)


class _Bridge(QObject):
    """Thread-safe bridge: worker thread -> Qt main thread."""

    status = Signal(str)
    thought = Signal(str)
    assistant_text = Signal(str)
    tool_start = Signal(object)
    tool_end = Signal(object)
    screenshot = Signal(object)
    step = Signal(int, int)
    error = Signal(str)
    done = Signal(object)
    ask_user = Signal(str, list)
    confirm = Signal(object, str)


class MainWindow(QMainWindow):
    def __init__(self, config: Config, config_path: Optional[Path] = None, backend_override: Optional[str] = None):
        super().__init__()
        self.config = config
        self.config_path = config_path
        self.backend_override = backend_override
        self.backend: Optional[DesktopBackend] = None
        self.agent: Optional[Agent] = None
        self.worker: Optional[threading.Thread] = None
        self._answer_box: "queue.Queue[Optional[str]]" = queue.Queue()
        self._confirm_box: "queue.Queue[bool]" = queue.Queue()
        self._pending_question = False
        self._current_tool_bubble = None
        self._task_started = 0.0
        self._last_shot: Optional[Screenshot] = None
        self._screenshots_dir = default_config_dir() / "screenshots"
        # Rotating file log for the GUI process (console stays clean; the app has its own status UI).
        from ..logging_setup import setup_logging
        setup_logging(config.log_level, console=False)
        self.bridge = _Bridge()
        self._connect_bridge()
        self.setWindowTitle(f"{__app_name__} v{__version__}")
        self.resize(1280, 800)
        # keeps our own windows out of the agent's screenshots and from under its mouse/keyboard
        self.guard = QtScreenGuard(self._guarded_windows, self)
        self.overlay = StatusOverlay(corner=self.config.overlay_corner, exclude_from_capture=self.config.overlay_exclude_from_capture)
        self.overlay.stop_requested.connect(self.stop_agent)
        self.overlay.show_main_requested.connect(self.restore_window)
        self._build_ui()
        self.chat.add("assistant", WELCOME.replace("Ctrl+Alt+Esc", self.config.stop_hotkey.title()))
        self._init_backend()
        if not self.config.api_key and "localhost" not in self.config.api_base_url and "127.0.0.1" not in self.config.api_base_url:
            self.chat.add("system", "هنوز API key تنظیم نشده است. از منوی ⚙ Settings اتصال به مدل زبانی را پیکربندی کنید.")
        self._refresh_info()

    # ================================================================== UI
    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(8)

        # toolbar -------------------------------------------------------------
        bar = QHBoxLayout()
        title = QLabel(f"{__app_name__}")
        title.setObjectName("Title")
        bar.addWidget(title)
        self.model_label = QLabel("")
        self.model_label.setObjectName("Muted")
        bar.addWidget(self.model_label)
        bar.addStretch(1)
        self.btn_new = QPushButton("+ New chat")
        self.btn_new.clicked.connect(self.new_chat)
        self.btn_screenshot = QPushButton("Screenshot")
        self.btn_screenshot.clicked.connect(self.manual_screenshot)
        self.btn_save = QPushButton("Save transcript")
        self.btn_save.clicked.connect(self.save_transcript)
        self.btn_settings = QPushButton("⚙ Settings")
        self.btn_settings.clicked.connect(self.open_settings)
        for b in (self.btn_new, self.btn_screenshot, self.btn_save, self.btn_settings):
            bar.addWidget(b)
        outer.addLayout(bar)

        # main split ----------------------------------------------------------
        split = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(split, 1)

        # left: chat
        left = QFrame()
        left.setObjectName("Panel")
        lv = QVBoxLayout(left)
        lv.setContentsMargins(6, 6, 6, 6)
        lv.setSpacing(6)
        self.chat = ChatView()
        lv.addWidget(self.chat, 1)

        opts = QHBoxLayout()
        self.chk_thoughts = QCheckBox("Show reasoning")
        self.chk_thoughts.setChecked(True)
        self.chk_thoughts.toggled.connect(lambda on: self.chat.set_visibility(thoughts=on))
        self.chk_tools = QCheckBox("Show actions")
        self.chk_tools.setChecked(True)
        self.chk_tools.toggled.connect(lambda on: self.chat.set_visibility(tools=on))
        opts.addWidget(self.chk_thoughts)
        opts.addWidget(self.chk_tools)
        opts.addStretch(1)
        self.step_label = QLabel("")
        self.step_label.setObjectName("Muted")
        opts.addWidget(self.step_label)
        lv.addLayout(opts)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFixedHeight(6)
        lv.addWidget(self.progress)

        input_row = QHBoxLayout()
        self.input = ChatInput()
        self.input.submitted.connect(self.send_message)
        input_row.addWidget(self.input, 1)
        btns = QVBoxLayout()
        self.btn_send = QPushButton("Send ➤")
        self.btn_send.setObjectName("Primary")
        self.btn_send.clicked.connect(lambda: self.input.submitted.emit(self.input.toPlainText().strip()) if self.input.toPlainText().strip() else None)
        self.btn_stop = QPushButton("■ Stop")
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_agent)
        btns.addWidget(self.btn_send)
        btns.addWidget(self.btn_stop)
        input_row.addLayout(btns)
        lv.addLayout(input_row)
        split.addWidget(left)

        # right: screenshot + info
        right = QSplitter(Qt.Orientation.Vertical)
        shot_panel = QFrame()
        shot_panel.setObjectName("Panel")
        sv = QVBoxLayout(shot_panel)
        sv.setContentsMargins(6, 6, 6, 6)
        head = QHBoxLayout()
        head.addWidget(QLabel("<b>Agent's view</b>"))
        self.shot_label = QLabel("")
        self.shot_label.setObjectName("Muted")
        head.addWidget(self.shot_label, 1)
        self.btn_open_shot = QPushButton("Open")
        self.btn_open_shot.setObjectName("Flat")
        self.btn_open_shot.setEnabled(False)
        self.btn_open_shot.clicked.connect(self.open_screenshot_window)
        head.addWidget(self.btn_open_shot)
        sv.addLayout(head)
        # Persistent source label: task/status updates and New chat must not hide demo mode.
        self.backend_label = QLabel()
        self.backend_label.setTextFormat(Qt.TextFormat.PlainText)
        self.backend_label.setWordWrap(True)
        sv.addWidget(self.backend_label)
        self.shot_view = ScreenshotView()
        self.shot_view.clicked.connect(self.open_screenshot_window)
        sv.addWidget(self.shot_view, 1)
        right.addWidget(shot_panel)

        info_tabs = QTabWidget()
        # actions log
        self.actions = QTreeWidget()
        self.actions.setHeaderLabels(["#", "Tool", "Arguments", "Result", "ms"])
        self.actions.setRootIsDecorated(False)
        self.actions.setAlternatingRowColors(False)
        self.actions.header().setStretchLastSection(False)
        self.actions.setColumnWidth(0, 36)
        self.actions.setColumnWidth(1, 120)
        self.actions.setColumnWidth(2, 260)
        self.actions.setColumnWidth(3, 260)
        self.actions.setColumnWidth(4, 60)
        info_tabs.addTab(self.actions, "Actions")
        # system info
        info_scroll = QScrollArea()
        info_scroll.setWidgetResizable(True)
        info_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.info_label = KeyValueLabel()
        info_scroll.setWidget(self.info_label)
        info_tabs.addTab(info_scroll, "System")
        # windows
        self.windows_tree = QTreeWidget()
        self.windows_tree.setHeaderLabels(["Title", "Process", "Position", "Size"])
        self.windows_tree.setRootIsDecorated(False)
        self.windows_tree.setColumnWidth(0, 320)
        self.windows_tree.setColumnWidth(1, 120)
        info_tabs.addTab(self.windows_tree, "Windows")
        info_tabs.currentChanged.connect(lambda i: self._refresh_info() if i in (1, 2) else None)
        right.addWidget(info_tabs)
        right.setSizes([480, 300])
        split.addWidget(right)
        split.setSizes([640, 640])

        # status bar ----------------------------------------------------------
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("Status")
        self.status.addWidget(self.status_label, 1)
        self.token_label = QLabel("")
        self.token_label.setObjectName("Status")
        self.status.addPermanentWidget(self.token_label)

        # shortcuts -----------------------------------------------------------
        act_settings = QAction(self)
        act_settings.setShortcut(QKeySequence("Ctrl+,"))
        act_settings.triggered.connect(self.open_settings)
        self.addAction(act_settings)
        act_stop = QAction(self)
        act_stop.setShortcut(QKeySequence("Escape"))
        act_stop.triggered.connect(lambda: self.stop_agent() if self.is_running() else None)
        self.addAction(act_stop)
        act_new = QAction(self)
        act_new.setShortcut(QKeySequence("Ctrl+N"))
        act_new.triggered.connect(self.new_chat)
        self.addAction(act_new)

    def _connect_bridge(self) -> None:
        b = self.bridge
        b.status.connect(self._on_status)
        b.thought.connect(self._on_thought)
        b.assistant_text.connect(self._on_assistant_text)
        b.tool_start.connect(self._on_tool_start)
        b.tool_end.connect(self._on_tool_end)
        b.screenshot.connect(self._on_screenshot)
        b.step.connect(self._on_step)
        b.error.connect(self._on_error)
        b.done.connect(self._on_done)
        b.ask_user.connect(self._on_ask_user)
        b.confirm.connect(self._on_confirm)

    # ============================================================= backend
    def _init_backend(self) -> None:
        kind = self.backend_override or self.config.backend
        if self.backend is not None:
            try:
                self.backend.close()
            except Exception:
                log.exception("backend close failed")
        # A new desktop must not inherit the old one's images, window handles or action history.
        self.backend = None
        self.agent = None
        self._clear_screenshot()
        try:
            self.backend = create_backend(kind, stop_hotkey=self.config.stop_hotkey, on_emergency_stop=self._emergency_stop)
        except Exception as exc:
            log.exception("backend init failed (requested: %s)", kind)
            self._rebuild_agent()
            self._clear_screenshot("دسترسی به دسکتاپ برقرار نیست؛ تنظیمات بک‌اند را بررسی کنید.")
            message = (f"Could not initialise desktop backend '{kind}':\n{exc}\n\n"
                       "No simulated fallback was used. Open Settings → Safety → Desktop backend "
                       "to choose windows for a real Windows desktop, or fake for an explicit demo.")
            self.backend_label.setToolTip(str(exc))
            self.status_label.setText("Desktop unavailable — check Settings")
            self.chat.add("error", message)
            QMessageBox.critical(self, "Backend error", message)
            return
        self._rebuild_agent()
        log.info("Desktop backend: %s (requested: %s)", self.backend.name, kind)
        if self.backend.name == "fake":
            self.chat.add("system", "⚠ DEMO: تصویر آبی، دسکتاپ شبیه‌سازی‌شده است، نه اسکرین‌شات صفحهٔ شما. "
                                    "برای تصویر واقعی روی ویندوز، در Settings → Safety → Desktop backend گزینهٔ windows را انتخاب کنید.")
        elif getattr(self.backend, "hotkey_registered", True) is False:
            self.chat.add("system", f"⚠ The emergency-stop hotkey '{self.config.stop_hotkey}' could not be registered "
                                    "(already used by another program). Use the Stop button or the mouse fail-safe instead.")

    def _rebuild_agent(self) -> None:
        events = AgentEvents(
            on_status=self.bridge.status.emit,
            on_thought=self.bridge.thought.emit,
            on_assistant_text=self.bridge.assistant_text.emit,
            on_tool_start=self.bridge.tool_start.emit,
            on_tool_end=self.bridge.tool_end.emit,
            on_screenshot=self.bridge.screenshot.emit,
            on_step=self.bridge.step.emit,
            on_error=self.bridge.error.emit,
            on_done=self.bridge.done.emit,
            ask_user=self._ask_user_blocking,
            confirm=self._confirm_blocking,
        )
        history = self.agent.followup_history() if self.agent else []
        self.guard.enabled = self.backend is not None and self.gui_mode != "none"
        self.agent = Agent(self.config, self.backend, events=events, guard=self.guard) if self.backend is not None else None
        if self.agent is not None:
            self.agent.history = history
        self.model_label.setText(f"·  {self.config.model}  @  {self.config.api_base_url}")
        self._update_backend_label()
        self._set_running(False)

    def _update_backend_label(self) -> None:
        title = f"{__app_name__} v{__version__}"
        if self.backend is None:
            text = "Desktop unavailable — Settings → Safety → Desktop backend"
            color = PALETTE["error"]
            title += " — Desktop unavailable"
        elif self.backend.name == "fake":
            text = ("DEMO · fake — simulated desktop, not your screen.\n"
                    "برای اسکرین‌شات واقعی روی ویندوز: Settings → Safety → Desktop backend → windows")
            color = PALETTE["warn"]
            title += " — DEMO"
        else:
            text = f"Capture source: {self.backend.name} · real desktop"
            color = PALETTE["success"]
        self.backend_label.setText(text)
        self.backend_label.setToolTip(text)
        self.backend_label.setStyleSheet(f"color: {color}; padding: 4px;")
        self.setWindowTitle(title)

    def _clear_screenshot(self, text: str = "هنوز اسکرین‌شاتی گرفته نشده است") -> None:
        self._last_shot = None
        self.shot_view.clear_image(text)
        self.shot_label.clear()
        self.shot_label.setToolTip("")
        self.btn_open_shot.setEnabled(False)

    def _emergency_stop(self) -> None:
        """Called from the hot-key thread."""
        if self.agent:
            self.agent.stop()
        self.bridge.status.emit("■ Emergency stop (hotkey)")

    # ================================================================ chat
    def is_running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    @Slot(str)
    def send_message(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        if self._pending_question:
            # answer to the agent's question
            self.chat.add("user", text)
            self._pending_question = False
            self.btn_send.setEnabled(False)
            self._answer_box.put(text)
            self.input.clear()
            self.overlay.set_paused(False)
            if self.is_running() and self.gui_mode in ("overlay", "minimize"):
                self.showMinimized()
            return
        if self.is_running():
            QMessageBox.information(self, "Busy", "The agent is still working. Press Stop to interrupt it.")
            return
        if self.agent is None:
            QMessageBox.warning(self, "Desktop unavailable", "Choose a working desktop backend in Settings → Safety first.")
            return
        problems = self.config.validate()
        if problems:
            QMessageBox.warning(self, "Settings", "Please fix the settings first:\n" + "\n".join(problems))
            self.open_settings()
            return
        self.chat.add("user", text)
        self.input.clear()
        self._set_running(True)
        self._task_started = time.time()
        self.actions.clear()
        self.step_label.setText("")
        self._enter_working_mode()

        def work() -> None:
            try:
                self.agent.run(text)
            except Exception as exc:  # pragma: no cover
                log.exception("worker crashed")
                self.bridge.error.emit(f"{type(exc).__name__}: {exc}")
                self.bridge.done.emit(RunOutcome("error", str(exc)))

        self.worker = threading.Thread(target=work, name="winagent-worker", daemon=True)
        self.worker.start()

    def stop_agent(self) -> None:
        if self.agent:
            self.agent.stop()
        if self._pending_question:
            self._pending_question = False
            self._answer_box.put(None)
        self._on_status("Stopping…")

    def new_chat(self) -> None:
        if self.is_running():
            if QMessageBox.question(self, "Stop task?", "A task is running. Stop it and start a new chat?") != QMessageBox.StandardButton.Yes:
                return
            self.stop_agent()
        if self.agent:
            self.agent.reset()
        self.chat.clear()
        self.actions.clear()
        self.chat.add("assistant", WELCOME)

    def _set_running(self, running: bool) -> None:
        self.btn_send.setEnabled(not running and self.agent is not None)
        # Do not race a manual capture against the worker's screenshot coordinate frame / screen guard.
        self.btn_screenshot.setEnabled(not running and self.agent is not None)
        self.btn_stop.setEnabled(running)
        self.btn_new.setEnabled(True)
        self.btn_settings.setEnabled(not running)
        self.progress.setRange(0, 0 if running else 1)
        if not running:
            self.progress.setValue(0)
        # while a task runs, a window we hid for a screenshot must come back WITHOUT taking the focus away from the
        # application the agent is operating; when idle (manual screenshot button) we want the focus back
        self.guard.reactivate = not running

    # ------------------------------------------------ window management
    #: set to True to apply the configured mode even on the simulated backend (tests / demos)
    force_gui_mode = False

    @property
    def gui_mode(self) -> str:
        """Effective GUI behaviour while the agent works (see ``Config.gui_mode_while_running``)."""
        mode = (self.config.gui_mode_while_running or "overlay").lower()
        if not self.force_gui_mode and (self.backend is None or self.backend.name != "windows"):
            # nothing to hide from on a simulated desktop; keep the overlay so its behaviour can be seen in demos
            return "overlay" if mode == "overlay" else "none"
        return mode

    def _guarded_windows(self) -> list[QWidget]:
        """Our top-level windows that must never end up in a screenshot or under the agent's pointer.

        Dialogs are deliberately left out: hiding a dialog that runs ``exec()`` would dismiss it, and the
        agent's own questions/confirmations block the worker anyway, so no screenshot is taken meanwhile.
        """
        wins: list[QWidget] = [self]
        if self.overlay.isVisible():
            wins.append(self.overlay)   # skipped for captures when the OS excludes it (overlay.capture_excluded)
        return wins

    def _enter_working_mode(self) -> None:
        mode = self.gui_mode
        self.guard.enabled = mode != "none"
        if mode in ("overlay", "minimize") and not self.isMinimized():
            self.showMinimized()
        if mode == "overlay":
            self.overlay.start()
        else:
            self.overlay.finish()
        # "visible": the window stays; the guard hides it for the instant of a screenshot / click underneath

    def _leave_working_mode(self) -> None:
        self.overlay.finish()
        if self.gui_mode == "none":
            return
        if self.isMinimized() or not self.isVisible():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    @Slot()
    def restore_window(self) -> None:
        """Bring the main window back (overlay button / questions / confirmations)."""
        if self.isMinimized() or not self.isVisible():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------ bridge slots
    @Slot(str)
    def _on_status(self, msg: str) -> None:
        self.status_label.setText(msg)
        if self.overlay.isVisible():
            self.overlay.set_status(msg)

    @Slot(str)
    def _on_thought(self, text: str) -> None:
        if text.strip():
            self.chat.add("thought", text.strip()[:2000])

    @Slot(str)
    def _on_assistant_text(self, text: str) -> None:
        if text.strip():
            self.chat.add("assistant", text.strip())

    @Slot(object)
    def _on_tool_start(self, call: ToolCall) -> None:
        args = json.dumps(call.arguments, ensure_ascii=False)
        if len(args) > 300:
            args = args[:300] + "…"
        self._current_tool_bubble = self.chat.add("tool", f"`{call.name}` {args}", title="Action")
        if self.overlay.isVisible():
            self.overlay.set_tool(f"{call.name} {args}"[:160])
        row = QTreeWidgetItem([str(self.actions.topLevelItemCount() + 1), call.name, args, "…", ""])
        row.setToolTip(2, json.dumps(call.arguments, ensure_ascii=False, indent=2))
        self.actions.addTopLevelItem(row)
        self.actions.scrollToBottom()

    @Slot(object)
    def _on_tool_end(self, result: ToolResult) -> None:
        summary = result.summary()
        bubble = self._current_tool_bubble
        if bubble is not None and bubble.text().startswith(f"`{result.call.name}`"):
            status = "✔" if result.ok else ("⊘" if result.denied else "✖")
            bubble.set_text(bubble.text() + f"\n{status} {summary}")
            if not result.ok:
                bubble.setStyleSheet(f"QFrame#Bubble {{ background: {PALETTE['tool_err']}; border-radius: 12px; border: 1px solid {PALETTE['border']}; }}")
        item = self.actions.topLevelItem(self.actions.topLevelItemCount() - 1)
        if item is not None and item.text(1) == result.call.name:
            item.setText(3, ("OK: " if result.ok else "ERR: ") + summary)
            item.setToolTip(3, result.to_text()[:4000])
            item.setText(4, f"{int(result.duration * 1000)}")
            if not result.ok:
                item.setForeground(3, Qt.GlobalColor.red)
        self._current_tool_bubble = None
        if self.overlay.isVisible():
            self.overlay.set_tool(f"{'✔' if result.ok else '✖'} {result.call.name}: {summary}"[:160])
        if result.call.name in ("open_app", "window_action", "focus_window", "list_windows"):
            QTimer.singleShot(300, self._refresh_windows)

    @Slot(object)
    def _on_screenshot(self, shot: Screenshot) -> None:
        self._last_shot = shot
        self.shot_view.set_image(shot.image)
        self.btn_open_shot.setEnabled(True)
        w, h = shot.size
        mode = "1:1" if shot.size == shot.raw_size else "scaled"
        if shot.coordinate_space == "normalized_1000":
            mode += "; coords 0–1000"
        self.shot_label.setText(f"{w}×{h}  (raw {shot.raw_size[0]}×{shot.raw_size[1]}, {mode})  "
                                f"{time.strftime('%H:%M:%S', time.localtime(shot.taken_at))}")
        self.shot_label.setToolTip(shot.describe() + f" Origin={shot.origin}; desktop bounds={shot.desktop_bounds}.")

    @Slot(int, int)
    def _on_step(self, step: int, max_steps: int) -> None:
        self.step_label.setText(f"step {step}/{max_steps}")
        self.progress.setRange(0, 0)
        if self.overlay.isVisible():
            self.overlay.set_step(step, max_steps)

    @Slot(str)
    def _on_error(self, msg: str) -> None:
        self.chat.add("error", msg)

    @Slot(object)
    def _on_done(self, outcome: RunOutcome) -> None:
        self._set_running(False)
        self._pending_question = False
        elapsed = time.time() - self._task_started if self._task_started else outcome.duration
        summary = {
            "completed": "✔ Task completed",
            "answered": "Answered",
            "stopped": "■ Stopped",
            "error": "✖ Error",
            "max_steps": "⚠ Step limit reached",
            "waiting_user": "❚❚ Paused – waiting for you",
        }.get(outcome.status, outcome.status)
        self.status_label.setText(f"{summary} · {outcome.steps} steps · {outcome.tool_calls} actions · {elapsed:.1f}s")
        usage = outcome.usage or {}
        self.token_label.setText(f"tokens: {usage.get('prompt_tokens', 0)} in / {usage.get('completion_tokens', 0)} out")
        if outcome.status == "max_steps":
            self.chat.add("system", outcome.message)
        if outcome.trace_file and outcome.status in ("error", "max_steps"):
            self.chat.add("system",
                          f"این جلسه یک لاگ کامل از ریکوست/ریسپانس مدل دارد (برای ارسال به پشتیبانی): {outcome.trace_file}")
        self._leave_working_mode()
        self._refresh_info()

    # ------------------------------------------------- blocking callbacks
    def _ask_user_blocking(self, question: str, options: list[str]) -> Optional[str]:
        """Runs in the worker thread: show the question in the chat and wait for the answer."""
        while not self._answer_box.empty():
            try:
                self._answer_box.get_nowait()
            except queue.Empty:
                break
        self.bridge.ask_user.emit(question, list(options))
        while True:
            try:
                answer = self._answer_box.get(timeout=0.25)
                return answer
            except queue.Empty:
                if self.agent and self.agent.stop_event.is_set():
                    return None

    @Slot(str, list)
    def _on_ask_user(self, question: str, options: list) -> None:
        self._pending_question = True
        self.btn_send.setEnabled(True)
        self.overlay.set_paused(True)
        self.restore_window()
        hint = "پاسخ خود را در کادر پایین بنویسید و Enter بزنید."
        if options:
            hint += "  گزینه‌ها: " + " | ".join(options)
        self.chat.add("system", hint)
        self.input.setFocus()

    def _confirm_blocking(self, call: ToolCall, reason: str) -> bool:
        while not self._confirm_box.empty():
            try:
                self._confirm_box.get_nowait()
            except queue.Empty:
                break
        self.bridge.confirm.emit(call, reason)
        while True:
            try:
                return self._confirm_box.get(timeout=0.25)
            except queue.Empty:
                if self.agent and self.agent.stop_event.is_set():
                    return False

    @Slot(object, str)
    def _on_confirm(self, call: ToolCall, reason: str) -> None:
        self.overlay.set_paused(True, "WinAgent needs your confirmation")
        self.restore_window()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Confirm action")
        box.setText(f"The agent wants to run <b>{call.name}</b>:")
        box.setInformativeText(reason)
        box.setDetailedText(json.dumps(call.arguments, ensure_ascii=False, indent=2))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        ok = box.exec() == QMessageBox.StandardButton.Yes
        self.chat.add("system", ("✔ Allowed: " if ok else "⊘ Denied: ") + call.name)
        self._confirm_box.put(ok)
        self.overlay.set_paused(False)
        if self.is_running() and self.gui_mode in ("overlay", "minimize"):
            self.showMinimized()   # get out of the way again

    # ============================================================= actions
    def manual_screenshot(self) -> None:
        if not self.agent or self.is_running():
            return
        try:
            shot = self.agent.executor.take_screenshot()
        except Exception as exc:
            log.exception("manual screenshot failed")
            self._clear_screenshot("اسکرین‌شات ناموفق بود؛ خطای ثبت‌شده را بررسی کنید.")
            self.chat.add("error", f"Screen capture failed: {exc}")
            QMessageBox.warning(self, "Screenshot", str(exc))
            return
        self._on_screenshot(shot)
        self._refresh_info()

    def open_screenshot_window(self) -> None:
        pix = self.shot_view.pixmap_full()
        if pix is None:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Screenshot")
        lay = QVBoxLayout(dlg)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        lbl = QLabel()
        lbl.setPixmap(pix)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll.setWidget(lbl)
        lay.addWidget(scroll)
        row = QHBoxLayout()
        btn_save = QPushButton("Save as…")

        def save() -> None:
            path, _ = QFileDialog.getSaveFileName(dlg, "Save screenshot", str(Path.home() / "screenshot.png"), "PNG (*.png);;JPEG (*.jpg)")
            if path:
                pix.save(path)

        btn_save.clicked.connect(save)
        row.addStretch(1)
        row.addWidget(btn_save)
        lay.addLayout(row)
        dlg.resize(min(pix.width() + 40, 1400), min(pix.height() + 90, 900))
        dlg.exec()

    def save_transcript(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save transcript", str(Path.home() / "winagent-chat.md"), "Markdown (*.md);;Text (*.txt)")
        if not path:
            return
        Path(path).write_text(self.chat.transcript(), encoding="utf-8")
        self.status_label.setText(f"Transcript saved to {path}")

    def open_settings(self) -> None:
        if self.is_running():
            return
        # Show the actual selection, including --demo / --backend, rather than an inactive config value.
        settings_cfg = self.config.copy()
        settings_cfg.backend = self.backend_override or self.config.backend
        dlg = SettingsDialog(settings_cfg, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_cfg = dlg.result_config()
        selection_changed = new_cfg.backend != settings_cfg.backend
        backend_changed = selection_changed or new_cfg.stop_hotkey != self.config.stop_hotkey or self.backend is None
        if selection_changed:
            self.backend_override = None   # an explicit GUI choice supersedes the launch flag
        elif self.backend_override:
            # Saving an API key must not accidentally persist a temporary --demo session as backend=fake.
            new_cfg.backend = self.config.backend
        self.config = new_cfg
        try:
            path = save_config(self.config, self.config_path)
            self.config_path = path
            self.status_label.setText(f"Settings saved to {path}")
        except OSError as exc:
            QMessageBox.warning(self, "Settings", f"Could not save settings: {exc}")
        from ..logging_setup import setup_logging
        setup_logging(self.config.log_level, console=False)   # idempotent; also (re)creates the file handler
        self.overlay.configure(self.config.overlay_corner, self.config.overlay_exclude_from_capture)
        if backend_changed:
            self._init_backend()
        else:
            self._rebuild_agent()
        self._refresh_info()

    # ================================================================ info
    def _refresh_info(self) -> None:
        if not self.backend:
            self.info_label.set_pairs([("backend", "unavailable"), ("requested backend", self.backend_override or self.config.backend)])
            self.windows_tree.clear()
            return
        try:
            info = self.backend.system_info()
            pairs = []
            for k, v in info.items():
                if k == "installed_apps" and isinstance(v, dict):
                    v = ", ".join(v) if v else "-"
                elif isinstance(v, (dict, list)):
                    v = json.dumps(v, ensure_ascii=False)
                pairs.append((k, v))
            pairs.append(("backend", self.backend.name))
            pairs.append(("model", self.config.model))
            pairs.append(("endpoint", self.config.api_base_url))
            pairs.append(("protocol", self.agent.protocol if self.agent else "-"))
            pairs.append(("config file", str(self.config_path or "-")))
            self.info_label.set_pairs(pairs)
        except Exception as exc:  # pragma: no cover
            self.info_label.set_pairs([("error", str(exc))])
        self._refresh_windows()

    def _refresh_windows(self) -> None:
        if not self.backend:
            return
        try:
            wins = self.backend.list_windows()
        except Exception:
            return
        self.windows_tree.clear()
        for w in wins[:80]:
            item = QTreeWidgetItem([w.title, w.process_name, f"{w.left},{w.top}", f"{w.width}×{w.height}"])
            if w.is_active:
                item.setText(0, "▶ " + w.title)
            self.windows_tree.addTopLevelItem(item)

    # ============================================================== close
    def closeEvent(self, event: QCloseEvent) -> None:
        if self.is_running():
            if QMessageBox.question(self, "Quit", "The agent is still working. Stop it and quit?") != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.stop_agent()
        self.guard.enabled = False   # never leave the worker waiting for a GUI thread that is going away
        self.overlay.finish()
        if self.backend:
            try:
                self.backend.close()
            except Exception:
                pass
        event.accept()


def run_gui(config: Config, config_path: Optional[Path] = None, backend_override: Optional[str] = None) -> int:
    # DPI mode must be selected before Qt creates any native windows; backend init alone is too late.
    from ..backends.windows import make_dpi_aware

    make_dpi_aware()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(__app_name__)
    app.setStyleSheet(STYLESHEET)
    win = MainWindow(config, config_path=config_path, backend_override=backend_override)
    win.show()
    return app.exec()
