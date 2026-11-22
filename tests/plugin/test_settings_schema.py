from typing import Any

import pytest
from assertpy import assert_that

from decker.common.text_style import TEXT_STYLE_DEFAULTS
from decker.plugin.settings_schema import (
    FIELD_TYPES,
    SettingField,
    SettingSection,
    SettingsValidationError,
    apply_schema,
    parse_settings_schema,
    schema_to_json,
    validate_settings,
)

COUNTER: list[dict[str, Any]] = [
    {"section": "Counting"},
    {"key": "step", "type": "int", "label": "Step", "default": 1, "min": 1, "max": 9999},
    {
        "key": "double_step",
        "type": "int",
        "label": "Double press step",
        "optional": True,
        "placeholder": "None (presses register immediately)",
    },
    {"key": "max", "type": "int", "label": "Max count", "default": 0, "min": 0, "special_value": "∞ (no limit)"},
]
COLOR: list[dict[str, Any]] = [{"key": "background", "type": "color", "label": "Background", "default": "#1f618d"}]
CHOICE: list[dict[str, Any]] = [
    {
        "key": "mode",
        "type": "choice",
        "label": "Mode",
        "default": "a",
        "options": [{"label": "A", "value": "a"}, {"label": "B", "value": "b"}],
    }
]


@pytest.fixture(name="counter_schema")
def fixture_counter_schema() -> tuple[Any, ...]:
    return parse_settings_schema(COUNTER, "counter")


def test_parses_sections_and_fields_in_order(counter_schema: tuple[Any, ...]) -> None:
    assert_that(counter_schema).is_length(4)
    assert_that(counter_schema[0]).is_equal_to(SettingSection("Counting"))
    step = counter_schema[1]
    assert_that(step).is_instance_of(SettingField)
    assert_that([e.key for e in counter_schema[1:]]).is_equal_to(["step", "double_step", "max"])
    assert_that(step.default).is_equal_to(1)
    assert_that(step.extra["min"]).is_equal_to(1)
    assert_that(counter_schema[2].optional).is_true()
    assert_that(counter_schema[2].default).is_none()
    assert_that(counter_schema[3].extra["special_value"]).is_equal_to("∞ (no limit)")
    assert_that(FIELD_TYPES).contains("int", "icon", "font")


def test_unknown_type_rejected() -> None:
    with pytest.raises(ValueError, match="here.*unknown field type"):
        parse_settings_schema([{"key": "a", "type": "slider"}], "here")


def test_duplicate_key_rejected() -> None:
    data = [{"key": "a", "type": "bool"}, {"key": "a", "type": "text"}]
    with pytest.raises(ValueError, match="here.*duplicate"):
        parse_settings_schema(data, "here")


def test_choice_requires_options() -> None:
    with pytest.raises(ValueError, match="options"):
        parse_settings_schema([{"key": "a", "type": "choice"}], "here")


def test_default_must_match_type() -> None:
    with pytest.raises(ValueError, match="here"):
        parse_settings_schema([{"key": "a", "type": "int", "default": "x"}], "here")


def test_default_must_be_valid_for_choice_and_color() -> None:
    with pytest.raises(ValueError, match="default"):
        parse_settings_schema([{**CHOICE[0], "default": "zzz"}], "here")
    with pytest.raises(ValueError, match="default"):
        parse_settings_schema([{**COLOR[0], "default": "blue"}], "here")


def test_non_list_and_bad_entries_rejected() -> None:
    bad_inputs: list[Any] = [{}, [1], [{"section": ""}], [{"type": "int"}], [{"key": "a", "type": "int", "min": "x"}]]
    for bad in bad_inputs:
        with pytest.raises(ValueError, match="here"):
            parse_settings_schema(bad, "here")


def test_missing_default_uses_zero_value() -> None:
    data = [{"key": k, "type": t} for k, t in (("i", "int"), ("f", "float"), ("t", "text"), ("b", "bool"))]
    schema = parse_settings_schema(data + CHOICE, "x")
    assert_that(apply_schema(schema, {})).is_equal_to({"i": 0, "f": 0.0, "t": "", "b": False, "mode": "a"})


