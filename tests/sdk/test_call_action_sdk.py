import asyncio
import json
import os
import queue
import threading
from datetime import datetime
from typing import Any

import pytest
from assertpy import assert_that

from decker_plugin_sdk import (
    ActionCallContext,
    ActionError,
    call_action,
    call_action_async,
    run,
)
from decker_plugin_sdk._actions import CallHost
from decker_plugin_sdk._pool import ElasticPool

WAIT = 5.0
RESULTS: dict[str, Any] = {}


def _value(_ctx: ActionCallContext) -> Any:
    return {"a": [1, 2]}


def _unencodable(_ctx: ActionCallContext) -> Any:
    return object()


def _too_big(_ctx: ActionCallContext) -> Any:
    return "x" * (1024 * 1024 + 1)


def _caller(ctx: ActionCallContext) -> Any:
    return call_action("t.target", {"k": ctx.args.get("k")})


def _catching(_ctx: ActionCallContext) -> Any:
    try:
        call_action("t.target", timeout=0.01)
    except ActionError as exc:
        return f"caught: {exc}"
    return "no error"


def _async_caller(_ctx: ActionCallContext) -> Any:
    async def main() -> Any:
        return await call_action_async("t.target", {"k": 1})

    return asyncio.run(main())


def _nest(ctx: ActionCallContext) -> Any:
    level = int(ctx.args["level"])
    if level >= 6:
        return level
    return call_action("t.nest", {"level": level + 1})


def _refuses(_ctx: ActionCallContext) -> Any:
    raise ActionError("HTTP 404")


def _crashes(_ctx: ActionCallContext) -> Any:
    raise ValueError("boom")


ACTIONS = {
    "t.refuses": _refuses,
    "t.crashes": _crashes,
    "t.value": _value,
    "t.unencodable": _unencodable,
    "t.big": _too_big,
    "t.caller": _caller,
    "t.catching": _catching,
    "t.async": _async_caller,
    "t.nest": _nest,
}


class _Host:
    """Plays the server over os.pipe streams; reads on a thread so every wait is bounded."""

    def __init__(self) -> None:
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        self.to_plugin = os.fdopen(in_w, "wb")
        self._in = os.fdopen(in_r, "rb")
        self._out = os.fdopen(out_w, "wb")
        self.from_plugin = os.fdopen(out_r, "rb")
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.thread = threading.Thread(target=lambda: run({}, self._in, self._out, ACTIONS))
        self.thread.start()
        self.reader = threading.Thread(target=self._read)
        self.reader.start()
        self.send("hello", {"protocol": 4, "decker_version": "x", "plugin_id": "t", "global_settings": {}})
        self.recv_until("ready")

    def _read(self) -> None:
        for line in self.from_plugin:
            self.messages.put(json.loads(line))
        self.messages.put(None)

    def send(self, event: str, payload: dict[str, Any]) -> None:
        self.to_plugin.write(json.dumps({"event": event, "payload": payload}).encode() + b"\n")
        self.to_plugin.flush()

    def recv(self) -> dict[str, Any]:
        try:
            message = self.messages.get(timeout=WAIT)
        except queue.Empty:
            raise AssertionError("no message from the plugin within the wait") from None
        assert message is not None, "plugin closed stdout"
        return message

    def recv_until(self, event: str) -> dict[str, Any]:
        for _ in range(100):
            message = self.recv()
            if message["event"] == event:
                return message
        raise AssertionError(f"no {event} message")

    def run_action(self, action_id: str, request: str, **extra: Any) -> None:
        payload = {
            "action": action_id,
            "request": request,
            "args": {},
            "input": "plugin",
            "called_at": "2026-11-21T10:00:00+00:00",
            "card": None,
            "caller": None,
            **extra,
        }
        self.send("run_action", payload)

    def close(self) -> None:
        self.to_plugin.close()
        self.thread.join(WAIT)
        self._out.close()
        self.reader.join(WAIT)
        for stream in (self._in, self.from_plugin):
            stream.close()


def _host() -> _Host:
    return _Host()


def test_action_return_value_is_sent_in_action_result() -> None:
    host = _host()
    try:
        host.run_action("t.value", "r1")
        assert_that(host.recv_until("action_result")["payload"]).is_equal_to(
            {"request": "r1", "ok": True, "value": {"a": [1, 2]}}
        )
    finally:
        host.close()


