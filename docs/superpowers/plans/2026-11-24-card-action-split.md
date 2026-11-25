# Card / action split and card editor tabs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove preset cards from the Pages, Profiles and Utilities plugins, reorder the binding dropdown (Do Nothing, card, built-in, global), and give the card editor Card / Press / Double Press / Long Press tabs with a modified `*` mark.

**Architecture:** Client-only changes in Decker (`action_edit.py`, `bindings_edit.py`, `card_config_panel.py`, `card_icons.py`, `schema_form.py`) plus manifest/code deletions in the decker-plugins repo. No server, protocol or manifest-format change. Two repos, two branches, independent of each other.

**Tech Stack:** Python 3.14, PyQt6, pytest + assertpy, black/isort/mypy/pylint (Decker); pytest + assertpy, ruff (decker-plugins).

**Spec:** `docs/superpowers/specs/2026-11-24-card-action-split-design.md` (Decker repo).

## Global Constraints

- Decker gates before done: `poe format`, `poe mypy`, `poe pylint` (≥ 9.0), `poe test` (coverage ≥ 85%), plus `mypy --platform win32 .` and `mypy --platform darwin .`. Methods < 50 lines, ≤ 5 args, annotate everything.
- decker-plugins gates: `uv run pytest plugins/<id>` and `uv run ruff check plugins/<id>` per touched plugin.
- Tab names and icons: Card `fas:image`, Press `fas:caret-right`, Double Press `fas:caret-square-right`, Long Press `fas:hourglass`. Modified tab text is `"<name> *"` (one space, asterisk after the text).
- Dropdown order: Do Nothing, card actions, built-in actions (Core plugin `com.decker.core`, minus `com.decker.core.nothing`), global actions of other plugins grouped by plugin.
- Plugin-scoped actions never appear in the sidebar Actions tab (already true: it lists global actions only); do not change `sidebar.py`.
- Removed cards are deleted with no migration; actions behind them stay global and unchanged.
- Branches: `feat/card-action-split` in each repo, cut from `main`. Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

1. Offline server / owner with no actions: the dropdown shows only Do Nothing, with no empty section headers (Task 1).
2. A stored binding naming an uninstalled or foreign plugin-scoped action still shows `(missing action)` selected and disabled, and its tab is marked modified (Tasks 1, 2).
3. A card with neither settings nor bindings (a plain Blank card): the editor shows the "This card has no settings" text, not an empty tab bar (Task 2).
4. After Save, or after the row's ↺ reset, a gesture tab's `*` clears without reopening the key (Task 2).
5. Pages and Profiles manifests with no `cards` still load, install and keep their actions' runtime (Task 4).

---

### Task 1: Binding dropdown sections

**Files:**
- Modify: `src/decker/client/widgets/action_edit.py` (`_shown_actions`, `_rebuild`, class docstring)
- Test: `tests/client/widgets/test_bindings_edit.py` (the dropdown tests at the top, lines ~94-140) and a new `tests/client/widgets/test_action_edit.py` if the existing file has no `ActionEdit`-only tests

**Interfaces:**
- Produces: `ActionEdit` item order for `owner` set: `Do Nothing`, header `<owner plugin name>` + owner's actions (either scope, sorted by `action_name`), header `Built-in` + Core actions except Do Nothing, header `Global` + one sub-header per other plugin (alphabetical by plugin label) with its global actions. Headers are disabled, bold items (existing `_add_group_header`). For `owner is None`: `(choose an action)`, then Built-in, then Global (no card section). A section with no actions adds no header.
- Constant: `_CORE_PLUGIN_ID = "com.decker.core"` in `action_edit.py`.

- [ ] **Step 1: Branch.** `git checkout -b feat/card-action-split` in `/work/decker/decker`.
- [ ] **Step 2: Write failing tests** with a fake `rpc` returning `plugins_actions_list` JSON (follow the fixtures already in `test_bindings_edit.py`). Actions: owner plugin `com.x.owner` with one `scope: plugin` and one `scope: global` action; Core with `nothing` and `delay` (global); plugin `com.y.other` with one global and one plugin-scoped action. Tests:
  - `test_sections_are_nothing_card_builtin_global`: item texts, in order, equal `["Do Nothing", "<Owner name>", <owner plugin action>, <owner global action>, "Built-in", "Delay", "Global", "<Other name>", <other global action>]` (headers disabled via `model().item(i).isEnabled()`).
  - `test_other_plugins_scoped_actions_are_not_listed`: the other plugin's `scope: plugin` action is absent.
  - `test_owner_global_action_is_listed_once`: appears under the card section only.
  - `test_empty_sections_have_no_header`: owner with no actions and no other plugins gives exactly `["Do Nothing", "Built-in", "Delay"]` when Core is present, and `["Do Nothing"]` when it is not (Review Focus 1).
  - `test_missing_value_still_shown_disabled` (Review Focus 2): keep or adapt the existing missing-action test to the new order.
  - `test_ownerless_has_no_card_section`: `owner=None` gives `(choose an action)`, `Built-in`..., `Global`...
