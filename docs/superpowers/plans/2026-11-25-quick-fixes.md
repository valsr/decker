# Quick Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One "Built in" picker section (Blank moves into Core), a key that can always be deleted, built-in `fas:` icons instead of bundled icon files, and a key right-click menu.

**Architecture:** Core gains the static Blank card; the Blank plugin folder goes; the key store maps the legacy Blank ids to the new ones when it reads rows. `key_get_states` stops failing on a card whose plugin or settings are broken. The key button emits one new signal for the context menu, which the dashboard turns into "select the key and show a gesture tab" or the existing delete flow.

**Tech Stack:** Python 3.14, PyQt6, SQLite, pytest + assertpy.

**Spec:** `docs/superpowers/specs/2026-11-25-quick-fixes-design.md`

## Global Constraints

- Quality gates before done: `poe format`, `poe mypy`, `poe pylint` (≥ 9.0), `poe test` (coverage ≥ 85%); also `mypy --platform win32 .` and `mypy --platform darwin .`.
- Black line length 120; annotate all signatures; max 5 args per function; methods under 50 lines.
- Tests use `pytest` + `assertpy`, mirror the source layout, no mocking of infrastructure unless unavoidable.
- New Core card id is `com.decker.core.blank`; legacy ids are `com.decker.blank` / `com.decker.blank.blank`.
- Retired cards `com.decker.blank.pagename` / `com.decker.blank.pagecount` still load as empty keys.
- Saved keys using the legacy Blank ids must keep working (alias on read; no on-disk rewrite).
- Delete Card from the context menu uses the same confirmation (`_confirm_clear_key`) and `key_unassign` as the hover button.
- Built-in icon refs are `fas:`/`far:` (Font Awesome); do not touch the plugins' `iconpack` folders.

## Review Focus

- A stored key on `com.decker.blank` / `com.decker.blank.blank` loads as the Core Blank card with its settings, appearance and bindings intact.
- A stored retired `com.decker.blank.pagename` key still loads as an empty key.
- Assignments for `com.decker.core` include legacy Blank rows (`assignments_for_plugin`, `plugin_ids`).
- A key whose card is unknown or whose stored settings are malformed still appears in `key_get_states` and can be cleared.
- Right-click on an empty key shows no menu; Delete Card there is impossible.

---

### Task 1: Blank card moves into Core

**Files:**
- Modify: `src/decker/builtin_plugins/com.decker.core/manifest.json` (add the card)
- Delete: `src/decker/builtin_plugins/com.decker.blank/` (whole folder)
- Modify: `src/decker/server/key_assignments.py:22-30,205-225,389-410` (ids, alias, queries)
- Modify: `src/decker/server/service.py:50,618-619`, `src/decker/server/plugins.py:76,886` only if they need nothing but the changed constants (they import `BLANK_PLUGIN_ID`/`BLANK_CARD_ID`; keep those names, new values)
- Modify tests: `tests/server/test_builtin_plugins.py`, `test_key_assignments.py`, `test_plugins.py`, `test_plugins_presets.py`, `test_plugins_bindings.py`, `test_actions_e2e.py`, `test_service.py`, `tests/client/views/test_dashboard.py`, `tests/client/widgets/test_sidebar.py` (replace `com.decker.blank` expectations with the Core ids)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `BLANK_PLUGIN_ID = "com.decker.core"`, `BLANK_CARD_ID = "com.decker.core.blank"`, `LEGACY_BLANK_PLUGIN_ID = "com.decker.blank"`, `LEGACY_BLANK_CARD_ID = "com.decker.blank.blank"` in `decker.server.key_assignments`; private `_canonical(plugin_uuid: str, card_uuid: str) -> tuple[str, str]`.

- [ ] **Step 1: Write the failing tests** in `tests/server/test_key_assignments.py`:
  - `test_legacy_blank_row_loads_as_core_blank`: insert a row with `plugin_uuid="com.decker.blank"`, `card_uuid="com.decker.blank.blank"`, settings `{"a": 1}`, bindings and appearance set; `store.get(...)` returns an assignment with `target.plugin_uuid == "com.decker.core"`, `target.card_uuid == "com.decker.core.blank"` and the same settings, appearance and bindings. Same check through `all_for_page`.
  - `test_legacy_blank_counts_for_core`: `store.assignments_for_plugin("com.decker.core")` includes that legacy row, and `store.plugin_ids()` contains `"com.decker.core"` and not `"com.decker.blank"`.
  - `test_retired_blank_cards_still_load_empty`: a row `("com.decker.blank", "com.decker.blank.pagename")` → `store.get` is `None`.
  In `tests/server/test_builtin_plugins.py` replace the Blank-manifest test with `test_core_manifest_has_the_static_blank_card`: the Core manifest's card ids are `["com.decker.core.blank"]`, `static` is True, name "Blank", tooltip "Draws only the background and title", and `BUILTIN_PLUGINS_DIR / "com.decker.blank"` does not exist.

