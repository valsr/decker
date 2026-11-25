# Card-local actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A card's gesture dropdown lists under "Card actions" only the plugin-scoped actions that card declares (plus plugin-scoped actions no card of the plugin declares).

**Architecture:** `CardSpec.actions` (manifest `cards[].actions`) is validated by the loader. `ActionRunner.listing` adds `cards` (ids of the cards that list the action) to every item of `plugins_actions_list`. The client passes the card id from the dashboard slot (`slot["card_uuid"]`) through `CardConfigPanel` and `BindingsEdit` to `ActionEdit`, which filters the Card actions section.

**Tech Stack:** Python 3.14, PyQt6, pytest + assertpy, black 120, mypy (default, win32, darwin), pylint >= 9.0, poe.

**Spec:** `docs/superpowers/specs/2026-11-25-card-local-actions-design.md`

## Global Constraints

- Decker version `0.8.1`; a plugin using `cards[].actions` declares `"decker": ">=0.8.1"`.
- `cards[].actions` lists ids of the plugin's own actions with `scope: "plugin"`; unknown ids, global ids and duplicates are `ManifestValidationError`s; presets cannot declare `actions`.
- A plugin-scoped action listed by no card of its plugin stays visible on every card of that plugin.
- The currently bound action stays listed in the dropdown even when not offered (never shown as "(missing action)" while installed).
- Section order stays: Do Nothing, Card actions, Built-in, Global (every plugin's global actions, the owner's included). Schema-form `ActionEdit`s (no card id) behave exactly as now.
- No change to run-time binding resolution or stored bindings.
- Quality gates before done: `poe format`, `poe mypy` plus `uv run mypy --platform win32 .` and `--platform darwin .`, `poe pylint` (>= 9.0), `poe test`.

## Review Focus

- A card that lists no actions in a plugin where other cards do: it must see the unlisted plugin-scoped actions only (Task 2 test).
- A stored binding to an action the card does not list (e.g. set before the manifest change): shown by name, not "(missing action)" (Task 3 test).
- A plugin-scoped action listed by two cards: shown on both, on neither's sibling (Task 2 test).
- A card id that is empty or unknown (Blank key, or the plugin failed to load): falls back to all of the plugin's plugin-scoped actions, no crash (Task 3 test).

---

### Task 1: Manifest field `cards[].actions`

**Files:**
- Modify: `src/decker/plugin/manifest.py` (`CardSpec`, `_parse_card`, manifest-level check beside `_check_preset_ids_unique`)
- Modify: `docs/plugins/package-and-manifest.md` (`cards[]` table)
- Test: `tests/plugin/test_manifest.py` (follow the existing preset/card tests there)

**Interfaces:**
- Produces: `CardSpec.actions: tuple[str, ...] = ()` (full ids as in `contributes.actions`).

- [ ] **Step 1: Write failing tests:** `test_card_actions_are_parsed` (ids kept in order), `test_card_actions_unknown_id_rejected`, `test_card_actions_global_action_rejected`, `test_card_actions_duplicate_rejected`, `test_preset_cannot_declare_actions`, `test_card_actions_default_empty`. Error messages contain the card id and the offending action id.
- [ ] **Step 2:** Run `uv run pytest tests/plugin/test_manifest.py -q` → FAIL.
- [ ] **Step 3:** Add the field; parse `entry.get("actions", [])` as a list of strings with `_parse_contribution_id(..., "card action id")`-style full-id handling matching how other ids are written in the manifest; validate against `contributes.actions` (scope `"plugin"`) once actions are parsed (a function `_check_card_actions(cards, actions)` called after both are built). Reject `actions` in `_parse_preset`'s existing unknown-key refusal.
- [ ] **Step 4:** Run the same command → PASS. Document the field in the `cards[]` table (optional, plugin-scoped actions only, requires `decker >=0.8.1`, fallback rule).
- [ ] **Step 5: Commit** `feat(plugin): cards declare their own actions`.

### Task 2: Server listing carries `cards`

**Files:**
- Modify: `src/decker/server/actions.py` (`ActionRunner.listing`)
- Modify: `src/decker/server/plugins.py` only if `listing` needs the cards of each plugin passed in (`list_actions`)
- Test: `tests/server/test_plugins.py` or the file that already asserts `list_actions` items

**Interfaces:**
- Produces: each `plugins_actions_list` item gains `"cards": list[str]` — ids of the cards in the action's own plugin whose `actions` contains it (empty when none does).