def test_apply_fills_defaults(counter_schema: tuple[Any, ...]) -> None:
    assert_that(apply_schema(counter_schema, {})).is_equal_to({"step": 1, "double_step": None, "max": 0})


def test_apply_drops_unknown_keys(counter_schema: tuple[Any, ...]) -> None:
    assert_that(apply_schema(counter_schema, {"bogus": 1, "step": 2})).does_not_contain_key("bogus")


def test_apply_replaces_invalid_values_with_default(counter_schema: tuple[Any, ...]) -> None:
    assert_that(apply_schema(counter_schema, {"step": "abc"})["step"]).is_equal_to(1)
    assert_that(apply_schema(counter_schema, {"step": 0})["step"]).is_equal_to(1)
    assert_that(apply_schema(counter_schema, {"step": True})["step"]).is_equal_to(1)
    color = parse_settings_schema(COLOR, "c")
    assert_that(apply_schema(color, {"background": "blue"})["background"]).is_equal_to("#1f618d")
    assert_that(apply_schema(color, {"background": "#00ff00"})["background"]).is_equal_to("#00ff00")


def test_apply_optional_empty_becomes_none(counter_schema: tuple[Any, ...]) -> None:
    assert_that(apply_schema(counter_schema, {"double_step": ""})["double_step"]).is_none()
    assert_that(apply_schema(counter_schema, {"double_step": None})["double_step"]).is_none()
    assert_that(apply_schema(counter_schema, {"double_step": 3})["double_step"]).is_equal_to(3)


def test_apply_coerces_int_strings(counter_schema: tuple[Any, ...]) -> None:
    assert_that(apply_schema(counter_schema, {"step": "5"})["step"]).is_equal_to(5)


def test_apply_choice_outside_options_uses_default() -> None:
    schema = parse_settings_schema(CHOICE, "c")
    assert_that(apply_schema(schema, {"mode": "zzz"})["mode"]).is_equal_to("a")
    assert_that(apply_schema(schema, {"mode": "b"})["mode"]).is_equal_to("b")


def test_apply_float_bool_text() -> None:
    data = [
        {"key": "f", "type": "float", "default": 1.5, "max": 2},
        {"key": "b", "type": "bool", "default": True},
        {"key": "t", "type": "text", "default": "x"},
    ]
    schema = parse_settings_schema(data, "x")
    assert_that(apply_schema(schema, {"f": "1.25", "b": "no", "t": 5})).is_equal_to({"f": 1.25, "b": True, "t": "x"})
    assert_that(apply_schema(schema, {"f": 3, "t": "hi"})).is_equal_to({"f": 1.5, "b": True, "t": "hi"})
    assert_that(apply_schema(schema, {"f": "nan"})["f"]).is_equal_to(1.5)


def test_schema_to_json_round_trips(counter_schema: tuple[Any, ...]) -> None:
    for data in (COUNTER, COLOR, CHOICE):
        schema = parse_settings_schema(data, "x")
        dumped = schema_to_json(schema)
        assert_that(parse_settings_schema(dumped, "x")).is_equal_to(schema)
    assert_that(schema_to_json(counter_schema)[1]).is_equal_to(COUNTER[1])
    assert_that(schema_to_json(counter_schema)[0]).is_equal_to({"section": "Counting"})


def test_huge_int_for_float_falls_back_to_default() -> None:
    schema = parse_settings_schema([{"key": "f", "type": "float", "default": 1.5}], "x")
    assert_that(apply_schema(schema, {"f": 10**400})["f"]).is_equal_to(1.5)


def test_huge_int_float_default_rejected() -> None:
    with pytest.raises(ValueError, match="here"):
        parse_settings_schema([{"key": "f", "type": "float", "default": 10**400}], "here")


