"""Cove Video Editor theme — palette + QSS for the v2.0 redesign.

Matches cove-nexus / cove-video-downloader: dark teal accent, Inter body
font, JetBrains Mono for timecodes and numeric labels, 12 px rounded panels.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication


# ── Palette ────────────────────────────────────────────────────────────────
BG          = "#0b1013"
BG_2        = "#0e1518"
PANEL       = "#121a1f"
PANEL_2     = "#151d23"
PANEL_HI    = "#192328"
BORDER      = "#1e2a31"
WINDOW_EDGE = "rgba(255, 255, 255, 0.18)"  # visible outer border on frameless QMainWindow
BORDER_HI   = "#27353d"
TEXT        = "#e6edf0"
TEXT_2      = "#a2b0b8"
TEXT_3      = "#6a7880"
ACCENT      = "#5eead4"
ACCENT_2    = "#2dd4bf"
ACCENT_INK  = "#052e2b"
DANGER      = "#f87171"
OK          = "#4ade80"
WARN        = "#fbbf24"

# Timeline-only derivatives (dark tints used for clip bodies and track backgrounds)
TRACK_BG      = "#0c1317"
RULER_BG      = "#0a1013"
VIDEO_CLIP_A  = "#1b3e4a"
VIDEO_CLIP_B  = "#14303a"
VIDEO_CLIP_BD = "#2a5866"
AUDIO_CLIP_A  = "#3a2e18"
AUDIO_CLIP_B  = "#2a220f"
AUDIO_CLIP_BD = "#5a4420"

# QColor aliases — handy in paint code.
C_BG          = QColor(BG)
C_BG_2        = QColor(BG_2)
C_PANEL       = QColor(PANEL)
C_PANEL_HI    = QColor(PANEL_HI)
C_BORDER      = QColor(BORDER)
C_BORDER_HI   = QColor(BORDER_HI)
C_TEXT        = QColor(TEXT)
C_TEXT_2      = QColor(TEXT_2)
C_TEXT_3      = QColor(TEXT_3)
C_ACCENT      = QColor(ACCENT)
C_ACCENT_2    = QColor(ACCENT_2)
C_WARN        = QColor(WARN)
C_DANGER      = QColor(DANGER)
C_OK          = QColor(OK)
C_TRACK_BG    = QColor(TRACK_BG)
C_RULER_BG    = QColor(RULER_BG)
C_VIDEO_CLIP_A  = QColor(VIDEO_CLIP_A)
C_VIDEO_CLIP_B  = QColor(VIDEO_CLIP_B)
C_VIDEO_CLIP_BD = QColor(VIDEO_CLIP_BD)
C_AUDIO_CLIP_A  = QColor(AUDIO_CLIP_A)
C_AUDIO_CLIP_B  = QColor(AUDIO_CLIP_B)
C_AUDIO_CLIP_BD = QColor(AUDIO_CLIP_BD)


# ── Fonts ──────────────────────────────────────────────────────────────────
SANS_FAMILY = "Inter"
MONO_FAMILY = "JetBrains Mono"


def _first_available(candidates: list[str], fallback: str) -> str:
    available = set(QFontDatabase.families())
    for c in candidates:
        if c in available:
            return c
    return fallback


def resolve_fonts() -> tuple[str, str]:
    """Pick Inter / JetBrains Mono if installed, otherwise sensible fallbacks
    that still feel close to the design."""
    sans = _first_available([SANS_FAMILY, "Inter Display", "Cantarell", "Roboto"], "Sans Serif")
    mono = _first_available(
        [MONO_FAMILY, "JetBrainsMono Nerd Font", "DejaVu Sans Mono", "Liberation Mono"],
        "Monospace",
    )
    return sans, mono


# ── QSS ────────────────────────────────────────────────────────────────────
def build_qss() -> str:
    sans, mono = resolve_fonts()
    return f"""
* {{
    outline: 0;
}}
QWidget {{
    background-color: {BG_2};
    color: {TEXT};
    font-family: "{sans}";
    font-size: 13px;
    selection-background-color: {ACCENT};
    selection-color: {ACCENT_INK};
}}
QMainWindow, QDialog {{
    background-color: {BG_2};
}}
    QMainWindow {{
        border: 1px solid {WINDOW_EDGE};
    }}
#CovePanel {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}
#CovePanelFlat {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QFrame#PreviewStage {{
    background: #000;
    border: none;
    border-radius: 0px;
}}
QFrame#TransportBar, QFrame#ExportBar, QFrame#ZoomBar {{
    background: #0f161a;
    border: 1px solid {BORDER};
    border-radius: 10px;
}}

