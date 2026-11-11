"""
Tema visual de la app: paleta oscura + acento ámbar, en la línea de
Jellyfin/Plex. Un solo QSS aplicado a nivel QApplication; los widgets que
necesitan verse distintos del resto (ej. el lienzo negro de video) siguen
usando su propio setStyleSheet() inline, que tiene prioridad sobre esto.
"""

BG = "#14161a"
SURFACE = "#1d2026"
SURFACE_HOVER = "#262a32"
BORDER = "#2c303a"
TEXT = "#eceef2"
TEXT_DIM = "#9aa0ab"
ACCENT = "#e0973c"
ACCENT_RGB = "224, 151, 60"  # mismo color que ACCENT, para armar rgba() en QSS (tabs, ver #libraryTab:checked)
ACCENT_HOVER = "#eaa855"
ACCENT_PRESSED = "#c47f2e"
DANGER = "#d1554a"
DANGER_HOVER = "#b8483f"

STYLESHEET = f"""
QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-size: 13px;
}}

QLabel {{
    background: transparent;
}}

QScrollArea {{
    border: none;
    background: transparent;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}

QPushButton {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 7px 16px;
    color: {TEXT};
}}
QPushButton:hover {{
    background-color: {SURFACE_HOVER};
    border-color: {ACCENT};
}}
QPushButton:pressed {{
    background-color: {ACCENT_PRESSED};
    border-color: {ACCENT_PRESSED};
    color: white;
}}
QPushButton:focus {{
    border: 2px solid {ACCENT};
}}

QLineEdit, QComboBox {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 10px;
    color: {TEXT};
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox QAbstractItemView {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    outline: none;
}}

QListWidget {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px;
    outline: none;
}}
QListWidget::item {{
    padding: 7px 8px;
    border-radius: 4px;
    color: {TEXT};
}}
QListWidget::item:hover {{
    background-color: {SURFACE_HOVER};
}}
QListWidget::item:selected {{
    background-color: {ACCENT};
    color: white;
}}

QDialog {{
    background-color: {BG};
}}

QPushButton#flatIconButton {{
    background-color: transparent;
    border: none;
    border-radius: 6px;
    padding: 4px 10px;
    font-size: 16px;
}}
QPushButton#flatIconButton:hover {{
    background-color: {SURFACE_HOVER};
    border-color: transparent;
}}
QPushButton#flatIconButton:pressed {{
    background-color: {SURFACE_HOVER};
    color: {ACCENT};
}}
QPushButton#flatIconButton:disabled {{
    color: {BORDER};
}}

QPushButton#dangerButton {{
    background-color: transparent;
    border: 1px solid {DANGER};
    color: {DANGER};
}}
QPushButton#dangerButton:hover {{
    background-color: {DANGER};
    border-color: {DANGER};
    color: white;
}}
QPushButton#dangerButton:pressed {{
    background-color: {DANGER_HOVER};
    border-color: {DANGER_HOVER};
    color: white;
}}

QPushButton#linkButton {{
    background-color: transparent;
    border: none;
    padding: 4px 2px;
    color: {ACCENT};
    text-align: left;
}}
QPushButton#linkButton:hover {{
    color: {ACCENT_HOVER};
    text-decoration: underline;
}}

QWidget#tabsBar, QWidget#headerBar {{
    background-color: {SURFACE};
    border-radius: 10px;
}}

QPushButton#libraryTab {{
    background-color: {BG};
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 7px 16px;
    color: {TEXT_DIM};
    font-weight: 500;
}}
QPushButton#libraryTab:hover {{
    background-color: {SURFACE_HOVER};
    border-color: transparent;
    color: {TEXT};
}}
QPushButton#libraryTab:checked {{
    background-color: rgba({ACCENT_RGB}, 0.15);
    color: {ACCENT};
    border: 1px solid {ACCENT};
}}

QWidget#seasonCard {{
    background-color: {SURFACE};
    border-radius: 8px;
}}

QWidget#episodeRow {{
    border-radius: 4px;
    border-bottom: 1px solid {BORDER};
}}
QWidget#episodeRow:hover {{
    background-color: {SURFACE_HOVER};
}}

QSlider::groove:horizontal {{
    height: 4px;
    background: {BORDER};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {ACCENT};
    width: 13px;
    height: 13px;
    margin: -5px 0;
    border-radius: 6px;
}}
QSlider::handle:horizontal:hover {{
    background: {ACCENT_HOVER};
}}

QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 5px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: {ACCENT};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: none;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER};
    border-radius: 5px;
    min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {ACCENT};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: none;
}}
"""
