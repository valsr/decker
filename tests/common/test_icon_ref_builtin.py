import pytest
from assertpy import assert_that

from decker.common.icon_ref import make_icon_ref, normalize_icon_value, parse_icon_ref

_PACKS = {
    "fab": "decker.builtin.fontawesome.brands",
    "far": "decker.builtin.fontawesome.regular",
    "fas": "decker.builtin.fontawesome.solid",
}


@pytest.mark.parametrize("prefix", sorted(_PACKS))
def test_short_forms_round_trip(prefix: str) -> None:
    ref = f"{prefix}:arrow-left2"

    assert_that(parse_icon_ref(ref)).is_equal_to((_PACKS[prefix], "arrow-left2.svg"))
    assert_that(make_icon_ref(_PACKS[prefix], "arrow-left2.svg")).is_equal_to(ref)


def test_pack_form_for_builtin_packs_still_parses() -> None:
    assert_that(parse_icon_ref("pack:decker.builtin.fontawesome.solid/play.svg")).is_equal_to(
        (_PACKS["fas"], "play.svg")
    )


@pytest.mark.parametrize("path", ["Play.svg", "play.png", "sub/play.svg", ".svg", "a--b.svg"])
def test_make_icon_ref_falls_back_to_the_pack_form(path: str) -> None:
    assert_that(make_icon_ref(_PACKS["fas"], path)).is_equal_to(f"pack:{_PACKS['fas']}/{path}")


@pytest.mark.parametrize("ref", ["fas:", "fas:../x", "fas:Play", "fas:play.svg", "fas:a/b", "fax:play", "fas:-a"])
def test_invalid_short_forms_are_not_references(ref: str) -> None:
    assert_that(parse_icon_ref(ref)).is_none()


def test_normalize_keeps_a_short_ref() -> None:
    assert_that(normalize_icon_value({"ref": "fas:play"})).is_equal_to({"ref": "fas:play", "color": "#ffffff"})
