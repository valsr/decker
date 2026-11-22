# Plugin Actions Revamp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cards become pure views and actions become the only thing a key does: every card has press, double press and long press bindings, every action runs in its plugin's process (built-ins included), plugins call actions and get values back, and plugins paint keys through a double-buffered canvas.

**Architecture:** Manifest format 3 (only format) adds action `scope`/`returns`, card `bindings` and declarative validation rules. The server stores per-key binding overrides next to the key's settings, registers gestures from the effective bindings and hands each recognised gesture to `ActionRunner` as an `ActionCall` (protocol 4 `run_action`). Plugins call actions through the server (`call_action`/`call_result`, chain and depth limit 8). The SDK gains `ActionCallContext`, `CardProxy`, `call_action` and a per-process canvas with a ~30 fps frame tick. Core becomes a built-in out-of-process plugin run with the server's interpreter. Counter, Pages, Profiles and Fontawesome (decker-plugins) are migrated last, and decker-plugins pins the final Decker commit.

**Tech Stack:** Python 3.14, PyQt6, RPyC, JSON lines over stdio, Pillow (canvas only), pytest + assertpy, black (120), isort, mypy (linux/win32/darwin), pylint; decker-plugins: uv, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-11-21-plugin-actions-revamp-design.md`

## Global Constraints

- Two repos, one branch each, nothing merges until both are done: `/work/decker/decker` on `feat/plugin-actions-revamp` (checked out); `/work/decker/decker-plugins` on `feat/plugin-actions-revamp`, created off `main` in Task 25.
- Quality gates before a phase is done (Decker): `poe format`, then `poe mypy` **and** `uv run mypy --platform win32 .` **and** `uv run mypy --platform darwin .` (write each command out; no POSIX-only APIs such as `signal.SIGKILL`, use `psutil`), `poe pylint` (≥ 9.0), `poe test` (coverage ≥ 85%). decker-plugins: `uv run ruff check .` and `uv run pytest`.
- Code style: annotate everything (returns, parameters, instance attributes); methods under 50 lines; at most 5 arguments (self counts); docstrings on public items only, one short line; black 120 + isort.
- Tests: pytest + assertpy, `-> None` on new tests, real components over mocks, unique test basenames across directories. Fast loop: `uv run pytest <paths> --no-cov`; Qt tests: `QT_QPA_PLATFORM=offscreen uv run pytest <paths> --no-cov`. Every phase ends with an end-to-end test through a real plugin process; tests that symlink an interpreter into a fixture venv skip `win32` (as `tests/server/test_options_e2e.py` does).
- Manifest `"format": 3` is the only accepted format; format 1 and 2 are refused with a message naming the format. No compatibility layer. `PROTOCOL_VERSION = 4`.
- Decker's version becomes `0.7.0` (pyproject and both built-in manifests); every migrated manifest declares `"decker": ">=0.7"`.
- Action `scope` is `"global"` or `"plugin"`, default `"plugin"`. A card may bind its own plugin's actions of either scope, any global action, or Do Nothing.
- Gestures are exactly `press`, `double_press`, `long_press`. Do Nothing is `com.decker.core.nothing`: handled by the server, never dispatched, its gesture never registered. An omitted manifest gesture is Do Nothing. A stored gesture that is missing means "manifest default"; stored `{"action": "com.decker.core.nothing"}` means the user chose Do Nothing.
- A binding to an action that is not installed (or a foreign plugin-scoped action) is kept and acts as Do Nothing.
- Validation rules: `required` (any field; `""`, `null` and missing are errors — a missing key is checked after its `default` is filled in, see Self-review note), `pattern` (text, `re.fullmatch`, compiled at manifest load, invalid pattern is a manifest error), `min_length`, `max_length` (text).
- Call limits: depth > 8 refused; `action_result`/`call_result` value ≤ 1 MiB encoded (`MAX_VALUE_BYTES = 1024 * 1024`); default timeout 30 s; frame tick ~33 ms; flood protection (100 msg/s, burst 200), watchdog and lazy start unchanged.
- Foreign plugins may touch a card (`set_image`, navigation, `show_ok`/`show_alert`, `get_image`) only while the call that carried it is in flight, keyed by request id; config writes are always refused.
- The SDK stays dependency-free at import time; Pillow is imported only when the canvas is used, and its absence raises a clear `RuntimeError`.
- Built-in plugins with a `runtime` run with `sys.executable` and the staged SDK, no uv environment, supervised and lazily started like any plugin.
- The screensaver rule stands: waking never fires an action (the device layer swallows the press before `dispatch_key_down`; do not move gesture dispatch above it).
- Docs in `docs/plugins/`, `CLAUDE.md`, `README.md` (both repos) are updated in Task 24/28, before the final pin.
- Commit messages end with a blank line then `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- **Uninstall and reinstall of a bound action's plugin:** the key keeps the stored binding, the gesture stops being registered (a press on a key whose double press pointed there fires at once), and after reinstall the binding works again with no user action. Test: Task 6 `test_binding_to_uninstalled_action_acts_as_nothing_and_survives_reinstall`.
- **Manifest default changes:** a key whose gesture was never touched follows a new manifest default after a plugin update; a gesture the user set to Do Nothing stays Do Nothing. Test: Task 6 `test_untouched_gesture_follows_new_manifest_default_and_explicit_nothing_stays`.
- **Callee dies or times out while it holds foreign-card access:** access ends with the call (timeout, crash or result); a late `set_image`/`switch_page` for that context is dropped, and the key keeps its last image. Tests: Task 15 `test_foreign_access_ends_when_the_call_times_out`, Task 21 `test_late_foreign_set_image_after_result_is_dropped`.
- **`call_action` outside an action:** from a card handler, an `@main` loop or async code it starts a new chain at depth 0 with no device, so `page`/`profile` arguments arrive unset and device templates expand to `""`, and it never deadlocks the worker pool. Tests: Task 17 `test_call_from_main_thread_starts_new_chain_without_device`, `test_call_action_async_from_coroutine`.
- **Copying keys, pages and profiles:** bindings travel with `key_copy`, `key_move`, page copy and profile clone, and `page` arguments inside binding args are remapped to the copied pages like card settings are. Test: Task 4 `test_copy_page_remaps_page_ids_inside_binding_args`.

---

## Phase 1 — Format 3 manifest, scope, validation rules, protocol 4

### Task 1: Validation rules and strict `apply_schema`

**Files:**
- Modify: `src/decker/plugin/settings_schema.py` (`SettingField`, `_EXTRA_KEYS`, `_parse_extra`, `_parse_field`, `apply_schema`, `_field_to_json`)
- Test: `tests/plugin/test_settings_schema.py`

**Interfaces:**
- Produces:
  - `SettingField.required: bool = False` (manifest key `required`, must be a bool; allowed on every type).
  - `_EXTRA_KEYS["text"]` gains `"pattern"`, `"min_length"`, `"max_length"`; `pattern` must be a string that `re.compile` accepts (else `ValueError` naming `where`); lengths are non-negative ints with `min_length <= max_length`; any of the three on a non-text field is a `ValueError`.
  - `class SettingsValidationError(ValueError)` with attribute `errors: dict[str, str]` (field key → message).
  - `validate_settings(schema: Sequence[SettingEntry], values: Mapping[str, Any]) -> dict[str, str]` — pure; reports: a present value that `_coerce` rejects or that is out of `min`/`max` range; `required` with an empty result (`""`, `None`, missing after the default is filled in); `pattern` not `re.fullmatch`ing; text shorter than `min_length` or longer than `max_length`. Object-normalised types (`text_style`, `icon`, `background`) never error except `required`.
  - `apply_schema(schema, values, strict: bool = False) -> dict[str, Any]` — unchanged when `strict` is false; when true raises `SettingsValidationError(errors)` if `validate_settings` is non-empty.
  - `schema_to_json` emits `required`, `pattern`, `min_length`, `max_length` when set (so the client can re-parse and validate).
  - Compiled patterns are cached: `_compiled(pattern: str) -> re.Pattern[str]` with `functools.lru_cache`.

- [ ] **Step 1: Write failing tests:** `test_required_flags_empty_null_and_missing_without_default` (`validate_settings` on `{"key":"u","type":"text","required":True}` gives `{"u": ...}` for `{}`, `{"u": ""}`, `{"u": None}`); `test_required_with_nonempty_default_accepts_missing_key`; `test_pattern_uses_fullmatch` (pattern `"[a-z]+"`: `"abc"` ok, `"abc1"` error); `test_invalid_pattern_is_a_parse_error` (`pytest.raises(ValueError, match="pattern")`); `test_min_and_max_length` (`min_length=3`: `"ab"` error containing `"3"`; `max_length=4`: `"abcde"` error); `test_length_rules_rejected_on_non_text_fields`; `test_strict_apply_schema_raises_with_all_field_errors` (`exc.errors` has both keys); `test_non_strict_apply_schema_still_falls_back` (out-of-range int returns the default, no raise); `test_invalid_typed_value_is_a_strict_error` (`"x"` for an int field); `test_rules_round_trip_through_schema_to_json` (`parse_settings_schema(schema_to_json(s), "w") == s`).
- [ ] **Step 2: Run** `uv run pytest tests/plugin/test_settings_schema.py --no-cov` — expect FAIL (`required` unknown / `validate_settings` missing).
- [ ] **Step 3: Implement** the interfaces above in `settings_schema.py`.
- [ ] **Step 4: Run** the Step 2 command, then `uv run pytest tests/plugin tests/server --no-cov -q` — expect PASS.
- [ ] **Step 5: Commit**

