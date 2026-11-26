# Package and manifest

## Package

- A package is a zip file named `<anything>.deckerplugin`. `manifest.json` is at the **archive root**.
- Entries must use `/` separators. Entries with backslashes, absolute paths, drive letters, `..` components, NUL
  bytes, symlinks, encrypted entries or duplicate names are rejected.
- Installed to `~/.decker/plugins/<plugin id>/`. Python plugins get their own environment in `.venv` inside that
  folder; uninstalling removes both.
- Size caps: see [limits](limits.md).

Build one with `poe cli plugin pack <folder>` (see the [quick start](sdk-quickstart.md)).

## manifest.json

JSON with snake_case keys. Unknown keys are ignored. `format` must be the integer `3`; formats `1` and `2` are refused
with a message naming the format, and there is no compatibility layer: a plugin written for format 2 has to be migrated
(see [What changed in format 3](#what-changed-in-format-3)).

```json
{
  "format": 3,
  "id": "com.example.hello",
  "name": "Hello",
  "version": "1.0.0",
  "author": "Example",
  "description": "Says hello.",
  "homepage": "https://example.com/hello",
  "icon": "icons/greet",
  "category": "Utilities",
  "decker": ">=0.7",
  "runtime": {
    "type": "python",
    "entry": "plugin.py",
    "python": ">=3.11",
    "requirements": ["pillow"]
  },
  "global_settings": [],
  "contributes": {
    "cards": [
      {
        "id": "com.example.hello.greet",
        "name": "Greet",
        "tooltip": "Press to greet",
        "icon": "icons/greet",
        "states": [{ "image": "icons/greet", "title": "Hi" }],
        "appearance": { "background": "#1f618d", "title": { "position": "bottom" } },
        "settings": [
          { "key": "name", "type": "text", "label": "Name", "default": "world" }
        ],
        "bindings": {
          "press": { "action": "com.example.hello.sendwave", "args": { "times": 1 } },
          "long_press": { "action": "com.example.hello.sendwave", "args": { "times": 3 } }
        }
      }
    ],
    "actions": [
      {
        "id": "com.example.hello.sendwave",
        "name": "Send a wave",
        "scope": "plugin",
        "returns": "The number of waves sent (int).",
        "settings": [{ "key": "times", "type": "int", "label": "Times", "default": 1, "min": 1 }]
      }
    ],
    "icon_packs": [
      { "id": "com.example.hello.icons", "name": "Hello icons", "path": "iconpack", "description": "Optional" }
    ]
  }
}
```

### Top-level fields

| Field | Required | Notes |
|-------|----------|-------|
| `format` | yes | Integer `3`. Formats `1` and `2` are refused. |
| `id` | yes | Reverse-domain id matching `^[a-z0-9]+(\.[a-z0-9-]+)+$`, e.g. `com.example.hello`. |
| `name`, `author` | yes | Non-empty strings. |
| `version` | yes | Semantic version `x.y.z` (pre-release/build suffixes allowed). |
| `description`, `homepage`, `category` | no | Strings. |
| `icon` | no | Path inside the package; the extension is optional (looked up as `.svg`, `@2x.png`, `.png`). A built-in Font Awesome short reference (`fas:`, `far:` or `fab:`, such as `fas:play`; see [Images](protocol.md#images)) names a bundled icon instead of a file, and is the usual choice for plugin and card icons. Values starting with `pack:` are reserved. |
| `decker` | no (yes for `variables`, action `variables`, `options_source` and `presets`) | Version specifier for Decker, e.g. `>=0.7` (format 3 needs Decker 0.7). Install is refused if the running server does not satisfy it. Set it to the release that supports a feature you use, for example `options_source` on a `choice` field: an older Decker refuses a field without `options`, and on one with `options` it ignores `options_source`, so the field becomes a plain static choice that replaces an unlisted saved value. |
| `runtime` | for `actions` (every action runs in its plugin's process), for `variables`, for `options_source` fields and for `cards` that are not `static` | See below. |
| `global_settings` | no | [Settings schema](settings-schema.md) for plugin-wide settings, shown in Settings, Plugins. |
| `permissions` | no | Reserved, ignored in v1. |
| `platforms` | no | Non-empty list of OS names (`linux`, `windows`, `macos`) the plugin supports. Omitted means all platforms. See [Platforms](#platforms). |
| `linux` | no | Object narrowing Linux support: `desktops` (`kde`, `gnome`, `other`) and `sessions` (`wayland`, `x11`). Only allowed when `platforms` is absent or includes `linux`. |
| `contributes` | yes | Object keyed by contribution kind; must be non-empty. An unknown kind is refused with "requires a newer version of Decker". |

### Platforms

`platforms` and `linux` are optional and additive. Plugins that declare neither support every
platform, and older Decker versions ignore both fields.

```json
{ "platforms": ["linux", "windows"], "linux": { "desktops": ["kde", "gnome"], "sessions": ["wayland"] } }
```

- `platforms` must be a non-empty list of strings; names are trimmed and lowercased. Unknown names are kept but never
  match, so a newer plugin does not break an older Decker. An empty list or non-string entries are a validation error.
- `linux.desktops` and `linux.sessions` are each a non-empty list; omitted means any. `linux` with a `platforms` list
  that lacks `linux` is a validation error.
- Matching: the server's OS must be in `platforms`. On a Linux server, its desktop must be in `linux.desktops` and its
  session in `linux.sessions`. The server detects the desktop from `XDG_CURRENT_DESKTOP` (contains `KDE` is `kde`,
  `GNOME` is `gnome`, anything else is `other`) and the session from `XDG_SESSION_TYPE`. When either is unset (a
  headless server) the host value is `unknown`, which satisfies any constraint on that axis.
- A mismatch never blocks anything: the plugin still loads (the server logs a warning), and the client shows a "Not
  supported on <platform>" badge. See [repositories](repositories.md) for the browser filter.
- `platform_info` (RPC) returns the server's `{os, desktop, session, label}` as JSON, for example label
  `Linux (KDE, Wayland)`; unknown parts are omitted from the label. Installed plugin info carries `platforms`, `linux`
  and a server-computed `supported`.
- Declare the platforms you have actually tested, and keep the plugins repository's support table in sync.

### `runtime`

| Field | Notes |
|-------|-------|
| `type` | `python` or `executable`. |
| `entry` | Path to the entry file inside the package. Must resolve inside the plugin folder. |
| `python` | Optional interpreter version or specifier for `uv` (python runtime only). A request below Python 3.10 is refused or raised to `>=3.10`, because the SDK needs 3.10+. |
| `requirements` | Python runtime only: pip requirement strings (name, version specifier, extras, markers). URL, git and local-path requirements are rejected. Installed with `uv` from the package index. |

- `python`: Decker runs `python -m decker_plugin_sdk <entry>` in the plugin's own `.venv`. The SDK is provided by
  Decker (you do not list it in `requirements`). `uv` must be installed on the server machine.
- `executable`: Decker runs `entry` directly (any language). The file must be executable; the installer sets the
  executable bit. You implement the [protocol](protocol.md) yourself.
- Every action needs a `runtime`: it runs in its plugin's process. A plugin that paints cards with the canvas lists
  `pillow` in `requirements` (the SDK itself needs no packages). Built-in plugins that declare a `runtime` (Core) are
  the exception: Decker runs them with its own interpreter and its copy of the SDK, with no uv environment, so they work
  offline, and they are supervised, started lazily and restarted like any plugin.

### `cards[]`

A card is what a key shows: it draws an image and a title. It does not handle presses itself; each gesture is bound
to an [action](#actions), and a card keeps the raw `key_down`/`key_up` events for visual feedback. Cards are what the
key picker lists.

A card is worth shipping only if it draws something a Blank card cannot, or owns state or refresh logic. A "Blank card
plus an action" is just an action: declare it as a `global` action and, if it makes a sensible key, a [preset](#presets) for it. In
the card editor a card's settings appear on the Card tab (hidden when it has none) and its gestures on the Press,
Double Press and Long Press tabs, where the Action dropdown lists Do Nothing, the card's own plugin-scoped actions (those the card lists in `actions`, plus any that no card of the plugin lists), the built-in actions, then every plugin's global actions.

| Field | Notes |
|-------|-------|
| `id` | Must start with `<plugin id>.` (same pattern as plugin ids). |
| `name`, `tooltip`, `icon` | Display data (`icon` may be a `fas:`/`far:`/`fab:` ref). |
| `bindings` | Default gesture bindings, see [Bindings](#bindings). |
| `actions` | Optional list of full ids of this plugin's own plugin-scoped actions (`contributes.actions`, `scope` `"plugin"`) that belong to this card: no duplicates, no global or unknown ids (a manifest error). In the card editor's "Card actions" group, a card shows the actions it lists; a plugin-scoped action that no card of the plugin lists stays visible on every card of that plugin. Requires `decker >=0.8.1`; an older Decker (0.8.0) ignores the field and shows all of the plugin's plugin-scoped actions. Presets cannot declare it. |
| `states` | Optional list of `{image, title}`; selected by `set_state` (index into the list). |
| `appearance` | Optional defaults for the Decker-drawn appearance: `background` (a plain colour string such as `"#1f618d"`, which stays valid, or the nested form `{"type": "none"|"color"|"image", "color": "#rrggbb", "image": {"ref": "pack:<pack id>/<path>" or "fas:play"}}`; see [Key backgrounds](../key-backgrounds.md)), `icon` (an icon value `{"ref": "pack:<pack id>/<path>" or a built-in short form such as "fas:play", "color": "#rrggbb"}`; users can also override it per key, which replaces the action's static `icon`/state image and any image the plugin sets itself) and `title` (a [`text_style`](settings-schema.md#text-style) object; give only the fields you want to change, e.g. `{"position": "top"}`). The older flat `show_title`, `title_position`, `title_font`, `title_size`, `title_color`, `title_outline` and `title_outline_width` keys, and a plain-string `title`, are still accepted and folded into `title`. |
| `settings` | [Settings schema](settings-schema.md) for the per-key form. |
| `static` | `true` makes a card with no code of its own and no process: it shows only its Decker-drawn `appearance` (and `states[0]`). A static card cannot declare `settings`, but it may declare `bindings`: the actions it binds run in their own plugin's process. A plugin whose cards are all static needs no `runtime` (unless it has actions). |

`gestures`, `card`, `action`, `action_settings` and `behavior` are not valid on a card in format 3 (a manifest that has
one is refused).

#### Bindings

`bindings` maps a gesture to the action it runs and that action's arguments. The gestures are exactly `press`,
`double_press` and `long_press`; any other key is a manifest error.

```json
"bindings": {
  "press":        { "action": "com.example.utilities.brightness_step", "args": { "step": 10 } },
  "double_press": { "action": "com.example.utilities.brightness_step", "args": { "step": -10 } },
  "long_press":   { "action": "com.decker.core.httprequest", "args": { "method": "GET", "url": "https://example.com/hook" } }
}
```

- An omitted gesture is **Do Nothing** (`com.decker.core.nothing`). `args` is optional (empty by default) and is
  validated against the action's [settings schema](settings-schema.md) when the key runs.
- A card may bind its own plugin's actions of either scope, any `global` action, or Do Nothing. Binding an action id
  that starts with the card's own plugin id but is not declared in the manifest is a manifest error. A binding to an
  action that is not installed, or to another plugin's `plugin`-scoped action, is kept but acts as Do Nothing.
- The manifest bindings are defaults. The user can rebind any gesture per key in the card editor. A gesture the user
  never touched follows the manifest default; a gesture the user explicitly set to Do Nothing stays Do Nothing.
- A gesture bound to Do Nothing is not registered with the key's gesture recognizer, so a press fires at once when
  `double_press` is unbound.
- The card sees its effective (stored or default) bindings, read-only: `will_appear` and `settings_changed` carry
  `bindings` (see the [protocol](protocol.md)), and an edit of the bindings sends `settings_changed`. The `args` of
  each binding arrive resolved against the action's `settings`, the same as card `settings`: a `page` argument is the page
  object (`page_id`, `name`, `index`, `profile_id`), a `profile` argument the profile object, and the server re-sends
  `settings_changed` when a rename changes them. Do Nothing and an action that is not installed keep their args as stored.

### `actions[]`

An action is something to do, not something to show: a function with settings that runs, may return a value, and
draws nothing (for example an HTTP request). Users reach actions through the bindings of cards; plugin code reaches
them with `call_action`.

| Field | Notes |
|-------|-------|
| `id` | Must start with `<plugin id>.`. |
| `name`, `tooltip`, `icon` | Display data. |
| `scope` | `"global"` (any plugin's cards and code may use it, and users reach it through card bindings and presets) or `"plugin"` (only this plugin's cards and code). Default `"plugin"`, so nothing is public by accident. Any other value is a manifest error. |
| `settings` | [Settings schema](settings-schema.md) for the action's arguments, including the validation rules `required`, `pattern`, `min_length` and `max_length`. |
| `returns` | Optional text describing the return value. Documentation only: it is shown to users and never checked. |
| `timeout` | Optional whole seconds, 1 to 300: how long a run may take before the server gives up. Default 30. A caller's own `timeout` wins, and a nested call never outlives the action that made it. |
| `variables` | Optional list of `{"name", "description?"}`: the per-card variables the action may set with `ctx.set_variable` (see [Action variables](#action-variables)). |

A plugin with actions needs a `runtime`: Decker starts the plugin and sends `run_action` (see the
[protocol](protocol.md)). Decker itself ships four actions in the built-in Core plugin, all `global`: **Do Nothing**
(`com.decker.core.nothing`, handled by the server, never sent to a process), **HTTP Request**
(`com.decker.core.httprequest`, returns `{"status", "body"}`), **Delay** (`com.decker.core.delay`, waits 0 to 60
seconds, manifest `timeout` 65) and **Sequence** (`com.decker.core.sequence`, runs up to ten actions in order within
300 seconds, manifest `timeout` 300).

#### Action variables

Each of a key's three bindings (`press`, `double_press`, `long_press`) has variables of its own, so one binding can use
what another produced: a card whose press starts a program and whose long press stops it can pass `{press.pid}`.

```json
{ "id": "com.example.runner.start", "variables": [ { "name": "pid", "description": "Process id of the program" } ] }
```

- **Declaring.** `name` matches `[a-z][a-z0-9_-]*` and may not be `status`; duplicates in one action are a manifest
  error. An action can set only the names it declares. `status` is reserved and not declarable: the server sets it on
  every binding when its action finishes, to `SUCCESS` if the action returned and `FAILURE` if it raised, timed out,
  was refused or its plugin failed. Do Nothing never touches `status`.
- **Setting.** `ctx.set_variable(name, value)` (see the [SDK](sdk-quickstart.md#action-variables)) stores the
  value on the binding that ran the action. Values are JSON, finite numbers only, at most 64 KiB encoded. A nested
  call has no binding unless its caller gives it a *slot*, so `set_variable` in an unslotted nested call is refused.
- **Slots.** `call_action(..., slot="step_1")` gives a nested call variables and a `status` of its own, read as
  `{press.step_1.pid}`. A slot matches `[a-z][a-z0-9_-]*`, at most 32 characters, and may not be `status`; a bad slot
  fails the call. There is one level: only a call made by a gesture binding's own action can pass a slot, and a
  slotted call's own nested calls get none. **Sequence** labels its steps `step_1` to `step_10`.
- **Reading.** `{<binding>.<name>}` and `{<binding>.<slot>.<name>}` expand in the arguments of any action of the same
  card, when the action runs (values change between presses and between Sequence steps). Examples: `{press.pid}`,
  `{double_press.status}`, `{long_press.status}`, `{press.step_1.pid}`. Only settings fields of type `text` or
  `text_style` with `"template": true` expand, so a whole-value reference is always text: null (not yet set) is empty,
  strings are as is and everything else is its JSON text. Core's HTTP Request expands `url` but not `body`, because
  `{{` and `}}` are template escapes and would corrupt a JSON body.
- **Left as typed.** A two-part reference is expanded only when the binding's action declared that name (or it is
  `status`); an undeclared name or unknown qualifier stays as typed, so it cannot clash with global `{name}` variables.
  A three-part slot reference is never left as typed: unset reads as empty. Write literal braces as `{{` and `}}`.
- **Lifetime.** Variables live in server memory only. They survive page and profile switches and moving the key, are
  dropped when the key is unassigned or its card replaced, and at server restart. Changing a binding's *action*
  resets that binding's variables and slots to unset; editing only its arguments does not. A key's variables are
  listed by the `key_variables(device_id, page_id, key_index)` RPC, as `{binding: {name: value, slot: {name: value}}}`
  (empty for an empty key).
- A plugin that declares action `variables` needs `"decker": ">=0.8"` and protocol 6.

### What changed in format 3

Decker 0.7 accepts only `"format": 3`. Compared with format 2:

- Cards no longer declare `gestures`, handle `on_press`/`on_double_press`/`on_long_press` or call `set_gestures`; they
  declare `bindings` and the actions do the work.
- The preset form of a card (`card`, `action`, `action_settings`) and `behavior` are gone, together with the built-in
  Run Action and Switch cards and the `slots` setting type. A stored key that names a retired card loads as an empty
  key. Swapping the card a key shows is not available.
- Actions gain `scope` and `returns`, take their arguments as `ctx.args`, and may return a value.
- Protocol 4 replaces the gesture events with `run_action`/`action_result` carrying the call details, and adds
  `call_action`/`call_result` and `get_image`/`image_result`.

### `presets[]`

A preset is a ready-made key: dropping one from the Cards list onto a key assigns a **Blank** card with the preset's
bindings and appearance. The result is a plain Blank key; nothing links it to the preset, so a later plugin update
does not change keys already placed.

```json
{
  "id": "com.example.pages.preset-next",
  "name": "Next page",
  "tooltip": "Go to the next page",
  "icon": "fas:forward",
  "appearance": { "title": { "text": "Next" }, "icon": { "ref": "fas:forward", "color": "#ffffff" } },
  "bindings": { "press": { "action": "com.example.pages.next", "args": { "wrap": true } } }
}
```

| Field | Notes |
|-------|-------|
| `id` | Required; starts with `<plugin id>.`; unique among the plugin's presets, cards and actions. |
| `name`, `tooltip` | Display data for the picker. |
| `icon` | The picker thumbnail: a plugin file or a built-in ref. Not copied to the key. |
| `appearance` | Same shape as a card's `appearance`, copied to the key. `appearance.icon.ref` must be a ref (`pack:`, `fas:`, `far:` or `fab:`), not a file path. Without `appearance.title.text` the key's title is the preset `name`; without `appearance.icon` the key shows no icon (a bound action's icon is not used). |
| `bindings` | Same shape as a card's [bindings](#bindings). Arguments may be incomplete; the user fills them in the editor. A preset may have no bindings. |

- A preset may bind only `global` actions (of any plugin) or Do Nothing, because the dropped key is a Blank card and
  cannot run its plugin's `plugin`-scoped actions. Binding the plugin's own `plugin`-scoped action is a manifest
  error; a foreign `plugin`-scoped action is not offered. A preset whose actions are not all currently usable by its plugin (action missing, plugin not loaded) is hidden.
- A preset may not declare `settings`, `states`, `static` or `gestures`; if it needs code, make it a card.
- The plugin needs no `runtime` for presets alone, but must declare a `decker` constraint that guarantees the Decker
  version adding them (`">=0.8"`).
- Unstyled SVG key icons draw white, so a preset icon needs no explicit colour on a dark key.

### `variables[]`

Global text variables the plugin exports for templated text (see [settings schema](settings-schema.md) and the
[protocol](protocol.md#variables)). Each entry has exactly one of:

| Field | Notes |
|-------|-------|
| `name` | An exact variable: lowercase `[a-z][a-z0-9_]*`, at most 64 characters. |
| `pattern` | A family such as `media_{device}_volume`; each `{placeholder}` stands for one or more `[a-z0-9_]` characters, placeholders are unique and not adjacent, and the name with the placeholders filled must be a valid name. The plugin registers the concrete names at runtime. |
| `description` | Optional text, listed by the `variables_list` RPC. |

- At most 256 entries per plugin; a name or pattern may not be declared twice.
- A name or pattern that collides with a core variable (`page_name`, `page_index`, `page_count`, `current_profile`,
  `profile_id`) makes the manifest invalid. At load, a plugin is rejected and recorded as a plugin load failure when
  one of its exact names is already owned by another plugin (exactly or through that plugin's pattern), when a pattern
  has the same shape as another plugin's pattern, or when a pattern would match another plugin's exact name; the other
  plugin keeps working.
- A plugin that declares variables needs a `runtime` and a `decker` version constraint (older Decker refuses it), and a
  plugin with only `variables` (no cards, actions or icon packs) is valid.
- Plugins that declare variables are started with the server and kept running; every other plugin stays lazy (see
  [limits](limits.md#process-lifecycle)).

### `icon_packs[]`

`{id, name, path, description?}`. `id` starts with the plugin id. `path` is a folder inside the package holding
image files (`svg`, `png`, `jpg`, `jpeg`, `gif`; GIFs animate on keys), optionally ordered by an `icons.json` index. Icon packs
need no `runtime`. Packs are served to clients over RPC and appear in the client's image library, where users can also pick them as key backgrounds; individual icons are limited to 4 MiB.

## Where plugins come from

Decker ships one built-in plugin. **Core** (`com.decker.core`) holds the static Blank card (`com.decker.core.blank`: a key with only a background and a title; the legacy ids `com.decker.blank` and `com.decker.blank.blank` still resolve to it in stored keys), the Do Nothing, HTTP Request, Delay and Sequence actions, presets for the last three, and runs as a normal plugin process on Decker's own interpreter (see [`runtime`](#runtime)). Built-in plugins cannot be uninstalled or replaced (a package or repository plugin with a built-in id is refused). Other plugins are installed from a `.deckerplugin` package or from a plugin repository
(see [repositories](repositories.md)); the default repository publishes the Counter, Pages and Profiles plugins.
