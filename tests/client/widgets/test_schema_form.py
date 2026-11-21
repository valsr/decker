from typing import Any

import pytest
from assertpy import assert_that
from PyQt6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
)

from decker.client.widgets.color_button import ColorButton
from decker.client.widgets.dynamic_choice_edit import DynamicChoiceEdit
from decker.client.widgets.schema_form import AppearanceForm, SchemaForm
from decker.client.widgets.text_style_edit import TextStyleEdit
from decker.common.model import DeviceHardwareInfo
from decker.common.text_style import TEXT_STYLE_DEFAULTS


def _form(schema: list[dict[str, Any]], values: dict[str, Any] | None = None) -> SchemaForm:
    form = SchemaForm()
    form.set_schema(schema, values or {})
    return form


class TestSchemaForm:  # pylint: disable=protected-access
    def test_int_round_trips(self, qapp):  # pylint: disable=unused-argument
        form = _form([{"key": "n", "type": "int", "label": "N", "default": 1, "min": 1, "max": 9}], {"n": 5})
        assert_that(form.values()).is_equal_to({"n": 5})

    def test_int_special_value_text(self, qapp):  # pylint: disable=unused-argument
        form = _form([{"key": "n", "type": "int", "default": 0, "min": 0, "special_value": "No limit"}])
        spin = form._fields["n"].widget
        assert isinstance(spin, QSpinBox)
        assert_that(spin.specialValueText()).is_equal_to("No limit")

    def test_optional_int_round_trips(self, qapp):  # pylint: disable=unused-argument
        form = _form([{"key": "n", "type": "int", "optional": True}], {"n": 4})
        assert_that(form.values()).is_equal_to({"n": 4})

    def test_optional_int_empty_is_none(self, qapp):  # pylint: disable=unused-argument
        form = _form([{"key": "n", "type": "int", "optional": True}], {"n": 4})
        widget = form._fields["n"].widget
        assert isinstance(widget, QLineEdit)
        widget.setText("")
        assert_that(form.values()["n"]).is_none()

    def test_float_round_trips(self, qapp):  # pylint: disable=unused-argument
        form = _form([{"key": "f", "type": "float", "default": 1.0, "min": 0, "max": 5, "step": 0.5, "decimals": 2}])
        form._fields["f"].set(2.25)
        assert_that(form.values()["f"]).is_equal_to(2.25)

    def test_text_round_trips(self, qapp):  # pylint: disable=unused-argument
        form = _form([{"key": "t", "type": "text", "default": "", "placeholder": "hint"}], {"t": "hello"})
        assert_that(form.values()).is_equal_to({"t": "hello"})
        widget = form._fields["t"].widget
        assert isinstance(widget, QLineEdit)
        assert_that(widget.placeholderText()).is_equal_to("hint")

    def test_multiline_text_round_trips(self, qapp):  # pylint: disable=unused-argument
        form = _form([{"key": "t", "type": "text", "multiline": True, "placeholder": "p"}], {"t": "a\nb"})
        assert isinstance(form._fields["t"].widget, QPlainTextEdit)
        assert_that(form.values()["t"]).is_equal_to("a\nb")

    def test_bool_round_trips(self, qapp):  # pylint: disable=unused-argument
        form = _form([{"key": "b", "type": "bool", "default": False}], {"b": True})
        assert isinstance(form._fields["b"].widget, QCheckBox)
        assert_that(form.values()["b"]).is_true()

    def test_choice_round_trips(self, qapp):  # pylint: disable=unused-argument
        schema = [
            {
                "key": "c",
                "type": "choice",
                "default": "a",
                "options": [{"label": "A", "value": "a"}, {"label": "B", "value": "b"}],
            }
        ]
        assert_that(_form(schema, {"c": "b"}).values()["c"]).is_equal_to("b")
        assert_that(_form(schema, {"c": "zzz"}).values()["c"]).is_equal_to("a")

    def test_color_round_trips(self, qapp):  # pylint: disable=unused-argument
        form = _form([{"key": "c", "type": "color", "default": "#000000"}], {"c": "#123456"})
        assert_that(form.values()["c"]).is_equal_to("#123456")

    def test_optional_color_round_trips_and_unchecked_is_none(self, qapp):  # pylint: disable=unused-argument
        schema = [{"key": "c", "type": "color", "optional": True}]
        assert_that(_form(schema, {"c": "#123456"}).values()["c"]).is_equal_to("#123456")
        assert_that(_form(schema, {}).values()["c"]).is_none()

    def test_font_round_trips(self, qapp):  # pylint: disable=unused-argument
        form = _form([{"key": "f", "type": "font", "default": "DejaVu Sans"}], {"f": "DejaVu Sans"})
        assert_that(form.values()["f"]).is_instance_of(str).is_not_empty()

    def test_icon_round_trips(self, qapp):  # pylint: disable=unused-argument
        value = {"ref": "pack:p/a.svg", "color": "#112233"}
        form = _form([{"key": "i", "type": "icon"}], {"i": value})
        assert_that(form.values()["i"]).is_equal_to(value)

    def test_sections_render_as_headings(self, qapp):  # pylint: disable=unused-argument
        form = _form([{"section": "Counting"}, {"key": "n", "type": "bool"}])
        titles = [lb.text() for lb in form.findChildren(QLabel) if lb.property("role") == "title"]
        assert_that(titles).is_equal_to(["Counting"])

    def test_help_becomes_tooltip(self, qapp):  # pylint: disable=unused-argument
        form = _form([{"key": "n", "type": "bool", "help": "Explains"}])
        assert_that(form._fields["n"].widget.toolTip()).is_equal_to("Explains")

    def test_values_returns_defaults_for_untouched_fields(self, qapp):  # pylint: disable=unused-argument
        schema: list[dict[str, Any]] = [
            {"key": "n", "type": "int", "default": 3, "min": 0, "max": 9},
            {"key": "t", "type": "text", "default": "x"},
            {"key": "b", "type": "bool", "default": True},
        ]
        assert_that(_form(schema).values()).is_equal_to({"n": 3, "t": "x", "b": True})

    def test_set_schema_replaces_previous_fields(self, qapp):  # pylint: disable=unused-argument
        form = _form([{"key": "a", "type": "bool"}])
        form.set_schema([{"key": "b", "type": "bool"}], {})
        assert_that(form.values()).contains_key("b").does_not_contain_key("a")


