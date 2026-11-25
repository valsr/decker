# Card presets, Actions tab removal and default white icons — design

Date: 2026-11-24. Touches Decker (server, client, built-in plugins, docs) and decker-plugins (Pages, Profiles,
Utilities). Follows `2026-11-24-card-action-split-design.md`, which deleted the preset cards and said presets would
come back as a deliberate feature. This is that feature.

## Intent

Users want a key like "Next page" or "Brightness up" in one drag. Today that means finding the action in the Actions
tab (global actions only), and the key then carries the action's name and icon by accident rather than by design. A
plugin author cannot say "ship a Next page key with this title and this icon". Presets give plugin authors that:
a preset is a **Blank card plus a name, an icon and gesture bindings (actions with arguments)**, declared in the
manifest and listed in the picker's Cards tab. With every ready-made key in one place, the Actions tab goes away.

Also in this change: SVG icons with no colour of their own draw **white** on a key, not black (black on a black key
is invisible, and it hits the Font Awesome icons Utilities uses).

## Goals

1. A manifest field `contributes.presets[]` (format 3 stays; a plugin that uses it declares a `"decker"` constraint
   for the Decker version that adds it, as `options_source` and `variables` do).
2. The picker's Cards tab lists presets next to cards, grouped under their plugin. Dropping one assigns a **Blank**
   card carrying the preset's bindings and appearance. The result is a plain Blank key: nothing links it to the preset
   afterwards.
