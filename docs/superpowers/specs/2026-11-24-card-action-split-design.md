# Card / action split and card editor tabs — design

Status: draft for approval (short design). Date: 2026-11-24. Touches Decker (client) and decker-plugins.

## Intent

The line between a **card** and an **action** has blurred. An action is headless work. A card is only worth being a
card when it gives the user something a Blank card plus an action cannot: a drawing surface for running code (a graph,
a countdown, a level), its own refresh, or state shared with its actions. Several plugins ship "preset" cards that are
only a Blank card with an action bound (Hotkey, Brightness Up, Next Page). They crowd the Cards tab and duplicate the
Actions tab, which already drops a Blank card bound to the dragged action.

This change cleans that up for the alpha release. Presets come back later as a deliberate feature (next push).

## Definition

A card is real only if it draws something a Blank card cannot, or owns state or refresh. Everything else is an action,
reached from the Actions tab.

## Goals

1. Remove preset cards from the plugin manifests and code (Approach A: delete, no migration).
2. Plugin-scoped actions stay out of the Actions tab; they are reachable only from their own card's binding dropdowns
   (already true: the tab lists global actions only).
3. Reorder the binding dropdown: **Do Nothing**, then **card actions**, then **built-in actions**, then **global
   actions**.
4. Card editor tabs: **Card, Press, Double Press, Long Press**, each with an icon, and an `*` after the tab text when
   its settings differ from their default.

## Non-goals

- Presets (a card that is declared as Blank + action). Next push.
- Migrating stored keys. A key that names a deleted card loads empty, as retired cards already do in format 3.
- Per-card exported variables (`{weather-card-url}`). Separate spec.
- New plugins from the todo list (System, Weather, Sensors).

## Anti-goals

- A new manifest concept that only exists to support presets before they are designed.
- Losing any action: every action behind a removed card stays available as a global action.

## Plugin changes (decker-plugins)

Delete these cards; keep their actions.

| Plugin | Cards removed | Cards kept |
|--------|---------------|------------|
| `com.decker.pages` | next, previous, goto | none (plugin becomes actions only) |
| `com.decker.profiles` | next, previous, goto | none (plugin becomes actions only) |
| `com.decker.utilities` | page, profile, brightness-up, brightness-down, deck-brightness-up, deck-brightness-down | counter, brightness-level, deck-brightness-level, sleep, timer, stopwatch |

Notes:

- The pages and profiles `next`/`previous` cards draw a caret in a chosen colour, and `goto` titles itself from the
  chosen page. These go with the cards (accepted: clean up first). The user recreates them from the Actions tab and the
  per-key icon/title overrides; presets will restore the convenience.
- Dead code goes too: card classes, caret rendering and manifest `settings` used only by removed cards, plus their
  tests. The pages and profiles plugins keep actions only, so their `runtime` stays (actions need it).
- `com.decker.counter` (old plugin) is untouched; Utilities' counter is a real card.
- Version bumps and the README plugin table follow the changes. The Utilities plan
  (`docs/superpowers/plans/2026-11-24-utilities-plugin.md`) is updated to drop its preset cards before it merges.

## Decker changes

### Binding dropdown order (`client/widgets/action_edit.py`)

`ActionEdit._shown_actions` and `_rebuild` replace the single alphabetical-by-plugin list with four sections for a
card's binding dropdown (owner set):

1. Do Nothing (as now).
2. Card actions: every action of the card's own plugin, either scope, under a header naming the plugin.
3. Built-in actions: the built-in Core plugin's actions except Do Nothing (HTTP Request, Delay, Sequence), under a
   "Built-in" header. Core is not repeated in the global section.
4. Global actions: other plugins' global actions, grouped by plugin as now, under a "Global" header, each plugin under
   its own sub-header.

The ownerless use (no card: the "None" entry) keeps working with the same order minus section 2. Within a section,
sort by action name. A stored value that is no longer listed still shows as disabled "Missing", as now.

These section placements are the design's own calls, not the user's: own-plugin global actions sit in "card actions"
only (not repeated under Global); the Core plugin is identified by id `com.decker.core`.

### Card editor tabs (`client/widgets/card_config_panel.py`, `bindings_edit.py`)

- Replace the Card/Actions pair with four tabs: Card (`fas:image`), Press (`fas:caret-right`), Double Press
  (`fas:caret-square-right`), Long Press (`fas:hourglass`). Icons come from the bundled Font Awesome, drawn through the
  existing icon loader.
- Each of Press, Double Press and Long Press holds what one binding group holds today: the `Action:` dropdown, the
  reset-to-default button and the action's argument form.
- A tab's text gets a trailing ` *` when modified: Card when any setting differs from its schema default; a gesture
  tab when its effective binding (action or arguments) differs from the manifest default (`effective_bindings`
  against the card's manifest `bindings`; an omitted default is Do Nothing). The mark updates live while editing.
- The Card tab is hidden when the card has no settings (as the current Actions tab is hidden for ownerless cards);
  the gesture tabs are hidden when the card has no bindings model, as today.
- The editor keeps the preview, Save and appearance rows above the tabs.

## Docs and tests

- `docs/plugins/` and `CLAUDE.md` describe the card definition and the dropdown order; the picker paragraph in
  `CLAUDE.md` is updated for the tab layout.
- Tests: `ActionEdit` ordering and sections (including the ownerless case and a missing value), tab labels and the
  `*` mark against defaults, and the plugin tests reduced to what remains.

## Approaches considered

- **A. Delete preset cards** (chosen by the user, for all plugins). Clean, no new concept; keys using them load empty,
  acceptable for an alpha.
- **B. Mark presets and hide them from the picker.** Keeps existing keys working and adds the Blank + action signal,
  but adds a manifest field and leaves dead cards. Deferred to the next push.
- **C. Delete and migrate stored keys to Blank + action.** Loses nothing, most work. Possibly later.

## Left to the builder

Exact header wording and styling in the dropdown; how the `*` is computed efficiently (compare against the manifest
default on each edit); whether pages/profiles keep their plugin ids and versions (bump minor); the mechanics of
removing dead settings and icons.