class TestTextStyleField:  # pylint: disable=protected-access
    _ENTRY: dict[str, Any] = {"key": "label", "type": "text_style", "label": "Label"}

    def test_round_trips(self, qapp):  # pylint: disable=unused-argument
        form = _form([self._ENTRY], {"label": {"text": "Hi", "bold": True}})
        assert isinstance(form._fields["label"].widget, TextStyleEdit)
        assert_that(form.values()["label"]).is_equal_to({**TEXT_STYLE_DEFAULTS, "text": "Hi", "bold": True})

    def test_default_applied(self, qapp):  # pylint: disable=unused-argument
        default = {**TEXT_STYLE_DEFAULTS, "position": "top"}
        form = _form([{**self._ENTRY, "default": default}])
        assert_that(form.values()["label"]).is_equal_to(default)

    def test_two_fields_are_independent(self, qapp):  # pylint: disable=unused-argument
        form = _form([self._ENTRY, {**self._ENTRY, "key": "other", "label": "Other"}], {"label": {"text": "A"}})
        form._fields["other"].set({"text": "B", "italic": True})
        assert_that(form.values()["label"]["text"]).is_equal_to("A")
        assert_that(form.values()["label"]["italic"]).is_false()
        assert_that(form.values()["other"]).contains_entry({"text": "B"}, {"italic": True})

    def test_row_label(self, qapp):  # pylint: disable=unused-argument
        form = _form([self._ENTRY])
        assert_that([lb.text() for lb in form.findChildren(QLabel)]).contains("Label:")


