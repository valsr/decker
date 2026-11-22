import base64
import json
from pathlib import Path
from typing import Any

from assertpy import assert_that
from PyQt6.QtCore import QPoint, QSize, Qt
from PyQt6.QtWidgets import QListView, QVBoxLayout

from decker.client.card_icons import card_pixmap
from decker.client.widgets.sidebar import (
    DRAG_ICON_SIZE,
    ICON_SIZE,
    SidebarWidget,
    _DraggableCardList,
)

_ACTIONS = [
    {"plugin_uuid": "p", "plugin_name": "P", "card_uuid": "p.show", "card_name": "Show Clock"},
    {"plugin_uuid": "p", "plugin_name": "P", "card_uuid": "p.toggle", "card_name": "Toggle Light"},
]


def _first_list(sidebar):  # pylint: disable=protected-access
    return sidebar._sections[0].card_list


def _count(sidebar):  # pylint: disable=protected-access
    return sum(s.card_list.count() for s in sidebar._sections)


class TestSidebarWidget:
    def test_set_actions_populates_list(self, qapp):  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        sidebar.set_cards(_ACTIONS)
        assert_that(_count(sidebar)).is_equal_to(2)  # pylint: disable=protected-access

    def test_search_filters_case_insensitively(self, qapp):  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        sidebar.set_cards(_ACTIONS)

        sidebar._search.setText("clock")  # pylint: disable=protected-access

        assert_that(_count(sidebar)).is_equal_to(1)  # pylint: disable=protected-access
        item = _first_list(sidebar).item(0)  # pylint: disable=protected-access
        assert item is not None
        assert_that(item.text()).is_equal_to("Show Clock")

    def test_search_with_no_matches_clears_list(self, qapp):  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        sidebar.set_cards(_ACTIONS)

        sidebar._search.setText("nonexistent")  # pylint: disable=protected-access

        assert_that(_count(sidebar)).is_equal_to(0)  # pylint: disable=protected-access

    def test_clearing_search_restores_full_list(self, qapp):  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        sidebar.set_cards(_ACTIONS)
        sidebar._search.setText("clock")  # pylint: disable=protected-access

        sidebar._search.setText("")  # pylint: disable=protected-access

        assert_that(_count(sidebar)).is_equal_to(2)  # pylint: disable=protected-access

    def test_scroll_area_fills_remaining_space(self, qapp):  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        layout = sidebar.layout()
        assert isinstance(layout, QVBoxLayout)

        assert_that(layout.stretch(layout.indexOf(sidebar._stack))).is_equal_to(1)  # pylint: disable=protected-access


class TestSections:  # pylint: disable=protected-access
    _ACTIONS: list[dict[str, Any]] = [
        {"plugin_uuid": "z", "plugin_name": "Zed", "card_uuid": "z.a", "card_name": "Za"},
        {
            "plugin_uuid": "b",
            "plugin_name": "Blank",
            "plugin_builtin": True,
            "card_uuid": "b.b",
            "card_name": "Blank",
        },
        {"plugin_uuid": "a", "plugin_name": "Alpha", "card_uuid": "a.x", "card_name": "Ax"},
        {"plugin_uuid": "a", "plugin_name": "Alpha", "card_uuid": "a.y", "card_name": "Ay"},
    ]

    def test_one_section_per_plugin_builtin_first_then_by_name(self, qapp):  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        sidebar.set_cards(self._ACTIONS)

        assert_that(sidebar.sections).is_equal_to([("Built in", ["Blank"]), ("Alpha", ["Ax", "Ay"]), ("Zed", ["Za"])])

    def test_search_drops_sections_without_matches(self, qapp):  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        sidebar.set_cards(self._ACTIONS)

        sidebar._search.setText("ax")

        assert_that(sidebar.sections).is_equal_to([("Alpha", ["Ax"])])

    def test_folding_hides_the_grid_and_survives_a_refresh(self, qapp):  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        sidebar.set_cards(self._ACTIONS)
        alpha = sidebar._sections[1]

        alpha.header.click()

        assert_that(alpha.expanded).is_false()
        assert_that(alpha.card_list.isHidden()).is_true()
        sidebar.set_cards(self._ACTIONS)
        assert_that([s.expanded for s in sidebar._sections]).is_equal_to([True, False, True])
        sidebar._sections[1].header.click()
        assert_that(sidebar._sections[1].expanded).is_true()


