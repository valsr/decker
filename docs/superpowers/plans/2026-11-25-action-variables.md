# Action Variables Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every key binding its own variables (`status` plus action-declared ones), let templates read them as `{press.pid}`, and give Sequence steps variables of their own (`{press.step_1.pid}`).

**Architecture:** An in-memory `CardVariableStore` on the server, keyed by key context id, binding and optional slot. A gesture run carries a `BindingRef` (context, binding, slot) on its `ActionCall`; nested calls inherit it through their `parent` request when they pass a `slot`. The runner reports finished calls to the manager, which sets `status`. Plugins set values through a new request/answer protocol event. Template expansion gets a card layer in `PluginManager._resolve_args`.

**Tech Stack:** Python 3.12, pytest + assertpy, black/isort/mypy/pylint (see CLAUDE.md quality gates).

**Spec:** `docs/superpowers/specs/2026-11-25-action-variables-design.md`

## Global Constraints

- Protocol version goes 5 → 6 (`PROTOCOL_VERSION`); docs say plugins declaring action `variables` need Decker `">=0.8"`.
- Variable and slot names match `[a-z][a-z0-9_-]*`; `status` is reserved (cannot be declared, cannot be a slot); slots are at most 32 chars; duplicate declared names are manifest errors.
- Values are JSON, at most 64 KiB encoded (`MAX_VALUE_BYTES` in the new module, not the 1 MiB action-result limit).
- Bindings are `press`, `double_press`, `long_press` (`decker.plugin.bindings.GESTURES`); `status` is `"SUCCESS"` or `"FAILURE"`.
- Only `template: true` text/text_style fields expand; whole-value references are text (null → `""`, numbers/bools/objects → JSON text).
- Values survive deactivation and moves; dropped on unassign/replace and server restart; changing a binding's *action* resets its variables, changing only args does not.
- Do Nothing never touches `status`. A nested call without `slot` is attached to no binding and its `set_variable` errors.
- Quality gates before the last commit: `poe format`, `poe mypy` (plus `mypy --platform win32 .` and `--platform darwin .`), `poe pylint` (≥ 9.0), `poe test` (≥ 85%). Methods < 50 lines, ≤ 5 args, docstrings on public items only.

## Review Focus

1. A value over 64 KiB, or not JSON, is refused with an error to the plugin and nothing stored (Task 4).
2. `set_variable` arriving after its action ended, or from another plugin's request id, is refused (Task 4).
3. A literal `{{press.pid}}` in text stays the literal `{press.pid}`, never expanded (Task 2).
4. A plugin crashing or timing out mid-run leaves that binding's `status` `FAILURE` (Task 4).
5. A bad slot (`status`, `Bad`, 33 chars) fails the nested call; two quick presses never mix one binding's values into another's (Tasks 4 and 5).

---

### Task 1: Manifest `variables` on actions

**Files:**
- Modify: `src/decker/plugin/manifest.py` (`ActionSpec` ~line 103, `_parse_action` ~line 447)
- Modify: `src/decker/server/actions.py` (`listing`, add `"variables"` to each dict)
- Create: `src/decker/common/card_variables.py` (rules only, grown in Task 2)
- Test: `tests/plugin/test_manifest.py`, `tests/server/test_actions.py`

**Interfaces:**
- Produces: `ActionVariable(name: str, description: str = "")` frozen dataclass in `manifest.py`; `ActionSpec.variables: tuple[ActionVariable, ...] = ()`.
- Produces in `common/card_variables.py`: `NAME_PATTERN: re.Pattern[str]` (`[a-z][a-z0-9_-]*`), `RESERVED_NAME = "status"`, `MAX_SLOT_LENGTH = 32`, `MAX_VALUE_BYTES = 64 * 1024`, `is_valid_name(name: str) -> bool`, `is_valid_slot(slot: str) -> bool` (valid name, ≤ 32 chars, not `status`).

- [ ] **Step 1: Write failing tests** in `tests/plugin/test_manifest.py`: an action with `"variables": [{"name": "pid", "description": "process id"}]` loads with `spec.variables == (ActionVariable("pid", "process id"),)`; an action without it has `()`; each of these raises `ManifestValidationError`: name `status`, name `Bad`, duplicate `pid`, `variables` not a list, an entry without `name`. In `tests/server/test_actions.py` assert `listing()` items carry `"variables": [{"name": "pid", "description": ...}]`.
- [ ] **Step 2: Run** `uv run pytest tests/plugin/test_manifest.py tests/server/test_actions.py -k variables -v`; expect FAIL (no `ActionVariable`).
- [ ] **Step 3: Implement** the dataclass, `_parse_variables(value: Any, action_id: str) -> tuple[ActionVariable, ...]` (called from `_parse_action`), the rules module, and the listing key.
- [ ] **Step 4: Run** the same command; expect PASS. Also `poe mypy`.
- [ ] **Step 5: Commit** `feat: actions can declare card variables`.