- [ ] **Step 3: Run** `uv run pytest tests/client/widgets/test_bindings_edit.py -q`; expect the new order tests to FAIL and the old plugin-grouped expectations to need updating.
- [ ] **Step 4: Implement** in `action_edit.py`: replace `_shown_actions` with `_sections() -> list[tuple[str, list[dict[str, Any]]]]` (header title, actions) per the Interfaces block, and have `_rebuild` walk it, adding a header per non-empty section and, inside Global only, a sub-header per plugin. Keep `_nothing_icon`, the missing-value handling and `listed` bookkeeping (use the actions actually added). Update the class docstring.
- [ ] **Step 5: Run** `uv run pytest tests/client/widgets/test_bindings_edit.py tests/client/widgets/test_schema_form.py -q`; expect PASS (`ActionParamsEdit` and generic action fields use `ActionEdit`; fix expectations that assumed the old order).
- [ ] **Step 6: Commit** `feat(client): order binding dropdown card, built-in, global`.

---

### Task 2: Card editor tabs with modified marks

**Files:**
- Modify: `src/decker/client/widgets/bindings_edit.py`, `src/decker/client/widgets/card_config_panel.py`, `src/decker/client/widgets/schema_form.py`, `src/decker/client/card_icons.py`
- Test: `tests/client/widgets/test_bindings_edit.py`, `tests/client/widgets/test_card_config_panel.py`, `tests/client/widgets/test_schema_form.py`, `tests/client/test_card_icons.py`

