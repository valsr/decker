"""Tests for decker.client.widgets.settings_dialog.SettingsDialog."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from assertpy import assert_that
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QDialogButtonBox

from decker.client import config as client_config
from decker.client.assets import load_stylesheet
from decker.client.connection_manager import ServerState
from decker.client.context import AppContext
from decker.client.events import PluginLoadFailedEvent
from decker.client.events.event_manager import EventManager
from decker.client.widgets.settings_dialog import SettingsDialog

# pylint: disable=protected-access,redefined-outer-name,unused-argument


@pytest.fixture
def manager():
    AppContext.reset()
    EventManager.reset()
    connection_manager = MagicMock()
    connection_manager.state = ServerState.HEALTHY
    AppContext.get().connection_manager = connection_manager
    yield connection_manager
    AppContext.reset()
    EventManager.reset()


@pytest.fixture
def dialog(qapp, isolated_config, manager):
    original_sheet = qapp.styleSheet()
    dlg = SettingsDialog()
    yield dlg
    dlg.close()
    qapp.setStyleSheet(original_sheet)


def _select_theme(dlg: SettingsDialog, theme: str) -> None:
    dlg._theme_combo.setCurrentIndex(dlg._theme_combo.findData(theme))


class TestLayout:
    def test_is_non_modal_with_general_server_ui_plugins_tabs(self, dialog):
        tabs = [dialog._tabs.tabText(i) for i in range(dialog._tabs.count())]

        assert_that(dialog.isModal()).is_false()
        assert_that(tabs).is_equal_to(["General", "Server", "Plugins", "Device"])

    def test_bottom_buttons_are_apply_and_close(self, dialog):
        buttons = [dialog._buttons.standardButton(b) for b in dialog._buttons.buttons()]

        assert_that(buttons).contains_only(QDialogButtonBox.StandardButton.Apply, QDialogButtonBox.StandardButton.Close)


class TestOpenAndClose:
    def test_open_loads_current_config(self, dialog, isolated_config):
        client_config.set_server("myhost", 1234)
        client_config.set_theme("light")

        dialog.open_dialog()

        assert_that(dialog.isVisible()).is_true()
        assert_that(dialog._host_input.text()).is_equal_to("myhost")
        assert_that(dialog._port_input.text()).is_equal_to("1234")
        assert_that(dialog._selected_theme()).is_equal_to("light")

    def test_escape_closes(self, dialog):
        dialog.open_dialog()

        dialog.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))

        assert_that(dialog.isVisible()).is_false()

    def test_close_button_closes_without_saving(self, dialog):
        dialog.open_dialog()
        dialog._host_input.setText("otherhost")

        dialog._buttons.button(QDialogButtonBox.StandardButton.Close).click()

        assert_that(dialog.isVisible()).is_false()
        assert_that(client_config.get_server_host()).is_equal_to("localhost")

    def test_reopening_discards_unapplied_edits(self, dialog):
        dialog.open_dialog()
        dialog._host_input.setText("otherhost")
        dialog.reject()

        dialog.open_dialog()

        assert_that(dialog._host_input.text()).is_equal_to("localhost")

    def test_open_while_visible_keeps_edits(self, dialog):
        dialog.open_dialog()
        dialog._host_input.setText("otherhost")

        dialog.open_dialog()

        assert_that(dialog._host_input.text()).is_equal_to("otherhost")


class TestApplyServer:
    def test_apply_saves_server_and_requests_reconnect(self, dialog, manager):
        dialog.open_dialog()
        dialog._host_input.setText("otherhost")
        dialog._port_input.setText("9999")

        dialog._buttons.button(QDialogButtonBox.StandardButton.Apply).click()

        assert_that(client_config.get_server_host()).is_equal_to("otherhost")
        assert_that(client_config.get_server_port()).is_equal_to(9999)
        manager.request_reconnect.assert_called_once()
        assert_that(dialog._message_label.text()).is_equal_to("Applied.")
        assert_that(dialog.isVisible()).is_true()

    def test_apply_without_server_change_does_not_reconnect(self, dialog, manager):
        dialog.open_dialog()
        _select_theme(dialog, "light")

        assert_that(dialog.apply()).is_true()

        manager.request_reconnect.assert_not_called()

    @pytest.mark.parametrize("port", ["abc", "", "0", "65536", "-1"])
    def test_invalid_port_saves_nothing(self, dialog, manager, port):
        dialog.open_dialog()
        dialog._tabs.setCurrentWidget(dialog._general_tab)
        dialog._port_input.setText(port)
        _select_theme(dialog, "light")

        assert_that(dialog.apply()).is_false()

        assert_that(client_config.get_theme()).is_equal_to("dark")
        manager.request_reconnect.assert_not_called()
        assert_that(dialog._message_label.text()).contains("65535")
        assert_that(dialog._tabs.currentWidget()).is_same_as(dialog._server_tab)

    def test_empty_host_is_rejected(self, dialog):
        dialog.open_dialog()
        dialog._host_input.setText("  ")

        assert_that(dialog.apply()).is_false()


class TestApplyTheme:
    def test_apply_light_saves_and_applies_stylesheet(self, dialog, qapp):
        dialog.open_dialog()
        _select_theme(dialog, "light")

        dialog.apply()

        assert_that(client_config.get_theme()).is_equal_to("light")
        assert_that(qapp.styleSheet()).is_equal_to(load_stylesheet("light"))

    def test_selecting_theme_does_not_apply_until_apply(self, dialog, qapp):
        dialog.open_dialog()
        before = qapp.styleSheet()

        _select_theme(dialog, "light")

        assert_that(qapp.styleSheet()).is_equal_to(before)
        assert_that(client_config.get_theme()).is_equal_to("dark")

    def test_custom_path_enabled_only_for_custom_theme(self, dialog):
        dialog.open_dialog()
        assert_that(dialog._custom_path_input.isEnabled()).is_false()

        _select_theme(dialog, "custom")

        assert_that(dialog._custom_path_input.isEnabled()).is_true()
        assert_that(dialog._browse_btn.isEnabled()).is_true()

    def test_apply_custom_reads_file(self, dialog, qapp, tmp_path):
        sheet = tmp_path / "mine.qss"
        sheet.write_text("QWidget { color: red; }", encoding="utf-8")
        dialog.open_dialog()
        _select_theme(dialog, "custom")
        dialog._custom_path_input.setText(str(sheet))

        assert_that(dialog.apply()).is_true()

        assert_that(qapp.styleSheet()).is_equal_to("QWidget { color: red; }")
        assert_that(client_config.get_custom_style_sheet()).is_equal_to(str(sheet))

    @pytest.mark.parametrize("path", ["", "missing.qss"])
    def test_custom_without_readable_file_saves_nothing(self, dialog, tmp_path, path):
        dialog.open_dialog()
        _select_theme(dialog, "custom")
        dialog._custom_path_input.setText(str(tmp_path / path) if path else "")

        assert_that(dialog.apply()).is_false()

        assert_that(client_config.get_theme()).is_equal_to("dark")
        assert_that(dialog._tabs.currentWidget()).is_same_as(dialog._general_tab)

    def test_browse_fills_path(self, dialog, monkeypatch):
        monkeypatch.setattr(
            "decker.client.widgets.settings_dialog.QFileDialog.getOpenFileName",
            lambda *_args: ("/picked.qss", ""),
        )

        dialog._browse_stylesheet()

        assert_that(dialog._custom_path_input.text()).is_equal_to("/picked.qss")


class TestLiveUpdates:
    def test_status_reflects_connection_state_while_open(self, dialog, manager):
        dialog.open_dialog()
        assert_that(dialog._status_label.text()).is_equal_to("Connected")

        manager.state = ServerState.DISCONNECTED
        dialog._status_timer.timeout.emit()

        assert_that(dialog._status_label.text()).is_equal_to("Disconnected")

    def test_status_timer_runs_only_while_visible(self, dialog):
        dialog.open_dialog()
        assert_that(dialog._status_timer.isActive()).is_true()

        dialog.reject()

        assert_that(dialog._status_timer.isActive()).is_false()

    def test_status_without_connection_manager(self, dialog):
        AppContext.get().connection_manager = None

        dialog.open_dialog()

        assert_that(dialog._status_label.text()).is_equal_to("Disconnected")

    def test_plugin_failures_appear_live(self, dialog):
        dialog.open_dialog()
        plugin_list = dialog._plugins_tab._plugin_list

        consumed = EventManager.get().emit(PluginLoadFailedEvent(plugin_uuid="com.x.broken", error="boom"))

        assert_that(consumed).is_false()
        texts = [plugin_list.item(i).text() for i in range(plugin_list.count())]
        assert_that(texts).contains("com.x.broken\nCould not be loaded · Failed")

    def test_set_failed_plugins_replaces_the_failed_rows(self, dialog):
        dialog.set_failed_plugins({"b": "e2", "a": "e1"})
        dialog.set_failed_plugins({"a": "e1"})

        plugin_list = dialog._plugins_tab._plugin_list
        ids = [plugin_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(plugin_list.count())]
        assert_that(ids).contains("a").does_not_contain("b")


class TestGeneralTab:
    def test_close_to_tray_is_on_by_default(self, dialog):
        dialog.open_dialog()

        assert_that(dialog._close_to_tray_check.isChecked()).is_true()

    def test_apply_saves_close_to_tray(self, dialog):
        dialog.open_dialog()
        dialog._close_to_tray_check.setChecked(False)

        assert_that(dialog.apply()).is_true()

        assert_that(client_config.get_close_to_tray()).is_false()

    def test_unapplied_change_is_discarded(self, dialog):
        dialog.open_dialog()
        dialog._close_to_tray_check.setChecked(False)
        dialog.reject()

        dialog.open_dialog()

        assert_that(dialog._close_to_tray_check.isChecked()).is_true()


_MIB = 1024 * 1024


def _info(**overrides: Any):
    from decker.client.server_settings import ServerSettingsInfo

    values: dict[str, Any] = {
        "port": 18861,
        "memory_limit": 1024 * _MIB,
        "icon_max_file_size": 4 * _MIB,
        "stop_when_last_client_leaves": False,
        "reconfigurable": True,
        "last_apply_error": None,
    }
    values.update(overrides)
    return ServerSettingsInfo(**values)


def _server_fields(dlg):
    return [dlg._memory_spin, dlg._image_spin, dlg._stop_check]


class TestServerReconfiguration:
    def test_fields_are_disabled_when_the_server_does_not_allow_it(self, dialog, manager):
        manager.server_settings = _info(reconfigurable=False)

        dialog.open_dialog()

        assert_that([f.isEnabled() for f in _server_fields(dialog)]).is_equal_to([False, False, False])
        assert_that(dialog._host_input.isEnabled()).is_true()
        assert_that(dialog._port_input.isEnabled()).is_true()
        assert_that(dialog._memory_spin.toolTip()).contains("--allow-client-reconfiguration")

    def test_fields_are_disabled_without_server_settings(self, dialog, manager):
        manager.server_settings = None

        dialog.open_dialog()

        assert_that([f.isEnabled() for f in _server_fields(dialog)]).is_equal_to([False, False, False])

    def test_fields_are_enabled_and_loaded_when_allowed(self, dialog, manager):
        manager.server_settings = _info(
            memory_limit=512 * _MIB, icon_max_file_size=2 * _MIB, stop_when_last_client_leaves=True
        )

        dialog.open_dialog()

        assert_that([f.isEnabled() for f in _server_fields(dialog)]).is_equal_to([True, True, True])
        assert_that(dialog._memory_spin.value()).is_equal_to(512)
        assert_that(dialog._image_spin.value()).is_equal_to(2048)
        assert_that(dialog._stop_check.isChecked()).is_true()

    def test_apply_sends_only_the_changed_values(self, dialog, manager):
        manager.server_settings = _info()
        manager.set_server_settings.return_value = (True, None)
        dialog.open_dialog()
        dialog._memory_spin.setValue(256)

        assert_that(dialog.apply()).is_true()

        manager.set_server_settings.assert_called_once_with({"memory_limit": 256 * _MIB})
        manager.request_reconnect.assert_not_called()

    def test_a_host_change_is_never_sent_to_the_server(self, dialog, manager):
        manager.server_settings = _info()
        dialog.open_dialog()
        dialog._host_input.setText("other.example")

        dialog.apply()

        manager.set_server_settings.assert_not_called()
        assert_that(client_config.get_server_host()).is_equal_to("other.example")

    def test_a_port_change_is_sent_stored_and_reconnects(self, dialog, manager):
        manager.server_settings = _info()
        manager.set_server_settings.return_value = (True, None)
        dialog.open_dialog()
        dialog._port_input.setText("19000")

        assert_that(dialog.apply()).is_true()

        manager.set_server_settings.assert_called_once_with({"port": 19000})
        assert_that(client_config.get_server_port()).is_equal_to(19000)
        manager.request_reconnect.assert_called_once()

    def test_a_server_refusal_saves_nothing(self, dialog, manager):
        manager.server_settings = _info()
        manager.set_server_settings.return_value = (False, "port is already used")
        dialog.open_dialog()
        dialog._port_input.setText("19000")

        assert_that(dialog.apply()).is_false()

        assert_that(client_config.get_server_port()).is_equal_to(18861)
        assert_that(dialog._message_label.text()).contains("port is already used")

    def test_when_not_allowed_a_port_change_only_changes_the_client_target(self, dialog, manager):
        manager.server_settings = _info(reconfigurable=False)
        dialog.open_dialog()
        dialog._port_input.setText("19000")

        dialog.apply()

        manager.set_server_settings.assert_not_called()
        assert_that(client_config.get_server_port()).is_equal_to(19000)
        manager.request_reconnect.assert_called_once()

    def test_an_unreachable_new_port_reverts_the_client_port(self, dialog, manager):
        manager.server_settings = _info()
        manager.set_server_settings.return_value = (True, None)
        dialog.open_dialog()
        dialog._port_input.setText("19000")
        dialog.apply()
        manager.request_reconnect.reset_mock()
        manager.state = ServerState.DISCONNECTED
        manager.is_connected = False

        dialog._check_restart_result()

        assert_that(client_config.get_server_port()).is_equal_to(18861)
        manager.request_reconnect.assert_called_once()
        assert_that(dialog._message_label.text()).contains("18861")

    def test_a_reachable_new_port_is_kept(self, dialog, manager):
        manager.server_settings = _info()
        manager.set_server_settings.return_value = (True, None)
        dialog.open_dialog()
        dialog._port_input.setText("19000")
        dialog.apply()
        manager.is_connected = True

        dialog._check_restart_result()

        assert_that(client_config.get_server_port()).is_equal_to(19000)

    def test_the_last_apply_error_is_shown(self, dialog, manager):
        manager.server_settings = _info(last_apply_error="The server could not start on port 19000.")

        dialog.open_dialog()

        assert_that(dialog._apply_error_label.isHidden()).is_false()
        assert_that(dialog._apply_error_label.text()).contains("19000")
