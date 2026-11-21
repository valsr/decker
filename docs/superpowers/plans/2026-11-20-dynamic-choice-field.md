# Dynamic Choice Field Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `choice` setting can name an `options_source`; the plugin supplies its options at runtime through `@options`, and the client shows them in an editable dropdown that never loses a saved value.

**Architecture:** The schema keeps `options_source` and accepts any string for such a field. The server's `OptionsRunner` asks the plugin (`get_options` / `options_result`, 5 s timeout, starts a lazy plugin for the request) and cleans the answer; a `plugin_options` RPC returns it. The SDK gains `@options`. The client's `DynamicChoiceEdit` loads in a worker thread and fills an editable combo through a Qt signal.

**Tech Stack:** Python 3.14, PyQt6, RPyC, pytest + assertpy, black (120), mypy, pylint.

**Spec:** `docs/superpowers/specs/2026-11-20-dynamic-choice-field-design.md`

## Global Constraints

- Quality gates before done: `poe format`, `poe mypy` **and** `uv run mypy --platform win32 .` **and** `uv run mypy --platform darwin .` (CI runs all three; never use `signal.SIGKILL`/POSIX-only APIs, use `psutil`), `poe pylint` (>= 9.0), `poe test` (coverage >= 85%). Do not run a loop with an unquoted `$var` of flags: the shell is zsh and does not split it; write each mypy command out.
- Annotate everything; methods under 50 lines; max 5 args (self counts); docstrings on public items only, one short line. Tests: pytest + assertpy, real components over mocks, `-> None`, unique basenames across all test directories.
- `options_source` names: `[a-z][a-z0-9_]*`, at most 64 characters, scoped to the declaring plugin; a plugin is only asked for sources its own schemas declare.
- Option values and labels are strings, at most 256 characters; at most 256 options; repeated values dropped; static `options` first, then dynamic.
- With `options_source`, the stored value is any string up to 256 characters; a value not in the list is never replaced.
- The GUI thread never waits on a plugin; the server waits at most 5 s.
- Protocol is additive: `PROTOCOL_VERSION` and `hello.protocol` become 3. Pure Python/Qt, all five platform tiers. End-to-end real-process tests skip win32 explicitly.
- Update `docs/plugins/` (`settings-schema.md`, `protocol.md`, `sdk-quickstart.md`, `limits.md`), `CLAUDE.md`, `README.md` in the same change.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

- A saved value that the plugin no longer lists stays in the stored settings and in the control; it is never replaced by the first option.
- A plugin answering with garbage (not a list, entries missing keys, non-strings, 10 000 options, duplicate values, huge strings) yields a clean capped list and no exception.
- A slow or dead plugin: `plugin_options` returns an error within about 5 s, the GUI thread is never blocked, and a pending fetch is released when the plugin stops or on `destroy`.
- Cross-plugin isolation: unknown plugin ids and undeclared sources are refused; an `options_result` with an unknown or another plugin's request id is ignored.
- A form rebuilt while a fetch is in flight: the late result must not touch the old (deleted) widgets or the new form.

---

### Task 1: Option helpers and the `options_source` schema

**Files:**
- Create: `src/decker/common/options.py`
- Modify: `src/decker/plugin/settings_schema.py` (`_EXTRA_KEYS`, `_parse_options`, `_parse_extra`, `_parse_default`, `_coerce`, `_field_to_json`)
- Test: `tests/common/test_option_helpers.py`, `tests/plugin/test_settings_schema.py`

