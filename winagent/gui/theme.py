"""Colours and the application-wide stylesheet."""

from __future__ import annotations

PALETTE = {
    "bg": "#0f1115",
    "panel": "#161a22",
    "panel2": "#1c2130",
    "border": "#262c3a",
    "text": "#e6e9ef",
    "muted": "#8b93a7",
    "accent": "#4f8cff",
    "accent2": "#7c5cff",
    "user": "#243b6b",
    "assistant": "#1e2433",
    "tool": "#1a2a24",
    "tool_err": "#3a1f22",
    "thought": "#2a2438",
    "success": "#3ddc97",
    "warn": "#ffb454",
    "error": "#ff5c5c",
}

STYLESHEET = f"""
* {{
    font-family: "Segoe UI", "Vazirmatn", "Tahoma", "Noto Sans", sans-serif;
    font-size: 10.5pt;
}}
QMainWindow, QDialog, QWidget#Root {{
    background: {PALETTE['bg']};
    color: {PALETTE['text']};
}}
QWidget {{
    color: {PALETTE['text']};
}}
QFrame#Panel, QWidget#Panel {{
    background: {PALETTE['panel']};
    border: 1px solid {PALETTE['border']};
    border-radius: 10px;
}}
QLabel#Title {{
    font-size: 15pt;
    font-weight: 600;
}}
QLabel#Muted {{
    color: {PALETTE['muted']};
}}
QLabel#Status {{
    color: {PALETTE['muted']};
    padding: 2px 6px;
}}
QPlainTextEdit, QTextEdit, QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QListWidget, QTreeWidget {{
    background: {PALETTE['panel2']};
    border: 1px solid {PALETTE['border']};
    border-radius: 8px;
    padding: 6px;
    selection-background-color: {PALETTE['accent']};
}}
QPlainTextEdit:focus, QTextEdit:focus, QLineEdit:focus, QComboBox:focus {{
    border: 1px solid {PALETTE['accent']};
}}
QPushButton {{
    background: {PALETTE['panel2']};
    border: 1px solid {PALETTE['border']};
    border-radius: 8px;
    padding: 7px 14px;
    min-height: 18px;
}}
QPushButton:hover {{
    border-color: {PALETTE['accent']};
}}
QPushButton:pressed {{
    background: {PALETTE['border']};
}}
QPushButton:disabled {{
    color: {PALETTE['muted']};
    border-color: {PALETTE['border']};
}}
QPushButton#Primary {{
    background: {PALETTE['accent']};
    border: none;
    color: white;
    font-weight: 600;
}}
QPushButton#Primary:hover {{
    background: #3f7bef;
}}
QPushButton#Primary:disabled {{
    background: #2c3a5c;
    color: #9aa6c4;
}}
QPushButton#Danger {{
    background: {PALETTE['error']};
    border: none;
    color: white;
    font-weight: 600;
}}
QPushButton#Danger:disabled {{
    background: #4a2a2c;
    color: #b58b8d;
}}
QPushButton#Flat {{
    background: transparent;
    border: none;
    padding: 4px 8px;
    color: {PALETTE['muted']};
}}
QPushButton#Flat:hover {{
    color: {PALETTE['text']};
}}
QCheckBox {{
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid {PALETTE['border']};
    background: {PALETTE['panel2']};
}}
QCheckBox::indicator:checked {{
    background: {PALETTE['accent']};
    border-color: {PALETTE['accent']};
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {PALETTE['border']};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {PALETTE['muted']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
}}
QScrollBar::handle:horizontal {{
    background: {PALETTE['border']};
    border-radius: 5px;
    min-width: 30px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}
QSplitter::handle {{
    background: {PALETTE['border']};
}}
QSplitter::handle:horizontal {{
    width: 2px;
}}
QSplitter::handle:vertical {{
    height: 2px;
}}
QTabWidget::pane {{
    border: 1px solid {PALETTE['border']};
    border-radius: 8px;
    top: -1px;
}}
QTabBar::tab {{
    background: {PALETTE['panel']};
    border: 1px solid {PALETTE['border']};
    padding: 6px 14px;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    margin-right: 2px;
    color: {PALETTE['muted']};
}}
QTabBar::tab:selected {{
    background: {PALETTE['panel2']};
    color: {PALETTE['text']};
}}
QProgressBar {{
    background: {PALETTE['panel2']};
    border: 1px solid {PALETTE['border']};
    border-radius: 6px;
    height: 8px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {PALETTE['accent']};
    border-radius: 6px;
}}
QToolTip {{
    background: {PALETTE['panel2']};
    color: {PALETTE['text']};
    border: 1px solid {PALETTE['border']};
    padding: 4px;
}}
QMenu {{
    background: {PALETTE['panel2']};
    border: 1px solid {PALETTE['border']};
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 18px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background: {PALETTE['accent']};
}}
QGroupBox {{
    border: 1px solid {PALETTE['border']};
    border-radius: 8px;
    margin-top: 12px;
    padding: 10px 6px 6px 6px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {PALETTE['muted']};
}}
QStatusBar {{
    background: {PALETTE['panel']};
    border-top: 1px solid {PALETTE['border']};
}}
QSpinBox::up-button, QDoubleSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::down-button {{
    width: 18px;
    border: none;
    background: transparent;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    width: 8px; height: 8px;
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 6px solid {PALETTE['muted']};
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    width: 8px; height: 8px;
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 6px solid {PALETTE['muted']};
}}
QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox::down-arrow {{
    image: none;
    width: 8px; height: 8px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 6px solid {PALETTE['muted']};
    margin-right: 6px;
}}
QComboBox QAbstractItemView {{
    background: {PALETTE['panel2']};
    border: 1px solid {PALETTE['border']};
    selection-background-color: {PALETTE['accent']};
}}
QHeaderView::section {{
    background: {PALETTE['panel']};
    color: {PALETTE['muted']};
    border: none;
    border-bottom: 1px solid {PALETTE['border']};
    border-right: 1px solid {PALETTE['border']};
    padding: 4px 6px;
}}
QTreeWidget::item {{
    padding: 3px 2px;
}}
QTreeWidget::item:selected, QListWidget::item:selected {{
    background: {PALETTE['user']};
}}
QTableCornerButton::section {{
    background: {PALETTE['panel']};
    border: none;
}}
"""