_COUNTER_PNG = Path(__file__).parents[2] / "fixtures" / "plugins" / "com.decker.testfixture" / "icons" / "counter.png"
_COUNTER: dict[str, Any] = {
    "plugin_uuid": "com.decker.testfixture",
    "plugin_name": "Counter",
    "card_uuid": "com.decker.testfixture.counter",
    "card_name": "Counter",
    "icon_b64": base64.b64encode(_COUNTER_PNG.read_bytes()).decode("ascii"),
}


class TestActionGrid:  # pylint: disable=protected-access
    def test_shows_tiles_with_icon_above_name(self, qapp):  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        sidebar.set_cards(_ACTIONS)
        card_list = _first_list(sidebar)

        assert_that(card_list.viewMode()).is_equal_to(QListView.ViewMode.IconMode)
        assert_that(card_list.isWrapping()).is_true()
        assert_that(card_list.iconSize()).is_equal_to(QSize(ICON_SIZE, ICON_SIZE))

    def test_items_carry_icon_and_tooltip(self, qapp):  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        sidebar.set_cards([_COUNTER, _ACTIONS[0]])

        counter = sidebar._sections[0].card_list.item(0)
        plain = sidebar._sections[1].card_list.item(0)
        assert counter is not None and plain is not None
        assert_that(counter.icon().isNull()).is_false()
        assert_that(plain.icon().isNull()).is_false()  # generated letter tile
        assert_that(counter.toolTip()).is_equal_to("Counter: Counter")

    def test_counter_tile_uses_counter_image(self, qapp):  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        sidebar.set_cards([_COUNTER])
        item = _first_list(sidebar).item(0)
        assert item is not None

        shown = item.icon().pixmap(ICON_SIZE, ICON_SIZE).toImage()

        assert_that(shown.pixelColor(ICON_SIZE // 2, 4).name()).is_equal_to(
            card_pixmap(_COUNTER, ICON_SIZE).toImage().pixelColor(ICON_SIZE // 2, 4).name()
        )

    def test_icons_are_reused_across_searches(self, qapp):  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        sidebar.set_cards([_COUNTER])
        first = sidebar._icons[("com.decker.testfixture", "com.decker.testfixture.counter")]

        sidebar._search.setText("count")

        assert_that(sidebar._icons[("com.decker.testfixture", "com.decker.testfixture.counter")]).is_same_as(first)


class TestDrag:  # pylint: disable=protected-access
    def test_drag_shows_action_icon_centred_on_cursor(self, qapp):  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        sidebar.set_cards([_COUNTER])
        item = _first_list(sidebar).item(0)
        assert item is not None

        drag = _first_list(sidebar).build_drag(item)

        pixmap = drag.pixmap()
        assert_that(pixmap.toImage()).is_equal_to(card_pixmap(_COUNTER, DRAG_ICON_SIZE).toImage())
        assert_that(drag.hotSpot()).is_equal_to(QPoint(pixmap.width() // 2, pixmap.height() // 2))

    def test_drag_payload_identifies_action_without_icon_bytes(self, qapp):  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        sidebar.set_cards([_COUNTER])
        item = _first_list(sidebar).item(0)
        assert item is not None

        mime = _first_list(sidebar).build_drag(item).mimeData()

        assert mime is not None
        payload = json.loads(bytes(mime.data("application/x-decker-card").data()))
        assert_that(payload).is_equal_to(
            {
                "plugin_uuid": "com.decker.testfixture",
                "plugin_name": "Counter",
                "card_uuid": "com.decker.testfixture.counter",
                "card_name": "Counter",
            }
        )

    def test_start_drag_without_current_item_does_nothing(self, qapp):  # pylint: disable=unused-argument
        _DraggableCardList().startDrag(Qt.DropAction.CopyAction)  # should not raise


_ACTION_LIST: list[dict[str, Any]] = [
    {
        "plugin_uuid": "com.decker.core",
        "plugin_name": "Core",
        "plugin_builtin": True,
        "action_uuid": "com.decker.core.http",
        "action_name": "HTTP Request",
        "scope": "global",
    },
    {
        "plugin_uuid": "com.t.audio",
        "plugin_name": "Audio",
        "action_uuid": "com.t.audio.up",
        "action_name": "Volume up",
        "scope": "global",
        "icon_b64": _COUNTER["icon_b64"],
    },
    {
        "plugin_uuid": "com.t.audio",
        "plugin_name": "Audio",
        "action_uuid": "com.t.audio.mute",
        "action_name": "Mute",
        "scope": "global",
    },
    {
        "plugin_uuid": "com.t.audio",
        "plugin_name": "Audio",
        "action_uuid": "com.t.audio.private",
        "action_name": "Private",
        "scope": "plugin",
    },
    {
        "plugin_uuid": "com.decker.core",
        "plugin_name": "Core",
        "plugin_builtin": True,
        "action_uuid": "com.decker.core.nothing",
        "action_name": "Do Nothing",
        "scope": "global",
    },
]


def _texts_and_icons(pane):
    result = {}
    for section in pane.sections:
        for row in range(section.card_list.count()):
            item = section.card_list.item(row)
            assert item is not None
            result[item.text()] = item.icon()
    return result


def _drop_payload(section, row: int = 0) -> dict:
    item = section.card_list.item(row)
    assert item is not None
    mime = section.card_list.build_drag(item).mimeData()
    assert mime is not None
    return json.loads(bytes(mime.data("application/x-decker-card").data()))


class TestTabs:  # pylint: disable=protected-access
    def test_there_are_a_cards_tab_and_an_actions_tab_with_icons(self, qapp):  # pylint: disable=unused-argument
        sidebar = SidebarWidget()

        assert_that(sidebar.tab_names).is_equal_to(["Cards", "Actions"])
        assert_that([not sidebar._tabs.tabIcon(i).isNull() for i in range(2)]).is_equal_to([True, True])

    def test_the_tabs_switch_the_list_and_the_search_hint(self, qapp):  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        sidebar.set_cards(_ACTIONS)
        sidebar.set_actions(_ACTION_LIST)

        assert_that(sidebar._stack.currentWidget()).is_same_as(sidebar._cards_pane)
        assert_that(sidebar._search.placeholderText()).is_equal_to("Search cards...")
        sidebar._tabs.setCurrentIndex(1)

        assert_that(sidebar._stack.currentWidget()).is_same_as(sidebar._actions_pane)
        assert_that(sidebar._search.placeholderText()).is_equal_to("Search actions...")

    def test_actions_are_grouped_by_plugin_with_the_built_in_one_first(self, qapp):  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        sidebar.set_actions(_ACTION_LIST)

        assert_that(sidebar.action_sections).is_equal_to(
            [
                ("Built in", ["HTTP Request"]),
                ("Audio", ["Volume up", "Mute"]),
            ]
        )

    def test_one_search_filters_both_lists(self, qapp):  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        sidebar.set_cards(_ACTIONS)
        sidebar.set_actions(_ACTION_LIST)

        sidebar._search.setText("mute")

        assert_that(sidebar.action_sections).is_equal_to([("Audio", ["Mute"])])
        assert_that(sidebar.sections).is_empty()

    def test_actions_tab_hides_plugin_scoped_actions_and_do_nothing(
        self, qapp
    ) -> None:  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        sidebar.set_actions(_ACTION_LIST)

        assert_that(sidebar.action_sections).is_equal_to(
            [("Built in", ["HTTP Request"]), ("Audio", ["Volume up", "Mute"])]
        )

    def test_action_tile_uses_the_action_icon(self, qapp) -> None:  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        sidebar.set_actions(_ACTION_LIST)
        icons = _texts_and_icons(sidebar._actions_pane)

        task = icons["Mute"].pixmap(ICON_SIZE, ICON_SIZE).toImage()
        own = icons["Volume up"].pixmap(ICON_SIZE, ICON_SIZE).toImage()

        assert_that(own == task).is_false()
        assert_that(icons["HTTP Request"].pixmap(ICON_SIZE, ICON_SIZE).toImage() == task).is_true()

    def test_action_drag_drops_a_blank_card(self, qapp) -> None:  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        sidebar.set_actions(_ACTION_LIST)

        payload = _drop_payload(sidebar._actions_pane.sections[0])  # the built-in section: HTTP Request

        assert_that(payload["plugin_uuid"]).is_equal_to("com.decker.blank")
        assert_that(payload["card_uuid"]).is_equal_to("com.decker.blank.blank")
        assert_that(payload["preset_action"]).is_equal_to("com.decker.core.http")

    def test_action_tiles_have_no_options_menu(self, qapp):  # pylint: disable=unused-argument
        sidebar = SidebarWidget()
        sidebar.set_actions(_ACTION_LIST)

        card_list = sidebar._actions_pane.sections[0].card_list

        assert_that(card_list.contextMenuPolicy()).is_equal_to(Qt.ContextMenuPolicy.DefaultContextMenu)
        assert_that(card_list.actions()).is_empty()
