"""The stdio event loop: decode server events, dispatch handlers, write replies."""

import json
import sys
import threading
from collections import deque
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, BinaryIO, TypeVar

from . import _options, _variables
from ._actions import (
    _FRAME,
    ActionCallContext,
    ActionError,
    ActionFn,
    CallFrame,
    CallHost,
    Input,
    encode_value,
    set_calls,
)
from ._canvas import FrameTicker
from ._context import MAX_LINE_BYTES, CardContext, encode_line
from ._pool import ElasticPool
from ._proxy import CardProxy, OwnCardProxy

if TYPE_CHECKING:
    from PIL.Image import Image

PLUGIN_WIDE = "\0plugin"  # queue key for context-less events

_REGISTRY: dict[str, type["DeckerPlugin"]] = {}
_T = TypeVar("_T", bound=type["DeckerPlugin"])


class DeckerPlugin:
    """Base class for cards; override the handlers you need."""

    def on_will_appear(self, ctx: CardContext) -> None:
        """The key became visible."""

    def on_will_disappear(self, ctx: CardContext) -> None:
        """The key is no longer visible."""

    def on_key_down(self, ctx: CardContext) -> None:
        """Raw key down."""

    def on_key_up(self, ctx: CardContext) -> None:
        """Raw key up."""

    def on_settings_changed(self, ctx: CardContext) -> None:
        """The key's settings changed."""

    def on_global_settings_changed(self, ctx: CardContext) -> None:
        """The plugin's global settings changed."""

    def on_paint(self, ctx: CardContext, img: "Image") -> None:
        """Draw the key on ``img`` (RGBA, key-sized); runs after ``ctx.queue_paint()``."""


def card(card_id: str) -> "Callable[[_T], _T]":
    """Register a DeckerPlugin subclass under a manifest card id."""

    def decorate(cls: _T) -> _T:
        _REGISTRY[card_id] = cls
        return cls

    return decorate


def registered_cards() -> dict[str, type[DeckerPlugin]]:
    """The cards registered so far with @card."""
    return dict(_REGISTRY)


_HANDLERS = {
    "will_appear": "on_will_appear",
    "will_disappear": "on_will_disappear",
    "key_down": "on_key_down",
    "key_up": "on_key_up",
    "settings_changed": "on_settings_changed",
    "global_settings_changed": "on_global_settings_changed",
}


def _warn(text: str) -> None:
    print(f"decker_plugin_sdk: {text}", file=sys.stderr, flush=True)