**Interfaces:**
- Produces (`decker.common.options`): `MAX_OPTIONS = 256`, `MAX_TEXT = 256`, `MAX_SOURCE_LENGTH = 64`; `is_valid_source(name: str) -> bool`; `clean_options(raw: Any) -> list[dict[str, str]]` — a non-list gives `[]`; entries that are not dicts with `str` `label` and `str` `value` are dropped; label and value are cut to `MAX_TEXT`; later entries repeating a value are dropped; at most `MAX_OPTIONS` entries are kept.
- Schema: a `choice` field accepts `options_source` (a valid source name, else `ValueError`); `options` is required and non-empty only when there is no source (with a source it may be absent, giving an empty tuple); the default is the field's string `default`, else the first static option's value, else `""`; `_coerce` for a field with a source accepts any `str` of at most `MAX_TEXT` characters and nothing else; `_field_to_json` exports `options_source` (and `options`, possibly `[]`).

- [ ] **Step 1: Write failing tests.** `test_option_helpers.py`: `test_valid_sources` (`output_devices`, `a1` valid; `Out`, `1a`, ``, a 65-character name invalid); `test_clean_options_keeps_valid_entries_in_order`; `test_clean_options_drops_malformed_entries` (missing label, non-string value, a bare string, `None`); `test_clean_options_cuts_long_text_to_256`; `test_clean_options_drops_repeated_values_keeping_the_first`; `test_clean_options_caps_at_256`; `test_clean_options_of_a_non_list_is_empty`. `test_settings_schema.py`: `test_choice_with_options_source_and_no_options_parses`; `test_choice_without_options_or_source_still_rejected`; `test_invalid_options_source_rejected` (`Bad Name`, a number); `test_options_source_choice_accepts_any_string_and_keeps_it` (via `apply_schema`, value `"usb_headset"` not in static options survives; a 257-character string and an int fall back to the default); `test_options_source_choice_default_rules` (explicit default; else first static option; else `""`); `test_options_source_is_exported_in_json`.
- [ ] **Step 2: Run** `uv run pytest tests/common/test_option_helpers.py tests/plugin/test_settings_schema.py --no-cov` — expect FAIL.
- [ ] **Step 3: Implement** `decker/common/options.py` and the schema changes (static-option validation stays as is when there is no source).
- [ ] **Step 4: Run** the same command — expect PASS.
- [ ] **Step 5: Commit** `feat: options_source on choice fields and option helpers`.

### Task 2: `OptionsRunner`

**Files:**
- Create: `src/decker/server/options_runner.py`
- Test: `tests/server/test_options_runner.py` (follow the supervisor/FakeProcess rig of `tests/server/test_variable_runner.py`)

**Interfaces:**
- Consumes: `clean_options` (Task 1), `PluginSupervisor` (`acquire`, `release`, `send`, `is_failed`).
- Produces:
  - `@dataclass(frozen=True) class OptionsResult: ok: bool; options: tuple[dict[str, str], ...] = (); error: str = ""`
  - `class OptionsRunner: __init__(self, supervisor: PluginSupervisor, timeout: float = 5.0)`
    - `fetch(plugin_id: str, source: str) -> OptionsResult` — blocking up to `timeout`; if the plugin is failed returns `OptionsResult(False, error="the plugin has failed")` at once; otherwise registers a pending request, `supervisor.acquire`s the plugin (so a lazy plugin starts), sends `get_options` `{"request": <id>, "source": source}`, waits, and always `release`s and removes the pending request in a `finally`; a timeout gives `error="timed out"`; the plugin's `ok: false` gives its `error` text (cut to 256 characters); an `ok: true` answer gives `clean_options(payload["options"])`.
    - `on_result(plugin_id: str, payload: Mapping[str, Any]) -> None` — ignores a payload whose request id is unknown, not a string, or belongs to a different plugin; the first answer wins.
    - `plugin_stopped(plugin_id: str) -> None` — wakes and fails that plugin's pending fetches (`error="the plugin stopped"`).
    - `stop_all() -> None` — wakes every pending fetch; later `fetch` calls return `OptionsResult(False, error="the server is stopping")`.

