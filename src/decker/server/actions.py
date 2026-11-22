"""Runs plugin actions: the registry of what is available and the execution (built-in or in a plugin process)."""

import logging
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from decker.plugin.bindings import NOTHING_ACTION_ID
from decker.plugin.loader import InstalledPlugin
from decker.plugin.manifest import ActionSpec
from decker.plugin.protocol import Message
from decker.plugin.runtime.supervisor import PluginSupervisor
from decker.plugin.settings_schema import (
    SettingsValidationError,
    apply_schema,
    schema_to_json,
)
from decker.server.action_calls import ActionCall
from decker.server.builtin_actions import BUILTIN_ACTIONS, ActionError

DEFAULT_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class ActionResult:
    """How a run ended: ``ok``, or the reason it did not."""

    ok: bool
    error: str = ""
    value: Any = None


ArgsResolver = Callable[[ActionSpec, Mapping[str, Any], str | None], dict[str, Any]]


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
        resolve: ArgsResolver,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._supervisor = supervisor
        self._on_failure = on_failure
        self._resolve = resolve
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
                "scope": spec.scope,
                "returns": spec.returns,
            }
            for plugin, spec in entries
        ]

    def scope_of(self, action_id: str) -> tuple[str, str] | None:
        """The owning plugin id and scope of an installed action, or None."""
        with self._lock:
            entry = self._actions.get(action_id)
        return (entry[0].manifest.id, entry[1].scope) if entry is not None else None

    def run(self, call: ActionCall) -> None:
        """Run a call without waiting; a failure is reported through ``on_failure``."""
        threading.Thread(target=self.execute, args=(call,), name="run-action", daemon=True).start()

    def execute(self, call: ActionCall) -> ActionResult:
        """Run a call and wait for it to end (up to the timeout)."""
        result = self._execute(call)
        if not result.ok:
            logging.warning("Action %s failed: %s", call.action, result.error)
            self._on_failure(call.action, result.error)
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

    def _execute(self, call: ActionCall) -> ActionResult:
        if call.action == NOTHING_ACTION_ID:
            return ActionResult(True)
        with self._lock:
            entry = self._actions.get(call.action)
        if entry is None:
            return ActionResult(False, f"unknown action {call.action}")
        plugin, spec = entry
        try:
            validated = apply_schema(spec.settings, dict(call.args), strict=True)
        except SettingsValidationError as exc:
            detail = "; ".join(f"{key}: {message}" for key, message in exc.errors.items())
            return ActionResult(False, f"invalid arguments: {detail}")
        args = self._resolve(spec, validated, call.device)
        if plugin.manifest.runtime is None:
            return self._run_builtin(BUILTIN_ACTIONS.get(call.action), args)
        return self._run_in_plugin(plugin.manifest.id, call, args)

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

    def _run_in_plugin(self, plugin_id: str, call: ActionCall, args: Mapping[str, Any]) -> ActionResult:
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
                Message("run_action", None, call.payload(request, args)),
            )
            if not pending.done.wait(call.timeout or self._timeout):
                return ActionResult(False, "timed out")
            return pending.result
        finally:
            self._supervisor.release(plugin_id)
            with self._lock:
                self._pending.pop(request, None)
