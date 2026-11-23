from typing import Any

import pytest
from assertpy import assert_that

from decker_plugin_sdk import CardContext, CardProxy, DeckerPlugin, OwnCardProxy

Sent = list[tuple[str, str | None, dict[str, Any]]]


def _payload() -> dict[str, Any]:
    return {
        "context": "ctx1",
        "card": "card-uuid",
        "plugin": "com.example.p",
        "device": "dev1",
        "key": {"index": 3, "column": 1, "row": 0},
        "size": [72, 80],
        "settings": {"a": 1},
        "last_paint": None,
    }


def _proxy(sent: Sent, payload: dict[str, Any] | None = None) -> CardProxy:
    return CardProxy(payload or _payload(), "req1", lambda e, c, p: sent.append((e, c, p)))


def test_proxy_identity_and_physical_properties() -> None:
    proxy = _proxy([])
    assert_that(proxy.context).is_equal_to("ctx1")
    assert_that(proxy.card_id).is_equal_to("card-uuid")
    assert_that(proxy.plugin_id).is_equal_to("com.example.p")
    assert_that(proxy.device).is_equal_to("dev1")
    assert_that((proxy.key_index, proxy.column, proxy.row)).is_equal_to((3, 1, 0))
    assert_that(proxy.size).is_equal_to((72, 80))
    assert_that(proxy.last_paint).is_none()
    proxy.queue_paint()
    assert_that(proxy.last_paint).is_none()


def test_settings_is_a_read_only_copy() -> None:
    payload = _payload()
    proxy = _proxy([], payload)
    payload["settings"]["a"] = 2
    assert_that(proxy.settings["a"]).is_equal_to(1)
    with pytest.raises(TypeError):
        proxy.settings["a"] = 5  # type: ignore[index]


def test_navigation_carries_the_request_id() -> None:
    sent: Sent = []
    _proxy(sent).switch_page("next", True)
    assert_that(sent).is_equal_to([("switch_page", "ctx1", {"to": "next", "wrap": True, "request": "req1"})])


def test_proxy_has_no_setter_or_instance() -> None:
    proxy = _proxy([])
    assert_that(hasattr(proxy, "set_settings")).is_false()
    assert_that(hasattr(proxy, "instance")).is_false()


def test_closed_proxy_refuses_calls() -> None:
    sent: Sent = []
    proxy = _proxy(sent)
    proxy.close()
    with pytest.raises(RuntimeError, match="card access ended with the action call"):
        proxy.show_ok()
    assert_that(sent).is_empty()


def test_own_proxy_calls_go_through_the_card_context() -> None:
    sent: Sent = []
    ctx = CardContext("ctx1", "card", "dev1", 3, 1, 0, _send=lambda e, c, p: sent.append((e, c, p)))
    instance = DeckerPlugin()
    proxy = OwnCardProxy(_payload(), ctx, instance)
    proxy.set_settings({"x": 1})
    proxy.show_ok()
    proxy.close()
    proxy.show_alert()
    assert_that(proxy.instance).is_same_as(instance)
    assert_that(sent).is_equal_to(
        [("set_settings", "ctx1", {"settings": {"x": 1}}), ("show_ok", "ctx1", {}), ("show_alert", "ctx1", {})]
    )