- [ ] **Step 1: Write failing tests:** `test_fetch_returns_cleaned_options`; `test_fetch_sends_get_options_with_request_and_source`; `test_ok_false_returns_the_error`; `test_timeout_returns_timed_out_and_releases_the_plugin` (short timeout; assert the supervisor refcount returns to 0); `test_failed_plugin_returns_at_once`; `test_garbage_options_are_cleaned_not_raised` (non-list, dicts with ints, 10 000 entries, duplicates, huge strings); `test_result_from_another_plugin_or_unknown_request_is_ignored`; `test_duplicate_answer_is_ignored_first_wins`; `test_plugin_stopped_wakes_pending_fetches`; `test_stop_all_wakes_pending_and_refuses_new`; `test_pending_map_is_empty_after_every_outcome`.
- [ ] **Step 2: Run** `uv run pytest tests/server/test_options_runner.py --no-cov` — expect FAIL.
- [ ] **Step 3: Implement** `OptionsRunner` (a lock and a dict of pending entries with an `Event`; no callback or supervisor call under the lock that a callback could re-enter).
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `feat: OptionsRunner asks a plugin for the options of a source`.

### Task 3: PluginManager routing, the RPC and protocol 3

**Files:**
- Modify: `src/decker/plugin/protocol.py` (`PROTOCOL_VERSION = 3`), `src/decker/server/plugins.py` (construct the runner, `_route_message`, `plugin_options`, `_on_plugin_exit`, `_on_plugin_failed`, `remove_plugin`/unregister paths, `destroy`), `src/decker/server/service.py` (`plugin_options`)
- Test: `tests/server/test_plugins_options.py`, `tests/server/test_service.py`, `tests/server/test_plugins_variables.py` (the hello-protocol test)

**Interfaces:**
- Consumes: `OptionsRunner`/`OptionsResult` (Task 2).
- Produces:
  - `PluginManager.plugin_options(plugin_id: str, source: str) -> OptionsResult` — returns `OptionsResult(False, error="unknown plugin")` for a plugin that is not loaded and `OptionsResult(False, error="unknown options source")` when no `choice` field with that `options_source` exists in any card's settings, any action's settings or the global settings of that plugin; otherwise `runner.fetch`.
  - `DeckerService.plugin_options(plugin_id: str, source: str) -> dict` (`@exposed @api_response`, one-line docstring): `{"ok": bool, "options": [{"label","value"}...], "error": str}`; `{"ok": False, "options": [], "error": "no server"}` without a context.
  - Routing: `options_result` goes to `runner.on_result`; every place that calls `self._variable_runner.plugin_stopped(...)` also calls `self._options_runner.plugin_stopped(...)`; `destroy` calls `stop_all`; the hello uses `PROTOCOL_VERSION` (3).

- [ ] **Step 1: Write failing tests** (real `PluginManager` with a FakeProcess like `tests/server/test_plugins_variables.py`): `test_plugin_options_asks_the_declaring_plugin_and_returns_cleaned_options` (a card setting with `options_source`); `test_source_declared_in_action_and_global_settings_is_accepted`; `test_undeclared_source_is_refused_without_a_request` (no `get_options` sent); `test_unknown_plugin_is_refused`; `test_options_result_routes_to_the_runner`; `test_plugin_exit_fails_a_pending_fetch_at_once`; `test_destroy_wakes_pending_fetches`; `test_hello_protocol_is_3` (update the existing variables hello test to 3); service tests: `test_plugin_options_rpc_returns_ok_and_options`, `..._returns_the_error_text`, `..._without_context_is_not_ok`.
- [ ] **Step 2: Run** `uv run pytest tests/server/test_plugins_options.py tests/server/test_service.py tests/server/test_plugins_variables.py --no-cov -k "options or hello or Options"` — expect FAIL.
- [ ] **Step 3: Implement** the routing, `plugin_options`, the RPC and the protocol bump.
- [ ] **Step 4: Run** the Step 2 command and `tests/server tests/plugin` — expect PASS.
- [ ] **Step 5: Commit** `feat: plugin_options RPC and get_options/options_result (protocol 3)`.

