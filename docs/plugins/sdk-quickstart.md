# Python SDK and CLI quick start

`decker_plugin_sdk` is a small, dependency-free package that Decker provides to Python plugins. It runs the stdio
loop, redirects `print` and any stray output to stderr (the protocol pipe is protected), and maps handler calls to
the [protocol](protocol.md). Handlers run off the reading thread, so a slow handler does not block `ping`.

## Create, pack and install

```bash
poe cli plugin new com.example.hello      # scaffold ./com.example.hello (manifest, plugin.py, icons)
poe cli plugin pack com.example.hello     # validate and write com.example.hello-0.1.0.deckerplugin
poe cli plugin install com.example.hello-0.1.0.deckerplugin
poe cli plugin list
poe cli plugin remove com.example.hello
```

- `install` uploads the package to the running server, prints progress while the environment is built and exits
  with a non-zero status if the install fails. It works without restarting anything. Only one install or uninstall
  runs at a time; a second one is refused with a "busy" error.
- Installing is only allowed from the server's own machine unless `server.allow_remote_plugin_install` is `true`.
- `uv` must be installed on the server machine for plugins with a `python` runtime.

## A minimal card

```python
from decker_plugin_sdk import CardContext, DeckerPlugin, card


@card("com.example.hello.greet")
class Greet(DeckerPlugin):
    def on_will_appear(self, ctx: CardContext) -> None:
        ctx.set_title(ctx.settings.get("name", "world"))

    def on_press(self, ctx: CardContext) -> None:
        ctx.set_title("Hello!")
```

## A minimal action

An action is a function with settings that draws nothing; users run it from the built-in **Run Action** card (or a
preset card you ship). Declare it under `contributes.actions` in the manifest and register the function:

```python
from decker_plugin_sdk import action


@action("com.example.hello.sendwave")
def send_wave(settings: dict) -> None:
    for _ in range(int(settings.get("times", 1))):
        ...  # do the work; raise to report a failure, which the user sees
```

Actions run on a worker thread, independently of any key. Return normally for success; an exception is reported to
the user as the action's error.

## Variables, `init` and `main`

