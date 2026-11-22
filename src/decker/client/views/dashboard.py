"""Dashboard view — the main working surface: sidebar, page tabs, key grid, config panel."""

import json
import logging
import uuid
from collections import OrderedDict
from typing import Any

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QActionGroup, QIcon
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QMenu,
    QMessageBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from decker.client.call_bridge import CallBridge
from decker.client.context import AppContext
from decker.client.events import (
    ActionListUpdatedEvent,
    AppEvent,
    AppRulesUpdatedEvent,
    CardListUpdatedEvent,
    DetectionStatusEvent,
    DeviceListUpdatedEvent,
    KeyDisplayStatesUpdatedEvent,
    PagesUpdatedEvent,
    ProfilesUpdatedEvent,
    WindowFocusedEvent,
    WindowPickedEvent,
)
from decker.client.ui_state_store import UiStateStore
from decker.client.views.base import BaseView
from decker.client.widgets.bindings_edit import BindingsSpec
from decker.client.widgets.button_grid import ButtonGridWidget
from decker.client.widgets.card_config_panel import (
    CardConfigPanel,
    SaveCallback,
    SaveOutcome,
)
from decker.client.widgets.icon_edit import IconEdit
from decker.client.widgets.page_tab_bar import PageTabBar
from decker.client.widgets.profiles_dialog import ProfilesDialog
from decker.client.widgets.sidebar import SidebarWidget
from decker.common.model import (
    DeviceHardwareInfo,
    DevicePages,
    DeviceProfiles,
    DeviceRules,
    PageInfo,
)

_DEFAULT_SIDEBAR_WIDTH = 240

_SPLITTER_MAIN_KEY = "splitter_main"
_SPLITTER_CONTENT_KEY = "splitter_content"
_SIDEBAR_COLLAPSED_KEY = "sidebar_collapsed"
# Fetched animations are base64 blobs of up to a few MiB each; 32 MiB holds the animations of several pages of keys
# while keeping a long-lived view (e.g. a clock key with a new id every cycle) from growing without limit.
_ANIMATION_CACHE_BYTES = 32 * 1024 * 1024
_EMPTY_CACHE_LIMIT = 256  # animation ids the server had no image for; tiny, so bounded by count only


# Events only the dashboard consumes; MainWindow forwards them to it even while another view is active.
DASHBOARD_EVENTS: tuple[type, ...] = (
    AppRulesUpdatedEvent,
    DetectionStatusEvent,
    WindowFocusedEvent,
    WindowPickedEvent,
)