- [ ] **Step 2: Run** `poe test tests/server/test_key_assignments.py tests/server/test_builtin_plugins.py` (or `uv run pytest` on those files). Expected: the new tests FAIL.

- [ ] **Step 3: Implement.**
  - Core manifest: add `{"id": "com.decker.core.blank", "name": "Blank", "tooltip": "Draws only the background and title", "icon": "far:square", "static": true}` to `contributes.cards`. Confirm a static card in a plugin that has a `runtime` activates without starting Core's process (`PluginManager.activate_card` / `deactivate_card` use `card.headless`); if `static` is not enough, make the card headless the same way the old Blank card was.
  - `key_assignments.py`: set the four constants above; `RETIRED_CARDS` entries for pagename/pagecount use `LEGACY_BLANK_PLUGIN_ID`; add `_canonical()` mapping the legacy pair to the new pair and leaving everything else alone; call it in `_to_assignment` when building the `CardTarget`; `assignments_for_plugin(plugin_id)` selects `plugin_uuid IN (?, ?)` with the legacy id added when `plugin_id == BLANK_PLUGIN_ID`; `plugin_ids()` returns canonical plugin ids.
  - Delete the `com.decker.blank` folder; remove any leftover references to it in `pyproject.toml` package data or the builtin loader if present.
  - Update the other tests' Blank ids.

- [ ] **Step 4: Run the full suites** `poe test` (server and client). Expected: PASS. Also assert in `tests/client/widgets/test_sidebar.py` that built-in entries from Core collapse into a single "Built in" section titled `BUILTIN_TITLE` (one `listing` entry).

- [ ] **Step 5: Commit** `feat: move the Blank card into Core and alias the legacy ids`.

---

### Task 2: A key can always be deleted