def _text_style_schema(default: Any = None) -> tuple[Any, ...]:
    entry: dict[str, Any] = {"key": "label", "type": "text_style", "label": "Label"}
    if default is not None:
        entry["default"] = default
    return parse_settings_schema([entry], "t")


def test_text_style_type_accepted() -> None:
    assert_that(FIELD_TYPES).contains("text_style")
    assert_that(_text_style_schema()[0].default).is_equal_to(dict(TEXT_STYLE_DEFAULTS))


def test_text_style_default_completed_from_partial() -> None:
    default = _text_style_schema({"position": "top"})[0].default
    assert_that(default).is_equal_to({**TEXT_STYLE_DEFAULTS, "position": "top"})


def test_text_style_non_dict_default_rejected() -> None:
    with pytest.raises(ValueError, match="default"):
        _text_style_schema("bold")


def test_apply_schema_text_style_invalid_fields_fall_back_per_field() -> None:
    result = apply_schema(_text_style_schema(), {"label": {"size": "abc", "position": "left", "text": "Hi"}})
    assert_that(result["label"]).is_equal_to({**TEXT_STYLE_DEFAULTS, "text": "Hi"})


def test_apply_schema_text_style_non_dict_uses_default() -> None:
    assert_that(apply_schema(_text_style_schema(), {"label": 5})["label"]).is_equal_to(dict(TEXT_STYLE_DEFAULTS))


def test_apply_schema_text_style_missing_uses_default() -> None:
    assert_that(apply_schema(_text_style_schema({"bold": True}), {})["label"]["bold"]).is_true()


def test_apply_schema_text_style_invalid_field_falls_back_to_field_default() -> None:
    result = apply_schema(_text_style_schema({"position": "top"}), {"label": {"position": "left"}})
    assert_that(result["label"]["position"]).is_equal_to("top")


def test_apply_schema_text_style_returns_copy() -> None:
    schema = _text_style_schema()
    apply_schema(schema, {})["label"]["text"] = "changed"
    assert_that(apply_schema(schema, {})["label"]["text"]).is_equal_to("")
    assert_that(schema[0].default["text"]).is_equal_to("")


def test_text_style_round_trips_through_schema_to_json() -> None:
    schema = _text_style_schema({"position": "top"})
    assert_that(parse_settings_schema(schema_to_json(schema), "t")).is_equal_to(schema)


def _color_schema(**extra: Any) -> tuple[Any, ...]:
    return parse_settings_schema([{"key": "c", "type": "color", "label": "C", **extra}], "t")


def test_color_alpha_accepts_rrggbbaa() -> None:
    schema = _color_schema(alpha=True, default="#11223380")
    assert_that(schema[0].default).is_equal_to("#11223380")
    assert_that(apply_schema(schema, {"c": "#aabbccdd"})["c"]).is_equal_to("#aabbccdd")
    assert_that(apply_schema(schema, {"c": "#aabbcc"})["c"]).is_equal_to("#aabbcc")


def test_color_without_alpha_rejects_rrggbbaa() -> None:
    with pytest.raises(ValueError, match="default"):
        _color_schema(default="#11223380")
    schema = _color_schema(default="#112233")
    assert_that(apply_schema(schema, {"c": "#aabbccdd"})["c"]).is_equal_to("#112233")


def test_color_alpha_must_be_boolean() -> None:
    with pytest.raises(ValueError, match="alpha"):
        _color_schema(alpha="yes")


def test_color_alpha_round_trips_through_schema_to_json() -> None:
    schema = _color_schema(alpha=True)
    assert_that(schema_to_json(schema)[0]).contains_entry({"alpha": True})
    assert_that(parse_settings_schema(schema_to_json(schema), "t")).is_equal_to(schema)


def test_color_alpha_garbage_falls_back_to_default() -> None:
    schema = _color_schema(alpha=True, default="#112233")
    assert_that(apply_schema(schema, {"c": "#1122334"})["c"]).is_equal_to("#112233")


def _icon_schema(default: Any = None) -> tuple[Any, ...]:
    entry: dict[str, Any] = {"key": "glyph", "type": "icon", "label": "Glyph"}
    if default is not None:
        entry["default"] = default
    return parse_settings_schema([entry], "t")


