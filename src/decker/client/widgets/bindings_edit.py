"""The card editor's binding rows: what a press, double press and long press of a key do."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from decker.client.widgets.action_edit import ActionEdit
from decker.client.widgets.schema_form import ActionParamsEdit
from decker.plugin.bindings import NOTHING_ACTION_ID

_GESTURES: tuple[tuple[str, str], ...] = (
    ("press", "Press"),
    ("double_press", "Double press"),
    ("long_press", "Long press"),
)
_RESET_TEXT = "Reset to default"


@dataclass(frozen=True)
class BindingsSpec:
    """The manifest's default bindings and the key's stored ones, both as ``{gesture: {"action", "args"}}``."""

    defaults: dict[str, dict[str, Any]]
    stored: dict[str, dict[str, Any]]


def _split(binding: Mapping[str, Any] | None) -> tuple[str, dict[str, Any]]:
    binding = binding or {}
    action = binding.get("action")
    args = binding.get("args")
    return (action if isinstance(action, str) else NOTHING_ACTION_ID), (dict(args) if isinstance(args, dict) else {})


class _BindingRow(QWidget):  # pylint: disable=missing-function-docstring

    changed = pyqtSignal()

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._default: tuple[str, dict[str, Any]] = (NOTHING_ACTION_ID, {})
        self._shown_action = NOTHING_ACTION_ID
        self._action_edit = ActionEdit(owner="")
        self._params = ActionParamsEdit()
        self._reset = QPushButton(_RESET_TEXT)
        self._error = QLabel()
        self._error.setProperty("role", "error")
        self._error.setWordWrap(True)
        self._error.hide()
        header = QHBoxLayout()
        header.addWidget(QLabel(f"{title}:"))
        header.addWidget(self._action_edit, 1)
        header.addWidget(self._reset)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(header)
        layout.addWidget(self._params)
        layout.addWidget(self._error)
        self._action_edit.value_changed.connect(self._on_action_chosen)
        self._params.value_changed.connect(self._on_edited)
        self._reset.clicked.connect(self._on_reset)

    def load(self, default: Mapping[str, Any] | None, stored: Mapping[str, Any] | None, plugin_id: str) -> None:
        self._action_edit.set_owner(plugin_id)
        self._action_edit.reload()  # the actions may have changed (or been unreachable) since the row was built
        self._show(*_split(default))
        self._default = (self._action_edit.value(), self._params.value())
        if stored is not None:
            self._show(*_split(stored))
        self.set_errors("", {})
        self._refresh()

    def binding(self) -> tuple[str, dict[str, Any]]:
        return self._action_edit.value(), self._params.value()

    def is_default(self) -> bool:
        return self.binding() == self._default

    def stored_value(self) -> dict[str, Any]:
        action, args = self.binding()
        return {"action": action, "args": args} if args else {"action": action}

    def errors(self) -> dict[str, str]:
        return self._params.errors()

    def has_errors(self) -> bool:
        return self._params.has_errors() or bool(self._error.text())

    def set_errors(self, own: str, args: Mapping[str, str]) -> None:
        self._error.setText(own)
        self._error.setVisible(bool(own))
        self._params.set_errors(args)

    def _show(self, action: str, args: dict[str, Any]) -> None:
        self._shown_action = action
        self._action_edit.set_value(action)
        self._params.set_action(action, args)

    def _refresh(self) -> None:
        self._reset.setEnabled(not self.is_default())

    def _on_action_chosen(self) -> None:
        if self._action_edit.value() == self._shown_action:
            return  # the same action picked again keeps its arguments
        self._shown_action = self._action_edit.value()
        self._params.set_action(self._action_edit.value(), {})
        self._on_edited()

    def _on_edited(self) -> None:
        self._error.setText("")
        self._error.hide()
        self._refresh()
        self.changed.emit()

    def _on_reset(self) -> None:
        self._show(*self._default)
        self.set_errors("", {})
        self._refresh()
        self.changed.emit()


class BindingsEdit(QWidget):
    """Three rows (press, double press, long press); ``value()`` holds only what differs from the manifest."""

    changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._rows: dict[str, _BindingRow] = {}
        for gesture, title in _GESTURES:
            row = _BindingRow(title)
            row.changed.connect(self.changed.emit)
            self._rows[gesture] = row
            layout.addWidget(row)

    def set_bindings(self, spec: BindingsSpec, plugin_id: str) -> None:
        """Show the stored bindings (the manifest default where a gesture has none). Emits nothing."""
        for gesture, row in self._rows.items():
            row.blockSignals(True)
            row.load(spec.defaults.get(gesture), spec.stored.get(gesture), plugin_id)
            row.blockSignals(False)

    def value(self) -> dict[str, dict[str, Any]]:
        """The bindings that differ from the manifest default, in their stored shape."""
        return {g: row.stored_value() for g, row in self._rows.items() if not row.is_default()}

    def errors(self) -> dict[str, str]:
        """Validation messages of the rows' argument forms, keyed ``<gesture>.<key>``."""
        return {f"{g}.{key}": msg for g, row in self._rows.items() for key, msg in row.errors().items()}

    def has_errors(self) -> bool:
        """Whether any row currently shows an error."""
        return any(row.has_errors() for row in self._rows.values())

    def set_errors(self, errors: Mapping[str, str]) -> None:
        """Show server errors (``<gesture>.<key>``, or ``<gesture>`` for the whole row) until the row is edited."""
        for gesture, row in self._rows.items():
            prefix = f"{gesture}."
            args = {k.removeprefix(prefix): str(m) for k, m in errors.items() if k.startswith(prefix)}
            row.set_errors(str(errors.get(gesture, "")), args)