### Task 2: Store and template expansion

**Files:**
- Modify: `src/decker/common/card_variables.py` (add the reference grammar)
- Create: `src/decker/server/card_variables.py`
- Modify: `src/decker/common/template.py` (`_TOKEN`)
- Test: `tests/common/test_card_variables.py`, `tests/server/test_card_variables.py`, `tests/common/test_template.py`

**Interfaces:**
- Consumes: Task 1 rules.
- Produces `CardVariableStore` (thread-safe, methods under one lock):
  - `set(context: str, binding: str, name: str, value: Any, slot: str | None = None) -> None`
  - `set_status(context: str, binding: str, ok: bool, slot: str | None = None) -> None` (stores `"SUCCESS"`/`"FAILURE"` under `status`)
  - `reset_binding(context: str, binding: str) -> None` (variables and slots)
  - `drop(context: str) -> None`
  - `snapshot(context: str) -> dict[str, dict[str, Any]]` (deep copy, `{binding: {name: value, slot: {name: value}}}`)
  - `view(context: str, declared: Mapping[str, frozenset[str]]) -> Mapping[str, str]`: a `Mapping` whose `get("press.pid")` returns the rendered text, `""` when unset, or `None` (left as typed) when two-part and the name is not in `declared[binding] | {"status"}` or the binding is unknown; three-part refs never return `None` for a valid binding and slot syntax. `declared` maps binding → names the bound action declared.
- Produces `render_value(value: Any) -> str` in `common/card_variables.py`: `None` → `""`, `str` as is, everything else `json.dumps`.
- `expand_template` accepts dotted tokens: `_TOKEN` becomes `\{\{|\}\}|\{([a-z_][a-z0-9_]*(?:\.[a-z][a-z0-9_-]*){0,2})\}`; `template_names` returns them too. The lookup is the mapping's `get`, so global names without dots behave exactly as before.

- [ ] **Step 1: Write failing tests.** `test_template.py`: `expand_template("{press.pid}-{x}", {"press.pid": "42", "x": "a"}) == "42-a"`; `"{{press.pid}}"` with the same mapping → `"{press.pid}"`; `"{press.nope}"` with no entry stays as typed; `template_names("{press.step_1.pid} {a}") == {"press.step_1.pid", "a"}`. `test_card_variables.py` (server): set then `view(...).get("press.pid") == "42"`; unset declared → `""`; undeclared two-part → `None`; `press.status` always known; `press.step_1.pid` unset → `""`; `reset_binding` clears `status`, vars and slots of that binding only; `drop` removes all; `render_value` of `{"a": 1}`, `3`, `True`, `None`, `"x"`.
- [ ] **Step 2: Run** `uv run pytest tests/common tests/server/test_card_variables.py -v`; expect FAIL.
- [ ] **Step 3: Implement** store, `render_value`, view class and the regex change.
- [ ] **Step 4: Run** the same plus `tests/server/test_page_vars.py` and `tests/server/test_plugins_variables.py` (existing template behaviour must not change); expect PASS.
- [ ] **Step 5: Commit** `feat: per-card variable store and dotted template references`.

### Task 3: Protocol, SDK `set_variable` and `call_action(slot=)`

**Files:**
- Modify: `src/decker/plugin/protocol.py` (version 6; `set_variable` in `PLUGIN_EVENTS`, `set_variable_result` in `SERVER_EVENTS`)
- Modify: `src/decker_plugin_sdk/_actions.py` (`ActionCallContext.set_variable`, `call_action`/`call_action_async` `slot` parameter, `CallHost.call`), `src/decker_plugin_sdk/_runtime.py` (route `set_variable_result` to the waiter)
- Test: `tests/plugin/test_protocol.py`, `tests/sdk/test_actions_sdk.py`, `tests/sdk/test_call_action_sdk.py`

**Interfaces:**
- Produces protocol: plugin → server `set_variable` payload `{"id": str, "request": str, "name": str, "value": Any}`; server → plugin `set_variable_result` payload `{"id": str, "ok": bool, "error": str}` (`error` only when not ok). `call_action` payload gains optional `"slot": str`.
- Produces SDK: `ActionCallContext.set_variable(self, name: str, value: Any) -> None` (blocks on the answer; raises `ActionError(error)` when refused; raises `ActionError("value is not JSON")` before sending for unencodable values); `call_action(action_id, args=None, timeout=None, device=None, slot=None)` and the async twin pass `slot` into the message only when not None.
- Reuse the `_Waiter` pattern of `CallHost.call` for the answer wait (keyed by `id`; timeout 10 s → `ActionError("set_variable timed out")`).