def test_an_action_error_is_reported_as_its_bare_message() -> None:
    host = _host()
    try:
        host.run_action("t.refuses", "r1")
        assert_that(host.recv_until("action_result")["payload"]).is_equal_to(
            {"request": "r1", "ok": False, "error": "HTTP 404"}
        )
        host.run_action("t.crashes", "r2")
        assert_that(host.recv_until("action_result")["payload"]["error"]).is_equal_to("ValueError: boom")
    finally:
        host.close()


def test_unencodable_return_value_is_an_error() -> None:
    host = _host()
    try:
        host.run_action("t.unencodable", "r1")
        payload = host.recv_until("action_result")["payload"]
        assert_that(payload["ok"]).is_false()
        assert_that(payload["error"]).contains("JSON")
        host.run_action("t.big", "r2")
        payload = host.recv_until("action_result")["payload"]
        assert_that(payload["ok"]).is_false()
        assert_that(payload["error"]).contains("too large")
    finally:
        host.close()


def test_call_action_returns_call_result_value() -> None:
    host = _host()
    try:
        host.run_action("t.caller", "r1", args={"k": 7}, chain="ch", depth=0)
        call = host.recv_until("call_action")["payload"]
        assert_that(call["action"]).is_equal_to("t.target")
        assert_that(call["args"]).is_equal_to({"k": 7})
        assert_that(call["timeout"]).is_equal_to(30.0)
        host.send("call_result", {"request": "unknown", "ok": True, "value": 0})  # ignored
        host.send("call_result", {"request": call["request"], "ok": True, "value": {"answer": 42}})
        assert_that(host.recv_until("action_result")["payload"]["value"]).is_equal_to({"answer": 42})
    finally:
        host.close()


def test_call_action_raises_action_error_on_refusal() -> None:
    host = _host()
    try:
        host.run_action("t.caller", "r1")
        call = host.recv_until("call_action")["payload"]
        host.send("call_result", {"request": call["request"], "ok": False, "error": "depth limit"})
        payload = host.recv_until("action_result")["payload"]
        assert_that(payload["ok"]).is_false()
        assert_that(payload["error"]).contains("depth limit")
    finally:
        host.close()


def test_call_action_times_out() -> None:
    host = _host()
    try:
        host.run_action("t.catching", "r1")
        call = host.recv_until("call_action")["payload"]
        payload = host.recv_until("action_result")["payload"]
        assert_that(payload["value"]).starts_with("caught: ").contains("timed out")
        host.send("call_result", {"request": call["request"], "ok": True, "value": 1})  # late reply is ignored
        host.send("ping", {})
        assert_that(host.recv_until("pong")["event"]).is_equal_to("pong")
    finally:
        host.close()


def test_nested_call_inside_action_sends_chain_and_depth_plus_one() -> None:
    host = _host()
    try:
        host.run_action("t.caller", "r1", chain="ch", depth=2)
        call = host.recv_until("call_action")["payload"]
        assert_that(call).contains_entry({"chain": "ch"}, {"depth": 3}).does_not_contain_key("device")
        host.send("call_result", {"request": call["request"], "ok": True, "value": None})
        host.recv_until("action_result")
    finally:
        host.close()


def test_call_from_main_thread_starts_new_chain_without_device() -> None:
    host = _host()
    try:
        outcome: list[Any] = []
        caller = threading.Thread(target=lambda: outcome.append(call_action("t.target", {"x": 1})))
        caller.start()
        call = host.recv_until("call_action")["payload"]
        assert_that(call).does_not_contain_key("chain", "depth", "device")
        host.send("call_result", {"request": call["request"], "ok": True, "value": "done"})
        caller.join(WAIT)
        assert_that(outcome).is_equal_to(["done"])
    finally:
        host.close()


def test_call_action_async_from_coroutine() -> None:
    host = _host()
    try:
        host.run_action("t.async", "r1", chain="ch", depth=0)
        call = host.recv_until("call_action")["payload"]
        assert_that(call).contains_entry({"chain": "ch"}, {"depth": 1})
        host.send("call_result", {"request": call["request"], "ok": True, "value": "async"})
        assert_that(host.recv_until("action_result")["payload"]["value"]).is_equal_to("async")
    finally:
        host.close()


