"""Runs plugin actions: the registry of what is available and the execution (built-in or in a plugin process)."""

import json
import logging
import math
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from decker.common.card_variables import is_valid_slot
from decker.plugin.bindings import NOTHING_ACTION_ID
from decker.plugin.loader import InstalledPlugin
from decker.plugin.manifest import ActionSpec
from decker.plugin.protocol import MAX_VALUE_BYTES, Message
from decker.plugin.runtime.supervisor import PluginSupervisor
from decker.plugin.settings_schema import (
    SettingsValidationError,
    apply_schema,
    schema_to_json,
)
from decker.server.action_calls import MAX_DEPTH, ActionCall, BindingRef

DEFAULT_TIMEOUT_S = 30.0
MAX_CALL_TIMEOUT_S = 300.0
_ENDED_KEPT = 1024  # runs remembered after they end, so a late nested call still finds its parent


@dataclass(frozen=True)
class ActionResult:
    """How a run ended: ``ok``, or the reason it did not."""

    ok: bool
    error: str = ""
    value: Any = None


ArgsResolver = Callable[[ActionSpec, Mapping[str, Any], ActionCall], dict[str, Any]]


class _Pending:
    def __init__(self, plugin_id: str, call: ActionCall) -> None:
        self.plugin_id = plugin_id
        self.call = call
        self.done = threading.Event()
        self.result = ActionResult(False, "no answer")
        self.deadline = 0.0