**Interfaces:**
- `card_icons.tab_icon(ref: str, size: int = 16) -> QIcon`: a bundled Font Awesome icon (`read_builtin_ref(ref)` bytes, base64 into `decode_icon` with the application palette's text colour as tint, scaled to `size`); an empty `QIcon()` for an unknown ref.
- `schema_form.SchemaForm.defaults() -> dict[str, Any]`: what `values()` returns for the current schema with no stored values (build the fields with an empty values dict on throwaway widgets).
- `bindings_edit.GESTURES: tuple[tuple[str, str], ...]` (public rename of `_GESTURES`) with titles `("press", "Press"), ("double_press", "Double Press"), ("long_press", "Long Press")`.
- `BindingsEdit` becomes a `QObject` (not a widget) owning the rows: `row_widget(gesture: str) -> QWidget` (the row, now a plain `QWidget` with no group title, same internals), `is_modified(gesture: str) -> bool` (`not row.is_default()`), and the existing `set_bindings`, `value`, `errors`, `has_errors`, `set_errors`, `changed`.
- `card_config_panel`: remove `ACTIONS_TAB`; `CARD_TAB = "Card"` stays; add `tab_text(name: str, modified: bool) -> str`. `self._tabs` has tabs in order Card, Press, Double Press, Long Press, each with its icon.

- [ ] **Step 1: Failing tests, `tab_icon` and `SchemaForm.defaults`:** `tab_icon("fas:image")` is non-null; `tab_icon("fas:nope")` is null. For a schema with `text` default `"a"` and `bool` default `False`, `defaults()` equals `{"t": "a", "b": False}` after the form was set to other values.
- [ ] **Step 2: Implement** both; run `uv run pytest tests/client/test_card_icons.py tests/client/widgets/test_schema_form.py -q`; expect PASS.
- [ ] **Step 3: Failing tests, panel** (extend `test_card_config_panel.py`, replacing the assertion near line 628 that expects `[CARD_TAB, ACTIONS_TAB]`):
  - `test_tabs_are_card_press_double_press_long_press`: tab texts equal `["Card", "Press", "Double Press", "Long Press"]` for a card with settings and bindings; every tab icon is non-null.
  - `test_card_tab_marked_when_a_setting_differs_from_default`: edit a setting away from its default, tab 0 text is `"Card *"`; set it back, `"Card"`.
  - `test_gesture_tab_marked_when_binding_differs_from_manifest_default`: change Press to another action, `"Press *"`; click the row's ↺ reset, `"Press"` (Review Focus 4). Double Press and Long Press stay unmarked.
  - `test_marks_clear_after_a_saving_round_trip`: after Save whose echoed `BindingsSpec.stored` equals the default for a gesture, that tab is unmarked (Review Focus 4).
  - `test_card_tab_hidden_without_settings`: schema `[]` with bindings: tab 0 not visible, current tab is Press.
  - `test_gesture_tabs_hidden_without_bindings`: `bindings=None` with settings: only the Card tab is visible.
  - `test_no_settings_and_no_bindings_shows_hint` (Review Focus 3): both absent, tab 0 is visible and contains the "This card has no settings" label.
  - `test_missing_stored_action_marks_its_tab` (Review Focus 2): stored binding naming an unlisted action gives `"Press *"`.
- [ ] **Step 4: Run** `uv run pytest tests/client/widgets/test_card_config_panel.py -q`; expect the new tests to FAIL.
- [ ] **Step 5: Implement `BindingsEdit` as a `QObject`** per the Interfaces block (keep `_BindingRow` logic; drop the `QGroupBox` title; the row is a `QWidget`). Update `tests/client/widgets/test_bindings_edit.py` for the QObject (tests that used it as a widget now show `row_widget(g)`; the "titled group" test becomes a test that each row has the Action label and reset button).
- [ ] **Step 6: Implement the panel:** in `_build_form_page`, add four tabs (`_scrolling(...)` per page; Card page keeps `_no_settings` and `_settings_form`; each gesture page is `_scrolling(self._bindings_edit.row_widget(g))`), set icons with `tab_icon`. In `_refresh_state`, set each tab's text with `tab_text`: Card modified when `self._settings_form.values() != self._settings_form.defaults()`, gesture tabs from `self._bindings_edit.is_modified(g)`; also refresh from `show_card_config`. In `show_card_config`: hide the Card tab when `not settings_schema` unless no gesture tab is visible either (then keep it, showing `_no_settings`); hide the three gesture tabs when `bindings is None`; select the first visible tab. Keep `_current_values`, dirty tracking and error routing unchanged.
- [ ] **Step 7: Run** `uv run pytest tests/client -q`; expect PASS. Then `poe format && poe mypy && poe pylint`.
- [ ] **Step 8: Commit** `feat(client): card editor Card/Press/Double Press/Long Press tabs with modified marks`.

---

### Task 3: Decker docs

**Files:**
- Modify: `CLAUDE.md` (the Plugins bullet: the picker/card-editor sentence), `docs/plugins/package-and-manifest.md` (`cards[]` intro), `docs/plugins/sdk-quickstart.md` only if it describes the editor tabs

- [ ] **Step 1:** In `CLAUDE.md` replace the "Card tab ... and an Actions tab (three titled binding groups ...)" text with the four tabs (names, icons, `*` mark, Card tab hidden without settings) and the dropdown order (Do Nothing, card actions, built-in, global grouped by plugin).
- [ ] **Step 2:** In `package-and-manifest.md` under `cards[]`, add the card definition: a card is worth shipping only if it draws something a Blank card cannot or owns state/refresh; a "Blank card plus an action" is just an action, reached from the Actions tab (a global action).
- [ ] **Step 3:** `rg -n "Actions tab|ACTIONS_TAB" docs CLAUDE.md README.md` and fix any stale mention.
- [ ] **Step 4: Commit** `docs: card/action definition and editor tabs`.

---

### Task 4: Pages and Profiles become actions-only

**Files** (repo `/work/decker/decker-plugins`, branch `feat/card-action-split` from `main`):
- Modify: `plugins/com.decker.pages/manifest.json`, `plugins/com.decker.pages/plugin.py`, `plugins/com.decker.profiles/manifest.json`, `plugins/com.decker.profiles/plugin.py`, `README.md`
- Delete: `plugins/com.decker.pages/assets/`, `plugins/com.decker.profiles/assets/` (carets and NOTICE, once nothing reads them), and the card tests
- Test: `plugins/com.decker.pages/tests/test_manifest.py`, `.../test_goto.py`, `.../test_arrows.py`, the profiles equivalents

**Interfaces:**
- Manifest: no `cards` key, `version` `"3.0.0"` (breaking: cards removed). Actions, `runtime` and icons unchanged. The three global actions per plugin (`next`, `previous`, `goto`) keep their ids and settings.

- [ ] **Step 1: Branch** `git checkout -b feat/card-action-split` in `/work/decker/decker-plugins`.
- [ ] **Step 2: Failing tests:** in each `test_manifest.py`, assert version `"3.0.0"`, `manifest.cards` is empty, the action ids and `scope == "global"` map is unchanged, and `manifest.runtime` is not None (Review Focus 5). Run `uv run pytest plugins/com.decker.pages plugins/com.decker.profiles -q`; expect FAIL on those.
- [ ] **Step 3: Implement.** Pages `plugin.py`: remove `ArrowCard`, `NextCard`, `PreviousCard`, `GotoCard`, `render_caret`, `_press_page` and the caret/colour constants and unused imports; keep the three actions and `_chosen_page`/`_needs_card`. Update the module docstring. Profiles `plugin.py`: the same for its cards (read it first; it mirrors pages). Remove the `cards` arrays from both manifests. Delete `assets/` if no remaining code reads it (`rg assets plugins/com.decker.pages`).
- [ ] **Step 4: Tests:** delete `test_arrows.py` and any card-only tests; keep and adapt action tests (the goto action tests may live in `test_goto.py`: keep those, drop the title tests). Run both plugin suites and `uv run ruff check plugins/com.decker.pages plugins/com.decker.profiles`; expect PASS.
- [ ] **Step 5:** Update `README.md`: plugin table and the test counts line (`counter 19, pages N, profiles M`) with the new numbers; mention that pages/profiles ship actions only.
- [ ] **Step 6: Commit** `feat(pages,profiles): drop preset cards, keep actions (3.0.0)`.

---

### Task 5: Utilities loses its preset cards

**Files:**
- Modify: `plugins/com.decker.utilities/manifest.json`, `plugin.py`, `navigation.py`; `docs/superpowers/plans/2026-11-24-utilities-plugin.md`; `README.md` if it lists the cards
- Delete: `plugins/com.decker.utilities/assets/` (caret SVGs, used only by the removed page/profile cards; confirm with `rg "caret|assets" plugins/com.decker.utilities --glob '!tests'`)
- Test: `plugins/com.decker.utilities/tests/test_manifest.py`, `test_navigation.py`, `test_brightness_action.py`, `test_deck_brightness.py`

**Interfaces:**
- Cards removed: `com.decker.utilities.page`, `.profile`, `.brightness-up`, `.brightness-down`, `.deck-brightness-up`, `.deck-brightness-down`. Cards kept: `counter`, `brightness-level`, `deck-brightness-level`, `sleep`, `timer`, `stopwatch`. All actions stay; version stays `1.0.0` (unreleased).
- `navigation.py` exports only the `page` and `profile` actions (and the helpers they use); `plugin.py` imports `from navigation import page, profile`.

- [ ] **Step 1: Failing test** in `test_manifest.py`: `{c.id for c in _MANIFEST.cards}` equals the six kept ids prefixed `com.decker.utilities.`; and every action id in the removed cards' bindings still exists in `_MANIFEST.actions` (for the kept cards). Run `uv run pytest plugins/com.decker.utilities/tests/test_manifest.py -q`; expect FAIL.
- [ ] **Step 2: Implement:** remove the six `cards` entries from the manifest; in `navigation.py` remove `render_caret`, the caret constants, `_press_args`, `_NavigationCard`, `PageCard`, `ProfileCard` and unused imports, keeping the two actions and their target helpers; update the module docstring and `plugin.py` import; delete `assets/`.
- [ ] **Step 3: Tests:** in `test_navigation.py` delete the card/caret tests and keep the action tests; in `test_brightness_action.py` and `test_deck_brightness.py` replace any use of the removed static `brightness-up/down` cards with direct action calls or the level cards' bindings. Run `uv run pytest plugins/com.decker.utilities -q` and `uv run ruff check plugins/com.decker.utilities`; expect PASS.
- [ ] **Step 4:** Edit the Utilities plan (`docs/superpowers/plans/2026-11-24-utilities-plugin.md`) and, if it names them, the Utilities design spec in the Decker repo: drop the Page, Profile and Up/Down card tasks and mentions; note the actions are reached from the Actions tab.
- [ ] **Step 5: Commit** `feat(utilities): drop preset cards, keep actions`.

---

## Self-review

- Spec coverage: plugin deletions (Tasks 4, 5), dropdown order (1), tabs + icons + `*` + Card-tab hiding (2), docs (3), plugin-scoped actions stay out of the Actions tab (Global Constraints; no code change needed). Utilities plan/README upkeep covered in Tasks 4-5.
- Names: `tab_icon`, `tab_text`, `SchemaForm.defaults`, `BindingsEdit.row_widget`/`is_modified`, `GESTURES`, `_CORE_PLUGIN_ID` are each defined once and used consistently.
- Proportion: the plan is mostly interfaces and test names; bodies are left to the implementer.
