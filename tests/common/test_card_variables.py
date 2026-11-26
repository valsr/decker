import pytest
from assertpy import assert_that

from decker.common.card_variables import is_valid_slot


@pytest.mark.parametrize("slot", ["step_1", "a-b", "a" * 32])
def test_valid_slots(slot: str) -> None:
    assert_that(is_valid_slot(slot)).is_true()


@pytest.mark.parametrize("slot", ["status", "Bad", "1x", "", "a" * 33])
def test_invalid_slots(slot: str) -> None:
    assert_that(is_valid_slot(slot)).is_false()
