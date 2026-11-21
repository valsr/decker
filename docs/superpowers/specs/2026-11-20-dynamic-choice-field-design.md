# Dynamic choice field — design

Status: draft for approval. Date: 2026-11-20. Project 2 of 3: plugin variables (done, #54), this field type, then the
Media plugin (`com.decker.media` in `decker-plugins`), whose Volume and Mic cards pick an audio device with it.

## Intent

A `choice` setting can only list options fixed in the manifest. Some lists exist only at runtime: the audio devices of
this machine, the media players that are running. This adds an `options_source` to the `choice` field type, so the
plugin supplies the options when the settings form opens.

## Behaviour

### The field

```json
{ "key": "device", "type": "choice", "label": "Output device", "default": "default",
  "options": [{ "label": "Default output", "value": "default" }],
  "options_source": "output_devices" }
```

- `options_source` is a name, `[a-z][a-z0-9_]*`, at most 64 characters. It belongs to the plugin whose schema declares
  it: there is no global registry, and a plugin is only ever asked for its own sources.
- A `choice` field needs `options` (non-empty), `options_source`, or both. With both, the static options come first and
  the plugin's options follow, without repeating a value already listed.
- With `options_source` the stored value is **any string up to 256 characters** (not only a listed one): a device that
  is unplugged at the moment must keep its saved value. The default is the field's `default` if it is a string, else
  the first static option's value, else `""`. Option values and labels from the plugin are strings.
- The field works wherever a settings schema is used: card settings, action settings (including the Run Action
  parameters), plugin global settings, and the Switch slot forms.

### Asking the plugin

- When a form with such a field is built, the client asks the server for that plugin's options for the source, in the
  background (never on the GUI thread). A refresh button next to the control asks again.
- The server asks the plugin process with a `get_options` request and waits for its `options_result`, up to **5 s**.
  A plugin that is not running is started for the request and released afterwards, like an action run; a failed
  plugin gives an error at once.
- The plugin's answer is cleaned before it is returned: entries that are not `{label: str, value: str}` are dropped,
  labels and values are cut to 256 characters, repeated values are dropped, and at most **256 options** are kept.
- A source the plugin does not define, a timeout, or an error gives the client an error string and no options.

### The control

- An **editable dropdown**: static options first, then the plugin's, and the user can also type any value.
- A stored value that is not in the list stays selected as typed (shown with an "unavailable" tooltip); it is never
  replaced by the first option.
- While the options load, and if they fail, the control still works with the static options plus the current value;
  the tooltip carries the error. The form never waits for a slow or dead plugin.

## SDK surface (Python)

```python
@options("output_devices")            # sync or async; runs in the SDK's worker pool
def output_devices() -> list[dict[str, str]]:
    return [{"label": "Speakers (Realtek)", "value": "speakers_realtek"}]
```

It may return `{"label", "value"}` dicts, `(label, value)` tuples, or plain strings (label = value). An unknown source or
an exception answers with an error.

## Protocol additions (additive; `hello.protocol` becomes 3)

| Direction | Event | Payload |
|-----------|-------|---------|
| server to plugin | `get_options` | `{"request": "<id>", "source": "output_devices"}` |
| plugin to server | `options_result` | `{"request": "<id>", "ok": true, "options": [{"label", "value"}, ...]}` or `{"request", "ok": false, "error": "text"}` |

An SDK without this feature ignores `get_options`, which the server reports as a timeout.

## Goals

- A plugin can offer a runtime list in a settings form, and a saved choice survives the list changing.
- The GUI never blocks on a plugin; a missing list degrades to the static options plus typing.
- The mechanism is generic: Media is its first user, not its only one.

## Non-goals

- Options that depend on other fields of the same form; multi-select; server-side caching; pushing list changes to an
  open form (the user refreshes); a search box for long lists; dynamic options for `page`, `profile` or `action` types.

## Anti-goals (a failure even if it works)

- Replacing a saved value with the first option because it is not listed; blocking the GUI thread on a plugin; the
  server being able to ask a plugin for a source its schema does not declare.

## Constraints

- Server and client stay decoupled; the client reaches plugins only through the server.
- Pure Python and Qt, all five platform tiers. Manifest `decker` constraint of the version that ships this is needed
  by a plugin that uses `options_source` (older Decker would reject the field).
- Docs: `docs/plugins/` (`settings-schema.md`, `protocol.md`, `sdk-quickstart.md`, `limits.md`), `CLAUDE.md`,
  `README.md` change with it. Run mypy for `--platform win32` and `--platform darwin` too.

## Approaches considered

- **A. Plugin-provided options through an `options_source` (chosen).** Generic, no registry, language-neutral on the
  wire. Cost: one request type, an asynchronous control, relaxed validation.
- **B. Options derived from registered variables.** Small, but only works for lists that are also variables and has no
  labels. Rejected.
- **C. The server polls and caches option lists.** Adds staleness and polling for something a form only needs when
  it opens. Rejected.

## Design

### Schema (`plugin/settings_schema.py`)

- `choice` parsing accepts `options_source` (validated name); `options` stays required when there is no source.
  `_coerce` for such a field accepts any string up to 256 characters; the default rule above; `_field_to_json`
  exports `options_source` and the static `options`.

### Server

- `server/options_runner.py`, modelled on `ActionRunner`: `OptionsRunner(supervisor, timeout=5.0)` with
  `fetch(plugin_id, source) -> OptionsResult(ok, options, error)` (acquire/start/release like `_run_in_plugin`),
  `on_result(plugin_id, payload)`, `plugin_stopped(plugin_id)`; results sanitised as above.
- `PluginManager` routes `options_result`, exposes `plugin_options(plugin_id, source)` which first checks that the
  plugin is loaded and that some schema of it (card, action or global settings) declares that `options_source`.
- RPC `plugin_options(plugin_id: str, source: str) -> dict` (`@exposed @api_response`): `{"ok": true, "options": [...]}`
  or `{"ok": false, "error": "..."}`.

### SDK (`decker_plugin_sdk`)

- Decorator `options(source)`, a registry of sources, `get_options` handled in the worker pool, answering
  `options_result`; sync and async functions; the result normalised as above.

### Client (`widgets/schema_form.py` and callers)

- `SchemaForm.set_schema` and `AppearanceForm`'s builder take an optional `plugin_id`; callers pass it where they know
  it (`CardConfigPanel`, `ActionParamsEdit` via the action's plugin, `plugins_tab` global settings, `_SlotConfig`).
- New `DynamicChoiceEdit` widget (an editable combo plus a refresh tool button) loads in a worker thread and fills the
  combo through a Qt signal; used by `_build_field` when a `choice` entry has `options_source` and a plugin id is known
  (without one it falls back to the static options plus typing).

## Left to the builder

- Module and class names beyond those above, the refresh button's icon and placement, request coalescing for
  concurrent fetches of the same source, and the docs wording.
