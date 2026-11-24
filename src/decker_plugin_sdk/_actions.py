"""Action registration and the context an action receives."""

import asyncio
import contextvars
import json
import math
import threading
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, TypeVar

from ._pool import ElasticPool
from ._proxy import CardProxy

MAX_VALUE_BYTES = 1024 * 1024  # keep in sync with decker.plugin.protocol.MAX_VALUE_BYTES
MAX_TIMEOUT = 300.0  # the server caps call timeouts here
WAIT_AFTER_RACE = 1.0


class Input(StrEnum):
    """What triggered an action."""

    PRESS = "press"
    DOUBLE_PRESS = "double_press"
    LONG_PRESS = "long_press"
    PLUGIN = "plugin"


@dataclass(frozen=True)
class ActionCallContext:  # pylint: disable=too-many-instance-attributes
    """Everything an action function is told about its call."""

    action: str
    request: str
    called_at: datetime
    input: Input
    card: CardProxy | None
    args: dict[str, Any]
    caller: str | None
    device_id: str | None = None
    _chain: str = ""
    _depth: int = 0
    _emit: Callable[[str, dict[str, Any]], None] | None = None

    def _navigate(self, event: str, payload: dict[str, Any]) -> None:
        if self.device_id is None or self._emit is None:
            raise ActionError("no device for this call")
        self._emit(event, {"request": self.request, "device": self.device_id, **payload})

    def switch_page(self, to: str = "next", wrap: bool = False) -> None:
        """Switch the call's device to the ``next``/``previous``/``first``/``last`` page; ``wrap`` loops at the ends."""
        self._navigate("switch_page", {"to": to, "wrap": wrap})

    def go_to_page(self, page_id: str) -> None:
        """Switch the call's device to the page with this id."""
        self._navigate("switch_page", {"page_id": page_id})

    def switch_profile(self, to: str = "next", wrap: bool = False) -> None:
        """Switch the call's device to the ``next``/``previous``/``first``/``last`` profile; ``wrap`` loops."""
        self._navigate("switch_profile", {"to": to, "wrap": wrap})

    def go_to_profile(self, profile_id: int) -> None:
        """Switch the call's device to the profile with this id."""
        self._navigate("switch_profile", {"profile_id": profile_id})


ActionFn = Callable[[ActionCallContext], Any]
_F = TypeVar("_F", bound=ActionFn)
_ACTIONS: dict[str, ActionFn] = {}


def action(action_id: str) -> "Callable[[_F], _F]":
    """Register ``fn(ctx: ActionCallContext)`` under a manifest action id; it runs when the action does."""

    def decorate(fn: _F) -> _F:
        _ACTIONS[action_id] = fn
        return fn

    return decorate


def registered_actions() -> dict[str, ActionFn]:
    """The actions registered so far with @action."""
    return dict(_ACTIONS)


class ActionError(Exception):
    """A call_action was refused, failed or timed out; the message says why."""


@dataclass(frozen=True)
class CallFrame:
    """The call an action runs in: what a nested call_action forwards."""

    chain: str
    depth: int
    request: str
    device: str | None = None


_FRAME: contextvars.ContextVar[CallFrame | None] = contextvars.ContextVar("decker_call_frame", default=None)


def encode_value(value: Any) -> str | None:
    """The error text when value cannot be sent as an action result, else None."""
    try:
        size = len(json.dumps(value).encode("utf-8"))
    except (TypeError, ValueError, RecursionError) as exc:
        return f"return value is not JSON-encodable: {exc}"
    if size > MAX_VALUE_BYTES:
        return f"return value too large ({size} bytes, limit {MAX_VALUE_BYTES})"
    return None


class _Waiter:
    def __init__(self) -> None:
        self.done: threading.Event = threading.Event()
        self.payload: dict[str, Any] = {}


class CallHost:
    """Sends call_action requests and matches the call_result replies."""

    def __init__(self, send: Callable[[str, str | None, dict[str, Any] | None], None], pool: ElasticPool) -> None:
        self._send: Callable[[str, str | None, dict[str, Any] | None], None] = send
        self._pool: ElasticPool = pool
        self._lock: threading.Lock = threading.Lock()
        self._pending: dict[str, _Waiter] = {}

    def deliver(self, payload: dict[str, Any]) -> None:
        """Hand a call_result to its waiting caller; unknown requests are ignored."""
        with self._lock:
            waiter = self._pending.pop(str(payload.get("request")), None)
        if waiter is not None:
            waiter.payload = payload
            waiter.done.set()

    def blocking(self) -> AbstractContextManager[None]:
        """Mark the calling pool worker as waiting (see ElasticPool.blocking)."""
        return self._pool.blocking()

    def call(self, action_id: str, args: dict[str, Any] | None, timeout: float | None, device: str | None) -> Any:
        """Send call_action and wait for its call_result; no timeout lets the server use the action's own."""
        if timeout is not None and (
            not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0
        ):
            raise ActionError(f"timeout must be a positive finite number, not {timeout!r}")
        try:
            json.dumps(args or {})
        except (TypeError, ValueError, RecursionError) as exc:
            raise ActionError(f"args are not JSON-encodable: {exc}") from exc
        request = uuid.uuid4().hex
        message: dict[str, Any] = {"request": request, "action": action_id, "args": args or {}}
        if timeout is not None:
            message["timeout"] = timeout
        frame = _FRAME.get()
        if frame is not None:
            message.update(chain=frame.chain, depth=frame.depth + 1, parent=frame.request)
        device = device or (frame.device if frame is not None else None)
        if device:
            message["device"] = device
        waiter = _Waiter()
        with self._lock:
            self._pending[request] = waiter
        try:
            self._send("call_action", None, message)
        except BaseException:
            self._forget(request)
            raise
        with self._pool.blocking():
            arrived = waiter.done.wait(MAX_TIMEOUT + 1 if timeout is None else min(timeout, MAX_TIMEOUT) + 1)
        if not arrived and self._forget(request) is not None:
            raise ActionError(f"call to {action_id} timed out")
        waiter.done.wait(WAIT_AFTER_RACE)  # a reply took the entry just before the timeout did
        if not waiter.payload:
            raise ActionError(f"call to {action_id} timed out")
        if not waiter.payload.get("ok"):
            raise ActionError(str(waiter.payload.get("error") or "call failed"))
        return waiter.payload.get("value")

    def _forget(self, request: str) -> "_Waiter | None":
        with self._lock:
            return self._pending.pop(request, None)


_CALLS: CallHost | None = None


def set_calls(host: CallHost | None) -> None:
    """Make host the target of call_action."""
    global _CALLS  # pylint: disable=global-statement
    _CALLS = host


def call_action(
    action_id: str, args: dict[str, Any] | None = None, timeout: float | None = None, device: str | None = None
) -> Any:
    """Run an action through the server and return its value; raises ActionError on refusal, failure or timeout."""
    host = _CALLS
    if host is None:
        raise RuntimeError("no Decker plugin runtime is running")
    return host.call(action_id, args, timeout, device)


async def call_action_async(
    action_id: str, args: dict[str, Any] | None = None, timeout: float | None = None, device: str | None = None
) -> Any:
    """call_action for coroutines; it waits on a thread so the event loop stays free."""
    host = _CALLS
    if host is None:
        raise RuntimeError("no Decker plugin runtime is running")
    with host.blocking():  # the pool worker running this loop is waiting too
        return await asyncio.to_thread(call_action, action_id, args, timeout, device)
