"""Main application window — view registry, navigation, and event handler registration."""

from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QSystemTrayIcon,
)

from decker.client import config as client_config
from decker.client.assets import ICON_PATH
from decker.client.call_bridge import CallBridge
from decker.client.connection_manager import ConnectionManager
from decker.client.context import AppContext
from decker.client.events import (
    ActionFailedEvent,
    ActionListUpdatedEvent,
    AppEvent,
    CardListUpdatedEvent,
    DeviceListUpdatedEvent,
    KeyDisplayStatesUpdatedEvent,
    KeyEvent,
    OpenUrlRequestedEvent,
    PagesUpdatedEvent,
    PluginInstallFinishedEvent,
    PluginLoadFailedEvent,
    PluginUpdatesAvailableEvent,
    PresetListUpdatedEvent,
    ProfilesUpdatedEvent,
    ScreensaverSettingsEvent,
    ScreensaverStateEvent,
    ServerConnectedEvent,
    ServerDisconnectedEvent,
    ViewActivatedEvent,
    ViewDeactivatedEvent,
)
from decker.client.events.event_manager import EventManager
from decker.client.ui_state_store import UiStateStore
from decker.client.update_runner import RESULT_OK, UpdateRunner
from decker.client.view_name import ViewName
from decker.client.views import (
    BaseView,
    DashboardView,
    DeviceView,
    ErrorView,
    ReconnectWaitingOverlay,
)
from decker.client.views.dashboard import DASHBOARD_EVENTS
from decker.client.widgets.about_dialog import AboutDialog, server_version
from decker.client.widgets.plugins_tab import (
    PACKAGE_SUFFIX,
    install_allowed,
    install_package_file,
    refresh_install_allowed,
)
from decker.client.widgets.settings_dialog import SettingsDialog
from decker.common.model import DeviceHardwareInfo

_WINDOW_GEOMETRY_KEY = "window_geometry"

_DRAIN_INTERVAL_MS = 16
_INITIAL_VIEW_RECHECK_MS = 50
_TOAST_DURATION_MS = 4000
_ACTION_TOAST_DURATION_MS = 20000


ACTIVE_VIEW_EVENTS: tuple[type, ...] = (
    DeviceListUpdatedEvent,
    CardListUpdatedEvent,
    ActionListUpdatedEvent,
    PresetListUpdatedEvent,
    KeyDisplayStatesUpdatedEvent,
    KeyEvent,
    PagesUpdatedEvent,
    ProfilesUpdatedEvent,
)


