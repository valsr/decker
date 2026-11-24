"""Deck tracking and plugin-wide deck event hooks."""

import threading
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, TypeVar

from ._actions import _FRAME, ActionError

_F = TypeVar("_F", bound=Callable[..., Any])

DEVICE_CONNECTED = "device_connected"
DEVICE_DISCONNECTED = "device_disconnected"
BRIGHTNESS_CHANGED = "deck_brightness_changed"

_HOOKS: dict[str, list[Callable[..., Any]]] = {}


def _hook(event: str, fn: _F) -> _F:
    _HOOKS.setdefault(event, []).append(fn)
    return fn


def on_device_connected(fn: Callable[[str], None]) -> Callable[[str], None]:
    """Register ``fn(device_id)``; it runs on the worker pool when a deck connects."""
    return _hook(DEVICE_CONNECTED, fn)


def on_device_disconnected(fn: Callable[[str], None]) -> Callable[[str], None]:
    """Register ``fn(device_id)``; it runs on the worker pool when a deck disconnects."""
    return _hook(DEVICE_DISCONNECTED, fn)


def on_deck_brightness_changed(fn: Callable[[str, int], None]) -> Callable[[str, int], None]:
    """Register ``fn(device_id, percent)``; it runs on the worker pool when a deck's brightness changes."""
    return _hook(BRIGHTNESS_CHANGED, fn)


class _Waiter:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.payload: dict[str, Any] = {}


class DeckHost:
    """Per-runtime state behind the module helpers: the connected decks and the pool that runs the hooks."""

    def __init__(
        self,
        submit: Callable[..., None],
        log_error: Callable[[str], None],
        send: Callable[[str, str | None, dict[str, Any] | None], None],
        blocking: Callable[[], AbstractContextManager[None]],
    ) -> None:
        self._submit = submit
        self._log_error = log_error
        self._send = send
        self._blocking = blocking
        self._pending: dict[str, _Waiter] = {}
        self._lock = threading.Lock()
        self._devices: set[str] = set()

    def devices(self) -> list[str]:
        with self._lock:
            return sorted(self._devices)

    def device_event(self, event: str, payload: dict[str, Any]) -> None:
        """Track a connect or disconnect now (the reader thread keeps messages in order), run hooks on the pool."""
        device = payload.get("device")
        if not isinstance(device, str):
            return
        with self._lock:
            if event == DEVICE_CONNECTED:
                self._devices.add(device)
            else:
                self._devices.discard(device)
        self.dispatch(event, device)

    def brightness_changed(self, payload: dict[str, Any]) -> None:
        """Run the brightness hooks for a deck_brightness_changed message."""
        device, percent = payload.get("device"), payload.get("percent")
        if isinstance(device, str) and isinstance(percent, int):
            self.dispatch(BRIGHTNESS_CHANGED, device, percent)

    def deliver_brightness(self, payload: dict[str, Any]) -> None:
        """Hand a deck_brightness reply to its waiting caller; unknown requests are ignored."""
        with self._lock:
            waiter = self._pending.pop(str(payload.get("request")), None)
        if waiter is not None:
            waiter.payload = payload
            waiter.done.set()

    def get_brightness(self, device_id: str | None, timeout: float) -> int:
        """Ask the server for a deck's brightness; raises ActionError on error, timeout or no device."""
        device = self._device(device_id)
        request = uuid.uuid4().hex
        waiter = _Waiter()
        with self._lock:
            self._pending[request] = waiter
        try:
            self._send("get_deck_brightness", None, {"request": request, "device": device})
            with self._blocking():
                arrived = waiter.done.wait(timeout)
        finally:
            with self._lock:
                self._pending.pop(request, None)
        if not arrived:
            raise ActionError("get_deck_brightness timed out")
        percent = waiter.payload.get("percent")
        if not isinstance(percent, int):
            raise ActionError(str(waiter.payload.get("error") or "get_deck_brightness failed"))
        return percent

    def set_brightness(self, percent: int, device_id: str | None) -> None:
        """Ask the server to set a deck's brightness; the server clamps it to 1-100."""
        self._send("set_deck_brightness", None, {"device": self._device(device_id), "percent": percent})

    @staticmethod
    def _device(device_id: str | None) -> str:
        frame = _FRAME.get()
        device = device_id or (frame.device if frame is not None else None)
        if not device:
            raise ActionError("no device: pass device_id")
        return device

    def dispatch(self, event: str, *args: Any) -> None:
        for hook in list(_HOOKS.get(event, ())):
            self._submit(self._call, event, hook, *args)

    def _call(self, event: str, hook: Callable[..., Any], *args: Any) -> None:
        try:
            hook(*args)
        except Exception as exc:  # pylint: disable=broad-except
            self._log_error(f"{event} hook {getattr(hook, '__name__', hook)!r} failed: {exc}")


_current: DeckHost | None = None


def set_current(host: DeckHost | None) -> None:
    """Make host the target of the module-level helpers."""
    global _current  # pylint: disable=global-statement
    _current = host


def connected_devices() -> list[str]:
    """Ids of the decks the server reports as connected, sorted."""
    return _current.devices() if _current is not None else []


def get_deck_brightness(device_id: str | None = None, timeout: float = 5.0) -> int:
    """A deck's brightness in percent (default: the current call's device); raises ActionError on failure."""
    if _current is None:
        raise RuntimeError("no Decker plugin runtime is running")
    return _current.get_brightness(device_id, timeout)


def set_deck_brightness(percent: int, device_id: str | None = None) -> None:
    """Set a deck's brightness, clamped to 1-100 (default: the current call's device); the server logs refusals."""
    if _current is None:
        raise RuntimeError("no Decker plugin runtime is running")
    _current.set_brightness(percent, device_id)