```bash
git add src/decker/plugin/settings_schema.py tests/plugin/test_settings_schema.py
git commit -m "feat: required, pattern and length rules with a strict apply_schema

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2: Retire Run Action and Switch

The format 3 parser (Task 3) cannot load Core's Run Action and Switch cards: they are only valid because `CardSpec.headless` treats presets of Run Action and `behavior: "switch"` as process-less (`src/decker/plugin/manifest.py:96-104`). They are therefore retired before the parser changes, while format 2 still loads.

**Files:**
- Delete: `src/decker/server/switch.py`, `src/decker/common/slots.py`, `src/decker/client/widgets/slots_edit.py`, `tests/server/test_switch.py`, `tests/server/test_plugins_switch.py`, `tests/server/test_plugins_switch_variables.py`, `tests/common/test_slots.py`, `tests/client/widgets/test_slots_edit.py`
- Modify: `src/decker/builtin_plugins/com.decker.core/manifest.json` (drop both cards; keep `com.decker.core.httprequest`), `src/decker/plugin/manifest.py` (drop `SWITCH_CARD_ID` import, `CardSpec.switch`, `base`, `action`, `action_settings`, `bound`, `_parse_binding`, `_parse_behavior`, `RUN_ACTION_CARD_ID`; `headless` returns `self.static`), `src/decker/plugin/settings_schema.py` (drop `"slots"`), `src/decker/server/plugins.py` (drop the "Switch cards" section `plugins.py:656-817`, `_Live.parent`, `_run_bound`, `_switch_lock`, `_switches`, `_reshow_switches` calls, the switch branches in `activate_card`/`deactivate_card`/`dispatch_settings_changed`/`_gesture_emitter`/`_recompose`/`_key_appearance`/`_preview_subject`), `src/decker/server/actions.py` (drop `bound_action`), `src/decker/server/key_assignments.py`, `src/decker/client/widgets/schema_form.py` (drop `_slots_field`), `src/decker/client/widgets/sidebar.py` (`_action_drag`), `src/decker/client/views/dashboard.py` (`_on_card_dropped`)
- Test: `tests/server/test_key_assignments.py`, `tests/server/test_plugins.py` (delete `TestRunActionCard`), `tests/server/test_actions.py` (delete preset/Run Action tests), `tests/server/test_builtin_plugins.py`, `tests/client/widgets/test_sidebar.py`

**Interfaces:**
- Produces: `RETIRED_CARDS: frozenset[tuple[str, str]] = frozenset({("com.decker.core", "com.decker.core.runaction"), ("com.decker.core", "com.decker.core.switch")})` in `decker.server.key_assignments`. `KeyAssignmentStore.get`, `all_for_page` and `assignments_for_plugin` skip rows naming a retired card and log one `logging.info` per row ("... loads as an empty key"); the row itself is left in the database (no migration).
- Produces: until Task 11 the Actions tab drags the Blank card (`com.decker.blank` / `com.decker.blank.blank`) carrying `preset_action`; the dashboard assigns it with empty settings (the binding is added in Task 11).

- [ ] **Step 1: Write failing tests:** `test_retired_run_action_row_loads_as_an_empty_key` and `test_retired_switch_row_is_skipped_by_all_for_page` (insert rows via `assign` with the retired ids, then `get` is `None` and `all_for_page` is `[]`); `test_core_ships_no_cards` (Core manifest `cards == ()`, actions contain `com.decker.core.httprequest`); `test_action_drag_drops_a_blank_card` (sidebar drag payload `card_uuid == "com.decker.blank.blank"`, `preset_action` set).
- [ ] **Step 2: Run** `uv run pytest tests/server/test_key_assignments.py tests/server/test_builtin_plugins.py --no-cov` and `QT_QPA_PLATFORM=offscreen uv run pytest tests/client/widgets/test_sidebar.py --no-cov` — expect FAIL.
- [ ] **Step 3: Implement** the removals and `RETIRED_CARDS` filtering; delete the listed files and the tests that only covered them.
- [ ] **Step 4: Run** `uv run pytest tests --no-cov -q` with `QT_QPA_PLATFORM=offscreen` — expect PASS; `grep -rn "slots\|runaction\|SwitchController" src` returns nothing but the `RETIRED_CARDS` constant.
- [ ] **Step 5: Commit** `git add -A src tests && git commit` with message `refactor: retire the Run Action and Switch cards and the slots type` (+ footer).

### Task 3: Manifest format 3, bindings defaults, protocol 4, Decker 0.7.0

**Files:**
- Create: `src/decker/plugin/bindings.py`
- Modify: `src/decker/plugin/manifest.py`, `src/decker/plugin/protocol.py`, `pyproject.toml` (`version = "0.7.0"`), both built-in manifests (`"format": 3`, `"version": "0.7.0"`), `src/decker/cli/plugin_scaffold.py` (validation dict `format: 3`), `src/decker/cli/templates/plugin/manifest.json` (`format: 3`, `decker: ">=0.7"`, no `gestures`)
- Modify (migrate to format 3; format 1 `contributes.actions` that were cards become `contributes.cards`; drop `gestures`): `tests/fixtures/plugins/com.decker.testfixture/manifest.json`, `tests/server/fixtures/options_plugin/manifest.json`, `tests/server/fixtures/variable_plugin/manifest.json` and every inline manifest in `tests/asset/test_iconpack.py`, `tests/plugin/test_loader.py`, `tests/plugin/test_manifest.py`, `tests/plugin/test_package.py`, `tests/server/test_actions.py`, `tests/server/test_appearance.py`, `tests/server/test_contributions.py`, `tests/server/test_plugin_installer.py`, `tests/server/test_plugins_background.py`, `tests/server/test_plugins_options.py`, `tests/server/test_plugins.py`, `tests/server/test_plugins_variables.py`, `tests/server/test_protocol_conformance.py`, `tests/server/test_repositories.py`, `tests/server/test_update_checker.py` (index `"format": 1` in `test_repository_index.py` is the index format — leave it)
- Test: `tests/plugin/test_bindings.py` (new), `tests/plugin/test_manifest.py`, `tests/plugin/test_protocol.py`, `tests/common/test_version.py`, `tests/cli/test_plugin_scaffold.py`

**Interfaces:**
- Produces (`decker.plugin.bindings`, importable by server and client):
  - `GESTURES: tuple[str, ...] = ("press", "double_press", "long_press")`
  - `NOTHING_ACTION_ID: str = "com.decker.core.nothing"`
  - `@dataclass(frozen=True) class Binding: action: str; args: Mapping[str, Any]` (`args` stored as `MappingProxyType`, `hash=False`); property `is_nothing -> bool`; `to_json() -> dict[str, Any]` → `{"action": ..., "args": {...}}`.
  - `parse_binding(raw: Any, where: str) -> Binding` — needs `{"action": <valid reverse-DNS id>, "args"?: object}`; raises `ValueError` mentioning `where`.
  - `parse_manifest_bindings(raw: Any, where: str) -> dict[str, Binding]` — `raw` is an object whose keys are a subset of `GESTURES`; returns all three gestures, omitted ones `Binding(NOTHING_ACTION_ID, {})`.
  - `normalize_stored_bindings(raw: Any) -> dict[str, Binding]` — keeps only well-formed entries for known gestures; `None`, malformed and unknown entries are dropped (meaning "use the default"); never raises.
  - `effective_bindings(defaults: Mapping[str, Binding], stored: Mapping[str, Binding]) -> dict[str, Binding]` — stored wins per gesture.
  - `stored_to_json(stored: Mapping[str, Binding]) -> dict[str, dict[str, Any]]`.
- Produces (`decker.plugin.manifest`):
  - `SUPPORTED_FORMATS = (3,)`; a manifest with `format` 1 or 2 raises `ManifestValidationError("manifest format 2 is no longer supported: Decker 0.7 needs format 3")` (format named); `_legacy_cards`, `CARDS_FORMAT` and `VALID_GESTURES` are removed.
  - `ActionSpec.scope: Literal["global", "plugin"] = "plugin"` (any other value is an error); `ActionSpec.returns: str = ""` (must be a string).
  - `CardSpec.bindings: Mapping[str, Binding]` (from `parse_manifest_bindings`, `hash=False`); `CardSpec.gestures`, `card`, `action`, `action_settings`, `behavior` keys are rejected as unknown ("format 3 cards declare bindings instead of gestures").
  - A binding whose action id starts with `"<plugin id>."` must name one of the manifest's own actions (else `ManifestValidationError`). A static card may declare `bindings` (but still no `settings`). Another plugin's action cannot be scope-checked when one manifest is parsed; a foreign plugin-scoped binding is caught when gestures are registered (Task 6, acts as Do Nothing, logged as a warning) and on save (Task 7).
- Produces (`decker.plugin.protocol`): `PROTOCOL_VERSION = 4`; `MAX_VALUE_BYTES = 1024 * 1024`; `SERVER_EVENTS` = previous set minus `press`, `double_press`, `long_press`, plus `run_action`, `call_result`, `image_result`; `PLUGIN_EVENTS` = previous set minus `set_gestures`, plus `action_result`, `call_action`, `get_image`, `switch_page`, `switch_profile`. (These are documentation constants; runtime removal of press events and `set_gestures` happens in Task 6.)
- Server behaviour in this task: `PluginManager` registers `{Gesture.PRESS}` for every card (no manifest gestures any more) and still honours `set_gestures`, so the suite stays green until Task 6.

- [ ] **Step 1: Write failing tests:** `test_bindings.py`: `test_manifest_bindings_fill_omitted_gestures_with_nothing`; `test_parse_binding_rejects_missing_action_and_non_object_args`; `test_unknown_gesture_in_manifest_bindings_is_an_error`; `test_normalize_stored_bindings_drops_null_and_garbage`; `test_effective_bindings_prefer_stored`. `test_manifest.py`: `test_format_2_manifest_is_refused_naming_the_format` (`match="format 2"`), same for format 1; `test_action_scope_defaults_to_plugin` and `test_action_scope_global`; `test_bad_scope_is_an_error`; `test_returns_is_documentation_text`; `test_card_bindings_parsed_with_defaults`; `test_card_gestures_key_is_rejected`; `test_binding_to_undeclared_own_action_is_an_error`; `test_binding_to_foreign_action_is_accepted`; `test_static_card_may_declare_bindings`. `test_protocol.py`: `PROTOCOL_VERSION == 4` and the exact new event sets. `test_version.py`: version `"0.7.0"`. `test_plugin_scaffold.py`: the scaffolded manifest loads and has `format == 3`.
- [ ] **Step 2: Run** `uv run pytest tests/plugin tests/common/test_version.py tests/cli/test_plugin_scaffold.py --no-cov` — expect FAIL.
- [ ] **Step 3: Implement** `bindings.py`, the manifest and protocol changes and the version bump; run `poetry install` (or `uv sync`) so `decker_version()` reports `0.7.0`.
- [ ] **Step 4: Migrate** every listed manifest to format 3 (built-in Blank and Core: `"format": 3`, `"version": "0.7.0"`; Core's HTTP Request gets `"scope": "global"`).
- [ ] **Step 5: Run** `uv run pytest tests --no-cov -q` (Qt offscreen) — expect PASS.
- [ ] **Step 6: Commit** `feat: manifest format 3 with action scope and card bindings, protocol 4, Decker 0.7.0` (+ footer).

---

## Phase 2 — Bindings: storage, gestures, dispatch, Do Nothing, editor

### Task 4: Binding storage with the key

**Files:**
- Modify: `src/decker/server/key_assignments.py` (`_SCHEMA`, `_COLUMNS`, `KeyAssignment`, `assign`, `copy_key`, `copy_page`, `_remap_pages`, `_to_assignment`, new `_add_bindings_column`, `update_bindings`), `src/decker/common/model.py` (`KeyState.bindings`), `src/decker/server/service.py` (`key_assign`, `key_states`)
- Test: `tests/server/test_key_assignments.py`, `tests/server/test_service.py`

**Interfaces:**
- Produces: column `bindings TEXT NOT NULL DEFAULT '{}'` (added by `_add_bindings_column` for existing databases); `KeyAssignment.bindings: dict[str, Any] = field(default_factory=dict)` (raw stored JSON: `{gesture: {"action", "args"}}`); `KeyAssignmentStore.assign(device_id, page_id, key_index, target, settings, bindings: dict[str, Any] | None = None)` (a reassign resets bindings to the given value or `{}`); `update_bindings(device_id: str, page_id: str, key_index: int, bindings: dict[str, Any]) -> bool`; `move`, `copy_key`, `copy_page` carry bindings; `copy_page` remaps page ids that are exact values inside each binding's `args` with the same `page_map` as settings.
- Produces: `KeyState.bindings: dict[str, Any] = field(default_factory=dict)` filled by `key_states`; `DeckerService.key_assign(..., settings_json: str = "{}", bindings_json: str = "{}") -> bool`.

- [ ] **Step 1: Write failing tests:** `test_bindings_round_trip_and_default_empty`; `test_update_bindings_returns_false_for_missing_slot`; `test_reassign_resets_bindings`; `test_bindings_column_added_to_an_existing_database` (create the table with the pre-change schema, then `create()`); `test_move_and_copy_key_carry_bindings`; `test_copy_page_remaps_page_ids_inside_binding_args` (`{"press": {"action": "x.y.go", "args": {"page": "p1"}}}` copied with `page_map={"p1": "p9"}` gives `"p9"`); service: `test_key_assign_stores_bindings_json` and `test_key_states_report_stored_bindings`.
- [ ] **Step 2: Run** `uv run pytest tests/server/test_key_assignments.py tests/server/test_service.py --no-cov` — expect FAIL.
- [ ] **Step 3: Implement** the interfaces.
- [ ] **Step 4: Run** the Step 2 command and `tests/server/test_server_pages.py tests/server/test_profiles_store.py` (profile clone uses `copy_page`) — expect PASS.
- [ ] **Step 5: Commit** `feat: store per-key gesture bindings with the key assignment` (+ footer).

### Task 5: `ActionCall` and the protocol 4 `ActionRunner`

**Files:**
- Create: `src/decker/server/action_calls.py`
- Modify: `src/decker/server/actions.py`, `src/decker_plugin_sdk/_runtime.py` (`_run_action` reads `payload["args"]`)
- Test: `tests/server/test_action_calls.py` (new), `tests/server/test_actions.py`

**Interfaces:**
- Produces (`decker.server.action_calls`):
  - `INPUTS: frozenset[str] = frozenset({"press", "double_press", "long_press", "plugin"})`; `MAX_DEPTH = 8`.
  - `@dataclass(frozen=True) class ActionCall: action: str; args: Mapping[str, Any]; input: str; card: Mapping[str, Any] | None = None; caller: str | None = None; chain: str = <uuid4 hex>; depth: int = 0; device: str | None = None; called_at: datetime = <now, UTC-aware>; timeout: float | None = None`.
  - `ActionCall.payload(request: str, args: Mapping[str, Any]) -> dict[str, Any]` → exactly the spec's `run_action` payload: `action`, `request`, `args`, `input`, `called_at` (ISO 8601 with offset, milliseconds), `card`, `caller`, `chain`, `depth`.
- Produces (`decker.server.actions`):
  - `ActionResult(ok: bool, error: str = "", value: Any = None)`.
  - `ArgsResolver = Callable[[ActionSpec, Mapping[str, Any], str | None], dict[str, Any]]` — (spec, validated args, device id) → delivered args.
  - `ActionRunner(supervisor, on_failure: Callable[[str, str], None], resolve: ArgsResolver, timeout: float = DEFAULT_TIMEOUT_S)`; the `builtin` parameter is kept only as the module-level `BUILTIN_ACTIONS` lookup until Task 20 removes it.
  - `ActionRunner.execute(call: ActionCall) -> ActionResult` — Do Nothing (`NOTHING_ACTION_ID`) returns `ActionResult(True)` without sending anything; unknown action → `ok=False`; args validated with `apply_schema(spec.settings, call.args, strict=True)` (on `SettingsValidationError` → `ok=False`, `error="invalid arguments: <key>: <message>; ..."`, nothing sent); then `resolve(spec, validated, call.device)`; then `run_action` with `call.payload(...)`; waits `call.timeout or self._timeout`. Failures call `on_failure(action_id, error)` as today.
  - `ActionRunner.run(call: ActionCall) -> None` — background `execute`, value discarded.
  - `ActionRunner.scope_of(action_id: str) -> tuple[str, str] | None` — `(plugin id, scope)` of an installed action.
  - `listing()` items gain `"scope"` and `"returns"`.
- Consumes: Task 1 `apply_schema(strict=True)`, `SettingsValidationError`; Task 3 `ActionSpec.scope`, `NOTHING_ACTION_ID`.

- [ ] **Step 1: Write failing tests:** `test_action_calls.py`: `test_payload_has_every_protocol_4_field` (exact key set; `called_at` parses with `datetime.fromisoformat` and is aware); `test_each_call_gets_its_own_chain`. `test_actions.py` (supervisor fake as today): `test_nothing_runs_nothing_and_succeeds` (no message sent); `test_invalid_args_fail_without_reaching_the_plugin` (error contains the field key, `on_failure` called once); `test_args_are_resolved_for_the_device_before_sending` (resolver records `(spec.id, args, "dev-1")`, the sent `args` equal its return); `test_run_action_message_carries_input_card_and_chain`; `test_listing_reports_scope_and_returns`.
- [ ] **Step 2: Run** `uv run pytest tests/server/test_action_calls.py tests/server/test_actions.py tests/sdk/test_runtime.py --no-cov` — expect FAIL.
- [ ] **Step 3: Implement**; `PluginManager` constructs the runner with `resolve=self._resolve_args`, where `_resolve_args(spec, args, device)` returns `deliver_settings(spec.settings, args, layout, variables)`; with a device it uses `self._device_layout(device)` and `self._expansion(page_variables(layout))`; with `device is None` it uses `DeviceLayout()` and `VariableChain({name: "" for name in CORE_VARIABLE_NAMES}, self.variables.view())`, so page/profile arguments arrive unset and device templates expand to `""`.
- [ ] **Step 4: Run** the Step 2 command — expect PASS.
- [ ] **Step 5: Commit** `feat: ActionCall and protocol 4 run_action with strict argument validation` (+ footer).

### Task 6: Gestures from bindings; dispatch through `ActionRunner`

**Files:**
- Modify: `src/decker/server/plugins.py` (`_Live`, `activate_card`, `dispatch_key_down/up`, `dispatch_settings_changed`, `_gesture_emitter`, `_send_will_appear`, `_compose`, `_route_context_message`, remove `_on_set_gestures` and `_gestures_from`), `src/decker/server/page_controller.py` (`_show`), `src/decker/server/service.py` (`key_assign`, `_activate_slot`), `src/decker/server/devices/__init__.py` (`Device.key_size`), `src/decker/builtin_plugins/com.decker.core/manifest.json` (add Do Nothing), new `src/decker/builtin_plugins/com.decker.core/icons/nothing.png` (an X), `tests/fixtures/plugins/com.decker.testfixture/` (manifest gains plugin-scoped `increment`/`reset` actions bound to press/long press; `plugin.py` answers `run_action` and stops sending `set_gestures`)
- Test: `tests/server/test_plugins_bindings.py` (new, uses `Env` from `tests/server/test_plugins.py`), `tests/server/test_plugins.py` (`TestCounterEndToEnd` rewritten to bindings; any test that expects a recorder plugin to receive `press`/`long_press` now asserts `run_action` or is removed), `tests/server/test_page_controller.py`

**Interfaces:**
- Consumes: Task 3 `Binding`, `effective_bindings`, `normalize_stored_bindings`; Task 4 `KeyAssignment.bindings`; Task 5 `ActionCall`, `ActionRunner.run`, `scope_of`.
- Produces (`PluginManager`):
  - `set_bindings(target: CardTarget, stored: Mapping[str, Any]) -> None` — remembers the stored bindings for the context (in memory, like `store_settings` feeds activation); on a live card it recomputes the effective bindings, re-registers gestures, sends `settings_changed` and recomposes. Called before `activate_card` by `PageController._show`, `DeckerService.key_assign` and `_activate_slot`.
  - `_Live.bindings: dict[str, Binding]` (effective), `_registered(live) -> frozenset[Gesture]` — gestures whose binding is not Do Nothing and whose action is installed and allowed (own plugin, or `scope == "global"`); the recognizer gets this set on `key_down`.
  - `_card_payload(live) -> dict[str, Any]` — `{"context", "card", "plugin", "device", "key": {"index","column","row"}, "size": [w, h], "settings": <delivered>, "last_paint": None}` (`last_paint` filled in Task 21).
  - On a gesture: `ActionRunner.run(ActionCall(action, args, input=<gesture>, card=_card_payload(live), device=target.device_id))`. Cards no longer receive `press`/`double_press`/`long_press`; `key_down`/`key_up` still go to cards with a process. Static cards feed their recognizer too.
  - `will_appear` and `settings_changed` payloads gain `"bindings": {gesture: {"action","args"}}` (effective, all three) and `will_appear` gains `"size": [w, h]`.
  - `set_gestures` from a plugin is dropped (logged at debug).
  - Blank face: for `com.decker.blank.blank` with no plugin image, no icon override and no state image, `_compose` uses the press binding's action icon; when the resolved title text is empty it uses the action's name as the title. Do Nothing gives the plain Blank face.
  - `Device.key_size -> tuple[int, int]` (`KEY_PIXEL_WIDTH`, `KEY_PIXEL_HEIGHT`); `PluginManager._key_size(target)` falls back to `(72, 72)`.

- [ ] **Step 1: Write failing tests** (`test_plugins_bindings.py`, fixture counter from `tests/fixtures/plugins/com.decker.testfixture`): `test_press_runs_the_default_bound_action_in_the_plugin` (image changes after key down/up; `received("run_action")` payload `input == "press"`, `card["context"] == "ctx-1"`); `test_cards_no_longer_receive_press_events`; `test_key_down_and_up_still_reach_the_card`; `test_nothing_gesture_is_not_registered` (`_registered(live)` excludes it); `test_press_fires_at_once_when_double_press_is_nothing` (image changes before the double-press window, 0.15 s); `test_stored_binding_overrides_default`; `test_set_bindings_on_live_card_reregisters_and_sends_settings_changed_with_bindings`; `test_binding_to_uninstalled_action_acts_as_nothing_and_survives_reinstall` (remove the plugin, gesture not registered, stored value unchanged; `add_plugin` again, press runs it); `test_untouched_gesture_follows_new_manifest_default_and_explicit_nothing_stays`; `test_foreign_plugin_scoped_binding_is_not_registered`; `test_static_card_binding_a_global_action_runs_it` (Blank bound to the counter fixture's global action); `test_invalid_stored_args_raise_action_failed_with_field_error`; `test_blank_shows_press_action_icon_and_name`; `test_will_appear_carries_bindings_and_size`; `test_set_gestures_is_ignored`. `test_page_controller.py`: `test_show_passes_stored_bindings_before_activation`.
- [ ] **Step 2: Run** `uv run pytest tests/server/test_plugins_bindings.py tests/server/test_page_controller.py --no-cov` — expect FAIL.
- [ ] **Step 3: Implement** in `plugins.py` (keep each method under 50 lines; put binding resolution in private helpers `_effective(live)`, `_allowed(live, binding)`, `_on_gesture(live, gesture)`); add Do Nothing to Core: `{"id": "com.decker.core.nothing", "name": "Do Nothing", "tooltip": "Does nothing", "icon": "icons/nothing", "scope": "global"}`; update the fixture plugin and rewrite `TestCounterEndToEnd`.
- [ ] **Step 4: Run** `uv run pytest tests/server --no-cov -q` — expect PASS.
- [ ] **Step 5: Commit** `feat: register gestures from card bindings and dispatch them as action calls` (+ footer).

### Task 7: Save RPCs validate settings and bindings

**Files:**
- Modify: `src/decker/server/service.py` (`key_update_settings`, new `key_update_bindings`, `plugins_cards_list`/`plugins_actions_list` via the manager), `src/decker/server/plugins.py` (`list_cards`, `list_actions`, new `validate_card_settings`, `validate_bindings`)
- Test: `tests/server/test_service.py`, `tests/server/test_plugins_bindings.py`

**Interfaces:**
- Produces (`PluginManager`): `validate_card_settings(target: CardTarget, settings: Mapping[str, Any]) -> dict[str, str]`; `validate_bindings(target: CardTarget, stored: Mapping[str, Any]) -> dict[str, str]` — keys `"<gesture>.<arg key>"` for argument errors and `"<gesture>"` for an action the card may not bind (foreign plugin-scoped); an uninstalled action is not an error (kept, acts as Do Nothing). `list_cards()` items gain `"bindings"` (manifest defaults, all three gestures, `Binding.to_json()`) and lose `"action"`; `list_actions()` items carry `"scope"`, `"returns"`.
- Produces (`DeckerService`):
  - `key_update_settings(device_id, page_id, key_index, settings_json) -> str` — JSON `{"ok": bool, "errors": {key: message}}`; on errors nothing is persisted.
  - `key_update_bindings(device_id: str, page_id: str, key_index: int, bindings_json: str) -> str` — same shape (error keys as above); persists with `update_bindings`, then `plugin_manager.set_bindings` when the page is active.
- Consumes: Task 1 `validate_settings`; Task 4 `update_bindings`; Task 6 `set_bindings`.

- [ ] **Step 1: Write failing tests:** `test_key_update_settings_returns_field_errors_and_does_not_persist`; `test_key_update_settings_ok_shape`; `test_key_update_bindings_persists_and_updates_live_card`; `test_key_update_bindings_rejects_bad_args_with_gesture_prefixed_keys`; `test_key_update_bindings_refuses_foreign_plugin_scoped_action`; `test_key_update_bindings_keeps_uninstalled_action`; `test_cards_list_carries_default_bindings`; `test_actions_list_carries_scope`.
- [ ] **Step 2: Run** `uv run pytest tests/server/test_service.py tests/server/test_plugins_bindings.py --no-cov` — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the Step 2 command and `uv run pytest tests/cli --no-cov` — expect PASS.
- [ ] **Step 5: Commit** `feat: settings and bindings save RPCs return field errors` (+ footer).

### Task 8: SDK cards lose gesture handlers; scaffold binds an action

**Files:**
- Delete: `src/decker_plugin_sdk/_gestures.py`
- Modify: `src/decker_plugin_sdk/_runtime.py` (remove `on_press`, `on_double_press`, `on_long_press`, `gestures`, `_send_gestures`, those `_HANDLERS` entries), `src/decker_plugin_sdk/__init__.py` (drop `Gesture`), `src/decker_plugin_sdk/_context.py` (`CardContext.bindings: dict[str, Any]`, `size: tuple[int, int] = (72, 72)`), `tests/sdk/fixtures/echo_plugin.py`, `src/decker/cli/templates/plugin/manifest.json` and `plugin.py` (a plugin-scoped action `{{plugin_id}}.hello` bound to press; until Task 14 gives it `ctx.card` it only logs)
- Test: `tests/sdk/test_runtime.py`, `tests/cli/test_plugin_scaffold.py`

**Interfaces:**
- Produces: `CardContext.bindings` updated from `will_appear`/`settings_changed` payloads; `CardContext.size` from `will_appear`'s `size`.

- [ ] **Step 1: Write failing tests:** `test_press_event_is_ignored_by_the_sdk` (no handler call, no reply); `test_will_appear_and_settings_changed_fill_bindings`; `test_will_appear_fills_size`; `test_sdk_exports_no_gesture`; delete `test_set_gestures_*`/`test_settings_changed_resends_gestures`; scaffold `test_scaffold_creates_valid_plugin` asserts the card's press binding names the scaffolded action.
- [ ] **Step 2: Run** `uv run pytest tests/sdk tests/cli/test_plugin_scaffold.py --no-cov` — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the Step 2 command — expect PASS.
- [ ] **Step 5: Commit** `feat!: SDK cards keep key down/up only and see their bindings` (+ footer).

### Task 9: Inline validation in settings forms

**Files:**
- Modify: `src/decker/client/widgets/schema_form.py` (`SchemaForm`), `src/decker/client/widgets/card_config_panel.py` (`SaveCallback`, `_on_save`, `_refresh_state`), `src/decker/client/views/dashboard.py` (`_make_save_callback`, `_save_key_settings`, `_key_call`)
- Test: `tests/client/widgets/test_schema_form.py`, `tests/client/widgets/test_card_config_panel.py`

**Interfaces:**
- Produces (`SchemaForm`): `errors() -> dict[str, str]` — `validate_settings(parse_settings_schema(schema, "form"), values())` (the client re-parses the JSON schema it was given); `set_errors(errors: Mapping[str, str]) -> None` — server errors shown until the field is edited; each field row shows its message in a `QLabel` with `role="error"` under the control; rows are flagged on load when stored values are already invalid.
- Produces (`card_config_panel`): `@dataclass(frozen=True) class SaveOutcome: ok: bool; errors: Mapping[str, str] = field(default_factory=dict)` — error keys `"settings.<key>"` / `"bindings.<gesture>.<key>"`; `SaveCallback = Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], SaveOutcome]` (settings, appearance, bindings — bindings is `{}` until Task 10); Save is disabled while any form reports errors, and a refused save routes errors back to the forms and keeps the edits.
- Consumes: Task 1 `validate_settings`, `parse_settings_schema`; Task 7 RPC result shape.

- [ ] **Step 1: Write failing tests:** `test_required_field_shows_inline_error_and_clears_when_filled`; `test_pattern_error_shows_inline`; `test_stored_invalid_value_loads_flagged`; `test_server_errors_show_on_their_fields`; `test_save_disabled_while_errors`; `test_refused_save_keeps_edits_and_shows_errors` (save callback returns `SaveOutcome(False, {"settings.url": "required"})`).
- [ ] **Step 2: Run** `QT_QPA_PLATFORM=offscreen uv run pytest tests/client/widgets/test_schema_form.py tests/client/widgets/test_card_config_panel.py --no-cov` — expect FAIL.
- [ ] **Step 3: Implement**; the dashboard parses the `key_update_settings` JSON into a `SaveOutcome`.
- [ ] **Step 4: Run** `QT_QPA_PLATFORM=offscreen uv run pytest tests/client --no-cov -q` — expect PASS.
- [ ] **Step 5: Commit** `feat: settings forms show validation errors inline and refuse Save` (+ footer).

### Task 10: Binding rows in the card editor

**Files:**
- Create: `src/decker/client/widgets/bindings_edit.py`
- Modify: `src/decker/client/widgets/action_edit.py` (`ActionEdit`), `src/decker/client/widgets/card_config_panel.py` (`show_card_config`, `_current_values`, `_on_save`), `src/decker/client/views/dashboard.py` (`_refresh_config_panel`, `_sync_slot`, save callback calls `key_update_bindings`)
- Test: `tests/client/widgets/test_bindings_edit.py` (new), `tests/client/widgets/test_card_config_panel.py`

**Interfaces:**
- Produces (`action_edit`): `ActionEdit(rpc: Callable[[], RpcCaller | None] | None = None, parent: QWidget | None = None, owner: str | None = None)` — with `owner` set, items are Do Nothing (X icon, first) then `owner`'s actions of either scope and every `scope == "global"` action (excluding Do Nothing), each `[icon] Action name` from `icon_b64`; without `owner` (the generic `action` field) it lists global actions after "(choose an action)". `set_owner(plugin_id: str) -> None`. An unknown stored id shows "(missing action)" disabled, as today.
- Produces (`bindings_edit`):
  - `@dataclass(frozen=True) class BindingsSpec: defaults: dict[str, dict[str, Any]]; stored: dict[str, dict[str, Any]]`.
  - `class BindingsEdit(QWidget)` with `changed = pyqtSignal()`; `set_bindings(spec: BindingsSpec, plugin_id: str) -> None`; `value() -> dict[str, dict[str, Any]]` — stored shape: only gestures that differ from the manifest default (a row reset to default is omitted; an explicit Do Nothing is kept); `errors() -> dict[str, str]` (`"<gesture>.<key>"`); `set_errors(errors: Mapping[str, str]) -> None`.
  - Three rows labelled Press, Double press, Long press: an `ActionEdit(owner=plugin_id)`, then the action's argument form (an `ActionParamsEdit`, whose dynamic choices use the action's own plugin id — already true at `schema_form.py:365-371`) or "This action has no settings", and a "Reset to default" button enabled only when the row differs from the default. Changing the action resets its arguments to the action's defaults.
- Produces: `CardConfigPanel.show_card_config(..., plugin_id: str = "", bindings: BindingsSpec | None = None)`; bindings join the loaded/baseline tuple so dirty tracking and "identical values re-sent leave edits alone" include them; `_on_save` passes `bindings_edit.value()` to the save callback.
- Consumes: Task 4 `KeyState.bindings`; Task 7 `list_cards()["bindings"]`, `list_actions()["scope"]`, `key_update_bindings`; Task 9 `SaveOutcome`.

- [ ] **Step 1: Write failing tests** (fake rpc listing actions of two plugins, one plugin-scoped, one global, plus Do Nothing): `test_dropdown_lists_nothing_own_actions_and_globals_only`; `test_row_shows_manifest_default_until_changed`; `test_changing_action_resets_args_to_defaults`; `test_action_without_settings_shows_hint`; `test_reset_to_default_omits_gesture_from_value`; `test_explicit_nothing_is_kept_in_value`; `test_binding_arg_errors_show_inline`; `test_missing_action_stays_selected_and_disabled`; `test_binding_arg_form_asks_the_action_plugin_for_options` (an `options_source` field in a foreign global action's schema is built with that action's plugin id, not the card's); panel: `test_bindings_edit_marks_panel_dirty_and_save_sends_bindings`.
- [ ] **Step 2: Run** `QT_QPA_PLATFORM=offscreen uv run pytest tests/client/widgets/test_bindings_edit.py tests/client/widgets/test_card_config_panel.py --no-cov` — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `QT_QPA_PLATFORM=offscreen uv run pytest tests/client --no-cov -q` — expect PASS.
- [ ] **Step 5: Commit** `feat: press, double press and long press binding rows in the card editor` (+ footer).

### Task 11: Actions tab lists global actions and drops a bound Blank

**Files:**
- Modify: `src/decker/client/widgets/sidebar.py` (`set_actions`, `_action_icon`, `_action_drag`, remove `_preset_of`), `src/decker/client/views/dashboard.py` (`_on_card_dropped`)
- Test: `tests/client/widgets/test_sidebar.py`, `tests/client/views/test_dashboard*.py` (the existing dashboard drop test file)

**Interfaces:**
- Produces: the Actions tab shows only `scope == "global"` actions other than Do Nothing, each with its own `icon_b64` (fallback `tasks.svg`); the drag payload is the Blank card with `card_name` = action name and `preset_action` = action id; the dashboard sends `key_assign(..., "{}", json.dumps({"press": {"action": id, "args": {}}}))`.

- [ ] **Step 1: Write failing tests:** `test_actions_tab_hides_plugin_scoped_actions_and_do_nothing`; `test_action_tile_uses_the_action_icon`; `test_dropping_an_action_assigns_blank_with_press_bound` (fake connection manager records the `key_assign` call's `bindings_json`).
- [ ] **Step 2: Run** `QT_QPA_PLATFORM=offscreen uv run pytest tests/client/widgets/test_sidebar.py tests/client/views --no-cov` — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the Step 2 command — expect PASS.
- [ ] **Step 5: Commit** `feat: Actions tab lists global actions and drops a Blank bound to the action` (+ footer).

### Task 12: Phase 2 end to end through a real plugin process

**Files:**
- Create: `tests/server/fixtures/actions_plugin/manifest.json`, `tests/server/fixtures/actions_plugin/plugin.py`, `tests/server/test_actions_e2e.py`

**Interfaces:**
- Produces (fixture, SDK plugin `com.decker.acttest`, format 3, `decker: ">=0.7"`, no requirements): card `com.decker.acttest.tile` (bindings: press → `com.decker.acttest.mark` with `{"tag": "p"}`, long press → `com.decker.acttest.mark` with `{"tag": "l"}`, double press omitted); actions `com.decker.acttest.mark` (plugin scope; settings `tag` text `required`, `pattern: "[a-z]+"`; appends `tag` to a `marks.log` file in the plugin dir) and `com.decker.acttest.public` (global; settings `page` of type `page`; writes the received args as JSON to `public.log`). Later phases extend this fixture.
- Produces (`test_actions_e2e.py`): an `_Env` like `tests/server/test_options_e2e.py` (copy fixture, symlink `sys.executable` as the venv python, real `PluginManager`, `FakePages`, real `FakeDeviceManager` from `tests/server/test_plugins.py`), `pytestmark` skipping `win32`.

- [ ] **Step 1: Write the tests:** `test_press_runs_bound_plugin_action_in_the_process` (key down/up → `marks.log == "p"` within 15 s); `test_unbound_double_press_does_not_delay_press` (log written before 0.35 s elapse); `test_long_press_runs_its_binding`; `test_blank_bound_to_global_action_resolves_page_args_for_the_device` (stored page id `"p1"` arrives as `{"page_id": "p1", "name": "Home", ...}`); `test_invalid_stored_args_never_reach_the_plugin_and_announce_action_failed`.
- [ ] **Step 2: Run** `uv run pytest tests/server/test_actions_e2e.py --no-cov` three times — expect PASS, not flaky.
- [ ] **Step 3: Run the phase gates** (`poe format`, the three mypy runs, `poe pylint`, `poe test`) — expect all green, coverage ≥ 85%.
- [ ] **Step 4: Commit** `test: bindings end to end through a real plugin process` (+ footer).

---

## Phase 3 — `ActionCallContext` and `CardProxy` (no canvas)

### Task 13: `CardProxy`

**Files:**
- Create: `src/decker_plugin_sdk/_proxy.py`
- Modify: `src/decker_plugin_sdk/__init__.py`
- Test: `tests/sdk/test_card_proxy.py` (new)

**Interfaces:**
- Produces (exported): `class CardProxy` — `CardProxy(payload: Mapping[str, Any], request: str, send: Sender)` built from a `run_action` `card` payload; read-only properties `context`, `card_id`, `plugin_id`, `device`, `key_index`, `column`, `row`, `size: tuple[int, int]`, `settings: Mapping[str, Any]` (a `MappingProxyType` over a copy), `last_paint: datetime | None`; methods `queue_paint() -> None` (no-op until Task 22), `show_ok()`, `show_alert()`, `switch_page(to: str = "next", wrap: bool = False)`, `go_to_page(page_id: str)`, `switch_profile(to: str = "next", wrap: bool = False)`, `go_to_profile(profile_id: int)`, and `close() -> None`. Every call sends the event with `context=<card context>` and `"request": <request>` in the payload; after `close()` calls raise `RuntimeError("card access ended with the action call")`. `CardProxy` has no `set_settings` and no `instance` attribute.
- Produces: `class OwnCardProxy(CardProxy)` — `OwnCardProxy(payload, ctx: CardContext, instance: DeckerPlugin)`: the same members, but calls go through the live `CardContext` (no `request`), plus `instance` and `set_settings(settings: dict[str, Any]) -> None`; `close()` is a no-op.

- [ ] **Step 1: Write failing tests:** `test_proxy_identity_and_physical_properties`; `test_settings_is_a_read_only_copy` (`TypeError` on item assignment; mutating the source payload afterwards does not change it); `test_navigation_carries_the_request_id` (`switch_page` message `context` and `payload["request"]`); `test_proxy_has_no_setter_or_instance`; `test_closed_proxy_refuses_calls`; `test_own_proxy_calls_go_through_the_card_context` (`set_settings` and `show_ok` sent for the context without `request`; `instance` is the given object).
- [ ] **Step 2: Run** `uv run pytest tests/sdk/test_card_proxy.py --no-cov` — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/sdk --no-cov` — expect PASS.
- [ ] **Step 5: Commit** `feat: CardProxy for the card an action was called from` (+ footer).

