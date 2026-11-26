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

A card draws; it does not handle presses. It gets the raw `on_key_down`/`on_key_up` for visual feedback, and what a
press does is the action bound to the gesture in the manifest's `bindings` (see
[package and manifest](package-and-manifest.md#bindings)).

```python
from decker_plugin_sdk import CardContext, DeckerPlugin, card


@card("com.example.hello.greet")
class Greet(DeckerPlugin):
    def on_will_appear(self, ctx: CardContext) -> None:
        ctx.set_title(ctx.settings.get("name", "world"))

    def on_key_down(self, ctx: CardContext) -> None:
        ctx.set_title("...")
```

## A minimal action

An action is a function that draws nothing; users run it from the bindings of a card. Declare it under
`contributes.actions` in the manifest (with its `scope`) and register the function. It receives an
`ActionCallContext` and may return any JSON-encodable value (at most 1 MiB encoded):

```python
from decker_plugin_sdk import ActionCallContext, ActionError, Input, action


@action("com.example.hello.sendwave")
def send_wave(ctx: ActionCallContext) -> int:
    times = int(ctx.args.get("times", 1))
    if times > 10:
        raise ActionError("too many waves")  # the user sees exactly this message
    if ctx.input is Input.LONG_PRESS and ctx.card is not None:
        ctx.card.show_ok()
    return times  # the value reaches whoever called the action
```

Actions run on the SDK's worker pool, independently of any key, and may overlap. Return normally for success; an
exception is reported as the action's error. An `ActionError(msg)` shows the bare message; any other exception shows
`"<Type>: <message>"` (for example `ValueError: boom`); every action exception is also logged. A return value that is not JSON-encodable or is
larger than 1 MiB is an error.

`ActionCallContext` attributes:

| Attribute | Meaning |
|-----------|---------|
| `action` | The action id that was called |
| `request` | The call's request id |
| `called_at` | When the call was made, a timezone-aware `datetime` |
| `input` | `Input.PRESS`, `Input.DOUBLE_PRESS`, `Input.LONG_PRESS` or `Input.PLUGIN` (a call from plugin code) |
| `card` | A `CardProxy` for the key that fired, or `None` for a `plugin` call |
| `args` | The validated arguments (a `dict`) |
| `caller` | The calling plugin's id for `plugin` calls, else `None` |
| `device_id` | The device the call runs for (from the card or the chain), else `None` |
| `switch_page(to, wrap)`, `go_to_page(page_id)`, `switch_profile(to, wrap)`, `go_to_profile(profile_id)` | Navigate `device_id` while the call runs; raise `ActionError("no device for this call")` when it is `None` |

### CardProxy

`ctx.card` describes the key an action was bound on. It works only while the call is in flight.

| Member | Meaning |
|--------|---------|
| `context`, `card_id`, `plugin_id`, `device` | Identity |
| `key_index`, `column`, `row`, `size` | Physical properties; `size` is the key's pixel size `(width, height)` |
| `settings` | A read-only copy of the card's configuration |
| `last_paint` | When a plugin last painted the key (`datetime` or `None`) |
| `canvas` | The key's drawing surface, see [Painting](#painting) |
| `queue_paint()` | Ask for the canvas buffer to be sent again |
| `show_ok()`, `show_alert()` | As on `CardContext` |
| `switch_page(to, wrap)`, `go_to_page(page_id)`, `switch_profile(to, wrap)`, `go_to_profile(profile_id)` | Navigation for the card's device, so global actions such as Next Page work from any plugin |

For a card of the **same plugin** the proxy is an `OwnCardProxy`: it also has `instance` (the live `@card` object, so an
action can change its state) and `set_settings(dict)`; it stays usable after the call and its `queue_paint()` runs
`on_paint`. For a card of **another plugin** there is no setter, config writes are refused, and the card is touchable
only until the action returns (see [Foreign cards](protocol.md#foreign-cards)); drawing on it or sending it a command later raises `RuntimeError`.

### Calling actions

```python
from decker_plugin_sdk import ActionError, call_action, call_action_async

try:
    result = call_action("com.decker.core.httprequest", {"method": "GET", "url": "https://example.com"}, timeout=30)
except ActionError as exc:   # refused, failed or timed out; the message says why
    ...

result = await call_action_async("com.example.hello.sendwave", {"times": 2})  # in async code
```

`call_action(action_id, args=None, timeout=None, device=None, slot=None)` (`slot` names the caller's variable slot, protocol 6; `ctx.set_variable(name, value)` sets a variable from an action and raises `ActionError` if refused) blocks until the action returns its value and raises
`ActionError` when the call is refused (see [Action calls](protocol.md#action-calls) for who may call what), fails or
times out; `timeout` is capped at 300 s. `timeout=None` (the default) lets the server use the called action's manifest
`timeout` (1-300 s) and otherwise 30 s; an explicit `timeout` overrides it. A nested call is also capped by the time its
caller has left. Inside an action it forwards the call chain, so the depth limit of 8 and the
device apply automatically. A nested call blocks only its own worker, and the pool grows for nested calls so a chain
cannot starve itself. `call_action` outside a running plugin raises `RuntimeError`.

### Painting

A card can draw its key with Pillow. Add `pillow` to `runtime.requirements` (the SDK imports it only when you use the
canvas; without it, `RuntimeError` says so). `set_image(bytes)` still works for plugins without Pillow.

```python
from decker_plugin_sdk import CardContext, DeckerPlugin, card


@card("com.example.hello.dial")
class Dial(DeckerPlugin):
    def on_will_appear(self, ctx: CardContext) -> None:
        ctx.queue_paint()

    def on_paint(self, ctx: CardContext, img) -> None:   # img: Pillow RGBA image, the key's size
        from PIL import ImageDraw
        ImageDraw.Draw(img).ellipse((8, 8, img.width - 8, img.height - 8), outline="white", width=4)
```

- `ctx.canvas.paint()` (or `card.canvas.paint()` on a proxy) is a context manager yielding the RGBA back buffer. Several
  painters may hold it at once; when the **last** one leaves, a snapshot is taken and the key is marked dirty (leaving
  `paint()` counts as `queue_paint()`).
- A per-process **frame tick** (about 30 per second) sends the latest finished snapshot of dirty keys as `set_image`
  PNG. It sends at most 2 frames per tick (about 60 per second) to stay under the 100 messages per second flood limit,
  the remaining dirty keys wait for the next tick, and a half-drawn buffer is never sent. For a foreign card the frame
  is sent before `action_result`.
- The canvas is only the plugin image layer: Decker still draws the user's background and title around it, and the
  user's icon override wins. The latest frame wins when several plugins paint one key.
- A plugin's own cards start from the last frame they sent (or transparent); a foreign card's first `canvas` access
  fetches the key's current image with `get_image`.
- `ctx.queue_paint()` asks for `on_paint(ctx, img)` to run inside `paint()` before the next tick; repeated requests
  before it runs count once. `ctx.last_paint` is the time this plugin last sent a frame for the key.

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
- `get_variable`, `has_variable` and `connected_devices` return `None`, `False` and `[]` when no runtime is running (for
  example in unit tests); the other functions raise `RuntimeError` then.

## Decks coming and going

`connected_devices()` returns the ids of the decks the server reports as connected (sorted). Register
`@on_device_connected` and `@on_device_disconnected` functions taking the device id; they run on the worker pool, once
per message. The hooks must be plain (synchronous) callables: an `async def` handler is not awaited. A deck that reopens sends a disconnect and then a connect, so the list ends up correct; a repeated connect
changes nothing in the list.

```python
import decker_plugin_sdk as sdk

@sdk.on_device_connected
def seen(device_id: str) -> None:
    print("deck up:", device_id, sdk.connected_devices())
```

## Deck brightness

`get_deck_brightness(device_id=None, timeout=5.0)` returns a deck's normal brightness (1-100) and
`set_deck_brightness(percent, device_id=None)` sets it (clamped to 1-100; it is stored as the screensaver's normal
`brightness`, so it survives a wake). Without `device_id` they use the device of the current action call; a card passes
`ctx.device` explicitly. Both raise `ActionError` when there is no device; `get_deck_brightness` also on an error or
timeout. `@on_deck_brightness_changed` registers `fn(device_id, percent)`, run on the worker pool whenever any deck's
normal brightness changes.

```python
@sdk.on_deck_brightness_changed
def dimmed(device_id: str, percent: int) -> None:
    print(device_id, "is now at", percent)

sdk.set_deck_brightness(sdk.get_deck_brightness(device_id) - 10, device_id)
```

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
    def on_will_appear(self, ctx: CardContext) -> None:
        ctx.set_title(str(ctx.settings.get("device")))
```

The function takes no arguments, may be `async def`, and runs on the SDK's worker pool, so a slow one does not block
`ping`. It must return a list (or tuple). An exception, a result that is not a list, or a source nobody registered is
answered with an error, which the form shows in the control's tooltip. Entries that are not a dict, a pair (a tuple or a two-item list) or a string,
and entries whose text cannot be encoded as UTF-8, are dropped. The server then cleans the list (see
[protocol](protocol.md#dynamic-choice-options)); the setting arrives at the card as the stored string, whether or not it
is still listed. A manifest that uses `options_source` must set its `decker` constraint to the release that supports it (see [Dynamic choices](settings-schema.md#dynamic-choices)).

## Handlers (override what you need)

`on_will_appear`, `on_will_disappear`, `on_key_down`, `on_key_up`, `on_settings_changed`,
`on_global_settings_changed` and `on_paint(ctx, img)`. The handlers `on_press`, `on_double_press`, `on_long_press`
and `gestures(settings)` no longer exist (protocol 4): bind actions in the manifest instead.

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

Attributes: `context`, `card`, `device`, `key_index`, `column`, `row`, `settings`, `global_settings`, `bindings` (the effective bindings, read-only), `size`, `state`, `last_paint`, `canvas`.

Methods: `set_image(data, format="png")`, `set_icon(ref, color=None)` (show an icon: an installed icon pack, one of Decker's System Icons, or a built-in Font Awesome short ref such as `fas:play`, see
[protocol](protocol.md#images)), `clear_image()`, `set_title(title)`, `set_state(n)`, `show_alert()`,
`show_ok()`, `queue_paint()`, `set_settings(dict)` (send only the keys you changed), `set_global_settings(dict)`, `switch_page(to="next", wrap=False)`
(`next`, `previous`, `first`, `last`), `go_to_page(page_id)` (see [Pages](protocol.md#pages)), `switch_profile(to="next", wrap=False)`, `go_to_profile(profile_id)` (see [Profiles](protocol.md#profiles)), `open_url(url)`, `log(message, level="info")`.

The SDK accepts either `global_settings` or `settings` as the key in `global_settings_changed`; the server sends
`global_settings`.