class TestAlphaColorField:  # pylint: disable=protected-access
    _ENTRY: dict[str, Any] = {"key": "bg", "type": "color", "default": "#000000", "alpha": True}

    def test_rgba_round_trips(self, qapp):  # pylint: disable=unused-argument
        form = _form([self._ENTRY], {"bg": "#11223380"})
        assert_that(form.values()["bg"]).is_equal_to("#11223380")

    def test_plain_color_field_ignores_rgba(self, qapp):  # pylint: disable=unused-argument
        form = _form([{"key": "bg", "type": "color", "default": "#000000"}], {"bg": "#11223380"})
        assert_that(form.values()["bg"]).is_equal_to("#000000")


class TestAppearanceForm:  # pylint: disable=protected-access
    _SCHEMA: list[dict[str, Any]] = [
        {"section": "Ignored"},
        {"key": "title", "type": "text_style", "label": "Title"},
        {"key": "icon", "type": "icon", "label": "Icon"},
        {"key": "background", "type": "color", "label": "Background", "default": "#000000", "alpha": True},
    ]

    def test_each_field_gets_its_own_aligned_labelled_row(self, qapp):  # pylint: disable=unused-argument
        form = AppearanceForm()
        form.set_schema(self._SCHEMA, {})

        layout = form.layout()
        assert isinstance(layout, QGridLayout)
        texts = [lb.text() for lb in form.findChildren(QLabel) if lb.text().endswith(":")]
        assert_that(texts).is_equal_to(["Title:", "Icon:", "Background:"])
        rows = {key: layout.getItemPosition(layout.indexOf(f.widget))[0] for key, f in form._fields.items()}
        assert_that(rows).is_equal_to({"title": 0, "icon": 1, "background": 2})

    def test_colour_fields_are_swatch_only(self, qapp):  # pylint: disable=unused-argument
        form = AppearanceForm()
        form.set_schema(self._SCHEMA, {"background": "#11223380"})

        button = form._fields["background"].widget
        assert isinstance(button, ColorButton)
        assert_that(button.text()).is_equal_to("")
        assert_that(button.toolTip()).is_equal_to("#11223380")

    def test_regular_form_still_shows_the_colour_name(self, qapp):  # pylint: disable=unused-argument
        form = _form([{"key": "bg", "type": "color", "default": "#000000"}], {"bg": "#112233"})
        button = form._fields["bg"].widget
        assert isinstance(button, ColorButton)
        assert_that(button.text()).is_equal_to("#112233")

    def test_values_round_trip(self, qapp):  # pylint: disable=unused-argument
        form = AppearanceForm()
        form.set_schema(self._SCHEMA, {"title": {"text": "Hi"}, "background": "#11223380"})

        values = form.values()

        assert_that(values["title"]["text"]).is_equal_to("Hi")
        assert_that(values["background"]).is_equal_to("#11223380")

    def test_set_schema_twice_replaces_fields(self, qapp):  # pylint: disable=unused-argument
        form = AppearanceForm()
        form.set_schema(self._SCHEMA, {})
        form.set_schema(self._SCHEMA[1:2], {})

        assert_that(list(form.values())).is_equal_to(["title"])
        assert_that([lb.text() for lb in form.findChildren(QLabel)]).is_equal_to(["Title:"])

    def test_edits_emit_changed_but_loading_does_not(self, qapp):  # pylint: disable=unused-argument
        form = AppearanceForm()
        seen: list[bool] = []
        form.changed.connect(lambda: seen.append(True))
        form.set_schema(self._SCHEMA, {"title": {"text": "Hi"}})
        assert_that(seen).is_empty()

        form._fields["background"].set("#445566")
        form._fields["background"].widget.color_changed.emit("#445566")  # type: ignore[attr-defined]
        form._fields["icon"].widget.value_changed.emit()  # type: ignore[attr-defined]

        assert_that(seen).is_length(2)


