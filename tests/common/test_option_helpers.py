from typing import Any

from assertpy import assert_that

from decker.common.options import (
    MAX_OPTIONS,
    MAX_SOURCE_LENGTH,
    MAX_TEXT,
    clean_options,
    is_valid_source,
)


def test_valid_sources() -> None:
    assert_that(is_valid_source("output_devices")).is_true()
    assert_that(is_valid_source("a1")).is_true()
    assert_that(is_valid_source("a" * MAX_SOURCE_LENGTH)).is_true()
    for bad in ("Out", "1a", "", "a" * (MAX_SOURCE_LENGTH + 1)):
        assert_that(is_valid_source(bad)).is_false()


def test_clean_options_keeps_valid_entries_in_order() -> None:
    raw = [{"label": "B", "value": "b"}, {"label": "A", "value": "a"}, {"label": "", "value": ""}]
    assert_that(clean_options(raw)).is_equal_to(raw)


def test_clean_options_drops_malformed_entries() -> None:
    raw: list[Any] = [
        {"value": "x"},
        {"label": "L", "value": 3},
        {"label": 3, "value": "v"},
        "bare",
        None,
        {"label": "ok", "value": "ok"},
    ]
    assert_that(clean_options(raw)).is_equal_to([{"label": "ok", "value": "ok"}])


def test_clean_options_cuts_long_text_to_256() -> None:
    result = clean_options([{"label": "é" * 300, "value": "v" * 257}])
    assert_that(result).is_equal_to([{"label": "é" * MAX_TEXT, "value": "v" * MAX_TEXT}])


def test_clean_options_drops_repeated_values_keeping_the_first() -> None:
    raw = [{"label": "one", "value": "x"}, {"label": "two", "value": "x"}, {"label": "three", "value": "y"}]
    assert_that(clean_options(raw)).is_equal_to([{"label": "one", "value": "x"}, {"label": "three", "value": "y"}])


def test_clean_options_caps_at_256() -> None:
    raw = [{"label": str(i), "value": str(i)} for i in range(MAX_OPTIONS + 10)]
    result = clean_options(raw)
    assert_that(result).is_length(MAX_OPTIONS)
    assert_that(result[-1]).is_equal_to({"label": "255", "value": "255"})


def test_clean_options_of_a_non_list_is_empty() -> None:
    for bad in (None, "abc", {"label": "a", "value": "a"}, 5):
        assert_that(clean_options(bad)).is_equal_to([])
