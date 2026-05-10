import json
import os
import queue
import threading
import time
import ctypes
from ctypes import wintypes
from collections import deque

from PySide6.QtCore import QPoint, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFont, QImage, QMouseEvent, QPainter, QPainterPath, QPen, QLinearGradient, QColor, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.paths import ensure_writable_file, resource_path
from core.monthly_card_reset import (
    CONFIG_KEY_ENABLED as MONTHLY_CARD_RESET_ENABLED_KEY,
    CONFIG_KEY_LAST_DATE as MONTHLY_CARD_RESET_LAST_DATE_KEY,
    DEFAULT_CONFIG as MONTHLY_CARD_RESET_DEFAULT_CONFIG,
    MonthlyCardDailyResetScheduler,
    perform_double_escape_reset,
)
from core.state_machine import StateMachine
from core.version import APP_DISPLAY_NAME, APP_VERSION
from gui.encyclopedia import EncyclopediaWidget
from gui.fishing_record import FishingRecordWidget
from gui.theme import (
    APP_COLORS,
    add_shadow,
    panel_stylesheet,
    primary_button_stylesheet,
    scroll_area_stylesheet,
    scrollbar_stylesheet,
    secondary_button_stylesheet,
    text_edit_stylesheet,
)

CONFIG_FILE = ensure_writable_file("config.json")


class BackdropFrame(QFrame):
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        path = QPainterPath()
        path.addRoundedRect(rect.adjusted(0, 0, -1, -1), 32, 32)
        painter.fillPath(path, QColor(APP_COLORS["bg_alt"]))

        glow1 = QLinearGradient(rect.topLeft(), rect.bottomRight())
        glow1.setColorAt(0.0, QColor(17, 199, 214, 62))
        glow1.setColorAt(0.55, QColor(8, 18, 30, 0))
        glow1.setColorAt(1.0, QColor(17, 199, 214, 0))
        painter.fillPath(path, glow1)

        glow2 = QLinearGradient(rect.topRight(), rect.bottomLeft())
        glow2.setColorAt(0.0, QColor(120, 170, 255, 28))
        glow2.setColorAt(0.4, QColor(10, 18, 28, 0))
        glow2.setColorAt(1.0, QColor(10, 18, 28, 0))
        painter.fillPath(path, glow2)

        painter.setPen(QPen(QColor(74, 107, 141, 72), 1))
        painter.drawPath(path)
        super().paintEvent(event)


class NavButton(QPushButton):
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedHeight(52)
        self.setFont(QFont("Microsoft YaHei UI", 11, QFont.DemiBold))
        self.setStyleSheet(
            f"""
            QPushButton {{
                text-align: left;
                padding-left: 18px;
                color: {APP_COLORS['text_dim']};
                border: 1px solid transparent;
                outline: none;
                border-radius: 18px;
                background-color: transparent;
            }}
            QPushButton:hover {{
                color: {APP_COLORS['text']};
                background-color: rgba(255, 255, 255, 0.04);
            }}
            QPushButton:checked {{
                color: {APP_COLORS['accent_soft']};
                background-color: rgba(29, 208, 214, 0.14);
                border: 1px solid rgba(29, 208, 214, 0.36);
            }}
            """
        )


class SettingsCategoryButton(QPushButton):
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMinimumHeight(46)
        self.setFont(QFont("Microsoft YaHei UI", 10, QFont.DemiBold))
        self.setStyleSheet(
            f"""
            QPushButton {{
                text-align: left;
                padding: 0 14px;
                color: {APP_COLORS['text_dim']};
                border: 1px solid rgba(111, 145, 182, 0.12);
                outline: none;
                border-radius: 15px;
                background-color: rgba(255, 255, 255, 0.025);
            }}
            QPushButton:hover {{
                color: {APP_COLORS['text']};
                background-color: rgba(255, 255, 255, 0.055);
                border: 1px solid rgba(111, 145, 182, 0.22);
            }}
            QPushButton:checked {{
                color: {APP_COLORS['accent_soft']};
                background-color: rgba(29, 208, 214, 0.14);
                border: 1px solid rgba(29, 208, 214, 0.42);
            }}
            """
        )


class TitleButton(QPushButton):
    def __init__(self, kind, hover_color, parent=None):
        super().__init__("", parent)
        self.kind = kind
        self.hover_color = hover_color
        self.setFixedSize(46, 34)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_Hover, True)
        self.setStyleSheet("QPushButton { background: transparent; border: none; outline: none; }")

    def set_kind(self, kind):
        if self.kind != kind:
            self.kind = kind
            self.update()

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = self.rect().adjusted(3, 4, -3, -4)
        if self.isDown():
            bg = QColor(255, 255, 255, 38)
        elif self.underMouse():
            bg = QColor(self.hover_color)
        else:
            bg = QColor(255, 255, 255, 0)
        painter.setPen(Qt.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, 11, 11)

        icon_color = QColor(APP_COLORS["text"])
        if self.kind == "close" and self.underMouse():
            icon_color = QColor(255, 255, 255)
        elif not self.underMouse():
            icon_color = QColor(APP_COLORS["text_dim"])

        pen = QPen(icon_color, 1.35)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)

        cx = self.width() / 2
        cy = self.height() / 2
        if self.kind == "min":
            painter.drawLine(int(cx - 5), int(cy + 1), int(cx + 5), int(cy + 1))
        elif self.kind == "max":
            painter.drawRect(int(cx - 5), int(cy - 5), 10, 10)
        elif self.kind == "restore":
            painter.drawRect(int(cx - 6), int(cy - 2), 8, 8)
            painter.drawRect(int(cx - 2), int(cy - 6), 8, 8)
        elif self.kind == "close":
            painter.drawLine(int(cx - 5), int(cy - 5), int(cx + 5), int(cy + 5))
            painter.drawLine(int(cx + 5), int(cy - 5), int(cx - 5), int(cy + 5))