class TestIconField:  # pylint: disable=protected-access
    def test_builds_an_icon_edit(self, qapp):  # pylint: disable=unused-argument
        from decker.client.widgets.icon_edit import (
            IconEdit,  # pylint: disable=import-outside-toplevel
        )

        form = _form([{"key": "i", "type": "icon", "label": "Icon"}])
        assert isinstance(form._fields["i"].widget, IconEdit)
        assert_that([lb.text() for lb in form.findChildren(QLabel)]).contains("Icon:")

    def test_default_applied(self, qapp):  # pylint: disable=unused-argument
        default = {"ref": "pack:p/a.svg", "color": "#ff0000"}
        form = _form([{"key": "i", "type": "icon", "default": default}])
        assert_that(form.values()["i"]).is_equal_to(default)

    def test_garbage_value_uses_defaults(self, qapp):  # pylint: disable=unused-argument
        form = _form([{"key": "i", "type": "icon"}], {"i": "/tmp/a.png"})
        assert_that(form.values()["i"]).is_equal_to({"ref": "", "color": "#ffffff"})


class TestPageField:
    def test_schema_form_builds_a_page_edit_and_round_trips(self, qapp):  # pylint: disable=unused-argument
        from decker.client.widgets.page_edit import PageEdit

        form = _form([{"key": "p", "type": "page", "label": "Page"}], {"p": "abc"})

        assert_that(form.findChild(PageEdit)).is_not_none()
        assert_that(form.values()["p"]).is_equal_to("abc")


class TestSchemaFormChanged:  # pylint: disable=protected-access
    def test_every_control_kind_emits_changed(self, qapp):  # pylint: disable=unused-argument
        schema: list[dict[str, Any]] = [
            {"key": "n", "type": "int", "default": 1},
            {"key": "t", "type": "text"},
            {"key": "b", "type": "bool"},
            {"key": "c", "type": "choice", "options": [{"label": "A", "value": "a"}, {"label": "B", "value": "b"}]},
            {"key": "col", "type": "color"},
        ]
        form = _form(schema)
        seen: list[bool] = []
        form.changed.connect(lambda: seen.append(True))

        form._fields["n"].widget.setValue(5)  # type: ignore[attr-defined]
        form._fields["t"].widget.setText("x")  # type: ignore[attr-defined]
        form._fields["b"].widget.setChecked(True)  # type: ignore[attr-defined]
        form._fields["c"].widget.setCurrentIndex(1)  # type: ignore[attr-defined]
        form._fields["col"].widget.color_changed.emit("#ffffff")  # type: ignore[attr-defined]

        assert_that(len(seen)).is_greater_than_or_equal_to(5)

    def test_loading_values_does_not_emit_changed(self, qapp):  # pylint: disable=unused-argument
        form = SchemaForm()
        seen: list[bool] = []
        form.changed.connect(lambda: seen.append(True))

        form.set_schema([{"key": "n", "type": "int"}, {"key": "t", "type": "text"}], {"n": 3, "t": "x"})

        assert_that(seen).is_empty()


