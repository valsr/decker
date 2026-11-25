"""Settings control for an action: a dropdown of the actions the server's plugins provide, stored as the action id."""

import json
import logging
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QPointF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QStandardItemModel,
)
from PyQt6.QtWidgets import QComboBox, QWidget

from decker.client.card_icons import decode_icon
from decker.client.context import AppContext
from decker.client.icon_picker import RpcCaller
from decker.plugin.bindings import NOTHING_ACTION_ID

_NONE = "(choose an action)"
_MISSING = "(missing action)"
_OFFLINE_HINT = "Connect to the server to list its actions"
_NOTHING = "Do Nothing"
_ICON_SIZE = 16
_CORE_PLUGIN_ID = "com.decker.core"
_BUILT_IN = "Built-in"
_GLOBAL = "Global"
_CARD = "Card actions"


def _app_rpc() -> RpcCaller | None:
    return AppContext.get().connection_manager


def fetch_actions(rpc: RpcCaller | None) -> list[dict[str, Any]] | None:
    """The server's actions (None when they cannot be listed)."""
    if rpc is None:
        return None
    try:
        data = json.loads(rpc.call("plugins_actions_list") or "null")
    except Exception as exc:  # pylint: disable=broad-except
        logging.warning("Could not list actions: %s", exc)
        return None
    return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else None


def _nothing_icon() -> QIcon:
    pixmap = QPixmap(_ICON_SIZE, _ICON_SIZE)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#888888"), 2))
    low, high = 3.0, _ICON_SIZE - 3.0
    painter.drawLine(QPointF(low, low), QPointF(high, high))
    painter.drawLine(QPointF(low, high), QPointF(high, low))
    painter.end()
    return QIcon(pixmap)


def _action_icon(action: dict[str, Any]) -> QIcon:
    pixmap = decode_icon(str(action.get("icon_b64") or ""))
    return QIcon(pixmap) if pixmap is not None else QIcon()


def _plugin_label(action: dict[str, Any]) -> str:
    return str(action.get("plugin_name") or action.get("plugin_uuid") or "")


def _sort_key(action: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        _plugin_label(action).lower(),
        str(action.get("plugin_uuid")),
        str(action.get("action_name")),
        str(action.get("action_uuid")),
    )


class ActionEdit(QComboBox):
    """Pick an action; ``value()`` is its id.

    With an ``owner``: Do Nothing, then Card actions (the owner's plugin-scoped actions), Built-in (Core) and Global
    (every plugin's global actions, the owner's included, one sub-header per plugin). Without an owner the card section
    is left out. Empty sections have no header.
    """

    value_changed = pyqtSignal()

    def __init__(
        self,
        rpc: Callable[[], RpcCaller | None] | None = None,
        parent: QWidget | None = None,
        owner: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._rpc = rpc or _app_rpc
        self._owner = owner
        self._value = ""
        self._actions: list[dict[str, Any]] = []
        self._online = False
        self.activated.connect(self._on_activated)
        self.reload()

    def value(self) -> str:
        """The chosen action id, or an empty string."""
        return self._value

    def set_value(self, value: Any) -> None:
        """Show ``value`` (anything but a string means none) without emitting ``value_changed``."""
        self._value = value if isinstance(value, str) else ""
        self._rebuild()

    def set_owner(self, plugin_id: str) -> None:
        """List the actions the plugin ``plugin_id`` may bind; keeps the shown value."""
        self._owner = plugin_id
        self._rebuild()

    def reload(self) -> None:
        """Re-read the actions from the server."""
        listed = fetch_actions(self._rpc())
        self._online = listed is not None
        self._actions = listed or []
        self._rebuild()

    def _sections(self) -> list[tuple[str, list[dict[str, Any]]]]:
        usable = [a for a in self._actions if a.get("action_uuid") != NOTHING_ACTION_ID]
        card = [
            a
            for a in usable
            if self._owner is not None and a.get("plugin_uuid") == self._owner and a.get("scope") != "global"
        ]
        built_in = [a for a in usable if a.get("plugin_uuid") == _CORE_PLUGIN_ID and a not in card]
        rest = [a for a in usable if a not in card and a not in built_in and a.get("scope") == "global"]
        sections = [(_CARD, card), (_BUILT_IN, built_in), (_GLOBAL, rest)]
        return [(title, sorted(actions, key=_sort_key)) for title, actions in sections if actions]

    def _rebuild(self) -> None:
        self.blockSignals(True)
        self.clear()
        if self._owner is None:
            self.addItem(_NONE, "")
        else:
            self.addItem(_nothing_icon(), _NOTHING, NOTHING_ACTION_ID)
        listed = {NOTHING_ACTION_ID} if self._owner is not None else set()
        for title, actions in self._sections():
            self._add_group_header(title)
            group: str | None = None
            for action in actions:
                if title == _GLOBAL and action.get("plugin_uuid") != group:
                    group = str(action.get("plugin_uuid"))
                    self._add_group_header(_plugin_label(action), sub=True)
                self.addItem(_action_icon(action), str(action.get("action_name")), str(action["action_uuid"]))
                listed.add(str(action["action_uuid"]))
        if self._value and self._online and self._value not in listed:
            self.insertSeparator(self.count())
            self.addItem(_MISSING, self._value)
            self._disable_last()
        self.setCurrentIndex(max(self.findData(self._value), 0))
        self.setToolTip("" if self._online else _OFFLINE_HINT)
        self.blockSignals(False)

    def _add_group_header(self, title: str, sub: bool = False) -> None:
        if not sub:
            self.insertSeparator(self.count())
        self.addItem(title)
        self._disable_last(bold=True)

    def _disable_last(self, bold: bool = False) -> None:
        model = self.model()
        item = model.item(self.count() - 1) if isinstance(model, QStandardItemModel) else None
        if item is None:
            return
        item.setEnabled(False)
        if bold:
            font = QFont(self.font())
            font.setBold(True)
            item.setFont(font)

    def _on_activated(self, index: int) -> None:
        data = self.itemData(index)
        self._value = data if isinstance(data, str) else ""
        self.value_changed.emit()