def test_icon_default_is_empty_ref_and_white() -> None:
    assert_that(_icon_schema()[0].default).is_equal_to({"ref": "", "color": "#ffffff"})


def test_icon_default_completed_from_partial() -> None:
    assert_that(_icon_schema({"color": "#ff0000"})[0].default).is_equal_to({"ref": "", "color": "#ff0000"})


def test_icon_non_dict_default_rejected() -> None:
    with pytest.raises(ValueError, match="default"):
        _icon_schema("pack:p/a.svg")


def test_apply_schema_icon_invalid_fields_fall_back_per_field() -> None:
    result = apply_schema(_icon_schema(), {"glyph": {"ref": "pack:p/../x", "color": "#112233"}})
    assert_that(result["glyph"]).is_equal_to({"ref": "", "color": "#112233"})


def test_apply_schema_icon_non_dict_uses_default() -> None:
    assert_that(apply_schema(_icon_schema(), {"glyph": "pack:p/a.svg"})["glyph"]).is_equal_to(
        {"ref": "", "color": "#ffffff"}
    )


def test_apply_schema_icon_missing_uses_default() -> None:
    schema = _icon_schema({"color": "#00ff00"})
    assert_that(apply_schema(schema, {})["glyph"]).is_equal_to({"ref": "", "color": "#00ff00"})


def test_apply_schema_icon_invalid_field_falls_back_to_field_default() -> None:
    schema = _icon_schema({"color": "#00ff00"})
    assert_that(apply_schema(schema, {"glyph": {"color": "red"}})["glyph"]["color"]).is_equal_to("#00ff00")


def test_apply_schema_icon_returns_copy() -> None:
    schema = _icon_schema()
    apply_schema(schema, {})["glyph"]["ref"] = "pack:p/a.svg"
    assert_that(apply_schema(schema, {})["glyph"]["ref"]).is_equal_to("")
    assert_that(schema[0].default["ref"]).is_equal_to("")


def test_icon_round_trips_through_schema_to_json() -> None:
    schema = _icon_schema({"color": "#ff0000"})
    assert_that(parse_settings_schema(schema_to_json(schema), "t")).is_equal_to(schema)


def test_icon_accepts_rgba_colour() -> None:
    assert_that(apply_schema(_icon_schema(), {"glyph": {"color": "#11223380"}})["glyph"]["color"]).is_equal_to(
        "#11223380"
    )


@pytest.mark.parametrize("type_", ["text", "text_style"])
def test_template_flag_accepted_on_text_and_text_style(type_: str) -> None:
    (entry,) = parse_settings_schema([{"key": "a", "type": type_, "template": True}], "here")
    assert isinstance(entry, SettingField)
    assert_that(entry.extra["template"]).is_true()


def test_template_flag_must_be_boolean() -> None:
    with pytest.raises(ValueError, match="template"):
        parse_settings_schema([{"key": "a", "type": "text", "template": "yes"}], "here")


def test_template_flag_rejected_on_other_types() -> None:
    with pytest.raises(ValueError, match="template"):
        parse_settings_schema([{"key": "a", "type": "int", "template": True}], "here")


def test_template_round_trips_through_schema_to_json() -> None:
    schema = parse_settings_schema([{"key": "a", "type": "text", "template": True}], "here")
    assert_that(schema_to_json(schema)[0]["template"]).is_true()
    reparsed = parse_settings_schema(schema_to_json(schema), "x")[0]
    assert isinstance(reparsed, SettingField)
    assert_that(reparsed.extra["template"]).is_true()


def test_page_type_defaults_to_empty_string() -> None:
    (entry,) = parse_settings_schema([{"key": "p", "type": "page"}], "here")
    assert isinstance(entry, SettingField)
    assert_that(entry.default).is_equal_to("")
    assert_that(apply_schema((entry,), {})).is_equal_to({"p": ""})