- [ ] **Step 1: Write failing tests:** `test_listing_cards_empty_when_unlisted`, `test_listing_cards_for_shared_action` (listed by two cards → both ids, manifest order), `test_listing_cards_global_action_is_empty`.
- [ ] **Step 2:** Run the file → FAIL.
- [ ] **Step 3:** Compute `cards` from `plugin.manifest.cards` in `listing`; keep every existing key.
- [ ] **Step 4:** Run → PASS; the RPC docs/CLI listing mention (grep `plugins_actions_list` in docs) gains the field.
- [ ] **Step 5: Commit** `feat(server): actions list says which cards declare them`.

### Task 3: Client filters Card actions by card id; version 0.8.1; docs

**Files:**
- Modify: `src/decker/client/widgets/action_edit.py` (`ActionEdit.__init__`, `set_owner(plugin_id: str, card_id: str = "")`, `_sections`)
- Modify: `src/decker/client/widgets/bindings_edit.py` (`BindingsEdit.set_bindings(spec, plugin_id, card_id="")`, `load(default, stored, plugin_id, card_id="")`)
- Modify: `src/decker/client/widgets/card_config_panel.py` (`show_card_config(..., plugin_id, card_id="", bindings)`; include `card_id` in the `loaded` tuple)
- Modify: `src/decker/client/views/dashboard.py` (~line 738: `card_id=str(slot.get("card_uuid") or "")`)
- Modify: `pyproject.toml` and wherever 0.8.0 is asserted (`grep -rn "0.8.0"`) → `0.8.1`; `CLAUDE.md` (card editor sentence: Card actions = the card's declared plugin-scoped actions plus unlisted ones), `docs/plugins/package-and-manifest.md` if not done
- Test: `tests/client/widgets/test_action_edit.py`, `test_bindings_edit.py`, `test_card_config_panel.py`

**Interfaces:**
- Consumes: action dicts with `"cards": list[str]` (missing key treated as `[]`).
- Produces: Card actions = the owner's plugin-scoped actions where `cards` is empty or contains `card_id`; with `card_id == ""` every owner plugin-scoped action (current behaviour). The action equal to the current value is always included even when filtered out.

- [ ] **Step 1: Write failing tests** in `test_action_edit.py` (extend the `_OWNER` fixtures with `cards`): `test_card_actions_limited_to_the_card`, `test_unlisted_plugin_actions_visible_on_every_card`, `test_shared_action_on_both_cards`, `test_bound_action_stays_listed_when_not_offered` (value set to another card's action → shown by name, no "(missing action)"), `test_empty_card_id_lists_all_plugin_actions`, `test_unknown_card_id_lists_unlisted_only`. In `test_card_config_panel.py`: `test_card_id_reaches_the_dropdown`.
- [ ] **Step 2:** Run `uv run pytest tests/client -q` → FAIL.
- [ ] **Step 3:** Implement as the Interfaces block; the bound-action rule belongs in `_sections` (add the action whose id equals `self._value` to Card actions if it is the owner's plugin-scoped action that was filtered out). Plumb `card_id` through the three widgets and the dashboard.
- [ ] **Step 4:** Run `uv run pytest tests/client -q` → PASS; then run all four gates plus win32/darwin mypy.
- [ ] **Step 5: Commit** `feat(client): card actions follow the card, Decker 0.8.1`.

### Task 4: decker-plugins — declare card actions (separate PR, after Decker merges)

**Files:**
- Modify: `/work/decker/decker-plugins/pyproject.toml` (pin the merged Decker commit) and `uv.lock`
- Modify: `plugins/com.decker.utilities/manifest.json` (Counter, Sleep, Timer, Stopwatch cards declare `actions`; version `2.1.1`, `"decker": ">=0.8.1"`), `plugins/com.decker.counter/manifest.json` (its card declares the counter actions if it has plugin-scoped ones; version patch bump, `>=0.8.1`)
- Test: each plugin's `tests/test_manifest.py`

**Interfaces:**
- Consumes: `CardSpec.actions` from Task 1 via the pinned Decker.
- Produces: Counter card → `counter-increment`, `counter-decrement`, `counter-reset`; Sleep card → `sleep-press`, `sleep-after`; Timer card → `timer-toggle`, `timer-reset`; Stopwatch card → `stopwatch-toggle`, `stopwatch-reset` (use the full ids as the manifest spells them; Brightness cards declare none).

- [ ] **Step 1: Write failing tests:** `test_cards_declare_their_actions` (the mapping above), `test_every_plugin_action_is_claimed_by_a_card` (no plugin-scoped action left unlisted), version/`decker` assertions to the new values.
- [ ] **Step 2:** Move the pin, `uv sync`, run `uv run pytest -q` → FAIL.
- [ ] **Step 3:** Edit the manifests; update the README plugin table versions and the `>=0.8.1` mention.
- [ ] **Step 4:** Run `uv run pytest -q` and `ruff format --check .` / `ruff check .` → PASS.
- [ ] **Step 5: Commit** `feat(utilities,counter): cards declare their own actions`.
