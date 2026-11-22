import json
from typing import Any

import pytest
from assertpy import assert_that
from PyQt6.QtGui import QStandardItemModel
from PyQt6.QtWidgets import QLabel, QPushButton

from decker.client.context import AppContext
from decker.client.widgets import dynamic_choice_edit, schema_form
from decker.client.widgets.action_edit import ActionEdit
from decker.client.widgets.bindings_edit import BindingsEdit, BindingsSpec
from decker.client.widgets.dynamic_choice_edit import DynamicChoiceEdit
from decker.plugin.bindings import NOTHING_ACTION_ID

_URL_SCHEMA: list[dict[str, Any]] = [
    {"key": "url", "type": "text", "label": "URL", "default": "", "required": True},
    {"key": "n", "type": "int", "label": "N", "default": 3, "min": 1, "max": 9},
]
_VOICE_SCHEMA: list[dict[str, Any]] = [
    {"key": "voice", "type": "choice", "label": "Voice", "options_source": "voices", "options": []}
]


def _action(plugin: str, name: str, scope: str, schema: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "plugin_uuid": plugin,
        "plugin_name": plugin,
        "action_uuid": f"{plugin}.{name}",
        "action_name": name.title(),
        "scope": scope,
        "settings_schema": schema or [],
        "icon_b64": "",
    }


_ACTIONS = [
    _action("com.decker.core", "nothing", "global"),
    _action("com.a", "own", "plugin", _URL_SCHEMA),
    _action("com.a", "shared", "global"),
    _action("com.b", "private", "plugin"),
    _action("com.b", "web", "global", _URL_SCHEMA),
    _action("com.b", "say", "global", _VOICE_SCHEMA),
]
_OWN = {"action": "com.a.own", "args": {"url": "http://x", "n": 3}}


@pytest.fixture(autouse=True)
def _rpc(qapp):  # pylint: disable=unused-argument
    class _Rpc:
        def call(self, method: str, *_args: Any) -> Any:
            return json.dumps(_ACTIONS) if method == "plugins_actions_list" else None

    AppContext._instance = None  # pylint: disable=protected-access
    AppContext.get().connection_manager = _Rpc()  # type: ignore[assignment]
    yield
    AppContext._instance = None  # pylint: disable=protected-access


def _edit(defaults: dict[str, Any] | None = None, stored: dict[str, Any] | None = None) -> BindingsEdit:
    edit = BindingsEdit()
    edit.set_bindings(BindingsSpec(defaults or {}, stored or {}), "com.a")
    return edit


def _combos(edit: BindingsEdit) -> list[ActionEdit]:
    return edit.findChildren(ActionEdit)


def _choose(combo: ActionEdit, action_id: str) -> None:
    index = combo.findData(action_id)
    assert index >= 0
    combo.setCurrentIndex(index)
    combo.activated.emit(index)


def _texts(combo: ActionEdit) -> list[str]:
    return [combo.itemText(i) for i in range(combo.count())]


def _reset_buttons(edit: BindingsEdit) -> list[QPushButton]:
    return [b for b in edit.findChildren(QPushButton) if b.text() == "Reset to default"]


def _visible_texts(edit: BindingsEdit) -> list[str]:
    return [label.text() for label in edit.findChildren(QLabel) if label.text() and not label.isHidden()]


