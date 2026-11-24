"""Deck tracking and plugin-wide deck event hooks."""

import threading
from collections.abc import Callable
from typing import Any, TypeVar

_F = TypeVar("_F", bound=Callable[..., Any])

DEVICE_CONNECTED = "device_connected"
DEVICE_DISCONNECTED = "device_disconnected"

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


class DeckHost:
    """Per-runtime state behind the module helpers: the connected decks and the pool that runs the hooks."""

    def __init__(self, submit: Callable[..., None], log_error: Callable[[str], None]) -> None:
        self._submit = submit
        self._log_error = log_error
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