class TestChangedEmitsOncePerEdit:  # pylint: disable=protected-access
    def _count(self, form, edit) -> int:
        seen: list[bool] = []
        form.changed.connect(lambda: seen.append(True))
        edit()
        return len(seen)

    def test_a_spinbox_change_emits_once(self, qapp):  # pylint: disable=unused-argument
        form = _form([{"key": "n", "type": "int", "default": 1}])
        spin = form._fields["n"].widget

        assert_that(self._count(form, lambda: spin.setValue(4))).is_equal_to(1)  # type: ignore[attr-defined]

    def test_a_font_change_emits_once(self, qapp):  # pylint: disable=unused-argument
        form = _form([{"key": "f", "type": "font"}])
        combo = form._fields["f"].widget
        combo.addItem("Another Font")  # type: ignore[attr-defined]  # a bare CI machine may know only one family
        other = combo.count() - 1  # type: ignore[attr-defined]

        assert_that(self._count(form, lambda: combo.setCurrentIndex(other))).is_equal_to(1)  # type: ignore[attr-defined]

    def test_a_title_style_change_emits_once(self, qapp):  # pylint: disable=unused-argument
        form = AppearanceForm()
        form.set_schema([{"key": "title", "type": "text_style", "label": "Title"}], {})
        edit = form._fields["title"].widget

        count = self._count(form, lambda: edit._toolbar._size.setValue(30))  # type: ignore[attr-defined]

        assert_that(count).is_equal_to(1)

    def test_an_icon_colour_change_emits_once(self, qapp, tmp_path):  # pylint: disable=unused-argument
        form = AppearanceForm()
        form.set_schema([{"key": "icon", "type": "icon", "label": "Icon"}], {})
        edit = form._fields["icon"].widget

        count = self._count(form, lambda: edit._on_colour_changed("#123456"))  # type: ignore[attr-defined]

        assert_that(count).is_equal_to(1)


class TestCompactIcon:  # pylint: disable=protected-access
    def test_the_appearance_form_docks_the_icon_in_a_toolbar_and_regular_forms_do_not(
        self, qapp
    ):  # pylint: disable=unused-argument
        appearance = AppearanceForm()
        appearance.set_schema([{"key": "icon", "type": "icon", "label": "Icon"}], {})
        regular = _form([{"key": "icon", "type": "icon", "label": "Icon"}])

        assert_that(appearance._fields["icon"].widget._toolbar).is_true()  # type: ignore[attr-defined]
        assert_that(regular._fields["icon"].widget._toolbar).is_false()  # type: ignore[attr-defined]


class TestProfileFields:  # pylint: disable=protected-access
    def setup_method(self):
        from decker.client.context import AppContext

        AppContext._instance = None
        self.calls: list[tuple[Any, ...]] = []
        outer = self

        class _Rpc:
            def call(self, method, *args):
                import json

                outer.calls.append((method, *args))
                if method == "profiles_list":
                    profiles = [{"profile_id": 0, "name": "Default"}, {"profile_id": 2, "name": "Work"}]
                    return json.dumps({"device_id": "dev1", "profiles": profiles, "active_profile_id": 0})
                pages = [("w1", "Docs")] if args[1:] == (2,) else [("p1", "Home"), ("p2", "More")]
                return json.dumps({"pages": [{"page_id": i, "name": n} for i, n in pages]})

        context = AppContext.get()
        context.connection_manager = _Rpc()  # type: ignore[assignment]
        context.active_device = DeviceHardwareInfo(id="dev1", type="Deck", vendor_id=0, product_id=0, num_keys=6)

    _SCHEMA = [
        {"key": "who", "type": "profile", "label": "Profile"},
        {"key": "where", "type": "page", "label": "Page", "profile_field": "who"},
    ]

    def test_builds_a_profile_edit_and_round_trips(self, qapp):  # pylint: disable=unused-argument
        from decker.client.widgets.profile_edit import ProfileEdit

        form = _form(self._SCHEMA, {"who": "2", "where": "w1"})

        assert_that(form.findChild(ProfileEdit)).is_not_none()
        assert_that(form.values()).is_equal_to({"who": "2", "where": "w1"})

    def test_the_page_list_follows_the_stored_profile_on_load(self, qapp):  # pylint: disable=unused-argument
        from decker.client.widgets.page_edit import PageEdit

        form = _form(self._SCHEMA, {"who": "2", "where": "w1"})
        page = form.findChild(PageEdit)

        assert_that([page.itemText(i) for i in range(page.count())]).is_equal_to(["(no page)", "Docs"])
        assert_that(page.value()).is_equal_to("w1")

    def test_changing_the_profile_reloads_and_clears_the_page_and_emits_changed(
        self, qapp
    ):  # pylint: disable=unused-argument
        from decker.client.widgets.page_edit import PageEdit
        from decker.client.widgets.profile_edit import ProfileEdit

        form = _form(self._SCHEMA, {"who": "", "where": "p2"})
        seen: list[bool] = []
        form.changed.connect(lambda: seen.append(True))
        profile, page = form.findChild(ProfileEdit), form.findChild(PageEdit)

        profile.setCurrentIndex(2)
        profile.activated.emit(2)

        assert_that(profile.value()).is_equal_to("2")
        assert_that(page.value()).is_equal_to("")
        assert_that([page.itemText(i) for i in range(page.count())]).is_equal_to(["(no page)", "Docs"])
        assert_that(seen).is_not_empty()

    def test_profile_field_may_come_after_the_page_field(self, qapp):  # pylint: disable=unused-argument
        from decker.client.widgets.page_edit import PageEdit

        form = _form(list(reversed(self._SCHEMA)), {"who": "2", "where": "w1"})

        assert_that(form.findChild(PageEdit).value()).is_equal_to("w1")

    def test_changing_the_profile_never_reports_a_stale_page(self, qapp):  # pylint: disable=unused-argument
        from decker.client.widgets.profile_edit import ProfileEdit

        form = _form(self._SCHEMA, {"who": "", "where": "p2"})
        reported: list[dict[str, Any]] = []
        form.changed.connect(lambda: reported.append(form.values()))
        profile = form.findChild(ProfileEdit)

        profile.setCurrentIndex(2)
        profile.activated.emit(2)

        assert_that(reported).is_not_empty()
        assert_that(all(values["where"] == "" for values in reported)).is_true()


