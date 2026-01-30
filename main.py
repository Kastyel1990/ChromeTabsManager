import sys
import json
import asyncio
import threading
import base64
import queue  # Для thread-safe коммуникации между Qt и asyncio
import platform
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QPushButton, 
                             QScrollArea, QHBoxLayout, QMenu, QFrame, QLabel, QSizePolicy, QSystemTrayIcon)
from PyQt6.QtCore import Qt, QPropertyAnimation, QRect, pyqtSignal, QObject, QTimer, QUrl, QSize, QPoint
from PyQt6.QtGui import QPixmap, QPainter, QPen, QBrush, QPolygon, QColor, QIcon
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PyQt6.QtSvg import QSvgRenderer
from PyQt6 import sip
import websockets, ctypes
import os

# Цвета групп Chrome
CHROME_COLORS = {
    "grey": "#5F6368", "blue": "#1A73E8", "red": "#D93025", 
    "yellow": "#FABB06", "green": "#1E8E3E", "pink": "#D01884", 
    "purple": "#9333E6", "cyan": "#12B5CB", "orange": "#E8710A"
}

network_manager = None
icon_cache = {}

# Thread-safe очередь для команд из Qt в asyncio
command_queue = queue.Queue()

class CommSignal(QObject):
    data_received = pyqtSignal(dict)
    send_command = pyqtSignal(str)

signals = CommSignal()

