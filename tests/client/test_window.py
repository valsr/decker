"""Tests for decker.client.window.MainWindow."""

import threading
from unittest.mock import MagicMock, patch

import pytest
from assertpy import assert_that
from PyQt6.QtCore import QMimeData, Qt, QUrl
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QMessageBox

from decker.client.context import AppContext
from decker.client.events import (
    ActionFailedEvent,
    AppRulesUpdatedEvent,
    DetectionStatusEvent,
    DeviceListUpdatedEvent,
    OpenUrlRequestedEvent,
    PagesUpdatedEvent,
    PluginInstallFinishedEvent,
    PluginLoadFailedEvent,
    PluginUpdatesAvailableEvent,
    ProfilesUpdatedEvent,
    ServerConnectedEvent,
    ServerDisconnectedEvent,
    WindowFocusedEvent,
    WindowPickedEvent,
)
from decker.client.events.event_manager import EventManager
from decker.client.ui_state_store import UiStateStore
from decker.client.view_name import ViewName
from decker.client.views.dashboard import DASHBOARD_EVENTS
from decker.client.widgets import plugins_tab
from decker.client.widgets.plugins_tab import refresh_install_allowed
from decker.client.window import ACTIVE_VIEW_EVENTS, MainWindow, _should_close_to_tray
from decker.common.model import (
    AppRule,
    DefaultRule,
    DeviceHardwareInfo,
    DevicePages,
    DeviceProfiles,
    DeviceRules,
    MatchCondition,
    PageInfo,
    ProfileInfo,
    WindowInfo,
)


@pytest.fixture
def connection_manager() -> MagicMock:
    manager = MagicMock()
    manager.is_connected = False
    return manager


@pytest.fixture
def window(qapp, connection_manager, tmp_path):  # pylint: disable=unused-argument,redefined-outer-name
    AppContext._instance = None  # pylint: disable=protected-access
    EventManager.reset()
    win = MainWindow(connection_manager, ui_state_store=UiStateStore(tmp_path / "config.db"))
    for view in win._views.values():  # pylint: disable=protected-access
        view.on_activate = MagicMock()  # type: ignore[method-assign]
        view.on_deactivate = MagicMock()  # type: ignore[method-assign]
        view.handle_event = MagicMock()  # type: ignore[method-assign]
        view.save_state = MagicMock()  # type: ignore[method-assign]
    return win


class TestRegisterView:
    def test_all_views_registered(self, window):  # pylint: disable=redefined-outer-name
        assert_that(set(window._views.keys())).is_equal_to(set(ViewName))  # pylint: disable=protected-access


class TestSwitchTo:
    def test_activates_target_and_sets_active(self, window):  # pylint: disable=redefined-outer-name
        window.switch_to(ViewName.DASHBOARD)

        window._views[ViewName.DASHBOARD].on_activate.assert_called_once()  # pylint: disable=protected-access
        assert_that(window._active).is_equal_to(ViewName.DASHBOARD)  # pylint: disable=protected-access

    def test_deactivates_outgoing_before_activating_incoming(self, window):  # pylint: disable=redefined-outer-name
        calls = []
        window._views[ViewName.CONNECTING].on_deactivate.side_effect = lambda: calls.append(
            "deactivate"
        )  # pylint: disable=protected-access
        window._views[ViewName.DASHBOARD].on_activate.side_effect = lambda: calls.append(
            "activate"
        )  # pylint: disable=protected-access

        window.switch_to(ViewName.CONNECTING)
        window.switch_to(ViewName.DASHBOARD)

        assert_that(calls).is_equal_to(["deactivate", "activate"])

    def test_is_noop_when_target_already_active(self, window):  # pylint: disable=redefined-outer-name
        window.switch_to(ViewName.DASHBOARD)
        window._views[ViewName.DASHBOARD].on_activate.reset_mock()  # pylint: disable=protected-access

        window.switch_to(ViewName.DASHBOARD)

        window._views[ViewName.DASHBOARD].on_activate.assert_not_called()  # pylint: disable=protected-access