/* ── Buttons ───────────────────────────────────────────────────────── */
QPushButton {{
    background: #141d22;
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 12px;
    font-weight: 500;
}}
QPushButton:hover {{
    background: {PANEL_HI};
    border-color: {BORDER_HI};
}}
QPushButton:pressed {{
    background: #0d1317;
}}
QPushButton:disabled {{
    color: {TEXT_3};
    background: #10171b;
    border-color: {BORDER};
}}

QPushButton#PrimaryButton {{
    background: {ACCENT};
    color: {ACCENT_INK};
    border: 1px solid {ACCENT};
    font-weight: 600;
    padding: 9px 20px;
    border-radius: 8px;
}}
QPushButton#PrimaryButton:hover {{
    background: {ACCENT_2};
    border-color: {ACCENT_2};
}}
QPushButton#PrimaryButton:disabled {{
    background: #1a2a2e;
    color: {TEXT_3};
    border-color: {BORDER};
}}

QPushButton#DangerButton {{
    background: transparent;
    color: {DANGER};
    border: 1px solid rgba(248,113,113,0.4);
}}
QPushButton#DangerButton:hover {{
    background: rgba(248,113,113,0.1);
    border-color: {DANGER};
}}

QPushButton#GhostButton {{
    background: transparent;
    border: 1px dashed {BORDER_HI};
    color: {TEXT_2};
}}
QPushButton#GhostButton:hover {{
    color: {TEXT};
    border-color: {ACCENT};
}}

QPushButton#FlatButton {{
    background: transparent;
    color: {TEXT_2};
    border: none;
    padding: 3px 8px;
    font-size: 11px;
}}
QPushButton#FlatButton:hover {{
    color: {TEXT};
    background: {PANEL_HI};
    border-radius: 4px;
}}
QPushButton#FlatButton:checked {{
    color: {ACCENT};
}}

QFrame#ExportLogPanel {{
    background: #0a1013;
    border: 1px solid {BORDER};
    border-top: none;
    border-radius: 0 0 8px 8px;
}}
QTextEdit#ExportLog {{
    background: #060c0f;
    color: {TEXT_2};
    border: none;
    border-radius: 4px;
    padding: 4px 8px;
    font-family: "{mono}";
    font-size: 11px;
}}

QToolButton {{
    background: transparent;
    color: {TEXT_2};
    border: none;
    border-radius: 6px;
    padding: 5px 8px;
}}
QToolButton:hover {{
    background: {PANEL_HI};
    color: {TEXT};
}}
QToolButton:checked {{
    background: #1c272d;
    color: {TEXT};
}}
QToolButton:disabled {{
    color: {TEXT_3};
}}

QToolButton#CtrlBtn {{
    background: transparent;
    color: {TEXT_2};
    border-radius: 6px;
    padding: 0;
}}
QToolButton#CtrlBtn:hover {{
    background: #1a2328;
    color: {TEXT};
}}
QToolButton#CloseBtn:hover {{
    background: #e11d48;
    color: #fff;
}}

/* ── Input fields ───────────────────────────────────────────────── */
QLineEdit, QTextEdit, QPlainTextEdit {{
    background: #0a1013;
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: {ACCENT};
    selection-color: {ACCENT_INK};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border-color: {ACCENT};
}}
QLineEdit#Timecode {{
    background: #05090b;
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 10px;
    font-family: "{mono}";
    font-size: 13px;
    font-weight: 500;
}}
QLineEdit#Timecode:focus {{
    border-color: {ACCENT};
}}

QComboBox {{
    background: #0a1013;
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 12px;
    padding-right: 32px;
    font-size: 12.5px;
}}
QComboBox:hover {{
    border-color: {BORDER_HI};
}}
QComboBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{
    border: none;
    width: 28px;
}}
QComboBox::down-arrow {{
    image: none;
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {TEXT_3};
    margin-right: 10px;
}}
QComboBox QAbstractItemView {{
    background: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER_HI};
    border-radius: 6px;
    padding: 4px;
    selection-background-color: {PANEL_HI};
    selection-color: {TEXT};
    outline: 0;
}}

QDoubleSpinBox, QSpinBox {{
    background: #0a1013;
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 6px;
}}

QCheckBox {{
    spacing: 6px;
    color: {TEXT_2};
}}
QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {BORDER_HI};
    border-radius: 3px;
    background: #0a1013;
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}

