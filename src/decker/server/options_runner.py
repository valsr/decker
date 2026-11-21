"""Asks a plugin for the options of a dynamic choice field and waits for its answer."""

import logging
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from decker.common.options import clean_options
from decker.plugin.protocol import Message
from decker.plugin.runtime.supervisor import PluginSupervisor

DEFAULT_TIMEOUT_S = 5.0
_MAX_ERROR = 256


@dataclass(frozen=True)
class OptionsResult:
    """The options a source supplied, or why it could not."""

    ok: bool
    options: tuple[dict[str, str], ...] = ()
    error: str = ""


@dataclass
class _Pending:
    plugin_id: str
    done: threading.Event = field(default_factory=threading.Event)
    result: OptionsResult = OptionsResult(False, error="no answer")


class OptionsRunner:
    """Sends ``get_options`` to a plugin and blocks the caller until ``options_result`` arrives."""

    def __init__(self, supervisor: PluginSupervisor, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._supervisor: PluginSupervisor = supervisor
        self._timeout: float = timeout
        self._lock: threading.Lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._closed: bool = False

    def fetch(self, plugin_id: str, source: str) -> OptionsResult:
        """Ask the plugin for the options of `source`; never raises for a bad plugin answer."""
        if self._supervisor.is_failed(plugin_id):
            return OptionsResult(False, error="the plugin has failed")
        request = uuid.uuid4().hex
        pending = _Pending(plugin_id)
        with self._lock:
            if self._closed:
                return OptionsResult(False, error="the server is stopping")
            self._pending[request] = pending
        try:
            self._supervisor.acquire(plugin_id)  # starts a lazy plugin; the message queues until it is ready
            self._supervisor.send(plugin_id, Message("get_options", None, {"request": request, "source": source}))
            if not pending.done.wait(self._timeout):
                return OptionsResult(False, error="timed out")
            return pending.result
        finally:
            self._supervisor.release(plugin_id)
            with self._lock:
                self._pending.pop(request, None)

    def on_result(self, plugin_id: str, payload: Mapping[str, Any]) -> None:
        """A plugin answered a ``get_options`` request; the first answer counts."""
        pending = self._take(payload.get("request") if isinstance(payload, Mapping) else None, plugin_id)
        if pending is None:
            logging.debug("[%.64s] dropped an options result for an unknown request", plugin_id)
            return
        pending.result = self._result_of(payload)
        pending.done.set()

    def _take(self, request: Any, plugin_id: str) -> _Pending | None:
        with self._lock:
            pending = self._pending.get(request) if isinstance(request, str) else None
            if pending is None or pending.plugin_id != plugin_id:
                return None
            return self._pending.pop(str(request))

    def plugin_stopped(self, plugin_id: str) -> None:
        """Fail every fetch waiting on the plugin."""
        self._fail(lambda pid: pid == plugin_id, "the plugin stopped")

    def stop_all(self) -> None:
        """Server shutdown: fail every waiting fetch and refuse new ones."""
        with self._lock:
            self._closed = True
        self._fail(lambda _pid: True, "the server is stopping")

    def _fail(self, matches: Callable[[str], bool], error: str) -> None:
        with self._lock:
            waiting = [(r, p) for r, p in self._pending.items() if matches(p.plugin_id)]
            for request, _pending in waiting:
                del self._pending[request]
        for _request, pending in waiting:
            pending.result = OptionsResult(False, error=error)
            pending.done.set()

    @staticmethod
    def _result_of(payload: Mapping[str, Any]) -> OptionsResult:
        if payload.get("ok") is True:
            return OptionsResult(True, tuple(clean_options(payload.get("options"))))
        error = payload.get("error")
        return OptionsResult(False, error=error[:_MAX_ERROR] if isinstance(error, str) else "the options source failed")