### Task 4: SDK `@options`

**Files:**
- Create: `src/decker_plugin_sdk/_options.py`
- Modify: `src/decker_plugin_sdk/_runtime.py` (handle `get_options`), `src/decker_plugin_sdk/__init__.py`
- Test: `tests/sdk/test_options_sdk.py` (follow `tests/sdk/test_variables_sdk.py`), `tests/sdk/test_options_rules_parity.py`

**Interfaces:**
- Produces: `options(source: str) -> Callable[[F], F]` exported from `decker_plugin_sdk`; `_options.reset() -> None` (tests); `class OptionsHost: __init__(self, send: Sender, log_error: Callable[[str], None])` with `answer(payload: dict[str, Any]) -> None`. The SDK cannot import `decker`: it duplicates the cleaning rules (same limits as `decker.common.options`).
- Behaviour: `get_options` events are answered in the worker pool (`_Runtime._submit`) with `options_result`; the function is sync or async (async runs through the same helper `_variables` uses for coroutines); its result may be a list of `{"label","value"}` dicts, `(label, value)` tuples or plain strings (label = value); everything else is dropped; the answer is `{"request", "ok": true, "options": [...]}`; an unknown source, a non-list result or an exception answers `{"request", "ok": false, "error": "..."}` (exception text cut to 256 characters, logged once as an error event).