class LogoImageCard(QFrame):
    def __init__(self, image_path=None, parent=None):
        super().__init__(parent)
        self.image_path = image_path or resource_path("logo.jpg")
        self.source_pixmap = QPixmap(self.image_path) if os.path.exists(self.image_path) else QPixmap()
        self.setFixedSize(254, 146)
        self.setStyleSheet("QFrame { background: transparent; border: none; }")

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        rect = self.rect().adjusted(0, 0, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(rect, 28, 28)

        if not self.source_pixmap.isNull():
            scaled = self.source_pixmap.scaled(
                self.size(),
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
            x = int((self.width() - scaled.width()) / 2)
            y = int((self.height() - scaled.height()) / 2)
            painter.setClipPath(path)
            painter.drawPixmap(x, y, scaled)
            painter.setClipping(False)
        else:
            fallback = QLinearGradient(rect.topLeft(), rect.bottomRight())
            fallback.setColorAt(0.0, QColor(29, 208, 214, 190))
            fallback.setColorAt(1.0, QColor(10, 45, 66, 220))
            painter.fillPath(path, fallback)

        sheen = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        sheen.setColorAt(0.0, QColor(255, 255, 255, 32))
        sheen.setColorAt(0.45, QColor(255, 255, 255, 0))
        sheen.setColorAt(1.0, QColor(0, 0, 0, 28))
        painter.fillPath(path, sheen)

        painter.setPen(QPen(QColor(136, 230, 238, 72), 1))
        painter.drawPath(path)


class TitleBrand(QFrame):
    def __init__(self, window=None, parent=None):
        super().__init__(parent)
        self.window_ref = window
        self.setStyleSheet("QFrame { background: transparent; border: none; }")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)

        mark = QFrame()
        mark.setFixedSize(5, 24)
        mark.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        mark.setStyleSheet(
            f"""
            QFrame {{
                background-color: {APP_COLORS['accent_soft']};
                border: none;
                border-radius: 2px;
            }}
            """
        )
        layout.addWidget(mark, 0, Qt.AlignVCenter)

        title = QLabel(
            "<span style='color:#63E4E4;'>异环</span>"
            "<span style='color:#F3F8FF;'>自动钓鱼</span>"
        )
        title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        title.setStyleSheet(
            """
            QLabel {
                background: transparent;
                border: none;
                font-family: 'Microsoft YaHei UI';
                font-size: 15px;
                font-weight: 900;
            }
            """
        )
        layout.addWidget(title, 0, Qt.AlignVCenter)

        self.version_label = QLabel(f"YHo AutoFish v{APP_VERSION}")
        self.version_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.version_label.setStyleSheet(
            f"""
            QLabel {{
                background-color: rgba(29, 208, 214, 0.34);
                border: 1px solid rgba(99, 228, 228, 0.42);
                border-radius: 11px;
                color: #BAF1F5;
                padding: 4px 14px;
                font-family: 'Microsoft YaHei UI';
                font-size: 9px;
                font-weight: bold;
            }}
            """
        )
        layout.addWidget(self.version_label, 0, Qt.AlignVCenter)

    def mousePressEvent(self, event):
        if self.window_ref is not None and event.button() == Qt.LeftButton:
            title_bar = getattr(self.window_ref, "title_bar", None)
            if title_bar is not None:
                title_bar.dragging = True
                title_bar.drag_pos = event.globalPosition().toPoint() - self.window_ref.frameGeometry().topLeft()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        title_bar = getattr(self.window_ref, "title_bar", None) if self.window_ref is not None else None
        if title_bar is not None and title_bar.dragging and not self.window_ref.isMaximized():
            self.window_ref.move(event.globalPosition().toPoint() - title_bar.drag_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        title_bar = getattr(self.window_ref, "title_bar", None) if self.window_ref is not None else None
        if title_bar is not None:
            title_bar.dragging = False
        super().mouseReleaseEvent(event)


class CustomTitleBar(QFrame):
    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window_ref = window
        self.drag_pos = QPoint()
        self.dragging = False
        self.setFixedHeight(56)
        self.setStyleSheet(
            """
            QFrame {
                background-color: rgba(15, 27, 43, 0.86);
                border-top-left-radius: 32px;
                border-top-right-radius: 32px;
                border: 1px solid rgba(74, 107, 141, 0.22);
                border-bottom: none;
            }
            """
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 12, 14, 10)
        layout.setSpacing(10)

        self.title_brand = TitleBrand(window)
        layout.addWidget(self.title_brand)
        layout.addStretch()

        self.btn_min = TitleButton("min", "rgba(90, 129, 166, 0.22)")
        self.btn_max = TitleButton("max", "rgba(90, 129, 166, 0.22)")
        self.btn_close = TitleButton("close", "rgba(255, 102, 126, 0.58)")

        self.btn_min.clicked.connect(self.window_ref.showMinimized)
        self.btn_max.clicked.connect(self.window_ref.toggle_maximize_restore)
        self.btn_close.clicked.connect(self.window_ref.close)

        layout.addWidget(self.btn_min)
        layout.addWidget(self.btn_max)
        layout.addWidget(self.btn_close)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.window_ref.toggle_maximize_restore()
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.dragging = True
            self.drag_pos = event.globalPosition().toPoint() - self.window_ref.frameGeometry().topLeft()
            event.accept()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self.dragging and not self.window_ref.isMaximized():
            self.window_ref.move(event.globalPosition().toPoint() - self.drag_pos)
            event.accept()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.dragging = False
        super().mouseReleaseEvent(event)

    def sync_state(self):
        self.btn_max.set_kind("restore" if self.window_ref.isMaximized() else "max")


class StatusChip(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedHeight(36)
        self.set_status("待机中", "idle")

    def set_status(self, text, tone="idle"):
        color = {
            "idle": APP_COLORS["text_dim"],
            "running": APP_COLORS["accent_soft"],
            "stopped": APP_COLORS["danger"],
        }.get(tone, APP_COLORS["text_dim"])
        self.setText(text)
        self.setStyleSheet(
            f"""
            QLabel {{
                background-color: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(111, 145, 182, 0.18);
                border-radius: 18px;
                color: {color};
                padding: 0 12px;
                font-size: 12px;
                font-weight: 800;
            }}
            """
        )


class NoWheelSlider(QSlider):
    def wheelEvent(self, event):
        event.ignore()


class RecognitionInitWorker(QThread):
    completed = Signal(bool, str)

    def __init__(self, state_machine, parent=None):
        super().__init__(parent)
        self.state_machine = state_machine

    def run(self):
        try:
            ok = self.state_machine.prepare_recognition_modules()
            if self.isInterruptionRequested():
                return
            if ok:
                self.completed.emit(True, "识别模块初始化完成，可以开始钓鱼。")
            else:
                self.completed.emit(False, self.state_machine.get_ocr_init_failure_message())
        except Exception as exc:
            if self.isInterruptionRequested():
                return
            detail = self.state_machine.get_ocr_init_failure_message()
            self.completed.emit(False, f"{detail} 原始异常: {exc}")


class TakeoverPauseDialog(QDialog):
    def __init__(self, detail, parent=None):
        super().__init__(None)
        self.setModal(False)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(620, 330)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(0)

        shell = QFrame()
        shell.setStyleSheet(
            """
            QFrame {
                background-color: rgba(19, 7, 11, 0.98);
                border: 2px solid rgba(255, 87, 104, 0.92);
                border-radius: 28px;
            }
            """
        )
        add_shadow(shell, blur=42, alpha=180, offset=(0, 12))
        root.addWidget(shell)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(30, 26, 30, 24)
        layout.setSpacing(14)

        badge = QLabel("自动钓鱼已暂停")
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(
            """
            QLabel {
                background-color: rgba(255, 87, 104, 0.20);
                border: 1px solid rgba(255, 151, 90, 0.55);
                border-radius: 18px;
                color: #FFE7B0;
                font-size: 28px;
                font-weight: 900;
                padding: 12px 18px;
            }
            """
        )
        layout.addWidget(badge)

        detail_label = QLabel(f"检测到用户接管：{detail or '游戏窗口内输入'}")
        detail_label.setWordWrap(True)
        detail_label.setAlignment(Qt.AlignCenter)
        detail_label.setStyleSheet(
            "background: transparent; border: none; color: #FFF7E8; font-size: 15px; font-weight: 800;"
        )
        layout.addWidget(detail_label)

        body = QLabel("程序已释放全部按键并停止后续操作。需要继续时，请重新点击“开始钓鱼”，并保持挂机状态不要操作游戏。")
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignCenter)
        body.setStyleSheet(
            "background: transparent; border: none; color: #FFD3D7; font-size: 13px; line-height: 1.6;"
        )
        layout.addWidget(body)

        close_btn = QPushButton("我知道了")
        close_btn.setFocusPolicy(Qt.NoFocus)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setMinimumHeight(42)
        close_btn.setStyleSheet(
            """
            QPushButton {
                background-color: rgba(255, 207, 102, 0.96);
                color: #261307;
                border: none;
                border-radius: 18px;
                padding: 8px 24px;
                font-size: 14px;
                font-weight: 900;
            }
            QPushButton:hover {
                background-color: rgba(255, 226, 143, 1.0);
            }
            """
        )
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, 0, Qt.AlignCenter)


class ToastPopup(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setStyleSheet(
            f"""
            QFrame {{
                background-color: rgba(9, 20, 34, 0.94);
                border: 1px solid rgba(99, 228, 228, 0.36);
                border-radius: 18px;
            }}
            QLabel {{
                background: transparent;
                border: none;
                color: {APP_COLORS['text']};
                font-size: 13px;
                font-weight: 800;
            }}
            """
        )
        add_shadow(self, blur=24, alpha=110, offset=(0, 8))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(8)
        self.label = QLabel("")
        layout.addWidget(self.label)
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide)
        self.hide()

    def show_message(self, text, tone="info"):
        color = {
            "success": APP_COLORS["success"],
            "warning": APP_COLORS["warning"],
            "danger": APP_COLORS["danger"],
        }.get(tone, APP_COLORS["accent_soft"])
        self.label.setText(f"<span style='color:{color};'>●</span> {text}")
        self.adjustSize()
        self.reposition()
        self.raise_()
        self.show()
        self.hide_timer.start(2200)

    def reposition(self):
        parent = self.parentWidget()
        if parent:
            x = parent.width() - self.width() - 34
            y = 78
            self.move(max(24, x), y)


class FloatingControlWindow(QFrame):
    _EVENT_OBJECT_LOCATIONCHANGE = 0x800B
    _OBJID_WINDOW = 0
    _WINEVENT_OUTOFCONTEXT = 0x0000
    _WINEVENT_SKIPOWNPROCESS = 0x0002
    _SWP_NOSIZE = 0x0001
    _SWP_NOZORDER = 0x0004
    _SWP_NOACTIVATE = 0x0010
    _SWP_ASYNCWINDOWPOS = 0x4000
    _WinEventProc = ctypes.WINFUNCTYPE(
        None,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.HWND,
        wintypes.LONG,
        wintypes.LONG,
        wintypes.DWORD,
        wintypes.DWORD,
    )

    def __init__(self, app_window):
        super().__init__(None)
        self.app_window = app_window
        self._last_window_find = 0.0
        self._last_target_pos = None
        self._event_hook = None
        self._event_callback = None
        self._pending_hook_update = False
        self._user_visible_requested = False
        self._temporarily_hidden_by_game = False
        self._hidden_for_capture = False
        self._collapsed = False
        self._active_page_index = 0
        self._last_log_version = -1
        self._last_log_text = None
        self.setWindowTitle("异环自动钓鱼悬浮控制")
        self.setWindowFlags(
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.NoFocus)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(0)

        self.shell = QFrame()
        self.shell.setStyleSheet(
            """
            QFrame {
                background-color: rgba(10, 20, 34, 0.92);
                border: 1px solid rgba(103, 234, 236, 0.28);
                border-radius: 22px;
            }
            """
        )
        add_shadow(self.shell, blur=28, alpha=120, offset=(0, 10))
        root.addWidget(self.shell)

        layout = QVBoxLayout(self.shell)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(11)

        header = QHBoxLayout()
        title = QLabel("钓鱼悬浮窗")
        title.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 14px; font-weight: 900;"
        )
        header.addWidget(title)
        header.addStretch()

        self.collapse_btn = QPushButton("−")
        self.collapse_btn.setFixedSize(30, 30)
        self.collapse_btn.setFocusPolicy(Qt.NoFocus)
        self.collapse_btn.setCursor(Qt.PointingHandCursor)
        self.collapse_btn.setToolTip("折叠悬浮窗")
        self.collapse_btn.setStyleSheet(self._icon_button_stylesheet())
        self.collapse_btn.clicked.connect(self.toggle_collapsed)
        header.addWidget(self.collapse_btn)

        self.close_btn = QPushButton("×")
        self.close_btn.setFixedSize(30, 30)
        self.close_btn.setFocusPolicy(Qt.NoFocus)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setToolTip("关闭悬浮窗")
        self.close_btn.setStyleSheet(self._icon_button_stylesheet(danger=True))
        self.close_btn.clicked.connect(self.hide_by_user)
        header.addWidget(self.close_btn)
        layout.addLayout(header)

        self.status_label = QLabel()
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setFixedHeight(34)
        layout.addWidget(self.status_label)

        self.mode_bar = QFrame()
        self.mode_bar.setStyleSheet(
            """
            QFrame {
                background-color: rgba(255, 255, 255, 0.045);
                border: 1px solid rgba(111, 145, 182, 0.16);
                border-radius: 17px;
            }
            """
        )
        mode_layout = QHBoxLayout(self.mode_bar)
        mode_layout.setContentsMargins(4, 4, 4, 4)
        mode_layout.setSpacing(4)

        self.control_page_btn = QPushButton("控制")
        self.control_page_btn.setCheckable(True)
        self.control_page_btn.setFocusPolicy(Qt.NoFocus)
        self.control_page_btn.setCursor(Qt.PointingHandCursor)
        self.control_page_btn.clicked.connect(lambda: self.switch_floating_page(0))
        mode_layout.addWidget(self.control_page_btn)

        self.log_page_btn = QPushButton("日志")
        self.log_page_btn.setCheckable(True)
        self.log_page_btn.setFocusPolicy(Qt.NoFocus)
        self.log_page_btn.setCursor(Qt.PointingHandCursor)
        self.log_page_btn.clicked.connect(lambda: self.switch_floating_page(1))
        mode_layout.addWidget(self.log_page_btn)
        layout.addWidget(self.mode_bar)

        self.body_stack = QStackedWidget()
        self.body_stack.setStyleSheet("QStackedWidget { background: transparent; border: none; }")
        layout.addWidget(self.body_stack)

        self.control_page = QWidget()
        self.control_page.setStyleSheet("background: transparent; border: none;")
        control_layout = QVBoxLayout(self.control_page)
        control_layout.setContentsMargins(0, 0, 0, 0)
        control_layout.setSpacing(11)

        actions = QHBoxLayout()
        actions.setSpacing(8)

        self.start_btn = QPushButton("▶ 开始")
        self.start_btn.setFixedHeight(42)
        self.start_btn.setFocusPolicy(Qt.NoFocus)
        self.start_btn.setCursor(Qt.PointingHandCursor)
        self.start_btn.clicked.connect(self.app_window.handle_primary_action)
        actions.addWidget(self.start_btn)

        self.stop_btn = QPushButton("■ 停止")
        self.stop_btn.setFixedHeight(42)
        self.stop_btn.setFocusPolicy(Qt.NoFocus)
        self.stop_btn.setCursor(Qt.PointingHandCursor)
        self.stop_btn.clicked.connect(self.app_window.stop_bot)
        actions.addWidget(self.stop_btn)
        control_layout.addLayout(actions)

        self.debug_panel = QFrame()
        self.debug_panel.setProperty("variant", "soft")
        self.debug_panel.setStyleSheet(panel_stylesheet())
        self.debug_panel.setMinimumHeight(198)
        debug_layout = QVBoxLayout(self.debug_panel)
        debug_layout.setContentsMargins(12, 12, 12, 12)
        debug_layout.setSpacing(10)

        debug_title = QLabel("调试溜鱼视图")
        debug_title.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['accent_soft']}; font-size: 12px; font-weight: 800;"
        )
        debug_layout.addWidget(debug_title)

        self.debug_preview = QLabel("等待画面...")
        self.debug_preview.setAlignment(Qt.AlignCenter)
        self.debug_preview.setFixedSize(236, 118)
        self.debug_preview.setStyleSheet(
            """
            background-color: rgba(5, 12, 20, 0.78);
            border: 1px solid rgba(87, 119, 153, 0.18);
            border-radius: 16px;
            color: #9AB0CA;
            font-size: 12px;
            font-weight: 700;
            """
        )
        debug_layout.addWidget(self.debug_preview)
        control_layout.addWidget(self.debug_panel)
        self.body_stack.addWidget(self.control_page)

        self.log_page = QWidget()
        self.log_page.setStyleSheet("background: transparent; border: none;")
        log_layout = QVBoxLayout(self.log_page)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(0)

        self.floating_log_view = QPlainTextEdit()
        self.floating_log_view.setReadOnly(True)
        self.floating_log_view.setUndoRedoEnabled(False)
        self.floating_log_view.setMaximumBlockCount(int(self.app_window.config.get("log_line_limit", 320)))
        self.floating_log_view.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.floating_log_view.setStyleSheet(self._floating_log_stylesheet())
        log_layout.addWidget(self.floating_log_view)
        self.body_stack.addWidget(self.log_page)
        self.switch_floating_page(0)

        self.position_timer = QTimer(self)
        self.position_timer.setTimerType(Qt.PreciseTimer)
        self.position_timer.timeout.connect(self.position_near_game)
        self.position_timer.setInterval(16)

        self.visibility_timer = QTimer(self)
        self.visibility_timer.setTimerType(Qt.CoarseTimer)
        self.visibility_timer.timeout.connect(self.sync_game_visibility)
        self.visibility_timer.setInterval(250)
        self.refresh_state()

    def _icon_button_stylesheet(self, danger=False):
        hover_bg = "rgba(255, 102, 126, 0.24)" if danger else "rgba(29, 208, 214, 0.16)"
        hover_border = "rgba(255, 102, 126, 0.34)" if danger else "rgba(29, 208, 214, 0.32)"
        return f"""
        QPushButton {{
            background-color: rgba(255, 255, 255, 0.05);
            color: {APP_COLORS['text_dim']};
            border: 1px solid rgba(111, 145, 182, 0.18);
            border-radius: 15px;
            font-size: 16px;
            font-weight: 900;
        }}
        QPushButton:hover {{
            background-color: {hover_bg};
            border: 1px solid {hover_border};
            color: {APP_COLORS['text']};
        }}
        """

    def _mode_button_stylesheet(self, active=False):
        if active:
            return f"""
            QPushButton {{
                background-color: rgba(29, 208, 214, 0.22);
                color: {APP_COLORS['accent_soft']};
                border: 1px solid rgba(29, 208, 214, 0.35);
                border-radius: 13px;
                min-height: 28px;
                font-size: 12px;
                font-weight: 900;
            }}
            """
        return f"""
        QPushButton {{
            background-color: transparent;
            color: {APP_COLORS['text_dim']};
            border: 1px solid transparent;
            border-radius: 13px;
            min-height: 28px;
            font-size: 12px;
            font-weight: 800;
        }}
        QPushButton:hover {{
            background-color: rgba(255, 255, 255, 0.055);
            color: {APP_COLORS['text']};
        }}
        """

    def _floating_log_stylesheet(self):
        return f"""
        QPlainTextEdit {{
            background-color: rgba(5, 12, 20, 0.82);
            color: {APP_COLORS['text_dim']};
            border: 1px solid rgba(87, 119, 153, 0.18);
            border-radius: 16px;
            padding: 10px;
            font-family: Consolas, 'Microsoft YaHei UI';
            font-size: 12px;
            selection-background-color: rgba(29, 208, 214, 0.24);
        }}
        {scrollbar_stylesheet(compact=True)}
        """

    def switch_floating_page(self, index):
        self._active_page_index = 1 if index == 1 else 0
        self.body_stack.setCurrentIndex(self._active_page_index)
        self.control_page_btn.setChecked(self._active_page_index == 0)
        self.log_page_btn.setChecked(self._active_page_index == 1)
        self.control_page_btn.setStyleSheet(self._mode_button_stylesheet(self._active_page_index == 0))
        self.log_page_btn.setStyleSheet(self._mode_button_stylesheet(self._active_page_index == 1))
        if self._active_page_index == 1:
            self.refresh_log_view(force=True)
        self.refresh_panel_size()

    def toggle_collapsed(self):
        self._collapsed = not self._collapsed
        self.collapse_btn.setText("□" if self._collapsed else "−")
        self.collapse_btn.setToolTip("展开悬浮窗" if self._collapsed else "折叠悬浮窗")
        if not self._collapsed and self._active_page_index == 1:
            self.refresh_log_view(force=True)
        self.refresh_panel_size()
        self.position_near_game()

    def refresh_panel_size(self):
        debug_enabled = bool(self.app_window.config.get("debug_mode", False))
        self.debug_panel.setVisible(debug_enabled and not self._collapsed and self._active_page_index == 0)
        self.mode_bar.setVisible(not self._collapsed)
        self.body_stack.setVisible(not self._collapsed)

        if self._collapsed:
            width, height = 304, 112
        elif self._active_page_index == 1:
            width, height = 340, 410
        else:
            width = 304
            height = 452 if debug_enabled else 226

        self.setFixedSize(width, height)
        self.adjustSize()

    def refresh_log_view(self, force=False):
        if not hasattr(self, "floating_log_view"):
            return
        if not force and (not self.isVisible() or self._collapsed or self._active_page_index != 1):
            return

        version = getattr(self.app_window, "_log_version", 0)
        if not force and version == self._last_log_version:
            return

        self.floating_log_view.setMaximumBlockCount(int(self.app_window.config.get("log_line_limit", 320)))
        text = "\n".join(self.app_window.log_deque)
        if not text:
            text = "--- 异环自动钓鱼初始化完成 ---\n请确保游戏窗口处于可操作状态。"
        if force or text != self._last_log_text:
            self.floating_log_view.setPlainText(text)
            scrollbar = self.floating_log_view.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
            self._last_log_text = text
        self._last_log_version = version

    @property
    def user_visible_requested(self):
        return self._user_visible_requested

    def set_user_visible_requested(self, requested):
        self._user_visible_requested = bool(requested)
        if not self._user_visible_requested:
            self._temporarily_hidden_by_game = False
            self._release_event_hook()
            if self.position_timer.isActive():
                self.position_timer.stop()
            if self.visibility_timer.isActive():
                self.visibility_timer.stop()
            self.hide()
            if hasattr(self.app_window, "_sync_user_takeover_exclude_rects"):
                self.app_window._sync_user_takeover_exclude_rects()
            self._update_toggle_button()
            return

        if not self.visibility_timer.isActive():
            self.visibility_timer.start()
        self.sync_game_visibility()

    def hide_by_user(self):
        self.set_user_visible_requested(False)

    def _update_toggle_button(self):
        if hasattr(self.app_window, "float_toggle_btn"):
            self.app_window.float_toggle_btn.setText("隐藏" if self._user_visible_requested else "悬浮窗")

    def sync_game_visibility(self):
        if not self._user_visible_requested:
            return
        if self._hidden_for_capture:
            if self.isVisible():
                self.hide()
            return

        rect = self._current_game_rect(allow_find=True)
        if rect is None:
            self._hide_until_game_visible()
            return

        self._temporarily_hidden_by_game = False
        self._update_toggle_button()
        if not self.isVisible():
            self.show()
            self.raise_()
        self.position_near_game(rect)

    def _hide_until_game_visible(self):
        self._temporarily_hidden_by_game = True
        self._last_target_pos = None
        self._release_event_hook()
        if self.isVisible():
            self.hide()
            if hasattr(self.app_window, "_sync_user_takeover_exclude_rects"):
                self.app_window._sync_user_takeover_exclude_rects()
        self._update_toggle_button()

    def set_capture_hidden(self, hidden):
        hidden = bool(hidden)
        if self._hidden_for_capture == hidden:
            return
        self._hidden_for_capture = hidden
        if hidden:
            self._last_target_pos = None
            self._release_event_hook()
            if self.isVisible():
                self.hide()
        elif self._user_visible_requested:
            if not self.visibility_timer.isActive():
                self.visibility_timer.start()
            self.sync_game_visibility()
        if hasattr(self.app_window, "_sync_user_takeover_exclude_rects"):
            self.app_window._sync_user_takeover_exclude_rects()

    def _current_game_rect(self, allow_find=False):
        wm = self.app_window.sm.wm
        rect = wm.get_client_rect()
        now = time.monotonic()
        if rect is None and allow_find and now - self._last_window_find > 1.2:
            self._last_window_find = now
            wm.find_window()
            rect = wm.get_client_rect()
        return rect

    def refresh_state(self):
        running = self.app_window.sm.is_running
        modules_ready = self.app_window.modules_ready
        modules_initializing = self.app_window.modules_initializing
        status_text = "运行中" if running else "待机中"
        status_color = APP_COLORS["accent_soft"] if running else APP_COLORS["text_dim"]
        dot_color = APP_COLORS["success"] if running else APP_COLORS["warning"]
        self.status_label.setText(f"<span style='color:{dot_color};'>●</span> {status_text}")
        self.status_label.setStyleSheet(
            f"""
            QLabel {{
                background-color: rgba(255, 255, 255, 0.05);
                border: 1px solid rgba(111, 145, 182, 0.18);
                border-radius: 16px;
                color: {status_color};
                font-size: 12px;
                font-weight: 900;
            }}
            """
        )
        if modules_initializing:
            self.start_btn.setText(self.app_window.init_button_text("▶ 初始化"))
        elif modules_ready:
            self.start_btn.setText("▶ 开始")
        else:
            self.start_btn.setText("▶ 初始化")
        self.start_btn.setEnabled(not running and not modules_initializing)
        self.stop_btn.setEnabled(running)
        self.start_btn.setStyleSheet(primary_button_stylesheet())
        self.stop_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: rgba(255, 82, 117, 0.16);
                color: {APP_COLORS['danger']};
                border: 1px solid rgba(255, 82, 117, 0.34);
                border-radius: 18px;
                min-height: 40px;
                padding: 0 14px;
                font-size: 13px;
                font-weight: 900;
            }}
            QPushButton:hover {{
                background-color: rgba(255, 82, 117, 0.26);
                color: {APP_COLORS['text']};
            }}
            QPushButton:disabled {{
                background-color: rgba(255, 255, 255, 0.035);
                color: {APP_COLORS['text_soft']};
                border: 1px solid rgba(111, 145, 182, 0.12);
            }}
            """
        )
        self.refresh_debug_visibility()

    def refresh_debug_visibility(self):
        self.refresh_panel_size()

    def _ensure_event_hook(self):
        if self._event_hook:
            return
        hwnd = self.app_window.sm.wm.hwnd
        if not hwnd:
            return

        def _callback(_hook, event, hwnd_event, id_object, _id_child, _event_thread, _event_time):
            if event != self._EVENT_OBJECT_LOCATIONCHANGE:
                return
            target_hwnd = self.app_window.sm.wm.hwnd
            if not hwnd_event or not target_hwnd:
                return
            if int(hwnd_event) != int(target_hwnd):
                return
            if id_object != self._OBJID_WINDOW:
                return
            if self._pending_hook_update:
                return
            self._pending_hook_update = True
            QTimer.singleShot(0, self._position_from_hook)

        self._event_callback = self._WinEventProc(_callback)
        user32 = ctypes.windll.user32
        try:
            user32.SetWinEventHook.restype = wintypes.HANDLE
            user32.SetWinEventHook.argtypes = [
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
                self._WinEventProc,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
            ]
            user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
        except Exception:
            pass
        self._event_hook = user32.SetWinEventHook(
            self._EVENT_OBJECT_LOCATIONCHANGE,
            self._EVENT_OBJECT_LOCATIONCHANGE,
            0,
            self._event_callback,
            0,
            0,
            self._WINEVENT_OUTOFCONTEXT | self._WINEVENT_SKIPOWNPROCESS,
        )

    def _release_event_hook(self):
        if self._event_hook:
            try:
                ctypes.windll.user32.UnhookWinEvent(self._event_hook)
            except Exception:
                pass
        self._event_hook = None
        self._event_callback = None
        self._pending_hook_update = False

    def _position_from_hook(self):
        self._pending_hook_update = False
        self.position_near_game()

    def _move_to_target(self, target):
        if self._last_target_pos == target:
            return
        self._last_target_pos = target
        if self.pos() == target:
            return
        try:
            ctypes.windll.user32.SetWindowPos(
                int(self.winId()),
                0,
                target.x(),
                target.y(),
                0,
                0,
                self._SWP_NOSIZE | self._SWP_NOZORDER | self._SWP_NOACTIVATE | self._SWP_ASYNCWINDOWPOS,
            )
        except Exception:
            self.move(target)

    def position_near_game(self, rect=None):
        if not self.isVisible():
            return

        if rect is None:
            rect = self._current_game_rect(allow_find=True)

        if rect:
            self._ensure_event_hook()
            left, top, _width, _height = rect
            target = QPoint(left + 16, top + 16)
        else:
            self._hide_until_game_visible()
            return

        self._move_to_target(target)
        if hasattr(self.app_window, "_sync_user_takeover_exclude_rects"):
            self.app_window._sync_user_takeover_exclude_rects()

    def showEvent(self, event):
        super().showEvent(event)
        if self._user_visible_requested and not self.position_timer.isActive():
            self.position_timer.start()
        self.refresh_state()
        self.refresh_log_view()
        self.position_near_game()

    def set_debug_frame(self, frame):
        if frame is None or not self.app_window.config.get("debug_mode", False):
            return
        if self._collapsed or self._active_page_index != 0:
            return
        rgb_frame = frame[:, :, ::-1].copy()
        height, width, channel = rgb_frame.shape
        image = QImage(rgb_frame.data, width, height, channel * width, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(image).scaled(
            self.debug_preview.size(),
            Qt.KeepAspectRatio,
            Qt.FastTransformation,
        )
        self.debug_preview.setPixmap(pixmap)

    def hideEvent(self, event):
        super().hideEvent(event)
        self._release_event_hook()
        if self.position_timer.isActive():
            self.position_timer.stop()
        self._update_toggle_button()

    def closeEvent(self, event):
        self._user_visible_requested = False
        if self.position_timer.isActive():
            self.position_timer.stop()
        if self.visibility_timer.isActive():
            self.visibility_timer.stop()
        self._release_event_hook()
        super().closeEvent(event)


class AppWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_DISPLAY_NAME} v{APP_VERSION}")
        self.resize(1420, 920)
        self.setMinimumSize(1200, 760)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self.default_config = {
            "tracking_strength": 180,
            "hold_threshold": 5,
            "deadzone_threshold": 1,
            "fishing_timeout": 180,
            "hook_wait_timeout": 90,
            "cast_animation_delay": 2,
            "settlement_close_delay": 1,
            "bar_missing_timeout": 3,
            "pre_control_timeout": 14,
            "recovery_timeout": 8,
            "fishing_result_check_interval": 0.65,
            "fishing_failed_check_interval": 1.25,
            "empty_ready_confirm_delay": 0.45,
            "bar_confidence_threshold": 0.45,
            "feed_forward_gain": 0.18,
            "safe_zone_ratio": 0.08,
            "control_release_cross_ratio": 0.012,
            "control_reengage_ratio": 0.018,
            "control_switch_ratio": 0.08,
            "control_min_hold_time": 0.14,
            "user_takeover_protection": True,
            "user_takeover_mouse_threshold": 12,
            "user_takeover_start_grace": 1.20,
            "log_line_limit": 320,
            "auto_switch_to_log": True,
            "debug_mode": False,
            "auto_sell_catch_threshold": 0,
            **MONTHLY_CARD_RESET_DEFAULT_CONFIG,
        }
        self.config = dict(self.default_config)
        self.load_config()

        self.log_queue = queue.Queue()
        self.debug_queue = queue.Queue()
        self.log_deque = deque(maxlen=int(self.config.get("log_line_limit", 320)))
        self._log_version = 0
        self.sm = StateMachine(log_queue=self.log_queue, debug_queue=self.debug_queue)
        self.floating_window = None
        self._main_hidden_for_capture = False
        self._main_capture_was_visible = False
        self._main_capture_geometry = None
        self._main_capture_window_state = None
        self.modules_ready = False
        self.modules_initializing = False
        self.init_animation_step = 0
        self.ocr_init_worker = None
        self._shutting_down = False
        self._settings_building = False
        self._settings_dirty = False
        self._settings_category_buttons = []
        self._settings_category_keys = {}
        self._setting_widgets = {}
        self._settings_saved_snapshot = {}
        self.monthly_card_reset_scheduler = MonthlyCardDailyResetScheduler()
        self._monthly_card_reset_in_progress = False

        self.init_ui()
        self._sync_runtime_preferences()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.process_queue)
        self.timer.start(60)

        self.monthly_card_reset_timer = QTimer(self)
        self.monthly_card_reset_timer.setTimerType(Qt.CoarseTimer)
        self.monthly_card_reset_timer.timeout.connect(self._check_monthly_card_daily_reset)
        self.monthly_card_reset_timer.start(15000)
        QTimer.singleShot(1000, self._check_monthly_card_daily_reset)

        self.init_animation_timer = QTimer(self)
        self.init_animation_timer.timeout.connect(self._tick_init_animation)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown_background_tasks)

    def load_config(self):
        if not os.path.exists(CONFIG_FILE):
            return
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as file:
                self.config.update(json.load(file))
        except Exception as exc:
            print(f"Config load error: {exc}")

    def save_config(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as file:
                json.dump(self.config, file, ensure_ascii=False, indent=4)
            self._sync_runtime_preferences()
            self.write_log("[配置] 高级设置已保存。")
            return True
        except Exception as exc:
            self.write_log(f"[配置] 保存失败: {exc}")
            return False

    def _save_config_silent(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as file:
                json.dump(self.config, file, ensure_ascii=False, indent=4)
            return True
        except Exception as exc:
            print(f"Config save error: {exc}")
            return False

    def _monthly_card_reset_enabled(self):
        snapshot = getattr(self, "_settings_saved_snapshot", {}) or {}
        if MONTHLY_CARD_RESET_ENABLED_KEY in snapshot:
            return bool(snapshot.get(MONTHLY_CARD_RESET_ENABLED_KEY))
        return bool(self.config.get(MONTHLY_CARD_RESET_ENABLED_KEY, False))

    def _save_monthly_card_reset_last_date(self, date_key):
        self.config[MONTHLY_CARD_RESET_LAST_DATE_KEY] = str(date_key or "")
        try:
            data = {}
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as file:
                    loaded = json.load(file)
                    if isinstance(loaded, dict):
                        data = loaded
            data[MONTHLY_CARD_RESET_LAST_DATE_KEY] = self.config[MONTHLY_CARD_RESET_LAST_DATE_KEY]
            if MONTHLY_CARD_RESET_ENABLED_KEY not in data:
                data[MONTHLY_CARD_RESET_ENABLED_KEY] = self._monthly_card_reset_enabled()
            with open(CONFIG_FILE, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=4)
            return True
        except Exception as exc:
            self.log_queue.put(f"[月卡复位] 记录每日触发日期失败: {exc}")
            return False

    def _check_monthly_card_daily_reset(self):
        if getattr(self, "_shutting_down", False):
            return
        if getattr(self, "_monthly_card_reset_in_progress", False):
            return
        if not self._monthly_card_reset_enabled():
            return

        last_date = self.config.get(MONTHLY_CARD_RESET_LAST_DATE_KEY, "")
        scheduler = self.monthly_card_reset_scheduler
        if not scheduler.should_trigger(True, last_date):
            return

        trigger_date = scheduler.date_key()
        self._monthly_card_reset_in_progress = True
        self.write_log("[月卡复位] 已到北京时间 05:02，准备执行 ESC、等待 2 秒、再 ESC。")
        thread = threading.Thread(
            target=self._run_monthly_card_daily_reset_sequence,
            args=(trigger_date,),
            daemon=True,
        )
        thread.start()

    def _run_monthly_card_daily_reset_sequence(self, trigger_date):
        try:
            ok = perform_double_escape_reset(
                self.sm.ctrl,
                window_manager=self.sm.wm,
                user_activity=self.sm.user_activity,
                input_lock=getattr(self.sm, "_input_lock", None),
                delay_seconds=2.0,
                tap_duration=0.12,
            )
            if ok:
                self._save_monthly_card_reset_last_date(trigger_date)
                self.log_queue.put("[月卡复位] 双 ESC 复位指令已完成。")
            else:
                self.log_queue.put("[月卡复位] 未找到或无法聚焦游戏窗口，本次未发送 ESC。")
        except Exception as exc:
            self.log_queue.put(f"[月卡复位] 执行失败: {exc}")
        finally:
            self._monthly_card_reset_in_progress = False

    def _sync_runtime_preferences(self):
        self.config["log_line_limit"] = int(self.config.get("log_line_limit", 320))
        self.log_deque = deque(self.log_deque, maxlen=self.config["log_line_limit"])
        self._log_version += 1
        if hasattr(self, "log_textbox"):
            self.log_textbox.setText("\n".join(self.log_deque))
        self._apply_state_machine_config()
        self._refresh_debug_view_state()
        if self.floating_window is not None:
            self.floating_window.refresh_state()
            self.floating_window.refresh_log_view(force=True)

    def _apply_state_machine_config(self):
        self.sm.update_config("tracking_strength", self.config.get("tracking_strength", 180))
        self.sm.update_config("t_hold", self.config.get("hold_threshold", 5))
        self.sm.update_config("t_deadzone", self.config.get("deadzone_threshold", 1))
        self.sm.update_config("fishing_timeout", self.config.get("fishing_timeout", 180))
        self.sm.update_config("hook_wait_timeout", self.config.get("hook_wait_timeout", 90))
        self.sm.update_config("cast_animation_delay", self.config.get("cast_animation_delay", 2))
        self.sm.update_config("settlement_close_delay", self.config.get("settlement_close_delay", 1))
        self.sm.update_config("bar_missing_timeout", self.config.get("bar_missing_timeout", 3))
        self.sm.update_config("pre_control_timeout", self.config.get("pre_control_timeout", 14))
        self.sm.update_config("recovery_timeout", self.config.get("recovery_timeout", 8))
        self.sm.update_config("fishing_result_check_interval", self.config.get("fishing_result_check_interval", 0.65))
        self.sm.update_config("fishing_failed_check_interval", self.config.get("fishing_failed_check_interval", 1.25))
        self.sm.update_config("empty_ready_confirm_delay", self.config.get("empty_ready_confirm_delay", 0.45))
        self.sm.update_config("bar_confidence_threshold", self.config.get("bar_confidence_threshold", 0.45))
        self.sm.update_config("feed_forward_gain", self.config.get("feed_forward_gain", 0.18))
        self.sm.update_config("safe_zone_ratio", self.config.get("safe_zone_ratio", 0.08))
        self.sm.update_config("control_release_cross_ratio", self.config.get("control_release_cross_ratio", 0.012))
        self.sm.update_config("control_reengage_ratio", self.config.get("control_reengage_ratio", 0.018))
        self.sm.update_config("control_switch_ratio", self.config.get("control_switch_ratio", 0.08))
        self.sm.update_config("control_min_hold_time", self.config.get("control_min_hold_time", 0.14))
        self.sm.update_config("user_takeover_protection", self.config.get("user_takeover_protection", True))
        self.sm.update_config("user_takeover_mouse_threshold", self.config.get("user_takeover_mouse_threshold", 12))
        self.sm.update_config("user_takeover_start_grace", self.config.get("user_takeover_start_grace", 1.20))
        self.sm.update_config("debug_mode", self.config.get("debug_mode", False))
        self.sm.update_config("auto_sell_catch_threshold", self.config.get("auto_sell_catch_threshold", 0))

    def _refresh_debug_view_state(self):
        if not hasattr(self, "debug_preview"):
            return

        if self.config.get("debug_mode", False):
            self.debug_state_label.setText("调试溜鱼视图已开启")
            if self.debug_preview.pixmap() is None:
                self.debug_preview.setText("等待溜鱼画面...")
            self.debug_help_label.setText("开始钓鱼后将实时显示识别到的绿条、黄条与中心位置。")
        else:
            self.debug_state_label.setText("调试溜鱼视图未开启")
            self.debug_preview.clear()
            self.debug_preview.setText("当前未开启")
            self.debug_help_label.setText("如需反馈识别问题，请在高级设置中开启调试溜鱼视图后再复现问题。")

    def _set_debug_frame(self, frame):
        if not hasattr(self, "debug_preview"):
            return
        if frame is None or not self.config.get("debug_mode", False):
            return

        rgb_frame = frame[:, :, ::-1].copy()
        height, width, channel = rgb_frame.shape
        image = QImage(rgb_frame.data, width, height, channel * width, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(image).scaled(
            self.debug_preview.size(),
            Qt.KeepAspectRatio,
            Qt.FastTransformation,
        )
        self.debug_preview.setPixmap(pixmap)
        if self.floating_window is not None and self.floating_window.isVisible():
            self.floating_window.set_debug_frame(frame)

    def init_ui(self):
        central = QWidget()
        central.setStyleSheet("background: transparent;")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(0)

        self.shell = BackdropFrame()
        shell_layout = QVBoxLayout(self.shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        root.addWidget(self.shell)

        self.title_bar = CustomTitleBar(self)
        shell_layout.addWidget(self.title_bar)

        content = QWidget()
        content.setStyleSheet(
            """
            QWidget {
                background: transparent;
                border-bottom-left-radius: 32px;
                border-bottom-right-radius: 32px;
            }
            """
        )
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(18, 18, 18, 18)
        content_layout.setSpacing(18)
        shell_layout.addWidget(content, 1)

        self.sidebar = self._build_sidebar()
        content_layout.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        self.stack.setStyleSheet("QStackedWidget { background: transparent; }")
        content_layout.addWidget(self.stack, 1)

        self.page_record = FishingRecordWidget(self.sm.record_mgr)
        self.page_encyclopedia = None
        self.page_encyclopedia_placeholder = QWidget()
        self.page_encyclopedia_placeholder.setStyleSheet("background: transparent;")
        self.page_log = self._build_log_page()
        self.page_settings = self._build_settings_page()

        self.stack.addWidget(self.page_record)
        self.stack.addWidget(self.page_encyclopedia_placeholder)
        self.stack.addWidget(self.page_log)
        self.stack.addWidget(self.page_settings)
        self.switch_page(0, self.nav_record)

        self.toast = ToastPopup(self)
        self.update_primary_buttons()

    def _ensure_encyclopedia_page(self):
        if self.page_encyclopedia is not None:
            return self.page_encyclopedia

        page = EncyclopediaWidget(self.sm.record_mgr)
        placeholder_index = self.stack.indexOf(self.page_encyclopedia_placeholder)
        if placeholder_index < 0:
            placeholder_index = 1
        self.stack.removeWidget(self.page_encyclopedia_placeholder)
        self.page_encyclopedia_placeholder.deleteLater()
        self.stack.insertWidget(placeholder_index, page)
        self.page_encyclopedia = page
        return page

    def toggle_maximize_restore(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        if hasattr(self, "title_bar"):
            self.title_bar.sync_state()

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, "title_bar"):
            self.title_bar.sync_state()

    def showEvent(self, event):
        super().showEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "toast") and self.toast.isVisible():
            self.toast.reposition()

    def closeEvent(self, event):
        self.shutdown_background_tasks()
        super().closeEvent(event)

    def shutdown_background_tasks(self):
        if getattr(self, "_shutting_down", False):
            return
        self._shutting_down = True

        for timer_name in ("timer", "monthly_card_reset_timer", "init_animation_timer"):
            timer = getattr(self, timer_name, None)
            if timer is not None and timer.isActive():
                timer.stop()

        if self.sm.is_running:
            self.sm.stop()

        if self.floating_window is not None:
            self.floating_window.close()

        self._stop_worker_thread("ocr_init_worker", "识别初始化线程", wait_ms=1800)

    def _stop_worker_thread(self, attr_name, label, wait_ms=1200, terminate_wait_ms=800):
        worker = getattr(self, attr_name, None)
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.requestInterruption()
                worker.quit()
                if not worker.wait(wait_ms):
                    print(f"[AppWindow] {label}关闭超时，正在强制结束。", flush=True)
                    worker.terminate()
                    worker.wait(terminate_wait_ms)
        except RuntimeError:
            pass
        finally:
            setattr(self, attr_name, None)

    def show_toast(self, text, tone="info"):
        if hasattr(self, "toast"):
            self.toast.show_message(text, tone)

    def init_button_text(self, prefix="初始化模块"):
        dots = "." * ((self.init_animation_step % 3) + 1)
        return f"{prefix}{dots}"

    def update_primary_buttons(self):
        running = self.sm.is_running
        if self.modules_initializing:
            self.btn_start.setText(self.init_button_text("初始化模块"))
            self.btn_start.setEnabled(False)
        elif self.modules_ready:
            self.btn_start.setText("开始钓鱼")
            self.btn_start.setEnabled(not running)
        else:
            self.btn_start.setText("初始化模块")
            self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        if self.floating_window is not None:
            self.floating_window.refresh_state()

    def _tick_init_animation(self):
        self.init_animation_step += 1
        self.update_primary_buttons()

    def handle_primary_action(self):
        if self.sm.is_running:
            return
        if not self.modules_ready:
            self.start_module_initialization()
            return
        self.start_bot()

    def start_module_initialization(self):
        if getattr(self, "_shutting_down", False):
            return
        if self.modules_ready:
            self.update_primary_buttons()
            return
        if self.modules_initializing:
            return
        self.modules_initializing = True
        self.init_animation_step = 0
        self.init_animation_timer.start(360)
        self.update_primary_buttons()
        self.write_log("[系统] 开始初始化鱼名、重量与界面文字 OCR 识别模块...")
        self.show_toast("正在初始化识别模块", "info")

        self.ocr_init_worker = RecognitionInitWorker(self.sm, self)
        self.ocr_init_worker.completed.connect(self._handle_module_init_result)
        self.ocr_init_worker.finished.connect(self.ocr_init_worker.deleteLater)
        self.ocr_init_worker.start()

    def _handle_module_init_result(self, ok, message):
        if getattr(self, "_shutting_down", False):
            return
        self.init_animation_timer.stop()
        self.modules_initializing = False
        self.modules_ready = bool(ok)
        self.update_primary_buttons()
        self.write_log(f"[系统] {message}")
        toast_message = message if ok else "识别模块初始化失败，详情已写入运行日志。"
        self.show_toast(toast_message, "success" if ok else "danger")
        self.ocr_init_worker = None

    def toggle_floating_window(self):
        if self.floating_window is None:
            self.floating_window = FloatingControlWindow(self)

        if self.floating_window.user_visible_requested:
            self.floating_window.set_user_visible_requested(False)
            return

        self.floating_window.refresh_state()
        self.floating_window.set_user_visible_requested(True)

    def _build_sidebar(self):
        panel = QFrame()
        panel.setFixedWidth(294)
        panel.setStyleSheet(
            """
            QFrame {
                background-color: rgba(10, 20, 35, 0.70);
                border: 1px solid rgba(62, 92, 123, 0.22);
                border-radius: 32px;
            }
            """
        )
        add_shadow(panel, blur=34, alpha=110, offset=(0, 12))

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        logo_card = LogoImageCard()
        add_shadow(logo_card, blur=24, alpha=96, offset=(0, 10))
        layout.addWidget(logo_card, 0, Qt.AlignHCenter)

        nav_panel = QFrame()
        nav_panel.setStyleSheet(
            """
            QFrame {
                background-color: rgba(18, 29, 44, 0.58);
                border: 1px solid rgba(111, 145, 182, 0.14);
                border-radius: 26px;
            }
            """
        )
        nav_layout = QVBoxLayout(nav_panel)
        nav_layout.setContentsMargins(12, 12, 12, 12)
        nav_layout.setSpacing(8)

        self.nav_record = NavButton("钓鱼记录")
        self.nav_encyclopedia = NavButton("图鉴记录")
        self.nav_log = NavButton("运行日志")
        self.nav_settings = NavButton("高级设置")

        self.nav_record.clicked.connect(lambda: self.switch_page(0, self.nav_record))
        self.nav_encyclopedia.clicked.connect(lambda: self.switch_page(1, self.nav_encyclopedia))
        self.nav_log.clicked.connect(lambda: self.switch_page(2, self.nav_log))
        self.nav_settings.clicked.connect(lambda: self.switch_page(3, self.nav_settings))

        for button in [self.nav_record, self.nav_encyclopedia, self.nav_log, self.nav_settings]:
            nav_layout.addWidget(button)
        layout.addWidget(nav_panel)

        control_panel = QFrame()
        control_panel.setMinimumHeight(238)
        control_panel.setStyleSheet(
            """
            QFrame {
                background-color: rgba(18, 29, 44, 0.66);
                border: 1px solid rgba(111, 145, 182, 0.16);
                border-radius: 26px;
            }
            """
        )
        control_layout = QVBoxLayout(control_panel)
        control_layout.setContentsMargins(18, 18, 18, 18)
        control_layout.setSpacing(12)

        status_title = QLabel("运行状态")
        status_title.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 16px; font-weight: 800;")
        control_layout.addWidget(status_title)

        self.status_chip = StatusChip()
        control_layout.addWidget(self.status_chip)

        control_hint = QLabel("可在主程序或游戏内悬浮窗控制开始与停止。")
        control_hint.setWordWrap(True)
        control_hint.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text_soft']}; font-size: 12px;")
        control_layout.addWidget(control_hint)

        self.btn_start = QPushButton("开始钓鱼")
        self.btn_start.setMinimumHeight(44)
        self.btn_start.setFocusPolicy(Qt.NoFocus)
        self.btn_start.setStyleSheet(primary_button_stylesheet())
        self.btn_start.clicked.connect(self.handle_primary_action)
        control_layout.addWidget(self.btn_start)

        self.btn_stop = QPushButton("停止运行")
        self.btn_stop.setMinimumHeight(42)
        self.btn_stop.setFocusPolicy(Qt.NoFocus)
        self.btn_stop.setStyleSheet(secondary_button_stylesheet())
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_bot)
        control_layout.addWidget(self.btn_stop)
        layout.addWidget(control_panel)

        layout.addStretch()

        author_panel = QFrame()
        author_panel.setMinimumHeight(96)
        author_panel.setStyleSheet(
            """
            QFrame {
                background-color: rgba(18, 29, 44, 0.52);
                border: 1px solid rgba(111, 145, 182, 0.14);
                border-radius: 20px;
            }
            """
        )
        author_layout = QHBoxLayout(author_panel)
        author_layout.setContentsMargins(12, 12, 12, 12)
        author_layout.setSpacing(8)
        author_layout.addStretch(1)

        self.float_toggle_btn = QPushButton("悬浮窗")
        self.float_toggle_btn.setFixedSize(62, 56)
        self.float_toggle_btn.setCursor(Qt.PointingHandCursor)
        self.float_toggle_btn.setFocusPolicy(Qt.NoFocus)
        self.float_toggle_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: rgba(22, 209, 214, 0.10);
                color: {APP_COLORS['accent_soft']};
                border: 1px solid rgba(22, 209, 214, 0.34);
                border-radius: 18px;
                font-size: 12px;
                font-weight: 900;
            }}
            QPushButton:hover {{
                background-color: rgba(22, 209, 214, 0.20);
                color: {APP_COLORS['text']};
            }}
            """
        )
        self.float_toggle_btn.clicked.connect(self.toggle_floating_window)
        author_layout.addWidget(self.float_toggle_btn, 0, Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(author_panel)
        return panel

    def _build_log_page(self):
        page = QFrame()
        page.setProperty("variant", "elevated")
        page.setStyleSheet(panel_stylesheet())

        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)

        title = QLabel("运行日志")
        title.setProperty("role", "headline")
        layout.addWidget(title)

        subtitle = QLabel("查看自动钓鱼的实时状态、异常提示与关键步骤输出。")
        subtitle.setProperty("role", "subtle")
        layout.addWidget(subtitle)

        content_row = QHBoxLayout()
        content_row.setSpacing(18)

        self.log_textbox = QTextEdit()
        self.log_textbox.setReadOnly(True)
        self.log_textbox.setStyleSheet(text_edit_stylesheet())
        self.log_textbox.append("--- 异环自动钓鱼初始化完成 ---\n请确保游戏窗口处于可操作状态。")
        content_row.addWidget(self.log_textbox, 5)

        debug_panel = QFrame()
        debug_panel.setProperty("variant", "soft")
        debug_panel.setStyleSheet(panel_stylesheet())
        add_shadow(debug_panel, blur=20, alpha=85, offset=(0, 8))
        debug_panel.setFixedWidth(340)

        debug_layout = QVBoxLayout(debug_panel)
        debug_layout.setContentsMargins(16, 16, 16, 16)
        debug_layout.setSpacing(10)

        debug_title = QLabel("调试溜鱼视图")
        debug_title.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 16px; font-weight: 800;"
        )
        debug_layout.addWidget(debug_title)

        debug_note = QLabel("用于回看溜鱼阶段识别到的绿条、黄条位置，方便定位识别问题。")
        debug_note.setWordWrap(True)
        debug_note.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px;"
        )
        debug_layout.addWidget(debug_note)

        self.debug_state_label = QLabel()
        self.debug_state_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['accent_soft']}; font-size: 12px; font-weight: 700;"
        )
        debug_layout.addWidget(self.debug_state_label)

        self.debug_preview = QLabel()
        self.debug_preview.setAlignment(Qt.AlignCenter)
        self.debug_preview.setMinimumSize(300, 118)
        self.debug_preview.setStyleSheet(
            """
            background-color: rgba(8, 15, 24, 0.78);
            border: 1px solid rgba(87, 119, 153, 0.18);
            border-radius: 18px;
            """
        )
        debug_layout.addWidget(self.debug_preview)

        self.debug_help_label = QLabel()
        self.debug_help_label.setWordWrap(True)
        self.debug_help_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_soft']}; font-size: 12px;"
        )
        debug_layout.addWidget(self.debug_help_label)
        debug_layout.addStretch()

        content_row.addWidget(debug_panel, 2)
        layout.addLayout(content_row, 1)
        self._refresh_debug_view_state()
        return page

    def _build_settings_page(self):
        self._settings_building = True
        self._setting_widgets = {}
        self._settings_category_buttons = []
        self._settings_category_keys = {}

        page = QWidget()
        page.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        content = QFrame()
        content.setProperty("variant", "elevated")
        content.setStyleSheet(panel_stylesheet())
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(28, 24, 28, 22)
        content_layout.setSpacing(16)

        title = QLabel("高级设置")
        title.setProperty("role", "headline")
        content_layout.addWidget(title)

        subtitle = QLabel("按用途查看和调整自动钓鱼参数。建议只修改当前遇到问题对应的分类，保存后立即应用。")
        subtitle.setProperty("role", "subtle")
        content_layout.addWidget(subtitle)

        body_row = QHBoxLayout()
        body_row.setSpacing(18)

        category_panel = QFrame()
        category_panel.setProperty("variant", "soft")
        category_panel.setFixedWidth(220)
        category_panel.setStyleSheet(panel_stylesheet())
        category_layout = QVBoxLayout(category_panel)
        category_layout.setContentsMargins(14, 14, 14, 14)
        category_layout.setSpacing(8)

        category_title = QLabel("设置分类")
        category_title.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 15px; font-weight: 900;"
        )
        category_layout.addWidget(category_title)

        category_note = QLabel("选择左侧分类后，只显示对应参数。")
        category_note.setWordWrap(True)
        category_note.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_soft']}; font-size: 11px;"
        )
        category_layout.addWidget(category_note)

        self.settings_category_layout = category_layout
        self.settings_stack = QStackedWidget()
        self.settings_stack.setStyleSheet("QStackedWidget { background: transparent; border: none; }")

        fishing_page, fishing_layout = self._build_settings_category_page(
            "溜鱼控制",
            "控制游标追随耐力条的力度、死区、前馈和按键切换逻辑。这里的参数会直接影响溜鱼手感。",
        )
        fishing_keys = []
        self.slider_hold = self._settings_block(
            fishing_layout,
            "跟鱼力度",
            "数值越大，PID 修正和前馈追赶越积极；程序会保持按键到接近越过中心，适配较慢的 A/D 移速。",
            self.config.get("tracking_strength", 180),
            70,
            240,
            "tracking_strength",
        )
        fishing_keys.append("tracking_strength")
        self.slider_hold_threshold = self._settings_block(
            fishing_layout,
            "稳定释放阈值",
            "游标接近绿条中心时重新触发按键的阈值；默认偏低，减少在中心一侧提前滑行。",
            self.config.get("hold_threshold", 5),
            2,
            30,
            "hold_threshold",
        )
        fishing_keys.append("hold_threshold")
        self.slider_deadzone = self._settings_block(
            fishing_layout,
            "跟鱼死区",
            "数值越小，游标出现约 1 像素偏离就会更快按键追赶；最低档会更频繁左右修正。",
            self.config.get("deadzone_threshold", 1),
            1,
            15,
            "deadzone_threshold",
        )
        fishing_keys.append("deadzone_threshold")
        self.slider_feed_forward = self._settings_block(
            fishing_layout,
            "预测追赶前馈",
            "根据耐力条移动趋势提前补偿 A/D 按键。值越大越主动，过大可能造成越过中心后的摆动。",
            self.config.get("feed_forward_gain", 0.18),
            0,
            45,
            "feed_forward_gain",
            value_scale=0.01,
            display_scale=1,
            display_suffix="%",
            config_decimals=3,
        )
        fishing_keys.append("feed_forward_gain")
        self.slider_safe_zone = self._settings_block(
            fishing_layout,
            "中心安全区宽度",
            "按耐力条宽度计算的安全区。值越小越贴近中心追鱼，值越大越稳但反应会更保守。",
            self.config.get("safe_zone_ratio", 0.08),
            4,
            28,
            "safe_zone_ratio",
            value_scale=0.01,
            display_scale=1,
            display_suffix="%",
            config_decimals=3,
        )
        fishing_keys.append("safe_zone_ratio")
        self.slider_release_cross = self._settings_block(
            fishing_layout,
            "过中心释放比例",
            "游标越过目标中心后释放当前方向的比例阈值。较低值会更快停手，适合现在较慢的官方移速。",
            self.config.get("control_release_cross_ratio", 0.012),
            1,
            12,
            "control_release_cross_ratio",
            value_scale=0.01,
            display_scale=1,
            display_suffix="%",
            config_decimals=3,
        )
        fishing_keys.append("control_release_cross_ratio")
        self.slider_reengage = self._settings_block(
            fishing_layout,
            "重新接管比例",
            "释放后再次按键追赶的比例阈值。数值越低，左右修正越频繁，响应越快。",
            self.config.get("control_reengage_ratio", 0.018),
            1,
            18,
            "control_reengage_ratio",
            value_scale=0.01,
            display_scale=1,
            display_suffix="%",
            config_decimals=3,
        )
        fishing_keys.append("control_reengage_ratio")
        self.slider_switch = self._settings_block(
            fishing_layout,
            "反向切换比例",
            "误差超过该比例时允许直接换方向追赶。值越小越灵敏，值越大越少来回切换。",
            self.config.get("control_switch_ratio", 0.08),
            4,
            25,
            "control_switch_ratio",
            value_scale=0.01,
            display_scale=1,
            display_suffix="%",
            config_decimals=3,
        )
        fishing_keys.append("control_switch_ratio")
        self.slider_min_hold = self._settings_block(
            fishing_layout,
            "最短按键保持",
            "每次 A/D 按下后的最短保持时间。值越小越灵敏，值过小可能增加高频抖动。",
            self.config.get("control_min_hold_time", 0.14),
            3,
            35,
            "control_min_hold_time",
            value_scale=0.01,
            display_scale=0.01,
            display_suffix="s",
            display_decimals=2,
            config_decimals=3,
        )
        fishing_keys.append("control_min_hold_time")
        fishing_layout.addStretch()
        self._add_settings_category("溜鱼控制", fishing_page, fishing_keys)

        timing_page, timing_layout = self._build_settings_category_page(
            "流程与超时",
            "控制抛竿、等待、恢复和结算阶段的等待时间。用于适配机器性能、网络延迟和游戏动画速度。",
        )
        timing_keys = []
        self.slider_timeout = self._settings_block(
            timing_layout,
            "防卡死超时",
            "单次钓鱼等待超过该时长时自动重置，避免界面停在异常状态。",
            self.config.get("fishing_timeout", 180),
            60,
            300,
            "fishing_timeout",
        )
        timing_keys.append("fishing_timeout")
        self.slider_hook_wait_timeout = self._settings_block(
            timing_layout,
            "上钩等待超时",
            "抛竿后超过该时长仍未识别到上钩提示时，回到待机重新检测，避免永久卡住。",
            self.config.get("hook_wait_timeout", 90),
            30,
            180,
            "hook_wait_timeout",
        )
        timing_keys.append("hook_wait_timeout")
        self.slider_bar_missing = self._settings_block(
            timing_layout,
            "耐力条丢失容忍",
            "耐力条短暂识别不到时等待的秒数；画面抖动或帧率低可适当调大。",
            self.config.get("bar_missing_timeout", 3),
            1,
            5,
            "bar_missing_timeout",
        )
        timing_keys.append("bar_missing_timeout")
        self.slider_cast_delay = self._settings_block(
            timing_layout,
            "抛竿动画等待",
            "按下抛竿键后等待动画完成的秒数；机器或网络较慢时可适当调大。",
            self.config.get("cast_animation_delay", 2),
            1,
            5,
            "cast_animation_delay",
        )
        timing_keys.append("cast_animation_delay")
        self.slider_close_delay = self._settings_block(
            timing_layout,
            "结算关闭等待",
            "捕获后按 ESC 关闭结算界面，再等待回到可抛竿状态的秒数。",
            self.config.get("settlement_close_delay", 1),
            1,
            5,
            "settlement_close_delay",
        )
        timing_keys.append("settlement_close_delay")
        self.slider_pre_control_timeout = self._settings_block(
            timing_layout,
            "上钩后进入溜鱼超时",
            "识别上钩并按 F 后，超过该秒数仍未进入有效溜鱼控制时自动恢复。",
            self.config.get("pre_control_timeout", 14),
            10,
            30,
            "pre_control_timeout",
        )
        timing_keys.append("pre_control_timeout")
        self.slider_recovery_timeout = self._settings_block(
            timing_layout,
            "异常恢复超时",
            "失败或异常后等待回到可抛竿界面的最长秒数，超过后会尝试轻量恢复。",
            self.config.get("recovery_timeout", 8),
            4,
            20,
            "recovery_timeout",
        )
        timing_keys.append("recovery_timeout")
        timing_layout.addStretch()
        self._add_settings_category("流程与超时", timing_page, timing_keys)

        auto_sell_page, auto_sell_layout = self._build_settings_category_page(
            "自动售鱼",
            "达到设定钓获数量后，程序会在下一次回到可抛竿界面时进入鱼获出售界面并执行一键出售。设为 0 表示关闭。",
        )
        auto_sell_keys = []
        self.slider_auto_sell_threshold = self._settings_block(
            auto_sell_layout,
            "累计钓获自动售鱼",
            "从点击开始钓鱼后开始计数；达到该数量后只会在确认钓鱼初始界面时触发售鱼，避免在结算、溜鱼或异常恢复中误操作。",
            self.config.get("auto_sell_catch_threshold", 0),
            0,
            999,
            "auto_sell_catch_threshold",
            display_suffix="条",
        )
        auto_sell_keys.append("auto_sell_catch_threshold")
        auto_sell_layout.addStretch()
        self._add_settings_category("自动售鱼", auto_sell_page, auto_sell_keys)

        monthly_card_page, monthly_card_layout = self._build_settings_category_page(
            "月卡复位",
            "仅供已开通游戏内月卡的用户手动开启。开启后，程序会按北京时间每天 05:02 执行一次双 ESC 复位。",
        )
        monthly_card_keys = []
        self.monthly_card_reset_button = self._settings_toggle_block(
            monthly_card_layout,
            "每日 05:02 双 ESC 复位",
            "确认自己是游戏内月卡用户后再开启。开启后，每个北京时间日期最多触发一次：先按 ESC，等待 2 秒，再按 ESC，帮助回到钓鱼初始界面。",
            self.config.get(MONTHLY_CARD_RESET_ENABLED_KEY, False),
            MONTHLY_CARD_RESET_ENABLED_KEY,
        )
        monthly_card_keys.append(MONTHLY_CARD_RESET_ENABLED_KEY)
        monthly_card_layout.addStretch()
        self._add_settings_category("月卡复位", monthly_card_page, monthly_card_keys)

        recognition_page, recognition_layout = self._build_settings_category_page(
            "识别与判定",
            "控制耐力条置信度和结算/失败检测频率。数值越激进，响应越快；数值越保守，误判风险越低。",
        )
        recognition_keys = []
        self.slider_bar_confidence = self._settings_block(
            recognition_layout,
            "耐力条最低置信度",
            "低于该置信度时会复用短时间历史位置或忽略结果，降低树林等背景色误参与的风险。",
            self.config.get("bar_confidence_threshold", 0.45),
            25,
            85,
            "bar_confidence_threshold",
            value_scale=0.01,
            display_scale=1,
            display_suffix="%",
            config_decimals=3,
        )
        recognition_keys.append("bar_confidence_threshold")
        self.slider_result_interval = self._settings_block(
            recognition_layout,
            "成功结算检测间隔",
            "溜鱼阶段检测成功结算特征的间隔。越小越快识别结算，但截图匹配频率会更高。",
            self.config.get("fishing_result_check_interval", 0.65),
            7,
            30,
            "fishing_result_check_interval",
            value_scale=0.05,
            display_scale=0.05,
            display_suffix="s",
            display_decimals=2,
            config_decimals=2,
        )
        recognition_keys.append("fishing_result_check_interval")
        self.slider_failed_interval = self._settings_block(
            recognition_layout,
            "失败横幅检测间隔",
            "检测鱼儿溜走提示的间隔。越小越快恢复，但会增加失败模板匹配频率。",
            self.config.get("fishing_failed_check_interval", 1.25),
            14,
            60,
            "fishing_failed_check_interval",
            value_scale=0.05,
            display_scale=0.05,
            display_suffix="s",
            display_decimals=2,
            config_decimals=2,
        )
        recognition_keys.append("fishing_failed_check_interval")
        self.slider_empty_ready_delay = self._settings_block(
            recognition_layout,
            "空杆回到初始确认",
            "看到初始界面后继续确认的时长，用于区分空杆和短暂结算切换。",
            self.config.get("empty_ready_confirm_delay", 0.45),
            5,
            60,
            "empty_ready_confirm_delay",
            value_scale=0.05,
            display_scale=0.05,
            display_suffix="s",
            display_decimals=2,
            config_decimals=2,
        )
        recognition_keys.append("empty_ready_confirm_delay")
        recognition_layout.addStretch()
        self._add_settings_category("识别与判定", recognition_page, recognition_keys)

        safety_page, safety_layout = self._build_settings_category_page(
            "安全接管",
            "当用户点击游戏窗口或在游戏内按键时，程序会释放按键并暂停，避免和用户操作冲突。",
        )
        safety_keys = []
        self.takeover_protection_button = self._settings_toggle_block(
            safety_layout,
            "用户接管自动暂停",
            "开启后，只检测游戏窗口内点击和游戏内键盘操作；移动鼠标不会触发暂停，悬浮窗点击也会被排除。",
            self.config.get("user_takeover_protection", True),
            "user_takeover_protection",
        )
        safety_keys.append("user_takeover_protection")
        self.slider_takeover_start_grace = self._settings_block(
            safety_layout,
            "启动接管宽限",
            "点击开始后短暂忽略用户输入，避免启动瞬间的鼠标点击被误认为接管。",
            self.config.get("user_takeover_start_grace", 1.20),
            0,
            50,
            "user_takeover_start_grace",
            value_scale=0.1,
            display_scale=0.1,
            display_suffix="s",
            display_decimals=1,
            config_decimals=2,
        )
        safety_keys.append("user_takeover_start_grace")
        safety_layout.addStretch()
        self._add_settings_category("安全接管", safety_page, safety_keys)

        display_page, display_layout = self._build_settings_category_page(
            "界面与日志",
            "控制运行日志保留量、启动后页面跳转和调试溜鱼视图。调试视图会增加少量界面刷新开销。",
        )
        display_keys = []
        self.slider_log_limit = self._settings_block(
            display_layout,
            "日志保留条数",
            "控制运行日志页保留的最近输出数量，数值越大可回看更多记录。",
            self.config.get("log_line_limit", 320),
            120,
            800,
            "log_line_limit",
        )
        display_keys.append("log_line_limit")
        self.auto_log_button = self._settings_toggle_block(
            display_layout,
            "启动后跳转运行日志",
            "开启后，点击开始钓鱼会自动切换到运行日志页，方便观察实时状态。",
            self.config.get("auto_switch_to_log", True),
            "auto_switch_to_log",
        )
        display_keys.append("auto_switch_to_log")
        self.debug_view_button = self._settings_toggle_block(
            display_layout,
            "调试溜鱼视图",
            "开启后，运行日志页会显示溜鱼阶段的实时识别画面，便于排查识别异常与反馈问题。",
            self.config.get("debug_mode", False),
            "debug_mode",
        )
        display_keys.append("debug_mode")
        display_layout.addStretch()
        self._add_settings_category("界面与日志", display_page, display_keys)

        category_layout.addStretch()
        body_row.addWidget(category_panel)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.settings_scroll.setStyleSheet(scroll_area_stylesheet())
        self.settings_scroll.viewport().setStyleSheet("background: transparent;")
        self.settings_scroll.viewport().setAutoFillBackground(False)
        self.settings_scroll.setWidget(self.settings_stack)
        body_row.addWidget(self.settings_scroll, 1)
        content_layout.addLayout(body_row, 1)

        action_bar = QFrame()
        self.settings_action_bar = action_bar
        action_bar.setObjectName("settingsActionBar")
        action_bar.setMinimumHeight(74)
        self._apply_settings_action_bar_style(False)
        add_shadow(action_bar, blur=22, alpha=90, offset=(0, 6))
        action_layout = QHBoxLayout(action_bar)
        action_layout.setContentsMargins(18, 12, 18, 12)
        action_layout.setSpacing(12)

        self.settings_status_label = QLabel("当前设置已保存")
        self.settings_status_label.setWordWrap(True)
        self.settings_status_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_soft']}; font-size: 12px;"
        )
        action_layout.addWidget(self.settings_status_label, 1)

        self.reset_settings_btn = QPushButton("恢复当前分类推荐值")
        self.reset_settings_btn.setFocusPolicy(Qt.NoFocus)
        self.reset_settings_btn.setStyleSheet(secondary_button_stylesheet())
        self.reset_settings_btn.clicked.connect(self._reset_current_settings_category)
        action_layout.addWidget(self.reset_settings_btn)

        self.save_settings_btn = QPushButton("保存并应用")
        self.save_settings_btn.setFocusPolicy(Qt.NoFocus)
        self.save_settings_btn.setStyleSheet(primary_button_stylesheet())
        self.save_settings_btn.clicked.connect(self._save_settings)
        action_layout.addWidget(self.save_settings_btn)

        content_layout.addWidget(action_bar)

        layout.addWidget(content, 1)
        if self._settings_category_buttons:
            self._switch_settings_category(0)
        self._settings_building = False
        self._refresh_settings_saved_snapshot()
        self._set_settings_dirty(False)
        return page

    def _build_settings_category_page(self, title, description):
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 20px; font-weight: 900;"
        )
        layout.addWidget(title_label)

        note_label = QLabel(description)
        note_label.setWordWrap(True)
        note_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px;"
        )
        layout.addWidget(note_label)
        return page, layout

    def _add_settings_category(self, title, page, keys):
        index = self.settings_stack.addWidget(page)
        self._settings_category_keys[index] = list(keys)
        button = SettingsCategoryButton(title)
        button.clicked.connect(lambda _checked=False, category_index=index: self._switch_settings_category(category_index))
        self.settings_category_layout.addWidget(button)
        self._settings_category_buttons.append(button)
        return button

    def _switch_settings_category(self, index):
        if not hasattr(self, "settings_stack"):
            return
        self.settings_stack.setCurrentIndex(index)
        for button_index, button in enumerate(self._settings_category_buttons):
            button.setChecked(button_index == index)
        if hasattr(self, "settings_scroll"):
            QTimer.singleShot(0, lambda: self.settings_scroll.verticalScrollBar().setValue(0))

    def _apply_settings_action_bar_style(self, dirty):
        if not hasattr(self, "settings_action_bar"):
            return
        if dirty:
            bg = "rgba(43, 33, 15, 0.96)"
            border = "rgba(241, 190, 103, 0.74)"
        else:
            bg = "rgba(13, 31, 45, 0.96)"
            border = "rgba(99, 228, 228, 0.34)"
        self.settings_action_bar.setStyleSheet(
            f"""
            QFrame#settingsActionBar {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 20px;
            }}
            """
        )

    def _settings_panel(self):
        block = QFrame()
        block.setProperty("variant", "soft")
        block.setStyleSheet(panel_stylesheet())
        add_shadow(block, blur=20, alpha=85, offset=(0, 8))
        return block

    def _settings_readonly_block(self, parent_layout, title, note, value_text):
        block = self._settings_panel()
        layout = QVBoxLayout(block)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        top = QHBoxLayout()
        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 16px; font-weight: 800;"
        )
        top.addWidget(title_label)
        top.addStretch()
        value_label = QLabel(value_text)
        value_label.setAlignment(Qt.AlignCenter)
        value_label.setMinimumWidth(74)
        value_label.setStyleSheet(
            f"""
            QLabel {{
                color: {APP_COLORS['accent_soft']};
                background-color: rgba(29, 208, 214, 0.10);
                border: 1px solid rgba(29, 208, 214, 0.22);
                border-radius: 14px;
                padding: 6px 10px;
                font-size: 18px;
                font-weight: 900;
            }}
            """
        )
        top.addWidget(value_label)
        layout.addLayout(top)

        note_label = QLabel(note)
        note_label.setWordWrap(True)
        note_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px;"
        )
        layout.addWidget(note_label)
        parent_layout.addWidget(block)
        return block

    def _settings_action_buttons_block(self, parent_layout, title, note, actions):
        block = self._settings_panel()
        layout = QVBoxLayout(block)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 16px; font-weight: 800;"
        )
        layout.addWidget(title_label)

        note_label = QLabel(note)
        note_label.setWordWrap(True)
        note_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px;"
        )
        layout.addWidget(note_label)

        row = QHBoxLayout()
        row.setSpacing(10)
        for index, (text, callback) in enumerate(actions or ()):
            button = QPushButton(str(text))
            button.setFocusPolicy(Qt.NoFocus)
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(primary_button_stylesheet() if index == 0 else secondary_button_stylesheet())
            button.clicked.connect(callback)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)

        parent_layout.addWidget(block)
        return block

    def _settings_block(
        self,
        parent_layout,
        title,
        note,
        value,
        minimum,
        maximum,
        key,
        value_scale=1.0,
        display_scale=None,
        display_suffix="",
        display_decimals=0,
        config_decimals=None,
    ):
        block = self._settings_panel()

        layout = QVBoxLayout(block)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        top = QHBoxLayout()
        title_label = QLabel(title)
        title_label.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 16px; font-weight: 800;")
        top.addWidget(title_label)
        top.addStretch()

        display_scale = value_scale if display_scale is None else display_scale
        try:
            slider_value = int(round(float(value) / float(value_scale)))
        except (TypeError, ValueError, ZeroDivisionError):
            slider_value = int(minimum)
        slider_value = max(int(minimum), min(int(maximum), slider_value))

        value_label = QLabel(self._format_slider_label(slider_value, display_scale, display_suffix, display_decimals))
        value_label.setStyleSheet(
            f"""
            QLabel {{
                color: {APP_COLORS['accent_soft']};
                background-color: rgba(29, 208, 214, 0.10);
                border: 1px solid rgba(29, 208, 214, 0.22);
                border-radius: 14px;
                padding: 6px 10px;
                font-size: 18px;
                font-weight: 900;
            }}
            """
        )
        value_label.setMinimumWidth(74)
        value_label.setAlignment(Qt.AlignCenter)
        top.addWidget(value_label)
        layout.addLayout(top)

        note_label = QLabel(note)
        note_label.setWordWrap(True)
        note_label.setStyleSheet(f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px;")
        layout.addWidget(note_label)

        slider = NoWheelSlider(Qt.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(slider_value)
        slider.setFocusPolicy(Qt.NoFocus)
        slider.setStyleSheet(
            f"""
            QSlider::groove:horizontal {{
                background-color: rgba(255, 255, 255, 0.08);
                border-radius: 5px;
                height: 10px;
            }}
            QSlider::sub-page:horizontal {{
                background-color: rgba(29, 208, 214, 0.82);
                border-radius: 5px;
                height: 10px;
            }}
            QSlider::handle:horizontal {{
                background-color: #B8FFFF;
                border: 2px solid rgba(29, 208, 214, 0.92);
                width: 16px;
                margin: -4px 0;
                border-radius: 8px;
            }}
            """
        )
        slider.valueChanged.connect(
            lambda new_value,
            label=value_label,
            config_key=key,
            cfg_scale=value_scale,
            disp_scale=display_scale,
            suffix=display_suffix,
            disp_decimals=display_decimals,
            cfg_decimals=config_decimals: self._update_slider_value(
                label,
                config_key,
                new_value,
                cfg_scale,
                disp_scale,
                suffix,
                disp_decimals,
                cfg_decimals,
            )
        )
        layout.addWidget(slider)
        parent_layout.addWidget(block)
        self._setting_widgets[key] = {
            "type": "slider",
            "widget": slider,
            "value_scale": value_scale,
            "display_scale": display_scale,
            "display_suffix": display_suffix,
            "display_decimals": display_decimals,
            "config_decimals": config_decimals,
        }
        self.config[key] = self._config_from_slider_value(slider_value, value_scale, config_decimals)
        return slider

    def _settings_toggle_block(self, parent_layout, title, note, checked, key):
        block = self._settings_panel()
        layout = QHBoxLayout(block)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(14)

        text_col = QVBoxLayout()
        text_col.setSpacing(6)

        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 16px; font-weight: 800;"
        )
        text_col.addWidget(title_label)

        note_label = QLabel(note)
        note_label.setWordWrap(True)
        note_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px;"
        )
        text_col.addWidget(note_label)
        layout.addLayout(text_col, 1)

        button = QPushButton()
        button.setCheckable(True)
        button.setFocusPolicy(Qt.NoFocus)
        button.setChecked(bool(checked))
        button.setStyleSheet(secondary_button_stylesheet())
        button.setMinimumWidth(86)
        button.toggled.connect(
            lambda is_checked, cfg_key=key, btn=button: self._update_toggle_value(btn, cfg_key, is_checked)
        )
        self._update_toggle_value(button, key, bool(checked))
        layout.addWidget(button)

        parent_layout.addWidget(block)
        self._setting_widgets[key] = {"type": "toggle", "widget": button}
        return button

    def _settings_text_block(self, parent_layout, title, note, value, key, placeholder=""):
        block = self._settings_panel()
        layout = QVBoxLayout(block)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text']}; font-size: 16px; font-weight: 800;"
        )
        layout.addWidget(title_label)

        note_label = QLabel(note)
        note_label.setWordWrap(True)
        note_label.setStyleSheet(
            f"background: transparent; border: none; color: {APP_COLORS['text_dim']}; font-size: 12px;"
        )
        layout.addWidget(note_label)

        line_edit = QLineEdit(str(value or ""))
        line_edit.setPlaceholderText(placeholder)
        line_edit.setMinimumHeight(42)
        line_edit.setStyleSheet(
            f"""
            QLineEdit {{
                background-color: rgba(255, 255, 255, 0.05);
                border: 1px solid rgba(111, 145, 182, 0.22);
                border-radius: 14px;
                color: {APP_COLORS['text']};
                padding: 0 14px;
                font-size: 13px;
            }}
            QLineEdit:focus {{
                border: 1px solid rgba(29, 208, 214, 0.62);
                background-color: rgba(255, 255, 255, 0.07);
            }}
            """
        )
        line_edit.textChanged.connect(lambda text, cfg_key=key: self._update_text_value(cfg_key, text))
        layout.addWidget(line_edit)

        parent_layout.addWidget(block)
        self._setting_widgets[key] = {"type": "text", "widget": line_edit}
        return line_edit

    def _format_slider_label(self, value, display_scale=1.0, display_suffix="", display_decimals=0):
        display_value = float(value) * float(display_scale)
        if int(display_decimals) <= 0:
            text = str(int(round(display_value)))
        else:
            text = f"{display_value:.{int(display_decimals)}f}".rstrip("0").rstrip(".")
        return f"{text}{display_suffix}"

    def _config_from_slider_value(self, value, value_scale=1.0, config_decimals=None):
        scaled_value = float(value) * float(value_scale)
        if config_decimals is not None:
            return round(scaled_value, int(config_decimals))
        if abs(float(value_scale) - 1.0) < 0.000001:
            return int(round(scaled_value))
        return scaled_value

    def _update_slider_value(
        self,
        label,
        key,
        value,
        value_scale=1.0,
        display_scale=1.0,
        display_suffix="",
        display_decimals=0,
        config_decimals=None,
    ):
        label.setText(self._format_slider_label(value, display_scale, display_suffix, display_decimals))
        self.config[key] = self._config_from_slider_value(value, value_scale, config_decimals)
        self._mark_settings_dirty()

    def _update_toggle_value(self, button, key, checked):
        self.config[key] = bool(checked)
        button.setText("已开启" if checked else "已关闭")
        self._mark_settings_dirty()

    def _update_text_value(self, key, text):
        self.config[key] = str(text or "").strip()
        self._mark_settings_dirty()

    def _mark_settings_dirty(self):
        if getattr(self, "_settings_building", False):
            return
        self._set_settings_dirty(self._has_unsaved_settings_changes())

    def _set_settings_dirty(self, dirty):
        self._settings_dirty = bool(dirty)
        self._apply_settings_action_bar_style(self._settings_dirty)
        if hasattr(self, "save_settings_btn"):
            self.save_settings_btn.setEnabled(self._settings_dirty)
        if hasattr(self, "settings_status_label"):
            if self._settings_dirty:
                self.settings_status_label.setText("有未保存更改。保存后会立即同步到运行参数，并写入配置文件。")
                self.settings_status_label.setStyleSheet(
                    f"background: transparent; border: none; color: {APP_COLORS['warning']}; font-size: 12px; font-weight: 700;"
                )
            else:
                self.settings_status_label.setText("当前设置已保存")
                self.settings_status_label.setStyleSheet(
                    f"background: transparent; border: none; color: {APP_COLORS['text_soft']}; font-size: 12px;"
                )

    def _settings_snapshot_keys(self):
        if getattr(self, "_setting_widgets", None):
            return tuple(self._setting_widgets.keys())
        return tuple(self.default_config.keys())

    def _settings_config_snapshot(self):
        return {key: self.config.get(key) for key in self._settings_snapshot_keys()}

    def _refresh_settings_saved_snapshot(self):
        self._settings_saved_snapshot = self._settings_config_snapshot()

    def _settings_values_equal(self, left, right):
        if isinstance(left, bool) or isinstance(right, bool):
            return bool(left) == bool(right)
        if isinstance(left, (int, float)) or isinstance(right, (int, float)):
            try:
                return abs(float(left) - float(right)) <= 0.000001
            except (TypeError, ValueError):
                return False
        return left == right

    def _has_unsaved_settings_changes(self):
        snapshot = getattr(self, "_settings_saved_snapshot", {}) or {}
        for key, current_value in self._settings_config_snapshot().items():
            if key not in snapshot:
                return True
            if not self._settings_values_equal(current_value, snapshot.get(key)):
                return True
        return False

    def _reset_current_settings_category(self):
        if not hasattr(self, "settings_stack"):
            return
        index = self.settings_stack.currentIndex()
        keys = self._settings_category_keys.get(index, [])
        changed = False
        for key in keys:
            if key not in self.default_config:
                continue
            widget_info = self._setting_widgets.get(key)
            if not widget_info:
                continue
            default_value = self.default_config[key]
            if widget_info["type"] == "slider":
                slider = widget_info["widget"]
                value_scale = widget_info.get("value_scale", 1.0)
                try:
                    slider_value = int(round(float(default_value) / float(value_scale)))
                except (TypeError, ValueError, ZeroDivisionError):
                    continue
                slider_value = max(slider.minimum(), min(slider.maximum(), slider_value))
                if slider.value() != slider_value:
                    changed = True
                    slider.setValue(slider_value)
                else:
                    self.config[key] = self._config_from_slider_value(
                        slider_value,
                        value_scale,
                        widget_info.get("config_decimals"),
                    )
            elif widget_info["type"] == "toggle":
                button = widget_info["widget"]
                target = bool(default_value)
                if button.isChecked() != target:
                    changed = True
                    button.setChecked(target)
                else:
                    self.config[key] = target
            elif widget_info["type"] == "inverse_toggle":
                button = widget_info["widget"]
                target = not bool(default_value)
                if button.isChecked() != target:
                    changed = True
                    button.setChecked(target)
                else:
                    self.config[key] = not target
            elif widget_info["type"] == "text":
                line_edit = widget_info["widget"]
                target = str(default_value or "")
                if line_edit.text() != target:
                    changed = True
                    line_edit.setText(target)
                else:
                    self.config[key] = target
        if changed:
            self._mark_settings_dirty()
            self.show_toast("已恢复当前分类推荐值，请保存应用", "info")
        else:
            self.show_toast("当前分类已是推荐值", "info")

    def _save_settings(self):
        self._apply_state_machine_config()
        if self.save_config():
            self._refresh_settings_saved_snapshot()
            self._set_settings_dirty(False)
            self.show_toast("高级设置已保存并应用", "success")
            self._check_monthly_card_daily_reset()
        else:
            self.show_toast("设置保存失败，请查看运行日志", "danger")

    def switch_page(self, index, button):
        for nav in [self.nav_record, self.nav_encyclopedia, self.nav_log, self.nav_settings]:
            nav.setChecked(False)
        button.setChecked(True)
        if index == 1:
            self._ensure_encyclopedia_page()
        self.stack.setCurrentIndex(index)
        if index == 0:
            self.page_record.refresh_data()
        elif index == 1:
            self.page_encyclopedia.refresh_data()

    def write_log(self, msg):
        if msg == "CMD_STOP_UPDATE_GUI":
            self.update_ui_on_stop()
            return
        if msg == "CMD_MAIN_HIDE_FOR_CAPTURE":
            self._set_main_capture_hidden(True)
            return
        if msg == "CMD_MAIN_RESTORE_AFTER_CAPTURE":
            self._set_main_capture_hidden(False)
            return
        if msg == "CMD_FLOATING_HIDE_FOR_CAPTURE":
            self._set_floating_capture_hidden(True)
            return
        if msg == "CMD_FLOATING_RESTORE_AFTER_CAPTURE":
            self._set_floating_capture_hidden(False)
            return
        if isinstance(msg, str) and msg.startswith("CMD_USER_TAKEOVER_PAUSED"):
            reason = ""
            if "::" in msg:
                reason = msg.split("::", 1)[1]
            self.handle_user_takeover_pause(reason)
            return

        import time

        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self.log_deque.append(line)
        self._log_version += 1
        log_text = "\n".join(self.log_deque)
        if hasattr(self, "log_textbox"):
            self.log_textbox.setText(log_text)
            self.log_textbox.verticalScrollBar().setValue(self.log_textbox.verticalScrollBar().maximum())
        if self.floating_window is not None:
            self.floating_window.refresh_log_view()

    def _set_floating_capture_hidden(self, hidden):
        floating = getattr(self, "floating_window", None)
        if floating is None:
            return
        floating.set_capture_hidden(hidden)

    def _set_main_capture_hidden(self, hidden):
        hidden = bool(hidden)
        if hidden:
            if getattr(self, "_main_hidden_for_capture", False):
                return
            self._main_hidden_for_capture = True
            self._main_capture_was_visible = bool(self.isVisible())
            self._main_capture_geometry = self.geometry()
            self._main_capture_window_state = self.windowState()
            if self._main_capture_was_visible:
                if self.isMaximized() or self.isFullScreen():
                    self.showNormal()
                self.move(-32000, -32000)
            return

        if not getattr(self, "_main_hidden_for_capture", False):
            return
        was_visible = bool(getattr(self, "_main_capture_was_visible", False))
        geometry = getattr(self, "_main_capture_geometry", None)
        window_state = getattr(self, "_main_capture_window_state", None)
        self._main_hidden_for_capture = False
        self._main_capture_was_visible = False
        self._main_capture_geometry = None
        self._main_capture_window_state = None
        if was_visible:
            if geometry is not None:
                self.setGeometry(geometry)
            if window_state is not None:
                self.setWindowState(window_state)
            if not self.isVisible():
                self.show()
            self.raise_()

    def process_queue(self):
        self._sync_user_takeover_exclude_rects()
        try:
            while True:
                self.write_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass

        latest_debug_frame = None
        try:
            while True:
                latest_debug_frame = self.debug_queue.get_nowait()
        except queue.Empty:
            pass
        if latest_debug_frame is not None:
            self._set_debug_frame(latest_debug_frame)

    def _sync_user_takeover_exclude_rects(self):
        rects = []
        floating = getattr(self, "floating_window", None)
        if floating is not None and floating.isVisible():
            geom = floating.frameGeometry()
            padding = 10
            rects.append(
                (
                    geom.x() - padding,
                    geom.y() - padding,
                    geom.width() + padding * 2,
                    geom.height() + padding * 2,
                )
            )
        self.sm.update_config("user_takeover_exclude_rects", rects)

    def handle_user_takeover_pause(self, reason=""):
        self.update_ui_on_stop()
        self.status_chip.set_status("已暂停", "stopped")
        self.write_log(">>> 检测到用户接管游戏，自动钓鱼已暂停。")
        self.show_toast("检测到用户接管，自动钓鱼已暂停", "warning")
        if getattr(self, "_takeover_pause_dialog_visible", False):
            return

        detail = reason or "检测到游戏窗口内输入"
        dialog = TakeoverPauseDialog(detail)
        self._takeover_pause_dialog_visible = True
        self.takeover_pause_dialog = dialog
        dialog.finished.connect(lambda _result: setattr(self, "_takeover_pause_dialog_visible", False))
        rect = self.sm.wm.get_client_rect()
        if rect:
            left, top, width, height = rect
            dialog.move(int(left + width / 2 - dialog.width() / 2), int(top + height / 2 - dialog.height() / 2))
        else:
            screen_center = self.screen().availableGeometry().center()
            dialog.move(screen_center - dialog.rect().center())
        dialog.open()
        dialog.raise_()
        dialog.activateWindow()

    def _confirm_game_resolution_before_start(self):
        return True

    def start_bot(self):
        if self.sm.is_running:
            return
        if not self.modules_ready:
            self.start_module_initialization()
            return

        if not self._confirm_game_resolution_before_start():
            return

        self._apply_state_machine_config()
        self._takeover_pause_dialog_visible = False

        self.status_chip.set_status("运行中", "running")
        if self.config.get("auto_switch_to_log", True):
            self.switch_page(2, self.nav_log)
        self.write_log(">>> 启动自动钓鱼。")
        self.sm.start()
        self.update_primary_buttons()
        self.show_toast("自动钓鱼已启动", "success")

    def stop_bot(self):
        if not self.sm.is_running:
            self.show_toast("当前未在运行", "warning")
            return
        self.sm.stop()
        self.write_log(">>> 已发送停止指令。")
        self.update_ui_on_stop()
        self.show_toast("停止指令已发送", "warning")

    def update_ui_on_stop(self):
        self.status_chip.set_status("已停止", "stopped")
        self.update_primary_buttons()
        if hasattr(self, "debug_preview") and not self.config.get("debug_mode", False):
            self._refresh_debug_view_state()
        self.page_record.refresh_data()
        if self.page_encyclopedia is not None:
            self.page_encyclopedia.refresh_data()