class TestSwitchDevice:
    def test_sets_active_device_and_navigates_to_device_view(self, window):  # pylint: disable=redefined-outer-name
        window.switch_device("abc123")

        assert_that(AppContext.get().active_device).is_equal_to("abc123")
        assert_that(window._active).is_equal_to(ViewName.DEVICE)  # pylint: disable=protected-access


class TestDispatch:
    def test_server_disconnected_saves_last_view_and_switches_to_connecting(
        self, window
    ):  # pylint: disable=redefined-outer-name
        window.switch_to(ViewName.DASHBOARD)

        window.dispatch(ServerDisconnectedEvent(reason="lost"))

        assert_that(window._active).is_equal_to(ViewName.CONNECTING)  # pylint: disable=protected-access
        assert_that(window._last_view).is_equal_to(ViewName.DASHBOARD)  # pylint: disable=protected-access

    def test_server_disconnected_does_not_overwrite_last_view_when_already_connecting(
        self, window  # pylint: disable=redefined-outer-name
    ):
        window.switch_to(ViewName.DASHBOARD)
        window.dispatch(ServerDisconnectedEvent(reason="lost"))
        window.dispatch(ServerDisconnectedEvent(reason="lost again"))

        assert_that(window._last_view).is_equal_to(ViewName.DASHBOARD)  # pylint: disable=protected-access

    def test_server_connected_restores_last_view_and_clears_it(self, window):  # pylint: disable=redefined-outer-name
        window.switch_to(ViewName.DASHBOARD)
        window.dispatch(ServerDisconnectedEvent(reason="lost"))

        window.dispatch(ServerConnectedEvent(host="localhost", port=18861))

        assert_that(window._active).is_equal_to(ViewName.DASHBOARD)  # pylint: disable=protected-access
        assert_that(window._last_view).is_none()  # pylint: disable=protected-access

    def test_server_connected_falls_back_to_dashboard_with_no_last_view(
        self, window
    ):  # pylint: disable=redefined-outer-name
        window.dispatch(ServerConnectedEvent(host="localhost", port=18861))

        assert_that(window._active).is_equal_to(ViewName.DASHBOARD)  # pylint: disable=protected-access

    def test_action_failed_shows_a_toast(self, window):  # pylint: disable=redefined-outer-name
        window.switch_to(ViewName.DASHBOARD)

        window.dispatch(ActionFailedEvent(action_id="com.t.http", error="HTTP 500"))

        assert_that(window._toast.isHidden()).is_false()  # pylint: disable=protected-access
        assert_that(window._toast.text()).contains("Action failed").contains(
            "HTTP 500"
        )  # pylint: disable=protected-access

    def test_plugin_load_failed_shows_toast_and_does_not_forward_to_view(
        self, window
    ):  # pylint: disable=redefined-outer-name
        window.switch_to(ViewName.DASHBOARD)

        window.dispatch(PluginLoadFailedEvent(plugin_uuid="com.decker.broken", error="boom"))

        assert_that(window._toast.isHidden()).is_false()  # pylint: disable=protected-access
        assert_that(window._toast.text()).contains("com.decker.broken").contains(
            "boom"
        )  # pylint: disable=protected-access
        window._views[ViewName.DASHBOARD].handle_event.assert_not_called()  # pylint: disable=protected-access

    def test_open_url_opens_only_after_confirmation(self, window):  # pylint: disable=redefined-outer-name
        event = OpenUrlRequestedEvent(plugin_id="com.decker.x", url="https://example.com")
        seen: dict = {}
        answers = [QMessageBox.StandardButton.No.value, QMessageBox.StandardButton.Yes.value]

        def fake_exec(box):
            seen.update(
                text=box.text(),
                text_format=box.textFormat(),
                default=box.defaultButton().text().replace("&", ""),
            )
            return answers.pop(0)

        with (
            patch("decker.client.window.QDesktopServices.openUrl") as open_url,
            patch.object(QMessageBox, "exec", fake_exec),
        ):
            window.dispatch(event)
            assert_that(seen["text"]).is_equal_to("com.decker.x wants to open https://example.com")
            assert_that(seen["text_format"]).is_equal_to(Qt.TextFormat.PlainText)
            assert_that(seen["default"]).is_equal_to("No")
            open_url.assert_not_called()
            window.dispatch(event)
            open_url.assert_called_once()
            assert_that(open_url.call_args.args[0].toString()).is_equal_to("https://example.com")

    def test_open_url_prompts_do_not_stack(self, window):  # pylint: disable=redefined-outer-name
        event = OpenUrlRequestedEvent(plugin_id="com.decker.x", url="https://example.com")
        calls: list[int] = []

        def fake_exec(_box):
            calls.append(1)
            window.dispatch(event)
            window.dispatch(event)
            return QMessageBox.StandardButton.No.value

        with patch.object(QMessageBox, "exec", fake_exec):
            window.dispatch(event)

        assert_that(calls).is_length(1)

    def test_open_url_rejects_file_scheme(self, window):  # pylint: disable=redefined-outer-name
        with (
            patch("decker.client.window.QDesktopServices.openUrl") as open_url,
            patch.object(QMessageBox, "exec") as ask,
        ):
            window.dispatch(OpenUrlRequestedEvent(plugin_id="com.decker.x", url="file:///etc/passwd"))
            ask.assert_not_called()
            open_url.assert_not_called()

    def test_unknown_event_forwarded_to_active_view(self, window):  # pylint: disable=redefined-outer-name
        window.switch_to(ViewName.DASHBOARD)
        event = DeviceListUpdatedEvent(devices=[])

        window.dispatch(event)

        window._views[ViewName.DASHBOARD].handle_event.assert_called_once_with(
            event
        )  # pylint: disable=protected-access

    def test_pages_event_forwarded_to_active_view(self, window):  # pylint: disable=redefined-outer-name
        window.switch_to(ViewName.DASHBOARD)
        event = PagesUpdatedEvent(
            pages=DevicePages(device_id="d", pages=[PageInfo("p", "Page 1")], default_page_id="p", active_page_id="p")
        )

        window.dispatch(event)

        window._views[ViewName.DASHBOARD].handle_event.assert_called_once_with(  # pylint: disable=protected-access
            event
        )