@pytest.mark.parametrize("value", [5, None, ["x"]])
def test_page_value_coerces_non_strings_to_default(value: Any) -> None:
    schema = parse_settings_schema([{"key": "p", "type": "page"}], "here")
    assert_that(apply_schema(schema, {"p": value})).is_equal_to({"p": ""})


def test_page_value_keeps_strings() -> None:
    schema = parse_settings_schema([{"key": "p", "type": "page"}], "here")
    assert_that(apply_schema(schema, {"p": "abc"})).is_equal_to({"p": "abc"})


def test_page_default_must_be_a_string() -> None:
    with pytest.raises(ValueError, match="default"):
        parse_settings_schema([{"key": "p", "type": "page", "default": 5}], "here")


def test_profile_type_defaults_to_empty_string() -> None:
    (entry,) = parse_settings_schema([{"key": "p", "type": "profile"}], "here")
    assert isinstance(entry, SettingField)

    assert_that(entry.default).is_equal_to("")


@pytest.mark.parametrize("value", [3, None, True, "x", "-1", " 1"])
def test_profile_value_coerces_non_ids_to_default(value: Any) -> None:
    schema = parse_settings_schema([{"key": "p", "type": "profile"}], "here")

    assert_that(apply_schema(schema, {"p": value})).is_equal_to({"p": ""})


def test_profile_value_keeps_blank_and_decimal_ids() -> None:
    schema = parse_settings_schema([{"key": "p", "type": "profile"}], "here")

    assert_that(apply_schema(schema, {"p": "12"})).is_equal_to({"p": "12"})
    assert_that(apply_schema(schema, {"p": ""})).is_equal_to({"p": ""})


def test_page_profile_field_must_name_a_profile_field() -> None:
    ok = [{"key": "who", "type": "profile"}, {"key": "p", "type": "page", "profile_field": "who"}]
    assert_that(parse_settings_schema(ok, "here")).is_length(2)

    for bad in ("missing", "p"):
        data = [{"key": "who", "type": "text"}, {"key": "p", "type": "page", "profile_field": bad}]
        with pytest.raises(ValueError, match="profile_field"):
            parse_settings_schema(data, "here")

    with pytest.raises(ValueError, match="profile_field"):
        parse_settings_schema([{"key": "p", "type": "page", "profile_field": 3}], "here")


def test_profile_field_only_valid_on_page() -> None:
    with pytest.raises(ValueError, match="profile_field"):
        parse_settings_schema([{"key": "t", "type": "text", "profile_field": "x"}], "here")


def test_profile_field_survives_the_round_trip_to_clients() -> None:
    data = [{"key": "who", "type": "profile"}, {"key": "p", "type": "page", "profile_field": "who"}]

    sent = schema_to_json(parse_settings_schema(data, "here"))

    assert_that(sent[1]["profile_field"]).is_equal_to("who")
    assert_that(sent[0]["type"]).is_equal_to("profile")


def test_profile_value_with_a_trailing_newline_is_not_an_id() -> None:
    schema = parse_settings_schema([{"key": "p", "type": "profile"}], "here")

    assert_that(apply_schema(schema, {"p": "1\n"})).is_equal_to({"p": ""})


def test_slots_field_parses_and_apply_returns_four_keys() -> None:
    schema = parse_settings_schema([{"key": "slots", "type": "slots"}], "t")
    assert_that(apply_schema(schema, {})["slots"]).is_equal_to(
        {"display": None, "press": None, "double_press": None, "long_press": None}
    )
    result = apply_schema(schema, {"slots": {"press": {"kind": "action", "action": "a.b", "settings": {}}}})
    assert_that(result["slots"]["press"]).is_equal_to({"kind": "action", "action": "a.b", "settings": {}})
    assert_that(result["slots"]["display"]).is_none()


SOURCED: dict[str, Any] = {"key": "dev", "type": "choice", "label": "Device", "options_source": "output_devices"}


