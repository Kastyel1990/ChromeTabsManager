import sys
import json
import asyncio
import threading
import base64
import queue
import platform
import time
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QPushButton,
                             QScrollArea, QHBoxLayout, QMenu, QFrame, QLabel,
                             QSizePolicy, QSystemTrayIcon)
from PyQt6.QtCore import Qt, QPropertyAnimation, QRect, pyqtSignal, QObject, QTimer, QUrl, QSize, QPoint
from PyQt6.QtGui import QPixmap, QPainter, QPen, QBrush, QPolygon, QColor, QIcon
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PyQt6.QtSvg import QSvgRenderer
from PyQt6 import sip
import websockets, ctypes, ctypes.wintypes
import os

# ─── Цвета групп Chrome ───────────────────────────────────────────────────────
CHROME_COLORS = {
    "grey": "#5F6368", "blue": "#1A73E8", "red": "#D93025",
    "yellow": "#FABB06", "green": "#1E8E3E", "pink": "#D01884",
    "purple": "#9333E6", "cyan": "#12B5CB", "orange": "#E8710A"
}

network_manager = None
icon_cache = {}

# Thread-safe очередь команд Qt → asyncio
command_queue = queue.Queue()


class CommSignal(QObject):
    data_received = pyqtSignal(dict)
    send_command = pyqtSignal(str)


signals = CommSignal()


