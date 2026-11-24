"""Non-modal settings dialog with General, Server, Plugins and Device tabs, applied with an Apply button."""

from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from decker.client import config as client_config
from decker.client.assets import read_custom_stylesheet, resolve_theme
from decker.client.context import AppContext
from decker.client.events import ScreensaverSettingsEvent, ScreensaverStateEvent
from decker.client.server_settings import ServerSettingsInfo
from decker.client.themes import ThemeColors, apply_theme
from decker.client.widgets.plugins_tab import PluginsTab
from decker.client.widgets.screensaver_panel import ScreensaverPanel

_STATUS_REFRESH_MS = 1000
_RESTART_REVERT_MS = 30_000
_MIB = 1024 * 1024
_NOT_ALLOWED_TIP = (
    "The server does not allow clients to change its settings. Start it with --allow-client-reconfiguration."
)
_NOT_CONNECTED_TIP = "Connect to the server to change its settings."
_THEME_LABELS: dict[str, str] = {
    client_config.THEME_DARK: "Dark",
    client_config.THEME_LIGHT: "Light",
    client_config.THEME_CUSTOM: "Custom…",
}
_STATE_LABELS: dict[str, str] = {
    "disconnected": "Disconnected",
    "initializing": "Connected, loading…",
    "unhealthy": "Connected, server unhealthy",
    "healthy": "Connected",
}


