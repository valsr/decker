# Decker plugins

A Decker plugin is a folder that is shipped as a `.deckerplugin` package. This guide describes everything you need
to build one without reading Decker's source.

- [Package and manifest](package-and-manifest.md): archive layout, `manifest.json` fields, contribution kinds
- [Settings schema](settings-schema.md): the field types that generate settings forms
- [Protocol](protocol.md): the newline-delimited JSON messages exchanged with the Decker server
- [Python SDK and CLI quick start](sdk-quickstart.md): `poe cli plugin new|pack|install`
- [Limits, lifecycle and the launch environment](limits.md)
- [Plugin repositories](repositories.md): index format, update checks, hosting your own repository

Cards only draw; actions do the work. Every card binds its `press`, `double_press` and `long_press` gestures to an action
with arguments (or to the built-in Do Nothing), plugin code can call actions with `call_action`, and cards can draw
with a double-buffered Pillow canvas. Manifest format 3 and protocol 4 (Decker 0.7) replaced the Run Action and Switch
cards, card presets, `gestures`, `set_gestures` and the `slots` setting type; formats 1 and 2 are refused. `{page_name}`-style
templates, `page` settings and `switch_page` are described in the manifest, settings-schema and protocol pages above.
Decker ships two built-in plugins: Blank (static cards, including Page Name and Page Count) and Core (the Do Nothing
action, and the HTTP Request, Delay and Sequence actions, which run in a Core plugin process).

Plugins run with your user's permissions and are not sandboxed. Only install plugins from authors you trust.

## What a plugin can contribute

| Kind | What it is | Needs a runtime |
|------|------------|-----------------|
| `cards` | What a key shows: they appear in the card list (the key picker). Each binds its gestures to actions. A card that is not static runs as a separate process | for code-backed cards (not `static`) |
| `actions` | Headless things to do (a function with settings, e.g. an HTTP request). `scope` says who may use them: `global` (any card, any plugin's code) or `plugin` (only this plugin). Cards run them through their bindings | yes |
| `icon_packs` | A folder of images browsable from the image library | no |
| `variables` | Global text values other text can use as `{name}`; the plugin starts with the server and keeps running | yes (and a `decker` constraint) |

Decker draws the key background and title (the user's appearance settings). Your plugin supplies the key image with
`set_image` and may supply a fallback title with `set_title`.

## Known limitations (v1)

- `show_alert` and `show_ok` are accepted but have no visible effect yet. When a plugin fails (crashes repeatedly),
  its keys are cleared and show the label "Plugin failed" in the client; there is no hardware alert animation.
- The plugin process gets a minimal environment (see [limits](limits.md)). Variables such as `DISPLAY`,
  `WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` are not passed on, so plugins that talk to the
  desktop session (media keys over D-Bus, notifications, launching GUI programs) will not work without extra effort
  such as locating the session themselves. There is no allow-list setting in v1.
- `device_connected` is sent for the devices known when the plugin becomes ready and again whenever a deck connects;
  `device_disconnected` is sent when one goes away.