A plugin that declares [`contributes.variables`](package-and-manifest.md#variables) exports global text values for
templated text, and keeps running with no card on screen. Three decorators (sync or `async def`) shape the plugin:

- `@init` runs once per process start with a `PluginContext`, in the lifecycle thread **after** `ready` has been sent, so a
  slow `init` cannot trip the 10 s ready timeout or delay `will_appear`. Register variables and read initial state
  there. If it raises, the error is logged and `main` does not start; cards and actions keep working.
- `@main(interval=2.0)` runs repeatedly in the same lifecycle thread, after `init` has returned, pausing `interval` seconds between calls (bare `@main` means
  2.0), until the plugin shuts down. An exception is logged and the loop continues. `shutdown` stops it. An
  `async def` main with `@main(interval=None)` is called once and runs for the life of the plugin (an event-driven
  watcher); it is cancelled at shutdown. Several `@main` hooks run one after another, so a second one never runs while the first loops. Without `main` the thread simply ends and the process stays up.
- `@variable("name")` or `@variable("pattern_{x}")` marks the getter of a manifest variable. A pattern getter receives
  the placeholder values as keyword arguments. A getter returns `str`, `int`, `float`, `bool` (as `"true"`/`"false"`) or
  `None` (unset); a longer value than 1024 characters or another type fails the getter and the old value stays.

```python
from decker_plugin_sdk import PluginContext, init, main, variable

_count = 0
_items = {"red": 3, "blue": 5}   # pattern: item_{color}_stock

@init
def setup(ctx: PluginContext) -> None:
    for color in _items:
        ctx.register_variable(f"item_{color}_stock")

@main(interval=2.0)
def poll(ctx: PluginContext) -> None:
    global _count
    _count += 1
    ctx.signal_variable_updated("ticks")

@variable("ticks")
def ticks() -> int:
    return _count

@variable("item_{color}_stock")
def stock(color: str) -> int | None:
    return _items.get(color)
```

with `"variables": [{"name": "ticks"}, {"pattern": "item_{color}_stock"}]` in the manifest. A key text `Ticks {ticks}`
then shows the counter and redraws on each signal.

`PluginContext` (and the same functions at module level in `decker_plugin_sdk`, usable from cards and actions too):

| Function | Meaning |
|----------|---------|
| `register_variable(name)` | announce a concrete name inside one of your patterns; needs a matching `@variable` getter (logged and skipped otherwise). Exact manifest names need no call |
| `unregister_variable(name)` | withdraw a registered name, for example when a device went away |
| `signal_variable_updated(name)` | the value changed: the getter is evaluated, the local copy updated and the server told to refresh the cache |
| `get_variable(name)` | the current text of any global variable, or `None`; reads a local copy fed by the server, so it is instant |
| `has_variable(name)` | whether the variable has a value |

`PluginContext` also has `log(message, level="info")`. Behaviours to know:

- Getters are registered when the module is imported and answer as soon as `ready` is sent, even before `init`
  finishes.
- An exact getter wins over a pattern getter; among patterns the first registered wins.
- Each async hook or getter call runs on its own event loop, so an async client created in `init` cannot be reused in
  `main` or in getters; create it where you use it.
- Calling a sync SDK function from an async hook blocks that hook's loop while an async getter runs.
- `get_variable` and `has_variable` return `None` and `False` when no runtime is running (for example in unit tests);
  the other functions raise `RuntimeError` then.

## Dynamic choice options

A `choice` field with an [`options_source`](settings-schema.md#dynamic-choices) lists options the plugin supplies when
the settings form opens. Declare the field in the manifest:

```json
{ "key": "device", "type": "choice", "label": "Output device", "default": "default",
  "options": [{ "label": "Default output", "value": "default" }],
  "options_source": "output_devices" }
```

and register one function per source name with `@options`:

```python
from decker_plugin_sdk import CardContext, DeckerPlugin, card, options


@options("output_devices")
def output_devices() -> list:
    return [
        {"label": "Speakers (Realtek)", "value": "speakers_realtek"},  # a dict
        ("Headphones (USB)", "headphones_usb"),                        # a (label, value) tuple
        "hdmi",                                                        # a string: label and value are the same
    ]


@card("com.example.hello.volume")
class Volume(DeckerPlugin):
    def on_press(self, ctx: CardContext) -> None:
        ctx.set_title(str(ctx.settings.get("device")))
```

The function takes no arguments, may be `async def`, and runs on the SDK's worker pool, so a slow one does not block
`ping`. It must return a list (or tuple). An exception, a result that is not a list, or a source nobody registered is
answered with an error, which the form shows in the control's tooltip. Entries that are not a dict, a pair (a tuple or a two-item list) or a string,
and entries whose text cannot be encoded as UTF-8, are dropped. The server then cleans the list (see
[protocol](protocol.md#dynamic-choice-options)); the setting arrives at the card as the stored string, whether or not it
is still listed. A manifest that uses `options_source` must set its `decker` constraint to the release that supports it (see [Dynamic choices](settings-schema.md#dynamic-choices)).

## Handlers (override what you need)

`on_will_appear`, `on_will_disappear`, `on_key_down`, `on_key_up`, `on_press`, `on_double_press`, `on_long_press`,
`on_settings_changed`, `on_global_settings_changed`, plus `gestures(settings)` which may return a set of `Gesture`
values for this key (return `None` to keep the manifest's list).

## Fonts

Decker bundles the DejaVu family (Sans, Sans Condensed, Sans Mono, Serif, Serif Condensed; regular, bold, italic or
oblique, bold italic) and sets `DECKER_FONT_DIR` to its folder for every plugin process on every OS.
`decker_plugin_sdk.fonts.font_path(family="DejaVu Sans", bold=False, italic=False)` returns the absolute path of a face,
or `None` when the variable is unset, the family is unknown or the file is missing; it never raises. It is not exported
from the package root, and an older Decker has no `decker_plugin_sdk.fonts` module, so guard the import:

```python
try:
    from decker_plugin_sdk.fonts import font_path
except ImportError:  # older Decker
    def font_path(family: str = "DejaVu Sans", bold: bool = False, italic: bool = False) -> str | None:
        return None

path = font_path(bold=True)
font = ImageFont.truetype(path, 24) if path else ImageFont.load_default(24)  # None: use your own default
```

Sized `ImageFont.load_default(size)` needs Pillow >= 10.1.

## CardContext

Attributes: `context`, `card`, `device`, `key_index`, `column`, `row`, `settings`, `global_settings`, `state`.

Methods: `set_image(data, format="png")`, `set_icon(ref, color=None)` (show an icon from an installed icon pack or one of Decker's System Icons, see
[protocol](protocol.md#images)), `clear_image()`, `set_title(title)`, `set_state(n)`, `show_alert()`,
`show_ok()`, `set_settings(dict)` (send only the keys you changed), `set_global_settings(dict)`, `switch_page(to="next", wrap=False)`
(`next`, `previous`, `first`, `last`), `go_to_page(page_id)` (see [Pages](protocol.md#pages)), `switch_profile(to="next", wrap=False)`, `go_to_profile(profile_id)` (see [Profiles](protocol.md#profiles)), `open_url(url)`, `log(message, level="info")`.

The SDK accepts either `global_settings` or `settings` as the key in `global_settings_changed`; the server sends
`global_settings`.