# ─── Кастомная кнопка закрытия ────────────────────────────────────────────────
class CloseButton(QPushButton):
    """Кнопка с нарисованным X и круговым hover-эффектом в стиле Chrome."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(18, 18)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hovered = False
        self._pressed = False
        self.setFlat(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        # Убираем любой стандартный фон и рамку
        self.setStyleSheet("background: transparent; border: none; padding: 0;")

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._pressed = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._pressed = False
        self.update()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()

        if self._pressed:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor("#a50e0e")))
            painter.drawEllipse(1, 1, w - 2, h - 2)
            x_color = QColor("#ffffff")
        elif self._hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor("#c5221f")))
            painter.drawEllipse(1, 1, w - 2, h - 2)
            x_color = QColor("#ffffff")
        else:
            x_color = QColor("#9aa0a6")

        # Рисуем X двумя линиями
        pen = QPen(x_color)
        pen.setWidth(2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        m = 5
        painter.drawLine(m, m, w - m, h - m)
        painter.drawLine(w - m, m, m, h - m)
        painter.end()


# ─── Виджет одной вкладки ────────────────────────────────────────────────────
class TabWidget(QWidget):
    def __init__(self, tab_data, sidebar_app):
        super().__init__()
        self.sidebar_app = sidebar_app
        self.tab_id = None
        self.fav_icon_url = None
        self.is_active = None
        self.is_selected = False          # ← Новое: состояние выделения

        self.main_layout = QHBoxLayout(self)
        self.main_layout.setContentsMargins(0, 1, 4, 1)
        self.main_layout.setSpacing(0)

        self.base_frame = QFrame()
        self.base_frame.setObjectName("tabBaseFrame")
        self.base_frame.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.base_frame.customContextMenuRequested.connect(self.show_context_menu)
        self.base_frame.mousePressEvent = self.on_frame_click

        self.frame_layout = QHBoxLayout(self.base_frame)
        self.frame_layout.setContentsMargins(8, 4, 6, 4)
        self.frame_layout.setSpacing(10)

        # Иконка
        self.icon_label = QLabel()
        self.icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.icon_label.setObjectName("tabIcon")
        self.icon_label.setFixedSize(16, 16)
        self.icon_label.setScaledContents(False)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setStyleSheet("background: transparent; border: none; padding: 0;")
        self.set_initial_icon()

        # Заголовок
        self.title_label = QLabel(tab_data['title'][:40] or "Новая вкладка")
        self.title_label.setStyleSheet("color: #e8eaed; font-size: 11px; background: transparent;")
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        # Кнопка закрытия (кастомная)
        self.close_btn = CloseButton()
        self.close_btn.clicked.connect(self.on_close_click)

        self.frame_layout.addWidget(self.icon_label, 0)
        self.frame_layout.addWidget(self.title_label, 1)
        self.frame_layout.addWidget(self.close_btn, 0)
        self.main_layout.addWidget(self.base_frame)

        self.update_data(tab_data)

    # ── Стиль ────────────────────────────────────────────────────────────────
    def _update_style(self):
        """Обновляет стиль фрейма на основе is_active / is_selected."""
        if self.is_selected:
            bg_color   = "#1a3a5c"
            border     = "3px solid #8ab4f8"
            hover_bg   = "#1e4778"
        elif self.is_active:
            bg_color   = "#3c4043"
            border     = "3px solid #8ab4f8"
            hover_bg   = "#45474a"
        else:
            bg_color   = "#292a2d"
            border     = "3px solid transparent"
            hover_bg   = "#45474a"

        self.base_frame.setStyleSheet(f"""
            QFrame#tabBaseFrame {{
                background-color: {bg_color};
                border-radius: 4px;
                border-left: {border};
            }}
            QFrame#tabBaseFrame:hover {{ background-color: {hover_bg}; }}
        """)

    def set_selected(self, selected: bool):
        """Публичный метод — устанавливает/снимает выделение."""
        if self.is_selected != selected:
            self.is_selected = selected
            self._update_style()

    # ── Обновление данных ────────────────────────────────────────────────────
    def update_data(self, tab_data):
        """Обновляет содержимое виджета без его пересоздания."""
        new_active = tab_data['active']
        new_title  = tab_data['title'][:40] or "Новая вкладка"
        new_icon   = tab_data.get('favIcon', '')

        if self.tab_id != tab_data['id']:
            self.tab_id = tab_data['id']

        if self.is_active != new_active:
            self.is_active = new_active
            self._update_style()

        if self.title_label.text() != new_title:
            self.title_label.setText(new_title)

        if self.fav_icon_url != new_icon:
            self.fav_icon_url = new_icon
            self.set_initial_icon()

    # ── Клик по вкладке ──────────────────────────────────────────────────────
    def on_frame_click(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            modifiers = QApplication.keyboardModifiers()

            if modifiers & Qt.KeyboardModifier.ControlModifier:
                # Ctrl+Click — переключить выделение без активации вкладки
                if self.sidebar_app:
                    self.sidebar_app.toggle_tab_selection(self.tab_id)

            elif modifiers & Qt.KeyboardModifier.ShiftModifier:
                # Shift+Click — выделить диапазон от последнего кликнутого
                if self.sidebar_app:
                    self.sidebar_app.range_select_tabs(self.tab_id)

            else:
                # Обычный клик: сбросить выделение и активировать вкладку
                if self.sidebar_app:
                    self.sidebar_app.clear_selection()
                    self.sidebar_app.last_clicked_tab_id = self.tab_id
                cmd = json.dumps({"action": "activate", "id": self.tab_id})
                print(f"Sending activate for tab {self.tab_id}")
                command_queue.put(cmd)
                if self.sidebar_app:
                    self.sidebar_app.force_update = True
                    self.sidebar_app.scroll_to_active_tab = True
                    QTimer.singleShot(30, lambda: command_queue.put(
                        json.dumps({"action": "request_update"})))

    # ── Закрытие вкладки ─────────────────────────────────────────────────────
    def on_close_click(self):
        tid = self.tab_id
        cmd = json.dumps({"action": "close", "id": tid})
        print(f"Sending close for tab {tid}")
        command_queue.put(cmd)
        if self.sidebar_app:
            # Сохраняем время отправки — если вкладка не исчезнет за 600 мс, повторим
            self.sidebar_app.pending_closes[tid] = time.time()
            self.sidebar_app.force_update = True
            QTimer.singleShot(30, lambda: command_queue.put(
                json.dumps({"action": "request_update"})))

    # ── Контекстное меню ─────────────────────────────────────────────────────
    def show_context_menu(self, position):
        menu = QMenu(self)

        def on_menu_closed():
            if self.sidebar_app and not self.sidebar_app.underMouse():
                QTimer.singleShot(100, lambda: self.sidebar_app._check_hide())

        menu.aboutToHide.connect(on_menu_closed)

        menu_style = """
            QMenu {
                background-color: #35363a;
                color: #e8eaed;
                border: 1px solid #45474a;
                border-radius: 4px;
                padding: 4px;
            }
            QMenu::item { padding: 6px 24px 6px 24px; border-radius: 2px; }
            QMenu::item:selected { background-color: #8ab4f8; color: #202124; }
            QMenu::separator { height: 1px; background: #45474a; margin: 4px 8px; }
        """
        menu.setStyleSheet(menu_style)

        selected  = self.sidebar_app.selected_tab_ids if self.sidebar_app else set()
        is_multi  = len(selected) > 1 and self.tab_id in selected

        groups_actions = {}

        if is_multi:
            # ── Меню для нескольких выделенных вкладок ──────────────────────
            n = len(selected)
            close_sel = menu.addAction(f"✕  Закрыть выбранные  ({n})")
            menu.addSeparator()

            add_to_group_menu = menu.addMenu(f"Добавить {n} вкл. в группу")
            add_to_group_menu.setStyleSheet(menu_style)

            if hasattr(self, 'available_groups') and self.available_groups:
                for group in self.available_groups:
                    group_title = group.get('title') or f"Группа {group['id']}"
                    act = add_to_group_menu.addAction(f"📁 {group_title}")
                    groups_actions[act] = group['id']

            new_group_action  = add_to_group_menu.addAction("➕ Создать новую группу")
            remove_from_group = menu.addAction("Убрать из группы")

            chosen = menu.exec(self.base_frame.mapToGlobal(position))

            if chosen == close_sel:
                ids = list(self.sidebar_app.selected_tab_ids)
                command_queue.put(json.dumps({"action": "close_multiple", "ids": ids}))
                if self.sidebar_app:
                    self.sidebar_app.clear_selection()
                    self.sidebar_app.force_update = True
                    QTimer.singleShot(80, lambda: command_queue.put(
                        json.dumps({"action": "request_update"})))

            elif chosen == new_group_action:
                ids = list(self.sidebar_app.selected_tab_ids)
                command_queue.put(json.dumps({"action": "add_multiple_to_new_group", "ids": ids}))
                if self.sidebar_app:
                    self.sidebar_app.clear_selection()
                    self.sidebar_app.force_update = True
                    QTimer.singleShot(100, lambda: command_queue.put(
                        json.dumps({"action": "request_update"})))

            elif chosen in groups_actions:
                group_id = groups_actions[chosen]
                ids = list(self.sidebar_app.selected_tab_ids)
                command_queue.put(json.dumps({
                    "action": "add_multiple_to_group", "ids": ids, "groupId": group_id
                }))
                if self.sidebar_app:
                    self.sidebar_app.clear_selection()
                    self.sidebar_app.force_update = True
                    self.sidebar_app.scroll_to_group_id = group_id
                    QTimer.singleShot(80, lambda: command_queue.put(
                        json.dumps({"action": "request_update"})))

            elif chosen == remove_from_group:
                ids = list(self.sidebar_app.selected_tab_ids)
                command_queue.put(json.dumps({"action": "remove_multiple_from_group", "ids": ids}))
                if self.sidebar_app:
                    self.sidebar_app.clear_selection()
                    self.sidebar_app.force_update = True
                    QTimer.singleShot(50, lambda: command_queue.put(
                        json.dumps({"action": "request_update"})))

        else:
            # ── Меню для одной вкладки (оригинал) ───────────────────────────
            dup = menu.addAction("Дублировать")
            pin = menu.addAction("Закрепить / Открепить")
            menu.addSeparator()

            add_to_group_menu = menu.addMenu("Добавить в группу")
            add_to_group_menu.setStyleSheet(menu_style)

            if hasattr(self, 'available_groups') and self.available_groups:
                for group in self.available_groups:
                    group_title = group.get('title') or f"Группа {group['id']}"
                    act = add_to_group_menu.addAction(f"📁 {group_title}")
                    groups_actions[act] = group['id']

            new_group_action  = add_to_group_menu.addAction("➕ Создать новую группу")
            remove_from_group = menu.addAction("Убрать из группы")
            menu.addSeparator()
            others = menu.addAction("Закрыть другие")

            chosen = menu.exec(self.base_frame.mapToGlobal(position))

            if chosen == dup:
                command_queue.put(json.dumps({"action": "duplicate", "id": self.tab_id}))
                if self.sidebar_app:
                    self.sidebar_app.force_update = True
                    QTimer.singleShot(80, lambda: command_queue.put(
                        json.dumps({"action": "request_update"})))
            elif chosen == pin:
                command_queue.put(json.dumps({"action": "toggle_pin", "id": self.tab_id}))
                if self.sidebar_app:
                    self.sidebar_app.force_update = True
                    QTimer.singleShot(30, lambda: command_queue.put(
                        json.dumps({"action": "request_update"})))
            elif chosen == others:
                command_queue.put(json.dumps({"action": "close_others", "id": self.tab_id}))
                if self.sidebar_app:
                    self.sidebar_app.force_update = True
                    QTimer.singleShot(50, lambda: command_queue.put(
                        json.dumps({"action": "request_update"})))
            elif chosen == remove_from_group:
                command_queue.put(json.dumps({"action": "remove_from_group", "id": self.tab_id}))
                if self.sidebar_app:
                    self.sidebar_app.force_update = True
                    QTimer.singleShot(30, lambda: command_queue.put(
                        json.dumps({"action": "request_update"})))
            elif chosen == new_group_action:
                command_queue.put(json.dumps({"action": "add_to_new_group", "id": self.tab_id}))
                if self.sidebar_app:
                    self.sidebar_app.force_update = True
                    QTimer.singleShot(100, lambda: command_queue.put(
                        json.dumps({"action": "request_update"})))
            elif chosen in groups_actions:
                group_id = groups_actions[chosen]
                command_queue.put(json.dumps({
                    "action": "add_to_group", "id": self.tab_id, "groupId": group_id
                }))
                if self.sidebar_app:
                    self.sidebar_app.force_update = True
                    self.sidebar_app.scroll_to_group_id = group_id
                    QTimer.singleShot(50, lambda: command_queue.put(
                        json.dumps({"action": "request_update"})))

    # ── Иконки ───────────────────────────────────────────────────────────────
    def set_initial_icon(self):
        if not self.fav_icon_url:
            self.set_default_icon()
            return
        if self.fav_icon_url in icon_cache:
            self.icon_label.setPixmap(icon_cache[self.fav_icon_url])
            return
        if self.fav_icon_url.startswith('data:image'):
            try:
                _header, encoded = self.fav_icon_url.split(",", 1)
                data = base64.b64decode(encoded)
                self.process_image_data(data, self.fav_icon_url)
            except:
                self.set_default_icon()
        elif self.fav_icon_url.startswith('http'):
            request = QNetworkRequest(QUrl(self.fav_icon_url))
            request.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, "Mozilla/5.0")
            request.setAttribute(QNetworkRequest.Attribute.Http2AllowedAttribute, False)
            reply = network_manager.get(request)
            reply.finished.connect(lambda: self.on_icon_loaded(reply, self.fav_icon_url))
        else:
            self.set_default_icon()

    def set_default_icon(self):
        if sip.isdeleted(self) or sip.isdeleted(self.icon_label):
            return
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#9aa0a6"))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor("#5f6368")))
        painter.drawRect(3, 2, 10, 12)
        points = QPolygon([QPoint(13, 2), QPoint(13, 5), QPoint(10, 5)])
        painter.setBrush(QBrush(QColor("#9aa0a6")))
        painter.drawPolygon(points)
        painter.end()
        self.icon_label.setPixmap(pixmap)

    def on_icon_loaded(self, reply, url):
        if sip.isdeleted(self) or sip.isdeleted(self.icon_label):
            return
        if reply.error() == QNetworkReply.NetworkError.NoError:
            self.process_image_data(reply.readAll(), url)
        else:
            self.set_default_icon()
        reply.deleteLater()

    def process_image_data(self, data, url):
        if sip.isdeleted(self) or sip.isdeleted(self.icon_label):
            return
        pixmap = QPixmap()
        if b"<svg" in bytes(data[:200]).lower():
            try:
                renderer = QSvgRenderer(data)
                if renderer.isValid():
                    pixmap = QPixmap(16, 16)
                    pixmap.fill(Qt.GlobalColor.transparent)
                    p = QPainter(pixmap)
                    renderer.render(p)
                    p.end()
            except:
                self.set_default_icon()
                return
        else:
            pixmap.loadFromData(data)
        if not pixmap.isNull():
            scaled = pixmap.scaled(
                16, 16,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            icon_cache[url] = scaled
            if not sip.isdeleted(self) and not sip.isdeleted(self.icon_label):
                self.icon_label.setPixmap(scaled)
        else:
            self.set_default_icon()


# ─── Виджет группы вкладок ───────────────────────────────────────────────────
class GroupWidget(QWidget):
    def __init__(self, group_data, sidebar_app, is_expanded=True):
        super().__init__()
        self.sidebar_app = sidebar_app
        self.group_id = None
        self.color = None

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 4, 0, 4)
        self.main_layout.setSpacing(2)

        self.header = QPushButton(group_data['title'] or "Группа")
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.clicked.connect(self.toggle_collapse)

        self.tabs_container = QWidget()
        self.tabs_container.setObjectName("groupTabsContent")

        self.tabs_layout = QVBoxLayout(self.tabs_container)
        self.tabs_layout.setContentsMargins(18, 0, 0, 0)
        self.tabs_layout.setSpacing(0)

        self.main_layout.addWidget(self.header)
        self.main_layout.addWidget(self.tabs_container)

        self.update_data(group_data, is_expanded)

    def update_data(self, group_data, is_expanded):
        new_color = CHROME_COLORS.get(group_data['color'], "#5f6368")
        new_title = group_data['title'] or "Группа"

        self.group_id = group_data['id']
        if self.color != new_color or self.header.text() != new_title:
            self.color = new_color
            self.header.setText(new_title)
            self.header.setStyleSheet(f"""
                QPushButton {{
                    background-color: #202124; color: {self.color};
                    border: 1px solid {self.color}; border-radius: 6px;
                    padding: 6px 10px; font-weight: bold;
                    text-align: left; font-size: 10px;
                    margin-left: 6px; margin-right: 6px;
                }}
                QPushButton:hover {{ background-color: #303134; }}
            """)
            self.tabs_container.setStyleSheet(f"""
                QWidget#groupTabsContent {{
                    border-left: 2px solid {self.color};
                    margin-left: 10px;
                    background: transparent;
                }}
            """)

        self.is_expanded = is_expanded
        self.tabs_container.setVisible(self.is_expanded)

    def toggle_collapse(self):
        self.is_expanded = not self.is_expanded
        self.tabs_container.setVisible(self.is_expanded)
        if hasattr(self, 'sidebar_app') and self.sidebar_app:
            self.sidebar_app.group_states[self.group_id] = self.is_expanded

    def add_tab(self, tab_w):
        self.tabs_layout.addWidget(tab_w)


# ─── Главное окно ─────────────────────────────────────────────────────────────
class SidebarApp(QWidget):
    def __init__(self):
        super().__init__()
        self.w_open   = 350
        self.w_closed = 8

        self.group_states   = {}
        self.tab_widgets    = {}    # {tab_id: TabWidget}
        self.group_widgets  = {}    # {group_id: GroupWidget}
        self.last_data_raw  = ""
        self.force_update   = False
        self.scroll_to_tab_id    = None
        self.scroll_to_group_id  = None
        self.scroll_to_active_tab = False
        self.last_active_tab_id  = None
        self.tray_icon = None

        # ── Новое: мультиселект ──────────────────────────────────────────────
        self.selected_tab_ids   = set()   # множество выделенных tab_id
        self.last_clicked_tab_id = None   # для Shift+Click диапазона

        # ── Новое: retry закрытия ────────────────────────────────────────────
        self.pending_closes = {}  # {tab_id: float(timestamp)}

        # Троттлинг обновлений
        self.update_timer = QTimer()
        self.update_timer.setSingleShot(True)
        self.update_timer.setInterval(150)
        self.update_timer.timeout.connect(self.actual_ui_update)
        self.pending_data = None

        # Платформа
        self.is_windows = platform.system() == 'Windows'
        if self.is_windows:
            try:
                import ctypes
                self.user32 = ctypes.windll.user32
                if self.user32:
                    self.user32.WindowFromPoint.argtypes = [ctypes.wintypes.POINT]
                    self.user32.WindowFromPoint.restype  = ctypes.wintypes.HWND
            except:
                self.user32 = None

        screen      = QApplication.primaryScreen().availableGeometry()
        full_screen = QApplication.primaryScreen().geometry()

        if screen.y() > full_screen.y():
            self.offset_y = screen.y() - 10
        else:
            self.offset_y = screen.y()

        self.real_height = screen.height()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setGeometry(0, self.offset_y, self.w_closed, self.real_height)

        # Маркер-полоска для вызова панели
        self.marker = QFrame(self)
        self.marker.setGeometry(0, 0, 4, self.real_height)
        self.marker.setStyleSheet("background-color: rgba(138,180,248,0.01); border-radius: 2px;")

        # Основной контейнер
        self.container = QFrame(self)
        self.container.setGeometry(-self.w_open, 0, self.w_open, self.real_height)
        self.container.setStyleSheet(
            "background-color: #202124; border-right: 1px solid #3c4043;"
        )

        vbox = QVBoxLayout(self.container)
        vbox.setContentsMargins(0, 0, 0, 0)

        self.status_label = QLabel("Ожидание Chrome...")
        self.status_label.setStyleSheet(
            "color: #5f6368; font-size: 10px; padding: 5px;"
        )
        vbox.addWidget(self.status_label)

        # Список вкладок
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.scroll_content = QWidget()
        self.scroll_layout  = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll_layout.setContentsMargins(4, 5, 4, 5)
        self.scroll_layout.setSpacing(4)
        self.scroll.setWidget(self.scroll_content)

        self.scroll.setStyleSheet("""
            QScrollArea { border: none; background: transparent; }
            QScrollBar:vertical {
                border: none; background: #202124; width: 8px; margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #3c4043; min-height: 20px; border-radius: 4px;
            }
            QScrollBar::handle:vertical:hover { background: #5f6368; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
        """)

        vbox.addWidget(self.scroll)

        # Кнопка «Новая вкладка»
        self.new_tab_btn = QPushButton("+ Новая вкладка")
        self.new_tab_btn.setFixedHeight(32)
        self.new_tab_btn.setStyleSheet("""
            QPushButton {
                background-color: #3c4043; color: #8ab4f8;
                border: none; border-top: 1px solid #5f6368;
                font-size: 12px; font-weight: bold; padding: 8px;
            }
            QPushButton:hover   { background-color: #45474a; }
            QPushButton:pressed { background-color: #5f6368; }
        """)
        self.new_tab_btn.clicked.connect(self.create_new_tab)
        vbox.addWidget(self.new_tab_btn)

        # Анимация выдвижения
        self.anim = QPropertyAnimation(self.container, b"geometry")
        self.anim.setDuration(150)

        signals.data_received.connect(self.request_update)

    # ── Мультиселект ─────────────────────────────────────────────────────────
    def toggle_tab_selection(self, tab_id):
        """Переключает выделение одной вкладки (Ctrl+Click)."""
        if tab_id in self.selected_tab_ids:
            self.selected_tab_ids.discard(tab_id)
            if tab_id in self.tab_widgets:
                self.tab_widgets[tab_id].set_selected(False)
        else:
            self.selected_tab_ids.add(tab_id)
            if tab_id in self.tab_widgets:
                self.tab_widgets[tab_id].set_selected(True)
        self.last_clicked_tab_id = tab_id
        self._update_status_label()

    def clear_selection(self):
        """Снимает выделение со всех вкладок."""
        for tid in list(self.selected_tab_ids):
            if tid in self.tab_widgets:
                self.tab_widgets[tid].set_selected(False)
        self.selected_tab_ids.clear()
        self._update_status_label()

    def range_select_tabs(self, tab_id):
        """Выделяет диапазон вкладок от last_clicked_tab_id до tab_id (Shift+Click)."""
        if not self.pending_data:
            self.toggle_tab_selection(tab_id)
            return
        all_ids = [t['id'] for t in self.pending_data.get('tabs', [])]
        last = self.last_clicked_tab_id
        if last is None or last not in all_ids or tab_id not in all_ids:
            self.toggle_tab_selection(tab_id)
            return
        i1 = all_ids.index(last)
        i2 = all_ids.index(tab_id)
        start, end = min(i1, i2), max(i1, i2)
        for tid in all_ids[start:end + 1]:
            self.selected_tab_ids.add(tid)
            if tid in self.tab_widgets:
                self.tab_widgets[tid].set_selected(True)
        self.last_clicked_tab_id = tab_id
        self._update_status_label()

    def _update_status_label(self):
        n_tabs     = len(self.pending_data.get('tabs', [])) if self.pending_data else 0
        n_selected = len(self.selected_tab_ids)
        if n_selected > 0:
            self.status_label.setText(
                f"Вкладок: {n_tabs}  ·  Выбрано: {n_selected}"
            )
        else:
            self.status_label.setText(f"Вкладок: {n_tabs}")

    # ── Новая вкладка ────────────────────────────────────────────────────────
    def create_new_tab(self):
        cmd = json.dumps({"action": "new_tab"})
        print("Creating new tab")
        command_queue.put(cmd)
        self.force_update = True
        self.scroll_to_active_tab = True
        QTimer.singleShot(100, lambda: command_queue.put(
            json.dumps({"action": "request_update"})))

    # ── Проверка активности Chrome ───────────────────────────────────────────
    def is_chrome_in_foreground(self):
        if not self.is_windows or not self.user32:
            return True
        try:
            foreground_hwnd = self.user32.GetForegroundWindow()
            if not foreground_hwnd:
                return False
            if foreground_hwnd == int(self.winId()):
                return True

            pos = ctypes.wintypes.POINT()
            self.user32.GetCursorPos(ctypes.byref(pos))

            check_x    = self.w_closed + 10
            hwnd_under = self.user32.WindowFromPoint(ctypes.wintypes.POINT(check_x, pos.y))
            if not hwnd_under:
                return False

            hwnd_root = self.user32.GetAncestor(hwnd_under, 2)
            if hwnd_root == int(self.winId()):
                return True

            buffer = ctypes.create_unicode_buffer(256)
            self.user32.GetClassNameW(hwnd_root, buffer, 256)
            under_cls = buffer.value

            self.user32.GetClassNameW(foreground_hwnd, buffer, 256)
            fg_cls = buffer.value

            is_chrome_under  = "Chrome_WidgetWin" in under_cls or "Cent" in under_cls
            is_chrome_active = "Chrome_WidgetWin" in fg_cls    or "Cent" in fg_cls

            if not (is_chrome_active and is_chrome_under):
                return False
            if self.user32.IsIconic(hwnd_root):
                return False
            return True
        except:
            return False

    # ── Обновление UI ────────────────────────────────────────────────────────
    def request_update(self, data):
        data_str = json.dumps(data, sort_keys=True)
        if data_str == self.last_data_raw and not self.force_update:
            return
        self.pending_data  = data
        self.last_data_raw = data_str
        self.update_timer.start()

    def actual_ui_update(self):
        if not self.pending_data:
            return

        # Если открыто контекстное меню — откладываем
        if QApplication.activePopupWidget():
            self.update_timer.start(500)
            return

        if self.underMouse() and not self.force_update:
            self.update_timer.start(500)
            return

        force_update_active = self.force_update
        self.force_update   = False

        data      = self.pending_data
        tabs_data = data.get('tabs', [])
        if not tabs_data:
            return

        n_selected = len(self.selected_tab_ids)
        if n_selected > 0:
            self.status_label.setText(
                f"Вкладок: {len(tabs_data)}  ·  Выбрано: {n_selected}"
            )
        else:
            self.status_label.setText(f"Вкладок: {len(tabs_data)}")

        v_bar      = self.scroll.verticalScrollBar()
        old_scroll = v_bar.value()

        # 1. Удаляем виджеты исчезнувших вкладок
        current_tab_ids = {tab['id'] for tab in tabs_data}
        for tid in list(self.tab_widgets.keys()):
            if tid not in current_tab_ids:
                self.selected_tab_ids.discard(tid)    # снимаем из выделения
                self.pending_closes.pop(tid, None)    # снимаем из retry
                self.tab_widgets.pop(tid).deleteLater()

        # 2. Retry: если вкладка не закрылась за 600 мс — повторяем команду
        now = time.time()
        for tab_id, sent_time in list(self.pending_closes.items()):
            if tab_id not in current_tab_ids:
                self.pending_closes.pop(tab_id, None)
            elif now - sent_time > 0.6:
                print(f"Retry close for tab {tab_id}")
                command_queue.put(json.dumps({"action": "close", "id": tab_id}))
                self.pending_closes[tab_id] = now  # сброс таймера

        # 3. Удаляем виджеты исчезнувших групп
        current_group_ids = {g['id'] for g in data.get('groups', [])}
        for gid in list(self.group_widgets.keys()):
            if gid not in current_group_ids:
                self.group_widgets.pop(gid).deleteLater()

        # 4. Обновляем / создаём виджеты
        groups_map       = {g['id']: g for g in data.get('groups', [])}
        all_groups_data  = data.get('groups', [])

        if force_update_active and self.scroll_to_group_id:
            self.group_states[self.scroll_to_group_id] = True

        # Разворачиваем группу активной вкладки при автоскролле
        if self.scroll_to_active_tab:
            for tab in tabs_data:
                if tab['active']:
                    ag_id = tab['groupId'] if tab['groupId'] != -1 else None
                    if ag_id:
                        self.group_states[ag_id] = True
                    break

        target_widget       = None
        main_idx            = 0
        group_tab_indices   = {gid: 0 for gid in current_group_ids}
        added_groups_to_layout = set()

        for tab in tabs_data:
            tid  = tab['id']
            g_id = tab['groupId']

            if tid in self.tab_widgets:
                tab_widget = self.tab_widgets[tid]
                tab_widget.update_data(tab)
            else:
                tab_widget = TabWidget(tab, self)
                self.tab_widgets[tid] = tab_widget
                # Если ID уже в выделении (маловероятно, но на всякий случай)
                if tid in self.selected_tab_ids:
                    tab_widget.set_selected(True)

            tab_widget.available_groups = all_groups_data

            if g_id != -1 and g_id in groups_map:
                if g_id not in self.group_widgets:
                    self.group_widgets[g_id] = GroupWidget(
                        groups_map[g_id], self, self.group_states.get(g_id, True)
                    )
                else:
                    self.group_widgets[g_id].update_data(
                        groups_map[g_id], self.group_states.get(g_id, True)
                    )

                group_w = self.group_widgets[g_id]
                if g_id not in added_groups_to_layout:
                    item = self.scroll_layout.itemAt(main_idx)
                    if not item or item.widget() != group_w:
                        self.scroll_layout.insertWidget(main_idx, group_w)
                    main_idx += 1
                    added_groups_to_layout.add(g_id)

                g_item = group_w.tabs_layout.itemAt(group_tab_indices[g_id])
                if not g_item or g_item.widget() != tab_widget:
                    group_w.tabs_layout.insertWidget(group_tab_indices[g_id], tab_widget)
                group_tab_indices[g_id] += 1
            else:
                item = self.scroll_layout.itemAt(main_idx)
                if not item or item.widget() != tab_widget:
                    self.scroll_layout.insertWidget(main_idx, tab_widget)
                main_idx += 1

            if (self.scroll_to_active_tab or force_update_active) and tab['active']:
                target_widget = tab_widget
            elif self.scroll_to_tab_id == tid:
                target_widget = tab_widget

        # 5. Автоскролл
        if target_widget:
            self.scroll_to_active_tab = False
            self.scroll_to_tab_id     = None
            self.scroll_to_group_id   = None
            self.scroll_content.adjustSize()

            def do_scroll():
                if not sip.isdeleted(self) and not sip.isdeleted(target_widget):
                    self.scroll.ensureWidgetVisible(target_widget, 0, 100)

            QTimer.singleShot(200, do_scroll)
        else:
            def restore_scroll():
                if not sip.isdeleted(v_bar):
                    v_bar.setValue(old_scroll)

            QTimer.singleShot(1, restore_scroll)

    # ── Анимация ─────────────────────────────────────────────────────────────
    def enterEvent(self, event):
        if not self.is_chrome_in_foreground():
            return
        self.scroll_to_active_tab = True
        self.force_update = True
        self.setGeometry(0, self.offset_y, self.w_open, self.real_height)
        self.anim.stop()
        self.anim.setEndValue(QRect(0, 0, self.w_open, self.real_height))
        self.anim.start()
        command_queue.put(json.dumps({"action": "request_update"}))
        QTimer.singleShot(150, lambda: command_queue.put(
            json.dumps({"action": "request_update"})))

    def leaveEvent(self, event):
        if QApplication.activePopupWidget():
            return
        self.anim.stop()
        self.anim.setEndValue(QRect(-self.w_open, 0, self.w_open, self.real_height))
        self.anim.start()
        QTimer.singleShot(160, self._check_hide)

    def _check_hide(self):
        if not self.underMouse() and not QApplication.activePopupWidget():
            self.setGeometry(0, self.offset_y, self.w_closed, self.real_height)
        elif QApplication.activePopupWidget():
            QTimer.singleShot(500, self._check_hide)
        else:
            QTimer.singleShot(100, self._check_hide)


# ─── WebSocket сервер ─────────────────────────────────────────────────────────
connected_clients = set()


async def ws_handler(websocket):
    addr = websocket.remote_address
    print(f"Bridge connected: {addr}")
    connected_clients.add(websocket)
    print(f"Total connected clients: {len(connected_clients)}")
    try:
        async for message in websocket:
            data = json.loads(message)
            if data.get('type') == 'ping':
                continue
            signals.data_received.emit(data)
    except websockets.exceptions.ConnectionClosed:
        print(f"Bridge disconnected: {addr}")
    except Exception as e:
        print(f"WS Error: {e}")
    finally:
        connected_clients.discard(websocket)
        print(f"Client removed. Total connected: {len(connected_clients)}")


async def send_worker():
    """Отправляет команды из Qt-очереди всем подключённым расширениям."""
    print("Send worker is ALIVE and running")
    while True:
        try:
            await asyncio.sleep(0.01)
            try:
                cmd = command_queue.get_nowait()
                print(f">>> COMMAND: {cmd}")
            except queue.Empty:
                continue

            if not connected_clients:
                print("!!! No extensions connected")
            else:
                clients = list(connected_clients)
                print(f">>> Sending to {len(clients)} client(s)")
                for client in clients:
                    try:
                        await client.send(cmd)
                        print(f">>> Sent to {client.remote_address}")
                    except Exception as e:
                        print(f">>> Failed to send: {e}")
        except Exception as e:
            print(f"Worker Error: {e}")
            import traceback
            traceback.print_exc()


async def main_async():
    async with websockets.serve(ws_handler, "127.0.0.1", 8765):
        print("WebSocket Server started on ws://127.0.0.1:8765")
        asyncio.create_task(send_worker())
        await asyncio.Future()


def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


# ─── Точка входа ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    myappid = 'ChromeTabsAlt.1.0'
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except:
        pass

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    tray_icon = QSystemTrayIcon()
    icon_path = resource_path("icon128.ico")
    tray_icon.setIcon(QIcon(icon_path))

    tray_menu = QMenu()
    exit_action = tray_menu.addAction("Выход")
    exit_action.triggered.connect(app.quit)
    tray_icon.setContextMenu(tray_menu)
    tray_icon.show()

    threading.Thread(target=lambda: asyncio.run(main_async()), daemon=True).start()

    network_manager = QNetworkAccessManager()
    window = SidebarApp()
    window.show()

    sys.exit(app.exec())
