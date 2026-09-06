"""Reusable widgets: chat bubbles, screenshot viewer, chat input."""

from __future__ import annotations

import html
import re
from typing import Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QImage, QKeyEvent, QPixmap, QTextOption
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .theme import PALETTE

_RTL_RE = re.compile(r"[\u0590-\u05FF\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")


def is_rtl(text: str) -> bool:
    """Heuristic: text is RTL when its first strong character is Hebrew/Arabic."""
    for ch in text:
        if _RTL_RE.match(ch):
            return True
        if ch.isalpha():
            return False
    return False


def _md_to_html(text: str) -> str:
    """A tiny markdown subset (bold, code, code blocks, line breaks, lists)."""
    esc = html.escape(text)
    esc = re.sub(r"```(?:\w+)?\n(.*?)```", lambda m: f"<pre style='background:#0f1115;padding:8px;border-radius:6px;white-space:pre-wrap'>{m.group(1)}</pre>", esc, flags=re.S)
    esc = re.sub(r"`([^`\n]+)`", r"<code style='background:#0f1115;padding:1px 4px;border-radius:4px'>\1</code>", esc)
    esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
    esc = re.sub(r"(?m)^\s*[-•]\s+(.*)$", r"&nbsp;&nbsp;• \1", esc)
    esc = re.sub(r"(?m)^\s*(\d+)\.\s+(.*)$", r"&nbsp;&nbsp;\1. \2", esc)
    esc = esc.replace("\n", "<br>")
    esc = esc.replace("<pre style='background:#0f1115;padding:8px;border-radius:6px;white-space:pre-wrap'>", "<pre style='background:#0f1115;padding:8px;border-radius:6px;white-space:pre-wrap'>")
    return esc


