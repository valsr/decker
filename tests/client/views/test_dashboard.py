"""Tests for decker.client.views.dashboard.DashboardView."""

import dataclasses
import json
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from assertpy import assert_that
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QMenu, QMessageBox

from decker.client.call_bridge import CallBridge
from decker.client.context import AppContext
from decker.client.events import (
    AppRulesUpdatedEvent,
    CardListUpdatedEvent,
    DetectionStatusEvent,
    DeviceListUpdatedEvent,
    KeyDisplayStatesUpdatedEvent,
    PagesUpdatedEvent,
    PresetListUpdatedEvent,
    ProfilesUpdatedEvent,
)
from decker.client.ui_state_store import UiStateStore
from decker.client.views.dashboard import DashboardView, _confirm_delete_page
from decker.client.widgets.profiles_dialog import ProfilesDialog
from decker.common.model import (
    AppRule,
    DefaultRule,
    DeviceHardwareInfo,
    DevicePages,
    DeviceProfiles,
    DeviceRules,
    KeyInfo,
    MatchCondition,
    PageInfo,
    ProfileInfo,
)


def _make_keys(rows: int, cols: int) -> list[KeyInfo]:
    return [
        KeyInfo(row=r, col=c, pixel_width=72, pixel_height=72, type="key") for r in range(rows) for c in range(cols)
    ]


_KEYS_15 = _make_keys(3, 5)
_KEYS_6 = _make_keys(2, 3)

_D1 = DeviceHardwareInfo(id="aabbccdd", type="Stream Deck", vendor_id=0, product_id=0, num_keys=15, keys=_KEYS_15)
_D2 = DeviceHardwareInfo(id="11223344", type="Stream Deck Mini", vendor_id=0, product_id=0, num_keys=6, keys=_KEYS_6)
_DEVICES = [_D1, _D2]
_PAGES = DevicePages(
    device_id="dev1",
    pages=[PageInfo("p1", "Page 1"), PageInfo("p2", "Page 2")],
    default_page_id="p1",
    active_page_id="p1",
)


def _make_view(tmp_path) -> DashboardView:
    return DashboardView(ui_state_store=UiStateStore(tmp_path / "config.db"))