- [ ] **Step 1: Write failing tests:** `test_get_options_answers_with_dicts`; `test_tuples_and_strings_are_normalised`; `test_async_function_is_awaited`; `test_unknown_source_answers_not_ok`; `test_exception_answers_not_ok_and_logs`; `test_non_list_result_answers_not_ok`; `test_garbage_entries_are_dropped_and_capped` (10 000 entries, long strings, duplicates); `test_decorator_registers_per_source_and_reset_clears`; parity: `test_option_cleaning_matches_the_server` (a table of cases: valid, missing keys, non-strings, long text, duplicates, over the cap — compare the SDK's normalisation of dicts with `decker.common.options.clean_options`).
- [ ] **Step 2: Run** `uv run pytest tests/sdk --no-cov` — expect FAIL.
- [ ] **Step 3: Implement** `_options.py`, the one-line routing in `_runtime.py` (`elif event == "get_options": self._submit(self._opts.answer, payload)`), and the exports.
- [ ] **Step 4: Run** `uv run pytest tests/sdk --no-cov` — expect PASS.
- [ ] **Step 5: Commit** `feat: SDK @options answers get_options`.

### Task 5: `DynamicChoiceEdit` and the form builder

**Files:**
- Create: `src/decker/client/widgets/dynamic_choice_edit.py`
- Modify: `src/decker/client/widgets/schema_form.py` (`_build_field`, `SchemaForm.set_schema`, `AppearanceForm.set_schema` take `plugin_id: str = ""`)
- Test: `tests/client/widgets/test_dynamic_choice_edit.py`, `tests/client/widgets/test_schema_form.py`

**Interfaces:**
- Consumes: the `plugin_options` RPC contract (Task 3) through an injected fetch function.
- Produces:
  - `FetchOptions = Callable[[str, str], tuple[bool, list[dict[str, str]], str]]` (plugin id, source) -> (ok, options, error).
  - `fetch_options(rpc: RpcCaller | None, plugin_id: str, source: str) -> tuple[bool, list[dict[str, str]], str]` — calls `rpc.call("plugin_options", plugin_id, source)`, parses the JSON object, and returns `(False, [], "<reason>")` on any failure (never raises), following `fetch_actions` in `action_edit.py`.
  - `class DynamicChoiceEdit(QWidget)`: `__init__(entry: dict[str, Any], plugin_id: str, fetch: FetchOptions | None = None, parent: QWidget | None = None)`; `value() -> str`; `set_value(value: Any) -> None` (emits nothing); signal `value_changed`; `reload() -> None`. An editable `QComboBox` (static options first) plus a refresh `QToolButton`. `reload` loads on a daemon worker thread and delivers the result to the GUI thread through a Qt signal carrying a request token; a result whose token is not the latest, or that arrives after the widget was destroyed, is dropped. The combo keeps the current text, adds the dynamic options that are not already listed, and does not emit `value_changed` while filling. On failure it keeps the static options and the current text and sets the tooltip to the error. A value not in the list stays selected and its item gets the tooltip "unavailable".
  - `_build_field` returns a `DynamicChoiceEdit` field for a `choice` entry with `options_source` when `plugin_id` is non-empty; otherwise the existing static builder (with an editable combo when `options_source` is set, so a typed value is possible). `SchemaForm.set_schema(schema, values, plugin_id="")` and `AppearanceForm.set_schema(..., plugin_id="")` pass it through.

- [ ] **Step 1: Write failing tests** (pytest-qt, an injected fake fetch; follow neighbouring widget tests): `test_static_options_show_first_then_dynamic_without_repeats`; `test_stored_value_not_in_the_list_stays_selected`; `test_typing_a_value_is_returned_by_value`; `test_failure_keeps_static_options_and_current_value_and_sets_tooltip`; `test_loading_emits_no_value_changed`; `test_refresh_button_fetches_again`; `test_stale_result_after_a_newer_reload_is_dropped`; `test_result_after_the_widget_is_deleted_does_not_raise` (delete, then let the late result fire); `test_form_without_plugin_id_falls_back_to_static_options_and_typing`; `test_schema_form_builds_the_dynamic_field_when_plugin_id_is_given`; `test_schema_form_value_round_trip_for_an_unlisted_value`; `test_fetch_options_helper_parses_the_rpc_and_never_raises` (no rpc, bad JSON, `ok: false`).
- [ ] **Step 2: Run** `QT_QPA_PLATFORM=offscreen uv run pytest tests/client/widgets/test_dynamic_choice_edit.py tests/client/widgets/test_schema_form.py --no-cov` — expect FAIL.
- [ ] **Step 3: Implement** the widget, the fetch helper and the builder wiring; the worker thread must not touch widgets.
- [ ] **Step 4: Run** the Step 2 command, then `tests/client` — expect PASS.
- [ ] **Step 5: Commit** `feat: DynamicChoiceEdit, an editable dropdown filled by the plugin`.

### Task 6: Pass the plugin id from every form

**Files:**
- Modify: `src/decker/client/widgets/card_config_panel.py` (`show_card_config(..., plugin_id: str = "")`), `src/decker/client/views/dashboard.py` (the call near line 684), `src/decker/client/widgets/schema_form.py` (`ActionParamsEdit.set_action` uses the listed action's `plugin_uuid`), `src/decker/client/widgets/plugins_tab.py` (global settings form), `src/decker/client/widgets/slots_edit.py` (`_SlotConfig.show_forms(..., plugin_id: str = "")` and its caller; a slot's plugin is the card's plugin or the action's plugin)
- Test: `tests/client/widgets/test_card_config_panel.py`, `tests/client/widgets/test_slots_edit.py`, `tests/client/widgets/test_plugins_tab.py`, `tests/client/widgets/test_schema_form.py`

**Interfaces:**
- Consumes: `SchemaForm.set_schema(schema, values, plugin_id)` (Task 5).
- Produces: every settings form of a card, an action (including Run Action parameters and Switch slots) and a plugin's global settings builds its `options_source` fields with the owning plugin's id.

- [ ] **Step 1: Write failing tests:** `test_card_config_panel_passes_the_plugin_id_to_the_settings_form`; `test_action_params_use_the_actions_plugin_id`; `test_slot_config_uses_the_slots_plugin_id` (card slot and action slot); `test_plugins_tab_global_settings_use_the_plugin_id`; each asserts a `choice` field with `options_source` becomes a `DynamicChoiceEdit` bound to that id (inject a fake fetch or monkeypatch the helper, as the neighbouring tests do).
- [ ] **Step 2: Run** `QT_QPA_PLATFORM=offscreen uv run pytest tests/client --no-cov -k "plugin_id or options"` — expect FAIL.
- [ ] **Step 3: Implement** the parameters and call-site changes; read the dashboard call site to find where the key's plugin id is available.
- [ ] **Step 4: Run** `tests/client` — expect PASS.
- [ ] **Step 5: Commit** `feat: settings forms pass the owning plugin to dynamic choice fields`.

### Task 7: End to end through a real plugin process

**Files:**
- Create: `tests/server/fixtures/options_plugin/` (manifest + `plugin.py` using the SDK; no variables so it stays lazy)
- Test: `tests/server/test_options_e2e.py` (follow `tests/server/test_variables_e2e.py`: the interpreter link, `stage_sdk`, polling helpers, cleanup, explicit `skipif(sys.platform == "win32")`, `psutil` for any kill)

**Interfaces:**
- Consumes: Tasks 1-6 (real `PluginManager`, real supervisor, real SDK child).

- [ ] **Step 1: Write failing tests:** `test_options_flow_from_plugin_to_rpc_result` (a card setting `options_source` declared in the fixture manifest, `@options` returning dicts, tuples and strings); `test_lazy_plugin_is_started_for_the_request_and_released_after` (process running during the call, stopped after the idle grace shortened by the policy); `test_unknown_source_and_undeclared_source_errors`; `test_slow_source_times_out_and_the_plugin_stays_usable` (a short `OptionsRunner` timeout through a `PluginManager` keyword or the existing knob pattern; afterwards a normal source still answers); `test_saved_value_outside_the_list_survives_apply_schema` (settings store round trip).
- [ ] **Step 2: Run** `uv run pytest tests/server/test_options_e2e.py --no-cov` — expect FAIL.
- [ ] **Step 3: Build the fixture plugin** and fix whatever the end-to-end run exposes in Tasks 1-6 (report any change to earlier tasks; stop and report BLOCKED if the fix is larger than a few lines).
- [ ] **Step 4: Run** the e2e file three times in a row and `uv run pytest tests/server --no-cov -q` once in suite order — expect PASS, not flaky.
- [ ] **Step 5: Commit** `test: dynamic choice options end to end through a real plugin process`.

### Task 8: Docs and gates

**Files:**
- Modify: `docs/plugins/settings-schema.md` (the `choice` row and a short `options_source` section), `docs/plugins/protocol.md` (the two events, hello protocol 3), `docs/plugins/sdk-quickstart.md` (`@options` with a worked example, which must run), `docs/plugins/limits.md` (256 options, 256 characters, 5 s), `CLAUDE.md` (plugins paragraph), `README.md` (feature list; the platform table needs no change)
- Verify other statements that enumerate field types or `choice` rules (`grep -rn "choice" docs README.md CLAUDE.md`).

- [ ] **Step 1: Update the docs** from the spec; verify each claim against the code (names, limits, timeouts, events, the editable-dropdown behaviour, the fallback without a plugin id, an older SDK timing out).
- [ ] **Step 2: Run the example** from the quickstart through the SDK runtime (scratch file outside the repo, using the helpers the SDK tests use) and report the result.
- [ ] **Step 3: Gates:** `poe format`, `uv run mypy .`, `uv run mypy --platform win32 .`, `uv run mypy --platform darwin .` (three separate commands, run while no tests are running), `poe pylint`, then the full suite with coverage; the only known unrelated failure is `tests/server/test_builtin_plugins.py::test_blank_version_is_deckers`.
- [ ] **Step 4: Commit** `docs: dynamic choice fields in the plugin docs, CLAUDE.md and README`.
