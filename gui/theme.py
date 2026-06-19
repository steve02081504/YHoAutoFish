from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QGraphicsDropShadowEffect


APP_COLORS = {
    "bg": "#07101C",
    "bg_alt": "#0B1626",
    "sidebar": "#091423",
    "panel": "#0F1B2B",
    "panel_alt": "#142236",
    "panel_soft": "#1A2A40",
    "glass": "rgba(20, 34, 54, 0.78)",
    "glass_soft": "rgba(28, 44, 66, 0.58)",
    "glass_light": "rgba(235, 244, 255, 0.12)",
    "stroke": "#29425E",
    "stroke_soft": "#1E3249",
    "text": "#F3F8FF",
    "text_dim": "#9AB0CA",
    "text_soft": "#7187A3",
    "accent": "#1DD0D6",
    "accent_soft": "#63E4E4",
    "accent_deep": "#0E7E92",
    "danger": "#FF667E",
    "warning": "#F1BE67",
    "success": "#6FE39A",
}


RARITY_ORDER = ["金色稀有度", "紫色稀有度", "蓝色稀有度", "绿色稀有度", "废品"]

RARITY_META = {
    "金色稀有度": {"label": "金色", "color": "#F3BF55", "glow": "#705322"},
    "紫色稀有度": {"label": "紫色", "color": "#B975FF", "glow": "#5B2E84"},
    "蓝色稀有度": {"label": "蓝色", "color": "#65BAFF", "glow": "#1E5D8F"},
    "绿色稀有度": {"label": "绿色", "color": "#64D98B", "glow": "#225B3E"},
    "废品": {"label": "废品", "color": "#9AA5B5", "glow": "#4A5260"},
    "未知稀有度": {"label": "未知", "color": "#7CA5C2", "glow": "#395267"},
}


def rarity_meta(rarity):
    return RARITY_META.get(rarity, RARITY_META["未知稀有度"])


def add_shadow(widget, blur=28, color="#000000", alpha=120, offset=(0, 10)):
    shadow = QGraphicsDropShadowEffect()
    shadow.setBlurRadius(blur)
    if isinstance(color, str):
        qcolor = QColor(color)
        qcolor.setAlpha(alpha)
        shadow.setColor(qcolor)
    else:
        shadow.setColor(color)
    shadow.setOffset(*offset)
    widget.setGraphicsEffect(shadow)
    return shadow


def rounded_pixmap(source, width, height, radius=26, keep_full=True):
    if isinstance(source, str):
        pixmap = QPixmap(source)
    else:
        pixmap = source or QPixmap()

    if pixmap.isNull():
        return QPixmap()

    target = QSize(width, height)
    aspect_mode = Qt.KeepAspectRatio if keep_full else Qt.KeepAspectRatioByExpanding
    scaled = pixmap.scaled(target, aspect_mode, Qt.SmoothTransformation)
    result = QPixmap(target)
    result.fill(Qt.transparent)

    painter = QPainter(result)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, width, height, radius, radius)
    painter.setClipPath(path)
    x = (width - scaled.width()) / 2
    y = (height - scaled.height()) / 2
    painter.drawPixmap(int(x), int(y), scaled)
    painter.end()
    return result


def panel_stylesheet(accent=None):
    accent = accent or APP_COLORS["accent"]
    return f"""
    QWidget {{
        color: {APP_COLORS["text"]};
    }}
    QLabel {{
        background-color: transparent;
        border: none;
    }}
    QFrame {{
        background-color: {APP_COLORS["glass"]};
        border: 1px solid rgba(111, 145, 182, 0.18);
        border-radius: 24px;
    }}
    QFrame[variant="elevated"] {{
        background-color: rgba(20, 34, 54, 0.86);
        border: 1px solid rgba(93, 132, 174, 0.22);
    }}
    QFrame[variant="soft"] {{
        background-color: {APP_COLORS["glass_soft"]};
        border: 1px solid rgba(111, 145, 182, 0.15);
    }}
    QLabel[role="headline"] {{
        background-color: transparent;
        border: none;
        color: {APP_COLORS["text"]};
        font-size: 33px;
        font-weight: 900;
    }}
    QLabel[role="subtle"] {{
        background-color: transparent;
        border: none;
        color: {APP_COLORS["text_dim"]};
        font-size: 13px;
    }}
    QLabel[role="section"] {{
        background-color: transparent;
        border: none;
        color: {APP_COLORS["text"]};
        font-size: 19px;
        font-weight: 800;
    }}
    QLabel[role="accent-chip"] {{
        background-color: rgba(22, 209, 214, 0.12);
        color: {accent};
        border: 1px solid rgba(22, 209, 214, 0.28);
        border-radius: 15px;
        padding: 7px 14px;
        font-size: 12px;
        font-weight: 800;
    }}
    """