class TestDashboardView:
    def setup_method(self):
        AppContext._instance = None  # pylint: disable=protected-access

    def test_has_no_page_tabs_until_server_sends_pages(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        assert_that(view._page_tabs.tab_bar.count()).is_equal_to(0)  # pylint: disable=protected-access


class TestDeviceListHandling:
    def setup_method(self):
        AppContext._instance = None  # pylint: disable=protected-access

    def test_auto_selects_first_device_when_none_active(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        mock_window = MagicMock()
        AppContext.get().window = mock_window

        view.handle_event(DeviceListUpdatedEvent(devices=_DEVICES))

        mock_window.set_active_device.assert_called_once_with(_D1)

    def test_does_not_override_already_active_device(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        AppContext.get().active_device = _D2
        mock_window = MagicMock()
        AppContext.get().window = mock_window

        view.handle_event(DeviceListUpdatedEvent(devices=_DEVICES))

        mock_window.set_active_device.assert_not_called()

    def test_grid_resizes_to_active_devices_key_count(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        AppContext.get().active_device = _D2

        view.handle_event(DeviceListUpdatedEvent(devices=_DEVICES))

        assert_that(view._grid.key_count).is_equal_to(6)  # pylint: disable=protected-access

    def test_same_device_does_not_rebuild_grid(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        AppContext.get().active_device = _D1
        view.handle_event(DeviceListUpdatedEvent(devices=_DEVICES))

        with patch.object(view._grid, "build") as mock_build:  # pylint: disable=protected-access
            view.handle_event(DeviceListUpdatedEvent(devices=_DEVICES))
            mock_build.assert_not_called()


class TestCardListHandling:
    def setup_method(self):
        AppContext._instance = None  # pylint: disable=protected-access

    def test_populates_sidebar_cards(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        actions = [{"plugin_uuid": "p", "plugin_name": "P", "card_uuid": "p.a", "card_name": "A"}]

        view.handle_event(CardListUpdatedEvent(cards=actions))

        assert_that(view._sidebar._cards).is_equal_to(actions)  # pylint: disable=protected-access


class TestSidebarToggle:
    def setup_method(self):
        AppContext._instance = None  # pylint: disable=protected-access

    def test_toggle_collapses_sidebar_to_zero(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        view._main_splitter.setSizes([240, 760])  # pylint: disable=protected-access

        view._toggle_sidebar()  # pylint: disable=protected-access

        assert_that(view._main_splitter.sizes()[0]).is_equal_to(0)  # pylint: disable=protected-access

    def test_toggle_twice_restores_previous_width(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        view._main_splitter.setSizes([240, 760])  # pylint: disable=protected-access
        actual_width = view._main_splitter.sizes()[0]  # pylint: disable=protected-access

        view._toggle_sidebar()  # pylint: disable=protected-access
        view._toggle_sidebar()  # pylint: disable=protected-access

        assert_that(view._main_splitter.sizes()[0]).is_equal_to(actual_width)  # pylint: disable=protected-access


class TestSaveAndRestoreState:
    def setup_method(self):
        AppContext._instance = None  # pylint: disable=protected-access

    def test_save_state_persists_splitter_sizes(self, qapp, tmp_path):  # pylint: disable=unused-argument
        store = UiStateStore(tmp_path / "config.db")
        view = DashboardView(ui_state_store=store)
        view._main_splitter.setSizes([300, 700])  # pylint: disable=protected-access
        view._content_splitter.setSizes([500, 200])  # pylint: disable=protected-access
        actual_main = view._main_splitter.sizes()  # pylint: disable=protected-access
        actual_content = view._content_splitter.sizes()  # pylint: disable=protected-access

        view.save_state()

        assert_that(store.get("splitter_main")).is_equal_to(actual_main)
        assert_that(store.get("splitter_content")).is_equal_to(actual_content)
        assert_that(store.get("sidebar_collapsed")).is_equal_to(0)

    def test_save_state_records_collapsed_sidebar(self, qapp, tmp_path):  # pylint: disable=unused-argument
        store = UiStateStore(tmp_path / "config.db")
        view = DashboardView(ui_state_store=store)
        view._collapse_sidebar()  # pylint: disable=protected-access

        view.save_state()

        assert_that(store.get("sidebar_collapsed")).is_equal_to(1)

    def test_restores_saved_splitter_sizes_on_construction(self, qapp, tmp_path):  # pylint: disable=unused-argument
        db_path = tmp_path / "config.db"
        UiStateStore(db_path).set("splitter_main", [333, 667])

        view = DashboardView(ui_state_store=UiStateStore(db_path))

        sizes = view._main_splitter.sizes()  # pylint: disable=protected-access
        assert_that(sizes[0]).is_greater_than(0)
        assert_that(sizes[0]).is_less_than(sizes[1])

    def test_restores_collapsed_sidebar_on_construction(self, qapp, tmp_path):  # pylint: disable=unused-argument
        db_path = tmp_path / "config.db"
        UiStateStore(db_path).set("sidebar_collapsed", 1)

        view = DashboardView(ui_state_store=UiStateStore(db_path))

        assert_that(view._main_splitter.sizes()[0]).is_equal_to(0)  # pylint: disable=protected-access


class TestDeviceSwitcher:
    def setup_method(self):
        AppContext._instance = None  # pylint: disable=protected-access

    def test_selecting_device_updates_window_and_grid(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        mock_window = MagicMock()
        AppContext.get().window = mock_window
        view._devices = _DEVICES  # pylint: disable=protected-access

        view._on_device_selected(_D2)  # pylint: disable=protected-access

        mock_window.set_active_device.assert_called_once_with(_D2)
        assert_that(view._grid.key_count).is_equal_to(6)  # pylint: disable=protected-access


class TestViewMenu:  # pylint: disable=protected-access
    def setup_method(self):
        AppContext._instance = None

    def _menu(self, view: DashboardView):
        menu = view._page_tabs.menu
        view._page_tabs.menu_about_to_show.emit(menu)
        return menu

    def _action(self, menu, text: str):
        return next(a for a in menu.actions() if a.text() == text)

    def test_menu_lists_sidebar_device_settings(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        view._main_splitter.setSizes([200, 700])

        texts = [a.text() for a in self._menu(view).actions() if not a.isSeparator()]

        assert_that(texts).is_equal_to(["Hide Sidebar", "Device", "Edit Profiles…", "Settings", "About Decker…"])

    def test_menu_button_is_right_of_tabs(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        bar = view._page_tabs
        layout = bar.layout()
        assert layout is not None

        assert_that(layout.indexOf(bar._menu_button)).is_equal_to(layout.count() - 1)
        assert_that(bar._menu_button.menu()).is_same_as(bar.menu)

    def test_sidebar_label_follows_state_and_toggles(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        view._main_splitter.setSizes([200, 700])

        self._action(self._menu(view), "Hide Sidebar").trigger()

        assert_that(view._is_sidebar_collapsed()).is_true()
        self._action(self._menu(view), "Show Sidebar").trigger()
        assert_that(view._is_sidebar_collapsed()).is_false()

    def test_reopening_does_not_duplicate_items(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        self._menu(view)

        assert_that(self._menu(view).actions()).is_length(6)

    def test_device_submenu_without_devices(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        submenu = self._action(self._menu(view), "Device").menu()

        assert_that([(a.text(), a.isEnabled()) for a in submenu.actions()]).is_equal_to(
            [("No devices connected", False)]
        )

    def test_device_submenu_checks_active_and_selects(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        mock_window = MagicMock()
        AppContext.get().window = mock_window
        AppContext.get().active_device = _D1
        view._devices = _DEVICES

        actions = self._action(self._menu(view), "Device").menu().actions()

        assert_that([a.isChecked() for a in actions]).is_equal_to([True, False])
        actions[1].trigger()
        mock_window.set_active_device.assert_called_with(_D2)
        assert_that(view._grid.key_count).is_equal_to(6)

    def test_settings_opens_settings_dialog(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        mock_window = MagicMock()
        AppContext.get().window = mock_window

        self._action(self._menu(view), "Settings").trigger()

        mock_window.open_settings.assert_called_once()


def _active_dev1(view: DashboardView) -> None:
    """Set active device to id='dev1' and build a 15-key grid."""
    AppContext.get().active_device = DeviceHardwareInfo(
        id="dev1", type="Stream Deck", vendor_id=0, product_id=0, num_keys=15
    )
    view._grid.build(_KEYS_15)  # pylint: disable=protected-access
    view._pages = _PAGES  # pylint: disable=protected-access


class TestKeyDisplayStates:
    def setup_method(self):
        AppContext._instance = None  # pylint: disable=protected-access

    def test_display_states_event_updates_key_labels(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        _active_dev1(view)

        view.handle_event(
            KeyDisplayStatesUpdatedEvent(
                device_id="dev1",
                states=[
                    {
                        "key_index": 2,
                        "label": "42",
                        "plugin_uuid": "p",
                        "card_uuid": "a",
                        "context_id": "c",
                        "settings": {"step": 1},
                    }
                ],
            )
        )

        btn = view._grid._buttons.get(2)  # pylint: disable=protected-access
        assert btn is not None
        assert_that(btn.text()).is_equal_to("42")

    def test_display_states_event_ignored_for_different_device(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        _active_dev1(view)

        view.handle_event(
            KeyDisplayStatesUpdatedEvent(
                device_id="other-dev",
                states=[
                    {
                        "key_index": 0,
                        "label": "99",
                        "plugin_uuid": "p",
                        "card_uuid": "a",
                        "context_id": "c",
                        "settings": {},
                    }
                ],
            )
        )
        btn = view._grid._buttons.get(0)  # pylint: disable=protected-access
        assert btn is not None
        assert_that(btn.text()).is_equal_to("1")  # default label unchanged

    def test_display_states_populates_key_slots(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        _active_dev1(view)

        view.handle_event(
            KeyDisplayStatesUpdatedEvent(
                device_id="dev1",
                states=[
                    {
                        "key_index": 5,
                        "label": "7",
                        "plugin_uuid": "com.p",
                        "card_uuid": "com.p.a",
                        "context_id": "ctx1",
                        "settings": {"step": 2},
                    }
                ],
            )
        )
        assert_that(view._key_slots).contains_key(5)  # pylint: disable=protected-access
        assert_that(view._key_slots[5]["plugin_uuid"]).is_equal_to("com.p")  # pylint: disable=protected-access


class TestKeyInteractions:
    def setup_method(self):
        AppContext._instance = None  # pylint: disable=protected-access

    def test_edit_requested_without_slot_shows_placeholder(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        view._grid.build(_KEYS_6)  # pylint: disable=protected-access
        view._on_key_edit_requested(0)  # pylint: disable=protected-access
        assert_that(view._config_panel.currentIndex()).is_equal_to(0)

    def test_key_press_with_slot_simulates_press(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        _active_dev1(view)
        mock_cm = MagicMock()
        AppContext.get().connection_manager = mock_cm
        view._key_slots[3] = {  # pylint: disable=protected-access
            "plugin_uuid": "p",
            "card_uuid": "a",
            "context_id": "c",
            "settings": {},
        }
        view._on_key_pressed(3)  # pylint: disable=protected-access
        mock_cm.call.assert_called_once_with("key_simulate_press", "dev1", 3)

    def test_pencil_selects_the_key_without_pressing(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        _active_dev1(view)
        mock_cm = MagicMock()
        AppContext.get().connection_manager = mock_cm
        view._key_slots[3] = {  # pylint: disable=protected-access
            "plugin_uuid": "p",
            "card_uuid": "a",
            "context_id": "c",
            "settings": {},
        }

        view._on_key_edit_requested(3)  # pylint: disable=protected-access

        assert_that(view._selected_key).is_equal_to(3)  # pylint: disable=protected-access
        pressed = [c for c in mock_cm.call.call_args_list if c.args and c.args[0] == "key_simulate_press"]
        assert_that(pressed).is_empty()

    def test_key_press_does_not_change_the_selection(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        _active_dev1(view)
        AppContext.get().connection_manager = MagicMock()
        view._key_slots[3] = {"plugin_uuid": "p", "card_uuid": "a", "context_id": "c", "settings": {}}
        view._key_slots[4] = {"plugin_uuid": "p", "card_uuid": "a", "context_id": "d", "settings": {}}
        view._on_key_edit_requested(4)  # pylint: disable=protected-access

        view._on_key_pressed(3)  # pylint: disable=protected-access

        assert_that(view._selected_key).is_equal_to(4)  # pylint: disable=protected-access

    def test_key_press_without_a_slot_calls_nothing(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        _active_dev1(view)
        mock_cm = MagicMock()
        AppContext.get().connection_manager = mock_cm

        view._on_key_pressed(2)  # pylint: disable=protected-access

        mock_cm.call.assert_not_called()

    def test_key_press_without_a_connection_does_not_raise(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        _active_dev1(view)
        AppContext.get().connection_manager = None
        view._key_slots[3] = {
            "plugin_uuid": "p",
            "card_uuid": "a",
            "context_id": "c",
            "settings": {},
        }  # pylint: disable=protected-access

        view._on_key_pressed(3)  # pylint: disable=protected-access

    def test_long_press_simulates_a_long_press_and_does_not_select(  # pylint: disable=unused-argument
        self, qapp, tmp_path
    ):
        view = _make_view(tmp_path)
        _active_dev1(view)
        mock_cm = MagicMock()
        AppContext.get().connection_manager = mock_cm
        view._key_slots[3] = {
            "plugin_uuid": "p",
            "card_uuid": "a",
            "context_id": "c",
            "settings": {},
        }  # pylint: disable=protected-access

        view._on_key_long_pressed(3)  # pylint: disable=protected-access

        mock_cm.call.assert_called_once_with("key_simulate_long_press", "dev1", 3)
        assert_that(view._selected_key).is_none()  # pylint: disable=protected-access

    def test_grid_key_click_selects_and_the_play_button_presses(  # pylint: disable=unused-argument
        self, qapp, tmp_path
    ):
        view = _make_view(tmp_path)
        _active_dev1(view)
        mock_cm = MagicMock()
        AppContext.get().connection_manager = mock_cm
        view._key_slots[3] = {
            "plugin_uuid": "p",
            "card_uuid": "a",
            "context_id": "c",
            "settings": {},
        }  # pylint: disable=protected-access
        view._grid.build(_KEYS_6)  # pylint: disable=protected-access
        button = view._grid._buttons[3]  # pylint: disable=protected-access

        button.click()
        assert_that(view._selected_key).is_equal_to(3)  # pylint: disable=protected-access
        assert_that([c.args[0] for c in mock_cm.call.call_args_list]).does_not_contain("key_simulate_press")

        mock_cm.call.reset_mock()
        button._run_button.click()  # pylint: disable=protected-access
        mock_cm.call.assert_called_once_with("key_simulate_press", "dev1", 3)

    def test_key_long_pressed_simulates_long_press(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        _active_dev1(view)
        mock_cm = MagicMock()
        AppContext.get().connection_manager = mock_cm
        view._key_slots[1] = {
            "plugin_uuid": "p",
            "card_uuid": "a",
            "context_id": "c",
            "settings": {},
        }  # pylint: disable=protected-access
        view._on_key_long_pressed(1)  # pylint: disable=protected-access
        mock_cm.call.assert_called_once_with("key_simulate_long_press", "dev1", 1)

    def test_action_dropped_calls_key_assign_rpc(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        _active_dev1(view)
        mock_cm = MagicMock()
        mock_cm.call.return_value = True
        AppContext.get().connection_manager = mock_cm
        view._on_card_dropped(4, {"plugin_uuid": "com.p", "card_uuid": "com.p.a"})  # pylint: disable=protected-access
        assert_that(mock_cm.call.call_args[0][:4]).is_equal_to(("key_assign", "dev1", "p1", 4))
        assert_that(view._key_slots).contains_key(4)  # pylint: disable=protected-access

    @staticmethod
    def _preset_view(tmp_path, response):
        view = _make_view(tmp_path)
        _active_dev1(view)
        mock_cm = MagicMock()
        mock_cm.call.return_value = response
        AppContext.get().connection_manager = mock_cm
        view._config_panel = MagicMock()  # pylint: disable=protected-access
        return view, mock_cm

    _PRESET = {
        "plugin_uuid": "com.t.audio",
        "plugin_name": "Audio",
        "preset_uuid": "com.t.audio.mute",
        "preset_name": "Mute",
    }

    def test_dropping_a_preset_calls_key_assign_preset_and_selects_the_key(
        self, qapp, tmp_path
    ) -> None:  # pylint: disable=unused-argument
        view, cm = self._preset_view(tmp_path, {"ok": True, "errors": {}})

        view._on_card_dropped(2, self._PRESET)  # pylint: disable=protected-access

        args = cm.call.call_args[0]
        assert_that(args[:6]).is_equal_to(("key_assign_preset", "dev1", "p1", 2, "com.t.audio", "com.t.audio.mute"))
        assert_that(args[6]).is_not_empty()  # the fresh context id
        assert_that(view._select_after_refresh).is_equal_to(2)  # pylint: disable=protected-access
        cm.request_device_refresh.assert_called_once()

    def test_dropping_a_preset_with_errors_selects_the_key_on_the_press_tab(
        self, qapp, tmp_path
    ) -> None:  # pylint: disable=unused-argument
        view, _cm = self._preset_view(tmp_path, json.dumps({"ok": True, "errors": {"press.url": "required"}}))

        view._on_preset_dropped(2, self._PRESET)  # pylint: disable=protected-access
        view._key_slots[2] = {"plugin_uuid": "com.decker.core", "card_uuid": "com.decker.core.blank"}
        view._on_display_states_updated("dev1", [{"key_index": 2}])  # pylint: disable=protected-access

        view._config_panel.show_gesture_tab.assert_called_once_with("press")  # pylint: disable=protected-access

    def test_dropping_a_preset_without_errors_does_not_open_the_press_tab(
        self, qapp, tmp_path
    ) -> None:  # pylint: disable=unused-argument
        view, _cm = self._preset_view(tmp_path, {"ok": True, "errors": {}})

        view._on_preset_dropped(2, self._PRESET)  # pylint: disable=protected-access
        view._key_slots[2] = {"plugin_uuid": "com.decker.core", "card_uuid": "com.decker.core.blank"}
        view._on_display_states_updated("dev1", [{"key_index": 2}])  # pylint: disable=protected-access

        view._config_panel.show_gesture_tab.assert_not_called()  # pylint: disable=protected-access

    def test_moving_a_key_drops_a_pending_press_tab_request(
        self, qapp, tmp_path
    ) -> None:  # pylint: disable=unused-argument
        view, _cm = self._preset_view(tmp_path, {"ok": True, "errors": {"press.url": "required"}})
        view._on_preset_dropped(2, self._PRESET)  # pylint: disable=protected-access

        view._on_key_moved(2, 5, False)  # pylint: disable=protected-access
        view._key_slots[5] = {"plugin_uuid": "com.decker.core", "card_uuid": "com.decker.core.blank"}
        view._on_display_states_updated("dev1", [{"key_index": 5}])  # pylint: disable=protected-access

        view._config_panel.show_gesture_tab.assert_not_called()  # pylint: disable=protected-access

    def test_failed_preset_drop_changes_nothing(self, qapp, tmp_path) -> None:  # pylint: disable=unused-argument
        view, cm = self._preset_view(tmp_path, {"ok": False, "errors": {}})
        slot = {"plugin_uuid": "com.decker.core", "card_uuid": "com.decker.core.blank"}
        view._key_slots[2] = slot  # pylint: disable=protected-access

        view._on_card_dropped(2, self._PRESET)  # pylint: disable=protected-access

        assert_that(view._key_slots).is_equal_to({2: slot})  # pylint: disable=protected-access
        assert_that(view._select_after_refresh).is_none()  # pylint: disable=protected-access
        cm.request_device_refresh.assert_not_called()

    def test_preset_list_event_fills_the_sidebar(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        presets = [{"plugin_uuid": "p", "plugin_name": "P", "preset_uuid": "p.a", "preset_name": "Do"}]

        view.handle_event(PresetListUpdatedEvent(presets=presets))

        assert_that(view._sidebar.sections).is_equal_to([("P", ["Do"])])  # pylint: disable=protected-access

    def test_save_key_settings_calls_update_settings_rpc(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        _active_dev1(view)
        mock_cm = MagicMock()
        AppContext.get().connection_manager = mock_cm
        view._key_slots[2] = {
            "plugin_uuid": "p",
            "card_uuid": "a",
            "context_id": "c",
            "settings": {},
        }  # pylint: disable=protected-access
        view._save_key_settings(2, {"step": 5, "max": 10})  # pylint: disable=protected-access
        mock_cm.call.assert_called_once_with("key_update_settings", "dev1", "p1", 2, json.dumps({"step": 5, "max": 10}))

    def test_save_key_settings_reads_ok_from_the_json_result(
        self, qapp, tmp_path
    ) -> None:  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        _active_dev1(view)
        mock_cm = MagicMock()
        AppContext.get().connection_manager = mock_cm
        mock_cm.call.return_value = json.dumps({"ok": False, "errors": {"step": "is required"}})

        failed = view._save_key_settings(2, {"step": 5})  # pylint: disable=protected-access
        assert_that(failed.ok).is_false()
        assert_that(dict(failed.errors)).is_equal_to({"settings.step": "is required"})

        mock_cm.call.return_value = json.dumps({"ok": True, "errors": {}})
        assert_that(view._save_key_settings(2, {"step": 5}).ok).is_true()  # pylint: disable=protected-access

    def test_selecting_key_shows_schema_form_for_its_action(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        _active_dev1(view)
        AppContext.get().connection_manager = MagicMock()
        schema: list[dict[str, Any]] = [
            {"key": "step", "type": "int", "label": "Step", "default": 1, "min": 1, "max": 9}
        ]
        view.handle_event(
            CardListUpdatedEvent(
                cards=[{"plugin_uuid": "com.p", "card_uuid": "com.p.a", "card_name": "A", "settings_schema": schema}]
            )
        )
        view._key_slots[3] = {  # pylint: disable=protected-access
            "plugin_uuid": "com.p",
            "card_uuid": "com.p.a",
            "context_id": "c",
            "settings": {"step": 4},
        }

        view._on_key_edit_requested(3)  # pylint: disable=protected-access

        panel = view._config_panel  # pylint: disable=protected-access
        assert_that(panel.currentIndex()).is_equal_to(1)
        assert_that(panel._settings_form.values()).is_equal_to({"step": 4})  # pylint: disable=protected-access
        assert panel._loaded is not None  # pylint: disable=protected-access
        assert_that(panel._loaded[4]).is_equal_to("com.p")  # pylint: disable=protected-access

    def test_selecting_another_key_with_identical_values_drops_unsaved_edits(  # pylint: disable=unused-argument
        self, qapp, tmp_path
    ):
        view = _make_view(tmp_path)
        _active_dev1(view)
        AppContext.get().connection_manager = MagicMock()
        schema: list[dict[str, Any]] = [
            {"key": "step", "type": "int", "label": "Step", "default": 1, "min": 1, "max": 9}
        ]
        view.handle_event(
            CardListUpdatedEvent(
                cards=[{"plugin_uuid": "com.p", "card_uuid": "com.p.a", "card_name": "A", "settings_schema": schema}]
            )
        )
        for key in (3, 4):
            view._key_slots[key] = {  # pylint: disable=protected-access
                "plugin_uuid": "com.p",
                "card_uuid": "com.p.a",
                "context_id": f"c{key}",
                "settings": {"step": 2},
            }
        view._on_key_edit_requested(3)  # pylint: disable=protected-access
        panel = view._config_panel  # pylint: disable=protected-access
        panel._settings_form._fields["step"].set(8)  # pylint: disable=protected-access

        view._on_key_edit_requested(4)  # pylint: disable=protected-access

        assert_that(panel._settings_form.values()).is_equal_to({"step": 2})  # pylint: disable=protected-access

    def test_save_calls_key_update_settings_and_key_update_appearance(  # pylint: disable=unused-argument
        self, qapp, tmp_path
    ):
        view = _make_view(tmp_path)
        _active_dev1(view)
        mock_cm = MagicMock()
        AppContext.get().connection_manager = mock_cm
        view._key_slots[2] = {  # pylint: disable=protected-access
            "plugin_uuid": "p",
            "card_uuid": "a",
            "context_id": "c",
            "settings": {},
            "appearance": {"background": "#000000"},
            "appearance_defaults": {"background": "#000000"},
        }

        view._make_save_callback(2)({"step": 2}, {"background": "#ff0000"}, {})  # pylint: disable=protected-access

        calls = [c.args for c in mock_cm.call.call_args_list]
        assert_that(calls).is_equal_to(
            [
                ("key_update_settings", "dev1", "p1", 2, json.dumps({"step": 2})),
                ("key_update_appearance", "dev1", "p1", 2, json.dumps({"background": "#ff0000"})),
            ]
        )

    def test_changed_bindings_are_sent_and_unchanged_ones_are_not(  # pylint: disable=unused-argument
        self, qapp, tmp_path
    ) -> None:
        view, mock_cm = self._refusing_view(tmp_path, "nothing", {})
        view._key_slots[2]["bindings"] = {"press": {"action": "a"}}  # pylint: disable=protected-access
        save = view._make_save_callback(2)  # pylint: disable=protected-access

        save({}, {}, {"press": {"action": "a"}})
        assert_that([c.args[0] for c in mock_cm.call.call_args_list]).does_not_contain("key_update_bindings")

        save({}, {}, {"press": {"action": "b"}})
        assert_that([c.args[0] for c in mock_cm.call.call_args_list]).contains("key_update_bindings")

    def _refusing_view(self, tmp_path, refused: str, errors: dict[str, str]) -> tuple[DashboardView, MagicMock]:
        view = _make_view(tmp_path)
        _active_dev1(view)
        mock_cm = MagicMock()

        def call(method: str, *_args: Any) -> Any:
            if method == refused:
                return json.dumps({"ok": False, "errors": errors})
            return json.dumps({"ok": True, "errors": {}})

        mock_cm.call.side_effect = call
        AppContext.get().connection_manager = mock_cm
        view._key_slots[2] = {  # pylint: disable=protected-access
            "plugin_uuid": "p",
            "card_uuid": "a",
            "context_id": "c",
            "settings": {},
            "appearance_defaults": {},
        }
        return view, mock_cm

    def test_refused_settings_stop_the_save_before_bindings_and_appearance(  # pylint: disable=unused-argument
        self, qapp, tmp_path
    ) -> None:
        view, mock_cm = self._refusing_view(tmp_path, "key_update_settings", {"url": "bad"})

        outcome = view._make_save_callback(2)(
            {"url": "x"}, {"background": "#fff"}, {}
        )  # pylint: disable=protected-access

        assert_that(outcome.ok).is_false()
        assert_that(dict(outcome.errors)).is_equal_to({"settings.url": "bad"})
        assert_that([c.args[0] for c in mock_cm.call.call_args_list]).is_equal_to(["key_update_settings"])

    def test_refused_bindings_prefix_errors_and_skip_appearance(  # pylint: disable=unused-argument
        self, qapp, tmp_path
    ) -> None:
        view, mock_cm = self._refusing_view(tmp_path, "key_update_bindings", {"press.url": "bad", "long_press": "no"})

        outcome = view._make_save_callback(2)(
            {}, {"background": "#fff"}, {"press": {"action": "x"}}
        )  # pylint: disable=protected-access

        assert_that(outcome.ok).is_false()
        assert_that(dict(outcome.errors)).is_equal_to({"bindings.press.url": "bad", "bindings.long_press": "no"})
        assert_that([c.args[0] for c in mock_cm.call.call_args_list]).is_equal_to(
            ["key_update_settings", "key_update_bindings"]
        )
        assert_that(view._key_slots[2].get("bindings")).is_none()  # pylint: disable=protected-access

    def test_saved_bindings_update_the_slot_and_state_sync_carries_them(  # pylint: disable=unused-argument
        self, qapp, tmp_path
    ) -> None:
        view, _cm = self._refusing_view(tmp_path, "nothing", {})
        bindings = {"press": {"action": "com.decker.core.nothing"}}

        view._make_save_callback(2)({}, {}, bindings)  # pylint: disable=protected-access
        assert_that(view._key_slots[2]["bindings"]).is_equal_to(bindings)  # pylint: disable=protected-access

        view._sync_slot(
            2, {"plugin_uuid": "p", "bindings": {"long_press": {"action": "q"}}}
        )  # pylint: disable=protected-access
        assert_that(view._key_slots[2]["bindings"]).is_equal_to(
            {"long_press": {"action": "q"}}
        )  # pylint: disable=protected-access


class TestPages:  # pylint: disable=protected-access
    def setup_method(self):
        AppContext._instance = None

    def _view(self, tmp_path) -> tuple[DashboardView, MagicMock]:
        view = _make_view(tmp_path)
        _active_dev1(view)
        view._pages = None
        cm = MagicMock()
        cm.call.return_value = True
        AppContext.get().connection_manager = cm
        view.handle_event(PagesUpdatedEvent(pages=_PAGES))
        cm.call.reset_mock()
        return view, cm

    def _tab_names(self, view: DashboardView) -> list[str]:
        tabs = view._page_tabs.tab_bar
        return [tabs.tabText(i) for i in range(tabs.count())]

    def test_pages_event_renders_tabs_with_active_and_default(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, _cm = self._view(tmp_path)

        view.handle_event(
            PagesUpdatedEvent(pages=dataclasses.replace(_PAGES, active_page_id="p2", default_page_id="p2"))
        )

        assert_that(self._tab_names(view)).is_equal_to(["Page 1", "Page 2"])
        assert_that(view._page_tabs.current_page_id).is_equal_to("p2")
        assert_that(view._active_page_id).is_equal_to("p2")

    def test_pages_for_other_device_are_ignored(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, _cm = self._view(tmp_path)

        view.handle_event(PagesUpdatedEvent(pages=dataclasses.replace(_PAGES, device_id="other", pages=[])))

        assert_that(self._tab_names(view)).is_equal_to(["Page 1", "Page 2"])

    def test_page_change_clears_key_selection(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, _cm = self._view(tmp_path)
        view._key_slots[0] = {"plugin_uuid": "p", "settings": {}}
        view._selected_key = 0

        view.handle_event(PagesUpdatedEvent(pages=dataclasses.replace(_PAGES, active_page_id="p2")))

        assert_that(view._key_slots).is_empty()
        assert_that(view._selected_key).is_none()

    def test_editing_a_key_outlines_it_and_a_page_change_clears_it(
        self, qapp, tmp_path
    ):  # pylint: disable=unused-argument
        view, _cm = self._view(tmp_path)
        view._grid.build(_KEYS_15)  # pylint: disable=protected-access
        view._key_slots[3] = {"plugin_uuid": "p", "card_uuid": "a", "context_id": "c", "settings": {}}

        view._on_key_edit_requested(3)  # pylint: disable=protected-access
        assert_that(view._grid._buttons[3].property("selected")).is_true()  # pylint: disable=protected-access

        view.handle_event(PagesUpdatedEvent(pages=dataclasses.replace(_PAGES, active_page_id="p2")))

        assert_that(view._grid._buttons[3].property("selected")).is_false()  # pylint: disable=protected-access

    def test_selecting_tab_activates_page_on_server(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)

        view._page_tabs.tab_bar.setCurrentIndex(1)
        view._bridge.join()

        cm.call.assert_called_once_with("page_activate", "dev1", "p2")

    def test_failed_activation_restores_selected_tab(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)
        cm.call.return_value = False

        view._page_tabs.tab_bar.setCurrentIndex(1)
        view._bridge.join()
        qapp.processEvents()

        assert_that(view._page_tabs.current_page_id).is_equal_to("p1")

    def test_add_page_creates_then_activates_it(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)
        cm.call.side_effect = lambda method, *args: (
            json.dumps({"page_id": "p3", "name": "Page 3"}) if method == "page_add" else True
        )

        view._page_tabs.add_page_requested.emit()

        assert_that([c.args for c in cm.call.call_args_list]).is_equal_to(
            [("page_add", "dev1"), ("page_activate", "dev1", "p3")]
        )

    def test_add_page_failure_does_not_activate(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)
        cm.call.return_value = None

        view._page_tabs.add_page_requested.emit()

        cm.call.assert_called_once_with("page_add", "dev1")

    def test_delete_asks_first_and_deletes_on_yes(self, qapp, tmp_path, monkeypatch):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)
        asked: list[str] = []

        def _confirm(_parent, name):
            asked.append(name)
            return True

        monkeypatch.setattr("decker.client.views.dashboard._confirm_delete_page", _confirm)

        view._page_tabs.delete_page_requested.emit("p2")

        assert_that(asked).is_equal_to(["Page 2"])
        cm.call.assert_called_once_with("page_delete", "dev1", "p2")

    def test_delete_cancelled_does_nothing(self, qapp, tmp_path, monkeypatch):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)
        monkeypatch.setattr("decker.client.views.dashboard._confirm_delete_page", lambda *_: False)

        view._page_tabs.delete_page_requested.emit("p2")

        cm.call.assert_not_called()

    def test_confirm_dialog_maps_yes_and_cancel(self, qapp, tmp_path, monkeypatch):  # pylint: disable=unused-argument
        answers = iter([QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Cancel])
        monkeypatch.setattr("decker.client.views.dashboard.QMessageBox.question", lambda *_args: next(answers))

        assert_that(_confirm_delete_page(_make_view(tmp_path), "Page 1")).is_true()
        assert_that(_confirm_delete_page(_make_view(tmp_path), "Page 1")).is_false()

    def test_rename_and_set_default_call_server(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)

        view._page_tabs.rename_page_requested.emit("p2", "Media")
        view._page_tabs.set_default_page_requested.emit("p2")

        assert_that([c.args for c in cm.call.call_args_list]).is_equal_to(
            [("page_rename", "dev1", "p2", "Media"), ("page_set_default", "dev1", "p2")]
        )

    def test_page_calls_need_device_and_connection(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)
        AppContext.get().connection_manager = None

        view._page_tabs.set_default_page_requested.emit("p2")

        cm.call.assert_not_called()

    def test_switching_device_clears_tabs_and_requests_refresh(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)
        view._active_device_id = "dev1"
        AppContext.get().window = MagicMock()

        view._on_device_selected(_D2)

        assert_that(view._page_tabs.tab_bar.count()).is_equal_to(0)
        cm.request_device_refresh.assert_called_once()

    def test_key_actions_need_known_page(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)
        view._pages = None

        view._on_card_dropped(0, {"plugin_uuid": "com.p", "card_uuid": "com.p.a"})
        view._save_key_settings(0, {})

        cm.call.assert_not_called()


class TestSlotSync:  # pylint: disable=protected-access
    def setup_method(self):
        AppContext._instance = None

    def test_full_state_tracks_assigned_and_drops_unassigned(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        _active_dev1(view)
        view._key_slots[1] = {"plugin_uuid": "stale", "settings": {}}

        view.handle_event(
            KeyDisplayStatesUpdatedEvent(
                device_id="dev1",
                states=[
                    {"key_index": 0, "plugin_uuid": "com.p", "card_uuid": "a", "context_id": "c", "settings": {}},
                    {"key_index": 1, "plugin_uuid": None, "settings": {}},
                ],
            )
        )

        assert_that(view._key_slots).contains_key(0).does_not_contain_key(1)

    def test_partial_update_keeps_slot(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        _active_dev1(view)
        view._key_slots[0] = {"plugin_uuid": "com.p", "settings": {"step": 1}}

        view.handle_event(
            KeyDisplayStatesUpdatedEvent(device_id="dev1", states=[{"key_index": 0, "image_b64": None, "label": "7"}])
        )

        assert_that(view._key_slots[0]["settings"]).is_equal_to({"step": 1})


class TestKeyEditorWiring:  # pylint: disable=protected-access
    def setup_method(self):
        AppContext._instance = None  # pylint: disable=protected-access

    def _edit(self, tmp_path):
        view = _make_view(tmp_path)
        _active_dev1(view)
        cm = MagicMock()
        cm.call.return_value = "preview-b64"
        AppContext.get().connection_manager = cm
        view.handle_event(
            CardListUpdatedEvent(
                cards=[
                    {
                        "plugin_uuid": "com.p",
                        "card_uuid": "com.p.a",
                        "card_name": "Counter",
                        "settings_schema": [],
                    }
                ]
            )
        )
        view._key_slots[3] = {  # pylint: disable=protected-access
            "plugin_uuid": "com.p",
            "card_uuid": "com.p.a",
            "context_id": "c",
            "settings": {},
        }
        view._on_key_edit_requested(3)  # pylint: disable=protected-access
        return view, cm

    def test_header_shows_the_action_name(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, _cm = self._edit(tmp_path)

        assert_that(view._config_panel._header.text()).is_equal_to("Counter")

    def test_preview_provider_calls_key_preview_for_the_selected_key(  # pylint: disable=unused-argument
        self, qapp, tmp_path
    ):
        view, cm = self._edit(tmp_path)

        result = view._config_panel._preview_provider({"background": "#ff0000"})

        assert_that(result).is_equal_to("preview-b64")
        cm.call.assert_called_with("key_preview", "dev1", "p1", 3, json.dumps({"background": "#ff0000"}))

    def test_display_state_image_updates_the_preview_of_a_clean_selected_key(  # pylint: disable=unused-argument
        self, qapp, tmp_path
    ):
        view, _cm = self._edit(tmp_path)
        shown: list[str | None] = []
        view._config_panel.set_preview_image = shown.append

        view._on_display_states_updated("dev1", [{"key_index": 3, "image_b64": "TElWRQ=="}])
        view._on_display_states_updated("dev1", [{"key_index": 4, "image_b64": "T1RIRVI="}])

        assert_that(shown).is_equal_to(["TElWRQ=="])

    def test_display_state_image_does_not_replace_a_draft_preview(  # pylint: disable=unused-argument
        self, qapp, tmp_path
    ):
        view, _cm = self._edit(tmp_path)
        panel = view._config_panel
        panel._appearance_form._fields["title"].set({"text": "draft"})
        panel._on_edited()
        shown: list[str | None] = []
        panel.set_preview_image = shown.append

        panel._preview_timer.stop()

        view._on_display_states_updated("dev1", [{"key_index": 3, "image_b64": "TElWRQ=="}])

        assert_that(shown).is_empty()
        assert_that(panel._preview_timer.isActive()).is_true()  # recomposed over the new plugin image


class TestFinalReviewFixes:  # pylint: disable=protected-access
    def setup_method(self):
        AppContext._instance = None  # pylint: disable=protected-access

    def _view(self, tmp_path):
        view = _make_view(tmp_path)
        _active_dev1(view)
        cm = MagicMock()
        cm.call.return_value = ""
        AppContext.get().connection_manager = cm
        view.handle_event(
            CardListUpdatedEvent(
                cards=[{"plugin_uuid": "com.p", "card_uuid": "com.p.a", "card_name": "A", "settings_schema": []}]
            )
        )
        return view

    def _assign(self, view, key):
        view._key_slots[key] = {
            "plugin_uuid": "com.p",
            "card_uuid": "com.p.a",
            "context_id": f"c{key}",
            "settings": {},
        }

    def test_empty_key_shows_the_placeholder_without_an_outline(
        self, qapp, tmp_path
    ):  # pylint: disable=unused-argument
        view = self._view(tmp_path)

        view._on_key_edit_requested(2)

        assert_that(view._config_panel.currentIndex()).is_equal_to(0)
        assert_that(view._grid._buttons[2].property("selected")).is_false()

    def test_outline_returns_when_the_selected_key_gets_an_action(
        self, qapp, tmp_path
    ):  # pylint: disable=unused-argument
        view = self._view(tmp_path)
        view._on_key_edit_requested(2)

        self._assign(view, 2)
        view._refresh_config_panel()

        assert_that(view._grid._buttons[2].property("selected")).is_true()

    def test_outline_clears_when_the_edited_key_is_unassigned(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = self._view(tmp_path)
        self._assign(view, 2)
        view._on_key_edit_requested(2)
        assert_that(view._grid._buttons[2].property("selected")).is_true()

        view._key_slots.pop(2)
        view._refresh_config_panel()

        assert_that(view._grid._buttons[2].property("selected")).is_false()

    def test_device_switch_clears_the_outline(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = self._view(tmp_path)
        self._assign(view, 2)
        view._on_key_edit_requested(2)
        other = DeviceHardwareInfo(id="dev2", type="Stream Deck", vendor_id=0, product_id=0, num_keys=6, keys=_KEYS_6)

        view._refresh_grid_for_device(other)

        assert_that(view._selected_key).is_none()
        assert_that([b.property("selected") for b in view._grid._buttons.values()]).does_not_contain(True)

    def test_unpreviewable_key_does_not_show_the_previous_keys_picture(  # pylint: disable=unused-argument
        self, qapp, tmp_path
    ):
        view = self._view(tmp_path)
        self._assign(view, 0)
        self._assign(view, 1)
        view._on_display_states_updated("dev1", [{"key_index": 0, "image_b64": "TElWRQ=="}])
        view._on_key_edit_requested(0)
        shown: list[str | None] = []
        panel = view._config_panel
        setattr(panel._preview, "set_image", shown.append)

        view._on_key_edit_requested(1)  # no image of its own and the preview RPC returns ""

        assert_that(shown[-1]).is_none()

    def test_switching_to_a_key_seeds_the_preview_with_its_own_image(
        self, qapp, tmp_path
    ):  # pylint: disable=unused-argument
        view = self._view(tmp_path)
        self._assign(view, 0)
        self._assign(view, 1)
        view._on_display_states_updated("dev1", [{"key_index": 1, "image_b64": "T1RIRVI="}])
        shown: list[str | None] = []
        setattr(view._config_panel._preview, "set_image", shown.append)

        view._on_key_edit_requested(1)

        assert_that(shown).contains("T1RIRVI=")


class TestSaveResult:  # pylint: disable=protected-access
    def setup_method(self):
        AppContext._instance = None  # pylint: disable=protected-access

    def _view(self, tmp_path, result):
        view = _make_view(tmp_path)
        _active_dev1(view)
        cm = MagicMock()
        cm.call.return_value = result
        AppContext.get().connection_manager = cm
        view._key_slots[2] = {"plugin_uuid": "p", "card_uuid": "a", "context_id": "c", "settings": {}}
        return view

    def test_save_callback_reports_success(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = self._view(tmp_path, True)

        assert_that(view._make_save_callback(2)({"step": 2}, {}, {}).ok).is_true()

    @pytest.mark.parametrize("result", [False, None])
    def test_save_callback_reports_failure(self, qapp, tmp_path, result):  # pylint: disable=unused-argument
        view = self._view(tmp_path, result)

        assert_that(view._make_save_callback(2)({"step": 2}, {}, {}).ok).is_false()
        assert_that(view._key_slots[2]["settings"]).is_equal_to({})


_PROFILES = DeviceProfiles(
    device_id="dev1", profiles=[ProfileInfo(0, "Default", ""), ProfileInfo(2, "Work", "")], active_profile_id=0
)


def _open_dialog(view: DashboardView) -> ProfilesDialog:
    assert view._profiles_dialog is not None  # pylint: disable=protected-access
    return view._profiles_dialog  # pylint: disable=protected-access


class TestProfiles:  # pylint: disable=protected-access
    def setup_method(self):
        AppContext._instance = None

    def _view(self, tmp_path) -> tuple[DashboardView, MagicMock]:
        view = _make_view(tmp_path)
        _active_dev1(view)
        cm = MagicMock()
        cm.call.return_value = True
        AppContext.get().connection_manager = cm
        view.handle_event(PagesUpdatedEvent(pages=_PAGES))
        view.handle_event(ProfilesUpdatedEvent(profiles=_PROFILES))
        cm.call.reset_mock()
        return view, cm

    def test_selector_follows_the_servers_profiles(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, _cm = self._view(tmp_path)
        selector = view._page_tabs.profile_selector

        assert_that(selector.isVisibleTo(view._page_tabs)).is_true()
        assert_that(selector.text()).is_equal_to("Default")
        assert_that(selector.popup_list.count()).is_equal_to(2)

    def test_selector_is_hidden_until_profiles_arrive(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)

        assert_that(view._page_tabs.profile_selector.isVisibleTo(view._page_tabs)).is_false()

    def test_profiles_of_another_device_are_ignored(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, _cm = self._view(tmp_path)

        view.handle_event(
            ProfilesUpdatedEvent(profiles=DeviceProfiles("other", [ProfileInfo(5, "Zed", "")], active_profile_id=5))
        )

        assert_that(view._page_tabs.profile_selector.text()).is_equal_to("Default")

    def test_selecting_activates_on_the_server(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)

        view._page_tabs.profile_selector.profile_selected.emit(2)

        cm.call.assert_called_once_with("profile_activate", "dev1", 2)

    def test_add_creates_then_activates(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)
        cm.call.side_effect = lambda method, *args: (
            json.dumps({"profile_id": 3, "name": "Profile 3", "icon": ""}) if method == "profile_add" else True
        )

        view._page_tabs.profile_selector.add_requested.emit()

        assert_that([c.args for c in cm.call.call_args_list]).is_equal_to(
            [("profile_add", "dev1"), ("profile_activate", "dev1", 3)]
        )

    def test_add_failure_does_not_activate(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)
        cm.call.return_value = None

        view._page_tabs.profile_selector.add_requested.emit()

        cm.call.assert_called_once_with("profile_add", "dev1")

    def test_rename_calls_the_server(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)

        view._page_tabs.profile_selector.rename_requested.emit(2, "Games")

        cm.call.assert_called_once_with("profile_rename", "dev1", 2, "Games")

    def test_edit_profile_entry_opens_profiles_dialog_on_that_profile(self, qapp, tmp_path):
        view, _cm = self._view(tmp_path)

        view._page_tabs.profile_selector.edit_requested.emit(2)

        dialog = _open_dialog(view)
        assert_that(dialog).is_not_none()
        assert_that(dialog.isVisible()).is_true()
        assert_that(dialog.isModal()).is_false()
        assert_that(dialog._name_edit.text()).is_equal_to("Work")
        dialog.close()

    def test_disconnect_resets_an_open_window_pick(self, qapp, tmp_path):
        view, _cm = self._view(tmp_path)
        view._page_tabs.profile_selector.edit_requested.emit(2)
        dialog = _open_dialog(view)
        view.handle_event(DetectionStatusEvent("active", "", "kwin", True, None))
        dialog._spy_button.click()
        spy = dialog._spy
        assert spy is not None
        spy._pick_button.click()
        assert_that(spy._picking).is_true()

        view.on_server_disconnected()

        assert_that(spy._picking).is_false()
        spy.close()
        dialog.close()

    def test_menu_edit_profiles_opens_the_dialog_on_the_active_profile(self, qapp, tmp_path):
        view, _cm = self._view(tmp_path)
        menu = QMenu()
        view._populate_view_menu(menu)
        action = next(a for a in menu.actions() if a.text() == "Edit Profiles…")

        assert_that(action.isEnabled()).is_true()
        action.trigger()

        dialog = _open_dialog(view)
        assert_that(dialog).is_not_none()
        assert_that(dialog._name_edit.text()).is_equal_to("Default")
        dialog.close()

    def test_menu_ends_with_about(self, qapp, tmp_path):
        view, _cm = self._view(tmp_path)
        menu = QMenu()
        view._populate_view_menu(menu)

        assert_that(menu.actions()[-1].text()).is_equal_to("About Decker…")

    def test_edit_profile_reuses_the_open_dialog(self, qapp, tmp_path):
        view, _cm = self._view(tmp_path)

        view._page_tabs.profile_selector.edit_requested.emit(2)
        first = _open_dialog(view)
        view._page_tabs.profile_selector.edit_requested.emit(0)

        assert_that(view._profiles_dialog).is_same_as(first)
        assert_that(first._name_edit.text()).is_equal_to("Default")
        first.close()

    def test_dialog_profile_actions_go_through_page_call(self, qapp, tmp_path):
        view, cm = self._view(tmp_path)
        view._page_tabs.profile_selector.edit_requested.emit(2)

        _open_dialog(view)._clone_button.click()

        cm.call.assert_called_with("profile_clone", "dev1", 2)
        _open_dialog(view).close()

    def test_dialog_follows_rule_status_and_profile_events(self, qapp, tmp_path):
        view, _cm = self._view(tmp_path)
        view._page_tabs.profile_selector.edit_requested.emit(2)
        dialog = _open_dialog(view)
        rule = AppRule("r1", "Web", True, [MatchCondition("window_class", "equals", "x")], 0)

        view.handle_event(AppRulesUpdatedEvent(rules=DeviceRules("dev1", [rule], DefaultRule())))
        view.handle_event(DetectionStatusEvent("active", "", "kwin", True, None))
        view.handle_event(
            ProfilesUpdatedEvent(profiles=DeviceProfiles("dev1", [ProfileInfo(2, "Renamed", "")], active_profile_id=2))
        )

        assert_that(dialog._rule_list.count()).is_equal_to(1)
        assert_that(dialog._status is not None and dialog._status.state == "active").is_true()
        assert_that(dialog._name_edit.text()).is_equal_to("Renamed")
        dialog.close()

    def test_rules_and_status_seen_before_opening_are_shown(self, qapp, tmp_path):
        view, _cm = self._view(tmp_path)
        rule = AppRule("r1", "Web", True, [MatchCondition("window_class", "equals", "x")], 0)
        view.handle_event(AppRulesUpdatedEvent(rules=DeviceRules("dev1", [rule], DefaultRule())))
        view.handle_event(DetectionStatusEvent("active", "", "kwin", True, None))

        view._page_tabs.profile_selector.edit_requested.emit(2)

        assert_that(_open_dialog(view)._rule_list.count()).is_equal_to(1)
        status = _open_dialog(view)._status
        assert status is not None
        assert_that(status.backend).is_equal_to("kwin")
        _open_dialog(view).close()

    def test_pages_for_the_dialog_come_from_pages_list_of_that_profile(self, qapp, tmp_path):
        view, cm = self._view(tmp_path)
        cm.call.return_value = json.dumps({"pages": [{"page_id": "w1", "name": "Mail"}]})

        pages = view._pages_for_profile(2)

        cm.call.assert_called_once_with("pages_list", "dev1", 2)
        assert_that(pages).is_equal_to([PageInfo("w1", "Mail")])

    def test_failed_pages_lookup_is_none_and_not_cached(self, qapp, tmp_path):
        view, cm = self._view(tmp_path)
        cm.call.return_value = None

        assert_that(view._pages_for_profile(2)).is_none()
        cm.call.return_value = json.dumps({"pages": [{"page_id": "w1", "name": "Mail"}]})
        assert_that(view._pages_for_profile(2)).is_equal_to([PageInfo("w1", "Mail")])

    def _count_lookups(self, view, cm) -> int:
        cm.call.reset_mock()
        cm.call.return_value = json.dumps({"pages": [{"page_id": "w1", "name": "Mail"}]})
        view._pages_for_profile(2)
        view._pages_for_profile(2)
        view._pages_for_profile(0)
        return len([c for c in cm.call.call_args_list if c.args[0] == "pages_list"])

    def test_pages_lookup_is_cached_per_profile(self, qapp, tmp_path):
        view, cm = self._view(tmp_path)

        assert_that(self._count_lookups(view, cm)).is_equal_to(2)

    @pytest.mark.parametrize(
        "event",
        [PagesUpdatedEvent(pages=_PAGES), ProfilesUpdatedEvent(profiles=_PROFILES)],
    )
    def test_pages_cache_is_invalidated_by_server_events(self, qapp, tmp_path, event):
        view, cm = self._view(tmp_path)
        self._count_lookups(view, cm)

        view.handle_event(event)

        assert_that(self._count_lookups(view, cm)).is_equal_to(2)

    def test_pages_cache_is_dropped_on_device_switch(self, qapp, tmp_path):
        view, cm = self._view(tmp_path)
        self._count_lookups(view, cm)

        view._on_device_selected(_D2)

        assert_that(view._pages_cache).is_empty()

    def test_switching_device_closes_the_dialog(self, qapp, tmp_path):
        view, _cm = self._view(tmp_path)
        view._page_tabs.profile_selector.edit_requested.emit(2)
        dialog = _open_dialog(view)

        view._on_device_selected(_D2)

        assert_that(dialog.isVisible()).is_false()
        assert_that(view._profiles_dialog).is_none()

    def test_switching_device_hides_the_selector(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, _cm = self._view(tmp_path)

        view._on_device_selected(_D2)

        assert_that(view._page_tabs.profile_selector.isVisibleTo(view._page_tabs)).is_false()


class TestKeyMoveAndCopy:  # pylint: disable=protected-access
    def setup_method(self):
        AppContext._instance = None

    def _view(self, tmp_path, result=True) -> tuple[DashboardView, MagicMock]:
        view = _make_view(tmp_path)
        _active_dev1(view)
        view._pages = _PAGES
        cm = MagicMock()
        cm.call.return_value = result
        AppContext.get().connection_manager = cm
        view._grid.build(_KEYS_15)
        for index in (1, 4):
            view._key_slots[index] = {"plugin_uuid": "p", "card_uuid": "a", "context_id": f"c{index}", "settings": {}}
        return view, cm

    def test_a_drop_moves_through_the_server_and_selects_the_target(
        self, qapp, tmp_path
    ):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)

        view._grid.key_moved.emit(1, 3, False)

        cm.call.assert_called_once_with("key_move", "dev1", "p1", 1, 3)
        cm.request_device_refresh.assert_called_once_with()
        assert_that(view._selected_key).is_none()  # nothing stale is shown while the server's data is on its way
        assert_that(view._key_slots).is_empty()
        view.handle_event(KeyDisplayStatesUpdatedEvent(device_id="dev1", states=[_assigned_state(3)]))
        assert_that(view._selected_key).is_equal_to(3)

    def test_ctrl_drop_copies(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)

        view._grid.key_moved.emit(1, 3, True)

        cm.call.assert_called_once_with("key_copy", "dev1", "p1", 1, 3)
        view.handle_event(KeyDisplayStatesUpdatedEvent(device_id="dev1", states=[_assigned_state(3)]))
        assert_that(view._selected_key).is_equal_to(3)

    @pytest.mark.parametrize("result", [False, None])
    def test_a_failed_call_changes_nothing(self, qapp, tmp_path, result):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path, result)
        view._selected_key = 4

        view._grid.key_moved.emit(1, 3, False)

        assert_that(view._selected_key).is_equal_to(4)
        cm.request_device_refresh.assert_not_called()

    def test_nothing_is_called_without_a_device_page_or_connection(
        self, qapp, tmp_path
    ):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)
        view._pages = None
        view._grid.key_moved.emit(1, 3, False)
        view._pages = _PAGES
        AppContext.get().connection_manager = None
        view._grid.key_moved.emit(1, 3, False)
        AppContext.reset()
        view._grid.key_moved.emit(1, 3, False)

        cm.call.assert_not_called()

    def test_only_assigned_keys_are_draggable(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, _cm = self._view(tmp_path)

        view._sync_draggable()

        assert_that([i for i, b in view._grid._buttons.items() if b._draggable]).is_equal_to([1, 4])

    def test_resetting_the_selection_clears_draggability(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, _cm = self._view(tmp_path)
        view._sync_draggable()

        view._reset_key_selection()

        assert_that([i for i, b in view._grid._buttons.items() if b._draggable]).is_empty()


def _assigned_state(key_index: int) -> dict[str, Any]:
    return {
        "key_index": key_index,
        "plugin_uuid": "p",
        "card_uuid": "a",
        "context_id": f"c{key_index}",
        "settings": {},
    }


class TestKeyMoveSelection:  # pylint: disable=protected-access
    def setup_method(self):
        AppContext._instance = None

    def _view(self, tmp_path):
        view = _make_view(tmp_path)
        _active_dev1(view)
        view._pages = _PAGES
        cm = MagicMock()
        cm.call.return_value = True
        AppContext.get().connection_manager = cm
        view._grid.build(_KEYS_15)
        view._key_slots[1] = {"plugin_uuid": "p", "card_uuid": "a", "context_id": "c1", "settings": {}}
        return view, cm

    def test_the_selection_waits_for_the_target_to_come_back_from_the_server(
        self, qapp, tmp_path
    ):  # pylint: disable=unused-argument
        view, _cm = self._view(tmp_path)
        view._selected_key = 3  # the replaced key was open in the editor

        view._grid.key_moved.emit(1, 3, False)

        assert_that(view._selected_key).is_none()
        view.handle_event(KeyDisplayStatesUpdatedEvent(device_id="dev1", states=[_assigned_state(1)]))
        assert_that(view._selected_key).is_none()  # key 3 has not come back yet
        view.handle_event(KeyDisplayStatesUpdatedEvent(device_id="dev1", states=[_assigned_state(3)]))
        assert_that(view._selected_key).is_equal_to(3)

    def test_a_failed_move_does_not_arm_a_selection(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)
        cm.call.return_value = False

        view._grid.key_moved.emit(1, 3, False)
        view.handle_event(KeyDisplayStatesUpdatedEvent(device_id="dev1", states=[_assigned_state(3)]))

        assert_that(view._selected_key).is_none()


class TestKeyClear:  # pylint: disable=protected-access
    def setup_method(self):
        AppContext._instance = None

    def _view(self, tmp_path) -> tuple[DashboardView, MagicMock]:
        view = _make_view(tmp_path)
        _active_dev1(view)
        view._pages = _PAGES
        cm = MagicMock()
        cm.call.return_value = True
        AppContext.get().connection_manager = cm
        view._grid.build(_KEYS_15)
        view._key_slots[4] = {"plugin_uuid": "p", "card_uuid": "a", "context_id": "c4", "settings": {}}
        return view, cm

    def test_a_confirmed_clear_unassigns_the_key_and_closes_its_editor(
        self, qapp, tmp_path
    ):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)
        view._selected_key = 4

        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            view._grid.key_delete_requested.emit(4)

        cm.call.assert_called_once_with("key_unassign", "dev1", "p1", 4)
        cm.request_device_refresh.assert_called_once_with()
        assert_that(view._key_slots).does_not_contain_key(4)
        assert_that(view._selected_key).is_none()

    def test_a_declined_clear_sends_nothing(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)

        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
            view._grid.key_delete_requested.emit(4)

        cm.call.assert_not_called()
        assert_that(view._key_slots).contains_key(4)

    def test_a_key_of_an_unknown_card_is_listed_selectable_and_deletable(
        self, qapp, tmp_path
    ):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)
        view._key_slots.clear()
        state = {"key_index": 6, "label": "", "plugin_uuid": "com.gone", "card_uuid": "com.gone.x", "context_id": "c6"}
        view.handle_event(KeyDisplayStatesUpdatedEvent(device_id="dev1", states=[state]))
        view._selected_key = 6

        view._refresh_config_panel()  # an unknown card still opens the editor (empty schema)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            view._grid.key_delete_requested.emit(6)

        assert_that(view._key_slots).does_not_contain_key(6)
        cm.call.assert_any_call("key_unassign", "dev1", "p1", 6)

    def test_an_empty_key_is_not_cleared(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view, cm = self._view(tmp_path)

        with patch.object(QMessageBox, "question") as question:
            view._grid.key_delete_requested.emit(2)

        question.assert_not_called()
        cm.call.assert_not_called()


class TestDragScopeFollowsThePage:  # pylint: disable=protected-access
    def setup_method(self):
        AppContext._instance = None

    def test_the_grids_scope_is_the_active_device_and_page(self, qapp, tmp_path):  # pylint: disable=unused-argument
        view = _make_view(tmp_path)
        _active_dev1(view)
        view._grid.build(_KEYS_15)

        view.handle_event(PagesUpdatedEvent(pages=_PAGES))
        assert_that(view._grid._scope).is_equal_to("dev1/p1")

        view.handle_event(PagesUpdatedEvent(pages=dataclasses.replace(_PAGES, active_page_id="p2")))
        assert_that(view._grid._scope).is_equal_to("dev1/p2")


class TestKeyAnimations:  # pylint: disable=protected-access
    @pytest.fixture(autouse=True)
    def _qt(self, qapp):  # pylint: disable=unused-argument
        AppContext._instance = None  # pylint: disable=protected-access

    def _view(self, tmp_path):
        """A view on dev1 whose animation fetches wait in ``jobs`` until the test runs them."""
        view = _make_view(tmp_path)
        _active_dev1(view)
        cm = MagicMock()
        cm.call.return_value = "QU5JTQ=="
        AppContext.get().connection_manager = cm
        jobs: list[Any] = []
        view._bridge.submit = lambda work, done: jobs.append((work, done))  # type: ignore[method-assign]
        view._grid.set_key_animation = MagicMock()  # type: ignore[method-assign]
        return view, cm, jobs

    @staticmethod
    def _state(index: int, animation_id: str | None) -> dict[str, Any]:
        return {"key_index": index, "image_b64": "UE9TVEVS", "label": "", "animation_id": animation_id}

    @staticmethod
    def _run(jobs: list[Any]) -> None:
        while jobs:
            work, done = jobs.pop(0)
            done(work())

    def test_fetches_an_animation_once_per_animation_id(self, tmp_path):
        view, cm, jobs = self._view(tmp_path)

        view._on_display_states_updated("dev1", [self._state(2, "a1")])
        self._run(jobs)
        view._on_display_states_updated("dev1", [self._state(2, "a1")])
        self._run(jobs)
        assert_that(cm.call.call_args_list).is_equal_to([call("key_animation", "a1")])

        view._on_display_states_updated("dev1", [self._state(2, "a2")])
        self._run(jobs)
        assert_that(cm.call.call_args_list).is_equal_to([call("key_animation", "a1"), call("key_animation", "a2")])
        view._grid.set_key_animation.assert_called_with(2, "QU5JTQ==")

    def test_fetch_does_not_block_the_caller(self, tmp_path):
        view, cm, jobs = self._view(tmp_path)

        view._on_display_states_updated("dev1", [self._state(2, "a1")])

        cm.call.assert_not_called()
        assert_that(jobs).is_length(1)

    def test_a_cached_animation_is_not_refetched_after_a_page_switch(self, tmp_path):
        view, cm, jobs = self._view(tmp_path)
        view._on_display_states_updated("dev1", [self._state(2, "a1")])
        self._run(jobs)

        view.handle_event(PagesUpdatedEvent(pages=dataclasses.replace(_PAGES, device_id="dev1", active_page_id="p2")))
        view._grid.set_key_animation.assert_called_with(2, None)
        view.handle_event(PagesUpdatedEvent(pages=_PAGES))
        view._on_display_states_updated("dev1", [self._state(2, "a1")])

        assert_that(cm.call.call_count).is_equal_to(1)
        assert_that(jobs).is_empty()
        view._grid.set_key_animation.assert_called_with(2, "QU5JTQ==")

    def test_state_without_animation_id_clears_the_animation(self, tmp_path):
        view, _cm, jobs = self._view(tmp_path)
        view._on_display_states_updated("dev1", [self._state(2, "a1")])
        self._run(jobs)

        view._on_display_states_updated("dev1", [self._state(2, None)])

        view._grid.set_key_animation.assert_called_with(2, None)

    def test_an_empty_result_is_remembered_and_not_requested_again(self, tmp_path):
        view, cm, jobs = self._view(tmp_path)
        cm.call.return_value = ""

        view._on_display_states_updated("dev1", [self._state(2, "bad")])
        self._run(jobs)
        view._on_display_states_updated("dev1", [self._state(2, "bad")])
        view._on_display_states_updated("dev1", [self._state(2, None)])
        view._on_display_states_updated("dev1", [self._state(2, "bad")])

        assert_that(cm.call.call_count).is_equal_to(1)
        assert_that(jobs).is_empty()
        view._grid.set_key_animation.assert_called_with(2, "")

    def test_a_failed_call_is_retried_on_the_next_state_event(self, tmp_path):
        view, cm, jobs = self._view(tmp_path)
        cm.call.return_value = None
        view._on_display_states_updated("dev1", [self._state(2, "a1")])
        self._run(jobs)
        cm.call.return_value = "QU5JTQ=="

        view._on_display_states_updated("dev1", [self._state(2, "a1")])
        self._run(jobs)

        assert_that(cm.call.call_count).is_equal_to(2)
        view._grid.set_key_animation.assert_called_with(2, "QU5JTQ==")

    def test_a_stale_result_for_an_id_the_key_no_longer_has_is_dropped(self, tmp_path):
        view, cm, jobs = self._view(tmp_path)
        cm.call.side_effect = lambda _method, animation_id: f"{animation_id}-b64"
        view._on_display_states_updated("dev1", [self._state(2, "a1")])
        view._on_display_states_updated("dev1", [self._state(2, "a2")])

        self._run(jobs)  # a1's reply arrives after the key moved on to a2

        applied = [c.args for c in view._grid.set_key_animation.call_args_list]
        assert_that(applied).is_equal_to([(2, "a2-b64")])

    def test_a_result_for_a_key_that_left_the_page_is_dropped(self, tmp_path):
        view, _cm, jobs = self._view(tmp_path)
        view._on_display_states_updated("dev1", [self._state(2, "a1")])

        view.handle_event(PagesUpdatedEvent(pages=dataclasses.replace(_PAGES, active_page_id="p2")))
        view._grid.set_key_animation.reset_mock()
        self._run(jobs)

        view._grid.set_key_animation.assert_not_called()

    def test_states_for_another_device_fetch_nothing(self, tmp_path):
        view, _cm, jobs = self._view(tmp_path)

        view._on_display_states_updated("other", [self._state(2, "a1")])

        assert_that(jobs).is_empty()

    def test_without_a_connection_nothing_is_fetched(self, tmp_path):
        view, _cm, jobs = self._view(tmp_path)
        AppContext.get().connection_manager = None

        view._on_display_states_updated("dev1", [self._state(2, "a1")])

        assert_that(jobs).is_empty()

    def _show(self, view, jobs, key_index: int, animation_id: str) -> None:
        view._on_display_states_updated("dev1", [self._state(key_index, animation_id)])
        self._run(jobs)

    def test_cache_evicts_least_recently_used_and_keeps_a_cache_hit(self, tmp_path, monkeypatch):
        monkeypatch.setattr("decker.client.views.dashboard._ANIMATION_CACHE_BYTES", 12)
        view, cm, jobs = self._view(tmp_path)
        cm.call.side_effect = lambda _method, animation_id: f"{animation_id}----"  # 6 bytes each
        for animation_id in ("a1", "a2", "a1", "a3"):  # the third step is a cache hit that refreshes a1
            self._show(view, jobs, 2, animation_id)

        assert_that(list(view._animation_cache)).is_equal_to(["a1", "a3"])
        assert_that(cm.call.call_count).is_equal_to(3)

    def test_an_evicted_id_is_refetched_once_and_ids_in_use_are_not_refetched(self, tmp_path, monkeypatch):
        monkeypatch.setattr("decker.client.views.dashboard._ANIMATION_CACHE_BYTES", 10)
        view, cm, jobs = self._view(tmp_path)
        cm.call.side_effect = lambda _method, animation_id: f"{animation_id}----"
        self._show(view, jobs, 2, "a1")
        self._show(view, jobs, 3, "a2")  # over the cap, but both are on keys: nothing may be evicted
        assert_that(list(view._animation_cache)).is_equal_to(["a1", "a2"])
        self._show(view, jobs, 2, "a3")  # a1 is no longer in use and goes
        assert_that(list(view._animation_cache)).is_equal_to(["a2", "a3"])

        view._on_display_states_updated("dev1", [self._state(3, "a2")])  # still in use: no refetch
        assert_that(cm.call.call_count).is_equal_to(3)
        self._show(view, jobs, 2, "a1")  # evicted: fetched again, exactly once
        view._on_display_states_updated("dev1", [self._state(2, "a1")])
        assert_that(cm.call.call_count).is_equal_to(4)

    def test_empty_results_survive_eviction_pressure_from_big_entries(self, tmp_path, monkeypatch):
        monkeypatch.setattr("decker.client.views.dashboard._ANIMATION_CACHE_BYTES", 5)
        view, cm, jobs = self._view(tmp_path)
        cm.call.side_effect = lambda _method, animation_id: "" if animation_id == "bad" else f"{animation_id}----"
        for animation_id in ("bad", "a1", "a2", "a3"):
            self._show(view, jobs, 2, animation_id)
        calls = cm.call.call_count

        self._show(view, jobs, 2, "bad")

        assert_that(cm.call.call_count).is_equal_to(calls)

    def test_empty_results_are_bounded_by_count(self, tmp_path, monkeypatch):
        monkeypatch.setattr("decker.client.views.dashboard._EMPTY_CACHE_LIMIT", 2)
        view, cm, jobs = self._view(tmp_path)
        cm.call.return_value = ""
        for animation_id in ("e1", "e2", "e3"):
            self._show(view, jobs, 2, animation_id)

        assert_that(list(view._empty_animations)).is_equal_to(["e2", "e3"])

    def test_a_raising_call_still_completes_and_unsticks_the_id(self, tmp_path):
        view, cm, _jobs = self._view(tmp_path)
        view._bridge = CallBridge(view)  # the real bridge: it must deliver an exception to the done callback
        cm.call.side_effect = RuntimeError("rpc down")

        view._on_display_states_updated("dev1", [self._state(2, "a1")])
        view._bridge.join()
        QTest.qWait(50)  # type: ignore[call-arg,arg-type]

        assert_that(view._animations_pending).is_empty()
        assert_that(view._key_animation_ids).is_empty()
        cm.call.side_effect = None
        cm.call.return_value = "QU5JTQ=="
        view._on_display_states_updated("dev1", [self._state(2, "a1")])  # retried on the next event
        view._bridge.join()
        QTest.qWait(50)  # type: ignore[call-arg,arg-type]
        view._grid.set_key_animation.assert_called_with(2, "QU5JTQ==")
