"""Sidebar: a Cards tab and an Actions tab, each a searchable list in one collapsible section per plugin."""

import json
import math
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QByteArray, QMimeData, QPoint, QSize, Qt
from PyQt6.QtGui import QDrag, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from decker.client.card_icons import asset_pixmap, card_pixmap, decode_icon
from decker.plugin.bindings import NOTHING_ACTION_ID

CARD_MIME_TYPE = "application/x-decker-card"
ICON_SIZE = 48
DRAG_ICON_SIZE = 56
_TILE_SIZE = QSize(96, 104)  # room for a two-line name under the icon
_ICON_SOURCE_SIZE = ICON_SIZE * 2  # rendered at 2x so icons stay sharp on high-DPI screens
_DRAG_FIELDS = ("plugin_uuid", "plugin_name", "card_uuid", "card_name", "preset_action")
_BLANK_PLUGIN_ID = "com.decker.blank"
_BLANK_CARD_ID = "com.decker.blank.blank"
_DRAG_PIXMAP_ROLE = Qt.ItemDataRole.UserRole + 1  # an item's own drag picture, when it is not the card's icon
_TAB_ICON = 18
BUILTIN_TITLE = "Built in"


class _DraggableCardList(QListWidget):
    """Icon-mode QListWidget whose drags carry the card as custom MIME data and show its icon."""

    def build_drag(self, item: QListWidgetItem) -> QDrag:
        """Create the drag for item: the card (without its icon bytes) as MIME data, its icon as the image."""
        card: dict[str, Any] = item.data(Qt.ItemDataRole.UserRole) or {}
        payload = {field: card[field] for field in _DRAG_FIELDS if field in card}
        mime = QMimeData()
        mime.setData(CARD_MIME_TYPE, QByteArray(json.dumps(payload).encode()))
        drag = QDrag(self)
        drag.setMimeData(mime)
        override = item.data(_DRAG_PIXMAP_ROLE)
        pixmap = (
            override.scaled(
                DRAG_ICON_SIZE,
                DRAG_ICON_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            if isinstance(override, QPixmap)
            else card_pixmap(card, DRAG_ICON_SIZE)
        )
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(pixmap.width() // 2, pixmap.height() // 2))
        return drag

    def fit_height(self) -> None:
        """Size the list to all its rows, so the sidebar scrolls as a whole instead of each section."""
        viewport = self.viewport()
        columns = max(1, (viewport.width() if viewport is not None else _TILE_SIZE.width()) // _TILE_SIZE.width())
        rows = math.ceil(self.count() / columns)
        self.setFixedHeight(rows * _TILE_SIZE.height() + 2 * self.frameWidth() + 4)

    def resizeEvent(self, e: Any) -> None:  # pylint: disable=invalid-name
        """Refit the height when the width changes the number of columns."""
        super().resizeEvent(e)
        self.fit_height()

    def startDrag(self, supported_actions: Qt.DropAction) -> None:  # pylint: disable=invalid-name,unused-argument
        """Drag the current card by its icon."""
        item = self.currentItem()
        if item is not None:
            self.build_drag(item).exec(Qt.DropAction.CopyAction)


def _new_card_list() -> _DraggableCardList:
    card_list = _DraggableCardList()
    card_list.setObjectName("card-list")
    card_list.setViewMode(QListView.ViewMode.IconMode)
    card_list.setFlow(QListView.Flow.LeftToRight)
    card_list.setWrapping(True)
    card_list.setResizeMode(QListView.ResizeMode.Adjust)
    card_list.setMovement(QListView.Movement.Static)
    card_list.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
    card_list.setGridSize(_TILE_SIZE)
    card_list.setUniformItemSizes(True)
    card_list.setWordWrap(True)
    card_list.setDragEnabled(True)
    card_list.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
    card_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    return card_list


class _Section(QWidget):
    """One plugin's header (click to fold) above its grid of card tiles."""

    def __init__(self, plugin_id: str, title: str, expanded: bool) -> None:
        super().__init__()
        self.plugin_id = plugin_id
        self.card_list = _new_card_list()
        self.header = QToolButton()
        self.header.setObjectName("sidebar-section")
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.header.toggled.connect(self._on_toggled)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.header)
        layout.addWidget(self.card_list)
        self._on_toggled(expanded)

    @property
    def expanded(self) -> bool:
        """Whether the grid is shown."""
        return self.header.isChecked()

    def _on_toggled(self, expanded: bool) -> None:
        self.header.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.card_list.setVisible(expanded)


class _Pane(QScrollArea):
    """A scrolling list of entries grouped into one collapsible section per plugin, the built-in plugins first."""

    def __init__(
        self,
        name_key: str,
        id_key: str,
        icon_for: Callable[[dict[str, Any]], QIcon],
        drag_for: Callable[[dict[str, Any]], tuple[dict[str, Any], QPixmap | None]],
    ) -> None:
        super().__init__()
        self._name_key = name_key
        self._id_key = id_key
        self._icon_for = icon_for
        self._drag_for = drag_for
        self._entries: list[dict[str, Any]] = []
        self._needle = ""
        self.folded: set[str] = set()  # plugin ids the user collapsed; kept across refreshes and searches
        self.sections: list[_Section] = []
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.addStretch(1)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidget(self._content)

    @property
    def listing(self) -> list[tuple[str, list[str]]]:
        """``(title, names)`` of every section shown, in order."""
        return [
            (s.header.text(), [s.card_list.item(i).text() for i in range(s.card_list.count())])  # type: ignore[union-attr]
            for s in self.sections
        ]

    def set_entries(self, entries: list[dict[str, Any]]) -> None:
        """Replace the entries and redraw with the current search."""
        self._entries = entries
        self.refresh()

    def search(self, text: str) -> None:
        """Show only entries whose name contains ``text`` (case-insensitive)."""
        self._needle = text.strip().lower()
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the sections from the entries."""
        for section in self.sections:
            self._content_layout.removeWidget(section)
            section.deleteLater()
        self.sections = []
        groups: dict[str, list[dict[str, Any]]] = {}
        for entry in self._entries:
            if self._needle and self._needle not in str(entry.get(self._name_key, "")).lower():
                continue
            groups.setdefault(entry.get("plugin_uuid", ""), []).append(entry)
        for plugin_id in sorted(groups, key=lambda pid: self._order(pid, groups[pid])):
            self._add_section(plugin_id, groups[plugin_id])

    @staticmethod
    def _order(plugin_id: str, entries: list[dict[str, Any]]) -> tuple[bool, str]:
        builtin = any(e.get("plugin_builtin") for e in entries)
        return (not builtin, str(entries[0].get("plugin_name") or plugin_id).lower())

    def _add_section(self, plugin_id: str, entries: list[dict[str, Any]]) -> None:
        builtin = any(e.get("plugin_builtin") for e in entries)
        title = BUILTIN_TITLE if builtin else str(entries[0].get("plugin_name") or plugin_id)
        section = _Section(plugin_id, title, plugin_id not in self.folded)
        section.header.toggled.connect(lambda expanded, pid=plugin_id: self._remember_fold(pid, expanded))
        for entry in entries:
            name = str(entry.get(self._name_key, ""))
            drag_entry, drag_pixmap = self._drag_for(entry)
            item = QListWidgetItem(self._icon_for(entry), name)
            item.setData(Qt.ItemDataRole.UserRole, drag_entry)
            if drag_pixmap is not None:
                item.setData(_DRAG_PIXMAP_ROLE, drag_pixmap)
            plugin_name = entry.get("plugin_name", "")
            item.setToolTip(f"{plugin_name}: {name}" if plugin_name else name)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            section.card_list.addItem(item)
        section.card_list.fit_height()
        self._content_layout.insertWidget(len(self.sections), section)
        self.sections.append(section)

    def _remember_fold(self, plugin_id: str, expanded: bool) -> None:
        (self.folded.discard if expanded else self.folded.add)(plugin_id)


def _tile_icon(pixmap: QPixmap) -> QIcon:
    icon = QIcon(pixmap)
    icon.addPixmap(pixmap, QIcon.Mode.Selected)  # keep the icon untinted on the selected tile
    return icon


class SidebarWidget(QFrame):  # pylint: disable=too-many-instance-attributes
    """Cards and Actions tabs over one search box; each tab lists its entries grouped by plugin."""

    def __init__(self) -> None:
        super().__init__()
        self._cards: list[dict[str, Any]] = []
        self._card_icons: dict[tuple[str, str], QIcon] = {}
        self._tint = self.palette().text().color().name()

        self._cards_pane = _Pane("card_name", "card_uuid", self._card_icon, self._card_drag)
        self._actions_pane = _Pane("action_name", "action_uuid", self._action_icon, self._action_drag)
        self._tabs = QTabBar()
        self._tabs.setExpanding(True)
        self._tabs.setDrawBase(False)
        self._tabs.setIconSize(QSize(_TAB_ICON, _TAB_ICON))
        for name, asset, tip in (
            ("Cards", "images.svg", "Cards: what a key shows"),
            ("Actions", "tasks.svg", "Actions: things a key can run"),
        ):
            index = self._tabs.addTab(QIcon(asset_pixmap(asset, _TAB_ICON * 2, self._tint)), name)
            self._tabs.setTabToolTip(index, tip)
        self._stack = QStackedWidget()
        self._stack.addWidget(self._cards_pane)
        self._stack.addWidget(self._actions_pane)
        self._tabs.currentChanged.connect(self._stack.setCurrentIndex)
        self._tabs.currentChanged.connect(lambda _i: self._update_placeholder())

        self._search = QLineEdit()
        self._search.textChanged.connect(self._cards_pane.search)
        self._search.textChanged.connect(self._actions_pane.search)
        self._update_placeholder()

        layout = QVBoxLayout(self)
        layout.addWidget(self._tabs)
        layout.addWidget(self._search)
        layout.addWidget(self._stack, 1)

    # -- the cards pane keeps its old names, which the dashboard and the tests use
    @property
    def _scroll(self) -> QScrollArea:
        return self._cards_pane

    @property
    def _sections(self) -> list[_Section]:
        return self._cards_pane.sections

    @property
    def _icons(self) -> dict[tuple[str, str], QIcon]:
        return self._card_icons

    @property
    def sections(self) -> list[tuple[str, list[str]]]:
        """``(title, card names)`` of every card section shown, in order."""
        return self._cards_pane.listing

    @property
    def action_sections(self) -> list[tuple[str, list[str]]]:
        """``(title, action names)`` of every action section shown, in order."""
        return self._actions_pane.listing

    @property
    def tab_names(self) -> list[str]:
        """The tab titles, in order."""
        return [self._tabs.tabText(i) for i in range(self._tabs.count())]

    def set_cards(self, cards: list[dict[str, Any]]) -> None:
        """Replace the full card list."""
        self._cards = cards
        self._card_icons = {}
        self._cards_pane.set_entries(cards)

    def set_actions(self, actions: list[dict[str, Any]]) -> None:
        """Replace the action list; only global actions other than Do Nothing can be dropped on a key."""
        self._actions_pane.set_entries(
            [a for a in actions if a.get("scope") == "global" and a.get("action_uuid") != NOTHING_ACTION_ID]
        )

    def _update_placeholder(self) -> None:
        self._search.setPlaceholderText("Search cards..." if self._tabs.currentIndex() == 0 else "Search actions...")

    def _card_icon(self, card: dict[str, Any]) -> QIcon:
        key = (card.get("plugin_uuid", ""), card.get("card_uuid", ""))
        if key not in self._card_icons:
            self._card_icons[key] = _tile_icon(card_pixmap(card, _ICON_SOURCE_SIZE))
        return self._card_icons[key]

    @staticmethod
    def _card_drag(card: dict[str, Any]) -> tuple[dict[str, Any], QPixmap | None]:
        return card, None

    def _action_icon(self, action: dict[str, Any]) -> QIcon:
        pixmap = decode_icon(str(action.get("icon_b64") or ""))
        if pixmap is None:
            pixmap = asset_pixmap("tasks.svg", _ICON_SOURCE_SIZE, self._tint)
        return _tile_icon(pixmap)

    def _action_drag(self, action: dict[str, Any]) -> tuple[dict[str, Any], QPixmap | None]:
        """What dragging an action drops: a Blank card named for it, carrying the action as ``preset_action``."""
        action_id = str(action.get("action_uuid", ""))
        blank = {
            "plugin_uuid": _BLANK_PLUGIN_ID,
            "plugin_name": "Blank",
            "card_uuid": _BLANK_CARD_ID,
            "card_name": str(action.get("action_name", "")),
            "preset_action": action_id,
        }
        icon = decode_icon(str(action.get("icon_b64") or ""))
        return blank, icon or asset_pixmap("tasks.svg", _ICON_SOURCE_SIZE, self._tint)
