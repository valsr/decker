# Card-local actions in the action dropdown — design

## Intent
A card's gesture dropdown (Press / Double Press / Long Press) should offer only that card's own plugin-scoped actions
under "Card actions". Today it offers every plugin-scoped action of the card's plugin, so the Timer card lists Sleep,
Stopwatch and Counter actions. A plugin is a collection of cards and actions; a card is not the plugin.

## Goals / non-goals / anti-goals
- Goals: the card declares which plugin-scoped actions are its own; the editor shows exactly those under "Card actions";
  the manifest loader checks the declaration.
- Non-goals: presets (a dropped preset is a plain Blank key and binds only global actions, so no card id is needed);
  changing global actions, Built-in or the Global section; changing action scoping; any migration of stored bindings.
- Anti-goals: breaking existing third-party plugins; hiding an action from a card that already has it bound (a stored
  binding keeps working at run time; the dropdown shows it as "(missing action)" only if it is not offered, so the
  currently bound action is always kept listed).

## Approaches
1. **Card lists its actions** (chosen): `cards[].actions: ["com.decker.utilities.timer-toggle", ...]`. Reads next to the card; the loader
   validates ids. Cost: a shared action is listed on each card.
2. Action lists its cards (`actions[].cards`): better for shared actions, but finding a card's actions scans all actions.
3. Infer from default bindings: no manifest change, but misses actions a card does not bind by default. Rejected.

## Design
- Manifest: `CardSpec.actions: tuple[str, ...]` (ids of the plugin's own actions). Each id must exist in the plugin's
  `contributes.actions` and have `scope: "plugin"`; duplicates and unknown or global ids are manifest errors. Presets
  cannot declare it.
- Fallback: a plugin-scoped action that no card of the plugin lists stays visible on every card of that plugin (keeps
  existing plugins unchanged). A card that declares `actions` sees exactly those plus unlisted plugin-scoped actions.
- Server: `plugins_actions_list` items gain `cards: [card ids]` (empty = unlisted, shown on every card of the plugin).
- Client: `ActionEdit` and `BindingsEdit` get the card id alongside the plugin id (`set_owner(plugin_id, card_id)`);
  "Card actions" shows the actions whose `cards` contains the card id or is empty. The dropdown always keeps the
  currently bound action listed. Schema-form action fields (e.g. Sequence steps) pass no card id: unchanged.
- Bundled plugins: Utilities (decker-plugins repo) declares `actions` on Counter, Sleep, Timer and Stopwatch cards
  (Brightness cards bind global actions, so they declare none) and bumps its version; Counter plugin likewise.
- Version: Decker 0.8.1; a plugin using `cards[].actions` declares `decker >=0.8.1` (older Decker ignores or rejects the
  field; builder to confirm the parser's behaviour and state it in the docs).
- Docs: `docs/plugins/package-and-manifest.md`, CLAUDE.md.

## Left to the builder
Exact RPC payload key name, test layout, ordering inside "Card actions" (keep name order), the "keep the bound action
listed" mechanics.