class TestCheckInitialView:
    def test_connected_switches_to_dashboard_and_shows(
        self, window, connection_manager  # pylint: disable=redefined-outer-name
    ):
        connection_manager.is_connected = True

        window.check_initial_view()

        assert_that(window._active).is_equal_to(ViewName.DASHBOARD)  # pylint: disable=protected-access

    def test_apply_initial_view_falls_back_to_connecting_when_disconnected(
        self, window  # pylint: disable=redefined-outer-name
    ):
        window._apply_initial_view()  # pylint: disable=protected-access

        assert_that(window._active).is_equal_to(ViewName.CONNECTING)  # pylint: disable=protected-access

    def test_apply_initial_view_uses_dashboard_when_connected(
        self, window, connection_manager  # pylint: disable=redefined-outer-name
    ):
        connection_manager.is_connected = True

        window._apply_initial_view()  # pylint: disable=protected-access

        assert_that(window._active).is_equal_to(ViewName.DASHBOARD)  # pylint: disable=protected-access


class TestRestart:
    def test_stops_and_restarts_connection_manager_and_lands_on_connecting(
        self, window, connection_manager  # pylint: disable=redefined-outer-name
    ):
        window.switch_to(ViewName.DASHBOARD)
        window.dispatch(ServerDisconnectedEvent(reason="lost"))  # populate _last_view

        window.restart()

        connection_manager.stop.assert_called_once()
        connection_manager.start.assert_called_once()
        assert_that(window._active).is_equal_to(ViewName.CONNECTING)  # pylint: disable=protected-access
        assert_that(window._last_view).is_none()  # pylint: disable=protected-access
        assert_that(AppContext.get().window).is_same_as(window)


class TestCloseEvent:
    @pytest.fixture(autouse=True)
    def _quit_on_close(self, monkeypatch):
        monkeypatch.setattr("decker.client.window._should_close_to_tray", lambda: False)
        monkeypatch.setattr("decker.client.window.QApplication.instance", MagicMock)

    def test_stops_connection_manager(self, window, connection_manager):  # pylint: disable=redefined-outer-name
        window.closeEvent(QCloseEvent())

        connection_manager.stop.assert_called_once()

    def test_saves_window_geometry(self, window):  # pylint: disable=redefined-outer-name
        window.setGeometry(10, 20, 300, 400)

        window.closeEvent(QCloseEvent())

        saved = window._ui_state_store.get("window_geometry")  # pylint: disable=protected-access
        assert_that(saved).is_equal_to({"x": 10, "y": 20, "w": 300, "h": 400})

    def test_calls_save_state_on_every_view(self, window):  # pylint: disable=redefined-outer-name
        window.closeEvent(QCloseEvent())

        for view in window._views.values():  # pylint: disable=protected-access
            view.save_state.assert_called_once()


