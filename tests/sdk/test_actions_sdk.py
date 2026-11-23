import json
import os
import threading
from datetime import datetime, timezone
from typing import Any

from assertpy import assert_that

from decker_plugin_sdk import ActionCallContext, CardContext, DeckerPlugin, Input, run
from decker_plugin_sdk._proxy import CardProxy, OwnCardProxy

CALLED_AT = "2026-11-21T10:00:00.123+00:00"


class Counter(DeckerPlugin):
    def __init__(self) -> None:
        self.count: int = 0

    def on_will_appear(self, ctx: CardContext) -> None:
        ctx.set_title("up")  # lets a test wait until the card exists


SEEN: list[ActionCallContext] = []
STORED: list[CardProxy] = []


def _record(ctx: ActionCallContext) -> Any:
    SEEN.append(ctx)
    return None


def _bump(ctx: ActionCallContext) -> None:
    assert ctx.card is not None
    ctx.card.instance.count += 1  # type: ignore[attr-defined]


def _store(ctx: ActionCallContext) -> None:
    assert ctx.card is not None
    STORED.append(ctx.card)


def _boom(_ctx: ActionCallContext) -> None:
    raise ValueError("nope")


def _card_payload(plugin: str = "test.acts") -> dict[str, Any]:
    return {
        "context": "c1",
        "card": "card-uuid",
        "plugin": plugin,
        "device": "d",
        "key": {"index": 0, "column": 0, "row": 0},
        "size": [72, 72],
        "settings": {},
        "last_paint": None,
    }


def _call(action_id: str, request: str, **extra: Any) -> dict[str, Any]:
    return {
        "action": action_id,
        "request": request,
        "args": {"n": 1},
        "input": "plugin",
        "called_at": CALLED_AT,
        "card": None,
        "caller": None,
        "chain": "ch",
        "depth": 0,
        **extra,
    }


class _Host:
    def __init__(self, plugin_id: str = "test.acts") -> None:
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        self.to_plugin = os.fdopen(in_w, "wb")
        self._in = os.fdopen(in_r, "rb")
        self._out = os.fdopen(out_w, "wb")
        self.from_plugin = os.fdopen(out_r, "rb")
        actions = {"a.rec": _record, "a.bump": _bump, "a.store": _store, "a.boom": _boom}
        self.thread = threading.Thread(target=lambda: run({"test.acts.c": Counter}, self._in, self._out, actions))
        self.thread.start()
        self.send("hello", {"protocol": 4, "decker_version": "x", "plugin_id": plugin_id, "global_settings": {}})
        self.recv_until("ready")

    def send(self, event: str, payload: dict[str, Any], context: str | None = None) -> None:
        data: dict[str, Any] = {"event": event, "payload": payload}
        if context:
            data["context"] = context
        self.to_plugin.write(json.dumps(data).encode() + b"\n")
        self.to_plugin.flush()

    def recv_until(self, event: str) -> dict[str, Any]:
        while True:
            message = json.loads(self.from_plugin.readline())
            if message["event"] == event:
                return message

    def close(self) -> None:
        self.to_plugin.close()
        self.thread.join(5)
        for stream in (self._in, self._out, self.from_plugin):
            stream.close()


def _with_host(body: Any, plugin_id: str = "test.acts") -> None:
    host = _Host(plugin_id)
    try:
        body(host)
    finally:
        host.close()


def test_action_receives_context_with_args_input_and_called_at() -> None:
    SEEN.clear()

    def body(host: _Host) -> None:
        host.send("run_action", _call("a.rec", "r1", input="press"))
        assert_that(host.recv_until("action_result")["payload"]).is_equal_to({"request": "r1", "ok": True})
        ctx = SEEN[0]
        assert_that(ctx.action).is_equal_to("a.rec")
        assert_that(ctx.request).is_equal_to("r1")
        assert_that(ctx.args).is_equal_to({"n": 1})
        assert_that(ctx.input).is_equal_to(Input.PRESS)
        assert_that(ctx.called_at).is_equal_to(datetime(2026, 11, 21, 10, 0, 0, 123000, tzinfo=timezone.utc))

    _with_host(body)


def test_plugin_input_without_card_gives_none_card_and_caller() -> None:
    SEEN.clear()

    def body(host: _Host) -> None:
        host.send("run_action", _call("a.rec", "r2"))
        host.recv_until("action_result")
        assert_that(SEEN[0].input).is_equal_to(Input.PLUGIN)
        assert_that(SEEN[0].card).is_none()
        assert_that(SEEN[0].caller).is_none()

    _with_host(body)


def test_unparseable_called_at_falls_back_to_now() -> None:
    SEEN.clear()

    def body(host: _Host) -> None:
        host.send("run_action", _call("a.rec", "r2b", called_at="garbage"))
        host.recv_until("action_result")
        assert_that(SEEN[0].called_at.tzinfo).is_not_none()

    _with_host(body)


def test_unknown_input_value_is_answered_with_an_error() -> None:
    def body(host: _Host) -> None:
        host.send("run_action", _call("a.rec", "r3", input="wiggle"))
        payload = host.recv_until("action_result")["payload"]
        assert_that(payload["ok"]).is_false()
        assert_that(payload["request"]).is_equal_to("r3")

    _with_host(body)


def test_raising_action_reports_type_and_message() -> None:
    def body(host: _Host) -> None:
        host.send("run_action", _call("a.boom", "r4"))
        payload = host.recv_until("action_result")["payload"]
        assert_that(payload).is_equal_to({"request": "r4", "ok": False, "error": "ValueError: nope"})

    _with_host(body)


def test_own_live_card_gives_own_proxy_with_instance() -> None:
    def body(host: _Host) -> None:
        host.send(
            "will_appear",
            {"card": "test.acts.c", "device": "d", "key": {"index": 0, "column": 0, "row": 0}, "settings": {}},
            "c1",
        )
        host.recv_until("set_title")
        for index in range(2):
            host.send("run_action", _call("a.bump", f"r{index}", input="press", card=_card_payload()))
            assert_that(host.recv_until("action_result")["payload"]["ok"]).is_true()
        host.send("run_action", _call("a.rec", "rs", card=_card_payload()))
        host.recv_until("action_result")
        card = SEEN[-1].card
        assert_that(card).is_instance_of(OwnCardProxy)
        assert isinstance(card, OwnCardProxy)
        assert_that(card.instance.count).is_equal_to(2)  # type: ignore[attr-defined]

    SEEN.clear()
    _with_host(body)


def test_foreign_card_proxy_is_closed_after_the_action_returns() -> None:
    STORED.clear()

    def body(host: _Host) -> None:
        host.send("run_action", _call("a.store", "r5", card=_card_payload("other.plugin")))
        host.recv_until("action_result")
        assert_that(isinstance(STORED[0], OwnCardProxy)).is_false()
        assert_that(STORED[0].show_ok).raises(RuntimeError)

    _with_host(body)


def test_card_is_never_own_without_a_plugin_id() -> None:
    SEEN.clear()

    def body(host: _Host) -> None:
        host.send(
            "will_appear",
            {"card": "test.acts.c", "device": "d", "key": {"index": 0, "column": 0, "row": 0}, "settings": {}},
            "c1",
        )
        host.recv_until("set_title")
        host.send("run_action", _call("a.rec", "rn", card=_card_payload("")))
        host.recv_until("action_result")
        card = SEEN[-1].card
        assert_that(isinstance(card, OwnCardProxy)).is_false()
        assert_that(hasattr(card, "set_settings")).is_false()

    _with_host(body, plugin_id="")
