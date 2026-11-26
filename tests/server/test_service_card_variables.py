"""The ``key_variables`` RPC."""

import json
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from assertpy import assert_that

from decker.plugin.target import CardTarget
from decker.server.card_variables import CardVariableStore
from decker.server.key_assignments import KeyAssignmentStore
from decker.server.pages import initial_page_id
from decker.server.service import DeckerService
from tests.server.test_service import _PAGE_STORES, _make_context

P1 = initial_page_id("dev1")


@pytest.fixture(name="store")
def store_fixture() -> Iterator[KeyAssignmentStore]:
    with tempfile.TemporaryDirectory() as tmpdir:
        s = KeyAssignmentStore(db_path=Path(tmpdir) / "test.db")
        s.create(MagicMock())
        yield s
        while _PAGE_STORES:
            _PAGE_STORES.pop().destroy()
        s.destroy()


@pytest.fixture(name="variables")
def variables_fixture(store: KeyAssignmentStore) -> Iterator[CardVariableStore]:
    manager = MagicMock()
    manager.card_variables = CardVariableStore()
    DeckerService.configure_context(_make_context(manager, store))
    yield manager.card_variables
    DeckerService._context = None  # pylint: disable=protected-access


def _assign(store: KeyAssignmentStore, key_index: int = 0) -> CardTarget:
    target = CardTarget("com.p", "com.p.a", "ctx1", "dev1", key_index)
    store.assign("dev1", P1, key_index, target, {})
    return target


def test_returns_the_stored_values_of_the_key(store: KeyAssignmentStore, variables: CardVariableStore) -> None:
    target = _assign(store)
    variables.set(target.context_id, "press", "pid", 7)
    variables.set_status(target.context_id, "press", True)

    result = json.loads(DeckerService().key_variables("dev1", P1, 0))

    assert_that(result).is_equal_to({"press": {"pid": 7, "status": "SUCCESS"}})


def test_an_empty_key_has_no_variables(store: KeyAssignmentStore, variables: CardVariableStore) -> None:
    _assign(store)

    assert_that(json.loads(DeckerService().key_variables("dev1", P1, 3))).is_equal_to({})


def test_an_unknown_page_has_no_variables(store: KeyAssignmentStore, variables: CardVariableStore) -> None:
    _assign(store)

    assert_that(json.loads(DeckerService().key_variables("dev1", "nope", 0))).is_equal_to({})


def test_without_a_context_there_are_no_variables() -> None:
    DeckerService._context = None  # pylint: disable=protected-access

    assert_that(json.loads(DeckerService().key_variables("dev1", P1, 0))).is_equal_to({})