class TestCloseToTray:
    @pytest.fixture(autouse=True)
    def _app(self, monkeypatch):
        self.app = MagicMock()
        monkeypatch.setattr("decker.client.window.QApplication.instance", lambda: self.app)

    def test_close_hides_to_tray_and_keeps_running(
        self, window, connection_manager, monkeypatch
    ):  # pylint: disable=redefined-outer-name
        monkeypatch.setattr("decker.client.window._should_close_to_tray", lambda: True)
        window.show()
        event = QCloseEvent()

        window.closeEvent(event)

        assert_that(event.isAccepted()).is_false()
        assert_that(window.isVisible()).is_false()
        connection_manager.stop.assert_not_called()
        self.app.quit.assert_not_called()
        assert_that(window._ui_state_store.get("window_geometry")).is_not_none()  # pylint: disable=protected-access

    def test_close_without_close_to_tray_quits(
        self, window, connection_manager, monkeypatch
    ):  # pylint: disable=redefined-outer-name
        monkeypatch.setattr("decker.client.window._should_close_to_tray", lambda: False)

        window.closeEvent(QCloseEvent())

        connection_manager.stop.assert_called_once()
        self.app.quit.assert_called_once()

    def test_quit_app_shuts_down_even_with_close_to_tray(
        self, window, connection_manager, monkeypatch
    ):  # pylint: disable=redefined-outer-name
        monkeypatch.setattr("decker.client.window._should_close_to_tray", lambda: True)
        window.show()

        window.quit_app()

        connection_manager.stop.assert_called_once()
        self.app.quit.assert_called_once()

    @pytest.mark.parametrize(
        "enabled,tray_available,expected", [(True, True, True), (True, False, False), (False, True, False)]
    )
    def test_close_to_tray_needs_setting_and_a_tray(self, monkeypatch, enabled, tray_available, expected):
        monkeypatch.setattr("decker.client.window.client_config.get_close_to_tray", lambda: enabled)
        monkeypatch.setattr(
            "decker.client.window.QSystemTrayIcon.isSystemTrayAvailable", staticmethod(lambda: tray_available)
        )

        assert_that(_should_close_to_tray()).is_equal_to(expected)


class TestShowEvent:
    def test_restores_saved_geometry_on_first_show(self, window):  # pylint: disable=redefined-outer-name
        window._ui_state_store.set(  # pylint: disable=protected-access
            "window_geometry", {"x": 5, "y": 6, "w": 333, "h": 222}
        )

        with patch.object(MainWindow, "setGeometry") as mock_set_geometry:
            window.show()

        mock_set_geometry.assert_called_once_with(5, 6, 333, 222)

    def test_does_not_restore_again_on_subsequent_shows(self, window):  # pylint: disable=redefined-outer-name
        window._ui_state_store.set(  # pylint: disable=protected-access
            "window_geometry", {"x": 5, "y": 6, "w": 333, "h": 222}
        )
        window.show()

        with patch.object(MainWindow, "setGeometry") as mock_set_geometry:
            window.hide()
            window.show()

        mock_set_geometry.assert_not_called()

    def test_no_saved_geometry_does_not_call_set_geometry(self, window):  # pylint: disable=redefined-outer-name
        with patch.object(MainWindow, "setGeometry") as mock_set_geometry:
            window.show()

        mock_set_geometry.assert_not_called()