def line_edit_stylesheet():
    return f"""
    QLineEdit {{
        background-color: rgba(21, 35, 54, 0.86);
        color: {APP_COLORS["text"]};
        border: 1px solid rgba(111, 145, 182, 0.18);
        border-radius: 18px;
        padding: 12px 15px;
        font-size: 13px;
    }}
    QLineEdit:focus {{
        border: 1px solid rgba(29, 208, 214, 0.72);
        background-color: rgba(24, 40, 61, 0.92);
    }}
    """


def combo_stylesheet():
    return f"""
    QComboBox {{
        background-color: rgba(21, 35, 54, 0.86);
        color: {APP_COLORS["text"]};
        border: 1px solid rgba(111, 145, 182, 0.18);
        border-radius: 18px;
        padding: 10px 14px;
        font-size: 13px;
        min-width: 112px;
    }}
    QComboBox:focus {{
        border: 1px solid rgba(29, 208, 214, 0.72);
    }}
    QComboBox::drop-down {{
        border: none;
        width: 28px;
    }}
    QComboBox QAbstractItemView {{
        background-color: rgba(17, 27, 42, 0.98);
        color: {APP_COLORS["text"]};
        border: 1px solid rgba(111, 145, 182, 0.2);
        selection-background-color: rgba(29, 208, 214, 0.24);
        padding: 4px;
    }}
    """


def primary_button_stylesheet():
    return f"""
    QPushButton {{
        background-color: {APP_COLORS["accent"]};
        color: #051419;
        border: none;
        outline: none;
        border-radius: 18px;
        min-height: 44px;
        padding: 0 18px;
        font-size: 14px;
        font-weight: 900;
    }}
    QPushButton:hover {{
        background-color: {APP_COLORS["accent_soft"]};
    }}
    QPushButton:pressed {{
        background-color: #13B5C0;
    }}
    QPushButton:disabled {{
        background-color: rgba(57, 76, 93, 0.84);
        color: #90A1B1;
    }}
    """


def secondary_button_stylesheet():
    return f"""
    QPushButton {{
        background-color: rgba(22, 35, 54, 0.72);
        color: {APP_COLORS["text_dim"]};
        border: 1px solid rgba(111, 145, 182, 0.18);
        outline: none;
        border-radius: 18px;
        min-height: 40px;
        padding: 0 18px;
        font-size: 13px;
        font-weight: 700;
    }}
    QPushButton:hover {{
        color: {APP_COLORS["text"]};
        border: 1px solid rgba(22, 209, 214, 0.4);
        background-color: rgba(25, 40, 61, 0.86);
    }}
    QPushButton:checked {{
        background-color: rgba(22, 209, 214, 0.16);
        color: {APP_COLORS["accent_soft"]};
        border: 1px solid rgba(22, 209, 214, 0.52);
    }}
    """


def table_stylesheet():
    return f"""
    QTableWidget, QTableView {{
        background-color: transparent;
        color: {APP_COLORS["text"]};
        border: none;
        gridline-color: rgba(77, 102, 130, 0.18);
        selection-background-color: rgba(22, 209, 214, 0.12);
        alternate-background-color: rgba(255, 255, 255, 0.015);
        font-size: 13px;
    }}
    QHeaderView::section {{
        background-color: rgba(255, 255, 255, 0.03);
        color: {APP_COLORS["text_dim"]};
        border: none;
        border-bottom: 1px solid rgba(111, 145, 182, 0.14);
        padding: 10px;
        font-weight: 700;
    }}
    QScrollBar:vertical {{
        border: none;
        background: rgba(255, 255, 255, 0.04);
        width: 12px;
        margin: 8px 4px 8px 0;
        border-radius: 6px;
    }}
    QScrollBar::handle:vertical {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
            stop:0 rgba(103, 234, 236, 0.92),
            stop:1 rgba(29, 208, 214, 0.58));
        min-height: 40px;
        border-radius: 6px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
            stop:0 rgba(138, 243, 244, 0.98),
            stop:1 rgba(56, 222, 228, 0.68));
    }}
    QScrollBar:horizontal {{
        border: none;
        background: rgba(255, 255, 255, 0.04);
        height: 12px;
        margin: 0 8px 4px 8px;
        border-radius: 6px;
    }}
    QScrollBar::handle:horizontal {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 rgba(103, 234, 236, 0.90),
            stop:1 rgba(29, 208, 214, 0.54));
        min-width: 40px;
        border-radius: 6px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 rgba(138, 243, 244, 0.96),
            stop:1 rgba(56, 222, 228, 0.66));
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0px;
        height: 0px;
    }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal,
    QScrollBar::up-arrow:vertical, QScrollBar::down-arrow:vertical,
    QScrollBar::left-arrow:horizontal, QScrollBar::right-arrow:horizontal {{
        background: transparent;
        border: none;
        width: 0px;
        height: 0px;
    }}
    """


