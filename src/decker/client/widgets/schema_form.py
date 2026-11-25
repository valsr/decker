"""Form generated from a settings schema (the JSON list produced by ``schema_to_json``)."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont, QIntValidator
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFontComboBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from decker.client.context import AppContext
from decker.client.widgets.action_edit import ActionEdit, fetch_actions
from decker.client.widgets.background_edit import BackgroundEdit
from decker.client.widgets.color_button import ColorButton
from decker.client.widgets.dynamic_choice_edit import DynamicChoiceEdit
from decker.client.widgets.icon_edit import IconEdit
from decker.client.widgets.page_edit import PageEdit
from decker.client.widgets.profile_edit import ProfileEdit
from decker.client.widgets.text_style_edit import TextStyleEdit
from decker.plugin.settings_schema import (
    SettingEntry,
    parse_settings_schema,
    validate_settings,
)

_INT_LIMIT = 999_999_999
_DEFAULT_COLOR = "#000000"


@dataclass
class _Field:
    """A rendered schema entry: the outer widget plus how to read and write its value."""

    widget: QWidget
    get: Callable[[], Any]
    set: Callable[[Any], None]


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_field(entry: dict[str, Any]) -> _Field:
    default = _as_int(entry.get("default"), 0)
    if entry.get("optional"):
        edit = QLineEdit()
        edit.setValidator(QIntValidator(-_INT_LIMIT, _INT_LIMIT))

        def set_optional(value: Any) -> None:
            edit.setText("" if value in (None, "") else str(_as_int(value, 0)))

        fallback = None if entry.get("default") is None else default

        def get_optional() -> int | None:
            text = edit.text().strip()
            if not text:
                return None
            try:
                return int(text)
            except ValueError:  # a half-typed "-" passes the validator as Intermediate
                return fallback

        return _Field(edit, get_optional, set_optional)
    spin = QSpinBox()
    spin.setRange(_as_int(entry.get("min"), -_INT_LIMIT), _as_int(entry.get("max"), _INT_LIMIT))
    if entry.get("special_value"):
        spin.setSpecialValueText(str(entry["special_value"]))
    return _Field(spin, spin.value, lambda v: spin.setValue(_as_int(v, default)))


def _float_field(entry: dict[str, Any]) -> _Field:
    default = _as_float(entry.get("default"), 0.0)
    spin = QDoubleSpinBox()
    spin.setRange(_as_float(entry.get("min"), -1e9), _as_float(entry.get("max"), 1e9))
    spin.setDecimals(_as_int(entry.get("decimals"), 2))
    if entry.get("step"):
        spin.setSingleStep(_as_float(entry["step"], 1.0))
    if entry.get("special_value"):
        spin.setSpecialValueText(str(entry["special_value"]))
    return _Field(spin, spin.value, lambda v: spin.setValue(_as_float(v, default)))


def _text_field(entry: dict[str, Any]) -> _Field:
    if entry.get("multiline"):
        box = QPlainTextEdit()
        if entry.get("placeholder"):
            box.setPlaceholderText(str(entry["placeholder"]))
        return _Field(box, box.toPlainText, lambda v: box.setPlainText(str(v or "")))
    edit = QLineEdit()
    if entry.get("placeholder"):
        edit.setPlaceholderText(str(entry["placeholder"]))
    return _Field(edit, edit.text, lambda v: edit.setText(str(v or "")))


def _bool_field(_entry: dict[str, Any]) -> _Field:
    check = QCheckBox()
    return _Field(check, check.isChecked, lambda v: check.setChecked(bool(v)))


def _editable_choice_field(entry: dict[str, Any]) -> _Field:
    edit = DynamicChoiceEdit({**entry, "options_source": ""}, "")
    return _Field(edit, edit.value, edit.set_value)


def _choice_field(entry: dict[str, Any]) -> _Field:
    combo = QComboBox()
    for option in entry.get("options") or []:
        combo.addItem(str(option["label"]), option["value"])

    def set_value(value: Any) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    return _Field(combo, combo.currentData, set_value)


def _color_field(entry: dict[str, Any]) -> _Field:
    button = ColorButton(_DEFAULT_COLOR, alpha=bool(entry.get("alpha")))
    if not entry.get("optional"):
        return _Field(button, lambda: button.color, lambda v: button.set_color(str(v or _DEFAULT_COLOR)))
    check = QCheckBox()
    container = QWidget()
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(check)
    row.addWidget(button)
    row.addStretch()
    check.toggled.connect(button.setEnabled)

    def set_value(value: Any) -> None:
        check.setChecked(bool(value))
        button.setEnabled(bool(value))
        button.set_color(str(value or _DEFAULT_COLOR))

    return _Field(container, lambda: button.color if check.isChecked() else None, set_value)


def _font_field(_entry: dict[str, Any]) -> _Field:
    combo = QFontComboBox()
    return _Field(combo, lambda: combo.currentFont().family(), lambda v: combo.setCurrentFont(QFont(str(v))))


def _icon_field(_entry: dict[str, Any]) -> _Field:
    edit = IconEdit()
    return _Field(edit, edit.value, edit.set_value)


def _background_field(_entry: dict[str, Any]) -> _Field:
    edit = BackgroundEdit()
    return _Field(edit, edit.value, edit.set_value)


def _page_field(_entry: dict[str, Any]) -> _Field:
    edit = PageEdit()
    return _Field(edit, edit.value, edit.set_value)


def _profile_field(_entry: dict[str, Any]) -> _Field:
    edit = ProfileEdit()
    return _Field(edit, edit.value, edit.set_value)


def _action_field(_entry: dict[str, Any], plugin_id: str = "") -> _Field:
    edit = ActionEdit(owner=plugin_id or None)
    return _Field(edit, edit.value, edit.set_value)


def _action_params_field(_entry: dict[str, Any]) -> _Field:
    edit = ActionParamsEdit()
    return _Field(edit, edit.value, edit.set_value)


def _text_style_field(_entry: dict[str, Any]) -> _Field:
    edit = TextStyleEdit()
    return _Field(edit, edit.value, edit.set_value)


_BUILDERS: dict[str, Callable[[dict[str, Any]], _Field]] = {
    "int": _int_field,
    "float": _float_field,
    "text": _text_field,
    "bool": _bool_field,
    "choice": _choice_field,
    "color": _color_field,
    "font": _font_field,
    "icon": _icon_field,
    "background": _background_field,
    "page": _page_field,
    "profile": _profile_field,
    "action": _action_field,
    "action_params": _action_params_field,
    "text_style": _text_style_field,
}


def _dynamic_choice_field(entry: dict[str, Any], plugin_id: str) -> _Field:
    edit = DynamicChoiceEdit(entry, plugin_id)
    return _Field(edit, edit.value, edit.set_value)


def _build_field(entry: dict[str, Any], values: dict[str, Any], plugin_id: str = "") -> _Field | None:
    """The filled widget for a schema field, or None for an unknown type."""
    builder = _BUILDERS.get(str(entry.get("type")))
    if builder is None:
        return None
    if entry.get("type") == "choice" and entry.get("options_source"):
        field = _dynamic_choice_field(entry, plugin_id) if plugin_id else _editable_choice_field(entry)
    elif entry.get("type") == "action":
        field = _action_field(entry, plugin_id)
    else:
        field = builder(entry)
    field.set(values.get(entry["key"], entry.get("default")))
    if entry.get("help"):
        field.widget.setToolTip(str(entry["help"]))
    return field


def _watch(widget: QWidget, slot: Callable[[], None]) -> None:
    """Call ``slot`` once per user edit of ``widget``; controls inside a composite or spin box are not watched twice."""
    if isinstance(
        widget,
        (
            ActionEdit,
            ActionParamsEdit,
            BackgroundEdit,
            DynamicChoiceEdit,
            IconEdit,
            PageEdit,
            ProfileEdit,
            TextStyleEdit,
        ),
    ):
        widget.value_changed.connect(slot)
    elif isinstance(widget, ColorButton):
        widget.color_changed.connect(lambda _color: slot())
    elif isinstance(widget, QCheckBox):
        widget.toggled.connect(lambda _checked: slot())
    elif isinstance(widget, QComboBox):
        widget.currentIndexChanged.connect(lambda _index: slot())
    elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
        widget.valueChanged.connect(lambda _value: slot())
    elif isinstance(widget, QLineEdit):
        widget.textChanged.connect(lambda _text: slot())
    elif isinstance(widget, QPlainTextEdit):
        widget.textChanged.connect(slot)
    else:
        for child in widget.children():
            if isinstance(child, QWidget):
                _watch(child, slot)


class SchemaForm(QWidget):
    """Renders a settings schema as a form and reads the entered values back."""

    changed = pyqtSignal()

    def __init__(self, track_defaults: bool = True) -> None:
        super().__init__()
        self._track_defaults = track_defaults
        self._defaults: dict[str, Any] = {}
        self._layout = QFormLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._fields: dict[str, _Field] = {}
        self._error_labels: dict[str, QLabel] = {}
        self._server_errors: dict[str, str] = {}
        self._parsed: tuple[SettingEntry, ...] = ()
        self._schema: list[dict[str, Any]] = []

    def set_schema(self, schema: list[dict[str, Any]], values: dict[str, Any], plugin_id: str = "") -> None:
        """Rebuild the form for ``schema`` and fill it from ``values`` (missing keys use schema defaults)."""
        while self._layout.rowCount():
            self._layout.removeRow(0)
        self._fields = {}
        self._error_labels = {}
        self._server_errors = {}
        self._parsed = _parse(schema)
        self._schema = schema
        for entry in schema:
            if "section" in entry:
                self._layout.addRow(_section(str(entry["section"])))
                continue
            field = _build_field(entry, values, plugin_id)
            if field is None:
                continue
            self._fields[entry["key"]] = field
            label = entry.get("label", entry["key"])
            self._layout.addRow(f"{label}:", self._row(entry["key"], field.widget))
        self._link_profile_fields(schema)
        self._link_action_fields(schema)
        for key, field in self._fields.items():  # after linking, so a profile change clears the page before `changed`
            _watch(field.widget, self._edit_slot(key))
        self._defaults = self._compute_defaults(schema) if self._track_defaults else {}
        self._show_errors()

    def _row(self, key: str, control: QWidget) -> QWidget:
        row = QWidget()
        layout = QVBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(control)
        error = QLabel()
        error.setProperty("role", "error")
        error.setWordWrap(True)
        error.hide()
        layout.addWidget(error)
        self._error_labels[key] = error
        return row

    def errors(self) -> dict[str, str]:
        """Validation messages for the values entered, keyed by schema key."""
        return validate_settings(self._parsed, self.values())

    def has_errors(self) -> bool:
        """Whether any field currently shows an error."""
        return bool(self._displayed_errors())

    def set_errors(self, errors: Mapping[str, str]) -> None:
        """Show server-reported errors on their fields until each is edited; unknown keys are ignored."""
        self._server_errors = {key: str(message) for key, message in errors.items() if key in self._fields}
        self._show_errors()

    def _displayed_errors(self) -> dict[str, str]:
        return {**self.errors(), **self._server_errors}

    def _show_errors(self) -> None:
        shown = self._displayed_errors()
        for key, label in self._error_labels.items():
            label.setText(shown.get(key, ""))
            label.setVisible(key in shown)

    def _edit_slot(self, key: str) -> Callable[[], None]:
        return lambda: self._on_edited(key)

    def _on_edited(self, key: str) -> None:
        self._server_errors.pop(key, None)
        self._show_errors()
        self.changed.emit()

    def _link_profile_fields(self, schema: list[dict[str, Any]]) -> None:
        """Make each page field list the pages of the profile chosen in its ``profile_field``."""
        for entry in schema:
            profile = self._fields.get(str(entry.get("profile_field", "")))
            page = self._fields.get(str(entry.get("key"))) if profile is not None else None
            if profile is None or page is None:
                continue
            profile_edit, page_edit = profile.widget, page.widget
            if isinstance(profile_edit, ProfileEdit) and isinstance(page_edit, PageEdit):
                page_edit.set_profile(profile_edit.value())
                profile_edit.value_changed.connect(
                    lambda pe=profile_edit, ge=page_edit: ge.set_profile(pe.value(), clear_missing=True)
                )

    def _link_action_fields(self, schema: list[dict[str, Any]]) -> None:
        """Make each action_params field show the settings of the action chosen in its ``action_field``."""
        for entry in schema:
            chosen = self._fields.get(str(entry.get("action_field", "")))
            params = self._fields.get(str(entry.get("key"))) if chosen is not None else None
            if chosen is None or params is None:
                continue
            action_edit, params_edit = chosen.widget, params.widget
            if isinstance(action_edit, ActionEdit) and isinstance(params_edit, ActionParamsEdit):
                params_edit.set_action(action_edit.value(), params_edit.value())
                action_edit.value_changed.connect(lambda ae=action_edit, pe=params_edit: pe.set_action(ae.value(), {}))

    def values(self) -> dict[str, Any]:
        """The values currently entered, keyed by schema key."""
        return {key: field.get() for key, field in self._fields.items()}

    def defaults(self) -> dict[str, Any]:
        """What ``values()`` returns for the current schema when nothing has been entered."""
        return dict(self._defaults)

    def _compute_defaults(self, schema: list[dict[str, Any]]) -> dict[str, Any]:
        probe = SchemaForm(track_defaults=False)
        probe.set_schema(schema, {})
        result = probe.values()
        return result


class ActionParamsEdit(QWidget):
    """The settings form of one action; the owner tells it which action with ``set_action``."""

    value_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._form = SchemaForm()
        self._hint = QLabel()
        self._hint.setProperty("role", "muted")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._hint)
        layout.addWidget(self._form)
        self._action = ""
        self._known = False
        self._pending: dict[str, Any] = {}
        self._form.changed.connect(self.value_changed.emit)

    def value(self) -> dict[str, Any]:
        """The action's settings as entered (the stored ones while its action cannot be shown)."""
        return self._form.values() if self._action and self._known else dict(self._pending)

    def errors(self) -> dict[str, str]:
        """Validation messages for the values entered, keyed by schema key."""
        return self._form.errors()

    def has_errors(self) -> bool:
        """Whether any field of the action's form currently shows an error."""
        return self._form.has_errors()

    def set_errors(self, errors: Mapping[str, str]) -> None:
        """Show server-reported errors on the action's fields until each is edited."""
        self._form.set_errors(errors)

    def set_value(self, value: Any) -> None:
        """Remember stored settings; they show once ``set_action`` names their action. Emits nothing."""
        self._pending = dict(value) if isinstance(value, dict) else {}
        if self._action:
            self.set_action(self._action, self._pending)

    def set_action(self, action_id: str, values: dict[str, Any]) -> None:
        """Show the settings form of ``action_id`` filled from ``values``; an empty id shows nothing. Emits nothing."""
        self._action = action_id
        self._pending = dict(values)
        schema: list[dict[str, Any]] = []
        plugin_id = ""
        known = False
        for action in fetch_actions(AppContext.get().connection_manager) or []:
            if str(action.get("action_uuid")) == action_id:
                schema, known = list(action.get("settings_schema") or []), True
                plugin_id = str(action.get("plugin_uuid") or "")
        self._known = known
        self._form.blockSignals(True)
        self._form.set_schema(schema, self._pending, plugin_id)
        self._form.blockSignals(False)
        if not action_id:
            self._hint.setText("Choose an action first")
        elif not known:
            self._hint.setText("This action is not available")
        else:
            self._hint.setText("" if schema else "This action has no settings")
        self._hint.setVisible(bool(self._hint.text()))
        self._form.setVisible(bool(schema))