3. Remove the Actions tab of the picker and its drag path (`_action_drag`, `preset_action`, the `Actions` pane and
   `set_actions` in the sidebar, the client's `plugins_actions_list` use for the tab). The `plugins_actions_list` RPC
   stays: the binding dropdowns use it.
4. Migrate what exists:
   - **Core**: presets for HTTP Request, Delay and Sequence (they were reachable only from the Actions tab).
   - **Pages**: presets Next page, Previous page, Go to page, **Page Name** and **Page Count**. Page Name and Page
     Count (static cards in Blank today) move here; Blank keeps only the Blank card.
   - **Profiles**: presets Next profile, Previous profile, Go to profile, Profile Name (title `{current_profile}`).
   - **Utilities**: see "Utilities sort" below.
5. SVG key icons with no colour set draw white by default.

## Non-goals

- Keeping a key linked to its preset (a later plugin update changing keys already placed). Dropped keys are copies.
- Migrating stored keys: a key that names `com.decker.blank.pagename` or `.pagecount` loads empty after this change
  (the same alpha rule as retired cards in the split design); users re-drop the Pages preset. No compatibility
  alias.
- Users saving their own presets from an existing key ("save as preset"). Possible later; the manifest shape should
  not rule it out.
- Presets of anything other than Blank (a preset of a plugin's own drawing card). A preset is always Blank-based.
- Presets in the repository index format.
- Per-preset settings forms. A preset's arguments live in its bindings and are edited in the card editor afterwards.

## Anti-goals

- A preset must not become a second way to write a card: no `settings`, no process, no states. If something needs code
  to draw, it is a card.
- Removing the Actions tab must not strand an action: every global action is still reachable from a card's Action
  dropdown, and the ones that make a sensible key have a preset.
- White-by-default must not recolour anything that already has a colour: an SVG that sets its own fill, stroke or a
  user-chosen icon colour keeps it, and raster icons are untouched.

## Constraints

- Manifest and binding rules from `docs/plugins/package-and-manifest.md`: gestures are exactly `press`,
  `double_press`, `long_press`; a binding may name the plugin's own actions of either scope, any `global` action or Do
  Nothing (a foreign `plugin`-scoped action is refused at load).
- Key appearance `icon` values are refs (`pack:<pack id>/<path>` or `fas:play`), not plugin-relative file paths. A
  preset's picker icon (`icon`) may be a plugin file; the key icon (`appearance.icon`) may not.
- Static cards may already declare `appearance` and `bindings` (Blank's Page Name uses `appearance.title`). The preset
  mechanism reuses the same appearance and bindings parsing and validation.
- Code style and gates from `CLAUDE.md`; docs in `docs/plugins/` and `CLAUDE.md` stay in step with the manifest.

## Approaches

- **A. A new `contributes.presets[]` list (chosen).** Authored name, icon, appearance and bindings; the server serves a
  list; the client drops a Blank key with those values. One new manifest concept, one RPC.
- **B. Presets as flagged entries in `contributes.cards`.** Reuses the card list and picker, but cards are meant to
  draw something, and the split design just removed that blur. Rejected.
- **C. Auto-generate a preset per global action, no manifest field.** Least work, but no authored title, icon or
  arguments, so "Next page" could not differ from "Go to page". Rejected.

## Manifest: `contributes.presets[]`

```json
{
  "id": "com.decker.pages.next",
  "name": "Next page",
  "tooltip": "Go to the next page",
  "icon": "fas:forward",
  "appearance": { "title": { "text": "Next" }, "icon": { "ref": "fas:forward", "color": "#ffffff" } },
  "bindings": { "press": { "action": "com.decker.pages.next", "args": { "wrap": true } } }
}
```

| Field | Notes |
|-------|-------|
| `id` | Required; starts with `<plugin id>.`; unique among the plugin's presets, cards and actions (a preset may share the suffix of the action it wraps, as above, only if the full ids differ). |
| `name`, `tooltip` | Display data for the picker. |
| `icon` | The picker thumbnail: a plugin file or a built-in ref. Not copied to the key. |
| `appearance` | Same shape as a card's `appearance`; copied to the key as its stored appearance. `appearance.icon.ref` must be a ref, not a file path. If the preset gives no `appearance.title.text`, the key's title is the preset `name`. If it gives no `appearance.icon`, the key shows the press action's icon, as an action-bound Blank key does today. |
| `bindings` | Same shape and validation as a card's `bindings`. Arguments may be left incomplete (for example no page for Go to page); the user fills them in the editor. A preset may have no bindings at all (Page Name, Page Count). |

A preset may not declare `settings`, `states`, `static` or `gestures`. The plugin needs no `runtime` for presets
alone (the bound actions run in their own plugins, as for static cards).

Manifest validation: id prefix, unknown keys refused, bindings checked like a card's (own-plugin ids must exist, no
foreign `plugin`-scoped action), `appearance` checked like a card's. A plugin that declares presets must declare a
`"decker"` constraint that guarantees the Decker version adding them.

## Server

- `PluginManager.list_presets()` and an RPC `plugins_presets_list` (same shape as `plugins_cards_list`): per preset the
  plugin uuid and name, preset id and name, tooltip, picker icon (base64, like `icon_b64` for actions), appearance
  and bindings. A preset whose bound actions are not all currently usable by its plugin (action missing, plugin not
  loaded) is left out of the list; it comes back when the action does.
- An RPC `key_assign_preset(device_id, page_id, key_index, plugin_uuid, preset_id, context_id)` that assigns the Blank
  card and stores the preset's bindings and appearance on the key in one step (a failed half-assignment must not
  leave a Blank key without its bindings). It returns whether the preset's bindings pass strict validation, so the
  client knows when to open the editor (see Client). It is not loopback-gated, like `key_assign`.
- Presets of the built-in Core plugin come from `com.decker.core`'s manifest like any other plugin's.

## Client

- The picker has one list, Cards, holding cards and presets (group by plugin; a preset shows its plugin's name like a
  card does; search matches name and plugin). The Actions tab, its pane, its tab icon use and its search placeholder
  go. Presets drag like cards; the drop calls `key_assign_preset`.
- After dropping a preset whose bindings do not yet validate (a required argument is unset, such as Go to page's page),
  the key editor opens on the Press tab so the user can fill it in. A key that validates opens nothing, as a card
  drop does today. (My call.)
- `_on_card_dropped` drops the `preset_action` branch and gets a sibling for presets.
- The binding dropdowns (`action_edit.py`) are unchanged.

## Default white icons

In the key-face composition (`server/appearance.py` `compose_key_face`, fed from `server/plugins.py` `_compose`), an
SVG icon drawn with no `image_color` is tinted white (`#ffffff`) with the existing `decker.asset.tint.tint_svg`
instead of drawing in the SVG's default (black). `tint_svg` only checks the root `<svg>` tag for a `fill`, so the
white default needs its own check (my call): an SVG counts as **unstyled** when no element anywhere in the file sets a
`fill`, `stroke`, `style` or `<style>` colour other than `none`/`currentColor`; only unstyled SVGs are tinted. A
partly coloured SVG (some shapes filled, others relying on the default black) is left exactly as it is, and
`currentColor` follows the white. Raster images and animated GIFs are untouched; a user's icon colour or a card's
`appearance.icon.color` still wins. The editor preview uses `key_preview`, which composes the same way, so it matches.
Picker thumbnails keep the existing theme tint.

## Sequencing (my call)

Two repos, two PRs. Decker first (manifest field, RPCs, client, Core presets, Blank change, white icons, docs, version
bump). Then decker-plugins: move the `decker` pin in `pyproject.toml` to the Decker commit that has presets, and add the
Pages, Profiles and Utilities presets with their version bumps. The plugin releases (new `"decker"` constraint) wait
for the Decker release. Page Name and Page Count disappear from Blank in the Decker change and return with the Pages
release, so the two ship together.

## Utilities sort

Rule: a **card** draws something or owns state or refresh; an action is **global** when it makes sense on any
plugin's key and **local** (`plugin` scope) only when it works on its own card's state. Presets are the ready-made
keys for global actions that make a sensible one-press key.

| Item | Kind | Notes |
|------|------|-------|
| Counter, Brightness Level, Deck Brightness Level, Sleep, Timer, Stopwatch | cards | unchanged |
| `counter-increment`/`-decrement`/`-reset`, `sleep-press`, `sleep-after`, `timer-toggle`/`-reset`, `stopwatch-toggle`/`-reset` | local actions | they act on their own card's state (unchanged) |
| `brightness`, `deck-brightness`, `sleep`, `lock`, `sleep-cancel` | global actions | unchanged |
| `page`, `profile` | global actions | to compare with Pages and Profiles actions (see builder notes) |
| Presets (new) | Brightness Up, Brightness Down, Deck Brightness Up, Deck Brightness Down, Lock screen | each a Blank key bound to the matching global action on press, with a title and an icon |

No preset for plain Sleep: an accidental press sleeps the PC, and the Sleep card (with its cancellable countdown) is
the intended key. It stays reachable from a Blank key's Action dropdown. (My call.)

## Version, docs and tests

- Decker version bumps to the next minor; Core and Blank manifests follow; Pages, Profiles, Utilities and the
  `decker-plugins` README table follow with version bumps (Pages, Profiles, Utilities: new `"decker"` constraint).
- `docs/plugins/package-and-manifest.md` gets a `presets[]` section and drops the Actions-tab sentences; the
  `CLAUDE.md` plugins paragraph and picker paragraph are updated.
- Tests: manifest parsing and validation of presets; `list_presets` filtering; `key_assign_preset` (success,
  half-failure, unknown preset); client drop and editor-open; the white default (SVG without fill turns white, SVG with
  its own fill unchanged, PNG unchanged, explicit colour wins); plugin tests for the new manifests.

## Left to the builder

Exact RPC and field names beyond those given here; how a preset's picker icon is encoded for the client; wording and
styling of the merged list (group headers or a plugin label on each tile); what happens to an open Actions-tab
search; the new version number; whether `key_assign_preset` shares code with `key_assign`; whether the Utilities
`page`/`profile` actions duplicate the Pages and Profiles ones (report what you find in the plan; do not delete them in
this change without checking with the user).