class TestSettingsDialog:
    def test_plugin_load_failed_reaches_settings_dialog_and_toast(self, window):  # pylint: disable=redefined-outer-name
        window.dispatch(PluginLoadFailedEvent(plugin_uuid="com.decker.broken", error="boom"))

        dialog = window._settings_dialog  # pylint: disable=protected-access
        tooltips = [
            dialog._plugins_tab._plugin_list.item(i).toolTip()  # pylint: disable=protected-access
            for i in range(dialog._plugins_tab._plugin_list.count())  # pylint: disable=protected-access
        ]
        assert_that(tooltips).contains("Failed: boom")
        assert_that(window._toast.isHidden()).is_false()  # pylint: disable=protected-access

    def test_open_settings_shows_non_modal_dialog(
        self, window, isolated_config
    ):  # pylint: disable=redefined-outer-name,unused-argument
        window.open_settings()

        dialog = window._settings_dialog  # pylint: disable=protected-access
        assert_that(dialog.isVisible()).is_true()
        assert_that(dialog.isModal()).is_false()
        assert_that(dialog.parent()).is_same_as(window)
        dialog.close()

    def test_overlay_settings_button_opens_dialog(
        self, window, isolated_config
    ):  # pylint: disable=redefined-outer-name,unused-argument
        overlay = window._views[ViewName.CONNECTING]  # pylint: disable=protected-access

        overlay._settings_btn.click()  # pylint: disable=protected-access

        assert_that(window._settings_dialog.isVisible()).is_true()  # pylint: disable=protected-access
        window._settings_dialog.close()  # pylint: disable=protected-access