### Task 14: `ActionCallContext`, `Input` and the new `@action` signature

**Files:**
- Create: `src/decker_plugin_sdk/_actions.py`
- Modify: `src/decker_plugin_sdk/_runtime.py` (`_run_action`, live-context lookup for own cards), `src/decker_plugin_sdk/__init__.py`, `tests/sdk/fixtures/echo_plugin.py`, `tests/server/fixtures/actions_plugin/plugin.py`, `src/decker/cli/templates/plugin/plugin.py` (the scaffold's action calls `ctx.card.show_ok()`)
- Test: `tests/sdk/test_actions_sdk.py` (new), `tests/sdk/test_runtime.py`

**Interfaces:**
- Produces (exported from `decker_plugin_sdk`): `class Input(StrEnum): PRESS = "press"; DOUBLE_PRESS = "double_press"; LONG_PRESS = "long_press"; PLUGIN = "plugin"`; `@dataclass(frozen=True) class ActionCallContext: action: str; request: str; called_at: datetime; input: Input; card: CardProxy | None; args: dict[str, Any]; caller: str | None` plus private `_chain: str`, `_depth: int`, `_device: str | None`; `action(action_id)` registers `fn(ctx: ActionCallContext) -> Any`. An exception becomes `ok: false` with `"<Type>: <message>"` as today; an unparseable `called_at` falls back to now (UTC); an unknown `input` is answered `ok: false`.
- Produces: `card` is `None` for a `null` payload card; an `OwnCardProxy` when the card's plugin is this plugin and its context is live in this process; otherwise a `CardProxy`, closed right after the action returns.
- Consumes: Task 5 payload fields; Task 13 `CardProxy`, `OwnCardProxy`.

- [ ] **Step 1: Write failing tests:** `test_action_receives_context_with_args_input_and_called_at` (aware datetime equal to the payload's); `test_plugin_input_without_card_gives_none_card_and_caller`; `test_unknown_input_value_is_answered_with_an_error`; `test_raising_action_reports_type_and_message`; `test_own_live_card_gives_own_proxy_with_instance` (an action increments `ctx.card.instance.count` of an appeared echo card); `test_foreign_card_proxy_is_closed_after_the_action_returns` (the action stores `ctx.card`; a later call raises `RuntimeError`).
- [ ] **Step 2: Run** `uv run pytest tests/sdk --no-cov` — expect FAIL.
- [ ] **Step 3: Implement**; migrate fixtures and the scaffold template to `fn(ctx)`.
- [ ] **Step 4: Run** `uv run pytest tests/sdk tests/server/test_actions_e2e.py tests/cli --no-cov` — expect PASS.
- [ ] **Step 5: Commit** `feat: ActionCallContext and Input for SDK actions` (+ footer).

### Task 15: Server grants foreign card access while a call is in flight

**Files:**
- Modify: `src/decker/server/actions.py` (in-flight bookkeeping), `src/decker/server/plugins.py` (`_route_context_message`)
- Test: `tests/server/test_plugins_bindings.py`, `tests/server/test_actions_e2e.py`, `tests/server/fixtures/actions_plugin/` (add global action `com.decker.acttest.nav`: `ctx.card.go_to_page(ctx.args["page"]["page_id"])` then `ctx.card.show_ok()`)

**Interfaces:**
- Produces: `ActionRunner.may_touch(plugin_id: str, request: Any, context: str) -> bool` — true only while the request is pending, was sent to `plugin_id`, and its call's `card["context"] == context`; cleared on result, timeout, plugin stop and unregister.
- Produces (`PluginManager._route_context_message`): a message for a context owned by another plugin is handled only if its event is in `_FOREIGN_EVENTS = frozenset({"switch_page", "switch_profile", "show_ok", "show_alert"})` (Task 21 adds `set_image`) and `may_touch(...)` holds; `set_settings` and everything else from a non-owner is dropped and logged.

- [ ] **Step 1: Write failing tests:** unit: `test_foreign_navigation_accepted_during_the_call`; `test_foreign_navigation_without_request_is_dropped`; `test_foreign_access_ends_when_the_call_times_out` (runner timeout 0.2 s; navigation after it is dropped); `test_foreign_set_settings_is_always_refused`. e2e: `test_global_action_navigates_the_blank_card_device` (Blank bound to `nav`; press → `FakePages.activations == [("dev-1", "p1")]`).
- [ ] **Step 2: Run** `uv run pytest tests/server/test_plugins_bindings.py tests/server/test_actions_e2e.py --no-cov` — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the Step 2 command and the phase gates — expect PASS.
- [ ] **Step 5: Commit** `feat: foreign card navigation while the action call is in flight` (+ footer).

---

## Phase 4 — `call_action`, return values, chain and depth

### Task 16: Server side of `call_action` / `call_result`

**Files:**
- Modify: `src/decker/server/actions.py`, `src/decker/server/plugins.py` (`_route_message` routes `call_action`)
- Test: `tests/server/test_actions.py`, `tests/server/test_plugins_bindings.py`

**Interfaces:**
- Produces:
  - `ActionRunner.on_result` stores `payload["value"]` in `ActionResult.value` when `ok`; a value whose `json.dumps(...).encode()` exceeds `MAX_VALUE_BYTES` turns the result into `ok=False, error="return value larger than 1 MiB"`.
  - `ActionRunner.on_plugin_call(plugin_id: str, payload: Mapping[str, Any]) -> None` — handled on its own thread; replies exactly once with `Message("call_result", None, {"request", "ok", "value" | "error"})` to `plugin_id`. Refusals: unknown action; foreign `scope == "plugin"` action ("action <id> is not callable from <plugin>"); depth > `MAX_DEPTH` ("call depth limit (8) exceeded"); invalid args (strict validation, same message format as Task 5). The call is `ActionCall(input="plugin", card=None, caller=plugin_id, chain=<payload chain or new>, depth=<derived>, device=<payload device, else the device recorded for that chain>, timeout=<payload timeout or default>)`.
  - Depth is derived by the server: if the payload's `chain` matches a call currently pending at `plugin_id`, depth = that call's depth + 1, else `max(0, payload depth)`. The runner records `chain → device` while any call of that chain is pending.
- Consumes: Task 5 `ActionCall`, `scope_of`.

- [ ] **Step 1: Write failing tests:** `test_call_result_returns_the_callee_value`; `test_own_plugin_scoped_action_is_callable`; `test_foreign_plugin_scoped_action_is_refused`; `test_global_action_of_another_plugin_is_callable`; `test_depth_9_is_refused`; `test_chain_device_is_inherited_by_nested_calls`; `test_explicit_device_wins`; `test_oversized_value_is_an_error`; `test_gesture_run_discards_the_value_and_failure_still_announces`.
- [ ] **Step 2: Run** `uv run pytest tests/server/test_actions.py tests/server/test_plugins_bindings.py --no-cov` — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the Step 2 command — expect PASS.
- [ ] **Step 5: Commit** `feat: plugins call actions through the server and get values back` (+ footer).

### Task 17: SDK `call_action`, return values and the elastic worker pool

**Files:**
- Create: `src/decker_plugin_sdk/_pool.py`
- Modify: `src/decker_plugin_sdk/_actions.py`, `src/decker_plugin_sdk/_runtime.py` (`call_result` handling, actions on the elastic pool, value in `action_result`), `src/decker_plugin_sdk/__init__.py`
- Test: `tests/sdk/test_call_action_sdk.py` (new)

**Interfaces:**
- Produces (exported): `class ActionError(Exception)`; `call_action(action_id: str, args: dict[str, Any] | None = None, timeout: float = 30.0, device: str | None = None) -> Any` — sends `call_action {"request", "action", "args", "timeout", "chain"?, "depth"?, "device"?}` and blocks for `call_result` (local wait `timeout + 1` s), raising `ActionError(error)` on `ok: false` or timeout; `async def call_action_async(action_id, args=None, timeout=30.0, device=None) -> Any` (runs `call_action` via `asyncio.to_thread`, so the context variable carries the chain).
- Produces: a `contextvars.ContextVar` holding the current call frame (`chain`, `depth`, `device`), set while an action runs; inside an action `call_action` sends that chain and `depth + 1`; outside it sends neither (new chain at depth 0).
- Produces: action return value goes into `action_result.value`; a value that is not JSON-encodable or exceeds `MAX_VALUE_BYTES` (SDK mirror constant, "keep in sync" comment) is answered `ok: false` with a clear error.
- Produces (`_pool.py`): `class ElasticPool` with `submit(fn: Callable[..., Any], *args: Any) -> None`, `blocking() -> ContextManager[None]` (while a worker waits inside it the pool may run one extra thread), `shutdown() -> None`; base size `WORKERS = 4`. Actions run on it; `call_action` wraps its wait in `blocking()`.

- [ ] **Step 1: Write failing tests** (pipe harness from `tests/sdk/test_runtime.py`, acting as the server): `test_action_return_value_is_sent_in_action_result`; `test_unencodable_return_value_is_an_error`; `test_call_action_returns_call_result_value`; `test_call_action_raises_action_error_on_refusal`; `test_call_action_times_out`; `test_nested_call_inside_action_sends_chain_and_depth_plus_one`; `test_call_from_main_thread_starts_new_chain_without_device` (no `chain`/`depth`/`device` keys sent); `test_call_action_async_from_coroutine`; `test_nested_calls_deeper_than_pool_do_not_deadlock` (harness answers each `call_action` by sending a `run_action` back to the same process, six levels deep, with `WORKERS = 4`; all six answer within 5 s).
- [ ] **Step 2: Run** `uv run pytest tests/sdk/test_call_action_sdk.py --no-cov` — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/sdk --no-cov` and `uv run pytest tests/sdk/test_runtime.py::test_sdk_does_not_import_decker --no-cov` — expect PASS.
- [ ] **Step 5: Commit** `feat: SDK call_action, call_action_async and action return values` (+ footer).

### Task 18: Phase 4 end to end

**Files:**
- Modify: `tests/server/fixtures/actions_plugin/` (actions `com.decker.acttest.double` (plugin scope, returns `ctx.args["n"] * 2`), `com.decker.acttest.chain` (global; calls `double` with `n=21` and writes the value to `chain.log`), `com.decker.acttest.recurse` (plugin scope; calls itself with `n+1`, writes the last `n` reached and the error text)), `tests/server/test_actions_e2e.py`

- [ ] **Step 1: Write the tests:** `test_action_calls_own_plugin_action_and_gets_value` (`chain.log == "42"`); `test_recursion_stops_at_depth_8_with_an_error` (`n == 8`, error mentions the limit); `test_call_inside_a_gesture_chain_resolves_page_args_for_the_card_device`.
- [ ] **Step 2: Run** the e2e file three times — expect PASS.
- [ ] **Step 3: Run the phase gates** — expect green.
- [ ] **Step 4: Commit** `test: action calls, values and depth limit end to end` (+ footer).

---

## Phase 5 — Core as a built-in out-of-process plugin

### Task 19: Built-in plugins run with the server's interpreter

**Files:**
- Modify: `src/decker/plugin/loader.py` (`InstalledPlugin.builtin`, `scan_builtin`), `src/decker/plugin/runtime/launcher.py` (`launch_command`), `src/decker/server/plugins.py` (`_register_runtime`)
- Test: `tests/plugin/test_loader.py`, `tests/plugin/runtime/test_launcher.py` (existing or new), `tests/server/test_builtin_plugins.py`

**Interfaces:**
- Produces: `InstalledPlugin.builtin: bool = False`, set by `PluginLoader.scan_builtin()`; `launch_command(plugin_dir: Path, runtime: RuntimeSpec, builtin: bool = False) -> list[str]` — a built-in python runtime returns `[sys.executable, "-m", "decker_plugin_sdk", str(plugin_dir / runtime.entry)]` (no venv); `PluginManager` passes `plugin.builtin` and uses `launch_env(self._sdk_path)` as for any plugin. Core still has no `runtime` in this task, so its HTTP Request keeps the in-server path until Task 20.

- [ ] **Step 1: Write failing tests:** `test_scan_builtin_marks_plugins_builtin`; `test_builtin_python_runtime_uses_the_server_interpreter`; `test_user_python_runtime_still_uses_the_venv`; `test_builtin_plugin_starts_without_a_venv` (copy `tests/server/fixtures/actions_plugin` into a tmp folder, register it as `InstalledPlugin(manifest, dir, builtin=True)` via `add_plugin`, run one of its actions, assert it ran and no `.venv` exists).
- [ ] **Step 2: Run** `uv run pytest tests/plugin tests/server/test_builtin_plugins.py --no-cov` — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the Step 2 command — expect PASS.
- [ ] **Step 5: Commit** `feat: built-in plugins with a runtime run on the server's interpreter` (+ footer).

### Task 20: HTTP Request moves into Core's process

**Files:**
- Create: `src/decker/builtin_plugins/com.decker.core/plugin.py`
- Delete: `src/decker/server/builtin_actions.py`
- Modify: `src/decker/builtin_plugins/com.decker.core/manifest.json` (`"runtime": {"type": "python", "entry": "plugin.py"}`, `"decker": ">=0.7"`, HTTP Request `"returns": "{\"status\": int, \"body\": str}: the HTTP status and up to 512 KiB of the response body"`), `src/decker/server/actions.py` (remove the built-in executor path and `BUILTIN_ACTIONS`), `src/decker/plugin/manifest.py` (actions need a runtime)
- Test: `tests/server/test_core_plugin.py` (new; moves the HTTP tests out of `tests/server/test_actions.py`, loading `plugin.py` with `importlib.util.spec_from_file_location` and driving it through the SDK pipe harness), `tests/server/test_actions_e2e.py`, `tests/plugin/test_manifest.py`

**Interfaces:**
- Produces: `@action("com.decker.core.httprequest") def http_request(ctx: ActionCallContext) -> dict[str, Any]` — same URL-scheme check, method, body, content type and timeout as `builtin_actions.http_request`; status ≥ 400 or a network error raises `ActionError` with the same messages (`"HTTP 404"`, the URL error reason, `"the URL must start with http:// or https://"`); success returns `{"status": int, "body": str}` (body decoded UTF-8 with replacement, truncated to 512 KiB).
- Produces: a manifest with `actions` and no `runtime` raises `ManifestValidationError("manifest contributes actions but declares no runtime")` (lands here because Core gains its runtime in the same commit).

- [ ] **Step 1: Write failing tests:** `test_actions_without_runtime_are_a_manifest_error`; `test_post_sends_the_body_and_returns_status`; `test_get_returns_body`; `test_error_status_raises_action_error`; `test_only_http_urls_are_allowed`; `test_unreachable_host_is_a_failure`; e2e: `test_blank_bound_to_http_request_hits_the_server_through_the_core_process` (local `http.server`, real Core process via `sys.executable`; also asserts `supervisor.has_process("com.decker.core")`); `test_core_is_started_lazily` (no process before the first call).
- [ ] **Step 2: Run** `uv run pytest tests/server/test_core_plugin.py tests/server/test_actions_e2e.py --no-cov` — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the phase gates — expect green.
- [ ] **Step 5: Commit** `feat: HTTP Request runs in the built-in Core plugin process` (+ footer).

---

## Phase 6 — Canvas, frame tick, `get_image`, `last_paint`, `on_paint`

### Task 21: Server: `last_paint`, `get_image`, foreign `set_image`

**Files:**
- Modify: `src/decker/server/plugins.py` (`_Live.last_paint`, `_on_set_image`, `_card_payload`, `_route_message` for `get_image`, `_FOREIGN_EVENTS`)
- Test: `tests/server/test_plugins_paint.py` (new)

**Interfaces:**
- Produces: `_Live.last_paint: datetime | None` set (UTC) whenever a `set_image` is accepted for the key, from any plugin; `_card_payload` reports it as ISO text or `null`.
- Produces: `get_image {"request": <reply id>, "context": <ctx>, "call"?: <in-flight run_action request id>}` (plugin → server) answered with `image_result {"request": <reply id>, "image": <base64 PNG> | null, "format": "png"}`: the key's current plugin image layer (plugin image or pack icon, not background, title or the user's icon override) rasterised to the key's pixel size with `AssetRenderer`. Own contexts need no `call`; a foreign context needs `call` with `may_touch(plugin_id, call, context)` true; anything else answers `image: null`. (`call` is an addition to the spec's `{request, context}`: the reply id cannot double as the grant.)
- Produces: `set_image` added to `_FOREIGN_EVENTS`.

