"""Settings control for the Switch card's slots: four faux buttons that take dropped cards and actions."""

import json
import logging
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from decker.client.card_icons import asset_pixmap, card_pixmap, placeholder_pixmap
from decker.client.context import AppContext
from decker.client.icon_picker import RpcCaller
from decker.client.widgets.sidebar import CARD_MIME_TYPE
from decker.common.appearance_schema import APPEARANCE_SCHEMA
from decker.common.background import normalize_background
from decker.common.slots import SLOT_NAMES, SWITCH_CARD_ID, normalize_slots
from decker.common.text_style import fold_legacy_title

_TITLES = {"display": "Display", "press": "Press", "double_press": "Double Press", "long_press": "Long Press"}
_ICON_PX = 40
_EMPTY = "Drop a card here"
_EMPTY_ACTIONS = "Drop a card or action here"
Catalog = tuple[list[dict[str, Any]], list[dict[str, Any]]]


def _app_rpc() -> RpcCaller | None:
    return AppContext.get().connection_manager


def _fetch_list(rpc: RpcCaller, command: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(rpc.call(command) or "null")
    except Exception as exc:  # pylint: disable=broad-except
        logging.warning("Could not run %s: %s", command, exc)
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def fetch_catalog() -> Catalog:
    """The server's ``(cards, actions)`` lists (empty when offline)."""
    rpc = _app_rpc()
    if rpc is None:
        return [], []
    return _fetch_list(rpc, "plugins_cards_list"), _fetch_list(rpc, "plugins_actions_list")


def slot_from_drop(name: str, payload: Any) -> dict[str, Any] | None:
    """The slot value a dropped picker payload makes for ``name``, or None when that slot cannot take it."""
    if not isinstance(payload, dict):
        return None
    card = str(payload.get("card_uuid") or "")
    action = str(payload.get("preset_action") or "")
    if card == SWITCH_CARD_ID:
        return None
    if action:
        return None if name == "display" else {"kind": "action", "action": action, "settings": {}}
    plugin = str(payload.get("plugin_uuid") or "")
    if not plugin or not card:
        return None
    return {"kind": "card", "plugin": plugin, "card": card, "settings": {}, "appearance": {}}


def _payload(mime: Any) -> Any:
    if not mime.hasFormat(CARD_MIME_TYPE):
        return None
    try:
        return json.loads(bytes(mime.data(CARD_MIME_TYPE)).decode())
    except (ValueError, UnicodeDecodeError):
        return None


class _SlotButton(QFrame):
    """One faux button: shows the held item, takes drops, selects on click and clears with its badge."""

    clicked = pyqtSignal(str)
    cleared = pyqtSignal(str)
    dropped = pyqtSignal(str, dict)

    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._icon = QLabel()
        self._icon.setFixedSize(QSize(_ICON_PX, _ICON_PX))
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title = QLabel(_TITLES[name])
        self._title.setStyleSheet("font-weight: bold")
        self._item = QLabel(_EMPTY)
        self._item.setWordWrap(True)
        self.clear_button = QToolButton()
        self.clear_button.setText("×")
        self.clear_button.setToolTip("Clear this slot")
        self.clear_button.clicked.connect(lambda _checked=False: self.cleared.emit(self.name))
        texts = QVBoxLayout()
        texts.addWidget(self._title)
        texts.addWidget(self._item)
        row = QHBoxLayout(self)
        row.addWidget(self._icon)
        row.addLayout(texts, 1)
        row.addWidget(self.clear_button, 0, Qt.AlignmentFlag.AlignTop)
        self.show_item(None, "", None)

    def show_item(self, value: dict[str, Any] | None, label: str, pixmap: QPixmap | None) -> None:
        """Show what the slot holds (``value`` None is the empty state)."""
        self.clear_button.setVisible(value is not None)
        self._item.setText(label if value is not None else (_EMPTY if self.name == "display" else _EMPTY_ACTIONS))
        self._icon.setPixmap(pixmap if pixmap is not None else QPixmap())
        self._icon.setVisible(pixmap is not None)

    def set_selected(self, selected: bool) -> None:
        """Highlight the button as the selected slot."""
        self.setStyleSheet("_SlotButton { border: 2px solid palette(highlight); }" if selected else "")

    def item_text(self) -> str:
        """The name shown for the held item (or the empty hint)."""
        return self._item.text()

    def mousePressEvent(self, a0: Any) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Select this slot."""
        self.clicked.emit(self.name)
        super().mousePressEvent(a0)

    def _acceptable(self, event: Any) -> bool:
        mime = event.mimeData()
        return mime.hasFormat(CARD_MIME_TYPE) and slot_from_drop(self.name, _payload(mime)) is not None

    def dragEnterEvent(self, a0: Any) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Accept a card, or an action except on Display; never the Switch card."""
        if self._acceptable(a0):
            a0.acceptProposedAction()

    def dragMoveEvent(self, a0: Any) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Keep accepting while hovering."""
        if self._acceptable(a0):
            a0.acceptProposedAction()

    def dropEvent(self, a0: Any) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Fill the slot with the dropped card or action."""
        slot = slot_from_drop(self.name, _payload(a0.mimeData()))
        if slot is None:
            return
        self.dropped.emit(self.name, slot)
        a0.acceptProposedAction()


def _with_defaults(stored: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """The appearance the form shows: ``stored`` over the card's ``defaults``, title and background field-wise."""
    folded = fold_legacy_title(stored)
    merged = {**defaults, **folded}
    if "title" in merged:
        merged["title"] = {**defaults.get("title", {}), **folded.get("title", {})}
    if "background" in merged:
        base = normalize_background(defaults["background"]) if "background" in defaults else None
        merged["background"] = normalize_background(merged["background"], base)
    return merged


def _without_defaults(values: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Only what ``values`` changes from ``defaults`` (field-wise inside dict entries), so defaults stay inherited."""
    changed: dict[str, Any] = {}
    for key, value in values.items():
        default = defaults.get(key)
        if value is None or value == default:
            continue
        if isinstance(value, dict) and isinstance(default, dict):
            value = {k: v for k, v in value.items() if v != default.get(k)}
        changed[key] = value
    return changed


class _SlotConfig(QWidget):
    """The settings (and, for a card, appearance) forms of the selected slot; hidden when there is nothing to edit."""

    edited = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from decker.client.widgets.schema_form import (  # pylint: disable=import-outside-toplevel,cyclic-import; schema_form imports this module for its ``slots`` field
            AppearanceForm,
            SchemaForm,
        )

        self.settings_form = SchemaForm()
        self.appearance_form = AppearanceForm()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.appearance_form)
        layout.addWidget(self.settings_form)
        self.appearance_form.changed.connect(self.edited.emit)
        self.settings_form.changed.connect(self.edited.emit)
        self._defaults: dict[str, Any] = {}
        self._baseline: dict[str, Any] = {}
        self.setVisible(False)

    def show_forms(
        self,
        schema: list[dict[str, Any]] | None,
        value: dict[str, Any] | None,
        defaults: dict[str, Any],
        plugin_id: str = "",
    ) -> None:
        """Fill the forms from slot ``value`` and the card's appearance ``defaults`` (no schema or value hides them)."""
        self._defaults = fold_legacy_title(defaults)
        for form in (self.settings_form, self.appearance_form):
            form.blockSignals(True)
        if schema is None or value is None:
            self.settings_form.set_schema([], {})
            self.appearance_form.set_schema([], {})
        else:
            is_card = value["kind"] == "card"
            self._baseline = self._form_baseline(is_card)
            shown = _with_defaults(value.get("appearance", {}), self._defaults) if is_card else {}
            self.appearance_form.set_schema(APPEARANCE_SCHEMA if is_card else [], shown)
            self.settings_form.set_schema(schema, value["settings"], plugin_id)
        for form in (self.settings_form, self.appearance_form):
            form.blockSignals(False)
        self.appearance_form.setVisible(value is not None and value["kind"] == "card")
        self.setVisible(schema is not None and value is not None)

    def _form_baseline(self, is_card: bool) -> dict[str, Any]:
        """What the appearance form holds for a slot that sets nothing itself (schema filled in)."""
        if not is_card:
            return {}
        self.appearance_form.set_schema(APPEARANCE_SCHEMA, _with_defaults({}, self._defaults))
        return self.appearance_form.values()

    def edited_value(self, value: dict[str, Any]) -> dict[str, Any]:
        """``value`` with the forms' current entries."""
        edited = {**value, "settings": self.settings_form.values()}
        if value["kind"] == "card":
            edited["appearance"] = _without_defaults(self.appearance_form.values(), self._baseline)
        return edited


class SlotsEdit(QWidget):
    """The four slots (Display, Press, Double Press, Long Press) as drop-target buttons; one may be selected."""

    changed = pyqtSignal()
    selected_changed = pyqtSignal(str)

    def __init__(self, catalog: Callable[[], Catalog] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._catalog = catalog or fetch_catalog
        self._cards: list[dict[str, Any]] = []
        self._actions: list[dict[str, Any]] = []
        self._slots: dict[str, dict[str, Any] | None] = normalize_slots(None)
        self._selected: str | None = None
        self._buttons: dict[str, _SlotButton] = {}
        self._shown: tuple[str | None, Any] = (None, None)
        self._config = _SlotConfig()
        self._config.edited.connect(self._on_config_edited)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for name in SLOT_NAMES:
            button = _SlotButton(name)
            button.clicked.connect(self._select)
            button.cleared.connect(self._on_cleared)
            button.dropped.connect(self._on_dropped)
            self._buttons[name] = button
            layout.addWidget(button)
        layout.addWidget(self._config)
        self.reload()

    def reload(self) -> None:
        """Re-read the card and action names from the server."""
        self._cards, self._actions = self._catalog()
        self._shown = (None, None)  # the schemas may have changed under the shown form
        self._refresh()

    def value(self) -> dict[str, Any]:
        """All four slots (an empty slot is None)."""
        return normalize_slots(self._slots)

    def set_value(self, value: Any) -> None:
        """Show ``value`` without emitting ``changed``."""
        self._slots = normalize_slots(value)
        self._refresh()

    def selected_slot(self) -> str | None:
        """The selected slot's name, or None."""
        return self._selected

    def slot_value(self, name: str) -> dict[str, Any] | None:
        """What slot ``name`` holds."""
        return self._slots[name]

    def set_slot_value(self, name: str, value: dict[str, Any] | None) -> None:
        """Replace slot ``name`` (``None`` empties it) and emit ``changed``."""
        self._slots[name] = value
        self._refresh()
        self.changed.emit()

    def _select(self, name: str) -> None:
        if name == self._selected:
            return
        self._selected = name
        self._refresh()
        self.selected_changed.emit(name)

    def _on_cleared(self, name: str) -> None:
        self.set_slot_value(name, None)

    def _on_dropped(self, name: str, slot: dict[str, Any]) -> None:
        self.set_slot_value(name, slot)
        self._select(name)

    def _refresh(self) -> None:
        for name, button in self._buttons.items():
            value = self._slots.get(name)
            label, pixmap = self._describe(value)
            button.show_item(value, label, pixmap)
            button.set_selected(name == self._selected)
        self._refresh_config()

    def _refresh_config(self) -> None:
        value = self._slots.get(self._selected) if self._selected else None
        if (self._selected, value) == self._shown:
            return  # the form already shows it (e.g. it is the source of this very change)
        self._shown = (self._selected, value)
        entry = self._config_entry(value)
        schema = None if entry is None else list(entry.get("settings_schema") or [])
        defaults = dict(entry.get("appearance") or {}) if entry is not None and value is not None else {}
        is_card = bool(value and value["kind"] == "card")
        plugin_id = str((value["plugin"] if is_card else (entry or {}).get("plugin_uuid")) or "") if value else ""
        self._config.show_forms(schema, value, defaults if is_card else {}, plugin_id)

    def _config_entry(self, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        if value["kind"] == "action":
            return next((a for a in self._actions if a.get("action_uuid") == value["action"]), None)
        return next(
            (c for c in self._cards if c.get("plugin_uuid") == value["plugin"] and c.get("card_uuid") == value["card"]),
            None,
        )

    def _on_config_edited(self) -> None:
        name = self._selected
        value = self._slots.get(name) if name else None
        if name is None or value is None:
            return
        edited = self._config.edited_value(value)
        self._shown = (name, edited)
        self.set_slot_value(name, edited)

    def _describe(self, value: dict[str, Any] | None) -> tuple[str, QPixmap | None]:
        if value is None:
            return "", None
        if value["kind"] == "action":
            return self._describe_action(str(value["action"]))
        return self._describe_card(str(value["plugin"]), str(value["card"]))

    def _describe_card(self, plugin: str, card_id: str) -> tuple[str, QPixmap | None]:
        card = next((c for c in self._cards if c.get("plugin_uuid") == plugin and c.get("card_uuid") == card_id), None)
        if card is None:
            return f"(missing card) {card_id}", placeholder_pixmap("?", _ICON_PX)
        return str(card.get("card_name", card_id)), card_pixmap(card, _ICON_PX)

    def _describe_action(self, action_id: str) -> tuple[str, QPixmap | None]:
        action = next((a for a in self._actions if a.get("action_uuid") == action_id), None)
        name = f"(missing action) {action_id}" if action is None else str(action.get("action_name", action_id))
        return name, asset_pixmap("tasks.svg", _ICON_PX, self.palette().text().color().name())
