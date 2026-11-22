import pytest
from assertpy import assert_that

from decker.plugin.bindings import (
    GESTURES,
    NOTHING_ACTION_ID,
    Binding,
    effective_bindings,
    normalize_stored_bindings,
    parse_binding,
    parse_manifest_bindings,
    stored_to_json,
)


def test_manifest_bindings_fill_omitted_gestures_with_nothing() -> None:
    bindings = parse_manifest_bindings({"press": {"action": "com.a.b", "args": {"x": 1}}}, "card")

    assert_that(list(bindings)).is_equal_to(list(GESTURES))
    assert_that(bindings["press"].action).is_equal_to("com.a.b")
    assert_that(bindings["long_press"]).is_equal_to(Binding(NOTHING_ACTION_ID, {}))
    assert_that(bindings["long_press"].is_nothing).is_true()
    assert_that(bindings["press"].to_json()).is_equal_to({"action": "com.a.b", "args": {"x": 1}})


@pytest.mark.parametrize("raw", [{}, {"action": "nope"}, {"action": 3}, "x", {"action": "com.a.b", "args": []}])
def test_parse_binding_rejects_missing_action_and_non_object_args(raw: object) -> None:
    with pytest.raises(ValueError, match="where-here"):
        parse_binding(raw, "where-here")


def test_unknown_gesture_in_manifest_bindings_is_an_error() -> None:
    with pytest.raises(ValueError, match="swipe"):
        parse_manifest_bindings({"swipe": {"action": "com.a.b"}}, "card")
    with pytest.raises(ValueError, match="bindings must be an object"):
        parse_manifest_bindings([], "card")


def test_normalize_stored_bindings_drops_null_and_garbage() -> None:
    stored = normalize_stored_bindings(
        {
            "press": {"action": "com.a.b"},
            "double_press": None,
            "long_press": {"action": 1},
            "swipe": {"action": "com.a.c"},
        }
    )

    assert_that(stored).is_equal_to({"press": Binding("com.a.b", {})})
    assert_that(normalize_stored_bindings(None)).is_equal_to({})
    assert_that(normalize_stored_bindings("junk")).is_equal_to({})


def test_effective_bindings_prefer_stored() -> None:
    defaults = parse_manifest_bindings({"press": {"action": "com.a.b"}}, "card")
    stored = {"press": Binding(NOTHING_ACTION_ID, {}), "long_press": Binding("com.a.c", {"k": "v"})}

    effective = effective_bindings(defaults, stored)

    assert_that(effective["press"].is_nothing).is_true()
    assert_that(effective["long_press"].action).is_equal_to("com.a.c")
    assert_that(effective["double_press"].is_nothing).is_true()
    assert_that(stored_to_json(stored)["long_press"]).is_equal_to({"action": "com.a.c", "args": {"k": "v"}})