def test_choice_with_options_source_and_no_options_parses() -> None:
    (entry,) = parse_settings_schema([SOURCED], "here")
    assert isinstance(entry, SettingField)
    assert_that(entry.extra["options_source"]).is_equal_to("output_devices")
    assert_that(entry.extra["options"]).is_equal_to(())
    assert_that(entry.default).is_equal_to("")


def test_choice_without_options_or_source_still_rejected() -> None:
    with pytest.raises(ValueError, match="options"):
        parse_settings_schema([{"key": "a", "type": "choice"}], "here")
    with pytest.raises(ValueError, match="options"):
        parse_settings_schema([{"key": "a", "type": "choice", "options": []}], "here")


def test_invalid_options_source_rejected() -> None:
    for bad in ("Bad Name", 5, ""):
        with pytest.raises(ValueError, match="here.*options_source"):
            parse_settings_schema([{**SOURCED, "options_source": bad}], "here")
    with pytest.raises(ValueError, match="here.*options_source"):
        parse_settings_schema([{"key": "a", "type": "text", "options_source": "x"}], "here")


def test_options_source_choice_accepts_any_string_and_keeps_it() -> None:
    schema = parse_settings_schema([{**SOURCED, "default": "dflt"}], "here")
    assert_that(apply_schema(schema, {"dev": "usb_headset"})).is_equal_to({"dev": "usb_headset"})
    assert_that(apply_schema(schema, {"dev": ""})).is_equal_to({"dev": ""})
    assert_that(apply_schema(schema, {"dev": "x" * 256})).is_equal_to({"dev": "x" * 256})
    assert_that(apply_schema(schema, {"dev": "x" * 257})).is_equal_to({"dev": "dflt"})
    assert_that(apply_schema(schema, {"dev": 3})).is_equal_to({"dev": "dflt"})
    assert_that(apply_schema(schema, {"dev": True})).is_equal_to({"dev": "dflt"})


def test_options_source_choice_default_rules() -> None:
    explicit = parse_settings_schema([{**SOURCED, "default": "d"}], "here")[0]
    static = [{"label": "Auto", "value": "auto"}, {"label": "Two", "value": "two"}]
    first = parse_settings_schema([{**SOURCED, "options": static}], "here")[0]
    empty = parse_settings_schema([{**SOURCED, "options": []}], "here")[0]
    assert isinstance(explicit, SettingField) and isinstance(first, SettingField) and isinstance(empty, SettingField)
    assert_that(explicit.default).is_equal_to("d")
    assert_that(first.default).is_equal_to("auto")
    assert_that(empty.default).is_equal_to("")
    with pytest.raises(ValueError, match="here.*default"):
        parse_settings_schema([{**SOURCED, "default": 3}], "here")
    optional = parse_settings_schema([{**SOURCED, "optional": True}], "here")[0]
    assert isinstance(optional, SettingField)
    assert_that(optional.default).is_none()


def test_options_source_is_exported_in_json() -> None:
    static = [{"label": "Auto", "value": "auto"}]
    out = schema_to_json(parse_settings_schema([SOURCED, {**SOURCED, "key": "b", "options": static}], "here"))
    assert_that(out[0]["options_source"]).is_equal_to("output_devices")
    assert_that(out[0]["options"]).is_equal_to([])
    assert_that(out[1]["options"]).is_equal_to(static)
    assert_that(out[1]["default"]).is_equal_to("auto")


def test_sourced_choice_refuses_non_string_static_values() -> None:
    static = [{"label": "One", "value": 1}]
    with pytest.raises(ValueError, match="here.*options_source.*string values"):
        parse_settings_schema([{**SOURCED, "options": static}], "here")


def test_sourced_choice_refuses_overlong_static_values() -> None:
    static = [{"label": "Long", "value": "x" * 257}]
    with pytest.raises(ValueError, match="here.*options_source.*256"):
        parse_settings_schema([{**SOURCED, "options": static}], "here")
    ok = [{"label": "Long", "value": "x" * 256}]
    assert_that(parse_settings_schema([{**SOURCED, "options": ok}], "here")).is_length(1)


