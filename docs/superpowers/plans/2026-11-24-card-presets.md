# Card Presets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Plugins declare presets (a Blank key with a name, icon, appearance and gesture bindings) that the picker's Cards tab lists and drops; the Actions tab goes away; unstyled SVG icons draw white on keys.

**Architecture:** A new manifest kind `contributes.presets[]` is parsed into `PresetSpec`, served by `plugins_presets_list`, and dropped through one atomic RPC `key_assign_preset` that assigns the Blank card and stores the preset's bindings and appearance. The client merges presets into the Cards tab and deletes the Actions tab. The white default lives in the key-face compose path.

**Tech Stack:** Python 3.14, PyQt6, RPyC, pytest + assertpy, black (120), mypy (also `--platform win32/darwin`), pylint.

**Spec:** `docs/superpowers/specs/2026-11-24-card-presets-design.md`

Two repos, two PRs, in this order: Part 1 (Decker, repo `/work/decker/decker`), then Part 2 (decker-plugins, `/work/decker/decker-plugins`). Work Part 1 on a branch off the current one; Part 2 starts only after Part 1's commit exists.

## Global Constraints

- Gestures are exactly `press`, `double_press`, `long_press`; a preset binding names only a `global` action (any plugin's, including its own) or Do Nothing, because the dropped key is a Blank card. A preset bound to its own plugin's `plugin`-scoped action is a manifest error; a foreign `plugin`-scoped action is filtered at list time (Task 2).
- Preset `appearance.icon.ref` must be a ref (`pack:<pack id>/<path>` or `fas:play`), never a plugin file path; the preset's `icon` (picker thumbnail) may be a plugin file or a built-in ref and is not copied to the key.
- A preset may not declare `settings`, `states`, `static` or `gestures`.
- A plugin that declares presets must declare a `"decker"` constraint (as `variables` and `options_source` do). New Decker version: `0.8.0`; plugins declare `">=0.8"`.
- A preset's key title defaults to the preset `name` when `appearance.title.text` is absent; with no `appearance.icon` the key shows the press action's icon.
- Dropped keys are plain Blank keys (`com.decker.blank` / `com.decker.blank.blank`) with copied bindings and appearance; nothing links them to the preset.
- Old Blank cards `com.decker.blank.pagename` and `com.decker.blank.pagecount` load empty (no alias).
- Quality gates from `CLAUDE.md` before each commit that ends a task: `poe format`, `poe mypy` (plus `mypy --platform win32 .` and `mypy --platform darwin .` once at the end of Part 1), `poe pylint` (≥ 9.0), `poe test` (≥ 85 % coverage). Max 5 args per function, methods under 50 lines, public docstrings one line.
- Platform tiers unchanged; the README platform tables need no new row (no new OS-specific feature).
- Never delete Utilities' `page`/`profile` actions in this change.

## Review Focus

- A preset bound to another plugin's `plugin`-scoped action is filtered at list time, not refused at load; only a preset's OWN plugin-scoped bindings are refused at manifest load (tested through `from_dict`).
- Dropping a preset on a key that already has a card replaces it and deactivates the old card (no orphaned live instance), and on an unknown page returns False without writing anything.
- A preset whose bound action is not installed is hidden from `plugins_presets_list` and returns when the action appears (no stale cache).
- An existing page holding old `com.decker.blank.pagename`/`.pagecount` keys loads without error and those keys are empty.
- White default: SVG with `currentColor` goes white; partly coloured SVG (child `fill`) is untouched; `style="fill:#f00"` / `<style>` SVG untouched; PNG and animated GIF untouched; an explicit `appearance.icon.color` still wins; an unreadable SVG does not crash composition.
- A preset with no bindings (Page Name, Page Count) drops a Blank key with Do Nothing everywhere, and its title/background come only from its appearance.
- A preset whose bindings leave a required argument unset (Go to page) still drops (nothing rejected), and the drop result says the bindings are incomplete.

---

## Part 1 — Decker

### Task 1: Manifest `presets` kind

**Files:**
- Modify: `src/decker/plugin/manifest.py` (add `PresetSpec`, `_parse_preset`, field `presets` on `PluginManifest`, `KNOWN_CONTRIBUTION_KINDS`, `from_dict`)
- Modify: `src/decker/server/contributions.py` (`contributed_kinds` gains `"presets"`)
- Modify: `src/decker/server/plugins.py` (a `_PresetsHandler` with `kind = "presets"`, no-op `register`/`unregister`, added to the `ContributionRegistry` list at line ~234)
- Test: `tests/plugin/test_manifest.py`, `tests/server/test_contributions.py`

**Interfaces:**
- Produces: `PresetSpec(id: str, name: str, tooltip: str = "", icon: str | None = None, appearance: dict[str, Any], bindings: Mapping[str, Binding])` (frozen dataclass, `appearance`/`bindings` with `field(default_factory=dict, hash=False)` like `CardSpec`); `PluginManifest.presets: tuple[PresetSpec, ...] = ()`.

- [ ] **Step 1: Write failing tests** in `tests/plugin/test_manifest.py`, building manifests with `PluginManifest.from_dict`:
  - `test_preset_parses_name_icon_appearance_and_bindings`: a plugin with action `com.x.go` (global) and preset `com.x.next` with `bindings.press = {"action": "com.x.go", "args": {"wrap": True}}`; assert `manifest.presets[0].bindings["press"].args == {"wrap": True}` and `.appearance["title"]["text"] == "Next"`.
  - `test_presets_alone_need_no_runtime`: manifest with only a preset bound to a global action of another plugin (`com.decker.core.delay`), `"decker": ">=0.8"`, no runtime → loads.
  - `test_preset_id_must_start_with_plugin_id`, `test_preset_unknown_key_is_refused` (parametrize `settings`, `states`, `static`, `gestures`, `bogus`), `test_preset_unknown_appearance_key_is_refused`, `test_preset_icon_ref_must_not_be_a_path` (`appearance.icon.ref = "icons/x"` → error), `test_preset_binding_to_undeclared_own_action_is_refused`, `test_preset_needs_decker_constraint` (no `"decker"` → `ManifestValidationError` naming presets), `test_duplicate_preset_id_is_refused`.
  - In `tests/server/test_contributions.py`: `contributed_kinds` returns `"presets"` for a manifest declaring one.
- [ ] **Step 2: Run** `poe test tests/plugin/test_manifest.py tests/server/test_contributions.py -q` → FAIL.
- [ ] **Step 3: Implement.** `_parse_preset(entry: Any, plugin_id: str) -> PresetSpec` follows `_parse_card`: `_parse_contribution_id`, reject any key outside `{id, name, tooltip, icon, appearance, bindings}`, appearance keys checked against `APPEARANCE_KEYS` (reuse the loop from `_parse_card`; extract it into `_parse_appearance(entry, owner) -> dict`), `appearance.icon.ref` validated with the existing icon-ref parser (`parse_icon_ref` in `common/icon_ref.py`, accept `pack:`/`fas:`/`far:`/`fab:` only), bindings via `parse_manifest_bindings`. Extend `_check_own_bindings` to take presets too (messages say "preset"). Reject a foreign plugin-scoped action is impossible at manifest time (other plugins unknown), so it is enforced in Task 2's list filter. `from_dict`: `"presets"` joins `KNOWN_CONTRIBUTION_KINDS`; `presets` non-empty requires the same `_require_decker_constraint` check variables use (extract it from `_require_variable_prerequisites` without the runtime test); the "contributes no cards…" error lists presets too; duplicate preset ids refused.
- [ ] **Step 4: Run** the same command → PASS.
- [ ] **Step 5: Commit** `feat(plugin): presets manifest kind`.

### Task 2: Server list, atomic drop and retired cards

**Files:**
- Modify: `src/decker/server/plugins.py` (`PluginManager.list_presets`, `PluginManager.preset(plugin_id, preset_id)`)
- Modify: `src/decker/server/service.py` (RPCs `plugins_presets_list`, `key_assign_preset`)
- Modify: `src/decker/server/key_assignments.py` (`RETIRED_CARDS` gains `("com.decker.blank", "com.decker.blank.pagename")` and `("com.decker.blank", "com.decker.blank.pagecount")`; `KeyAssignmentStore.assign` gains `appearance: dict[str, Any] | None = None` stored in the same insert)
- Test: `tests/server/test_plugins.py` (or `test_plugins_bindings.py`), `tests/server/test_service.py`, `tests/server/test_key_assignments.py`

**Interfaces:**
- Consumes: `PresetSpec` (Task 1), `ActionRunner.scope_of(action_id) -> tuple[str, str] | None`, `PluginManager.validate_bindings(target, stored) -> dict[str, str]`.
- Produces: `PluginManager.list_presets() -> list[dict[str, Any]]` with keys `plugin_uuid`, `plugin_name`, `plugin_builtin`, `preset_uuid`, `preset_name`, `icon_b64`, `tooltip`, `appearance`, `bindings` (`{gesture: {"action", "args"}}`); `DeckerService.plugins_presets_list() -> list` (same decorators as `plugins_cards_list`); `DeckerService.key_assign_preset(device_id: str, page_id: str, key_index: int, plugin_uuid: str, preset_id: str, context_id: str) -> dict` returning `{"ok": bool, "errors": dict[str, str]}` (`errors` = `validate_bindings` result for the stored bindings; `ok` False for an unknown page or preset).

- [ ] **Step 1: Write failing tests.**
  - `test_list_presets_shape_and_icon` (fixture plugin with a preset; assert the keys above, `icon_b64` non-empty for a file icon).
  - `test_list_presets_hides_preset_with_missing_action` and `test_list_presets_hides_preset_bound_to_foreign_plugin_scoped_action`, and `test_preset_returns_when_action_plugin_installs` (install the action's plugin after, list again).
  - In `test_service.py`: `test_key_assign_preset_assigns_blank_with_bindings_and_appearance` (assert the stored assignment's `card_uuid == "com.decker.blank.blank"`, `bindings["press"]["action"]`, `appearance["title"]["text"]` equals the preset name when the preset gives none); `test_key_assign_preset_unknown_page_returns_not_ok_and_writes_nothing`; `test_key_assign_preset_unknown_preset_returns_not_ok`; `test_key_assign_preset_replaces_existing_card_and_deactivates_it` (spy on `deactivate_card`); `test_key_assign_preset_reports_incomplete_bindings` (Go to page without a page → `errors` non-empty, assignment still stored); `test_key_assign_preset_without_bindings_stores_empty_bindings`.
  - In `test_key_assignments.py`: `test_page_with_retired_blank_page_name_key_loads_empty`.
- [ ] **Step 2: Run** `poe test tests/server/test_plugins.py tests/server/test_service.py tests/server/test_key_assignments.py -q` → FAIL.
- [ ] **Step 3: Implement.** `list_presets` iterates `self.plugins()` like `list_cards`; a preset is included only when every non-Nothing binding's action resolves via `scope_of` to a global action or to an action owned by that same plugin (compute at call time, so no cache). `key_assign_preset` follows `key_assign`'s order: unknown page/preset → `{"ok": False, "errors": {}}`; deactivate an existing live card; `key_store.assign(..., settings={}, bindings, appearance)` with `appearance` = the preset's appearance plus `title.text = preset name` when absent (merge into a copy; keep other title fields); `pm.store_settings`, `pm.set_bindings`, `pm.set_appearance`, then `open_device` + `activate_card` when the page is active; return `validate_bindings` errors. Share the assign tail with `key_assign` via a private helper rather than copying. Use the Blank ids as module constants.
- [ ] **Step 4: Run** the same command → PASS.
- [ ] **Step 5: Commit** `feat(server): preset list and atomic preset drop`.

### Task 3: Unstyled SVG icons draw white

**Files:**
- Modify: `src/decker/asset/tint.py` (`is_unstyled_svg`)
- Modify: `src/decker/server/appearance.py` (`compose_key_image` icon layer near line 308 and `_overlay_layer` near line 389 use the default)
- Test: `tests/asset/test_tint.py`, `tests/server/test_appearance.py`

**Interfaces:**
- Produces: `is_unstyled_svg(data: bytes) -> bool` — True only for an SVG in which no element anywhere has a `fill`, `stroke` or `style` attribute, and no `<style>` element exists, whose value is a colour other than `none`/`currentColor` (a bare `fill="none"` or `currentColor` still counts as unstyled); False for non-SVG bytes. `default_icon_color(image: bytes, color: str | None) -> str | None` private to `appearance.py`: returns `color` when set, `"#ffffff"` when `color` is None and `is_unstyled_svg(image)`, else None.

- [ ] **Step 1: Write failing tests** (`test_tint.py`): `test_unstyled_when_no_colour_anywhere`, `test_currentcolor_and_fill_none_count_as_unstyled`, `test_child_fill_makes_it_styled`, `test_stroke_attribute_makes_it_styled`, `test_style_attribute_colour_makes_it_styled`, `test_style_element_makes_it_styled`, `test_png_is_not_unstyled`, `test_garbage_bytes_are_not_unstyled`. In `test_appearance.py`: render a key with a black-by-default SVG (`<svg ...><path d="M0 0h24v24H0z"/></svg>`) and no colour; assert the icon area's centre pixel is white; the same SVG with `plugin_image_color="#ff0000"` is red; an SVG with child `fill="#00ff00"` stays green; a PNG icon is unchanged.
- [ ] **Step 2: Run** `poe test tests/asset/test_tint.py tests/server/test_appearance.py -q` → FAIL.
- [ ] **Step 3: Implement** `is_unstyled_svg` with a small `xml.etree` parse guarded by the same `ParseError`-tolerant approach the renderer uses (return False on any parse error; use `defusedxml` only if the project already depends on it, otherwise a regex over attributes is acceptable). Apply `default_icon_color` where `render_bytes(..., color)` is called for the key icon in both places; the composition result for an animated GIF is unchanged.
- [ ] **Step 4: Run** the same command → PASS.
- [ ] **Step 5: Commit** `feat(asset): unstyled svg key icons draw white`.

### Task 4: Core presets, Blank cleanup

**Files:**
- Modify: `src/decker/builtin_plugins/com.decker.core/manifest.json` (presets, version `0.8.0`, `"decker": ">=0.8"`)
- Modify: `src/decker/builtin_plugins/com.decker.blank/manifest.json` (remove `pagename`, `pagecount`; description "A key that shows only a background and a title."; version `0.8.0`)
- Modify: `pyproject.toml` (`version = "0.8.0"`)
- Test: `tests/server/test_builtin_plugins.py` (replace `test_blank_ships_page_name_and_page_count_presets`)

**Interfaces:**
- Produces: Core presets `com.decker.core.preset-httprequest` ("HTTP Request"), `com.decker.core.preset-delay` ("Delay"), `com.decker.core.preset-sequence` ("Sequence"), each with `icon` equal to its action's icon and `bindings.press.action` set to `com.decker.core.httprequest` / `.delay` / `.sequence`, no `args`.

- [ ] **Step 1: Write failing tests:** `test_blank_ships_only_the_blank_card`; `test_core_ships_presets_for_httprequest_delay_and_sequence` (ids above, press actions, all three listed by `list_presets` on a real built-in load); `test_core_presets_not_listed_for_nothing`.
- [ ] **Step 2: Run** `poe test tests/server/test_builtin_plugins.py -q` → FAIL.
- [ ] **Step 3: Edit the manifests and the version.** Grep for other places that assert `0.7.2` (CLAUDE.md text is Task 6) and update tests that hard-code it.
- [ ] **Step 4: Run** `poe test tests/server -q` → PASS.
- [ ] **Step 5: Commit** `feat(builtin): Core presets, Blank keeps only the Blank card`.

### Task 5: Client — presets in the Cards tab, Actions tab removed

**Files:**
- Modify: `src/decker/client/connection_manager.py` (`_refresh_presets` mirroring `_refresh_cards`; new event `PresetListUpdatedEvent(presets: list[dict[str, Any]])` in the events module; refreshed wherever cards are refreshed)
- Modify: `src/decker/client/widgets/sidebar.py` (single pane; presets appear as entries in `_cards_pane` keyed by `preset_uuid`; drop `_actions_pane`, tabs, `set_actions`, `action_sections`, `_action_icon`, `_action_drag`, `_update_placeholder`, `tab_names`, the `tasks.svg`/`images.svg` tab use; placeholder text "Search cards...")
- Modify: `src/decker/client/views/dashboard.py` (`set_presets` wiring; `_on_card_dropped` loses the `preset_action` branch; new `_on_preset_dropped`; drag payload carries `preset_uuid`)
- Test: `tests/client/widgets/test_sidebar.py`, `tests/client/views/test_dashboard.py`, `tests/client/test_connection_manager.py`

**Interfaces:**
- Consumes: `plugins_presets_list`, `key_assign_preset` (Task 2).
- Produces: `SidebarWidget.set_presets(presets: list[dict[str, Any]]) -> None`; `SidebarWidget.sections` lists cards and presets together under each plugin, in plugin order, names sorted; drag payload for a preset has `plugin_uuid`, `plugin_name`, `preset_uuid`, `preset_name` and no `card_uuid`.

- [ ] **Step 1: Write failing tests:** sidebar — `test_presets_list_with_cards_under_their_plugin`, `test_search_matches_preset_name_and_plugin`, `test_there_is_no_actions_tab`, `test_preset_drag_payload_has_preset_uuid`; dashboard — `test_dropping_a_preset_calls_key_assign_preset_and_selects_the_key` (fake connection manager records the RPC args; response `{"ok": True, "errors": {}}`), `test_dropping_a_preset_with_errors_selects_the_key_on_the_press_tab`, `test_failed_preset_drop_changes_nothing`; connection manager — `test_presets_refresh_pushes_event_once_per_change`.
- [ ] **Step 2: Run** `poe test tests/client/widgets/test_sidebar.py tests/client/views/test_dashboard.py tests/client/test_connection_manager.py -q` → FAIL.
- [ ] **Step 3: Implement.** Reuse `_Pane` entry mechanics for presets (they differ from cards only in the id key and icon source, `decode_icon(preset["icon_b64"])`). After a successful drop, set `_select_after_refresh = key_index` as the existing flow does; when `errors` is non-empty also ask the config panel to show the Press tab (add `CardConfigPanel.show_gesture_tab(gesture: str) -> None` if none exists). Remove the Actions-tab tests and the `ActionListUpdatedEvent` consumer in the dashboard only if nothing else uses it (the binding dropdown reads `plugins_actions_list` itself). Delete `tasks.svg` only if no other code references it.
- [ ] **Step 4: Run** the same command → PASS.
- [ ] **Step 5: Commit** `feat(client): presets in the Cards tab, remove the Actions tab`.

### Task 6: Docs and gates for Part 1

**Files:**
- Modify: `docs/plugins/package-and-manifest.md` (a `presets[]` section; remove the Actions-tab sentences in the cards and bindings sections), `docs/plugins/README.md` if it lists kinds, `CLAUDE.md` (plugins paragraph: Blank, Core presets, picker paragraph, version `0.8.0`, `contributes` list gains `presets`), `README.md` if it mentions the Actions tab

- [ ] **Step 1:** Update the docs to match Tasks 1–5 (field table copied from the spec's manifest section).
- [ ] **Step 2:** Run all gates: `poe format`, `poe mypy`, `mypy --platform win32 .`, `mypy --platform darwin .`, `poe pylint`, `poe test`. Expected: all pass, coverage ≥ 85 %.
- [ ] **Step 3: Commit** `docs: presets, Actions tab removal`. Note the commit hash for Part 2.

---

## Part 2 — decker-plugins (after Part 1's commit exists)

### Task 7: Pin the new Decker and add Pages and Profiles presets

**Files:**
- Modify: `pyproject.toml` (`decker @ git+https://github.com/valsr/decker.git@<Part 1 commit>`), then `uv lock`
- Modify: `plugins/com.decker.pages/manifest.json` (version `3.1.0`, `"decker": ">=0.8"`, presets), `plugins/com.decker.profiles/manifest.json` (version `3.1.0`, same)
- Test: `plugins/com.decker.pages/tests/test_manifest.py`, `plugins/com.decker.profiles/tests/test_manifest.py`

**Interfaces:**
- Produces: Pages presets `com.decker.pages.preset-next` ("Next page", press `com.decker.pages.next` args `{"wrap": true}`, `icon` and `appearance.icon` `fas:forward`), `preset-previous` ("Previous page", `fas:backward`, `wrap: true`), `preset-goto` ("Go to page", press `com.decker.pages.goto`, no args, `fas:file`), `preset-name` ("Page Name", no bindings, `appearance.title = {"text": "{page_name}", "position": "middle", "size": 16}`), `preset-count` ("Page Count", no bindings, `title = {"text": "{page_index} / {page_count}", "position": "middle", "size": 24}`); Profiles `preset-next`, `preset-previous`, `preset-goto` on the profile actions (icons `fas:forward`, `fas:backward`, `fas:layer-group`) and `preset-name` ("Profile Name", title `{current_profile}`, size 16). Each `appearance.icon` is `{"ref": "<same fas ref>", "color": "#ffffff"}`.

- [ ] **Step 1: Write failing tests** per plugin: `test_presets_are_declared` (ids, names, press actions, args), `test_page_name_and_count_titles`, `test_presets_load_with_the_pinned_decker`, and update the existing version/`decker` assertions to `3.1.0` / `">=0.8"` with `is_compatible(manifest.decker, "0.7.2")` False.
- [ ] **Step 2:** Move the pin, `uv sync`, then run `uv run pytest plugins/com.decker.pages plugins/com.decker.profiles -q` → FAIL.
- [ ] **Step 3: Edit the manifests.**
- [ ] **Step 4: Run** the same command → PASS.
- [ ] **Step 5: Commit** `feat(pages,profiles): presets, require Decker 0.8`.

### Task 8: Utilities presets and the sort report

**Files:**
- Modify: `plugins/com.decker.utilities/manifest.json` (version `2.1.0`, `"decker": ">=0.8"`, presets)
- Modify: `plugins/com.decker.utilities/icons/` only if a preset needs a new icon (prefer `fas:` refs)
- Test: `plugins/com.decker.utilities/tests/test_manifest.py` (or the file that already asserts the manifest)
- Create: `docs/superpowers/notes/utilities-page-profile-overlap.md` in the decker repo (the report; findings only)

**Interfaces:**
- Produces presets `com.decker.utilities.preset-brightness-up` ("Brightness Up", press `com.decker.utilities.brightness`), `preset-brightness-down`, `preset-deck-brightness-up` (press `com.decker.utilities.deck-brightness`), `preset-deck-brightness-down`, `preset-lock` ("Lock screen", press `com.decker.utilities.lock`). Arguments follow each action's own settings (read `manifest.json` for the keys: a step of `+10`/`-10` or the equivalent mode field), icons `fas:sun`/`fas:lock` and, for deck brightness, `fas:tv`; titles are the preset names.

- [ ] **Step 1: Write failing tests:** `test_presets_are_declared_and_bound` (ids, press actions, args validate against the bound action's settings via `validate_settings`), `test_no_sleep_preset`, `test_actions_scopes_unchanged` (global: brightness, deck-brightness, sleep, lock, sleep-cancel, page, profile; plugin: counter-*, sleep-press, sleep-after, timer-*, stopwatch-*).
- [ ] **Step 2: Run** `uv run pytest plugins/com.decker.utilities -q` → FAIL.
- [ ] **Step 3: Edit the manifest.** Then compare Utilities `page`/`profile` actions with the Pages/Profiles actions (settings, behaviour in `navigation.py` vs `plugins/com.decker.pages/plugin.py`) and write the findings (same or different, which would be removed, effect on stored keys) to the note; do not delete anything.
- [ ] **Step 4: Run** the same command → PASS.
- [ ] **Step 5: Commit** `feat(utilities): brightness and lock presets`.

### Task 9: Repository housekeeping

**Files:**
- Modify: `README.md` plugin table (versions, the Pages/Profiles/Utilities descriptions mention presets), `tools` version checks if they list versions

- [ ] **Step 1:** Run `uv run pytest -q` at the repo root (includes `tests/tools`) → note failures.
- [ ] **Step 2:** Fix README/version fixtures the tools tests check (`check_versions`, `build_index`); rerun until PASS.
- [ ] **Step 3: Commit** `docs: presets in the plugin table`.

---

## Self-review

- **Spec coverage:** manifest field (T1), picker merge, Actions tab removal, drop (T5), atomic drop and visibility rule (T2), Core presets (T4), Blank cleanup and retired keys (T2, T4), Pages/Profiles presets incl. Page/Profile Name (T7), Utilities presets and sort (T8), white icons with the whole-file check (T3), sequencing and pin (T7), docs and version (T4, T6, T9), report on `page`/`profile` (T8). Not covered by design: user "save as preset", linked presets, repository index.
- **Type consistency:** `PresetSpec` (T1) → `list_presets` keys (T2) → sidebar payload `preset_uuid` (T5) → `key_assign_preset(..., preset_id, ...)` (T2/T5); the Blank ids are the constants in T2.
- **Proportion:** the plan is signatures, test names and values; bodies are left to the implementer.
