import json
from typing import Any

from assertpy import assert_that
from PyQt6.QtCore import QByteArray, QMimeData, QPointF, Qt
from PyQt6.QtGui import QDragEnterEvent, QDropEvent

from decker.client.widgets import dynamic_choice_edit
from decker.client.widgets.dynamic_choice_edit import DynamicChoiceEdit
from decker.client.widgets.slots_edit import SlotsEdit, _SlotButton
from decker.common.slots import SWITCH_CARD_ID

_CARDS = [{"plugin_uuid": "p", "card_uuid": "c", "card_name": "Clock"}]
_ACTIONS = [{"action_uuid": "http", "action_name": "HTTP Request"}]
_CARD = {"plugin_uuid": "p", "plugin_name": "P", "card_uuid": "c", "card_name": "Clock"}
_ACTION = {"plugin_uuid": "core", "card_uuid": "run", "preset_action": "http"}


_URL = [{"key": "url", "type": "text", "label": "URL"}]
_CFG_CARDS = [{**_CARDS[0], "settings_schema": _URL}]
_CFG_ACTIONS = [{**_ACTIONS[0], "settings_schema": _URL}]


_DEFAULTS = {"background": "#336699", "title": {"text": "Hi", "position": "middle"}}
_DEF_CARDS = [{**_CFG_CARDS[0], "appearance": _DEFAULTS}]


def _edit() -> SlotsEdit:
    return SlotsEdit(catalog=lambda: (_CARDS, _ACTIONS))


def _cfg_edit() -> SlotsEdit:
    return SlotsEdit(catalog=lambda: (_CFG_CARDS, _CFG_ACTIONS))


def _slot(edit: SlotsEdit, name: str) -> dict[str, Any]:
    value = edit.slot_value(name)
    assert value is not None
    return value


def _type_url(edit: SlotsEdit, text: str) -> None:
    widget: Any = edit._config.settings_form._fields["url"].widget  # pylint: disable=protected-access
    widget.setText(text)


def _card_slot(url: str = "") -> dict[str, Any]:
    return {"kind": "card", "plugin": "p", "card": "c", "settings": {"url": url}, "appearance": {}}


def _mime(payload: dict[str, Any]) -> QMimeData:
    mime = QMimeData()
    mime.setData("application/x-decker-card", QByteArray(json.dumps(payload).encode()))
    return mime


def _drop(button: _SlotButton, payload: dict[str, Any]) -> None:
    mime = _mime(payload)  # the event does not own it
    event = QDropEvent(
        QPointF(5, 5), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    )
    button.dropEvent(event)


