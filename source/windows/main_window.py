import re
import threading
import webbrowser
from enum import Enum
from pathlib import Path
from time import localtime, strftime

from items.base_list_widget_item import BaseListWidgetItem
from modules._platform import get_platform, set_locale
from modules.enums import MessageType
from modules.settings import (get_default_downloads_page,
                              get_default_library_page,
                              get_enable_download_notifications,
                              get_enable_new_builds_notifications,
                              get_launch_minimized_to_tray, get_library_folder,
                              get_taskbar_icon_color, is_library_folder_valid,
                              set_library_folder, taskbar_icon_paths)
from PyQt5.QtCore import QFile, QSize, Qt, QTextStream, pyqtSignal
from PyQt5.QtGui import QFont, QFontDatabase, QIcon
from PyQt5.QtNetwork import QLocalServer
from PyQt5.QtWidgets import (QAction, QFileDialog, QHBoxLayout, QLabel,
                             QMainWindow, QMenu, QPushButton, QSystemTrayIcon,
                             QTabWidget, QVBoxLayout, QWidget)
from threads.library_drawer import LibraryDrawer
from threads.remover import Remover
from threads.scraper import Scraper
from ui.main_window_ui import Ui_MainWindow
from urllib3 import PoolManager
from widgets.base_tool_box_widget import BaseToolBoxWidget
from widgets.download_widget import DownloadState, DownloadWidget
from widgets.library_widget import LibraryWidget

from windows.base_window import BaseWindow
from windows.dialog_window import DialogIcon, DialogWindow
from windows.settings_window import SettingsWindow
from windows.update_window import UpdateWindow


class AppState(Enum):
    IDLE = 1
    CHECKINGBUILDS = 2