def test_static_choice_without_a_source_still_accepts_int_values() -> None:
    static = [{"label": "One", "value": 1}, {"label": "Two", "value": 2}]
    schema = parse_settings_schema([{"key": "n", "type": "choice", "label": "N", "options": static}], "here")
    assert_that(apply_schema(schema, {"n": 2})).is_equal_to({"n": 2})


def test_sourced_choice_refuses_an_overlong_default() -> None:
    with pytest.raises(ValueError, match="here.*default"):
        parse_settings_schema([{**SOURCED, "default": "x" * 257}], "here")


def test_optional_sourced_choice_without_default_stays_none() -> None:
    entry = parse_settings_schema([{**SOURCED, "optional": True}], "here")[0]
    assert isinstance(entry, SettingField)
    assert_that(entry.default).is_none()


def _text_schema(**rules: Any) -> tuple[Any, ...]:
    return parse_settings_schema([{"key": "u", "type": "text", "label": "U", **rules}], "here")


def test_required_flags_empty_null_and_missing_without_default() -> None:
    schema = _text_schema(required=True)
    for values in ({}, {"u": ""}, {"u": None}):
        assert_that(validate_settings(schema, values)).contains_key("u")
    assert_that(validate_settings(schema, {"u": "x"})).is_empty()


def test_required_with_nonempty_default_accepts_missing_key() -> None:
    schema = _text_schema(required=True, default="seed")
    assert_that(validate_settings(schema, {})).is_empty()


def test_pattern_uses_fullmatch() -> None:
    schema = _text_schema(pattern="[a-z]+")
    assert_that(validate_settings(schema, {"u": "abc"})).is_empty()
    assert_that(validate_settings(schema, {"u": "abc1"})).contains_key("u")


def test_invalid_pattern_is_a_parse_error() -> None:
    with pytest.raises(ValueError, match="pattern"):
        _text_schema(pattern="[")


def test_min_and_max_length() -> None:
    schema = _text_schema(min_length=3, max_length=4)
    assert_that(validate_settings(schema, {"u": "ab"})["u"]).contains("3")
    assert_that(validate_settings(schema, {"u": "abcde"})).contains_key("u")
    assert_that(validate_settings(schema, {"u": "abc"})).is_empty()


def test_length_rules_rejected_on_non_text_fields() -> None:
    for rule in ({"pattern": "a"}, {"min_length": 1}, {"max_length": 1}):
        with pytest.raises(ValueError, match=next(iter(rule))):
            parse_settings_schema([{"key": "n", "type": "int", "label": "N", **rule}], "here")
    with pytest.raises(ValueError, match="min_length"):
        _text_schema(min_length=5, max_length=2)
    with pytest.raises(ValueError, match="max_length"):
        _text_schema(max_length=-1)


def test_strict_apply_schema_raises_with_all_field_errors() -> None:
    schema = parse_settings_schema(
        [
            {"key": "a", "type": "text", "label": "A", "required": True},
            {"key": "b", "type": "int", "label": "B", "min": 1},
        ],
        "here",
    )
    with pytest.raises(SettingsValidationError) as caught:
        apply_schema(schema, {"b": 0}, strict=True)
    assert_that(caught.value.errors).contains_key("a", "b")


def test_non_strict_apply_schema_still_falls_back() -> None:
    schema = parse_settings_schema([{"key": "b", "type": "int", "label": "B", "default": 5, "min": 1}], "here")
    assert_that(apply_schema(schema, {"b": 0})).is_equal_to({"b": 5})


def test_invalid_typed_value_is_a_strict_error() -> None:
    schema = parse_settings_schema([{"key": "b", "type": "int", "label": "B"}], "here")
    assert_that(validate_settings(schema, {"b": "x"})).contains_key("b")


def test_rules_round_trip_through_schema_to_json() -> None:
    schema = _text_schema(required=True, pattern="[a-z]+", min_length=1, max_length=9, default="ab")
    assert_that(parse_settings_schema(schema_to_json(schema), "w")).is_equal_to(schema)