class SettingsDialog(QDialog):  # pylint: disable=too-many-instance-attributes
    """Client settings. Edits are staged until Apply; Close or Esc discards unapplied edits.

    While open, the connection status and the failed-plugin list update live.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(False)

        self._screensaver_listeners: list[Callable[[ScreensaverSettingsEvent | ScreensaverStateEvent], None]] = []
        self._host_input: QLineEdit = QLineEdit()
        self._port_input: QLineEdit = QLineEdit()
        self._status_label: QLabel = QLabel()
        self._memory_spin: QSpinBox = QSpinBox()
        self._image_spin: QSpinBox = QSpinBox()
        self._stop_check: QCheckBox = QCheckBox("Stop the server when the last client disconnects")
        self._apply_error_label: QLabel = QLabel()
        self._loaded_server_values: tuple[int, int, bool] = (0, 0, False)
        self._revert_port: int | None = None
        self._restart_timer: QTimer = QTimer(self)
        self._restart_timer.setSingleShot(True)
        self._restart_timer.timeout.connect(self._check_restart_result)
        self._theme_combo: QComboBox = QComboBox()
        self._custom_path_input: QLineEdit = QLineEdit()
        self._browse_btn: QPushButton = QPushButton("Browse…")
        self._plugins_tab: PluginsTab = PluginsTab()
        self._screensaver_panel: ScreensaverPanel = ScreensaverPanel()
        self._message_label: QLabel = QLabel()
        self._buttons: QDialogButtonBox = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Close
        )

        self._close_to_tray_check: QCheckBox = QCheckBox("Close to tray")

        self._tabs: QTabWidget = QTabWidget()
        self._general_tab = self._build_general_tab()
        self._server_tab = self._build_server_tab()
        self._tabs.addTab(self._general_tab, "General")
        self._tabs.addTab(self._server_tab, "Server")
        self._tabs.addTab(self._plugins_tab, "Plugins")
        self._tabs.addTab(self._screensaver_panel, "Device")
        self.add_screensaver_listener(self._screensaver_panel.handle_event)

        self._buttons.rejected.connect(self.reject)
        apply_btn = self._buttons.button(QDialogButtonBox.StandardButton.Apply)
        if apply_btn is not None:
            apply_btn.clicked.connect(self.apply)

        bottom_row = QHBoxLayout()
        bottom_row.addWidget(self._message_label, 1)
        bottom_row.addWidget(self._buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(self._tabs, 1)
        layout.addLayout(bottom_row)
        self.resize(760, 640)

        self._status_timer: QTimer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_status)

    def add_screensaver_listener(
        self, listener: Callable[[ScreensaverSettingsEvent | ScreensaverStateEvent], None]
    ) -> None:
        """Register a callback for screensaver events, delivered whether or not the dialog is visible."""
        self._screensaver_listeners.append(listener)

    def handle_screensaver_event(self, event: ScreensaverSettingsEvent | ScreensaverStateEvent) -> None:
        """Deliver a screensaver event to the registered listeners."""
        for listener in list(self._screensaver_listeners):
            listener(event)

    def open_dialog(self) -> None:
        """Load current settings into the form and show the dialog, or raise it if already open."""
        if not self.isVisible():
            self._load_from_config()
            self._plugins_tab.refresh()
        self.show()
        self.raise_()
        self.activateWindow()

    def apply(self) -> bool:
        """Validate all tabs, then save and apply them. Returns False and saves nothing on error."""
        host = self._host_input.text().strip()
        port = _parse_port(self._port_input.text())
        if not host or port is None:
            return self._fail(self._server_tab, "Enter a host and a port between 1 and 65535.")
        theme = self._selected_theme()
        custom_path = self._custom_path_input.text().strip()
        if theme == client_config.THEME_CUSTOM and not custom_path:
            return self._fail(self._general_tab, "Choose a stylesheet file for the custom theme.")
        colors: ThemeColors | None = None
        if theme == client_config.THEME_CUSTOM:
            stylesheet = read_custom_stylesheet(custom_path)
            if stylesheet is None:
                return self._fail(self._general_tab, f"Cannot read stylesheet: {custom_path}")
        else:
            stylesheet, colors = resolve_theme(theme)

        if not self._apply_server_settings(port, port != client_config.get_server_port()):
            return False
        self._apply_server(host, port)
        client_config.set_close_to_tray(self._close_to_tray_check.isChecked())
        client_config.set_theme(theme, custom_path)
        app = QApplication.instance()
        if isinstance(app, QApplication):
            apply_theme(app, stylesheet, colors)
        self._show_message("Applied.", "success")
        return True

    def set_failed_plugins(self, failures: dict[str, str]) -> None:
        """Replace the failed-plugin list (plugin UUID → error message)."""
        self._plugins_tab.set_failed_plugins(failures)

    def showEvent(self, a0: Any) -> None:  # pylint: disable=invalid-name
        """Start live status updates while visible."""
        super().showEvent(a0)
        self._refresh_status()
        self._status_timer.start(_STATUS_REFRESH_MS)

    def hideEvent(self, a0: Any) -> None:  # pylint: disable=invalid-name
        """Stop live status updates while hidden."""
        self._status_timer.stop()
        super().hideEvent(a0)

    def _build_general_tab(self) -> QWidget:
        self._close_to_tray_check.setToolTip(
            "Closing the window keeps Decker running in the system tray. Use Quit in the tray menu to exit."
        )
        hint = QLabel("When off, closing the window quits Decker.")
        hint.setProperty("role", "muted")
        hint.setWordWrap(True)
        for theme, label in _THEME_LABELS.items():
            self._theme_combo.addItem(label, theme)
        self._theme_combo.currentIndexChanged.connect(self._update_custom_enabled)
        self._custom_path_input.setPlaceholderText("Path to a .qss file")
        self._browse_btn.clicked.connect(self._browse_stylesheet)

        path_row = QHBoxLayout()
        path_row.addWidget(self._custom_path_input, 1)
        path_row.addWidget(self._browse_btn)

        tab = QWidget()
        form = QFormLayout(tab)
        form.addRow("", self._close_to_tray_check)
        form.addRow("", hint)
        form.addRow("Theme", self._theme_combo)
        form.addRow("Stylesheet", path_row)
        return tab

    def _build_server_tab(self) -> QWidget:
        self._memory_spin.setRange(128, 1024 * 1024)
        self._memory_spin.setSuffix(" MiB")
        self._image_spin.setRange(64, 64 * 1024)
        self._image_spin.setSuffix(" KiB")
        self._apply_error_label.setProperty("role", "error")
        self._apply_error_label.setWordWrap(True)
        self._apply_error_label.hide()
        tab = QWidget()
        form = QFormLayout(tab)
        form.addRow("Host", self._host_input)
        form.addRow("Port", self._port_input)
        form.addRow("Memory limit", self._memory_spin)
        form.addRow("Max image file size", self._image_spin)
        form.addRow("", self._stop_check)
        form.addRow("Status", self._status_label)
        form.addRow("", self._apply_error_label)
        return tab

    def _load_from_config(self) -> None:
        self._close_to_tray_check.setChecked(client_config.get_close_to_tray())
        self._host_input.setText(client_config.get_server_host())
        self._port_input.setText(str(client_config.get_server_port()))
        index = self._theme_combo.findData(client_config.get_theme())
        self._theme_combo.setCurrentIndex(max(index, 0))
        self._custom_path_input.setText(client_config.get_custom_style_sheet())
        self._update_custom_enabled()
        self._load_server_settings()
        self._message_label.clear()

    def _server_info(self) -> ServerSettingsInfo | None:
        connection_manager = AppContext.get().connection_manager
        info = getattr(connection_manager, "server_settings", None)
        return info if isinstance(info, ServerSettingsInfo) else None

    def _load_server_settings(self) -> None:
        connection_manager = AppContext.get().connection_manager
        if connection_manager is not None and connection_manager.is_connected:
            connection_manager.refresh_server_settings()
        info = self._server_info()
        if info is not None:
            self._memory_spin.setValue(info.memory_limit // _MIB)
            self._image_spin.setValue(info.icon_max_file_size // 1024)
            self._stop_check.setChecked(info.stop_when_last_client_leaves)
        self._loaded_server_values = (self._memory_spin.value(), self._image_spin.value(), self._stop_check.isChecked())
        allowed = info is not None and info.reconfigurable
        tip = "" if allowed else (_NOT_CONNECTED_TIP if info is None else _NOT_ALLOWED_TIP)
        for widget in (self._memory_spin, self._image_spin, self._stop_check):
            widget.setEnabled(allowed)
            widget.setToolTip(tip)
        error = info.last_apply_error if info is not None else None
        self._apply_error_label.setText(error or "")
        self._apply_error_label.setVisible(bool(error))

    def _apply_server_settings(self, port: int, port_changed: bool) -> bool:
        info = self._server_info()
        connection_manager = AppContext.get().connection_manager
        if info is None or not info.reconfigurable or connection_manager is None:
            return True
        values = self._changed_server_values(port, port_changed)
        if not values:
            return True
        accepted, error = connection_manager.set_server_settings(values)
        if not accepted:
            return self._fail(self._server_tab, error or "The server refused the change.")
        if "port" in values:
            self._revert_port = client_config.get_server_port()
            self._restart_timer.start(_RESTART_REVERT_MS)
        return True

    def _changed_server_values(self, port: int, port_changed: bool) -> dict[str, Any]:
        memory, image, stop = self._loaded_server_values
        values: dict[str, Any] = {}
        if port_changed:
            values["port"] = port
        if self._memory_spin.value() != memory:
            values["memory_limit"] = self._memory_spin.value() * _MIB
        if self._image_spin.value() != image:
            values["icon_max_file_size"] = self._image_spin.value() * 1024
        if self._stop_check.isChecked() != stop:
            values["stop_when_last_client_leaves"] = self._stop_check.isChecked()
        return values

    def _check_restart_result(self) -> None:
        previous, self._revert_port = self._revert_port, None
        connection_manager = AppContext.get().connection_manager
        if previous is None or connection_manager is None or connection_manager.is_connected:
            return
        failed = client_config.get_server_port()
        client_config.set_server(client_config.get_server_host(), previous)
        connection_manager.request_reconnect()
        self._show_message(f"The server did not come back on port {failed}; restored port {previous}.", "error")

    def _apply_server(self, host: str, port: int) -> None:
        if host == client_config.get_server_host() and port == client_config.get_server_port():
            return
        client_config.set_server(host, port)
        connection_manager = AppContext.get().connection_manager
        if connection_manager is not None:
            connection_manager.request_reconnect()

    def _selected_theme(self) -> str:
        return str(self._theme_combo.currentData())

    def _update_custom_enabled(self) -> None:
        custom = self._selected_theme() == client_config.THEME_CUSTOM
        self._custom_path_input.setEnabled(custom)
        self._browse_btn.setEnabled(custom)

    def _browse_stylesheet(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose stylesheet", "", "Qt stylesheets (*.qss);;All files (*)")
        if path:
            self._custom_path_input.setText(path)

    def _refresh_status(self) -> None:
        connection_manager = AppContext.get().connection_manager
        state = str(connection_manager.state) if connection_manager is not None else "disconnected"
        self._status_label.setText(_STATE_LABELS.get(state, state))

    def _fail(self, tab: QWidget, message: str) -> bool:
        self._tabs.setCurrentWidget(tab)
        self._show_message(message, "error")
        return False

    def _show_message(self, message: str, role: str) -> None:
        self._message_label.setProperty("role", role)
        self._message_label.setText(message)
        style = self._message_label.style()
        if style is not None:
            style.unpolish(self._message_label)
            style.polish(self._message_label)


def _parse_port(text: str) -> int | None:
    try:
        port = int(text.strip())
    except ValueError:
        return None
    return port if 0 < port < 65536 else None