def _enter_accepted(button: _SlotButton, payload: dict[str, Any]) -> bool:
    mime = _mime(payload)  # the event does not own it
    event = QDragEnterEvent(
        QPointF(5, 5).toPoint(),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    event.ignore()
    button.dragEnterEvent(event)
    return event.isAccepted()


class TestSlotsEdit:  # pylint: disable=protected-access
    def test_drop_card_fills_slot_and_emits_changed(self, qapp):  # pylint: disable=unused-argument
        edit = _edit()
        changes: list[int] = []
        edit.changed.connect(lambda: changes.append(1))

        _drop(edit._buttons["press"], _CARD)

        assert_that(edit.slot_value("press")).is_equal_to(
            {"kind": "card", "plugin": "p", "card": "c", "settings": {}, "appearance": {}}
        )
        assert_that(changes).is_length(1)
        assert_that(edit._buttons["press"].item_text()).is_equal_to("Clock")

    def test_drop_preset_action_fills_action_slot(self, qapp):  # pylint: disable=unused-argument
        edit = _edit()

        _drop(edit._buttons["long_press"], _ACTION)

        assert_that(edit.slot_value("long_press")).is_equal_to({"kind": "action", "action": "http", "settings": {}})
        assert_that(edit._buttons["long_press"].item_text()).is_equal_to("HTTP Request")

    def test_display_rejects_action_drop(self, qapp):  # pylint: disable=unused-argument
        edit = _edit()
        button = edit._buttons["display"]

        _drop(button, _ACTION)

        assert_that(_enter_accepted(button, _ACTION)).is_false()
        assert_that(_enter_accepted(button, _CARD)).is_true()
        assert_that(edit.slot_value("display")).is_none()

    def test_switch_card_drop_rejected(self, qapp):  # pylint: disable=unused-argument
        edit = _edit()
        payload = {**_CARD, "card_uuid": SWITCH_CARD_ID}

        for name, button in edit._buttons.items():
            _drop(button, payload)
            assert_that(_enter_accepted(button, payload)).is_false()
            assert_that(edit.slot_value(name)).is_none()

    def test_clear_badge_empties_slot(self, qapp):  # pylint: disable=unused-argument
        edit = _edit()
        _drop(edit._buttons["press"], _CARD)
        changes: list[int] = []
        edit.changed.connect(lambda: changes.append(1))

        edit._buttons["press"].clear_button.click()

        assert_that(edit.slot_value("press")).is_none()
        assert_that(changes).is_length(1)

    def test_click_selects_slot_and_emits_selected_changed(self, qapp):  # pylint: disable=unused-argument
        edit = _edit()
        selected: list[str] = []
        edit.selected_changed.connect(selected.append)

        edit._buttons["double_press"].clicked.emit("double_press")

        assert_that(edit.selected_slot()).is_equal_to("double_press")
        assert_that(selected).is_equal_to(["double_press"])

    def test_unknown_items_show_fallback_label(self, qapp):  # pylint: disable=unused-argument
        edit = _edit()
        edit.set_value({"press": {"kind": "card", "plugin": "x", "card": "gone", "settings": {}, "appearance": {}}})

        assert_that(edit._buttons["press"].item_text()).contains("missing card")

    def test_set_value_does_not_emit_changed(self, qapp):  # pylint: disable=unused-argument
        edit = _edit()
        changes: list[int] = []
        edit.changed.connect(lambda: changes.append(1))

        edit.set_value({"press": {"kind": "action", "action": "http", "settings": {"a": 1}}})

        assert_that(changes).is_empty()
        assert_that(edit.value()["press"]).is_equal_to({"kind": "action", "action": "http", "settings": {"a": 1}})


class TestSlotConfig:  # pylint: disable=protected-access
    def test_selecting_card_slot_shows_its_settings_form(self, qapp):  # pylint: disable=unused-argument
        edit = _cfg_edit()
        edit.set_value({"press": _card_slot("http://x")})

        edit._select("press")

        config = edit._config
        assert_that(config.settings_form.values()).is_equal_to({"url": "http://x"})
        assert_that(config.appearance_form.values()).contains_key("title")
        assert_that(config.isHidden()).is_false()

    def test_editing_card_slot_settings_updates_slot_value(self, qapp):  # pylint: disable=unused-argument
        edit = _cfg_edit()
        edit.set_value({"press": _card_slot("a")})
        edit._select("press")
        changes: list[int] = []
        edit.changed.connect(lambda: changes.append(1))

        _type_url(edit, "b")

        assert_that(_slot(edit, "press")["settings"]).is_equal_to({"url": "b"})
        assert_that(changes).is_not_empty()
        assert_that(edit._config.settings_form.values()).is_equal_to({"url": "b"})

    def test_editing_card_slot_appearance_updates_slot_value(self, qapp):  # pylint: disable=unused-argument
        edit = _cfg_edit()
        edit.set_value({"press": _card_slot()})
        edit._select("press")

        title: Any = edit._config.appearance_form._fields["title"].widget
        line = title._line
        line.setText("Hi")
        line.textEdited.emit("Hi")

        assert_that(_slot(edit, "press")["appearance"]["title"]["text"]).is_equal_to("Hi")

    def test_card_manifest_appearance_defaults_show_in_the_form(self, qapp):  # pylint: disable=unused-argument
        edit = SlotsEdit(catalog=lambda: (_DEF_CARDS, _ACTIONS))
        edit.set_value({"press": _card_slot()})

        edit._select("press")

        values = edit._config.appearance_form.values()
        assert_that(values["title"]["text"]).is_equal_to("Hi")
        assert_that(values["background"]["color"]).is_equal_to("#336699")

    def test_editing_settings_does_not_pin_appearance_defaults(self, qapp):  # pylint: disable=unused-argument
        edit = SlotsEdit(catalog=lambda: (_DEF_CARDS, _ACTIONS))
        edit.set_value({"press": _card_slot()})
        edit._select("press")

        _type_url(edit, "b")

        assert_that(_slot(edit, "press")["appearance"]).is_equal_to({})

    def test_changing_one_appearance_field_stores_only_that_difference(self, qapp):  # pylint: disable=unused-argument
        edit = SlotsEdit(catalog=lambda: (_DEF_CARDS, _ACTIONS))
        edit.set_value({"press": _card_slot()})
        edit._select("press")

        title: Any = edit._config.appearance_form._fields["title"].widget
        title._line.setText("Yo")
        title._line.textEdited.emit("Yo")

        appearance = _slot(edit, "press")["appearance"]
        assert_that(appearance).contains_key("title").does_not_contain_key("background")
        assert_that(appearance["title"]["text"]).is_equal_to("Yo")

    def test_selecting_action_slot_shows_action_settings(self, qapp):  # pylint: disable=unused-argument
        edit = _cfg_edit()
        edit.set_value({"press": {"kind": "action", "action": "http", "settings": {"url": "u"}}})
        changes: list[int] = []
        edit.changed.connect(lambda: changes.append(1))

        edit._select("press")

        assert_that(changes).is_empty()
        assert_that(edit._config.settings_form.values()).is_equal_to({"url": "u"})
        assert_that(edit._config.appearance_form.isHidden()).is_true()
        _type_url(edit, "v")
        assert_that(edit.slot_value("press")).is_equal_to(
            {"kind": "action", "action": "http", "settings": {"url": "v"}}
        )

    def test_selecting_empty_slot_shows_nothing(self, qapp):  # pylint: disable=unused-argument
        edit = _cfg_edit()

        edit._select("press")

        assert_that(edit._config.isHidden()).is_true()

    def test_nothing_selected_shows_nothing(self, qapp):  # pylint: disable=unused-argument
        edit = _cfg_edit()

        assert_that(edit._config.isHidden()).is_true()

    def test_clearing_selected_slot_hides_its_form(self, qapp):  # pylint: disable=unused-argument
        edit = _cfg_edit()
        edit.set_value({"press": _card_slot()})
        edit._select("press")
        assert_that(edit._config.isHidden()).is_false()

        edit._buttons["press"].clear_button.click()

        assert_that(edit._config.isHidden()).is_true()

    def test_missing_card_shows_no_form(self, qapp):  # pylint: disable=unused-argument
        edit = _edit()
        edit.set_value({"press": {"kind": "card", "plugin": "x", "card": "y", "settings": {}, "appearance": {}}})

        edit._select("press")

        assert_that(edit._config.isHidden()).is_true()


class TestDropPinning:  # pylint: disable=protected-access
    def test_run_action_card_without_preset_makes_card_slot_everywhere(self, qapp):  # pylint: disable=unused-argument
        edit = _edit()
        payload = {"plugin_uuid": "com.decker.core", "card_uuid": "com.decker.core.runaction"}

        for name in edit._buttons:
            _drop(edit._buttons[name], payload)
            assert_that(_slot(edit, name)["kind"]).is_equal_to("card")

    def test_plugin_preset_card_makes_card_slot_with_empty_settings(self, qapp):  # pylint: disable=unused-argument
        edit = _edit()

        _drop(edit._buttons["press"], {"plugin_uuid": "p", "card_uuid": "preset-card", "action": "http"})

        assert_that(edit.slot_value("press")).is_equal_to(
            {"kind": "card", "plugin": "p", "card": "preset-card", "settings": {}, "appearance": {}}
        )


_VOICE = [{"key": "voice", "type": "choice", "label": "Voice", "options_source": "voices", "options": []}]


class TestSlotConfigPluginId:  # pylint: disable=protected-access
    def _field(self, value: dict[str, Any], monkeypatch) -> DynamicChoiceEdit:
        monkeypatch.setattr(dynamic_choice_edit, "_default_fetch", lambda: lambda _p, _s: (True, [], ""))
        cards = [{**_CARDS[0], "settings_schema": _VOICE}]
        actions = [{**_ACTIONS[0], "plugin_uuid": "com.t.x", "settings_schema": _VOICE}]
        edit = SlotsEdit(catalog=lambda: (cards, actions))
        edit.set_value({"press": value})
        edit._select("press")
        field = edit._config.settings_form._fields["voice"].widget
        assert isinstance(field, DynamicChoiceEdit)
        if field._thread is not None:
            field._thread.join(5)
        return field

    def test_slot_config_uses_the_slots_plugin_id_for_a_card_slot(
        self, qapp, monkeypatch
    ):  # pylint: disable=unused-argument
        value = {"kind": "card", "plugin": "p", "card": "c", "settings": {}, "appearance": {}}
        assert_that(self._field(value, monkeypatch)._plugin_id).is_equal_to("p")

    def test_slot_config_uses_the_slots_plugin_id_for_an_action_slot(
        self, qapp, monkeypatch
    ):  # pylint: disable=unused-argument
        value = {"kind": "action", "action": "http", "settings": {}}
        assert_that(self._field(value, monkeypatch)._plugin_id).is_equal_to("com.t.x")
