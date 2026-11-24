from datetime import datetime

from assertpy import assert_that

from decker.server.action_calls import INPUTS, MAX_DEPTH, ActionCall


def test_payload_has_every_protocol_4_field() -> None:
    call = ActionCall("a.b", {"x": 1}, "press", card={"id": "c"}, caller="p", depth=2)

    payload = call.payload("req1", {"x": 2})

    assert_that(set(payload)).is_equal_to(
        {"action", "request", "args", "input", "called_at", "card", "caller", "chain", "depth", "device"}
    )
    assert_that(payload["args"]).is_equal_to({"x": 2})
    assert_that((payload["action"], payload["request"], payload["input"], payload["depth"])).is_equal_to(
        ("a.b", "req1", "press", 2)
    )
    assert_that(datetime.fromisoformat(payload["called_at"]).tzinfo).is_not_none()


def test_each_call_gets_its_own_chain() -> None:
    assert_that(ActionCall("a", {}, "plugin").chain).is_not_equal_to(ActionCall("a", {}, "plugin").chain)


def test_constants() -> None:
    assert_that(INPUTS).is_equal_to({"press", "double_press", "long_press", "plugin"})
    assert_that(MAX_DEPTH).is_equal_to(8)


def test_payload_carries_the_device() -> None:
    assert_that(ActionCall("a", {}, "press", device="abcd1234").payload("r", {})["device"]).is_equal_to("abcd1234")
    assert_that(ActionCall("a", {}, "press").payload("r", {})["device"]).is_none()