def scroll_area_stylesheet():
    return f"""
    QScrollArea {{
        border: none;
        background-color: transparent;
    }}
    QScrollArea > QWidget > QWidget {{
        background-color: transparent;
    }}
    QScrollBar:vertical {{
        border: none;
        background: rgba(255, 255, 255, 0.04);
        width: 12px;
        margin: 10px 0;
        border-radius: 6px;
    }}
    QScrollBar::handle:vertical {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
            stop:0 rgba(103, 234, 236, 0.92),
            stop:1 rgba(29, 208, 214, 0.58));
        min-height: 34px;
        border-radius: 6px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
            stop:0 rgba(138, 243, 244, 0.98),
            stop:1 rgba(56, 222, 228, 0.68));
    }}
    QScrollBar:horizontal {{
        border: none;
        background: rgba(255, 255, 255, 0.04);
        height: 12px;
        margin: 0 10px;
        border-radius: 6px;
    }}
    QScrollBar::handle:horizontal {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 rgba(103, 234, 236, 0.90),
            stop:1 rgba(29, 208, 214, 0.54));
        min-width: 34px;
        border-radius: 6px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 rgba(138, 243, 244, 0.96),
            stop:1 rgba(56, 222, 228, 0.66));
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0px;
        height: 0px;
    }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal,
    QScrollBar::up-arrow:vertical, QScrollBar::down-arrow:vertical,
    QScrollBar::left-arrow:horizontal, QScrollBar::right-arrow:horizontal {{
        background: transparent;
        border: none;
        width: 0px;
        height: 0px;
    }}
    """


def scrollbar_stylesheet(compact=False):
    width = 9 if compact else 12
    radius = 4 if compact else 6
    min_handle = 28 if compact else 40
    margin_v = "8px 2px 8px 0" if compact else "10px 4px 10px 0"
    margin_h = "0 8px 2px 8px" if compact else "0 10px 4px 10px"
    return f"""
    QScrollBar:vertical {{
        border: none;
        background: rgba(255, 255, 255, 0.035);
        width: {width}px;
        margin: {margin_v};
        border-radius: {radius}px;
    }}
    QScrollBar::handle:vertical {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
            stop:0 rgba(126, 242, 244, 0.94),
            stop:0.55 rgba(29, 208, 214, 0.70),
            stop:1 rgba(14, 126, 146, 0.62));
        min-height: {min_handle}px;
        border-radius: {radius}px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
            stop:0 rgba(176, 252, 253, 0.98),
            stop:0.58 rgba(89, 231, 235, 0.86),
            stop:1 rgba(29, 208, 214, 0.72));
    }}
    QScrollBar::handle:vertical:pressed {{
        background: rgba(241, 190, 103, 0.92);
    }}
    QScrollBar:horizontal {{
        border: none;
        background: rgba(255, 255, 255, 0.035);
        height: {width}px;
        margin: {margin_h};
        border-radius: {radius}px;
    }}
    QScrollBar::handle:horizontal {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 rgba(126, 242, 244, 0.92),
            stop:0.55 rgba(29, 208, 214, 0.68),
            stop:1 rgba(14, 126, 146, 0.58));
        min-width: {min_handle}px;
        border-radius: {radius}px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 rgba(176, 252, 253, 0.98),
            stop:0.58 rgba(89, 231, 235, 0.84),
            stop:1 rgba(29, 208, 214, 0.70));
    }}
    QScrollBar::handle:horizontal:pressed {{
        background: rgba(241, 190, 103, 0.92);
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0px;
        height: 0px;
        border: none;
        background: transparent;
    }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal,
    QScrollBar::up-arrow:vertical, QScrollBar::down-arrow:vertical,
    QScrollBar::left-arrow:horizontal, QScrollBar::right-arrow:horizontal {{
        background: transparent;
        border: none;
        width: 0px;
        height: 0px;
    }}
    """


def text_edit_stylesheet():
    return f"""
    QTextEdit {{
        background-color: rgba(20, 34, 54, 0.82);
        color: {APP_COLORS["text"]};
        border: 1px solid rgba(111, 145, 182, 0.18);
        border-radius: 20px;
        padding: 14px;
        font-family: Consolas, 'Microsoft YaHei UI';
        font-size: 13px;
    }}
    {scrollbar_stylesheet(compact=False)}
    """