class Bubble(QFrame):
    """A single chat message."""

    KIND_COLORS = {
        "user": PALETTE["user"],
        "assistant": PALETTE["assistant"],
        "tool": PALETTE["tool"],
        "tool_error": PALETTE["tool_err"],
        "thought": PALETTE["thought"],
        "system": PALETTE["panel2"],
        "error": PALETTE["tool_err"],
    }
    KIND_LABEL = {
        "user": "You", "assistant": "WinAgent", "tool": "Action", "tool_error": "Action failed",
        "thought": "Thinking", "system": "System", "error": "Error",
    }

    def __init__(self, kind: str, text: str, *, title: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.kind = kind
        self._text = text
        self.setObjectName("Bubble")
        color = self.KIND_COLORS.get(kind, PALETTE["assistant"])
        self.setStyleSheet(f"QFrame#Bubble {{ background: {color}; border-radius: 12px; border: 1px solid {PALETTE['border']}; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 10)
        layout.setSpacing(4)
        header = QLabel(title or self.KIND_LABEL.get(kind, kind))
        header.setObjectName("Muted")
        header.setStyleSheet(f"color: {PALETTE['muted']}; font-size: 8.5pt; font-weight: 600; background: transparent; border: none;")
        layout.addWidget(header)
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.RichText)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.body.setOpenExternalLinks(True)
        self.body.setStyleSheet("background: transparent; border: none;")
        self.body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        if kind in ("tool", "tool_error", "thought"):
            f = self.body.font()
            f.setPointSizeF(max(8.0, f.pointSizeF() - 0.5))
            self.body.setFont(f)
        layout.addWidget(self.body)
        self.set_text(text)

    def set_text(self, text: str) -> None:
        self._text = text
        rtl = is_rtl(text)
        self.body.setAlignment(Qt.AlignmentFlag.AlignRight if rtl else Qt.AlignmentFlag.AlignLeft)
        self.body.setLayoutDirection(Qt.LayoutDirection.RightToLeft if rtl else Qt.LayoutDirection.LeftToRight)
        self.body.setText(_md_to_html(text))

    def append_text(self, text: str) -> None:
        self.set_text(self._text + text)

    def text(self) -> str:
        return self._text


class ChatView(QScrollArea):
    """Scrollable list of bubbles."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._container = QWidget()
        self._container.setObjectName("Root")
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(10, 10, 10, 10)
        self._layout.setSpacing(8)
        self._layout.addStretch(1)
        self.setWidget(self._container)
        self._autoscroll = True
        self.verticalScrollBar().rangeChanged.connect(self._on_range)
        self.verticalScrollBar().valueChanged.connect(self._on_value)
        self.bubbles: list[Bubble] = []
        self.show_thoughts = True
        self.show_tools = True

    def add(self, kind: str, text: str, title: Optional[str] = None) -> Bubble:
        bubble = Bubble(kind, text, title=title)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        if kind == "user":
            row.addStretch(1)
            row.addWidget(bubble, 6)
        elif kind in ("tool", "tool_error", "thought"):
            row.addWidget(bubble, 6)
            row.addStretch(1)
        else:
            row.addWidget(bubble, 7)
            row.addStretch(1)
        wrapper = QWidget()
        wrapper.setObjectName("Root")
        wrapper.setLayout(row)
        self._layout.insertWidget(self._layout.count() - 1, wrapper)
        bubble.setProperty("wrapper", wrapper)
        self.bubbles.append(bubble)
        if kind == "thought" and not self.show_thoughts:
            wrapper.hide()
        if kind in ("tool", "tool_error") and not self.show_tools:
            wrapper.hide()
        return bubble

    def clear(self) -> None:
        for b in self.bubbles:
            w = b.property("wrapper")
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.bubbles.clear()

    def set_visibility(self, *, thoughts: Optional[bool] = None, tools: Optional[bool] = None) -> None:
        if thoughts is not None:
            self.show_thoughts = thoughts
        if tools is not None:
            self.show_tools = tools
        for b in self.bubbles:
            w = b.property("wrapper")
            if w is None:
                continue
            if b.kind == "thought":
                w.setVisible(self.show_thoughts)
            elif b.kind in ("tool", "tool_error"):
                w.setVisible(self.show_tools)

    def transcript(self) -> str:
        lines = []
        for b in self.bubbles:
            lines.append(f"[{Bubble.KIND_LABEL.get(b.kind, b.kind)}]\n{b.text()}\n")
        return "\n".join(lines)

    def _on_range(self, _min: int, _max: int) -> None:
        if self._autoscroll:
            self.verticalScrollBar().setValue(_max)

    def _on_value(self, value: int) -> None:
        sb = self.verticalScrollBar()
        self._autoscroll = value >= sb.maximum() - 40


class ChatInput(QPlainTextEdit):
    """Multi-line input: Enter sends, Shift+Enter inserts a newline."""

    submitted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("پیام یا دستور خود را بنویسید…  (Enter برای ارسال، Shift+Enter برای خط جدید)")
        self.setWordWrapMode(QTextOption.WrapMode.WordWrap)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setTabChangesFocus(True)
        self._update_height()
        self.textChanged.connect(self._update_height)
        self.textChanged.connect(self._update_direction)
        self.history: list[str] = []
        self._hist_pos = -1

    def _update_height(self) -> None:
        doc_h = self.document().size().height()
        lines = max(1, min(8, int(doc_h)))
        fm = self.fontMetrics()
        h = int(fm.lineSpacing() * lines + 22)
        self.setFixedHeight(max(44, h))

    def _update_direction(self) -> None:
        text = self.toPlainText()
        opt = self.document().defaultTextOption()
        opt.setTextDirection(Qt.LayoutDirection.RightToLeft if is_rtl(text) else Qt.LayoutDirection.LeftToRight)
        self.document().setDefaultTextOption(opt)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier):
                super().keyPressEvent(event)
                return
            text = self.toPlainText().strip()
            if text:
                self.history.append(text)
                self._hist_pos = len(self.history)
                self.submitted.emit(text)
                self.clear()
            return
        if event.key() == Qt.Key.Key_Up and self.toPlainText() == "" and self.history:
            self._hist_pos = max(0, self._hist_pos - 1)
            self.setPlainText(self.history[self._hist_pos])
            self.moveCursor(self.textCursor().MoveOperation.End)
            return
        super().keyPressEvent(event)


class ScreenshotView(QLabel):
    """Shows the latest screenshot scaled to fit; click to open full size."""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap: Optional[QPixmap] = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(QSize(200, 120))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet(f"background: {PALETTE['panel2']}; border: 1px solid {PALETTE['border']}; border-radius: 8px; color: {PALETTE['muted']};")
        self.setText("هنوز اسکرین‌شاتی گرفته نشده است")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_image(self, image) -> None:
        """Accept a PIL image."""
        if image.mode != "RGB":
            image = image.convert("RGB")
        data = image.tobytes("raw", "RGB")
        qimg = QImage(data, image.width, image.height, image.width * 3, QImage.Format.Format_RGB888).copy()
        self._pixmap = QPixmap.fromImage(qimg)
        self._rescale()

    def pixmap_full(self) -> Optional[QPixmap]:
        return self._pixmap

    def _rescale(self) -> None:
        if self._pixmap is None:
            return
        scaled = self._pixmap.scaled(self.size() - QSize(4, 4), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.setPixmap(scaled)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rescale()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pixmap is not None:
            self.clicked.emit()
        super().mousePressEvent(event)


class KeyValueLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setWordWrap(True)
        self.setStyleSheet("background: transparent; border: none;")

    def set_pairs(self, pairs: list[tuple[str, str]]) -> None:
        rows = "".join(f"<tr><td style='color:{PALETTE['muted']};padding-right:10px'>{html.escape(k)}</td><td>{html.escape(str(v))}</td></tr>" for k, v in pairs)
        self.setText(f"<table>{rows}</table>")