/* ── Sliders ─────────────────────────────────────────────────────── */
QSlider::groove:horizontal {{
    height: 3px;
    background: #1e2a31;
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {ACCENT};
    border: 2px solid {BG};
    width: 12px;
    height: 12px;
    margin: -6px 0;
    border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{
    background: {ACCENT_2};
}}

/* ── Scrollbars ──────────────────────────────────────────────────── */
QScrollBar:horizontal {{
    background: transparent;
    height: 12px;
    margin: 0;
    border: none;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 12px;
    margin: 0;
    border: none;
}}
QScrollBar::handle:horizontal {{
    background: #1f2a30;
    min-width: 32px;
    border-radius: 6px;
    margin: 3px 2px;
}}
QScrollBar::handle:vertical {{
    background: #1f2a30;
    min-height: 32px;
    border-radius: 6px;
    margin: 2px 3px;
}}
QScrollBar::handle:horizontal:hover,
QScrollBar::handle:vertical:hover {{
    background: #2a3a42;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    background: transparent;
    width: 0;
    height: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}

/* ── Progress bar ────────────────────────────────────────────────── */
QProgressBar {{
    background: #0a1013;
    border: 1px solid {BORDER};
    border-radius: 4px;
    text-align: center;
    color: {TEXT_2};
    font-family: "{mono}";
    font-size: 11px;
    padding: 0 2px;
    min-height: 14px;
}}
QProgressBar::chunk {{
    background: {ACCENT};
    border-radius: 3px;
    margin: 1px;
}}

/* ── Menus ───────────────────────────────────────────────────────── */
QMenu {{
    background: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER_HI};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 16px;
    border-radius: 5px;
}}
QMenu::item:selected {{
    background: {PANEL_HI};
}}
QMenu::item:disabled {{
    color: {TEXT_3};
}}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 6px;
}}

/* ── Status bar ──────────────────────────────────────────────────── */
QStatusBar {{
    background: #0c1317;
    color: {TEXT_3};
    border-top: 1px solid {BORDER};
    font-family: "{mono}";
    font-size: 11px;
}}
QStatusBar::item {{
    border: none;
}}

/* ── Labels ──────────────────────────────────────────────────────── */
QLabel#Muted {{
    color: {TEXT_3};
    font-family: "{mono}";
    font-size: 11px;
    letter-spacing: 0.4px;
}}
QLabel#ExportLabel {{
    color: {TEXT_3};
    font-size: 10.5px;
    font-weight: 600;
    letter-spacing: 0.6px;
    text-transform: uppercase;
}}
QLabel#RangeLabel {{
    color: {TEXT_2};
    font-family: "{mono}";
    font-size: 11px;
}}
QLabel#BrandTitle {{
    color: {TEXT};
    font-size: 12.5px;
    font-weight: 600;
    letter-spacing: 0.2px;
}}
QLabel#VersionPill {{
    color: {TEXT_3};
    font-family: "{mono}";
    font-size: 10.5px;
    background: #0b1114;
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 2px 7px;
}}

/* ── Splitter ────────────────────────────────────────────────────── */
QSplitter::handle {{
    background: transparent;
}}
QSplitter::handle:horizontal {{
    width: 6px;
}}
QSplitter::handle:vertical {{
    height: 6px;
}}
QSplitter::handle:hover {{
    background: rgba(94,234,212,0.06);
}}

/* ── Message boxes ───────────────────────────────────────────────── */
QMessageBox {{
    background: {PANEL};
}}
QMessageBox QLabel {{
    color: {TEXT};
    font-size: 13px;
}}

QToolTip {{
    background: {PANEL_HI};
    color: {TEXT};
    border: 1px solid {BORDER_HI};
    border-radius: 5px;
    padding: 4px 8px;
}}
"""


# ── Palette install ────────────────────────────────────────────────────────
def apply_palette(app: QApplication) -> None:
    """Set the Qt palette so widgets we haven't QSS'd still blend in.

    The QSS rules above take precedence for styled widgets; the palette
    covers Qt-native fallbacks (file dialogs, message boxes, tool tips).
    """
    app.setStyle("Fusion")

    sans, _mono = resolve_fonts()
    base_font = QFont(sans, 10)
    app.setFont(base_font)

    p = QPalette()
    p.setColor(QPalette.Window,          QColor(BG_2))
    p.setColor(QPalette.WindowText,      QColor(TEXT))
    p.setColor(QPalette.Base,            QColor("#0a1013"))
    p.setColor(QPalette.AlternateBase,   QColor(PANEL))
    p.setColor(QPalette.ToolTipBase,     QColor(PANEL_HI))
    p.setColor(QPalette.ToolTipText,     QColor(TEXT))
    p.setColor(QPalette.Text,            QColor(TEXT))
    p.setColor(QPalette.Button,          QColor("#141d22"))
    p.setColor(QPalette.ButtonText,      QColor(TEXT))
    p.setColor(QPalette.BrightText,      QColor(DANGER))
    p.setColor(QPalette.Link,            QColor(ACCENT))
    p.setColor(QPalette.Highlight,       QColor(ACCENT))
    p.setColor(QPalette.HighlightedText, QColor(ACCENT_INK))
    p.setColor(QPalette.PlaceholderText, QColor(TEXT_3))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        p.setColor(QPalette.Disabled, role, QColor(TEXT_3))
    app.setPalette(p)


def apply_theme(app: QApplication) -> None:
    apply_palette(app)
    app.setStyleSheet(build_qss())