- [ ] **Step 1: Write failing tests**: protocol sets contain the new events and `PROTOCOL_VERSION == 6`; SDK test with a fake send/answer: `ctx.set_variable("pid", 7)` emits `set_variable` with the call's `request` and returns when `set_variable_result {ok: true}` arrives; with `{ok: false, error: "nope"}` raises `ActionError("nope")`; `call_action("a.b.c", slot="step_1")` puts `slot` in the sent message, and omits the key without it.
- [ ] **Step 2: Run** `uv run pytest tests/plugin/test_protocol.py tests/sdk -k "variable or slot or protocol" -v`; expect FAIL.
- [ ] **Step 3: Implement** as described; update every other place that hard-codes protocol 5 (`grep -rn "PROTOCOL_VERSION\|protocol.*5" src tests`).
- [ ] **Step 4: Run** `uv run pytest tests/plugin tests/sdk -v`; expect PASS.
- [ ] **Step 5: Commit** `feat: sdk set_variable and call slots (protocol 6)`.

### Task 4: Server wiring — binding refs, status, `set_variable`, slots

**Files:**
- Modify: `src/decker/server/action_calls.py` (`BindingRef`, `ActionCall.ref`)
- Modify: `src/decker/server/actions.py` (`ArgsResolver` takes the call; `on_finished` callback; `variable_target`; slot handling in `_plugin_call`)
- Modify: `src/decker/server/plugins.py` (`card_variables` store, `_on_gesture`, `_resolve_args`, message routing for `set_variable`, `set_bindings` reset, `drop_card_variables`)
- Modify: `src/decker/server/service.py` (drop calls at `key_assign` replacing a card, `key_unassign`, and the replaced card in `key_move` and `key_copy`; never for the moved source)
- Test: `tests/server/test_card_variables_e2e.py` (new, modelled on `tests/server/test_actions_e2e.py`), `tests/server/test_actions.py`, `tests/server/test_plugins_bindings.py`

**Interfaces:**
- Consumes: Task 2 store/view/`render_value`, Task 1 `ActionSpec.variables`, Task 3 protocol.
- Produces `BindingRef(context: str, binding: str, slot: str | None = None)` frozen dataclass; `ActionCall.ref: BindingRef | None = None`.
- Produces `ActionRunner.__init__(..., on_finished: Callable[[ActionCall, ActionResult], None] | None = None)`; it fires from `execute` and from `_plugin_call` for any call with `ref is not None` whose action is not Do Nothing, once the result is known.
- `ArgsResolver = Callable[[ActionSpec, Mapping[str, Any], ActionCall], dict[str, Any]]`; `_resolve_args(spec, args, call)` keeps its page/variable behaviour from `call.device`.
- Produces `ActionRunner.variable_target(plugin_id: str, request: Any) -> tuple[ActionCall, ActionSpec] | None`: the in-flight, not-done pending call owned by `plugin_id` for that request id, with its spec, or None.
- Produces `PluginManager.drop_card_variables(context_id: str) -> None`.

- [ ] **Step 1: Write failing tests** (real plugin process fixtures as in `test_actions_e2e.py`, or the in-process fakes `test_actions.py` already uses):
  - gesture run of an action that returns → `snapshot(ctx)["press"]["status"] == "SUCCESS"`; one that raises, times out, or whose plugin stops → `"FAILURE"`; Do Nothing leaves no `status`.
  - `set_variable` declared name stores the value and the plugin gets `ok`; undeclared name, `status`, value over 64 KiB, unknown/finished request id, another plugin's request id → `ok: false` and nothing stored.
  - `set_binding` with a changed action resets that binding only; same action with new args does not; a second binding is untouched.
  - `{press.pid}` in a `template: true` field of the `long_press` binding's action arguments arrives expanded with the value `press` stored; unset → empty; `{{press.pid}}` arrives as `{press.pid}`; a typo `{press.pdi}` arrives as typed.
  - slots: a parent run (gesture, binding `press`) calling a child with `slot="step_1"` → child's `set_variable("pid", 1)` stored at `press.step_1.pid`, child's finish sets `press.step_1.status`; a later child with `slot="step_2"` and template `{press.step_1.pid}` sees `1`; a child without slot → `set_variable` refused; slots `status`, `Bad`, 33 chars fail the call with an error; a slotted call whose parent is itself slotted gets no ref.
  - page switch (deactivate + activate) keeps the values; `drop_card_variables` clears them; `key_unassign` and replacing assign call it, `key_move` of the source does not.