class TabWidget(QWidget):
    def __init__(self, tab_data, sidebar_app=None):
        super().__init__()
        self.tab_id = tab_data['id']
        self.fav_icon_url = tab_data.get('favIcon', '')
        self.is_active = tab_data['active']
        self.sidebar_app = sidebar_app  # Ссылка на главное окно
        
        self.main_layout = QHBoxLayout(self)
        self.main_layout.setContentsMargins(0, 1, 4, 1) 
        self.main_layout.setSpacing(0)

        self.base_frame = QFrame()
        # Уникальное имя для стилизации только этой рамки
        self.base_frame.setObjectName("tabBaseFrame")

        # ВАЖНО: разрешаем контекстное меню
        self.base_frame.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.base_frame.customContextMenuRequested.connect(self.show_context_menu)

        # Перехватываем клик левой кнопкой по всей рамке
        self.base_frame.mousePressEvent = self.on_frame_click
        
        active_border = "3px solid #8ab4f8" if self.is_active else "3px solid transparent"
        bg_color = "#3c4043" if self.is_active else "#292a2d"
        
        # Используем #tabBaseFrame, чтобы стиль не уходил внутрь к иконкам
        self.base_frame.setStyleSheet(f"""
            QFrame#tabBaseFrame {{
                background-color: {bg_color};
                border-radius: 4px;
                border-left: {active_border};
            }}
            QFrame#tabBaseFrame:hover {{ background-color: #45474a; }}
        """)
        
        self.frame_layout = QHBoxLayout(self.base_frame)
        self.frame_layout.setContentsMargins(8, 4, 6, 4)
        self.frame_layout.setSpacing(10)

        # ИКОНКА - теперь она в безопасности от стилей группы
        self.icon_label = QLabel()
        self.icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.icon_label.setObjectName("tabIcon")
        self.icon_label.setFixedSize(16, 16)
        self.icon_label.setScaledContents(False) # ЗАПРЕТ на автоматическое растягивание
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setStyleSheet("background: transparent; border: none; padding: 0;")
        self.set_initial_icon()

        # Вместо QPushButton используем QLabel для текста, чтобы он не мешал кликам
        self.title_label = QLabel(tab_data['title'][:40] or "Новая вкладка")
        self.title_label.setStyleSheet("color: #e8eaed; font-size: 11px; background: transparent;")
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents) 

        # ТЕКСТ
        #self.btn = QPushButton(tab_data['title'][:40] or "Новая вкладка")
        #self.btn.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        #self.btn.setStyleSheet("background: transparent; border: none; color: #e8eaed; text-align: left; font-size: 11px;")
        #self.btn.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)

        # КРЕСТИК
        self.close_btn = QPushButton("×")
        self.close_btn.setFixedSize(20, 20)
        self.close_btn.setStyleSheet("""
            QPushButton { color: #9aa0a6; border: none; font-size: 16px; background: none; }
            QPushButton:hover { color: #f28b82; background: #3c4043; border-radius: 10px; }
        """)
        # Чтобы крестик работал отдельно от рамки
        self.close_btn.clicked.connect(self.on_close_click)

        self.frame_layout.addWidget(self.icon_label, 0)
        self.frame_layout.addWidget(self.title_label, 1)
        self.frame_layout.addWidget(self.close_btn, 0)
        self.main_layout.addWidget(self.base_frame)

        # Клик левой кнопкой - активация
        #self.title_label..clicked.connect(lambda: signals.send_command.emit(json.dumps({"action": "activate", "id": self.tab_id})))
        #self.close_btn.clicked.connect(lambda: signals.send_command.emit(json.dumps({"action": "close", "id": self.tab_id})))

    def on_frame_click(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            cmd = json.dumps({"action": "activate", "id": self.tab_id})
            print(f"Sending activate for tab {self.tab_id}")
            print(f"Qt thread putting command in queue: {cmd}")
            command_queue.put(cmd)
            # Принудительно обновляем UI немедленно
            if self.sidebar_app:
                self.sidebar_app.force_update = True
                QTimer.singleShot(30, lambda: command_queue.put(json.dumps({"action": "request_update"})))

    def on_close_click(self):
        cmd = json.dumps({"action": "close", "id": self.tab_id})
        print(f"Sending close for tab {self.tab_id}")
        print(f"Qt thread putting command in queue: {cmd}")
        command_queue.put(cmd)
        # Принудительно обновляем UI немедленно
        if self.sidebar_app:
            self.sidebar_app.force_update = True
            QTimer.singleShot(30, lambda: command_queue.put(json.dumps({"action": "request_update"})))

    def show_context_menu(self, position):
        menu = QMenu(self)
        
        # Обработчик закрытия меню
        def on_menu_closed():
            # Если панель всё ещё открыта и мышь не над ней, скрываем панель
            if self.sidebar_app and not self.sidebar_app.underMouse():
                QTimer.singleShot(100, lambda: self.sidebar_app._check_hide())
        
        menu.aboutToHide.connect(on_menu_closed)
        
        # Стилизация меню под Chrome
        menu.setStyleSheet("""
            QMenu {
                background-color: #35363a;
                color: #e8eaed;
                border: 1px solid #45474a;
                border-radius: 4px;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 24px 6px 24px;
                border-radius: 2px;
            }
            QMenu::item:selected {
                background-color: #8ab4f8;
                color: #202124;
            }
            QMenu::separator {
                height: 1px;
                background: #45474a;
                margin: 4px 8px;
            }
        """)    
        
        dup = menu.addAction("Дублировать")
        pin = menu.addAction("Закрепить / Открепить")
        menu.addSeparator()
        
        # Подменю для добавления в группу
        add_to_group_menu = menu.addMenu("Добавить в группу")
        add_to_group_menu.setStyleSheet(menu.styleSheet())
        
        # Получаем список групп из последних данных
        groups_actions = {}
        if hasattr(self, 'available_groups') and self.available_groups:
            for group in self.available_groups:
                group_title = group.get('title') or f"Группа {group['id']}"
                action = add_to_group_menu.addAction(f"📁 {group_title}")
                groups_actions[action] = group['id']
        
        new_group_action = add_to_group_menu.addAction("➕ Создать новую группу")
        remove_from_group = menu.addAction("Убрать из группы")
        
        menu.addSeparator()
        others = menu.addAction("Закрыть другие")
        
        # Перед открытием меню помечаем, что мы «в меню»
        action = menu.exec(self.base_frame.mapToGlobal(position))

        if action == dup:
            command_queue.put(json.dumps({"action": "duplicate", "id": self.tab_id}))
            # Принудительно обновляем UI немедленно
            if self.sidebar_app:
                self.sidebar_app.force_update = True
                QTimer.singleShot(80, lambda: command_queue.put(json.dumps({"action": "request_update"})))
        elif action == pin:
            command_queue.put(json.dumps({"action": "toggle_pin", "id": self.tab_id}))
            if self.sidebar_app:
                self.sidebar_app.force_update = True
                QTimer.singleShot(30, lambda: command_queue.put(json.dumps({"action": "request_update"})))
        elif action == others:
            command_queue.put(json.dumps({"action": "close_others", "id": self.tab_id}))
            if self.sidebar_app:
                self.sidebar_app.force_update = True
                QTimer.singleShot(50, lambda: command_queue.put(json.dumps({"action": "request_update"})))
        elif action == remove_from_group:
            command_queue.put(json.dumps({"action": "remove_from_group", "id": self.tab_id}))
            if self.sidebar_app:
                self.sidebar_app.force_update = True
                QTimer.singleShot(30, lambda: command_queue.put(json.dumps({"action": "request_update"})))
        elif action == new_group_action:
            command_queue.put(json.dumps({"action": "add_to_new_group", "id": self.tab_id}))
            if self.sidebar_app:
                self.sidebar_app.force_update = True
                # Скроллим к новой группе (будет создана, узнаем позже)
                QTimer.singleShot(100, lambda: command_queue.put(json.dumps({"action": "request_update"})))
        elif action in groups_actions:
            group_id = groups_actions[action]
            command_queue.put(json.dumps({"action": "add_to_group", "id": self.tab_id, "groupId": group_id}))
            if self.sidebar_app:
                self.sidebar_app.force_update = True
                self.sidebar_app.scroll_to_group_id = group_id  # Автоматически скроллим к группе
                QTimer.singleShot(50, lambda: command_queue.put(json.dumps({"action": "request_update"})))

    def set_initial_icon(self):
        # Если нет иконки, устанавливаем дефолтную
        if not self.fav_icon_url:
            self.set_default_icon()
            return
            
        if self.fav_icon_url in icon_cache:
            self.icon_label.setPixmap(icon_cache[self.fav_icon_url])
            return
        if self.fav_icon_url.startswith('data:image'):
            try:
                header, encoded = self.fav_icon_url.split(",", 1)
                data = base64.b64decode(encoded)
                self.process_image_data(data, self.fav_icon_url)
            except:
                self.set_default_icon()
        elif self.fav_icon_url.startswith('http'):
            request = QNetworkRequest(QUrl(self.fav_icon_url))
            request.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, "Mozilla/5.0")
            reply = network_manager.get(request)
            reply.finished.connect(lambda: self.on_icon_loaded(reply, self.fav_icon_url))
        else:
            self.set_default_icon()
    
    def set_default_icon(self):
        """Устанавливает иконку по умолчанию (символ страницы)"""
        if sip.isdeleted(self) or sip.isdeleted(self.icon_label):
            return
        
        # Создаём простую иконку документа
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.GlobalColor.transparent)
        
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Рисуем простой документ (квадрат с загнутым уголком)
        pen = QPen(QColor("#9aa0a6"))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor("#5f6368")))
        
        # Основной прямоугольник документа
        painter.drawRect(3, 2, 10, 12)
        
        # Загнутый уголок
        points = QPolygon([
            QPoint(13, 2),
            QPoint(13, 5),
            QPoint(10, 5)
        ])
        painter.setBrush(QBrush(QColor("#9aa0a6")))
        painter.drawPolygon(points)
        
        painter.end()
        
        self.icon_label.setPixmap(pixmap)

    def on_icon_loaded(self, reply, url):
        if sip.isdeleted(self) or sip.isdeleted(self.icon_label): return
        if reply.error() == QNetworkReply.NetworkError.NoError:
            self.process_image_data(reply.readAll(), url)
        else:
            # Если загрузка не удалась, устанавливаем дефолтную иконку
            self.set_default_icon()
        reply.deleteLater()

    def process_image_data(self, data, url):
        # Проверяем, что виджет ещё существует
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
            scaled = pixmap.scaled(16, 16, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            icon_cache[url] = scaled
            # Двойная проверка перед установкой pixmap
            if not sip.isdeleted(self) and not sip.isdeleted(self.icon_label):
                self.icon_label.setPixmap(scaled)
        else:
            # Если не удалось загрузить изображение, используем дефолтную иконку
            self.set_default_icon()

class GroupWidget(QWidget):
    def __init__(self, group_data, is_expanded=True, parent_app=None):
        super().__init__()
        self.group_id = group_data['id']
        self.parent_app = parent_app
        self.color = CHROME_COLORS.get(group_data['color'], "#5f6368")
        
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 4, 0, 4)
        self.main_layout.setSpacing(2)
        
        # Шапка группы
        self.header = QPushButton(group_data['title'] or "Группа")
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.setStyleSheet(f"""
            QPushButton {{
                background-color: #202124; color: {self.color};
                border: 1px solid {self.color}; border-radius: 6px;
                padding: 6px 10px; font-weight: bold; text-align: left; font-size: 10px;
                margin-left: 6px; margin-right: 6px;
            }}
            QPushButton:hover {{ background-color: #303134; }}
        """)
        
        # Контейнер для вкладок ГРУППЫ
        self.tabs_container = QWidget()
        self.tabs_container.setObjectName("groupTabsContent")
        
        self.tabs_layout = QVBoxLayout(self.tabs_container)
        # УВЕЛИЧИЛИ отступ слева до 14px, чтобы вкладки не касались линии
        self.tabs_layout.setContentsMargins(18, 0, 0, 0) 
        self.tabs_layout.setSpacing(0)
        
        # Линия группы
        self.tabs_container.setStyleSheet(f"""
            QWidget#groupTabsContent {{ 
                border-left: 2px solid {self.color}; 
                /* Линия теперь стоит в 10 пикселях от края окна */
                margin-left: 10px; 
                background: transparent;
            }}
        """)
        
        self.main_layout.addWidget(self.header)
        self.main_layout.addWidget(self.tabs_container)
        
        self.is_expanded = is_expanded
        self.tabs_container.setVisible(self.is_expanded)
        self.header.clicked.connect(self.toggle_collapse)

    def toggle_collapse(self):
        self.is_expanded = not self.is_expanded
        self.tabs_container.setVisible(self.is_expanded)
        if self.parent_app:
            self.parent_app.group_states[self.group_id] = self.is_expanded

    def add_tab(self, tab_w):
        # Добавляем вкладку в группу
        self.tabs_layout.addWidget(tab_w)

