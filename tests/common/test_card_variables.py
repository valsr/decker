import pytest
from assertpy import assert_that

from decker.common.card_variables import is_valid_slot, render_value


@pytest.mark.parametrize("slot", ["step_1", "a-b", "a" * 32])
def test_valid_slots(slot: str) -> None:
    assert_that(is_valid_slot(slot)).is_true()


@pytest.mark.parametrize("slot", ["status", "Bad", "1x", "", "a" * 33])
def test_invalid_slots(slot: str) -> None:
    assert_that(is_valid_slot(slot)).is_false()


def test_render_value() -> None:
    assert_that(render_value({"a": 1})).is_equal_to('{"a": 1}')
    assert_that(render_value(3)).is_equal_to("3")
    assert_that(render_value(True)).is_equal_to("true")
    assert_that(render_value(None)).is_equal_to("")
    assert_that(render_value("x")).is_equal_to("x")
