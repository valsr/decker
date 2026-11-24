from typing import Any

import pytest
from assertpy import assert_that

from decker.common.icon_ref import (
    DEFAULT_ICON_COLOR,
    ICON_REF_PREFIX,
    make_icon_ref,
    normalize_icon_value,
    parse_icon_ref,
)


def test_make_and_parse_round_trip() -> None:
    ref = make_icon_ref("com.decker.fontawesome.solid", "house.svg")
    assert_that(ref).is_equal_to("pack:com.decker.fontawesome.solid/house.svg")
    assert_that(parse_icon_ref(ref)).is_equal_to(("com.decker.fontawesome.solid", "house.svg"))


def test_prefix_constant() -> None:
    assert_that(ICON_REF_PREFIX).is_equal_to("pack:")


def test_path_may_contain_slashes() -> None:
    assert_that(parse_icon_ref("pack:p/sub/dir/a.svg")).is_equal_to(("p", "sub/dir/a.svg"))


@pytest.mark.parametrize(
    "bad",
    [
        None,
        5,
        "",
        "pack:",
        "pack:x",
        "pack:/a.svg",
        "pack:p/",
        "file:p/a",
        "pack:p/../a.svg",
        "pack:p//etc/passwd",
        "pack:p/a/../../b",
    ],
)
def test_parse_rejects_bad_refs(bad: Any) -> None:
    assert_that(parse_icon_ref(bad)).is_none()


def test_normalize_fills_defaults() -> None:
    assert_that(normalize_icon_value({})).is_equal_to({"ref": "", "color": DEFAULT_ICON_COLOR})


def test_normalize_keeps_valid_fields() -> None:
    value = {"ref": "pack:p/a.svg", "color": "#112233"}
    assert_that(normalize_icon_value(value)).is_equal_to(value)


def test_normalize_invalid_fields_fall_back_individually() -> None:
    assert_that(normalize_icon_value({"ref": "nope", "color": "red"})).is_equal_to(
        {"ref": "", "color": DEFAULT_ICON_COLOR}
    )
    assert_that(normalize_icon_value({"ref": "pack:p/a.svg", "color": "red"})).is_equal_to(
        {"ref": "pack:p/a.svg", "color": DEFAULT_ICON_COLOR}
    )


def test_normalize_accepts_rgba_color() -> None:
    assert_that(normalize_icon_value({"color": "#11223380"})["color"]).is_equal_to("#11223380")


def test_normalize_non_dict_gives_defaults() -> None:
    assert_that(normalize_icon_value("pack:p/a.svg")).is_equal_to({"ref": "", "color": DEFAULT_ICON_COLOR})
    assert_that(normalize_icon_value(None)).is_equal_to({"ref": "", "color": DEFAULT_ICON_COLOR})


def test_normalize_drops_unknown_keys() -> None:
    assert_that(normalize_icon_value({"bogus": 1})).does_not_contain_key("bogus")


def test_normalize_uses_given_defaults() -> None:
    defaults = {"ref": "pack:p/a.svg", "color": "#ff0000"}
    assert_that(normalize_icon_value({"ref": "bad", "color": "bad"}, defaults)).is_equal_to(defaults)


def test_normalize_returns_fresh_dict() -> None:
    first = normalize_icon_value({})
    first["ref"] = "pack:p/a.svg"
    assert_that(normalize_icon_value({})["ref"]).is_equal_to("")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("solid/house.svg", "house"),
        ("org.gnome.Foo", "org.gnome.Foo"),
        ("a/b/Icon.PNG", "Icon"),
        ("x.tar.svg", "x.tar"),
        ("photo.JPEG", "photo"),
        ("pic.jpg", "pic"),
        ("cat.gif", "cat"),
        ("noext", "noext"),
        (".svg", ".svg"),
        ("", ""),
        ("dir/", ""),
    ],
)
def test_icon_display_name_drops_one_image_suffix_only(path: str, expected: str) -> None:
    from decker.common.icon_ref import icon_display_name

    assert_that(icon_display_name(path)).is_equal_to(expected)


def test_user_icons_get_the_short_user_reference() -> None:
    assert_that(make_icon_ref("decker.user", "logo.svg")).is_equal_to("user:logo")
    assert_that(make_icon_ref("decker.user", "my_icon-2.png")).is_equal_to("user:my_icon-2")


def test_user_reference_parses_to_the_bare_id() -> None:
    assert_that(parse_icon_ref("user:logo")).is_equal_to(("decker.user", "logo"))
    assert_that(parse_icon_ref("pack:decker.user/logo.svg")).is_equal_to(("decker.user", "logo.svg"))


@pytest.mark.parametrize("bad", ["user:", "user:Logo", "user:../x", "user:a/b", "user:-a", "user:" + "a" * 65])
def test_user_reference_rejects_names_no_import_can_produce(bad: str) -> None:
    assert_that(parse_icon_ref(bad)).is_none()


def test_normalize_keeps_a_user_reference() -> None:
    assert_that(normalize_icon_value({"ref": "user:logo"})["ref"]).is_equal_to("user:logo")