class SidebarApp(QWidget):
    def __init__(self):
        super().__init__()
        self.w_open = 350
        self.w_closed = 8
        self.group_states = {}
        self.last_data_raw = "" # Кэш для сравнения данных
        self.force_update = False  # Флаг для принудительного обновления
        self.scroll_to_tab_id = None  # ID вкладки для автоскролла
        self.scroll_to_group_id = None  # ID группы для автоскролла
        self.scroll_to_active_tab = False  # Флаг для автоскролла к активной вкладке
        self.last_active_tab_id = None  # ID последней активной вкладки
        
        # Троттлинг обновлений
        self.update_timer = QTimer()
        self.update_timer.setSingleShot(True)
        self.update_timer.setInterval(150)
        self.update_timer.timeout.connect(self.actual_ui_update)
        self.pending_data = None
        
        # Определяем платформу для проверки активного окна
        self.is_windows = platform.system() == 'Windows'
        if self.is_windows:
            try:
                import ctypes
                self.user32 = ctypes.windll.user32
            except:
                self.user32 = None

        screen = QApplication.primaryScreen().availableGeometry()
        full_screen = QApplication.primaryScreen().geometry()
        
        # Проверяем положение панели задач
        # Если availableGeometry начинается не с 0, значит панель задач вверху
        if screen.y() > full_screen.y():
            self.offset_y = screen.y() - 10  # Панель задач вверху
        else:
            self.offset_y = screen.y()  # Панель задач внизу или сбоку
        
        self.real_height = screen.height()
        
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setGeometry(0, self.offset_y, self.w_closed, self.real_height)
        
        self.marker = QFrame(self)
        self.marker.setGeometry(0, 0, 4, self.real_height)
        self.marker.setStyleSheet("background-color: rgba(138, 180, 248, 0.01); border-radius: 2px;")

        self.container = QFrame(self)
        self.container.setGeometry(-self.w_open, 0, self.w_open, self.real_height)
        self.container.setStyleSheet("background-color: #202124; border-right: 1px solid #3c4043;")
        
        vbox = QVBoxLayout(self.container)
        vbox.setContentsMargins(0, 0, 0, 0)
        
        self.status_label = QLabel("Ожидание Chrome...")
        self.status_label.setStyleSheet("color: #5f6368; font-size: 10px; padding: 5px;")
        vbox.addWidget(self.status_label)
        
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll_layout.setContentsMargins(4, 5, 4, 5)
        self.scroll_layout.setSpacing(4)
        self.scroll.setWidget(self.scroll_content)

        self.scroll.setStyleSheet("""
            QScrollArea { border: none; background: transparent; }
            QScrollBar:vertical {
                border: none;
                background: #202124;
                width: 8px;
                margin: 0px 0px 0px 0px;
            }
            QScrollBar::handle:vertical {
                background: #3c4043;
                min-height: 20px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical:hover {
                background: #5f6368;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: none;
            }
        """)

        vbox.addWidget(self.scroll)
        
        # Кнопка создания новой вкладки (зафиксирована внизу)
        self.new_tab_btn = QPushButton("+ Новая вкладка")
        self.new_tab_btn.setFixedHeight(32)
        self.new_tab_btn.setStyleSheet("""
            QPushButton {
                background-color: #3c4043;
                color: #8ab4f8;
                border: none;
                border-top: 1px solid #5f6368;
                font-size: 12px;
                font-weight: bold;
                padding: 8px;
            }
            QPushButton:hover {
                background-color: #45474a;
            }
            QPushButton:pressed {
                background-color: #5f6368;
            }
        """)
        self.new_tab_btn.clicked.connect(self.create_new_tab)
        vbox.addWidget(self.new_tab_btn)
        
        self.anim = QPropertyAnimation(self.container, b"geometry")
        self.anim.setDuration(150)
        
        signals.data_received.connect(self.request_update)
    
    def create_new_tab(self):
        """Создание новой вкладки"""
        cmd = json.dumps({"action": "new_tab"})
        print("Creating new tab")
        command_queue.put(cmd)
        # Принудительно обновляем UI немедленно и скроллим к новой вкладке
        self.force_update = True
        self.scroll_to_active_tab = True  # Новая вкладка станет активной
        QTimer.singleShot(100, lambda: command_queue.put(json.dumps({"action": "request_update"})))
    
    def is_chrome_in_foreground(self):
        """Проверяет, находится ли Chrome на переднем плане"""
        if not self.is_windows or not self.user32:
            # На не-Windows платформах или если ctypes недоступен, всегда показываем
            return True
        
        try:
            # Получаем handle активного окна
            hwnd = self.user32.GetForegroundWindow()
            if not hwnd:
                return False
            
            # Получаем заголовок окна
            length = self.user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return False
            
            import ctypes
            buffer = ctypes.create_unicode_buffer(length + 1)
            self.user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value
            
            # Проверяем, содержит ли заголовок "Chrome"
            # Chrome окна обычно имеют формат: "Title - Google Chrome"
            return "Chrome" in title or "chrome" in title.lower() or "Cent" in title or "cent" in title.lower()
        except:
            # В случае ошибки всегда показываем панель
            return True
        # Принудительное обновление UI
        self.force_update = True
        QTimer.singleShot(80, lambda: command_queue.put(json.dumps({"action": "request_update"})))

    def request_update(self, data):
        # Если данные идентичны старым - игнорируем
        data_str = json.dumps(data, sort_keys=True)
        if data_str == self.last_data_raw:
            return
        
        self.pending_data = data
        self.last_data_raw = data_str
        self.update_timer.start()

    def actual_ui_update(self):
        if not self.pending_data: return
        
        # КРИТИЧЕСКИ ВАЖНО: Не обновляем UI, если открыто контекстное меню
        # НО если установлен флаг force_update, обновляем в любом случае
        if QApplication.activePopupWidget():
            # Меню открыто - всегда откладываем
            self.update_timer.start(500)
            return
        
        if self.underMouse() and not self.force_update:
            # Мышь над панелью, но обновление не принудительное - откладываем
            self.update_timer.start(500) 
            return
        
        # Сбрасываем флаг
        force_update_active = self.force_update
        self.force_update = False
        
        data = self.pending_data
        self.status_label.setText(f"Вкладок: {len(data.get('tabs', []))}")
        
        v_bar = self.scroll.verticalScrollBar()
        old_scroll = v_bar.value()

        # Очистка 
        while self.scroll_layout.count():
            item = self.scroll_layout.takeAt(0)
            w = item.widget()
            if w:
                w.hide()  # Сначала скрываем, чтобы избежать мерцания
                w.setParent(None)
                w.deleteLater()
            
        groups_map = {g['id']: g for g in data.get('groups', [])}
        created_groups = {}
        all_groups = data.get('groups', [])
        
        # Если нужно проскроллить к группе, разворачиваем её
        if force_update_active and self.scroll_to_group_id:
            self.group_states[self.scroll_to_group_id] = True
        
        # Находим активную вкладку и её группу (если нужен автоскролл)
        active_tab_id = None
        active_tab_group_id = None
        if self.scroll_to_active_tab:
            for tab in data.get('tabs', []):
                if tab['active']:
                    active_tab_id = tab['id']
                    active_tab_group_id = tab['groupId'] if tab['groupId'] != -1 else None
                    # Если активная вкладка в группе, разворачиваем эту группу
                    if active_tab_group_id:
                        self.group_states[active_tab_group_id] = True
                    break
        
        # Словарь для отслеживания позиций виджетов (для автоскролла)
        widget_positions = {}
        current_y = 0
        target_tab_id_for_scroll = None  # ID вкладки, к которой нужно проскроллить
        
        for tab in data.get('tabs', []):
            g_id = tab['groupId']
            if g_id != -1 and g_id in groups_map:
                if g_id not in created_groups:
                    # Проверяем, нужно ли развернуть эту группу
                    is_expanded = self.group_states.get(g_id, True)
                    g_w = GroupWidget(groups_map[g_id], is_expanded, self)
                    self.scroll_layout.addWidget(g_w)
                    created_groups[g_id] = g_w
                    widget_positions[f"group_{g_id}"] = current_y
                    current_y += 32  # Примерная высота заголовка группы
                    
                tab_widget = TabWidget(tab, sidebar_app=self)
                tab_widget.available_groups = all_groups
                created_groups[g_id].add_tab(tab_widget)
                widget_positions[f"tab_{tab['id']}"] = current_y
                
                # Если это группа, к которой нужно скроллить, находим активную вкладку в ней
                if force_update_active and self.scroll_to_group_id == g_id and tab['active']:
                    target_tab_id_for_scroll = tab['id']
                
                # Увеличиваем Y только если группа развёрнута
                if self.group_states.get(g_id, True):
                    current_y += 30  # Примерная высота вкладки
            else:
                tab_widget = TabWidget(tab, sidebar_app=self)
                tab_widget.available_groups = all_groups
                self.scroll_layout.addWidget(tab_widget)
                widget_positions[f"tab_{tab['id']}"] = current_y
                current_y += 30
        
        # Автоскролл к нужной вкладке или группе
        if force_update_active or self.scroll_to_active_tab:
            scroll_target_y = None
            
            # Приоритет 1: Скролл к активной вкладке (при открытии панели или создании новой вкладки)
            if self.scroll_to_active_tab and active_tab_id:
                key = f"tab_{active_tab_id}"
                if key in widget_positions:
                    scroll_target_y = widget_positions[key]
                self.scroll_to_active_tab = False
            # Приоритет 2: Скролл к конкретной вкладке
            elif self.scroll_to_tab_id:
                key = f"tab_{self.scroll_to_tab_id}"
                if key in widget_positions:
                    scroll_target_y = widget_positions[key]
                self.scroll_to_tab_id = None
            # Приоритет 3: Скролл к группе
            elif self.scroll_to_group_id:
                # Скроллим к активной вкладке в группе (или к началу группы)
                if target_tab_id_for_scroll:
                    key = f"tab_{target_tab_id_for_scroll}"
                    if key in widget_positions:
                        scroll_target_y = widget_positions[key]
                else:
                    key = f"group_{self.scroll_to_group_id}"
                    if key in widget_positions:
                        scroll_target_y = widget_positions[key]
                self.scroll_to_group_id = None
            
            if scroll_target_y is not None:
                # Центрируем: вычитаем половину высоты видимой области
                viewport_height = self.scroll.viewport().height()
                centered_scroll = max(0, scroll_target_y - viewport_height // 2)
                QTimer.singleShot(10, lambda: v_bar.setValue(int(centered_scroll)))
            elif not self.scroll_to_active_tab:
                # Просто сохраняем текущую позицию скролла (если не ждём активную вкладку)
                QTimer.singleShot(1, lambda: v_bar.setValue(old_scroll))
        else:
            # Обычное обновление - сохраняем позицию
            QTimer.singleShot(1, lambda: v_bar.setValue(old_scroll))

    def enterEvent(self, event):
        # Проверяем, что Chrome на переднем плане
        if not self.is_chrome_in_foreground():
            return
        
        # Устанавливаем флаг для автоскролла к активной вкладке
        self.scroll_to_active_tab = True
        self.force_update = True
        
        self.setGeometry(0, self.offset_y, self.w_open, self.real_height)
        self.anim.stop()
        self.anim.setEndValue(QRect(0, 0, self.w_open, self.real_height))
        self.anim.start()
        
        # Запрашиваем обновление данных для скролла к активной вкладке
        QTimer.singleShot(20, lambda: command_queue.put(json.dumps({"action": "request_update"})))

    def leaveEvent(self, event):
        # Если открыто контекстное меню, ничего не делаем
        if QApplication.activePopupWidget():
            return
            
        self.anim.stop()
        self.anim.setEndValue(QRect(-self.w_open, 0, self.w_open, self.real_height))
        self.anim.start()
        QTimer.singleShot(160, self._check_hide)

    def _check_hide(self):
        # Проверяем: мышь не над окном И не открыто ли меню
        if not self.underMouse() and not QApplication.activePopupWidget():
            self.setGeometry(0, self.offset_y, self.w_closed, self.real_height)
        elif QApplication.activePopupWidget():
            # Если меню открыто, проверяем снова через полсекунды
            QTimer.singleShot(500, self._check_hide)
        else:
            # Меню закрыто, но мышь всё ещё над окном - проверяем снова
            # (на случай если меню было закрыто кликом вне панели)
            QTimer.singleShot(100, self._check_hide)

# --- WebSocket Сервер ---
# Глобальный набор подключенных клиентов
connected_clients = set()

async def ws_handler(websocket):
    addr = websocket.remote_address
    print(f"Bridge connected: {addr}")
    
    # Добавляем клиента в набор при подключении
    connected_clients.add(websocket)
    print(f"Total connected clients: {len(connected_clients)}")
    
    try:
        async for message in websocket:
            data = json.loads(message)
            
            # ИСПРАВЛЕНИЕ: Игнорируем пинги, чтобы список не пропадал
            if data.get('type') == 'ping':
                continue
                
            signals.data_received.emit(data)
    except websockets.exceptions.ConnectionClosed:
        print(f"Bridge disconnected: {addr}")
    except Exception as e:
        print(f"WS Error: {e}")
    finally:
        # Удаляем клиента при отключении
        connected_clients.discard(websocket)
        print(f"Client removed. Total connected: {len(connected_clients)}")

async def send_worker():
    """Отправляет команды из очереди во все подключенные расширения"""
    print("Send worker is ALIVE and running")
    while True:
        try:
            # Небольшая задержка для снижения нагрузки CPU
            await asyncio.sleep(0.01)
            
            # Проверяем thread-safe очередь
            try:
                cmd = command_queue.get_nowait()
                print(f">>> COMMAND RECEIVED FROM Qt THREAD: {cmd}")
            except queue.Empty:
                continue
            
            if not connected_clients:
                print("!!! No extensions connected to send command to")
            else:
                # Отправляем всем подключенным клиентам
                clients = list(connected_clients)
                print(f">>> Sending to {len(clients)} client(s)")
                for client in clients:
                    try:
                        await client.send(cmd)
                        print(f">>> Successfully sent to {client.remote_address}")
                    except Exception as e:
                        print(f">>> Failed to send to client: {e}")
            
        except Exception as e:
            print(f"Worker Error: {e}")
            import traceback
            traceback.print_exc()

async def main_async():
    # Запускаем сервер
    async with websockets.serve(ws_handler, "127.0.0.1", 8765):
        print("WebSocket Server started on ws://127.0.0.1:8765")
        
        # ЗАПУСКАЕМ воркер отправки внутри этого же цикла
        worker_task = asyncio.create_task(send_worker())
        
        # Держим сервер запущенным
        await asyncio.Future() 

def resource_path(relative_path):
    """ Получает абсолютный путь к ресурсам (для PyInstaller) """
    try:
        # PyInstaller создает временную папку _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)

if __name__ == "__main__":
    
    
    # Фикс для иконки в панели задач (Windows)
    # Позволяет Windows объединять окна в одну группу и показывать иконку в таскбаре
    myappid = 'ChromeTabsAlt.1.0' 
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except:
        pass

    app = QApplication(sys.argv)
    
    # Чтобы приложение не закрывалось при закрытии окна
    app.setQuitOnLastWindowClosed(False)

    # Настройка иконки трея через PyQt6
    tray_icon = QSystemTrayIcon()
    icon_path = resource_path("icon128.ico")
    tray_icon.setIcon(QIcon(icon_path))

    # Создаем меню для трея
    tray_menu = QMenu()
    exit_action = tray_menu.addAction("Выход")
    
    # Логика кнопок меню
    exit_action.triggered.connect(app.quit)

    tray_icon.setContextMenu(tray_menu)
    tray_icon.show()

    # Запуск asyncio в отдельном потоке
    threading.Thread(target=lambda: asyncio.run(main_async()), daemon=True).start()

    network_manager = QNetworkAccessManager()
    window = SidebarApp()
    window.show()

    sys.exit(app.exec())