class DashboardView(BaseView):  # pylint: disable=too-many-instance-attributes
    """Sidebar | (page tabs + key grid / config panel), with persisted splitter layout."""

    def __init__(self, ui_state_store: UiStateStore | None = None) -> None:
        super().__init__()
        self._ui_state_store = ui_state_store or UiStateStore()
        self._build_ui()

    def _build_ui(self):
        self.resize(900, 700)
        self._last_expanded_sidebar_width = _DEFAULT_SIDEBAR_WIDTH
        self._pages: DevicePages | None = None  # the active device's pages, from the server
        self._profiles: DeviceProfiles | None = None  # the active device's profiles, from the server
        self._rules: DeviceRules | None = None  # the active device's app rules, from the server
        self._detection: DetectionStatusEvent | None = None  # the server's window detection status
        self._pages_cache: dict[int, list[PageInfo]] = {}  # profile id -> its pages, for the profiles dialog
        self._profiles_dialog: ProfilesDialog | None = None  # the open non-modal profiles and rules dialog
        self._devices: list[DeviceHardwareInfo] = []
        self._selected_key: int | None = None
        self._key_images: dict[int, str] = {}  # last composed image per key, to seed the editor preview
        self._key_slots: dict[int, dict[str, Any]] = {}  # key_index → assignment metadata
        self._select_after_refresh: int | None = None  # the key to open once its data arrives after a move or copy
        self._active_device_id: str | None = None
        self._cards: list[dict[str, Any]] = []  # card list from the server (with settings schemas)
        self._bridge = CallBridge(self)  # runs the animation fetches and page switches off the GUI thread
        self._refresh_panel_timer = QTimer(self)  # one panel rebuild per burst of key-state events
        self._refresh_panel_timer.setSingleShot(True)
        self._refresh_panel_timer.setInterval(0)
        self._refresh_panel_timer.timeout.connect(self._refresh_config_panel)
        self._key_animation_ids: dict[int, str] = {}  # key index → its applied (or being fetched) animation id
        self._animation_cache: OrderedDict[str, str] = OrderedDict()  # id → base64 image, least recently used first
        self._empty_animations: OrderedDict[str, None] = OrderedDict()  # ids the server had no image for
        self._animations_pending: set[str] = set()  # animation ids with a fetch in flight

        # UI sub-components
        self._sidebar = SidebarWidget()  # Panel with plugins and other menu items
        self._page_tabs = PageTabBar()
        self._grid = ButtonGridWidget()  # Main button view
        self._config_panel = CardConfigPanel()  # Button configuration

        self._content_splitter = QSplitter(Qt.Orientation.Vertical)
        self._content_splitter.addWidget(self._grid)
        self._content_splitter.addWidget(self._config_panel)
        self._content_splitter.setChildrenCollapsible(False)

        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self._page_tabs)
        content_layout.addWidget(self._content_splitter)

        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.addWidget(self._sidebar)
        self._main_splitter.addWidget(content_widget)
        self._main_splitter.setCollapsible(0, True)
        self._main_splitter.setCollapsible(1, False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._main_splitter)

        self._main_splitter.resize(self.size())
        self._content_splitter.resize(self._main_splitter.size())

        self._wire_signals()
        self._render_page_tabs()
        self._restore_state()

    def on_activate(self) -> None:
        pass

    def on_deactivate(self) -> None:
        pass

    def handle_event(self, event: AppEvent) -> bool | None:  # pylint: disable=useless-return
        if isinstance(event, DeviceListUpdatedEvent):
            self._on_devices_updated(event.devices)
        elif isinstance(event, CardListUpdatedEvent):
            self._cards = event.cards
            self._sidebar.set_cards(event.cards)
            self._refresh_config_panel()
        elif isinstance(event, ActionListUpdatedEvent):
            self._sidebar.set_actions(event.actions)
        elif isinstance(event, KeyDisplayStatesUpdatedEvent):
            self._on_display_states_updated(event.device_id, event.states)
        elif isinstance(event, PagesUpdatedEvent):
            self._on_pages_updated(event.pages)
        elif isinstance(event, ProfilesUpdatedEvent):
            self._on_profiles_updated(event.profiles)
        elif isinstance(event, AppRulesUpdatedEvent):
            self._on_rules_updated(event.rules)
        elif isinstance(event, DetectionStatusEvent):
            self._detection = event
            if self._profiles_dialog is not None:
                self._profiles_dialog.set_status(event)
        elif isinstance(event, WindowFocusedEvent) and self._profiles_dialog is not None:
            self._profiles_dialog.on_focused(event.window)
        elif isinstance(event, WindowPickedEvent) and self._profiles_dialog is not None:
            self._profiles_dialog.on_picked(event.window)
        return None

    def on_server_disconnected(self) -> None:
        if self._profiles_dialog is not None:
            self._profiles_dialog.on_disconnected()

    def save_state(self) -> None:
        """Persist splitter sizes and sidebar-collapsed state to config.db."""
        self._ui_state_store.set(_SPLITTER_MAIN_KEY, self._main_splitter.sizes())
        self._ui_state_store.set(_SPLITTER_CONTENT_KEY, self._content_splitter.sizes())
        self._ui_state_store.set(_SIDEBAR_COLLAPSED_KEY, 1 if self._is_sidebar_collapsed() else 0)

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def _wire_signals(self) -> None:
        self._page_tabs.menu_about_to_show.connect(self._populate_view_menu)
        self._page_tabs.page_selected.connect(self._on_page_selected)
        self._page_tabs.add_page_requested.connect(self._on_add_page)
        self._page_tabs.delete_page_requested.connect(self._on_delete_page)
        self._page_tabs.rename_page_requested.connect(self._on_rename_page)
        self._page_tabs.set_default_page_requested.connect(self._on_set_default_page)
        selector = self._page_tabs.profile_selector
        selector.profile_selected.connect(self._on_profile_selected)
        selector.add_requested.connect(self._on_add_profile)
        selector.edit_requested.connect(self._on_edit_profile)
        selector.rename_requested.connect(self._on_rename_profile)
        self._grid.key_moved.connect(self._on_key_moved)
        self._grid.key_pressed.connect(self._on_key_pressed)
        self._grid.key_edit_requested.connect(self._on_key_edit_requested)
        self._grid.key_long_pressed.connect(self._on_key_long_pressed)
        self._grid.card_dropped.connect(self._on_card_dropped)

    # ------------------------------------------------------------------
    # Persisted layout state
    # ------------------------------------------------------------------

    def _restore_state(self) -> None:
        splitter_main = self._ui_state_store.get(_SPLITTER_MAIN_KEY)
        if splitter_main:
            self._main_splitter.setSizes(splitter_main)
            if splitter_main[0] > 0:
                self._last_expanded_sidebar_width = splitter_main[0]
        splitter_content = self._ui_state_store.get(_SPLITTER_CONTENT_KEY)
        if splitter_content:
            self._content_splitter.setSizes(splitter_content)
        if self._ui_state_store.get(_SIDEBAR_COLLAPSED_KEY):
            self._collapse_sidebar()

    def _is_sidebar_collapsed(self) -> bool:
        return self._main_splitter.sizes()[0] == 0

    def _toggle_sidebar(self) -> None:
        if self._is_sidebar_collapsed():
            self._expand_sidebar()
        else:
            self._collapse_sidebar()

    def _collapse_sidebar(self) -> None:
        sizes = self._main_splitter.sizes()
        if sizes[0] > 0:
            self._last_expanded_sidebar_width = sizes[0]
        self._main_splitter.setSizes([0, sum(sizes)])

    def _expand_sidebar(self) -> None:
        sizes = self._main_splitter.sizes()
        total = sum(sizes)
        width = min(self._last_expanded_sidebar_width, total)
        self._main_splitter.setSizes([width, total - width])

    # ------------------------------------------------------------------
    # Devices and key grid
    # ------------------------------------------------------------------

    def _on_devices_updated(self, devices: list[DeviceHardwareInfo]) -> None:
        self._devices = devices
        active_device = AppContext.get().active_device
        if active_device is None and devices:
            active_device = devices[0]
            window = AppContext.get().window
            if window is not None:
                window.set_active_device(active_device)
        self._refresh_grid_for_device(active_device)

    def _refresh_grid_for_device(self, device: DeviceHardwareInfo | None) -> None:
        device_id = device.id if device is not None else None
        if device_id == self._active_device_id:
            return
        self._active_device_id = device_id
        self._reset_key_selection()
        self._clear_animations()
        self._grid.build(device.keys if device is not None else [])
        self._pages = None
        self._profiles = None
        self._rules = None
        self._pages_cache = {}
        self._close_profiles_dialog()
        self._render_page_tabs()
        self._render_profiles()
        cm = AppContext.get().connection_manager
        if cm is not None and device_id is not None:
            cm.request_device_refresh()

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    @property
    def _active_page_id(self) -> str | None:
        return self._pages.active_page_id if self._pages is not None else None

    def _on_pages_updated(self, pages: DevicePages) -> None:
        if pages.device_id != AppContext.get().active_device_id:
            return
        if pages.active_page_id != self._active_page_id:
            self._reset_key_selection()
            self._clear_animations()
        self._pages = pages
        self._pages_cache = {}
        self._sync_draggable()
        self._render_page_tabs()
        if self._profiles_dialog is not None:
            self._profiles_dialog.refresh()

    def _clear_animations(self) -> None:
        """Stop every key's movie (its key left the page); fetched animations stay cached."""
        for key_index in self._key_animation_ids:
            self._grid.set_key_animation(key_index, None)
        self._key_animation_ids.clear()

    def _sync_animation(self, key_index: int, animation_id: str | None) -> None:
        """Start, keep or stop the animation of a key of the visible page, fetching it only the first time."""
        if not animation_id:
            if self._key_animation_ids.pop(key_index, None) is not None:
                self._grid.set_key_animation(key_index, None)
            return
        if self._key_animation_ids.get(key_index) == animation_id:
            return
        self._key_animation_ids[key_index] = animation_id
        cached = self._cached_animation(animation_id)
        if cached is None:
            self._fetch_animation(animation_id)
        else:
            self._grid.set_key_animation(key_index, cached)

    def _cached_animation(self, animation_id: str) -> str | None:
        """The cached image for an id ("" when the server had none), refreshing its recency; None if unknown."""
        if animation_id in self._empty_animations:
            self._empty_animations.move_to_end(animation_id)
            return ""
        if animation_id in self._animation_cache:
            self._animation_cache.move_to_end(animation_id)
            return self._animation_cache[animation_id]
        return None

    def _remember_animation(self, animation_id: str, image_b64: str) -> None:
        if not image_b64:
            self._empty_animations[animation_id] = None
            self._empty_animations.move_to_end(animation_id)
            while len(self._empty_animations) > _EMPTY_CACHE_LIMIT:
                self._empty_animations.popitem(last=False)
            return
        self._animation_cache[animation_id] = image_b64
        self._animation_cache.move_to_end(animation_id)
        in_use = set(self._key_animation_ids.values())
        while sum(len(blob) for blob in self._animation_cache.values()) > _ANIMATION_CACHE_BYTES:
            victim = next((cached for cached in self._animation_cache if cached not in in_use), None)
            if victim is None:
                break
            del self._animation_cache[victim]

    def _fetch_animation(self, animation_id: str) -> None:
        cm = AppContext.get().connection_manager
        if cm is None or animation_id in self._animations_pending:
            return
        self._animations_pending.add(animation_id)
        self._bridge.submit(
            lambda: cm.call("key_animation", animation_id),
            lambda result: self._on_animation_fetched(animation_id, result),
        )

    def _on_animation_fetched(self, animation_id: str, result: Any) -> None:
        self._animations_pending.discard(animation_id)
        keys = [index for index, applied in self._key_animation_ids.items() if applied == animation_id]
        if not isinstance(result, str):  # the call failed: forget it so the next state event asks again
            for index in keys:
                del self._key_animation_ids[index]
            return
        self._remember_animation(animation_id, result)
        for index in keys:  # a key that changed or left the page since the request no longer has this id
            self._grid.set_key_animation(index, result)

    def _render_page_tabs(self) -> None:
        if self._pages is None:
            self._page_tabs.set_pages([])
            return
        self._page_tabs.set_pages(self._pages.pages, self._pages.active_page_id, self._pages.default_page_id)

    def _page_call_async(self, method: str, *args: Any) -> None:
        """Like _page_call but off the GUI thread: a page or profile switch waits for the hardware redraw."""
        device_id = AppContext.get().active_device_id
        cm = AppContext.get().connection_manager
        if not device_id or cm is None:
            return
        self._bridge.submit(lambda: cm.call(method, device_id, *args), lambda _result: None)

    def _page_call(self, method: str, *args: Any) -> Any:
        """Call a page RPC for the active device; the server's pages_changed push refreshes the tabs."""
        device_id = AppContext.get().active_device_id
        cm = AppContext.get().connection_manager
        if not device_id or cm is None:
            return None
        return cm.call(method, device_id, *args)

    def _on_page_selected(self, page_id: str) -> None:
        if page_id == self._active_page_id:
            return
        device_id = AppContext.get().active_device_id
        cm = AppContext.get().connection_manager
        if not device_id or cm is None:
            self._render_page_tabs()
            return

        def done(result: Any) -> None:
            if not result:
                self._render_page_tabs()  # undo the tab bar's optimistic selection

        self._bridge.submit(lambda: cm.call("page_activate", device_id, page_id), done)

    def _on_add_page(self) -> None:
        raw = self._page_call("page_add")
        try:
            page_id = json.loads(raw)["page_id"] if raw else None
        except (ValueError, TypeError, KeyError):
            page_id = None
        if page_id:
            self._page_call("page_activate", page_id)

    def _on_delete_page(self, page_id: str) -> None:
        page = next((p for p in self._pages.pages if p.page_id == page_id), None) if self._pages else None
        if page is not None and _confirm_delete_page(self, page.name):
            self._page_call("page_delete", page_id)

    def _on_rename_page(self, page_id: str, name: str) -> None:
        self._page_call("page_rename", page_id, name)

    def _on_set_default_page(self, page_id: str) -> None:
        self._page_call("page_set_default", page_id)

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    def _on_profiles_updated(self, profiles: DeviceProfiles) -> None:
        if profiles.device_id != AppContext.get().active_device_id:
            return
        self._profiles = profiles
        self._pages_cache = {}
        self._render_profiles()
        if self._profiles_dialog is not None:
            self._profiles_dialog.set_profiles(profiles)

    def _on_rules_updated(self, rules: DeviceRules) -> None:
        if rules.device_id != AppContext.get().active_device_id:
            return
        self._rules = rules
        if self._profiles_dialog is not None:
            self._profiles_dialog.set_rules(rules)

    def _render_profiles(self) -> None:
        selector = self._page_tabs.profile_selector
        if self._profiles is None:
            selector.hide()
            return
        selector.set_profiles(self._profiles.profiles, self._profiles.active_profile_id, _profile_icon)
        selector.show()

    def _on_profile_selected(self, profile_id: int) -> None:
        self._page_call_async("profile_activate", profile_id)

    def _on_add_profile(self) -> None:
        raw = self._page_call("profile_add")
        try:
            profile_id = json.loads(raw)["profile_id"] if raw else None
        except (ValueError, TypeError, KeyError):
            profile_id = None
        if isinstance(profile_id, int):
            self._page_call("profile_activate", profile_id)

    def _on_rename_profile(self, profile_id: int, name: str) -> None:
        self._page_call("profile_rename", profile_id, name)

    def _on_edit_active_profile(self) -> None:
        if self._profiles is not None:
            self._on_edit_profile(self._profiles.active_profile_id)

    def _on_edit_profile(self, profile_id: int) -> None:
        cm = AppContext.get().connection_manager
        profiles = self._profiles
        if profiles is None or cm is None:
            return
        if self._profiles_dialog is None:
            dialog = ProfilesDialog(self._page_call, cm, self._pages_for_profile, parent=self, icon_for=_profile_icon)
            dialog.set_profiles(profiles)
            if self._rules is not None:
                dialog.set_rules(self._rules)
            if self._detection is not None:
                dialog.set_status(self._detection)
            self._profiles_dialog = dialog
        self._profiles_dialog.select_profile(profile_id)
        self._profiles_dialog.show()
        self._profiles_dialog.raise_()
        self._profiles_dialog.activateWindow()

    def _close_profiles_dialog(self) -> None:
        dialog, self._profiles_dialog = self._profiles_dialog, None
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()

    def _pages_for_profile(self, profile_id: int) -> list[PageInfo] | None:
        """A profile's pages (cached until the server reports a change); None when the lookup failed."""
        if profile_id in self._pages_cache:
            return self._pages_cache[profile_id]
        raw = self._page_call("pages_list", profile_id)
        try:
            pages = [PageInfo(str(p["page_id"]), str(p["name"])) for p in json.loads(raw)["pages"]] if raw else None
        except (ValueError, TypeError, KeyError):
            pages = None
        if pages is not None:
            self._pages_cache[profile_id] = pages
        return pages

    def _reset_key_selection(self) -> None:
        self._key_slots = {}
        self._key_images = {}
        self._selected_key = None
        self._select_after_refresh = None
        self._sync_draggable()
        self._grid.set_selected(None)
        self._config_panel.show_placeholder()

    # ------------------------------------------------------------------
    # View menu (⚙ button)
    # ------------------------------------------------------------------

    def _populate_view_menu(self, menu: QMenu) -> None:
        menu.clear()
        menu.addAction("Show Sidebar" if self._is_sidebar_collapsed() else "Hide Sidebar", self._toggle_sidebar)
        menu.addMenu(self._build_device_menu(menu))
        menu.addSeparator()
        edit = menu.addAction("Edit Profiles…", self._on_edit_active_profile)
        if edit is not None:
            edit.setEnabled(self._profiles is not None)
        menu.addAction("Settings", self._open_settings)

    def _build_device_menu(self, parent: QMenu) -> QMenu:
        device_menu = QMenu("Device", parent)
        if not self._devices:
            empty = device_menu.addAction("No devices connected")
            if empty is not None:
                empty.setEnabled(False)
            return device_menu
        group = QActionGroup(device_menu)
        active_id = AppContext.get().active_device_id
        for device in self._devices:
            action = device_menu.addAction(f"{device.type}  ·  {device.serial or device.id}")
            if action is None:
                continue
            action.setCheckable(True)
            action.setChecked(device.id == active_id)
            action.setActionGroup(group)
            action.triggered.connect(lambda _checked=False, d=device: self._on_device_selected(d))
        return device_menu

    def _on_device_selected(self, device: DeviceHardwareInfo) -> None:
        window = AppContext.get().window
        if window is not None:
            window.set_active_device(device)
        self._refresh_grid_for_device(device)

    # ------------------------------------------------------------------
    # Key grid interactions
    # ------------------------------------------------------------------

    def _on_key_edit_requested(self, index: int) -> None:
        if index != self._selected_key:
            self._config_panel.forget_loaded()
            self._config_panel.set_preview_image(self._key_images.get(index))
        self._selected_key = index
        self._refresh_config_panel()

    def _sync_draggable(self) -> None:
        """Only keys with a card can be dragged, and drags are tagged with the device and page they start on."""
        self._grid.set_draggable_keys(self._key_slots.keys())
        device_id = AppContext.get().active_device_id
        page_id = self._active_page_id
        self._grid.set_drag_scope(f"{device_id}/{page_id}" if device_id and page_id else "")

    def _on_key_moved(self, from_index: int, to_index: int, copy: bool) -> None:
        """Move (or, with Ctrl, copy) a key's card; the destination key is selected afterwards."""
        device_id = AppContext.get().active_device_id
        page_id = self._active_page_id
        cm = AppContext.get().connection_manager
        if not device_id or not page_id or cm is None:
            return
        if not cm.call("key_copy" if copy else "key_move", device_id, page_id, from_index, to_index):
            return
        # nothing stale stays on screen: the editor reopens on the target once the server's data for it arrives
        self._reset_key_selection()
        self._select_after_refresh = to_index
        cm.request_device_refresh()

    def _on_key_pressed(self, index: int) -> None:
        device_id = AppContext.get().active_device_id
        if device_id and index in self._key_slots:
            cm = AppContext.get().connection_manager
            if cm is not None:
                self._bridge.submit(lambda: cm.call("key_simulate_press", device_id, index), lambda _result: None)

    def _on_key_long_pressed(self, index: int) -> None:
        device_id = AppContext.get().active_device_id
        if not device_id or index not in self._key_slots:
            return
        cm = AppContext.get().connection_manager
        if cm is not None:
            self._bridge.submit(lambda: cm.call("key_simulate_long_press", device_id, index), lambda _result: None)

    def _on_card_dropped(self, key_index: int, card_data: Any) -> None:
        device_id = AppContext.get().active_device_id
        page_id = self._active_page_id
        if not device_id or not page_id:
            return
        plugin_uuid = str(card_data.get("plugin_uuid", ""))
        card_uuid = str(card_data.get("card_uuid", ""))
        if not plugin_uuid or not card_uuid:
            return
        context_id = uuid.uuid4().hex
        settings: dict[str, Any] = {}
        cm = AppContext.get().connection_manager
        if cm is None:
            return
        ok = cm.call(
            "key_assign",
            device_id,
            page_id,
            key_index,
            plugin_uuid,
            card_uuid,
            context_id,
            json.dumps(settings),
        )
        if ok:
            self._key_slots[key_index] = {
                "plugin_uuid": plugin_uuid,
                "card_uuid": card_uuid,
                "context_id": context_id,
                "settings": settings,
            }
            self._grid.set_key_label(key_index, "0")
            self._sync_draggable()
            logging.debug("Assigned %s/%s to key %d", plugin_uuid, card_uuid, key_index)

    def _on_display_states_updated(self, device_id: str, states: list[dict[str, Any]]) -> None:
        if device_id != AppContext.get().active_device_id:
            return
        for state in states:
            key_index = int(state.get("key_index", -1))
            if key_index < 0:
                continue
            image_b64: str | None = state.get("image_b64")
            self._grid.set_key_image(key_index, image_b64)
            if image_b64:
                self._key_images[key_index] = image_b64
            else:
                self._key_images.pop(key_index, None)
            if key_index == self._selected_key and image_b64:
                if self._config_panel.is_dirty:
                    self._config_panel.refresh_preview_soon()  # recompose the draft over the plugin's new image
                else:
                    self._config_panel.set_preview_image(image_b64)
            if not image_b64:
                self._grid.set_key_label(key_index, str(state.get("label", "")))
            self._sync_slot(key_index, state)
            self._sync_animation(key_index, state.get("animation_id"))
        pending = self._select_after_refresh
        if pending is not None and pending in self._key_slots:
            self._select_after_refresh = None
            self._on_key_edit_requested(pending)
        self._refresh_panel_timer.start()

    def _refresh_config_panel(self) -> None:
        index = self._selected_key
        if index is None:
            return
        slot = self._key_slots.get(index)
        if slot is None:
            self._grid.set_selected(None)
            self._config_panel.show_placeholder()
            return
        self._grid.set_selected(index)
        card = next(
            (
                a
                for a in self._cards
                if a.get("plugin_uuid") == slot.get("plugin_uuid") and a.get("card_uuid") == slot.get("card_uuid")
            ),
            None,
        )
        self._config_panel.set_preview_provider(lambda appearance: self._preview_key(index, appearance))
        self._config_panel.show_card_config(
            list((card or {}).get("settings_schema") or []),
            slot.get("settings", {}),
            slot.get("appearance", {}),
            slot.get("appearance_defaults", {}),
            self._make_save_callback(index),
            title=str((card or {}).get("card_name") or ""),
            plugin_id=str(slot.get("plugin_uuid") or ""),
            bindings=BindingsSpec(dict((card or {}).get("bindings") or {}), dict(slot.get("bindings") or {})),
        )

    def _sync_slot(self, key_index: int, state: dict[str, Any]) -> None:
        """Keep slot metadata in sync with a full key state; partial (image/label) updates carry none."""
        if "plugin_uuid" not in state:
            return
        if not state.get("plugin_uuid"):
            self._key_slots.pop(key_index, None)
            self._sync_draggable()
            return
        self._key_slots[key_index] = {
            "plugin_uuid": str(state.get("plugin_uuid", "")),
            "card_uuid": str(state.get("card_uuid", "")),
            "context_id": str(state.get("context_id", "")),
            "settings": dict(state.get("settings") or {}),
            "appearance": dict(state.get("appearance") or {}),
            "appearance_defaults": dict(state.get("appearance_defaults") or {}),
            "bindings": dict(state.get("bindings") or {}),
        }
        self._sync_draggable()

    def _make_save_callback(self, key_index: int) -> SaveCallback:
        def _cb(settings: dict[str, Any], appearance: dict[str, Any], bindings: dict[str, Any]) -> SaveOutcome:
            errors: dict[str, str] = {}
            for save in (
                lambda: self._save_key_settings(key_index, settings),
                lambda: self._save_key_bindings(key_index, bindings),
                lambda: SaveOutcome(self._save_key_appearance(key_index, appearance)),
            ):
                outcome = save()
                errors.update(outcome.errors)
                if not outcome.ok:  # a refused save stops the rest, so nothing after it is written
                    return SaveOutcome(False, errors)
            return SaveOutcome(True, errors)

        return _cb

    def _save_key_settings(self, key_index: int, settings: dict[str, Any]) -> SaveOutcome:
        outcome = self._key_save_result("key_update_settings", key_index, settings)
        if outcome.ok and key_index in self._key_slots:
            self._key_slots[key_index]["settings"] = settings
        return outcome

    def _save_key_bindings(self, key_index: int, bindings: dict[str, Any]) -> SaveOutcome:
        slot = self._key_slots.get(key_index)
        if slot is not None and bindings == slot.get("bindings", {}):
            return SaveOutcome(True)  # unchanged: a kept-but-refused binding must not block other edits
        outcome = self._key_save_result("key_update_bindings", key_index, bindings, "bindings")
        if outcome.ok and key_index in self._key_slots:
            self._key_slots[key_index]["bindings"] = bindings
        return outcome

    def _save_key_appearance(self, key_index: int, appearance: dict[str, Any]) -> bool:
        saved = bool(self._key_call("key_update_appearance", key_index, appearance))
        if saved and key_index in self._key_slots:
            slot = self._key_slots[key_index]
            slot["appearance"] = {**slot.get("appearance_defaults", {}), **appearance}
        return saved

    def _preview_key(self, key_index: int, appearance: dict[str, Any]) -> str | None:
        device_id = AppContext.get().active_device_id
        page_id = self._active_page_id
        cm = AppContext.get().connection_manager
        if not device_id or not page_id or cm is None:
            return None
        result = cm.call("key_preview", device_id, page_id, key_index, json.dumps(appearance))
        return result if isinstance(result, str) else None

    def _key_call(self, method: str, key_index: int, payload: dict[str, Any]) -> Any:
        device_id = AppContext.get().active_device_id
        page_id = self._active_page_id
        cm = AppContext.get().connection_manager
        if not device_id or not page_id or cm is None:
            return None
        return cm.call(method, device_id, page_id, key_index, json.dumps(payload))

    def _key_save_result(
        self, method: str, key_index: int, payload: dict[str, Any], prefix: str = "settings"
    ) -> SaveOutcome:
        result = self._key_call(method, key_index, payload)
        if not isinstance(result, str):
            return SaveOutcome(bool(result))
        data = json.loads(result)
        errors = {f"{prefix}.{key}": str(msg) for key, msg in (data.get("errors") or {}).items()}
        return SaveOutcome(bool(data.get("ok")), errors)

    # ------------------------------------------------------------------
    # Other widget callbacks
    # ------------------------------------------------------------------

    def _open_settings(self) -> None:
        window = AppContext.get().window
        if window is not None:
            window.open_settings()


def _profile_icon(ref: str, color: str = "") -> QIcon | None:
    """The dropdown icon for a profile's icon ref and fill colour, or None to fall back to the Decker icon."""
    edit = IconEdit()
    edit.set_value({"ref": ref, "color": color or "#ffffff"})
    pixmap = edit.thumbnail()
    return None if pixmap.isNull() else QIcon(pixmap)


def _confirm_delete_page(parent: QWidget, name: str) -> bool:
    """Ask before deleting a page, since its key assignments are deleted with it."""
    answer = QMessageBox.question(
        parent,
        "Delete page",
        f"Delete page “{name}” and all of its key assignments?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        QMessageBox.StandardButton.Cancel,
    )
    return answer == QMessageBox.StandardButton.Yes