def _drop_event(*paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    event = MagicMock()
    event.mimeData.return_value = mime
    return event


class TestDropInstall:
    def test_drop_deckerplugin_triggers_install(
        self, window, connection_manager, tmp_path
    ):  # pylint: disable=redefined-outer-name
        connection_manager.call.return_value = True
        refresh_install_allowed()
        package = tmp_path / "a.deckerplugin"
        event = _drop_event(package)

        with patch("decker.client.window.install_package_file") as install:
            window.dropEvent(event)

        install.assert_called_once_with(package, window)
        event.acceptProposedAction.assert_called_once()

    def test_drop_other_files_ignored(
        self, window, connection_manager, tmp_path
    ):  # pylint: disable=redefined-outer-name
        connection_manager.call.return_value = True
        refresh_install_allowed()
        event = _drop_event(tmp_path / "a.txt")

        with patch("decker.client.window.install_package_file") as install:
            window.dragEnterEvent(event)
            window.dropEvent(event)

        install.assert_not_called()
        event.acceptProposedAction.assert_not_called()

    def test_drop_rejected_when_install_not_allowed(
        self, window, connection_manager, tmp_path
    ):  # pylint: disable=redefined-outer-name
        connection_manager.call.return_value = False
        refresh_install_allowed()
        event = _drop_event(tmp_path / "a.deckerplugin")

        with patch("decker.client.window.install_package_file") as install:
            window.dragEnterEvent(event)
            window.dropEvent(event)

        install.assert_not_called()
        event.acceptProposedAction.assert_not_called()

    def test_drag_enter_does_not_call_server(
        self, window, connection_manager, tmp_path
    ):  # pylint: disable=redefined-outer-name
        connection_manager.call.return_value = True
        refresh_install_allowed()
        connection_manager.call.reset_mock()

        window.dragEnterEvent(_drop_event(tmp_path / "a.deckerplugin"))
        window.dropEvent(_drop_event(tmp_path / "a.txt"))

        connection_manager.call.assert_not_called()

    def test_drag_enter_accepts_package_when_allowed(
        self, window, connection_manager, tmp_path
    ):  # pylint: disable=redefined-outer-name
        connection_manager.call.return_value = True
        refresh_install_allowed()
        event = _drop_event(tmp_path / "a.deckerplugin")

        window.dragEnterEvent(event)

        event.acceptProposedAction.assert_called_once()


def _settle_calls(window, qapp):  # pylint: disable=redefined-outer-name
    window._call_bridge.join()  # pylint: disable=protected-access
    qapp.processEvents()


class TestUpdateToast:
    @pytest.fixture(autouse=True)
    def _allowed(self):
        plugins_tab._install_allowed_cache = True
        yield
        plugins_tab._install_allowed_cache = False

    @staticmethod
    def _report(version="2.0.0"):
        updates = [{"id": "a.b", "name": "A", "installed": "1.0.0", "available": version}]
        return PluginUpdatesAvailableEvent(updates=updates, missing=[])

    def test_toast_action_shown_for_updates(
        self, window, connection_manager, qapp
    ):  # pylint: disable=redefined-outer-name
        connection_manager.call.return_value = "job-1"

        window.dispatch(self._report())

        assert_that(window._toast.text()).is_equal_to("1 plugin update available")  # pylint: disable=protected-access
        action = window._toast_action  # pylint: disable=protected-access
        assert_that(action.isHidden()).is_false()
        assert_that(action.text()).is_equal_to("Update all")
        action.click()
        _settle_calls(window, qapp)
        connection_manager.call.assert_called_with("plugin_install_from_repository", "a.b")

    def test_toast_for_missing_plugins(self, window):  # pylint: disable=redefined-outer-name
        missing = [{"id": f"m.{i}", "name": "M", "version": "1", "keys": 2} for i in range(2)]

        window.dispatch(PluginUpdatesAvailableEvent(updates=[], missing=missing))

        assert_that(window._toast.text()).is_equal_to("2 missing plugins available")  # pylint: disable=protected-access
        assert_that(window._toast_action.text()).is_equal_to("Install")  # pylint: disable=protected-access

    def test_toast_not_repeated_for_same_report(self, window):  # pylint: disable=redefined-outer-name
        window.dispatch(self._report())
        window._toast.hide()  # pylint: disable=protected-access

        window.dispatch(self._report())
        assert_that(window._toast.isHidden()).is_true()  # pylint: disable=protected-access

        window.dispatch(self._report("3.0.0"))
        assert_that(window._toast.isHidden()).is_false()  # pylint: disable=protected-access

    def test_summary_toast_after_run(self, window, connection_manager, qapp):  # pylint: disable=redefined-outer-name
        connection_manager.call.return_value = "job-1"
        window.dispatch(self._report())
        window._toast_action.click()  # pylint: disable=protected-access
        _settle_calls(window, qapp)

        window.dispatch(
            PluginInstallFinishedEvent(
                job_id="job-1", ok=True, plugin_id="a.b", version="2.0.0", previous_version="1.0.0", message=""
            )
        )

        assert_that(window._toast.text()).is_equal_to(
            "1 plugin installed or updated"
        )  # pylint: disable=protected-access
        assert_that(window._toast_action.isHidden()).is_true()  # pylint: disable=protected-access

    def test_toast_has_no_action_when_installs_are_not_allowed(self, window):  # pylint: disable=redefined-outer-name
        plugins_tab._install_allowed_cache = False

        window.dispatch(self._report())

        assert_that(window._toast.text()).is_equal_to("1 plugin update available")  # pylint: disable=protected-access
        assert_that(window._toast_action.isHidden()).is_true()  # pylint: disable=protected-access

    def test_disconnect_resets_a_run_in_flight(
        self, window, connection_manager, qapp
    ):  # pylint: disable=redefined-outer-name
        connection_manager.call.return_value = "job-1"
        window.dispatch(self._report())
        window._toast_action.click()  # pylint: disable=protected-access
        _settle_calls(window, qapp)
        assert_that(window._update_runner.busy).is_true()  # pylint: disable=protected-access

        window.dispatch(ServerDisconnectedEvent(reason="gone"))

        assert_that(window._update_runner.busy).is_false()  # pylint: disable=protected-access
        assert_that(window._toast.text()).contains("a.b", "Interrupted")  # pylint: disable=protected-access

    def test_start_call_runs_off_the_ui_thread(
        self, window, connection_manager, qapp
    ):  # pylint: disable=redefined-outer-name
        threads: list[int] = []

        def call(*_args):
            threads.append(threading.get_ident())
            return "job-1"

        connection_manager.call.side_effect = call
        window.dispatch(self._report())
        window._toast_action.click()  # pylint: disable=protected-access
        _settle_calls(window, qapp)

        assert_that(threads).is_length(1)
        assert_that(threads[0]).is_not_equal_to(threading.get_ident())


_FIREFOX = WindowInfo("firefox", "normal", "Mozilla", "/usr/bin/firefox", "firefox.desktop", 7)


@pytest.fixture
def live_window(qapp, connection_manager, tmp_path):  # pylint: disable=unused-argument,redefined-outer-name
    """A MainWindow with real views, driven through the EventManager queue like the running app."""
    AppContext._instance = None  # pylint: disable=protected-access
    EventManager.reset()
    win = MainWindow(connection_manager, ui_state_store=UiStateStore(tmp_path / "config.db"))
    AppContext.get().connection_manager = connection_manager
    AppContext.get().active_device = DeviceHardwareInfo(
        id="dev1", type="Stream Deck", vendor_id=0, product_id=0, num_keys=15
    )
    connection_manager.call.return_value = True
    win.switch_to(ViewName.DASHBOARD)
    yield win
    win.close()


def _push(*events) -> None:
    for event in events:
        EventManager.get().push(event)
    EventManager.get().drain_and_dispatch()


class TestAppProfileEventsReachTheDashboard:  # pylint: disable=protected-access
    def _open_spy(self, win):
        dashboard = win._views[ViewName.DASHBOARD]
        _push(
            ProfilesUpdatedEvent(DeviceProfiles("dev1", [ProfileInfo(0, "Default", "")], 0)),
            DetectionStatusEvent("active", "", "kwin", True, None),
        )
        dashboard._on_edit_profile(0)
        dialog = dashboard._profiles_dialog
        dialog._spy_button.click()
        return dashboard, dialog, dialog._spy

    def test_rules_status_focus_and_pick_update_the_open_dialog_and_spy(self, live_window):
        _, dialog, spy = self._open_spy(live_window)
        rule = AppRule("r1", "Browser", True, [MatchCondition("window_class", "equals", "firefox")], 0)

        _push(AppRulesUpdatedEvent(DeviceRules("dev1", [rule], DefaultRule())))
        assert_that(dialog._rule_list.count()).is_equal_to(1)

        _push(DetectionStatusEvent("active", "", "x11", False, None))
        assert dialog._status is not None and dialog._status.backend == "x11"

        spy._follow.setChecked(True)
        _push(WindowFocusedEvent(_FIREFOX))
        assert_that(spy._values["window_class"].text()).is_equal_to("firefox")

        spy.set_status(DetectionStatusEvent("active", "", "kwin", True, None))
        spy._pick_button.click()
        _push(WindowPickedEvent(WindowInfo("konsole", pid=9)))
        assert_that(spy._values["window_class"].text()).is_equal_to("konsole")
        spy.close()
        dialog.close()

    def test_events_while_another_view_is_active_are_not_lost(self, live_window):
        live_window.switch_to(ViewName.DEVICE)
        rule = AppRule("r1", "Browser", True, [], 0)

        _push(
            AppRulesUpdatedEvent(DeviceRules("dev1", [rule], DefaultRule())),
            DetectionStatusEvent("active", "", "kwin", True, None),
        )

        dashboard = live_window._views[ViewName.DASHBOARD]
        assert_that(dashboard._rules.rules).is_length(1)
        assert_that(dashboard._detection.backend).is_equal_to("kwin")

    def test_disconnect_resets_an_open_pick_and_switches_to_connecting(self, live_window):
        _, dialog, spy = self._open_spy(live_window)
        spy._pick_button.click()
        assert_that(spy._picking).is_true()

        _push(ServerDisconnectedEvent("lost"))

        assert_that(spy._picking).is_false()
        assert_that(live_window._active).is_equal_to(ViewName.CONNECTING)
        spy.close()
        dialog.close()

    def test_every_event_the_dashboard_handles_is_forwarded(self, live_window):
        import inspect  # pylint: disable=import-outside-toplevel
        import re  # pylint: disable=import-outside-toplevel

        source = inspect.getsource(live_window._views[ViewName.DASHBOARD].handle_event)
        handled = set(re.findall(r"isinstance\(event, (\w+)\)", source))
        forwarded = {t.__name__ for t in (*ACTIVE_VIEW_EVENTS, *DASHBOARD_EVENTS)}

        assert_that(handled - forwarded).is_empty()
        subscribed = EventManager.get()._subscribers
        assert_that([t for t in DASHBOARD_EVENTS if not subscribed[t]]).is_empty()
