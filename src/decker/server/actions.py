"""Runs plugin actions: the registry of what is available and the execution (built-in or in a plugin process)."""

import logging
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from decker.plugin.loader import InstalledPlugin
from decker.plugin.manifest import ActionSpec
from decker.plugin.protocol import Message
from decker.plugin.runtime.supervisor import PluginSupervisor
from decker.plugin.settings_schema import apply_schema, schema_to_json
from decker.server.builtin_actions import BUILTIN_ACTIONS, ActionError

DEFAULT_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class ActionResult:
    """How a run ended: ``ok``, or the reason it did not."""

    ok: bool
    error: str = ""


class _Pending:
    def __init__(self, plugin_id: str) -> None:
        self.plugin_id = plugin_id
        self.done = threading.Event()
        self.result = ActionResult(False, "no answer")


class ActionRunner:
    """Keeps the actions plugins contribute and runs them on demand, off the caller's thread."""

    def __init__(
        self,
        supervisor: PluginSupervisor,
        on_failure: Callable[[str, str], None],
        builtin: Mapping[str, Callable[[Mapping[str, Any]], None]] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._supervisor = supervisor
        self._on_failure = on_failure
        self._builtin = builtin if builtin is not None else BUILTIN_ACTIONS
        self._timeout = timeout
        self._lock = threading.Lock()
        self._actions: dict[str, tuple[InstalledPlugin, ActionSpec]] = {}
        self._pending: dict[str, _Pending] = {}

    def register(self, plugin: InstalledPlugin) -> None:
        """Make a plugin's actions available."""
        with self._lock:
            for spec in plugin.manifest.actions:
                self._actions[spec.id] = (plugin, spec)

    def unregister(self, plugin_id: str) -> None:
        """Forget a plugin's actions; runs waiting on it end as failed."""
        with self._lock:
            self._actions = {a: entry for a, entry in self._actions.items() if entry[0].manifest.id != plugin_id}
            waiting = [p for p in self._pending.values() if p.plugin_id == plugin_id]
        for pending in waiting:
            pending.result = ActionResult(False, "the plugin was removed")
            pending.done.set()

    def spec(self, action_id: str) -> ActionSpec | None:
        """The declared action, or None."""
        with self._lock:
            entry = self._actions.get(action_id)
        return entry[1] if entry is not None else None

    def listing(self, icon_b64: Callable[[InstalledPlugin, ActionSpec], str]) -> list[dict[str, Any]]:
        """Every available action as the dicts sent to clients."""
        with self._lock:
            entries = list(self._actions.values())
        return [
            {
                "plugin_uuid": plugin.manifest.id,
                "plugin_name": plugin.manifest.name,
                "action_uuid": spec.id,
                "action_name": spec.name,
                "tooltip": spec.tooltip,
                "icon_b64": icon_b64(plugin, spec),
                "settings_schema": schema_to_json(spec.settings),
            }
            for plugin, spec in entries
        ]

    def run(self, action_id: str, settings: Mapping[str, Any]) -> None:
        """Run an action without waiting; a failure is reported through ``on_failure``."""
        threading.Thread(target=self.execute, args=(action_id, settings), name="run-action", daemon=True).start()

    def execute(self, action_id: str, settings: Mapping[str, Any]) -> ActionResult:
        """Run an action and wait for it to end (up to the timeout)."""
        result = self._execute(action_id, settings)
        if not result.ok:
            logging.warning("Action %s failed: %s", action_id, result.error)
            self._on_failure(action_id, result.error)
        return result

    def on_result(self, plugin_id: str, payload: Mapping[str, Any]) -> None:
        """A plugin answered a ``run_action`` request."""
        request = payload.get("request")
        with self._lock:
            pending = self._pending.get(request) if isinstance(request, str) else None
        if pending is None or pending.plugin_id != plugin_id:
            logging.debug("[%s] dropped an action result for an unknown request", plugin_id)
            return
        ok = bool(payload.get("ok"))
        pending.result = ActionResult(ok, "" if ok else str(payload.get("error") or "the action failed"))
        pending.done.set()

    def _execute(self, action_id: str, settings: Mapping[str, Any]) -> ActionResult:
        with self._lock:
            entry = self._actions.get(action_id)
        if entry is None:
            return ActionResult(False, f"unknown action {action_id}")
        plugin, spec = entry
        values = apply_schema(spec.settings, dict(settings))
        executor = self._builtin.get(action_id)
        if plugin.manifest.runtime is None:
            return self._run_builtin(executor, values)
        return self._run_in_plugin(plugin.manifest.id, action_id, values)

    @staticmethod
    def _run_builtin(executor: Callable[[Mapping[str, Any]], None] | None, values: Mapping[str, Any]) -> ActionResult:
        if executor is None:
            return ActionResult(False, "the plugin has no code to run")
        try:
            executor(values)
        except ActionError as exc:
            return ActionResult(False, str(exc))
        except Exception as exc:  # pylint: disable=broad-except
            logging.exception("Built-in action crashed")
            return ActionResult(False, str(exc) or exc.__class__.__name__)
        return ActionResult(True)

    def _run_in_plugin(self, plugin_id: str, action_id: str, values: Mapping[str, Any]) -> ActionResult:
        if self._supervisor.is_failed(plugin_id):
            return ActionResult(False, "the plugin has failed")
        request = uuid.uuid4().hex
        pending = _Pending(plugin_id)
        with self._lock:
            self._pending[request] = pending
        self._supervisor.acquire(plugin_id)  # starts the process when no card keeps it running
        try:
            self._supervisor.send(
                plugin_id,
                Message("run_action", None, {"action": action_id, "request": request, "settings": dict(values)}),
            )
            if not pending.done.wait(self._timeout):
                return ActionResult(False, "timed out")
            return pending.result
        finally:
            self._supervisor.release(plugin_id)
            with self._lock:
                self._pending.pop(request, None)
