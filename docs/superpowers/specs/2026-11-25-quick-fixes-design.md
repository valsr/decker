# Quick fixes: Blank into Core, delete on failure, built-in icons, key context menu — design

## Intent
Four small polish items from `admin/todo.user.md`, bundled because each is a few dozen lines:
the picker shows two "Built in" sections, a card whose plugin failed cannot be deleted, plugins ship
icon files that Font Awesome already covers, and keys have no right-click shortcuts.

## Goals / non-goals / anti-goals
- Goals: one "Built in" section; a key can always be cleared; fewer bundled icon files; a key context menu that opens
  the editor on a gesture tab or deletes the card.
- Non-goals: action variables, page drag, the QSS theme (separate specs); changing any other built-in behaviour.
- Anti-goals: existing keys that use Blank loading empty; deleting a card without the usual confirmation.

## Approaches
1. **Blank card moves into Core, old id kept as an alias** (chosen). Core becomes the only built-in plugin, so the
   duplicate section disappears by construction. Cost: an alias in key loading.
2. Keep two plugins and merge the sections in the sidebar by `plugin_builtin`. Smaller, but leaves two built-ins and
   does not match "move Blank to Core".
3. Move Blank and rewrite stored keys on disk once. Cleaner end state but irreversible and riskier than an alias.

## Design
- **Blank into Core.** Core's manifest gains the card `com.decker.core.blank` (name Blank, same static card); the
  `com.decker.blank` plugin folder is removed. `key_assignments.py` (`BLANK_PLUGIN_ID`/`BLANK_CARD_ID`) points at the
  new ids and maps the legacy `com.decker.blank` / `com.decker.blank.blank` to them when assignments load, so stored
  keys keep working. The existing "retired pagename/pagecount loads empty" rule stays. Presets still assign a plain
  Blank key.
- **Delete when a plugin fails.** Today the hover delete button is shown only for keys in the dashboard's
  `_key_slots`, which `_sync_slot` fills from `key_get_states`. Cause is unconfirmed: the builder reproduces with a
  broken plugin and finds where the state drops out (`complete_settings` / `appearance_for` raising or the card
  missing). Fix so `key_get_states` always returns the assignment's identity fields for an assigned key, with empty
  settings/appearance when the card cannot be resolved, and the key shows as removable. `key_unassign` must work
  without a live card (it already deactivates defensively; verify).
- **Built-in icons.** Plugin manifests and presets shipped with Decker use `fas:`/`far:` refs instead of bundled icon
  files (Core: `icons/blank`, `icons/nothing` become Font Awesome refs, bundled files deleted when unreferenced).
  The decker-plugins repo plugins are migrated the same way where an equivalent exists; plugins that really need
  their own artwork keep an icon pack.
- **Key context menu.** Right-clicking a key with a card opens: Press, Double Press, Long Press, separator, Delete
  Card. The first three select the key and open the editor on that gesture tab (the existing `show_gesture_tab`
  path); Delete Card runs the same confirmation and `key_unassign` as the hover button. An empty key shows no menu.

## Left to the builder
Exact Font Awesome names; whether unreferenced icon files are deleted in this change; menu wording for the keys'
hotkeys; test layout. Docs: CLAUDE.md (built-in plugins paragraph), `docs/plugins/package-and-manifest.md`.