- [ ] **Step 1: Write failing tests:** `test_last_paint_recorded_on_set_image`; `test_run_action_card_payload_carries_last_paint`; `test_get_image_returns_own_plugin_layer_as_png_at_key_size`; `test_get_image_excludes_background_and_title`; `test_get_image_for_foreign_context_needs_an_in_flight_call`; `test_foreign_set_image_accepted_during_call`; `test_late_foreign_set_image_after_result_is_dropped`.
- [ ] **Step 2: Run** `uv run pytest tests/server/test_plugins_paint.py --no-cov` — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the Step 2 command — expect PASS.
- [ ] **Step 5: Commit** `feat: last_paint, get_image and foreign set_image during a call` (+ footer).

### Task 22: SDK canvas and frame tick

**Files:**
- Create: `src/decker_plugin_sdk/_canvas.py`
- Modify: `src/decker_plugin_sdk/_context.py` (`CardContext.canvas`, `queue_paint`, `last_paint`), `src/decker_plugin_sdk/_proxy.py` (`canvas`, `queue_paint`), `src/decker_plugin_sdk/_runtime.py` (`DeckerPlugin.on_paint`, paint items on the card queue, frame ticker start/stop, `image_result` handling, flush foreign snapshots before `action_result`)
- Test: `tests/sdk/test_canvas.py` (new)