def _clear_layout(layout: QHBoxLayout | QVBoxLayout | QGridLayout) -> None:
    """Remove and delete every widget in ``layout``, descending into nested layouts."""
    while layout.count():
        item = layout.takeAt(0)
        if item is None:
            continue
        if (widget := item.widget()) is not None:
            widget.setParent(None)
            widget.deleteLater()
        elif isinstance(nested := item.layout(), (QHBoxLayout, QVBoxLayout, QGridLayout)):
            _clear_layout(nested)


class AppearanceForm(QWidget):
    """The key's appearance as aligned ``Label: [control]`` rows (title, background, icon)."""

    changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setColumnStretch(1, 1)
        self._fields: dict[str, _Field] = {}

    def set_schema(self, schema: list[dict[str, Any]], values: dict[str, Any], plugin_id: str = "") -> None:
        """Rebuild the rows for ``schema``; sections are ignored."""
        _clear_layout(self._grid)
        self._fields = {}
        row = 0
        for entry in schema:
            field = None if "section" in entry else _build_field(entry, values, plugin_id)
            if field is None:
                continue
            if isinstance(field.widget, ColorButton):
                field.widget.set_show_name(False)
            if isinstance(field.widget, IconEdit):
                field.widget.set_toolbar(True)
            self._fields[entry["key"]] = field
            _watch(field.widget, self.changed.emit)
            label = QLabel(f"{entry.get('label', entry['key'])}:")
            self._grid.addWidget(label, row, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
            wide = entry.get("type") == "text_style"  # the title takes the whole width so its toolbar has room
            self._grid.addWidget(field.widget, row, 1, Qt.AlignmentFlag(0) if wide else Qt.AlignmentFlag.AlignLeft)
            row += 1

    def values(self) -> dict[str, Any]:
        """The values currently entered, keyed by schema key."""
        return {key: field.get() for key, field in self._fields.items()}


def _parse(schema: list[dict[str, Any]]) -> tuple[SettingEntry, ...]:
    try:
        return parse_settings_schema(schema, "form")
    except ValueError:
        return ()


def _section(title: str) -> QLabel:
    label = QLabel(title)
    label.setProperty("role", "title")
    label.setAlignment(Qt.AlignmentFlag.AlignLeft)
    return label
