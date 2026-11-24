"""System tray icon and menu for the Decker client."""

from typing import TYPE_CHECKING

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from decker.client.assets import ICON_PATH

if TYPE_CHECKING:
    from decker.client.window import MainWindow


class TrayIcon(QSystemTrayIcon):
    """Tray icon mirroring the app icon, with Show/Hide, Settings and Quit."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(QIcon(str(ICON_PATH)) if ICON_PATH.exists() else QIcon())
        self._window = window
        self.setToolTip("Decker")

        self._menu = QMenu()
        toggle_action = self._menu.addAction("Hide", self._toggle_window_visibility)
        assert toggle_action is not None
        self._toggle_action = toggle_action
        self._menu.addAction("Settings", self._open_settings)
        self._menu.addSeparator()
        self._menu.addAction("About Decker…", self._open_about)
        self._menu.addAction("Quit", self._quit)
        self.setContextMenu(self._menu)

        self._menu.aboutToShow.connect(self._update_toggle_label)
        self.activated.connect(self._on_activated)

    def _update_toggle_label(self) -> None:
        self._toggle_action.setText("Hide" if self._window.isVisible() else "Show")

    def _toggle_window_visibility(self) -> None:
        if self._window.isVisible():
            self._window.hide()
        else:
            self._window.show()
            self._window.raise_()
            self._window.activateWindow()

    def _open_settings(self) -> None:
        self._window.open_settings()

    def _open_about(self) -> None:
        self._window.open_about()

    def _quit(self) -> None:
        self._window.quit_app()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_window_visibility()