class ActionRunner:
    """Keeps the actions plugins contribute and runs them on demand, off the caller's thread."""

    def __init__(
        self,
        supervisor: PluginSupervisor,
        on_failure: Callable[[str, str], None],
        resolve: ArgsResolver,
        timeout: float = DEFAULT_TIMEOUT_S,
        on_finished: Callable[[ActionCall, ActionResult], None] | None = None,
    ) -> None:
        self._supervisor = supervisor
        self._on_failure = on_failure
        self._resolve = resolve
        self._timeout = timeout
        self._on_finished = on_finished
        self._lock = threading.Lock()
        self._actions: dict[str, tuple[InstalledPlugin, ActionSpec]] = {}
        self._pending: dict[str, _Pending] = {}
        self._ended: OrderedDict[str, str] = OrderedDict()

    def register(self, plugin: InstalledPlugin) -> None:
        """Make a plugin's actions available."""
        with self._lock:
            for spec in plugin.manifest.actions:
                self._actions[spec.id] = (plugin, spec)

    def unregister(self, plugin_id: str) -> None:
        """Forget a plugin's actions; runs waiting on it end as failed."""
        with self._lock:
            self._actions = {a: entry for a, entry in self._actions.items() if entry[0].manifest.id != plugin_id}
        self._end_pending(plugin_id, "the plugin was removed")

    def plugin_stopped(self, plugin_id: str) -> None:
        """The plugin's process ended: its calls in flight end as failed and lose their card access."""
        self._end_pending(plugin_id, "the plugin stopped")

    def may_touch(self, plugin_id: str, request: Any, context: str) -> bool:
        """Whether ``plugin_id`` may touch the card ``context`` now: only while a call it was sent carrying it runs."""
        if not isinstance(request, str):
            return False
        with self._lock:
            pending = self._pending.get(request)
        if pending is None or pending.plugin_id != plugin_id or pending.done.is_set() or pending.call.card is None:
            return False
        return pending.call.card.get("context") == context

    def variable_target(self, plugin_id: str, request: Any) -> tuple[ActionCall, ActionSpec] | None:
        """The in-flight call ``plugin_id`` was sent under ``request``, with its action's spec; None if there is none."""
        if not isinstance(request, str):
            return None
        with self._lock:
            pending = self._pending.get(request)
            entry = self._actions.get(pending.call.action) if pending is not None else None
        if pending is None or entry is None or pending.plugin_id != plugin_id or pending.done.is_set():
            return None
        return pending.call, entry[1]

    def _finished(self, call: ActionCall, result: ActionResult) -> None:
        if self._on_finished is None or call.ref is None or call.action == NOTHING_ACTION_ID:
            return
        try:
            self._on_finished(call, result)
        except Exception:  # pylint: disable=broad-except
            logging.exception("Action %s: recording its status failed", call.action)

    def may_drive(self, plugin_id: str, request: Any, device_id: str) -> bool:
        """Whether ``plugin_id`` may navigate ``device_id`` now: only while a call it was sent for that device runs."""
        if not isinstance(request, str):
            return False
        with self._lock:
            pending = self._pending.get(request)
        return (
            pending is not None
            and pending.plugin_id == plugin_id
            and not pending.done.is_set()
            and pending.call.device == device_id
        )

    def _end_pending(self, plugin_id: str, reason: str) -> None:
        with self._lock:
            waiting = {r: p for r, p in self._pending.items() if p.plugin_id == plugin_id}
            for request in waiting:
                del self._pending[request]
                self._remember_ended(request, plugin_id)
        for pending in waiting.values():
            pending.result = ActionResult(False, reason)
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
                "cards": [card.id for card in plugin.manifest.cards if spec.id in card.actions],
                "variables": [{"name": v.name, "description": v.description} for v in spec.variables],
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
        result = self._execute_safely(call)
        self._finished(call, result)
        if not result.ok:
            logging.warning("Action %s failed: %s", call.action, result.error)
            self._on_failure(call.action, result.error)
        return result

    def on_result(self, plugin_id: str, payload: Mapping[str, Any]) -> None:
        """A plugin answered a ``run_action`` request."""
        request = payload.get("request")
        with self._lock:
            pending = self._pending.get(request) if isinstance(request, str) else None
            if pending is not None and pending.plugin_id == plugin_id:
                self._pending.pop(str(request))
                self._remember_ended(str(request), plugin_id)
            else:
                pending = None
        if pending is None:
            logging.debug("[%s] dropped an action result for an unknown request", plugin_id)
            return
        ok = bool(payload.get("ok"))
        if not ok:
            pending.result = ActionResult(False, str(payload.get("error") or "the action failed"))
        else:
            pending.result = self._value_result(payload.get("value"))
        pending.done.set()

    @staticmethod
    def _value_result(value: Any) -> ActionResult:
        try:
            size = len(json.dumps(value).encode())
        except (TypeError, ValueError):
            return ActionResult(False, "return value is not JSON")
        if size > MAX_VALUE_BYTES:
            return ActionResult(False, "return value larger than 1 MiB")
        return ActionResult(True, value=value)

    def on_plugin_call(self, plugin_id: str, payload: Mapping[str, Any]) -> None:
        """A plugin asked to run an action; answered with ``call_result`` from its own thread."""
        request = payload.get("request")
        if not isinstance(request, str):
            logging.debug("[%s] dropped a call_action without a request id", plugin_id)
            return
        threading.Thread(
            target=self._answer_plugin_call, args=(plugin_id, request, payload), name="plugin-call", daemon=True
        ).start()

    def _answer_plugin_call(self, plugin_id: str, request: str, payload: Mapping[str, Any]) -> None:
        try:
            result = self._plugin_call(plugin_id, payload)
        except Exception as exc:  # pylint: disable=broad-except
            logging.exception("[%s] call_action crashed", plugin_id)
            result = ActionResult(False, str(exc) or exc.__class__.__name__)
        reply: dict[str, Any] = {"request": request, "ok": result.ok}
        if result.ok:
            reply["value"] = result.value
        else:
            reply["error"] = result.error
        try:
            self._supervisor.send(plugin_id, Message("call_result", None, reply))
        except Exception:  # pylint: disable=broad-except
            logging.warning("[%s] could not deliver a call_result", plugin_id, exc_info=True)

    @staticmethod
    def _malformed(payload: Mapping[str, Any]) -> str:
        timeout, depth = payload.get("timeout"), payload.get("depth")
        if not isinstance(payload.get("action"), str) or not isinstance(payload.get("args", {}), dict):
            return "action must be a string and args an object"
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            return "timeout must be a positive number"
        if depth is not None and (isinstance(depth, bool) or not isinstance(depth, int)):
            return "depth must be an integer"
        if not all(
            payload.get(key) is None or isinstance(payload.get(key), str) for key in ("chain", "device", "parent")
        ):
            return "chain, device and parent must be strings"
        return ""

    def _plugin_call(self, plugin_id: str, payload: Mapping[str, Any]) -> ActionResult:
        slot = payload.get("slot")
        if slot is not None and not (isinstance(slot, str) and is_valid_slot(slot)):
            return ActionResult(False, "invalid slot")
        parent = payload.get("parent")
        ref = self._slot_ref(plugin_id, parent, slot) if isinstance(parent, str) else None
        refused = self._refused_call(plugin_id, payload)
        if refused is not None:
            self._finished(ActionCall(str(payload.get("action")), {}, "plugin", ref=ref), refused)
            return refused
        action, chain, device, timeout = (payload.get(k) for k in ("action", "chain", "device", "timeout"))
        call_depth, inherited = self._derive(plugin_id, chain, payload.get("depth") or 0)
        call = ActionCall(
            str(action),
            payload.get("args", {}),
            "plugin",
            caller=plugin_id,
            chain=chain or uuid.uuid4().hex,
            depth=call_depth,
            device=device if device is not None else inherited,
            timeout=min(float(timeout), MAX_CALL_TIMEOUT_S) if timeout is not None else None,
            parent=parent,
            ref=ref,
        )
        result = self._execute_safely(call)
        self._finished(call, result)
        return result

    def _refused_call(self, plugin_id: str, payload: Mapping[str, Any]) -> ActionResult | None:
        """Why a call cannot even start (malformed, refused action, too deep), or None."""
        problem = self._malformed(payload)
        if problem:
            return ActionResult(False, f"malformed call: {problem}")
        refusal = self._refusal(plugin_id, str(payload.get("action")))
        if refusal:
            return ActionResult(False, refusal)
        depth, _ = self._derive(plugin_id, payload.get("chain"), payload.get("depth") or 0)
        if depth > MAX_DEPTH:
            return ActionResult(False, f"call depth limit ({MAX_DEPTH}) exceeded")
        return None

    def _slot_ref(self, plugin_id: str, parent: Any, slot: str | None) -> BindingRef | None:
        """The binding a slotted call writes to: its parent's, when the parent is a gesture run of the caller."""
        target = self.variable_target(plugin_id, parent) if slot is not None else None
        if target is None or target[0].ref is None or target[0].ref.slot is not None:
            return None
        return BindingRef(target[0].ref.context, target[0].ref.binding, slot)

    def _refusal(self, plugin_id: str, action: str) -> str:
        if action == NOTHING_ACTION_ID:
            return ""
        found = self.scope_of(action)
        if found is None:
            return f"unknown action {action}"
        owner, scope = found
        if scope == "plugin" and owner != plugin_id:
            return f"action {action} is not callable from {plugin_id}"
        return ""

    def _derive(self, plugin_id: str, chain: str | None, depth: int) -> tuple[int, str | None]:
        if chain is None:
            return max(0, depth), None
        with self._lock:
            pendings = [p for p in self._pending.values() if p.call.chain == chain]
        device = next((p.call.device for p in pendings if p.call.device is not None), None)
        mine = [p.call.depth for p in pendings if p.plugin_id == plugin_id]
        return (max(mine) + 1 if mine else max(0, depth)), device

    def _execute_safely(self, call: ActionCall) -> ActionResult:
        try:
            return self._execute(call)
        except Exception as exc:  # pylint: disable=broad-except
            logging.exception("Action %s crashed", call.action)
            return ActionResult(False, str(exc) or exc.__class__.__name__)

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
        args = self._resolve(spec, validated, call)
        return self._run_in_plugin(plugin.manifest.id, call, args)

    def timeout_for(self, call: ActionCall) -> float:
        """Seconds the call may take: its own timeout, else the action's, else the default; never past its parent."""
        spec = self.spec(call.action)
        if call.timeout is not None:
            wait = call.timeout
        elif spec is not None and spec.timeout is not None:
            wait = float(spec.timeout)
        else:
            wait = self._timeout
        wait = min(wait, MAX_CALL_TIMEOUT_S)
        left = self._parent_time_left(call)
        return wait if left is None else min(wait, left)

    def _parent_time_left(self, call: ActionCall) -> float | None:
        """Seconds left to the caller's run this call names; 0 once it ended; None when there is no such run."""
        if call.parent is None:
            return None
        with self._lock:
            parent = self._pending.get(call.parent)
            ended = self._ended.get(call.parent) == call.caller
        if parent is None:
            return 0.0 if ended else None
        if parent.plugin_id != call.caller:
            return None
        return 0.0 if parent.done.is_set() else parent.deadline - time.monotonic()

    def _remember_ended(self, request: str, plugin_id: str) -> None:
        """Note that a run ended (the lock is held)."""
        self._ended[request] = plugin_id
        while len(self._ended) > _ENDED_KEPT:
            self._ended.popitem(last=False)

    def _run_in_plugin(self, plugin_id: str, call: ActionCall, args: Mapping[str, Any]) -> ActionResult:
        if self._supervisor.is_failed(plugin_id):
            return ActionResult(False, "the plugin has failed")
        wait = self.timeout_for(call)
        if wait <= 0:
            return ActionResult(False, "the calling action ran out of time")
        request = uuid.uuid4().hex
        pending = _Pending(plugin_id, call)
        pending.deadline = time.monotonic() + wait
        with self._lock:
            self._pending[request] = pending
        self._supervisor.acquire(plugin_id)  # starts the process when no card keeps it running
        try:
            self._supervisor.send(
                plugin_id,
                Message("run_action", None, call.payload(request, args)),
            )
            if not pending.done.wait(wait):
                return ActionResult(False, "timed out")
            return pending.result
        finally:
            with self._lock:
                self._pending.pop(request, None)
                self._remember_ended(request, plugin_id)
            self._supervisor.release(plugin_id)