- [ ] **Step 2: Run** `uv run pytest tests/server/test_card_variables_e2e.py tests/server/test_actions.py tests/server/test_plugins_bindings.py -v`; expect FAIL.
- [ ] **Step 3: Implement.** `_on_gesture` builds `ActionCall(..., ref=BindingRef(context_id, gesture.value))`. In `_plugin_call` read `payload.get("slot")` (must be a valid slot string, else `ActionResult(False, "invalid slot")`) and derive the ref from `payload["parent"]` through `variable_target`: only when the parent's `call.ref` has no slot and its plugin is the caller. `_resolve_args` builds the mapping with `card_variables.view(call.ref.context, declared)` where `declared[binding]` is `frozenset(v.name for v in spec.variables)` of that binding's currently effective action (`live.bindings`), layered over the existing chain with the card layer first (page and global names have no dots, so no clash). Route `set_variable` in the plugin message handler next to the other no-context events: validate, store, answer with `set_variable_result` via `self._supervisor.send`; log refusals at warning once per plugin and debug after, like `_warn_refused`. Pass `on_finished` to `ActionRunner` to call `set_status`.
- [ ] **Step 4: Run** the same command plus `uv run pytest tests/server -q`; expect PASS.
- [ ] **Step 5: Commit** `feat: per-card variables, status and slots on the server`.

### Task 5: Core plugin — templates in HTTP Request, Sequence slots

**Files:**
- Modify: `src/decker/builtin_plugins/com.decker.core/manifest.json` (`url` gets `"template": true`; `body` does not — `}}` would corrupt nested JSON)
- Modify: `src/decker/builtin_plugins/com.decker.core/plugin.py` (`sequence` passes `slot=f"step_{number}"`)
- Test: `tests/server/test_core_plugin.py`

**Interfaces:**
- Consumes: Task 3 `call_action(..., slot=)`, Task 4 slot handling.
- Produces: Sequence step N runs with slot `step_N`, so `{press.step_1.status}` and any variables step 1's action set are readable by step 2's template fields.

- [ ] **Step 1: Write failing tests**: a Sequence of two steps run from a `press` gesture (Delay then HTTP Request against a local test server) leaves `press.step_1.status == "SUCCESS"`, `press.step_2.status == "SUCCESS"` and `press.status == "SUCCESS"`; a failing step 1 with `stop_on_error: false` has `press.step_1.status == "FAILURE"` and step 2's URL `http://127.0.0.1:{port}/{press.step_1.status}` reaches the server as `/FAILURE`; a manifest check that `url` is a template field and `body` is not.
- [ ] **Step 2: Run** `uv run pytest tests/server/test_core_plugin.py -v`; expect FAIL.
- [ ] **Step 3: Implement** the two edits.
- [ ] **Step 4: Run** `uv run pytest tests/server/test_core_plugin.py tests/server/test_builtin_plugins.py -v`; expect PASS.
- [ ] **Step 5: Commit** `feat: Sequence steps get variable slots; HTTP Request templates`.

### Task 6: RPC, docs, CLAUDE.md

**Files:**
- Modify: `src/decker/server/service.py` (`key_variables`)
- Modify: `docs/plugins/protocol.md`, `docs/plugins/sdk-quickstart.md`, `docs/plugins/package-and-manifest.md`, `CLAUDE.md` (Plugins paragraph: action `variables`, `set_variable`, slots, protocol 6, `key_variables`)
- Test: `tests/server/test_service_card_variables.py`

**Interfaces:**
- Produces RPC `key_variables(device_id: str, page_id: str, key_index: int) -> dict[str, Any]` decorated `@exposed`/`@api_response` like its neighbours: `snapshot` of the key's context, `{}` for an empty key or unknown page.

- [ ] **Step 1: Write failing test**: after a stored value the RPC returns `{"press": {"pid": 7, "status": "SUCCESS"}}`; an empty key returns `{}`.
- [ ] **Step 2: Run** `uv run pytest tests/server/test_service_card_variables.py -v`; expect FAIL.
- [ ] **Step 3: Implement** the RPC and write the docs: manifest `variables` entry shape and reserved `status`, `ctx.set_variable`, `call_action(slot=)`, the reference grammar and the "left as typed" rule, lifetime, protocol events and 64 KiB limit, `"decker": ">=0.8"`.
- [ ] **Step 4: Run** the quality gates from Global Constraints; all green.
- [ ] **Step 5: Commit** `feat: key_variables RPC and docs for card variables`.

---

## Self-review notes

- Spec coverage: declaring (T1), store/lifetime/reset (T2, T4), status (T4), slots (T3, T4, T5), set (T3, T4), reading (T2, T4), visibility (T6), editor opt-in for HTTP Request (T5), docs/version (T3, T6).
- Calls made beyond the spec (also listed in the hand-off message): `key_variables` is a separate RPC instead of extra arguments on `variables_list`; whole-value references expand to text, not typed values, because only text fields opt in to templates; the manifest `decker` floor stays `>=0.8`, so protocol 6 relies on the SDK shipped with Decker; a slotted call inside a slotted call gets no slot.
