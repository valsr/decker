"""Settings control for an action: a dropdown of the actions the server's plugins provide, stored as the action id."""

import json
import logging
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QStandardItemModel
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


class ActionEdit(QComboBox):
    """Pick an action; ``value()`` is its id, or ``""`` for none.

    With an ``owner`` plugin it lists Do Nothing, the owner's actions and every global action; without one, the
    global actions only.
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

    def _shown_actions(self) -> list[dict[str, Any]]:
        shown = [
            a
            for a in self._actions
            if a.get("action_uuid") != NOTHING_ACTION_ID
            and (a.get("scope") == "global" or (self._owner is not None and a.get("plugin_uuid") == self._owner))
        ]
        return sorted(shown, key=lambda a: (str(a.get("action_name")), str(a.get("action_uuid"))))

    def _rebuild(self) -> None:
        self.blockSignals(True)
        self.clear()
        if self._owner is None:
            self.addItem(_NONE, "")
        else:
            self.addItem(_nothing_icon(), _NOTHING, NOTHING_ACTION_ID)
        shown = self._shown_actions()
        for action in shown:
            self.addItem(_action_icon(action), str(action.get("action_name")), str(action["action_uuid"]))
        listed = {str(a["action_uuid"]) for a in shown}
        if self._owner is not None:
            listed.add(NOTHING_ACTION_ID)
        if self._value and self._online and self._value not in listed:
            self.addItem(_MISSING, self._value)
            model = self.model()
            item = model.item(self.count() - 1) if isinstance(model, QStandardItemModel) else None
            if item is not None:
                item.setEnabled(False)
        self.setCurrentIndex(max(self.findData(self._value), 0))
        self.setToolTip("" if self._online else _OFFLINE_HINT)
        self.blockSignals(False)

    def _on_activated(self, index: int) -> None:
        data = self.itemData(index)
        self._value = data if isinstance(data, str) else ""
        self.value_changed.emit()