**Interfaces:**
- Produces:
  - `class Canvas` with `paint() -> ContextManager[PIL.Image.Image]` (RGBA, the key's pixel size). A counting lock lets several painters hold it at once; when the last one leaves, the buffer is copied into the snapshot and the key is marked dirty; leaving `paint()` counts as `queue_paint()`. Pillow is imported inside `paint()`; without it `RuntimeError("the canvas needs Pillow: add pillow to your plugin's requirements")`.
  - `class FrameTicker(send: Sender, interval: float = 1 / 30)` — one daemon thread per process; each tick sends every dirty key's latest snapshot once as `set_image` PNG (foreign keys add the call's `request`); never reads the live buffer and never waits for a painter; no dirty keys, nothing sent.
  - Own cards: the buffer starts from the last frame this process sent for the key (or transparent). Foreign cards: the first `canvas` access sends `get_image` and blocks up to 5 s for `image_result` (transparent on failure).
  - `DeckerPlugin.on_paint(self, ctx: CardContext, img: Image) -> None`; `queue_paint()` on an own card enqueues one coalesced paint item on that context's queue, which runs `on_paint` inside `canvas.paint()`.
  - `CardContext.last_paint: datetime | None` — the time this process last sent a frame for the context (an own `CardProxy` built in a call prefers the payload's `last_paint`).
  - Before `action_result` is sent, dirty foreign snapshots are flushed immediately; foreign buffers are dropped when the call ends.
- Consumes: Task 13 proxies, Task 14 call frames, Task 21 `get_image`/`image_result`.

- [ ] **Step 1: Write failing tests:** `test_snapshot_taken_only_when_last_painter_releases` (two overlapping `paint()`; ticker sends after the second leaves); `test_half_drawn_frame_is_never_sent` (a painter sleeping mid-draw while ticks run: no `set_image` until it leaves); `test_ticker_sends_each_dirty_key_once`; `test_no_dirty_keys_sends_nothing`; `test_queue_paint_coalesces_into_one_on_paint`; `test_own_buffer_starts_from_last_sent_frame`; `test_foreign_canvas_fetches_current_image_first`; `test_foreign_paint_inside_action_lands_before_action_result` (message order on the pipe); `test_canvas_without_pillow_raises_clear_runtime_error` (monkeypatch `sys.modules["PIL"] = None`); `test_sdk_import_does_not_import_pil`.
- [ ] **Step 2: Run** `uv run pytest tests/sdk/test_canvas.py --no-cov` — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/sdk --no-cov` — expect PASS.
- [ ] **Step 5: Commit** `feat: double-buffered SDK canvas with a 30 fps frame tick and on_paint` (+ footer).

### Task 23: Phase 6 end to end

**Files:**
- Modify: `tests/server/fixtures/actions_plugin/` (card `tile` gains `on_paint` filling red; global action `com.decker.acttest.paint` fills the calling card's canvas blue; manifest `requirements` stay empty — the fixture venv is the test interpreter, which has Pillow), `tests/server/test_actions_e2e.py`

- [ ] **Step 1: Write the tests:** `test_queue_paint_lands_on_the_key` (key image red pixel within 15 s, `last_paint` set); `test_global_action_paints_a_foreign_blank_card_before_its_result` (Blank bound to `paint`: key pixel blue after the press; plugin image layer only — the user's background colour still shows at the edge when the action paints a centred square); `test_user_icon_override_still_wins_over_canvas_frames`.
- [ ] **Step 2: Run** the e2e file three times — expect PASS.
- [ ] **Step 3: Run the phase gates** — expect green.
- [ ] **Step 4: Commit** `test: canvas painting end to end` (+ footer).

---

## Phase 7 — Migration, docs, pin

### Task 24: Decker docs, CLAUDE.md and README

**Files:**
- Modify: `docs/plugins/README.md`, `docs/plugins/package-and-manifest.md` (format 3, `scope`, `returns`, `bindings`, removal of `gestures`/presets/`behavior`, runtime required for actions, built-in plugin runtime), `docs/plugins/settings-schema.md` (`required`, `pattern`, `min_length`, `max_length`, strict save validation, removal of `slots`, dynamic choices in argument forms), `docs/plugins/protocol.md` (protocol 4: `run_action` payload, `action_result.value`, `call_action`/`call_result`, `get_image`/`image_result`, foreign access rules, removal of gesture events and `set_gestures`, flood limits unchanged), `docs/plugins/sdk-quickstart.md` (`ActionCallContext`, `Input`, `CardProxy`, `call_action`, `call_action_async`, `ActionError`, canvas and `on_paint`, removed handlers), `docs/plugins/limits.md` (1 MiB values, depth 8, 30 s, frame tick, Pillow for the canvas), `CLAUDE.md` (Plugins paragraph: bindings, Do Nothing, Core process, removal of Run Action/Switch/slots, new RPCs `key_update_bindings` and the JSON result of `key_update_settings`), `README.md` (feature table: drop the Switch card row, add bindings, action calls, canvas)
- Test: `tests/plugin/test_docs_examples.py` only if one exists; otherwise verification is by running the quickstart example

- [ ] **Step 1: Update the docs.** Every manifest snippet is format 3 and loads with `PluginManifest.from_dict`.
- [ ] **Step 2: Verify** by extracting each JSON manifest snippet from `docs/plugins/*.md` into a scratch script (outside the repo) that calls `PluginManifest.from_dict` on it, and by running the quickstart's action example through the SDK pipe harness — expect no errors; report the result.
- [ ] **Step 3: Run** `grep -rn "Run Action\|runaction\|slots\|set_gestures\|on_press\|format\": 2" docs/plugins CLAUDE.md README.md` — expect no hits other than sentences that say these were removed in format 3 / protocol 4.
- [ ] **Step 4: Commit** `docs: plugin actions revamp in the plugin docs, CLAUDE.md and README` (+ footer).

### Task 25: decker-plugins branch and Counter

Work in `/work/decker/decker-plugins`.

**Files:**
- Modify: `plugins/com.decker.counter/manifest.json`, `plugins/com.decker.counter/plugin.py`, `plugins/com.decker.counter/tests/test_counter.py`

**Interfaces:**
- Produces (Counter `2.0.0`, `format: 3`, `decker: ">=0.7"`, `requirements: ["pillow>=12"]`): plugin-scoped actions `com.decker.counter.increment` (settings `step` int, default 1, min 1, max 9999), `com.decker.counter.decrement` (settings `step` int, default 1, min 1, max 9999), `com.decker.counter.reset` (no settings); card `com.decker.counter.counter` bindings press → increment `{"step": 1}`, double press → decrement `{"step": 1}`, long press → reset; card settings keep only `max`. The card keeps its count on the `@card` instance; actions change it through `ctx.card.instance` and call `ctx.card.queue_paint()`; `on_paint` draws the count with the existing `render` geometry into the canvas (the old `step`/`double_step` settings and `gestures()` are removed; a called action with no card raises `ActionError("Counter actions need a card")`).

- [ ] **Step 1: Branch and point at the Decker branch for development:** `git -C /work/decker/decker-plugins switch -c feat/plugin-actions-revamp main`, then `uv pip install -e /work/decker/decker` into the decker-plugins environment (not committed; the lock is fixed in Task 28).
- [ ] **Step 2: Write failing tests** (rewrite the harness to send `run_action` with a `card` payload for an appeared context and to collect `set_image` frames): `test_manifest_is_format_3_and_needs_decker_0_7`; `test_default_bindings_press_double_long`; `test_increment_adds_step_and_repaints`; `test_decrement_subtracts_step`; `test_reset_returns_to_zero`; `test_max_wraps_to_zero`; `test_action_without_card_fails_cleanly`; `test_count_rendering_is_unchanged` (pixel comparison with `render(n)` at the same size).
- [ ] **Step 3: Run** `uv run pytest plugins/com.decker.counter` — expect FAIL.
- [ ] **Step 4: Implement**; bump `version` to `2.0.0`.
- [ ] **Step 5: Run** `uv run pytest plugins/com.decker.counter` and `uv run ruff check .` — expect PASS.
- [ ] **Step 6: Commit** `feat!: Counter uses plugin actions bound to press, double press and long press` (+ footer).

### Task 26: Pages

**Files:**
- Modify: `plugins/com.decker.pages/manifest.json`, `plugin.py`, `tests/_pages_harness.py`, `tests/test_arrows.py`, `tests/test_goto.py`, `tests/test_manifest.py`

**Interfaces:**
- Produces (Pages `2.0.0`, format 3, `decker: ">=0.7"`): global actions `com.decker.pages.next` and `com.decker.pages.previous` (settings `wrap` bool default false), `com.decker.pages.goto` (settings `profile` profile, `page` page with `profile_field: "profile"`), each using `ctx.card.switch_page(...)`/`go_to_page(...)` and raising `ActionError("needs a card")` when `ctx.card is None`. Card ids are unchanged; the Next/Previous cards keep only the `color` setting and bind press to their action (`{"wrap": false}`); the Go To card has no settings, binds press to `goto`, and titles itself `"Go To <name>"` from its press binding's resolved `page` argument (read from `ctx.bindings["press"]["args"]["page"]` on `will_appear`/`settings_changed`; empty title when the press binding is not `goto` or names no page).

- [ ] **Step 1: Write failing tests:** `test_manifest_is_format_3_with_global_actions`; `test_next_action_switches_the_card_device_page_with_wrap`; `test_previous_action`; `test_goto_action_goes_to_the_page`; `test_actions_without_a_card_fail`; `test_goto_card_title_follows_press_binding_page`; `test_goto_card_title_empty_when_press_rebound`; keep the caret rendering tests.
- [ ] **Step 2: Run** `uv run pytest plugins/com.decker.pages` — expect FAIL.
- [ ] **Step 3: Implement**; bump version to `2.0.0`.
- [ ] **Step 4: Run** `uv run pytest plugins/com.decker.pages` and `uv run ruff check .` — expect PASS.
- [ ] **Step 5: Commit** `feat!: Pages navigation as global actions bound by its cards` (+ footer).

### Task 27: Profiles

**Files:**
- Modify: `plugins/com.decker.profiles/manifest.json`, `plugin.py`, `tests/_profiles_harness.py`, `tests/test_arrows.py`, `tests/test_goto.py`, `tests/test_manifest.py`

**Interfaces:**
- Produces (Profiles `2.0.0`, same pattern as Task 26): global actions `com.decker.profiles.next`/`previous` (`wrap`), `com.decker.profiles.goto` (`profile`); cards keep their ids; Go To shows the press binding's profile icon and name (from `ctx.bindings["press"]["args"]["profile"]`), cleared when rebound.

- [ ] **Step 1: Write failing tests** mirroring Task 26 (`test_next_action_switches_profile_with_wrap`, `test_goto_action_goes_to_the_profile`, `test_actions_without_a_card_fail`, `test_goto_card_shows_press_binding_profile_icon_and_name`, `test_goto_card_clears_when_press_rebound`, `test_manifest_is_format_3_with_global_actions`).
- [ ] **Step 2: Run** `uv run pytest plugins/com.decker.profiles` — expect FAIL.
- [ ] **Step 3: Implement**; bump version to `2.0.0`.
- [ ] **Step 4: Run** `uv run pytest plugins/com.decker.profiles` and `uv run ruff check .` — expect PASS.
- [ ] **Step 5: Commit** `feat!: Profiles navigation as global actions bound by its cards` (+ footer).

### Task 28: Fontawesome, tooling, docs and the Decker pin

**Files:**
- Modify: `plugins/com.decker.fontawesome/manifest.json` (`format: 3`, `decker: ">=0.7"`, version bump required by `tools/check_versions.py` — see the open question in the hand-off report), `tests/tools/conftest.py` (`write_plugin` writes `format: 3`), `README.md` (plugin table, Decker 0.7 requirement), `docs/platform-support.md` if it names manifest formats, `CLAUDE.md` if present, `pyproject.toml` (`decker @ git+https://github.com/valsr/decker.git@<final Decker commit>`), `uv.lock`

- [ ] **Step 1: Migrate** Fontawesome and the tools fixture; update README/docs.
- [ ] **Step 2: Run** `uv run pytest` and `uv run ruff check .` and `uv run python -m tools.pack_all --out "$(mktemp -d)"` — expect PASS and four packages.
- [ ] **Step 3: Finish Decker:** in `/work/decker/decker` run the full gates once more on the branch head and note `git rev-parse HEAD`. The commit must be on GitHub for `uv lock` to resolve it: ask the user before pushing `feat/plugin-actions-revamp` (push over HTTPS with the gh credential helper).
- [ ] **Step 4: Pin:** set the `decker @ git+https://github.com/valsr/decker.git@<that commit>` dependency, `uv lock`, `uv sync --locked`, then `uv run pytest` — expect PASS against the pinned Decker.
- [ ] **Step 5: Commit** `chore: Fontawesome format 3 and pin Decker to the plugin actions revamp commit` (+ footer).

---

## Self-review notes

- **`required` and defaults:** the spec lists "missing" as an error and also keeps `default` unchanged. This plan checks `required` after the default is filled in, so a required field with a non-empty default never fails on a missing key (e.g. `call_action(..., {})`). If the user wants the literal reading, Task 1's `test_required_with_nonempty_default_accepts_missing_key` flips.
- **Do Nothing on the Actions tab:** hidden there (dropping it would make an empty Blank); it heads every binding dropdown.
- **`get_image` format:** always PNG, rasterised server-side, so SVG plugin images can seed a Pillow buffer.
- **HTTP Request value:** `{"status", "body"}`; the spec leaves return values to each action.
- **Phase boundaries moved by the code:** Run Action/Switch retirement is Task 2 (Phase 1) because the format 3 parser cannot load them; Do Nothing joins Core in Task 6; the "actions need a runtime" rule lands with Core's runtime in Task 20.
