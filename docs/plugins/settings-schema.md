# Settings schema

`global_settings` and each card's and action's `settings` are an ordered list rendered top to bottom as a form.

## Entries

- Section heading: `{ "section": "Title" }`
- Field: `{ "key": "...", "type": "...", "label": "...", "default": ..., ...}`

Common field keys: `key` (unique within the list), `type`, `label`, `default`, `optional` (an empty value is allowed
and stored as `null`), `placeholder`, `help`.

## Types

| Type | Extra keys | Value |
|------|------------|-------|
| `int` | `min`, `max`, `step`, `special_value` (text shown at `min`, e.g. "no limit") | integer |
| `float` | `min`, `max`, `step`, `decimals` | finite number |
| `text` | `multiline`, `template` (boolean, default `false`: expand `{page_name}`, `{page_index}`, `{page_count}`, `{current_profile}`, `{profile_id}` before delivery; `{{`/`}}` are literal braces, unknown names stay as typed; plugin variables work too, see [Template variables](#template-variables)) | string |
| `bool` | | boolean |
| `choice` | `options`: `[{ "label": "...", "value": ... }]` (non-empty unless `options_source` is set); `options_source`: a name, see [Dynamic choices](#dynamic-choices) | one of the option values; with `options_source`, any string up to 256 characters |
| `color` | `alpha` (boolean; when `true` the picker has an opacity slider and `#rrggbbaa` is also valid, alpha last) | `#rrggbb` (or `#rrggbbaa`) |
| `font` | | font family name |
| `icon` | | an object, see [Icon](#icon); `default` may be partial |
| `background` | | an object `{"type": "none"\|"color"\|"image", "color": "#rrggbb", "image": {"ref": "pack:<pack id>/<path>"}}`, see [Key backgrounds](../key-backgrounds.md); a plain colour string is still accepted and read as `color` |
| `text_style` | `template` (as for `text`; only its `text`) | an object, see [Text style](#text-style); `default` may be partial |
| `page` | `profile_field` (key of a `profile` field of the same schema: the dropdown then lists that profile's pages; blank or absent: the key's own profile) | a page of the key's device: stored as its page id (string, default `""`), delivered as `{"page_id", "name", "index", "profile_id"}` (`index` is 1-based within its profile; `profile_id` is `-1` when the page is gone); action settings only, not allowed in `global_settings` |
| `profile` | | a profile of the key's device: stored as the decimal profile id (`""`, the default, follows the current profile), delivered as `{"profile_id", "name", "index", "icon"}` (`index` is the 1-based dropdown position, `icon` an icon ref, `decker:logo` for the Decker icon; an unset or deleted profile arrives as `{"profile_id": -1, "name": "", "index": 0, "icon": ""}`). Profile ids are small per-device integers and are **reused** after a profile is deleted, so a stored id can later mean a new profile; action settings only, not allowed in `global_settings` |
| `action` | | an action of any installed plugin: stored as its id (`""`: none). The dropdown lists every available action |
| `action_params` | `action_field` (key of an `action` field of the same schema; the form then shows that action's settings) | an object: the chosen action's settings. Changing the action resets it. Used by the Run Action card |

## Dynamic choices

A `choice` field with `options_source` gets part of its list from the plugin when the settings form opens (the audio
devices of this machine, the players that are running):

```json
{ "key": "device", "type": "choice", "label": "Output device", "default": "default",
  "options": [{ "label": "Default output", "value": "default" }],
  "options_source": "output_devices" }
```

- `options_source` is a name matching `[a-z][a-z0-9_]*`, at most 64 characters. It belongs to the plugin whose schema
  declares it: a plugin is only asked for sources one of its own schemas (card, action or `global_settings`) declares.
  The plugin answers it with [`@options`](sdk-quickstart.md#dynamic-choice-options) or a
  [`get_options`](protocol.md#dynamic-choice-options) handler.
- A `choice` field needs `options`, `options_source`, or both. The static `options` come first, then the plugin's, and a
  value already listed is not repeated. The plugin must supply values and labels that are strings of up to 256
  characters, and at most 256 options are kept (see [limits](limits.md)). Static option values of a field that has an
  `options_source` must be strings of up to 256 characters (anything else refuses the manifest); the first static value
  becomes the default as it is.
- The stored value is **any string up to 256 characters**, not only a listed one, so a device that is unplugged keeps
  its saved value. A value that is not in the list is kept as it is, never replaced by the first option. The
  `default` of such a field must be a string (anything else refuses the manifest); without one it is the first static
  option's value, or `""` when there are none (`null` for an `optional` field).
- Works wherever a settings schema is used: card settings, action settings (including the Run Action card's
  parameters), `global_settings` and the Switch card's slot forms. It needs the plugin to be reachable; the plugin is
  started for the request when it is not running.
- The control is an **editable dropdown** with a refresh button: choose a listed option or type any value. The list
  loads in the background, so the form opens at once. A saved value that is not listed shows as "unavailable" (a
  tooltip) and stays selected. If the plugin cannot be reached or fails, the control falls back to the static options
  plus typing, and the tooltip shows the error. The same fallback applies where the form does not know the owning plugin.
- A plugin that uses `options_source` must declare a manifest `decker` constraint (the manifest is refused without one)
  and set it to the release that supports it, and it needs a `runtime`. An older Decker refuses a field without
  `options`, and on one with `options` it ignores `options_source`, so the field becomes a plain static choice that
  replaces an unlisted saved value. The constraint is what keeps such a Decker from installing the plugin: the installer
  (and installing from a repository) refuses a package whose `decker` constraint the running server does not satisfy. It
  is checked at install time only; a plugin already installed is not checked again when the server starts.

## Template variables

A `text` field with `"template": true` (and a key's title text) expands `{name}` for the variables below. `{{` and `}}`
stay literal braces.

- **Page and profile variables** (per device, owned by the server): `page_name`, `page_index`, `page_count`,
  `current_profile`, `profile_id`. They win over a plugin variable of the same name.
- **Plugin variables** (global, declared in the manifest under [`variables[]`](package-and-manifest.md#variables)): a
  declared variable that is unset, or whose plugin is stopped, expands to an empty string. A name nobody declared stays
  as typed. A value is never expanded again, so a value containing `{page_name}` is shown as it is.

The server redraws the live keys whose templated text uses a variable when its value changes.

## Text style

`text_style` is one text field; entering it opens a formatting toolbar (font, size, bold, italic, underline,
strikethrough, position, colour, outline, outline width, visible). Declare it like any other field:

```json
{ "key": "label", "type": "text_style", "label": "Label", "default": { "position": "top", "size": 14 } }
```

The value is an object with these fields; missing or invalid fields fall back to the field's default (per field,
never the whole value):

| Field | Type | Default |
|-------|------|---------|
| `text` | string | `""` |
| `visible` | boolean | `true` |
| `font` | font family name | `"DejaVu Sans"` |
| `size` | integer 4–48 | `12` |
| `bold`, `italic`, `underline`, `strike` | boolean | `false` |
| `position` | `"top"`, `"middle"` or `"bottom"` | `"bottom"` |
| `color` | `#rrggbb` or `#rrggbbaa` (alpha last) | `"#ffffff"` |
| `outline` | `#rrggbb`, `#rrggbbaa` or `null` (no outline) | `null` |
| `outline_width` | integer 1–6 | `1` |

Two text areas on one key are two `text_style` fields. Decker's own key title (appearance key `title`) is a
`text_style` field.

## Icon

`icon` is a control for picking an icon from the image library (the installed icon packs): a preview, the icon's name, **Browse…** (a
searchable browser with the packs grouped by plugin) and a fill colour (RGBA). Declare it like any other field:

```json
{ "key": "icon", "type": "icon", "label": "Icon", "default": { "color": "#ffffff" } }
```

The value is an object; missing or invalid fields fall back to the field's default (per field, never the whole value):

| Field | Type | Default |
|-------|------|---------|
| `ref` | `""` (no icon) or `pack:<pack id>/<icon path>` | `""` |
| `color` | `#rrggbb` or `#rrggbbaa` (alpha last) | `"#ffffff"` |

To show the icon on the key, pass both to the SDK: `ctx.set_icon(settings["icon"]["ref"], settings["icon"]["color"])`
(see [protocol](protocol.md#images)). References with a `..` segment or an absolute path are treated as invalid.

## Validation

The **server** validates stored and incoming values against the schema, so a plugin always receives a complete,
typed settings object:

- unknown keys are dropped;
- missing, invalid, out-of-range or non-finite values fall back to the field's default (or `null` when `optional`);
- the client never duplicates defaults.

Settings reach the plugin in `will_appear.settings`, `settings_changed.settings` and, for plugin-wide settings,
`hello.global_settings` and `global_settings_changed.global_settings`. A plugin can write them back with
`set_settings` (per key) and `set_global_settings`; the server validates those too.