class BlenderLauncher(QMainWindow, BaseWindow, Ui_MainWindow):
    show_signal = pyqtSignal()
    close_signal = pyqtSignal()

    def __init__(self, app):
        super().__init__()
        self.setupUi(self)
        self.setAcceptDrops(True)

        # Server
        self.server = QLocalServer()
        self.server.listen("blender-launcher-server")
        self.server.newConnection.connect(self.new_connection)

        # Global scope
        self.app = app
        self.favorite = None
        self.status = "None"
        self.app_state = AppState.IDLE
        self.cashed_builds = []
        self.notification_pool = []
        self.windows = [self]
        self.manager = PoolManager(200)
        self.timer = None
        self.started = True
        self.latest_tag = ""

        # Setup window
        self.setWindowTitle("Blender Launcher")
        self.app.setWindowIcon(
            QIcon(taskbar_icon_paths[get_taskbar_icon_color()]))

        # Setup font
        QFontDatabase.addApplicationFont(
            ":/resources/fonts/OpenSans-SemiBold.ttf")
        self.font = QFont("Open Sans SemiBold", 10)
        self.font.setHintingPreference(QFont.PreferNoHinting)
        self.app.setFont(self.font)

        # Setup style
        file = QFile(":/resources/styles/global.qss")
        file.open(QFile.ReadOnly | QFile.Text)
        self.style_sheet = QTextStream(file).readAll()
        self.app.setStyleSheet(self.style_sheet)

        # Check library folder
        if is_library_folder_valid() is False:
            self.dlg = DialogWindow(
                self, title="Information",
                text="First, choose where Blender<br>builds will be stored",
                accept_text="Continue", cancel_text=None, icon=DialogIcon.INFO)
            self.dlg.accepted.connect(self.set_library_folder)
        else:
            self.draw()

    def set_library_folder(self):
        library_folder = Path.cwd().as_posix()
        new_library_folder = QFileDialog.getExistingDirectory(
            self, "Select Library Folder", library_folder,
            options=QFileDialog.DontUseNativeDialog | QFileDialog.ShowDirsOnly)

        if new_library_folder:
            set_library_folder(new_library_folder)
            self.draw(True)
        else:
            self.app.quit()

    def draw(self, polish=False):
        self.HeaderLayout = QHBoxLayout()
        self.HeaderLayout.setContentsMargins(1, 1, 1, 0)
        self.HeaderLayout.setSpacing(0)
        self.CentralLayout.addLayout(self.HeaderLayout)

        self.SettingsButton = \
            QPushButton(QIcon(":resources/icons/settings.svg"), "")
        self.SettingsButton.setIconSize(QSize(20, 20))
        self.SettingsButton.setFixedSize(36, 32)
        self.WikiButton = \
            QPushButton(QIcon(":resources/icons/wiki.svg"), "")
        self.WikiButton.setIconSize(QSize(20, 20))
        self.WikiButton.setFixedSize(36, 32)
        self.MinimizeButton = \
            QPushButton(QIcon(":resources/icons/minimize.svg"), "")
        self.MinimizeButton.setIconSize(QSize(20, 20))
        self.MinimizeButton.setFixedSize(36, 32)
        self.CloseButton = \
            QPushButton(QIcon(":resources/icons/close.svg"), "")
        self.CloseButton.setIconSize(QSize(20, 20))
        self.CloseButton.setFixedSize(36, 32)
        self.HeaderLabel = QLabel("Blender Launcher")
        self.HeaderLabel.setAlignment(Qt.AlignCenter)

        self.HeaderLayout.addWidget(self.SettingsButton, 0, Qt.AlignLeft)
        self.HeaderLayout.addWidget(self.WikiButton, 0, Qt.AlignLeft)
        self.HeaderLayout.addWidget(self.HeaderLabel, 1)
        self.HeaderLayout.addWidget(self.MinimizeButton, 0, Qt.AlignRight)
        self.HeaderLayout.addWidget(self.CloseButton, 0, Qt.AlignRight)

        self.SettingsButton.setProperty("HeaderButton", True)
        self.WikiButton.setProperty("HeaderButton", True)
        self.MinimizeButton.setProperty("HeaderButton", True)
        self.CloseButton.setProperty("HeaderButton", True)
        self.CloseButton.setProperty("CloseButton", True)

        # Tab layout
        self.TabWidget = QTabWidget()
        self.CentralLayout.addWidget(self.TabWidget)

        self.LibraryTab = QWidget()
        self.LibraryTabLayout = QVBoxLayout()
        self.LibraryTabLayout.setContentsMargins(0, 0, 0, 0)
        self.LibraryTab.setLayout(self.LibraryTabLayout)
        self.TabWidget.addTab(self.LibraryTab, "Library")

        self.DownloadsTab = QWidget()
        self.DownloadsTabLayout = QVBoxLayout()
        self.DownloadsTabLayout.setContentsMargins(0, 0, 0, 0)
        self.DownloadsTab.setLayout(self.DownloadsTabLayout)
        self.TabWidget.addTab(self.DownloadsTab, "Downloads")

        self.LibraryToolBox = BaseToolBoxWidget(self)

        self.LibraryStableListWidget = \
            self.LibraryToolBox.add_list_widget(
                "Stable Releases",
                "LibraryStableListWidget",
                "Nothing to show yet",
                extended_selection=True)
        self.LibraryDailyListWidget = \
            self.LibraryToolBox.add_list_widget(
                "Daily Builds",
                "LibraryDailyListWidget",
                "Nothing to show yet",
                extended_selection=True)
        self.LibraryExperimentalListWidget = \
            self.LibraryToolBox.add_list_widget(
                "Experimental Branches",
                "LibraryExperimentalListWidget",
                "Nothing to show yet",
                extended_selection=True)
        self.LibraryCustomListWidget = \
            self.LibraryToolBox.add_list_widget(
                "Custom Builds",
                "LibraryCustomListWidget",
                "Nothing to show yet",
                show_reload=True,
                extended_selection=True)
        self.LibraryTab.layout().addWidget(self.LibraryToolBox)

        self.DownloadsToolBox = BaseToolBoxWidget(self)

        self.DownloadsStableListWidget = \
            self.DownloadsToolBox.add_list_widget(
                "Stable Releases",
                "DownloadsStableListWidget",
                "No new builds available",
                False)
        self.DownloadsDailyListWidget = \
            self.DownloadsToolBox.add_list_widget(
                "Daily Builds",
                "DownloadsDailyListWidget",
                "No new builds available")
        self.DownloadsExperimentalListWidget = \
            self.DownloadsToolBox.add_list_widget(
                "Experimental Branches",
                "DownloadsExperimentalListWidget",
                "No new builds available")
        self.DownloadsTab.layout().addWidget(self.DownloadsToolBox)

        self.LibraryToolBox.setCurrentIndex(get_default_library_page())
        self.DownloadsToolBox.setCurrentIndex(get_default_downloads_page())

        # Connect buttons
        self.SettingsButton.clicked.connect(self.show_settings_window)
        self.WikiButton.clicked.connect(lambda: webbrowser.open(
            "https://github.com/DotBow/Blender-Launcher/wiki"))
        self.MinimizeButton.clicked.connect(self.showMinimized)
        self.CloseButton.clicked.connect(self.close)

        self.StatusBar.setContentsMargins(0, 0, 0, 2)
        self.StatusBar.setFont(self.font)
        self.statusbarLabel = QLabel()
        self.statusbarLabel.setIndent(8)
        self.NewVersionButton = QPushButton()
        self.NewVersionButton.hide()
        self.NewVersionButton.clicked.connect(self.show_update_window)
        self.statusbarVersion = QLabel(self.app.applicationVersion())
        self.StatusBar.addPermanentWidget(self.statusbarLabel, 1)
        self.StatusBar.addPermanentWidget(self.NewVersionButton)
        self.StatusBar.addPermanentWidget(self.statusbarVersion)

        # Draw library
        self.draw_library()

        # Setup tray icon context Menu
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit)
        hide_action = QAction("Hide", self)
        hide_action.triggered.connect(self.close)
        show_action = QAction("Show", self)
        show_action.triggered.connect(self._show)
        launch_favorite_action = QAction(
            QIcon(":resources/icons/favorite.svg"), "Blender", self)
        launch_favorite_action.triggered.connect(self.launch_favorite)

        tray_menu = QMenu()
        tray_menu.setFont(self.font)
        tray_menu.addAction(launch_favorite_action)
        tray_menu.addAction(show_action)
        tray_menu.addAction(hide_action)
        tray_menu.addAction(quit_action)

        # Setup tray icon
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(
            QIcon(taskbar_icon_paths[get_taskbar_icon_color()]))
        self.tray_icon.setToolTip("Blender Launcher")
        self.tray_icon.activated.connect(self.tray_icon_activated)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.messageClicked.connect(self._show)
        self.tray_icon.show()

        # Forse style update
        if polish is True:
            self.style().unpolish(self.app)
            self.style().polish(self.app)

        # Show window
        if get_launch_minimized_to_tray() is False:
            self._show()

    def show_update_window(self):
        download_widgets = []

        download_widgets.extend(self.DownloadsStableListWidget.items())
        download_widgets.extend(self.DownloadsDailyListWidget.items())
        download_widgets.extend(self.DownloadsExperimentalListWidget.items())

        for widget in download_widgets:
            if widget.state == DownloadState.DOWNLOADING:
                self.dlg = DialogWindow(
                    self, title="Warning",
                    text="In order to update Blender Launcher<br> \
                    complete all active downloads!",
                    accept_text="OK", cancel_text=None,
                    icon=DialogIcon.WARNING)

                return

        self.tray_icon.hide()
        self.close()
        self.update_window = UpdateWindow(self, self.latest_tag)

    def _show(self):
        platform = get_platform()

        if platform == "Windows":
            self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
            self.show()
            self.setWindowFlags(self.windowFlags() & ~Qt.WindowStaysOnTopHint)
            self.show()
        elif platform == "Linux":
            self.show()
            self.activateWindow()

        self.set_status()
        self.show_signal.emit()

    def show_message(self, message, value=None, type=None):
        if (type == MessageType.DOWNLOADFINISHED and
                get_enable_download_notifications() is False):
            return
        elif (type == MessageType.NEWBUILDS and
              get_enable_new_builds_notifications() is False):
            return

        if value not in self.notification_pool:
            if value is not None:
                self.notification_pool.append(value)
            self.tray_icon.showMessage(
                "Blender Launcher", message,
                QIcon(taskbar_icon_paths[get_taskbar_icon_color()]),
                10000)

    def launch_favorite(self):
        try:
            self.favorite.launch()
        except Exception:
            self.dlg = DialogWindow(
                self, text="Favorite build not found!",
                accept_text="OK", cancel_text=None)

    def tray_icon_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self._show()
        elif reason == QSystemTrayIcon.MiddleClick:
            self.launch_favorite()

    def quit(self):
        download_widgets = []

        download_widgets.extend(self.DownloadsStableListWidget.items())
        download_widgets.extend(self.DownloadsDailyListWidget.items())
        download_widgets.extend(self.DownloadsExperimentalListWidget.items())

        for widget in download_widgets:
            if widget.state == DownloadState.DOWNLOADING:
                self.dlg = DialogWindow(
                    self, title="Warning", text="Active downloads in progress!<br>\
                    Are you sure you want to quit?",
                    accept_text="Yes", cancel_text="No",
                    icon=DialogIcon.WARNING)

                self.dlg.accepted.connect(self.destroy)
                return

        self.destroy()

    def destroy(self):
        if self.timer is not None:
            self.timer.cancel()

        self.tray_icon.hide()
        self.app.quit()

    def draw_library(self, clear=False):
        self.set_status("Reading local builds")

        if clear:
            self.timer.cancel()
            self.scraper.quit()
            self.DownloadsStableListWidget.clear()
            self.DownloadsDailyListWidget.clear()
            self.DownloadsExperimentalListWidget.clear()
            self.started = True

        self.favorite = None

        self.LibraryStableListWidget.clear()
        self.LibraryDailyListWidget.clear()
        self.LibraryExperimentalListWidget.clear()
        self.LibraryCustomListWidget.clear()

        self.library_drawer = LibraryDrawer()
        self.library_drawer.build_found.connect(self.draw_to_library)
        self.library_drawer.finished.connect(self.draw_downloads)
        self.library_drawer.start()

    def reload_custom_builds(self):
        self.LibraryCustomListWidget.clear()
        self.library_drawer = LibraryDrawer()
        self.library_drawer.build_found.connect(self.draw_to_library)
        self.library_drawer.start()

    def draw_downloads(self):
        for page in self.DownloadsToolBox.pages:
            page.set_info_label_text("Checking for new builds")

        self.app_state = AppState.CHECKINGBUILDS
        self.set_status("Checking for new builds")
        self.scraper = Scraper(self, self.manager)
        self.scraper.links.connect(self.draw_new_builds)
        self.scraper.new_bl_version.connect(self.set_version)
        self.scraper.error.connect(self.connection_error)
        self.scraper.start()

    def connection_error(self):
        set_locale()
        utcnow = strftime(('%H:%M'), localtime())
        self.set_status("Connection Error at " + utcnow)
        self.app_state = AppState.IDLE

        self.timer = threading.Timer(600.0, self.draw_downloads)
        self.timer.start()

    def draw_new_builds(self, builds):
        self.cashed_builds.clear()
        self.cashed_builds.extend(builds)

        library_widgets = []
        download_widgets = []

        library_widgets.extend(self.LibraryStableListWidget.items())
        library_widgets.extend(self.LibraryDailyListWidget.items())
        library_widgets.extend(self.LibraryExperimentalListWidget.items())

        download_widgets.extend(self.DownloadsStableListWidget.items())
        download_widgets.extend(self.DownloadsDailyListWidget.items())
        download_widgets.extend(self.DownloadsExperimentalListWidget.items())

        for widget in download_widgets:
            if widget.build_info in builds:
                builds.remove(widget.build_info)
            elif widget.state != DownloadState.DOWNLOADING:
                widget.destroy()

        for widget in library_widgets:
            if widget.build_info in builds:
                builds.remove(widget.build_info)

        for build_info in builds:
            self.draw_to_downloads(build_info, not self.started)

        if (len(builds) > 0) and (not self.started):
            self.show_message(
                "New builds of Blender is available!",
                type=MessageType.NEWBUILDS)

        set_locale()
        utcnow = strftime(('%H:%M'), localtime())
        self.set_status("Last check at " + utcnow)
        self.app_state = AppState.IDLE

        for page in self.DownloadsToolBox.pages:
            page.set_info_label_text("No new builds available")

        self.timer = threading.Timer(600.0, self.draw_downloads)
        self.timer.start()
        self.started = False

    def draw_from_cashed(self, build_info):
        if self.app_state == AppState.IDLE:
            if build_info in self.cashed_builds:
                i = self.cashed_builds.index(build_info)
                self.draw_to_downloads(self.cashed_builds[i])

    def draw_to_downloads(self, build_info, show_new=False):
        branch = build_info.branch

        if branch == 'stable':
            list_widget = self.DownloadsStableListWidget
        elif branch == 'daily':
            list_widget = self.DownloadsDailyListWidget
        else:
            list_widget = self.DownloadsExperimentalListWidget

        item = BaseListWidgetItem(build_info.commit_time)
        widget = DownloadWidget(self, list_widget, item, build_info, show_new)
        list_widget.add_item(item, widget)

    def draw_to_library(self, path, show_new=False):
        category = Path(path).parent.name

        if category == 'stable':
            list_widget = self.LibraryStableListWidget
        elif category == 'daily':
            list_widget = self.LibraryDailyListWidget
        elif category == 'experimental':
            list_widget = self.LibraryExperimentalListWidget
        elif category == 'custom':
            list_widget = self.LibraryCustomListWidget
        else:
            return

        item = BaseListWidgetItem()
        widget = LibraryWidget(self, item, path, list_widget, show_new)
        list_widget.insert_item(item, widget)

    def set_status(self, status=None):
        if status is not None:
            self.status = status

        self.statusbarLabel.setText("Status: {0}".format(self.status))

    def set_version(self, latest_tag):
        current_tag = self.app.applicationVersion()
        latest_ver = re.sub(r'\D', '', latest_tag)
        current_ver = re.sub(r'\D', '', current_tag)

        if int(latest_ver) > int(current_ver):
            if latest_tag not in self.notification_pool:
                self.NewVersionButton.setText(
                    "Update to version {0}".
                    format(latest_tag.replace('v', '')))
                self.NewVersionButton.show()
                self.show_message(
                    "New version of Blender Launcher is available!",
                    latest_tag)

            self.latest_tag = latest_tag

    def show_settings_window(self):
        self.settings_window = SettingsWindow(self)

    def clear_temp(self):
        temp_folder = Path(get_library_folder()) / ".temp"
        self.remover = Remover(temp_folder)
        self.remover.start()

    def closeEvent(self, event):
        event.ignore()
        self.hide()
        self.close_signal.emit()

    def new_connection(self):
        self._show()

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat('text/plain'):
            e.accept()
        else:
            e.ignore()

    def dropEvent(self, e):
        print(e.mimeData().text())