class MainWindow(QMainWindow):  # pylint: disable=too-many-instance-attributes
    """Owns the QStackedWidget of registered views and routes AppEvents from the EventManager."""

    def __init__(self, connection_manager: ConnectionManager, ui_state_store: UiStateStore | None = None) -> None:
        super().__init__()
        self._connection_manager: ConnectionManager = connection_manager
        self._ui_state_store: UiStateStore = ui_state_store or UiStateStore()
        self._geometry_restored: bool = False
        self._quitting: bool = False
        self._views: dict[ViewName, BaseView] = {}
        self._active: ViewName | None = None
        self._last_view: ViewName | None = None
        self._open_url_prompt_active = False

        AppContext.get().window = self
        AppContext.get().connection_manager = connection_manager

        self._stack: QStackedWidget = QStackedWidget()
        self._toast: QLabel = QLabel(self)
        self._toast_action: QPushButton = QPushButton(self)
        self._toast_serial: int = 0
        self._last_updates_key: tuple[Any, ...] | None = None
        self._call_bridge: CallBridge = CallBridge(self)
        self._update_runner: UpdateRunner = UpdateRunner(
            connection_manager.call, on_complete=self._on_update_run_complete, submit=self._call_bridge.submit
        )
        # Created before the window's own handlers so it observes events the window consumes.
        self._settings_dialog: SettingsDialog = SettingsDialog(self)
        self._register_all_views()
        self._build_ui()
        self._register_event_handlers()
        self.setAcceptDrops(True)

        self._drain_timer: QTimer = QTimer(self)
        self._drain_timer.timeout.connect(EventManager.get().drain_and_dispatch)
        self._drain_timer.start(_DRAIN_INTERVAL_MS)

    def register_view(self, name: ViewName, view: BaseView) -> None:
        """Add a view to the stack and the navigation registry."""
        self._stack.addWidget(view)
        self._views[name] = view

    def switch_to(self, name: ViewName) -> None:
        """Deactivate the current view (if any) and activate the named view."""
        if self._active == name:
            return
        if self._active is not None:
            outgoing = self._active
            self._views[outgoing].on_deactivate()
            EventManager.get().emit(ViewDeactivatedEvent(outgoing))
        self._active = name
        self._stack.setCurrentWidget(self._views[name])
        self._views[name].on_activate()
        EventManager.get().emit(ViewActivatedEvent(name))

    def switch_device(self, device: DeviceHardwareInfo) -> None:
        """Set the active device and navigate to the device view."""
        AppContext.get().active_device = device
        self.switch_to(ViewName.DEVICE)

    def open_settings(self) -> None:
        """Show the non-modal settings dialog, or bring it to the front if already open."""
        self._settings_dialog.open_dialog()

    def open_about(self) -> None:
        """Show the modal About dialog, with the server version when connected."""
        AboutDialog(server_version(self._connection_manager), self).exec()

    def set_active_device(self, device: DeviceHardwareInfo) -> None:
        """Set the active device without changing the active view."""
        AppContext.get().active_device = device

    def dispatch(self, event: AppEvent) -> None:
        """Route an event through the EventManager. Kept for backward compatibility."""
        EventManager.get().emit(event)

    def restart(self) -> None:
        """Stop the connection manager, reset shared state, and re-run the startup sequence."""
        self._connection_manager.stop()
        AppContext.reset()
        AppContext.get().window = self
        AppContext.get().connection_manager = self._connection_manager
        self._last_view = None
        self.switch_to(ViewName.CONNECTING)
        self._connection_manager.start()

    def quit_app(self) -> None:
        """Shut down for real: persist state, stop the connection manager and exit the application."""
        self._quitting = True
        self.close()

    def closeEvent(self, a0: Any) -> None:  # pylint: disable=invalid-name
        """Hide to the tray when close-to-tray is on; otherwise persist state, stop and quit."""
        self._save_geometry()
        for view in self._views.values():
            view.save_state()
        if not self._quitting and _should_close_to_tray():
            if a0 is not None:
                a0.ignore()
            self.hide()
            return
        self._settings_dialog.close()
        self._connection_manager.stop()
        super().closeEvent(a0)
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def dragEnterEvent(self, a0: Any) -> None:  # pylint: disable=invalid-name
        """Accept drags that carry local .deckerplugin files, when the server allows installs."""
        if a0 is not None and _dropped_packages(a0.mimeData()) and install_allowed():
            a0.acceptProposedAction()
        elif a0 is not None:
            a0.ignore()

    def dropEvent(self, a0: Any) -> None:  # pylint: disable=invalid-name
        """Install each dropped .deckerplugin file; other files are ignored."""
        if a0 is None:
            return
        packages = _dropped_packages(a0.mimeData())
        if not packages or not install_allowed():
            a0.ignore()
            return
        a0.acceptProposedAction()
        for path in packages:
            install_package_file(path, self)

    def resizeEvent(self, a0: Any) -> None:  # pylint: disable=invalid-name
        """Keep the connecting overlay sized to fill the central widget."""
        super().resizeEvent(a0)
        overlay = self._views.get(ViewName.CONNECTING)
        if overlay is not None and overlay.isVisible():
            overlay.resize(self._stack.size())

    def showEvent(self, a0: Any) -> None:  # pylint: disable=invalid-name
        """Restore window geometry after the first show — required for Wayland compatibility."""
        super().showEvent(a0)
        if not self._geometry_restored:
            self._geometry_restored = True
            self._restore_geometry()

    def check_initial_view(self) -> None:
        """Land on DashboardView if already connected, otherwise wait briefly and recheck.

        Calls show() once the initial view is active, never before — avoids a blank frame.
        """
        if self._connection_manager.is_connected:
            self.switch_to(ViewName.DASHBOARD)
            self.show()
        else:
            QTimer.singleShot(_INITIAL_VIEW_RECHECK_MS, self._apply_initial_view)

    def _apply_initial_view(self) -> None:
        self.switch_to(ViewName.DASHBOARD)
        if not self._connection_manager.is_connected:
            self.switch_to(ViewName.CONNECTING)
        self.show()

    def _save_geometry(self) -> None:
        geometry = self.geometry()
        self._ui_state_store.set(
            _WINDOW_GEOMETRY_KEY,
            {"x": geometry.x(), "y": geometry.y(), "w": geometry.width(), "h": geometry.height()},
        )

    def _restore_geometry(self) -> None:
        saved = self._ui_state_store.get(_WINDOW_GEOMETRY_KEY)
        if saved is None:
            return
        self.setGeometry(saved["x"], saved["y"], saved["w"], saved["h"])

    def _register_event_handlers(self) -> None:
        em = EventManager.get()
        em.subscribe(ServerDisconnectedEvent, self._on_server_disconnected)
        em.subscribe(ServerConnectedEvent, self._on_server_connected)
        em.subscribe(PluginLoadFailedEvent, self._on_plugin_load_failed)
        em.subscribe(ActionFailedEvent, self._on_action_failed)
        em.subscribe(OpenUrlRequestedEvent, self._on_open_url_requested)
        em.subscribe(PluginInstallFinishedEvent, self._on_plugin_install_finished)
        em.subscribe(PluginUpdatesAvailableEvent, self._on_updates_available)
        for event_type in ACTIVE_VIEW_EVENTS:
            em.subscribe(event_type, self._forward_to_active_view)
        for event_type in DASHBOARD_EVENTS:
            em.subscribe(event_type, self._forward_to_dashboard)
        em.subscribe(ScreensaverSettingsEvent, self._forward_to_settings_dialog)
        em.subscribe(ScreensaverStateEvent, self._forward_to_settings_dialog)

    def _forward_to_settings_dialog(self, event: ScreensaverSettingsEvent | ScreensaverStateEvent) -> None:
        self._settings_dialog.handle_screensaver_event(event)

    def _forward_to_dashboard(self, event: Any) -> bool | None:
        return self._views[ViewName.DASHBOARD].handle_event(event)

    def _on_server_disconnected(self, _event: ServerDisconnectedEvent) -> bool:
        self._views[ViewName.DASHBOARD].on_server_disconnected()
        if self._active != ViewName.CONNECTING:
            self._last_view = self._active
        self.switch_to(ViewName.CONNECTING)
        self._update_runner.reset()
        return True

    def _on_server_connected(self, _event: ServerConnectedEvent) -> bool:
        refresh_install_allowed()
        self.switch_to(self._last_view or ViewName.DASHBOARD)
        self._last_view = None
        return True

    def _on_action_failed(self, event: ActionFailedEvent) -> bool:
        self._show_toast(f"Action failed: {event.error}")
        return True

    def _on_plugin_load_failed(self, event: PluginLoadFailedEvent) -> bool:
        self._show_toast(f"Plugin '{event.plugin_uuid}' failed to load: {event.error}")
        return True

    def _on_plugin_install_finished(self, event: PluginInstallFinishedEvent) -> None:
        if event.ok:
            self._show_toast(f"Installed plugin {event.plugin_id} {event.version}")
        else:
            self._show_toast(f"Plugin install failed: {event.message}")
        # After the per-job toast, so a finished run's summary toast replaces it.
        self._update_runner.on_finished(event.job_id, event.ok, event.message)

    def _on_open_url_requested(self, event: OpenUrlRequestedEvent) -> bool:
        if QUrl(event.url).scheme().lower() not in ("http", "https"):
            return True
        if self._open_url_prompt_active:
            return True  # one prompt at a time; a plugin looping open_url must not stack dialogs
        self._open_url_prompt_active = True
        try:
            box = QMessageBox(
                QMessageBox.Icon.Question,
                "Open link",
                f"{event.plugin_id} wants to open {event.url}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                self,
            )
            box.setTextFormat(Qt.TextFormat.PlainText)
            box.setDefaultButton(QMessageBox.StandardButton.No)
            answer = box.exec()
        finally:
            self._open_url_prompt_active = False
        if answer == QMessageBox.StandardButton.Yes.value:
            QDesktopServices.openUrl(QUrl(event.url))
        return True

    def _forward_to_active_view(self, event: Any) -> bool | None:
        if self._active is not None:
            return self._views[self._active].handle_event(event)
        return None

    def _on_updates_available(self, event: PluginUpdatesAvailableEvent) -> None:
        key = (
            tuple((u.get("id"), u.get("available")) for u in event.updates),
            tuple((m.get("id"), m.get("version")) for m in event.missing),
        )
        if key == self._last_updates_key:
            return
        self._last_updates_key = key
        if event.updates:
            ids = [str(u["id"]) for u in event.updates]
            self._show_toast(_count_text(len(ids), "plugin update", "available"), "Update all", ids)
        elif event.missing:
            ids = [str(m["id"]) for m in event.missing]
            self._show_toast(_count_text(len(ids), "missing plugin", "available"), "Install", ids)

    def _run_updates(self, plugin_ids: list[str]) -> None:
        self._hide_toast()
        self._update_runner.run(plugin_ids)

    def _on_update_run_complete(self, results: dict[str, str]) -> None:
        failed = {plugin_id: result for plugin_id, result in results.items() if result != RESULT_OK}
        done = len(results) - len(failed)
        text = _count_text(done, "plugin", "installed or updated")
        if failed:
            first_id, first_error = next(iter(failed.items()))
            text += f", {len(failed)} failed ({first_id}: {first_error})"
        self._show_toast(text)

    def _show_toast(self, message: str, action_text: str = "", plugin_ids: list[str] | None = None) -> None:
        self._toast_serial += 1
        serial = self._toast_serial
        self._toast.setText(message)
        self._toast.adjustSize()
        self._toast.move(12, self.height() - self._toast.height() - 12)
        self._toast.show()
        self._toast_action.hide()
        if action_text and plugin_ids and install_allowed():
            ids = list(plugin_ids)
            self._toast_action.setText(action_text)
            self._toast_action.adjustSize()
            self._toast_action.move(
                self._toast.x() + self._toast.width() + 8,
                self._toast.y() + (self._toast.height() - self._toast_action.height()) // 2,
            )
            try:
                self._toast_action.clicked.disconnect()
            except TypeError:
                pass  # nothing connected yet
            self._toast_action.clicked.connect(lambda: self._run_updates(ids))
            self._toast_action.show()
            self._toast_action.raise_()
        duration = _ACTION_TOAST_DURATION_MS if action_text else _TOAST_DURATION_MS
        QTimer.singleShot(duration, lambda: self._hide_toast(serial))

    def _hide_toast(self, serial: int | None = None) -> None:
        if serial is not None and serial != self._toast_serial:
            return  # a newer toast replaced this one
        self._toast.hide()
        self._toast_action.hide()

    def _register_all_views(self) -> None:
        overlay = ReconnectWaitingOverlay(on_settings=self.open_settings)
        overlay.setParent(self._stack)
        self._views[ViewName.CONNECTING] = overlay
        self.register_view(ViewName.DASHBOARD, DashboardView())
        self.register_view(ViewName.DEVICE, DeviceView())
        self.register_view(ViewName.ERROR, ErrorView())

    def _build_ui(self) -> None:
        self.setWindowTitle("Decker")
        self.resize(320, 240)
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.setCentralWidget(self._stack)
        self._toast.setObjectName("toast")
        self._toast.hide()
        self._toast_action.hide()


def _count_text(count: int, noun: str, suffix: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'} {suffix}"


def _should_close_to_tray() -> bool:
    # Without a system tray there is no way back to a hidden window, so closing must quit.
    return client_config.get_close_to_tray() and QSystemTrayIcon.isSystemTrayAvailable()


def _dropped_packages(mime: Any) -> list[Path]:
    if mime is None or not mime.hasUrls():
        return []
    paths = [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()]
    return [p for p in paths if p.suffix.lower() == PACKAGE_SUFFIX]
