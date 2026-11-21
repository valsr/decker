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

JSON with snake_case keys. Unknown keys are ignored. `format` must be the integer `1`; any other value is rejected.

```json
{
  "format": 1,
  "id": "com.example.hello",
  "name": "Hello",
  "version": "1.0.0",
  "author": "Example",
  "description": "Says hello.",
  "homepage": "https://example.com/hello",
  "icon": "icons/plugin",
  "category": "Utilities",
  "decker": ">=0.2",
  "runtime": {
    "type": "python",
    "entry": "plugin.py",
    "python": ">=3.11",
    "requirements": ["requests>=2"]
  },
  "global_settings": [],
  "contributes": {
    "cards": [
      {
        "id": "com.example.hello.greet",
        "name": "Greet",
        "tooltip": "Press to greet",
        "icon": "icons/greet",
        "gestures": ["press", "long_press"],
        "states": [{ "image": "icons/greet", "title": "Hi" }],
        "appearance": { "background": "#1f618d", "title": { "position": "bottom" } },
        "settings": [
          { "key": "name", "type": "text", "label": "Name", "default": "world" }
        ]
      },
      {
        "id": "com.example.hello.wave",
        "name": "Wave",
        "card": "com.decker.core.runaction",
        "action": "com.example.hello.sendwave",
        "action_settings": { "times": 3 },
        "appearance": { "title": { "text": "Wave" } }
      }
    ],
    "actions": [
      {
        "id": "com.example.hello.sendwave",
        "name": "Send a wave",
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
| `format` | yes | Integer `2`. Format `1` (before the card/action split) is still read: its `contributes.actions` entries are cards. |
| `id` | yes | Reverse-domain id matching `^[a-z0-9]+(\.[a-z0-9-]+)+$`, e.g. `com.example.hello`. |
| `name`, `author` | yes | Non-empty strings. |
| `version` | yes | Semantic version `x.y.z` (pre-release/build suffixes allowed). |
| `description`, `homepage`, `category` | no | Strings. |
| `icon` | no | Path inside the package; the extension is optional (looked up as `.svg`, `@2x.png`, `.png`). Values starting with `pack:` are reserved. |
| `decker` | no (yes for `variables` and `options_source`) | Version specifier for Decker, e.g. `>=0.6` (cards and actions need Decker 0.6). Install is refused if the running server does not satisfy it. Set it to the release that supports a feature you use, for example `options_source` on a `choice` field: an older Decker refuses a field without `options`, and on one with `options` it ignores `options_source`, so the field becomes a plain static choice that replaces an unlisted saved value. |
| `runtime` | for `actions`, for `variables`, for `options_source` fields and for `cards` that are not static presets or presets of Run Action | See below. |
| `global_settings` | no | [Settings schema](settings-schema.md) for plugin-wide settings, shown in Settings, Plugins. |
| `permissions` | no | Reserved, ignored in v1. |
| `platforms` | no | Non-empty list of OS names (`linux`, `windows`, `macos`) the plugin supports. Omitted means all platforms. See [Platforms](#platforms). |
| `linux` | no | Object narrowing Linux support: `desktops` (`kde`, `gnome`, `other`) and `sessions` (`wayland`, `x11`). Only allowed when `platforms` is absent or includes `linux`. |
| `contributes` | yes | Object keyed by contribution kind; must be non-empty. An unknown kind is refused with "requires a newer version of Decker". |

### Platforms

`platforms` and `linux` are optional and additive; `format` stays `1`. Plugins that declare neither support every
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

### `cards[]`

A card is what a key shows: it draws an image and a title, and may react to presses. Cards are what the key picker lists.


| Field | Notes |
|-------|-------|
| `id` | Must start with `<plugin id>.` (same pattern as plugin ids). |
| `name`, `tooltip`, `icon` | Display data. |
| `gestures` | Default gesture set: any of `press`, `double_press`, `long_press` (default `["press"]`). A running plugin can change it per key with `set_gestures`. |
| `states` | Optional list of `{image, title}`; selected by `set_state` (index into the list). |
| `appearance` | Optional defaults for the Decker-drawn appearance: `background` (a plain colour string such as `"#1f618d"`, which stays valid, or the nested form `{"type": "none"|"color"|"image", "color": "#rrggbb", "image": {"ref": "pack:<pack id>/<path>"}}`; see [Key backgrounds](../key-backgrounds.md); a manifest may keep the string; older Decker releases cannot draw the nested form, so the key fails to compose there: set `decker` to a release that supports it, or keep the string), `icon` (an icon value `{"ref": "pack:<pack id>/<path>", "color": "#rrggbb"}`; users can also override it per key, which replaces the action's static `icon`/state image and any image the plugin sets itself; older Decker releases reject an unknown appearance key, so set `decker` to a release that supports it) and `title` (a [`text_style`](settings-schema.md#text-style) object; give only the fields you want to change, e.g. `{"position": "top"}`). The older flat `show_title`, `title_position`, `title_font`, `title_size`, `title_color`, `title_outline` and `title_outline_width` keys, and a plain-string `title`, are still accepted and folded into `title`. |
| `settings` | [Settings schema](settings-schema.md) for the per-key form. |
| `static` | `true` makes a preset: no code handles it, no process is started, a press does nothing, and the key shows only its Decker-drawn `appearance` (and `states[0]`). A static card cannot declare `settings` or `gestures`. A plugin whose cards are all static needs no `runtime`. |
| `card` | Makes this card a preset of another card. The only base is the built-in **Run Action** card, `com.decker.core.runaction`. Together with `action` it gives a ready-made key such as "Volume Up". Not valid together with `static`. |
| `behavior` | `"switch"` makes a headless card the server hosts itself, with no process (the built-in Switch card, `com.decker.core.switch`, whose `slots` setting embeds other cards and actions). Any other value is a manifest error, and a switch cannot also be `static` or a preset. Only the built-in `com.decker.core.switch` may declare it; on any other card it is a manifest error. |
| `action` | With `card`: the id of the [action](#actions) a press runs. It may belong to any plugin. |
| `action_settings` | With `card`: the action's preconfigured settings. The card's own `settings` values, if it declares any, override them. |

In the key picker an action is listed on the Actions tab with the icon of its preset card, or a generic task icon when no plugin ships one; dragging it onto a key drops that preset card, or Run Action set to the action.

A preset of Run Action runs no code of its own and needs no `runtime`; the action it runs lives in the plugin that declares it.

### `actions[]`

An action is something to do, not something to show: a function with settings that runs and draws nothing (for example
an HTTP request). Users reach actions through the built-in **Run Action** card, which lets them pick any available
action and fill in its settings, or through a preset card that plugins ship (see `card`/`action` above).

| Field | Notes |
|-------|-------|
| `id` | Must start with `<plugin id>.`. |
| `name`, `tooltip`, `icon` | Display data. |
| `settings` | [Settings schema](settings-schema.md) for the action's parameters. |

A plugin with actions needs a `runtime`: Decker starts the plugin and sends `run_action` (see the [protocol](protocol.md)).
Decker itself ships one action, **HTTP Request** (`com.decker.core.httprequest`), in the built-in Core plugin.

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

Decker ships one built-in static plugin, **Blank** (`com.decker.blank`): a key with only a background and a title, a base for presets. It cannot be uninstalled or replaced (a package or repository plugin with a built-in id is refused). Other plugins are installed from a `.deckerplugin` package or from a plugin repository
(see [repositories](repositories.md)); the default repository publishes the Counter plugin.
