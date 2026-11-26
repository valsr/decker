from assertpy import assert_that

from decker.server.card_variables import CardVariableStore

DECLARED = {"press": frozenset({"pid"}), "long_press": frozenset()}


def test_set_then_view() -> None:
    store = CardVariableStore()
    store.set("c", "press", "pid", 42)
    assert_that(store.view("c", DECLARED).get("press.pid")).is_equal_to("42")


def test_unset_declared_is_empty() -> None:
    assert_that(CardVariableStore().view("c", DECLARED).get("press.pid")).is_equal_to("")


def test_undeclared_and_unknown_binding_stay_as_typed() -> None:
    view = CardVariableStore().view("c", DECLARED)
    assert_that(view.get("press.other")).is_none()
    assert_that(view.get("nope.pid")).is_none()
    assert_that(view.get("page_name")).is_none()


def test_status_is_always_known() -> None:
    store = CardVariableStore()
    view = store.view("c", DECLARED)
    assert_that(view.get("long_press.status")).is_equal_to("")
    store.set_status("c", "long_press", True)
    store.set_status("c", "press", False, "step_1")
    assert_that(view.get("long_press.status")).is_equal_to("SUCCESS")
    assert_that(view.get("press.step_1.status")).is_equal_to("FAILURE")


def test_slot_reference() -> None:
    store = CardVariableStore()
    view = store.view("c", DECLARED)
    assert_that(view.get("press.step_1.pid")).is_equal_to("")
    store.set("c", "press", "pid", "7", "step_1")
    assert_that(view.get("press.step_1.pid")).is_equal_to("7")
    assert_that(view.get("press.pid")).is_equal_to("")


def test_reset_binding_clears_only_that_binding() -> None:
    store = CardVariableStore()
    store.set("c", "press", "pid", 1)
    store.set("c", "press", "pid", 2, "s")
    store.set_status("c", "press", True)
    store.set("c", "long_press", "x", 3)
    store.reset_binding("c", "press")
    assert_that(store.snapshot("c")).is_equal_to({"long_press": {"x": 3}})


def test_snapshot_shape_and_copy() -> None:
    store = CardVariableStore()
    store.set("c", "press", "pid", {"a": 1})
    store.set("c", "press", "pid", 2, "s")
    snap = store.snapshot("c")
    assert_that(snap).is_equal_to({"press": {"pid": {"a": 1}, "s": {"pid": 2}}})
    snap["press"]["pid"]["a"] = 9
    assert_that(store.snapshot("c")["press"]["pid"]).is_equal_to({"a": 1})


def test_drop_removes_all() -> None:
    store = CardVariableStore()
    store.set("c", "press", "pid", 1)
    store.set("d", "press", "pid", 1)
    store.drop("c")
    assert_that(store.snapshot("c")).is_empty()
    assert_that(store.snapshot("d")).is_not_empty()