def _action_edit(form: Any) -> Any:
    return form._fields["action"].widget


def _params_edit(form: Any) -> Any:
    return form._fields["parameters"].widget


class TestActionFields:  # pylint: disable=protected-access
    _ACTIONS = [
        {
            "plugin_uuid": "com.t",
            "plugin_name": "Tools",
            "action_uuid": "com.t.http",
            "action_name": "HTTP",
            "settings_schema": [{"key": "url", "type": "text", "label": "URL", "default": ""}],
        },
        {
            "plugin_uuid": "com.t",
            "plugin_name": "Tools",
            "action_uuid": "com.t.mute",
            "action_name": "Mute",
            "settings_schema": [],
        },
    ]
    _SCHEMA = [
        {"key": "action", "type": "action", "label": "Action"},
        {"key": "parameters", "type": "action_params", "label": "Parameters", "action_field": "action"},
    ]

    def setup_method(self):
        import json

        from decker.client.context import AppContext

        AppContext._instance = None
        actions = self._ACTIONS

        class _Rpc:
            def call(self, method, *_args):
                return json.dumps(actions) if method == "plugins_actions_list" else None

        AppContext.get().connection_manager = _Rpc()  # type: ignore[assignment]

    def test_the_action_dropdown_lists_actions_and_round_trips(self, qapp):  # pylint: disable=unused-argument
        form = _form(self._SCHEMA, {"action": "com.t.mute", "parameters": {}})
        combo = _action_edit(form)

        assert_that([combo.itemText(i) for i in range(combo.count())]).is_equal_to(
            ["(choose an action)", "Tools: HTTP", "Tools: Mute"]
        )
        assert_that(form.values()["action"]).is_equal_to("com.t.mute")

    def test_the_parameters_form_follows_the_chosen_action(self, qapp):  # pylint: disable=unused-argument
        form = _form(self._SCHEMA, {"action": "com.t.http", "parameters": {"url": "http://x"}})
        params = _params_edit(form)
        assert_that(form.values()["parameters"]).is_equal_to({"url": "http://x"})

        combo = _action_edit(form)
        combo.setCurrentIndex(combo.findData("com.t.mute"))
        combo.activated.emit(combo.currentIndex())

        assert_that(params._form.isHidden()).is_true()
        assert_that(form.values()["parameters"]).is_equal_to({})

    def test_choosing_an_action_emits_changed_once_and_showing_one_does_not(
        self, qapp
    ):  # pylint: disable=unused-argument
        form = _form(self._SCHEMA, {"action": "", "parameters": {}})
        seen: list[int] = []
        form.changed.connect(lambda: seen.append(1))
        combo = _action_edit(form)

        combo.setCurrentIndex(combo.findData("com.t.http"))
        combo.activated.emit(combo.currentIndex())

        assert_that(seen).is_length(1)

    def test_settings_of_an_unavailable_action_are_kept(self, qapp):  # pylint: disable=unused-argument
        form = _form(self._SCHEMA, {"action": "com.gone.thing", "parameters": {"k": "v"}})

        assert_that(form.values()).is_equal_to({"action": "com.gone.thing", "parameters": {"k": "v"}})
        assert_that(_action_edit(form).currentText()).is_equal_to("(missing action)")

    def test_a_schema_with_action_fields_validates_server_side(self):
        from decker.plugin.settings_schema import apply_schema, parse_settings_schema

        schema = parse_settings_schema(self._SCHEMA, "t")

        assert_that(apply_schema(schema, {"action": "a.b", "parameters": {"x": 1}})).is_equal_to(
            {"action": "a.b", "parameters": {"x": 1}}
        )
        assert_that(apply_schema(schema, {"parameters": "junk"})).is_equal_to({"action": "", "parameters": {}})