**Files:**
- Modify: `src/decker/server/service.py:1112-1162` (`key_get_states`)
- Modify: `src/decker/server/plugins.py:604-613` (`complete_settings`, `appearance_for`) only if the reproduction shows they raise
- Test: `tests/server/test_service.py`; `tests/client/views/test_dashboard.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `key_get_states` always returns `plugin_uuid`/`card_uuid`/`context_id` for an assigned key, never raises because of one key's card.

- [ ] **Step 1: Reproduce first.** In `tests/server/test_service.py` write `test_key_states_survive_a_broken_card`: assign a key to a card id its plugin does not declare, and another with settings that make `complete_settings` fail (find which input does by trying a stored non-dict value, an unknown type, a plugin that is installed but failed); call `key_get_states` and assert it returns a state for every key with the assignment's `plugin_uuid`/`card_uuid` set. Run it; note which scenario fails today and which already passes, and say so in the commit message.

- [ ] **Step 2: Run it to see the failure** (`uv run pytest tests/server/test_service.py -k broken_card -v`). Expected: FAIL in at least one scenario. If none fails, stop and report that the cause is client-side; then read `src/decker/client/views/dashboard.py` `_sync_slot` / `_on_key_delete_requested` for the failing-plugin path (a `plugin_failed` event, a missing card in `self._cards`) and write the failing client test there instead.

- [ ] **Step 3: Implement** a private `_resolved_key(self, assignment: KeyAssignment) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]` in `service.py` returning `(settings, appearance, appearance_defaults)`: it calls `complete_settings` / `appearance_for` and, on `Exception`, logs a warning and returns the stored settings, the stored appearance and `{}`. Use it in `key_get_states`. Keep `key_unassign` working without a live card (add a test: unassign a key whose plugin is missing returns True and removes the row).

- [ ] **Step 4: Client test** in `tests/client/views/test_dashboard.py`: a key state with a `plugin_uuid` the dashboard has no card for still ends up in `_key_slots`, so `_grid`'s delete button is shown (`set_draggable_keys` includes it) and `_on_key_delete_requested` (with `_confirm_clear_key` patched to True) calls `key_unassign`. Fix `_refresh_config_panel` if needed so an unknown card shows the editor with an empty settings schema and the delete path intact.

- [ ] **Step 5: Run** `poe test`. Expected: PASS. **Commit** `fix: keep a key deletable when its card or plugin is broken`.

---

### Task 3: Built-in icons

**Files:**
- Modify: `src/decker/builtin_plugins/com.decker.core/manifest.json` (plugin, cards, actions, presets icons)
- Delete: `src/decker/builtin_plugins/com.decker.core/icons/` once nothing references it
- Modify: `decker-plugins/plugins/com.decker.{counter,pages,profiles,utilities}/manifest.json` (every `icons/<name>` ref)
- Delete the matching `icons/*.png` in each plugin when unreferenced (leave `iconpack/` alone)
- Test: `tests/server/test_builtin_plugins.py`; the decker-plugins repo's manifest tests

**Interfaces:** consumes `read_icon` / `read_builtin_ref` (`decker.plugin.icons`, `decker.asset.builtinicons`), which already read `fas:`/`far:` refs. Produces: no new API.

- [ ] **Step 1: Write the failing test** `test_builtin_plugin_icons_are_font_awesome_refs` in `tests/server/test_builtin_plugins.py`: for the Core manifest, every `icon` (plugin, cards, actions, presets) matches `^(fas|far|fab):[a-z0-9-]+$` and `read_builtin_ref(icon)` is not None; the folder `icons` does not exist.

- [ ] **Step 2: Run it.** Expected: FAIL (still `icons/blank`, `icons/nothing`).

- [ ] **Step 3: Implement.** Mapping: plugin and Blank → `far:square`; Do Nothing → `fas:ban`; HTTP Request (action and preset) → `fas:globe`. For the decker-plugins plugins pick a Font Awesome icon per `icons/<name>` (counter → `fas:calculator`, pages → `fas:file`, profiles → `fas:layer-group`, utilities → `fas:toolbox`), reusing the one already used by the same plugin's presets where one exists; each chosen name must exist in `src/decker/assets/fontawesome/`. Delete the icon files that nothing references any more (grep each before deleting) and bump each changed decker-plugins plugin's version.

- [ ] **Step 4: Run** `poe test` in decker, and the plugin tests in `decker-plugins` (`uv run pytest`). Expected: PASS. **Commit** (one per repo) `refactor: use built-in Font Awesome icons for plugins`.

---

### Task 4: Key context menu

**Files:**
- Modify: `src/decker/client/widgets/button_grid.py` (`KeyButtonWidget` signal and `contextMenuEvent`; `ButtonGridWidget` forwards it, near lines 153-176 and 514-560)
- Modify: `src/decker/client/views/dashboard.py` (connect near line 208; handler near `_on_key_delete_requested`)
- Test: `tests/client/widgets/test_button_grid.py`, `tests/client/views/test_dashboard.py`

**Interfaces:**
- Consumes: `KeyButtonWidget._draggable` (True for keys with a card), `KeyButtonWidget.delete_clicked`, `CardConfigPanel.show_gesture_tab(gesture: str)`, gesture names `"press"`, `"double_press"`, `"long_press"`.
- Produces: `KeyButtonWidget.gesture_edit_requested = pyqtSignal(int, str)`; `ButtonGridWidget.key_gesture_edit_requested = pyqtSignal(int, str)`; `DashboardView._on_key_gesture_edit_requested(self, index: int, gesture: str) -> None`.

- [ ] **Step 1: Write the failing tests.**
  - `test_context_menu_lists_gestures_and_delete`: for a draggable key, build the menu (expose `KeyButtonWidget._build_context_menu() -> QMenu`) and assert its action texts, ignoring separators, are `["Press", "Double Press", "Long Press", "Delete Card"]` with exactly one separator between Long Press and Delete Card.
  - `test_context_menu_actions_emit`: triggering "Long Press" emits `gesture_edit_requested(index, "long_press")`; triggering "Delete Card" emits `delete_clicked(index)`.
  - `test_empty_key_has_no_context_menu`: a non-draggable key's `_build_context_menu()` returns `None`.
  - Dashboard: `test_gesture_edit_request_selects_key_and_shows_tab`: with a key in `_key_slots`, calling `_on_key_gesture_edit_requested(2, "long_press")` selects key 2 for editing and the config panel's current gesture tab is Long Press.

- [ ] **Step 2: Run** those tests. Expected: FAIL (names undefined).

- [ ] **Step 3: Implement.** `contextMenuEvent` pops `_build_context_menu()` at the global position when it is not None. Selecting a gesture reuses the edit path: `_on_key_gesture_edit_requested` calls `_on_key_edit_requested(index)` and then `self._config_panel.show_gesture_tab(gesture)` (if the editor loads asynchronously, set the pending gesture the way `_press_tab_after_refresh` does, generalised to a `_gesture_tab_after_refresh: str | None`). Delete Card connects to the existing `delete_clicked` signal so the confirmation and `key_unassign` are the shared flow.

- [ ] **Step 4: Run** `poe test`. Expected: PASS. **Commit** `feat: key right-click menu with gesture tabs and Delete Card`.

---

### Task 5: Docs and gates

**Files:**
- Modify: `CLAUDE.md` (built-in plugins paragraph: Core now carries the Blank card, `com.decker.core.blank`, legacy ids aliased; the key grid bullet: right-click menu; the note on icons)
- Modify: `docs/plugins/package-and-manifest.md` (replace `com.decker.blank` references; mention `fas:` icon refs for plugin icons)

- [ ] **Step 1:** Update both files as described; search `docs/` and `CLAUDE.md` for `com.decker.blank` and fix each live reference (leave dated specs and plans alone).
- [ ] **Step 2: Run all gates**: `poe format && poe mypy && poe pylint && poe test`, then `mypy --platform win32 .` and `mypy --platform darwin .`. Expected: all pass, coverage ≥ 85%.
- [ ] **Step 3: Commit** `docs: Blank lives in Core; key context menu; built-in icons`.