class _Runtime:  # pylint: disable=too-many-instance-attributes
    def __init__(
        self,
        cards: Mapping[str, type[DeckerPlugin]],
        stdout: BinaryIO,
        actions: Mapping[str, ActionFn] | None = None,
    ) -> None:
        self._cards = cards
        self._actions = actions or {}
        self._stdout = stdout
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pool = ElasticPool()
        self._frames = FrameTicker(self.send, blocking=self._pool.blocking)
        self._paints: set[str] = set()
        self._calls = CallHost(self.send, self._pool)
        self._queues: dict[str, deque[dict[str, Any]]] = {}
        self._contexts: dict[str, tuple[DeckerPlugin, CardContext]] = {}
        self._global_settings: dict[str, Any] = {}
        self._plugin_id: str = ""
        self._stopping = False
        self._vars = _variables.VariableHost(self.send, self.log_error)
        self._opts = _options.OptionsHost(self.send, self.log_error)

    @property
    def calls(self) -> CallHost:
        return self._calls

    @property
    def variables(self) -> _variables.VariableHost:
        return self._vars

    def send(self, event: str, context: str | None = None, payload: dict[str, Any] | None = None) -> None:
        line = encode_line(event, context, payload)
        with self._write_lock:
            if self._stopping:
                return
            try:
                self._stdout.write(line)
                self._stdout.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass  # server went away; the reader will see EOF

    def log_error(self, message: str) -> None:
        _warn(message)
        self.send("log", None, {"level": "error", "message": message})

    def serve(self, stdin: BinaryIO) -> int:
        try:
            while True:
                line = stdin.readline(MAX_LINE_BYTES + 1)
                if not line:
                    return 0
                if not line.strip():
                    continue
                if not self._handle_line(line):
                    return 0
        finally:
            self._stopping = True
            self._vars.stop()
            self._frames.stop()
            self._pool.shutdown()

    def _handle_line(self, line: bytes) -> bool:
        """Process one line; False means stop the loop."""
        try:
            if len(line) > MAX_LINE_BYTES:
                raise ValueError("line too long")
            data = json.loads(line.decode("utf-8"))
        except (ValueError, RecursionError) as exc:  # includes JSON and Unicode decode errors
            _warn(f"skipping malformed line: {exc}")
            return True
        event = data.get("event") if isinstance(data, dict) else None
        context = data.get("context") if isinstance(data, dict) else None
        payload = data.get("payload") if isinstance(data, dict) else None
        if not isinstance(event, str) or not isinstance(payload or {}, dict) or not isinstance(context or "", str):
            _warn("skipping message with invalid event/context/payload")
            return True
        payload = payload or {}
        if event == "shutdown":
            return False
        if event == "ping":
            self.send("pong", context)
        elif event == "hello":
            self._on_hello(payload)
        elif event == "global_settings_changed":
            self._on_global_settings(payload)
        elif event == "get_variable":
            self._submit(self._vars.answer, payload)
        elif event == "get_options":
            self._submit(self._opts.answer, payload)
        elif event == "variables_changed":
            self._vars.changed(payload)
        elif event == "call_result":
            self._calls.deliver(payload)
        elif event == "image_result":
            self._frames.deliver(payload)
        elif event == "run_action":
            self._submit_action(payload)
        elif event in _HANDLERS:
            self._enqueue(context or PLUGIN_WIDE, {"event": event, "context": context, "payload": payload})
        return True

    def _submit_action(self, payload: dict[str, Any]) -> None:
        self._pool.submit(self._run_action, payload)

    def _submit(self, fn: Callable[[dict[str, Any]], None], payload: dict[str, Any]) -> None:
        self._pool.submit(fn, payload)

    def _run_action(self, payload: dict[str, Any]) -> None:
        request = payload.get("request")
        handler = self._actions.get(str(payload.get("action")))
        if handler is None:
            self.send("action_result", None, {"request": request, "ok": False, "error": "no such action"})
            return
        proxy: CardProxy | None = None
        token = None
        try:
            ctx = self._action_context(payload)
            proxy = ctx.card
            token = _FRAME.set(CallFrame(ctx._chain, ctx._depth, ctx._device))  # pylint: disable=protected-access
            value = handler(ctx)
            problem = encode_value(value)
            result: dict[str, Any] = {"request": request, "ok": problem is None}
            if problem is not None:
                result["error"] = problem
            elif value is not None:
                result["value"] = value
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.log_error(f"action {payload.get('action')} failed: {type(exc).__name__}: {exc}")
            error = str(exc) if isinstance(exc, ActionError) else f"{type(exc).__name__}: {exc}"
            result = {"request": request, "ok": False, "error": error}
        finally:
            if token is not None:
                _FRAME.reset(token)
        self._end_call(str(request), proxy)  # a foreign card is touchable only until action_result goes out
        self.send("action_result", None, result)

    def _end_call(self, request: str, proxy: CardProxy | None) -> None:
        if proxy is not None:
            proxy.close()
        self._frames.flush(request)

    def _action_context(self, payload: dict[str, Any]) -> ActionCallContext:
        request = str(payload.get("request"))
        caller = payload.get("caller")
        device = payload.get("device")
        fields: dict[str, Any] = {
            "action": str(payload.get("action")),
            "request": request,
            "called_at": self._called_at(payload.get("called_at")),
            "input": Input(str(payload.get("input"))),
            "args": self._dict(payload.get("args")),
            "caller": caller if isinstance(caller, str) else None,
            "_chain": str(payload.get("chain") or ""),
            "_depth": int(payload.get("depth") or 0),
            "_device": device if isinstance(device, str) else None,
        }
        # last: nothing after this can fail and leave the proxy unclosed
        return ActionCallContext(card=self._card_proxy(payload.get("card"), request), **fields)

    @staticmethod
    def _called_at(value: Any) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return datetime.now(timezone.utc)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def _card_proxy(self, data: Any, request: str) -> CardProxy | None:
        if not isinstance(data, dict):
            return None
        entry = self._contexts.get(str(data.get("context")))
        if entry is not None and bool(self._plugin_id) and data.get("plugin") == self._plugin_id:
            return OwnCardProxy(data, entry[1], entry[0])
        return CardProxy(data, request, self.send, self._frames)

    def _on_hello(self, payload: dict[str, Any]) -> None:
        settings = payload.get("global_settings")
        self._plugin_id = str(payload.get("plugin_id") or "")
        self._global_settings = settings if isinstance(settings, dict) else {}
        self.send("ready")
        self._vars.start()

    def _enqueue(self, key: str, item: dict[str, Any]) -> None:
        with self._state_lock:
            queue = self._queues.get(key)
            if queue is not None:
                queue.append(item)
                return
            self._queues[key] = deque([item])
        self._pool.submit(self._drain, key)

    def _drain(self, key: str) -> None:
        while True:
            with self._state_lock:
                queue = self._queues[key]
                if self._stopping or not queue:
                    del self._queues[key]
                    return
                item = queue.popleft()
            try:
                self._dispatch(item)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self.log_error(f"handler for {item['event']} failed: {type(exc).__name__}: {exc}")

    def _queue_paint(self, context: str) -> None:
        with self._state_lock:
            if context in self._paints:
                return
            self._paints.add(context)
        self._enqueue(context, {"event": "paint", "context": context, "payload": {}})

    def _paint(self, context: str) -> None:
        with self._state_lock:
            self._paints.discard(context)
        entry = self._contexts.get(context)
        if entry is not None:
            with entry[1].canvas.paint() as img:
                entry[0].on_paint(entry[1], img)

    def _dispatch(self, item: dict[str, Any]) -> None:
        event, context, payload = item["event"], item["context"], item["payload"]
        if context is None:
            return
        if event == "paint":
            self._paint(context)
            return
        if event == "will_appear":
            self._create(context, payload)
        entry = self._contexts.get(context)
        if entry is None:
            _warn(f"ignoring {event} for unknown context {context}")
            return
        plugin, ctx = entry
        if event == "settings_changed":
            ctx.settings = self._dict(payload.get("settings"))
            ctx.bindings = self._dict(payload.get("bindings"))
        elif event in ("key_down", "key_up"):
            ctx.state = payload.get("state", ctx.state) if isinstance(payload.get("state"), int) else ctx.state
        try:
            getattr(plugin, _HANDLERS[event])(ctx)
        finally:
            if event == "will_disappear":
                self._contexts.pop(context, None)

    @staticmethod
    def _dict(value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _size(value: Any) -> tuple[int, int]:
        if isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(v, int) for v in value):
            return (value[0], value[1])
        return (72, 72)

    def _create(self, context: str, payload: dict[str, Any]) -> None:
        card_id = payload.get("card")
        cls = self._cards.get(card_id) if isinstance(card_id, str) else None
        if cls is None:
            _warn(f"no card registered for {card_id!r}")
            return
        key = self._dict(payload.get("key"))
        state = payload.get("state")
        ctx = CardContext(
            context=context,
            card=str(card_id),
            device=str(payload.get("device", "")),
            key_index=int(key.get("index", 0)),
            column=int(key.get("column", 0)),
            row=int(key.get("row", 0)),
            settings=self._dict(payload.get("settings")),
            bindings=self._dict(payload.get("bindings")),
            size=self._size(payload.get("size")),
            global_settings=self._global_settings,
            state=state if isinstance(state, int) else 0,
            _send=self.send,
            _frames=self._frames,
            _paint=lambda: self._queue_paint(context),
        )
        self._contexts[context] = (cls(), ctx)

    def _on_global_settings(self, payload: dict[str, Any]) -> None:
        settings = self._dict(payload.get("global_settings", payload.get("settings")))
        self._global_settings = settings
        for context, (_, ctx) in list(self._contexts.items()):
            ctx.global_settings = settings
            self._enqueue(context, {"event": "global_settings_changed", "context": context, "payload": payload})


def run(
    cards: Mapping[str, type[DeckerPlugin]],
    stdin: BinaryIO,
    stdout: BinaryIO,
    actions: Mapping[str, ActionFn] | None = None,
) -> int:
    """Serve the protocol on the given streams until shutdown or EOF; returns the exit code."""
    runtime = _Runtime(cards, stdout, actions)
    _variables.set_current(runtime.variables)
    set_calls(runtime.calls)
    try:
        return runtime.serve(stdin)
    finally:
        _variables.set_current(None)
        set_calls(None)