class TestSlotsField:  # pylint: disable=unused-argument
    def test_value_round_trip_through_schema_form(self, qapp):
        slots = {"press": {"kind": "action", "action": "http", "settings": {}}}
        form = _form([{"key": "slots", "label": "Slots", "type": "slots"}], {"slots": slots})
        changes: list[int] = []
        form.changed.connect(lambda: changes.append(1))

        assert_that(form.values()["slots"]["press"]).is_equal_to(slots["press"])
        form._fields["slots"].widget.set_slot_value("press", None)  # type: ignore[attr-defined]  # pylint: disable=protected-access

        assert_that(changes).is_length(1)
        assert_that(form.values()["slots"]["press"]).is_none()


def _join(widget: Any) -> None:
    widget._thread.join(5)  # pylint: disable=protected-access


class TestDynamicChoiceInForm:  # pylint: disable=protected-access
    _SCHEMA = [
        {
            "key": "voice",
            "type": "choice",
            "label": "Voice",
            "options_source": "voices",
            "options": [{"label": "Default", "value": ""}],
        }
    ]

    def _patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from decker.client.widgets import (
            dynamic_choice_edit,  # pylint: disable=import-outside-toplevel
        )

        monkeypatch.setattr(
            dynamic_choice_edit, "_default_fetch", lambda: lambda _p, _s: (True, [{"label": "Bob", "value": "bob"}], "")
        )

    def test_schema_form_builds_the_dynamic_field_when_plugin_id_is_given(
        self, qapp, monkeypatch
    ):  # pylint: disable=unused-argument
        self._patch(monkeypatch)
        form = SchemaForm()
        form.set_schema(self._SCHEMA, {}, "plug")
        assert_that(form._fields["voice"].widget).is_instance_of(DynamicChoiceEdit)
        _join(form._fields["voice"].widget)

    def test_schema_form_value_round_trip_for_an_unlisted_value(
        self, qapp, monkeypatch
    ):  # pylint: disable=unused-argument
        self._patch(monkeypatch)
        form = SchemaForm()
        form.set_schema(self._SCHEMA, {"voice": "gone"}, "plug")
        assert_that(form.values()).is_equal_to({"voice": "gone"})
        _join(form._fields["voice"].widget)

    def test_appearance_form_passes_the_plugin_id(self, qapp, monkeypatch):  # pylint: disable=unused-argument
        self._patch(monkeypatch)
        form = AppearanceForm()
        form.set_schema(self._SCHEMA, {}, "plug")
        assert_that(form._fields["voice"].widget).is_instance_of(DynamicChoiceEdit)
        _join(form._fields["voice"].widget)
