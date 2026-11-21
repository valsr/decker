# Protocol

Decker talks to each plugin process over its **stdin/stdout**. Every message is one JSON object per line (UTF-8,
terminated by `\n`):

```json
{"event": "set_title", "context": "ctx-1", "payload": {"title": "Hi"}}
```

- `event`: required string.
- `context`: Decker's id for one key instance of a card. Omitted for plugin-wide events.
- `payload`: object, omitted when empty.

stdout is reserved for the protocol. Anything a plugin writes to **stderr** is logged by the server with the plugin
id as a prefix. Lines that are not valid protocol messages are logged and dropped. A line longer than 8 MiB is
discarded.

## Startup

1. The server starts your process and sends `hello`.
2. Send `ready` when you are set up. Until then the server queues events for you; if `ready` does not arrive
   within the ping timeout (10 s) the process is killed.
3. The server sends the full snapshot of the [variables](#variables) that have a value (`variables_changed`) and asks a
   plugin that declares variables for each of them (`get_variable`).
4. The server sends `will_appear` for every key showing one of your cards, then any events queued meanwhile.

## Server to plugin

| Event | Context | Payload |
|-------|---------|---------|
| `hello` | | `{"protocol": 3, "decker_version": "0.6.0", "plugin_id": "com.example.hello", "global_settings": {...}}` (first message) |
| `will_appear` | yes | `{"card": "<card id>", "device": "<device id>", "key": {"index": 0, "column": 0, "row": 0}, "settings": {...}, "state": 0}` |
| `will_disappear` | yes | none |
| `key_down`, `key_up` | yes | none (raw key events) |
| `press`, `double_press`, `long_press` | yes | none (recognised by the server for the key's registered gestures) |
| `settings_changed` | yes | `{"settings": {...}}` |
| `run_action` | | `{"action": "<action id>", "request": "<id>", "settings": {...}}`: run one of your [actions](package-and-manifest.md#actions) with its validated settings and answer with `action_result` carrying the same `request`. Runs are independent of any key and may overlap |
| `global_settings_changed` | | `{"global_settings": {...}}` |
| `device_connected` | | `{"device": "<device id>"}` |
| `get_variable` | | `{"request": "<id>", "name": "<variable>"}`: answer with `variable_value` carrying the same `request` (see [Variables](#variables)) |
| `variables_changed` | | `{"set": {"name": "value", ...}, "removed": ["name", ...]}`: the global variables that changed; the full set of values is sent once after `ready` |
| `get_options` | | `{"request": "<id>", "source": "<options source>"}`: answer with `options_result` carrying the same `request` (see [Dynamic choice options](#dynamic-choice-options)) |
| `ping` | | none; reply with `pong` |
| `shutdown` | | none; exit promptly |

(`device_disconnected` is part of the vocabulary but not sent in v1.)

## Plugin to server

| Event | Context | Payload |
|-------|---------|---------|
| `ready` | | none |
| `pong` | | none |
| `set_image` | yes | `{"image": "<base64>", "format": "png"}` (`png`, `jpeg`, `svg` or `gif`; an animated GIF animates on the key, see [Images](#images)), or `{"image": null}` to clear, or `{"icon": "pack:<pack id>/<path>", "color": "#rrggbb"}` to show an icon from an installed icon pack (see [Images](#images)) |
| `set_title` | yes | `{"title": "text"}`, shown only when the user set no title; titles are one line, so line breaks are drawn as a single space |
| `set_state` | yes | `{"state": 1}`, an index into the card's `states` |
| `show_alert`, `show_ok` | yes | none (accepted, no visible effect in v1) |
| `set_settings` | yes | `{"settings": {...}}`, validated against the card's schema and saved; keys you leave out keep their stored value, so send only what you changed (see [Pages](#pages)) |
| `switch_page` | yes | `{"to": "next"\|"previous"\|"first"\|"last", "wrap": false}` or `{"page_id": "<id>"}`: switch the key's device to that page (see [Pages](#pages)) |
| `switch_profile` | yes | `{"to": "next"\|"previous"\|"first"\|"last", "wrap": false}` or `{"profile_id": 2}`: switch the key's device to that profile's default page (see [Profiles](#profiles)) |
| `set_global_settings` | | `{"settings": {...}}`, validated against `global_settings` and saved |
| `set_gestures` | yes | `{"gestures": ["press", "long_press"]}`; replaces the key's gesture set |
| `action_result` | | `{"request": "<id>", "ok": true}` or `{"request": "<id>", "ok": false, "error": "text"}`, answering `run_action`. The server gives up after 30 s and shows the user the error |
| `register_variable` | | `{"name": "<concrete name>"}`: announce a name inside one of your declared patterns (see [Variables](#variables)) |
| `unregister_variable` | | `{"name": "<concrete name>"}`: withdraw it and its value |
| `variable_updated` | | `{"name": "<variable>"}`: the value changed; the server asks for it with `get_variable` |
| `variable_value` | | `{"request": "<id>", "ok": true, "value": "text"\|null}` or `{"request": "<id>", "ok": false, "error": "text"}`, answering `get_variable` |
| `options_result` | | `{"request": "<id>", "ok": true, "options": [{"label": "text", "value": "text"}, ...]}` or `{"request": "<id>", "ok": false, "error": "text"}`, answering `get_options` |
| `log` | | `{"level": "info", "message": "text"}`; levels `debug`, `info`, `warning`, `error` |
| `open_url` | | `{"url": "https://..."}`; only `http`/`https` with a host are accepted. Every connected client asks the user before opening it |

Messages for a context that is not active (for example after `will_disappear`) are dropped.

## Pages

`switch_page` acts on the device of the key (`context`) that sent it. `to` is relative to the page the device shows;
`wrap` (default `false`, only `true` counts) loops from the last page to the first and back. `page_id` goes to one of
that device's pages, **in any of its profiles** (the device switches profile first when needed); `to` stays inside the
profile the device shows. Unknown pages, a device with one page, an edge without wrap and malformed payloads are ignored
(logged by the server).

A `page` setting (see [settings schema](settings-schema.md)) arrives as `{"page_id": "...", "name": "...", "index": 1, "profile_id": 0}`
(`index` is 1-based within its profile); a deleted or unset page arrives as `{"page_id": "", "name": "", "index": 0, "profile_id": -1}`.
Settings fields that declare `"template": true` arrive with `{page_name}`, `{page_index}`, `{page_count}`, `{current_profile}`
and `{profile_id}` already replaced for the key's
device; the stored value stays as the user typed it, so a plugin must not write expanded text back: with
`set_settings`, send only the keys you changed. The server re-sends `settings_changed` when a page is renamed, added or
deleted or the device switches pages and a delivered value changes. Templates in `global_settings` are not expanded.

## Profiles

A device has one or more **profiles**, each a set of pages; `profile_id` is a small integer per device (the lowest unused
one is handed out, so ids are reused after a deletion). `switch_profile` acts on the device of the key that sent it:
`to` moves through the profiles in the order the user sees them (`wrap` as for pages), `profile_id` goes to that
profile. The device then shows the profile's default page. Unknown or non-integer ids, a device with one profile and
malformed payloads are ignored. A `profile` setting arrives resolved (see [settings schema](settings-schema.md)); its
`icon` is a ref a plugin can pass to `set_icon`, including `decker:logo`, the Decker icon.

## Variables

A plugin that declares [`contributes.variables`](package-and-manifest.md#variables) exports global text values that any
templated text can use as `{name}` (see [settings schema](settings-schema.md)). Ownership is the manifest declaration:
only the declaring plugin can set a name, and `register_variable` only accepts a concrete name that fits one of the
plugin's own patterns and is not taken. Malformed events (no string `name`), a `variable_updated` for a name the plugin
does not own and a `variable_value` that matches no open request are ignored (logged by the server).

- Exact names declared in the manifest need no `register_variable`. The server asks for each of them after `ready`
  and asks for a pattern name when it is registered.
- The server asks with `get_variable` and waits 5 s (see [limits](limits.md)). `variable_value` carries the text
  (`null` clears the value; numbers and booleans are turned into text, a value over 1024 characters is refused). When
  the plugin answers `ok: false`, sends an invalid value or does not answer in time, the old value stays and the
  server logs it.
- `variable_updated` is how a plugin says that a value changed. Repeated signals for one name while an ask is open are
  collapsed into one more ask after it.
- `variables_changed` goes to every ready plugin whenever any global variable changes, so a plugin can keep a local
  copy. Values of a plugin that stops or crashes are removed (they are never shown stale) and return after a restart.

## Dynamic choice options

A `choice` setting with an `options_source` (see [settings schema](settings-schema.md#dynamic-choices)) asks the plugin
for its options when a settings form opens. The server sends `get_options` with the source name and waits up to 5 s
(see [limits](limits.md)) for `options_result` with the same `request`.

- The server only asks for a source that one of the plugin's own schemas declares; the client gets an "unknown options
  source" error for any other name, without the plugin being asked.
- The server cleans the answer: entries that are not `{"label": str, "value": str}` are dropped, labels and values are
  cut to 256 characters, a repeated value is dropped, and at most 256 options are kept.
- `ok: false` (with an optional `error` text, kept to 256 characters), no answer in time, or the plugin stopping gives
  the client an error and no options. A source the plugin does not implement should be answered with `ok: false`.
- A plugin that is not running (a lazy plugin) is started for the request and released afterwards, like an action run;
  a plugin that is marked failed gives an error at once. The request is queued until the plugin sends `ready`.
- An SDK from before this feature ignores `get_options`, which the server reports as a timeout. The Python SDK answers
  with [`@options`](sdk-quickstart.md#dynamic-choice-options).

## Images

An icon from an installed icon pack is shown by reference: `{"icon": "pack:<pack id>/<path>", "color": "#rrggbb"}`
(`color` is optional and may be `#rrggbbaa`). The server reads the icon itself, so the plugin never touches pack
files. `image` and `icon` are mutually exclusive (a payload with both is dropped). A reference the server cannot read
(malformed, unknown pack, path outside the pack, too large, pack uninstalled later) clears the image and is logged.
Decker's own **System Icons** (the server's freedesktop icon theme) are packs too: `pack:decker.system.<category>/<icon name>` with `<category>` one of `actions`, `applications`, `categories`, `devices`, `emblems`, `emotes`, `mimetypes`, `places`, `status`, `other` and the icon's name without extension (for example `pack:decker.system.actions/edit-copy`). The name follows the active theme, and an icon the theme does not have is treated like a missing pack icon. The `decker.system.` pack id prefix is reserved and a plugin pack using it is refused.

**User library.** Icons the user imported (GIF, PNG, JPG, SVG) form the `decker.user` pack, always listed (even when empty) after the plugin packs: `pack:decker.user/<id>.<ext>`, for example `pack:decker.user/my-cat.gif`. Imports go through the `icon_import` RPC, which is loopback-gated like `plugin_install` (`icon_import_suggest` and `icon_import_preview` are open to every client). The `decker.user` pack id is reserved and a plugin pack using it is refused.

A pack icon is drawn at 70% of the key and centred, so the key's title stays readable around it (images sent with
`image` still fill the key). `color` recolours SVG icons: it sets the SVG's `color` (so `fill="currentColor"` follows it) and a default `fill`, and
the alpha becomes the SVG's opacity. Raster icons ignore it, and an invalid colour leaves the icon untinted.

A user can set an icon override on a key; it replaces whatever the plugin shows on that key (the manifest icon and anything sent with `set_image`, even later). Clearing the override brings the plugin's image back, so a plugin should not rely on its image always being visible.

Raster images larger than 4096 x 4096 pixels are rejected. Decker scales the image to the key and draws it over
the key background, then the title. Invalid base64 is dropped.

A GIF with 2 to 300 frames, sent as `image` or taken from a pack icon, animates on the key (unless the user turned
`animation.enabled` off). The key first shows the GIF's first frame; the animation starts once Decker has built it.
Frame delays under 20 ms are played as 100 ms. A GIF over a limit (see [limits](limits.md)), a single-frame GIF or one
that fails to decode is shown as its first frame (or, if undecodable, as no image) and never animates. Sending any other
image, or clearing the image, ends the animation.

## Flood protection and liveness

- Each plugin may send 100 messages per second sustained with a burst of 200. Messages over budget are dropped; a
  plugin that stays over budget for 5 s continuously is killed and counted as a crash.
- The server sends `ping` every 5 s. No `pong` within 10 s counts as a hang and the process is killed.

## Minimal executable plugin

```python
#!/usr/bin/env python3
import json, sys

def send(**message):
    print(json.dumps(message), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    event, context = message["event"], message.get("context")
    if event == "hello":
        send(event="ready")
    elif event == "will_appear":
        send(event="set_title", context=context, payload={"title": "Hello"})
    elif event == "ping":
        send(event="pong")
    elif event == "shutdown":
        break
```