class TestBindingsEdit:  # pylint: disable=protected-access
    def test_dropdown_lists_nothing_own_actions_and_globals_only(self) -> None:
        edit = _edit()

        assert_that(_texts(_combos(edit)[0])).is_equal_to(["Do Nothing", "Own", "Say", "Shared", "Web"])

    def test_generic_action_field_lists_only_globals(self) -> None:
        combo = ActionEdit()

        assert_that(_texts(combo)).is_equal_to(["(choose an action)", "Say", "Shared", "Web"])

    def test_row_shows_manifest_default_until_changed(self) -> None:
        edit = _edit(defaults={"press": _OWN})

        assert_that([c.value() for c in _combos(edit)]).is_equal_to(["com.a.own", NOTHING_ACTION_ID, NOTHING_ACTION_ID])
        assert_that(edit.value()).is_equal_to({})

    def test_stored_binding_wins_over_the_default(self) -> None:
        edit = _edit(defaults={"press": _OWN}, stored={"press": {"action": "com.a.shared"}})

        assert_that(_combos(edit)[0].value()).is_equal_to("com.a.shared")
        assert_that(edit.value()).is_equal_to({"press": {"action": "com.a.shared"}})

    def test_changing_action_resets_args_to_defaults(self) -> None:
        edit = _edit(defaults={"press": {"action": "com.a.own", "args": {"url": "http://x", "n": 7}}})
        _choose(_combos(edit)[0], "com.b.web")

        assert_that(edit.value()).is_equal_to({"press": {"action": "com.b.web", "args": {"url": "", "n": 3}}})

    def test_action_without_settings_shows_hint(self) -> None:
        edit = _edit(stored={"press": {"action": "com.a.shared"}})

        assert_that(_visible_texts(edit)).contains("This action has no settings")

    def test_reset_to_default_omits_gesture_from_value(self) -> None:
        edit = _edit(defaults={"press": _OWN})
        buttons = _reset_buttons(edit)
        assert_that(buttons[0].isEnabled()).is_false()
        _choose(_combos(edit)[0], "com.a.shared")
        assert_that(edit.value()).contains_key("press")
        assert_that(buttons[0].isEnabled()).is_true()

        buttons[0].click()

        assert_that(edit.value()).is_equal_to({})
        assert_that(_combos(edit)[0].value()).is_equal_to("com.a.own")
        assert_that(buttons[0].isEnabled()).is_false()

    def test_explicit_nothing_is_kept_in_value(self) -> None:
        edit = _edit(defaults={"press": _OWN})

        _choose(_combos(edit)[0], NOTHING_ACTION_ID)

        assert_that(edit.value()).is_equal_to({"press": {"action": NOTHING_ACTION_ID}})

    def test_edits_emit_changed(self) -> None:
        edit = _edit()
        seen: list[int] = []
        edit.changed.connect(lambda: seen.append(1))

        _choose(_combos(edit)[1], "com.a.shared")

        assert_that(seen).is_length(1)

    def test_binding_arg_errors_show_inline(self) -> None:
        edit = _edit(stored={"long_press": {"action": "com.b.web", "args": {"url": "x", "n": 3}}})

        edit.set_errors({"long_press.url": "is not valid"})

        assert_that(_visible_texts(edit)).contains("is not valid")
        assert_that(edit.has_errors()).is_true()

    def test_gesture_level_error_shows_on_the_row_until_it_is_edited(self) -> None:
        edit = _edit()

        edit.set_errors({"press": "this card cannot bind that action"})

        assert_that(_visible_texts(edit)).contains("this card cannot bind that action")
        assert_that(edit.has_errors()).is_true()
        _choose(_combos(edit)[0], "com.a.shared")
        assert_that(_visible_texts(edit)).does_not_contain("this card cannot bind that action")
        assert_that(edit.has_errors()).is_false()

    def test_errors_include_each_rows_argument_form_errors(self) -> None:
        edit = _edit(stored={"double_press": {"action": "com.b.web", "args": {"url": "", "n": 3}}})

        assert_that(edit.errors()).is_equal_to({"double_press.url": "is required"})
        assert_that(edit.has_errors()).is_true()

    def test_missing_action_stays_selected_and_disabled(self) -> None:
        edit = _edit(stored={"press": {"action": "com.gone.thing", "args": {"k": 1}}})
        combo = _combos(edit)[0]

        assert_that(combo.value()).is_equal_to("com.gone.thing")
        assert_that(combo.currentText()).is_equal_to("(missing action)")
        model = combo.model()
        assert isinstance(model, QStandardItemModel)
        item = model.item(combo.currentIndex())
        assert item is not None
        assert_that(item.isEnabled()).is_false()
        assert_that(edit.value()).is_equal_to({"press": {"action": "com.gone.thing", "args": {"k": 1}}})

    def test_binding_arg_form_asks_the_action_plugin_for_options(self, monkeypatch) -> None:
        monkeypatch.setattr(dynamic_choice_edit, "_default_fetch", lambda: lambda _p, _s: (True, [], ""))
        monkeypatch.setattr(schema_form, "fetch_actions", lambda _rpc: _ACTIONS)

        edit = _edit(stored={"press": {"action": "com.b.say", "args": {}}})

        field = edit.findChildren(DynamicChoiceEdit)[0]
        assert_that(field._plugin_id).is_equal_to("com.b")
