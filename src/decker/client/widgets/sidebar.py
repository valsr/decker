"""Sidebar: a searchable list of cards and presets in one collapsible section per plugin."""

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
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from decker.client.card_icons import card_pixmap

CARD_MIME_TYPE = "application/x-decker-card"
ICON_SIZE = 48
DRAG_ICON_SIZE = 56
_TILE_SIZE = QSize(96, 104)  # room for a two-line name under the icon
_ICON_SOURCE_SIZE = ICON_SIZE * 2  # rendered at 2x so icons stay sharp on high-DPI screens
_DRAG_FIELDS = ("plugin_uuid", "plugin_name", "card_uuid", "card_name", "preset_uuid", "preset_name")
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
        pixmap = card_pixmap({"icon_b64": card.get("icon_b64", ""), "card_name": _entry_name(card)}, DRAG_ICON_SIZE)
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


def _entry_name(entry: dict[str, Any]) -> str:
    return str(entry.get("card_name") or entry.get("preset_name") or "")


class _Pane(QScrollArea):
    """A scrolling list of entries grouped into one collapsible section per plugin, the built-in plugins first."""

    def __init__(self, icon_for: Callable[[dict[str, Any]], QIcon]) -> None:
        super().__init__()
        self._icon_for = icon_for
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
            haystack = f"{_entry_name(entry)} {entry.get('plugin_name', '')}".lower()
            if self._needle and self._needle not in haystack:
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
        for entry in sorted(entries, key=lambda e: _entry_name(e).lower()):
            name = _entry_name(entry)
            item = QListWidgetItem(self._icon_for(entry), name)
            item.setData(Qt.ItemDataRole.UserRole, entry)
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


class SidebarWidget(QFrame):
    """A search box over the cards and presets, grouped by plugin."""

    def __init__(self) -> None:
        super().__init__()
        self._cards: list[dict[str, Any]] = []
        self._presets: list[dict[str, Any]] = []
        self._card_icons: dict[tuple[str, str], QIcon] = {}
        self._pane = _Pane(self._icon)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search cards...")
        self._search.textChanged.connect(self._pane.search)

        layout = QVBoxLayout(self)
        layout.addWidget(self._search)
        layout.addWidget(self._pane, 1)

    # -- old names, which the dashboard and the tests use
    @property
    def _scroll(self) -> QScrollArea:
        return self._pane

    @property
    def _cards_pane(self) -> _Pane:
        return self._pane

    @property
    def _sections(self) -> list[_Section]:
        return self._pane.sections

    @property
    def _icons(self) -> dict[tuple[str, str], QIcon]:
        return self._card_icons

    @property
    def sections(self) -> list[tuple[str, list[str]]]:
        """``(title, names)`` of every section shown, cards and presets together, in order."""
        return self._pane.listing

    def set_cards(self, cards: list[dict[str, Any]]) -> None:
        """Replace the full card list."""
        self._cards = cards
        self._card_icons = {}
        self._pane.set_entries(self._cards + self._presets)

    def set_presets(self, presets: list[dict[str, Any]]) -> None:
        """Replace the full preset list."""
        self._presets = presets
        self._card_icons = {}
        self._pane.set_entries(self._cards + self._presets)

    def _icon(self, entry: dict[str, Any]) -> QIcon:
        key = (entry.get("plugin_uuid", ""), entry.get("card_uuid") or entry.get("preset_uuid", ""))
        if key not in self._card_icons:
            card = {"icon_b64": entry.get("icon_b64", ""), "card_name": _entry_name(entry)}
            self._card_icons[key] = _tile_icon(card_pixmap(card, _ICON_SOURCE_SIZE))
        return self._card_icons[key]