def test_nested_calls_deeper_than_pool_do_not_deadlock() -> None:
    host = _host()
    try:
        host.run_action("t.nest", "top", args={"level": 0}, chain="ch", depth=0)
        pending: dict[str, str] = {}  # run_action request -> call_action request awaiting its result
        top: dict[str, Any] | None = None
        for _ in range(40):
            message = host.recv()
            payload = message["payload"]
            if message["event"] == "call_action":
                run_request = f"run-{payload['request']}"
                pending[run_request] = payload["request"]
                host.run_action(
                    payload["action"], run_request, args=payload["args"], chain="ch", depth=payload["depth"]
                )
            elif message["event"] == "action_result":
                if payload["request"] == "top":
                    top = payload
                    break
                host.send(
                    "call_result",
                    {"request": pending.pop(payload["request"]), "ok": payload["ok"], "value": payload.get("value")},
                )
        assert_that(top).is_not_none()
        assert top is not None
        assert_that(top).contains_entry({"ok": True}, {"value": 6})
    finally:
        host.close()


def _async_nest(ctx: ActionCallContext) -> Any:
    level = int(ctx.args["level"])
    if level >= 6:
        return level

    async def main() -> Any:
        return await call_action_async("t.anest", {"level": level + 1})

    return asyncio.run(main())


ACTIONS["t.anest"] = _async_nest


def test_nested_async_calls_deeper_than_pool_do_not_deadlock() -> None:
    host = _host()
    try:
        host.run_action("t.anest", "top", args={"level": 0}, chain="ch", depth=0)
        pending: dict[str, str] = {}
        top: dict[str, Any] | None = None
        for _ in range(40):
            message = host.recv()
            payload = message["payload"]
            if message["event"] == "call_action":
                run_request = f"run-{payload['request']}"
                pending[run_request] = payload["request"]
                host.run_action(
                    payload["action"], run_request, args=payload["args"], chain="ch", depth=payload["depth"]
                )
            elif message["event"] == "action_result":
                if payload["request"] == "top":
                    top = payload
                    break
                host.send(
                    "call_result",
                    {"request": pending.pop(payload["request"]), "ok": payload["ok"], "value": payload.get("value")},
                )
        assert top is not None
        assert_that(top).contains_entry({"ok": True}, {"value": 6})
    finally:
        host.close()


def _calls() -> tuple[CallHost, list[tuple[str, dict[str, Any] | None]]]:
    sent: list[tuple[str, dict[str, Any] | None]] = []
    return CallHost(lambda event, _ctx, payload: sent.append((event, payload)), ElasticPool()), sent


def test_unencodable_args_raise_and_leave_no_pending_call() -> None:
    host, sent = _calls()
    with pytest.raises(ActionError, match="JSON"):
        host.call("t.target", {"when": datetime.now()}, 1.0, None)
    assert_that(sent).is_empty()
    assert_that(host._pending).is_empty()  # pylint: disable=protected-access


def test_failed_send_leaves_no_pending_call() -> None:
    def broken(_event: str, _ctx: str | None, _payload: dict[str, Any] | None) -> None:
        raise OSError("pipe")

    host = CallHost(broken, ElasticPool())
    with pytest.raises(OSError):
        host.call("t.target", None, 1.0, None)
    assert_that(host._pending).is_empty()  # pylint: disable=protected-access


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0, -1.0])
def test_invalid_timeout_is_refused_before_sending(timeout: float) -> None:
    host, sent = _calls()
    with pytest.raises(ActionError, match="timeout"):
        host.call("t.target", None, timeout, None)
    assert_that(sent).is_empty()


def test_reply_racing_the_local_timeout_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    host, _sent = _calls()
    real_wait = threading.Event.wait

    def racing_wait(event: threading.Event, timeout: float | None = None) -> bool:
        if not event.is_set() and host._pending:  # pylint: disable=protected-access
            request = next(iter(host._pending))  # pylint: disable=protected-access
            host.deliver({"request": request, "ok": True, "value": "late"})  # reply wins, then wait "times out"
            return False
        return real_wait(event, timeout)

    monkeypatch.setattr(threading.Event, "wait", racing_wait)
    assert_that(host.call("t.target", None, 1.0, None)).is_equal_to("late")
