# Plugin actions revamp — design

Status: draft for approval. Date: 2026-11-21. Branch: `feat/plugin-actions-revamp` (Decker, off `feat/dynamic-choice`)
and `feat/plugin-actions-revamp` (decker-plugins, off `main`).

## Intent

Format 2 split cards from actions, but cards still handle presses themselves (`on_press`, `set_gestures`), the Run
Action card and the Switch card are the only ways to bind an action to a key, and the HTTP Request action runs inside
the server. That leaves two meanings of "what a key does" and blurs the card/action separation.

The revamp makes **cards pure views** and **actions the only thing a key does**:

- **Cards** draw a key (and keep raw key down/up for visual feedback).
- **Actions** do the work. An action is either **global** (any plugin's cards and code may use it: HTTP request, media
  play/pause, volume up) or **plugin** scoped (only its own plugin's cards and code: timer start/stop, counter
  increment).
- **Every card has three bindings**, press, double press and long press, each holding one action plus its arguments,
  or the built-in **Do Nothing**. The manifest supplies defaults; the user can rebind any of them in the card editor.
- **Every action runs in its plugin's process**, including the built-in ones. Cards and actions of one plugin share
  that process, so they share data (a sensor read once feeds several cards).

The first consumer is the planned Utilities plugin (`admin/todo.user.md`): Brightness Level, Timer, Sleep and similar
cards whose gestures map to configurable plugin actions such as "+10% brightness".

## Goals

- Format 3 manifest: action `scope`, gesture binding defaults on cards, declarative argument validation.
- Per-card gesture bindings stored with the key and edited in the card editor; registered gestures derive from them.
- `ActionCallContext` and `CardProxy` in the SDK.
- Plugins call actions from their own code (`call_action`) and get return values.
- Double-buffered canvas painting with locked paints and frame-ticked flushing.
- HTTP Request moves into a built-in Core plugin process.
- Counter, Pages, Profiles (decker-plugins) and the built-in Core and Blank plugins migrated.
- Plugin docs (`docs/plugins/`), CLAUDE.md and READMEs updated in the same branch.

## Non-goals

- **Action chains.** A separate Sequence plugin will chain actions through `call_action`; it will need an
  "action list" settings type. This design keeps `action`/`action_params` extensible toward that but builds neither.
- Plugin-side validation hooks (a `@validate` round trip like `options_source`). Declarative rules only for now.
- Result schemas for action return values (an optional `returns` description is documentation only).
- Changing another plugin's card configuration. The card proxy is read-only for config; only the owning plugin's card
  code may call `set_settings`.
- A `"canvas": "full"` mode that paints over background and title.
- Format 2 compatibility.
- Card swapping (the retired Switch card's "show another card" slots). Dropped for now (user's decision); it may return
  as a "show card" action or in the Sequence plugin.
- Foreign plugins painting a card after their action has returned.

## Anti-goals

- A half-migrated `main`: nothing merges until the whole branch (both repos) is done.
- A compatibility layer for format 2 plugins.
- A painted frame that is half drawn reaching the key.
- Painting that erases the user's configured background, title or icon override.

## Constraints

- Decker conventions in `CLAUDE.md` (quality gates, mypy for linux/win32/darwin, ≤ 5 args, methods < 50 lines,
  coverage ≥ 85%, five platform tiers).
- The plugin process model stays: one child per plugin, JSON lines over stdio, supervisor, watchdog, flood protection
  (100 msg/s, burst 200), lazy start for plugins without variables.
- The SDK stays dependency-free at import time; Pillow is needed only to use the canvas.
- The screensaver rule stays: waking never fires an action.

## Approaches

1. **Phases in one branch, in dependency order** (chosen). Each phase is reviewed and tested on its own; nothing merges
   until all are done, so `main` never holds a half-migrated plugin system.
2. **Two or three separately merged projects** (bindings/actions, then calls, then canvas). Smaller reviews, but every
   merge has to keep decker-plugins working, so plugins are migrated more than once. Wasted work with a clean break.
3. **New runtime beside the old, then swap.** Safest rollback, but it is the compatibility layer already ruled out.

The user chose 1.

## Design

### Manifest format 3

Format 3 is the only accepted format; format 1 and 2 manifests are refused with a message naming the format. The
protocol version becomes 4.

**Actions** (`contributes.actions[]`):

| Field | Notes |
|-------|-------|
| `id`, `name`, `tooltip`, `icon` | As in format 2. |
| `scope` | `"global"` or `"plugin"`; default `"plugin"` so nothing is public by accident. |
| `settings` | Settings schema for the arguments (all existing field types and `default`, plus the validation rules below). |
| `returns` | Optional text describing the return value; documentation only. |

**Cards** (`contributes.cards[]`) lose `gestures`, the `card`/`action`/`action_settings` preset form and `behavior`.
They gain `bindings`:

```json
"bindings": {
  "press":        { "action": "com.decker.utilities.brightness_step", "args": { "step": 10 } },
  "double_press": { "action": "com.decker.utilities.brightness_step", "args": { "step": -10 } },
  "long_press":   { "action": "com.decker.utilities.brightness_set",  "args": { "level": 75 } }
}
```

An omitted gesture defaults to Do Nothing (`com.decker.core.nothing`). A binding may name the plugin's own actions of
either scope, any global action, or a built-in. `static` cards stay: no process and no code of their own, but they may
bind global actions (which run in their own plugin's process), and their plugin's own actions if it has a runtime.

A card binding a plugin-scoped action must belong to the same plugin (manifest error otherwise). A binding to an action
that is not installed is kept and acts as Do Nothing, like a Switch slot naming something missing today.

**Validation rules** added to the settings schema, checked by the server (`apply_schema` gains a strict mode that
reports errors instead of silently falling back) and shown inline by the client form:

- `required` (bool): an empty value (`""`, `null`, missing) is an error.
- `pattern` (text): a regular expression the whole value must match (Python `re.fullmatch`, compiled at manifest load;
  an invalid pattern is a manifest error).
- `min_length`, `max_length` (text).
- `default` already exists on every field and is unchanged.

### Bindings in storage and the server

- A key's stored assignment gains `bindings: {gesture: {action, args} | null}`. A missing gesture means "use the
  card's manifest default"; an explicit `{"action": "com.decker.core.nothing"}` means the user chose Do Nothing. This
  keeps manifest default changes flowing to keys the user never touched.
- The server registers gestures on the key's `GestureRecognizer` from the effective bindings: a gesture bound to Do
  Nothing is not registered (so a press fires at once when double press is unbound). `set_gestures` and the SDK's
  `gestures(settings)` are removed.
- On a recognised gesture, the server builds the call (action id, validated args, input type, timestamp, card info)
  and hands it to `ActionRunner`. Cards no longer receive `press`/`double_press`/`long_press` events; they still
  receive `key_down`/`key_up`.
- Arguments that use `page`, `profile` or template text fields are resolved/expanded for the key's device, as action
  settings are today.
- **Cards see their bindings** (designer's call): `will_appear` and `settings_changed` carry the effective
  `bindings` (read-only), and a binding edit sends `settings_changed`. Pages' Go To card titles itself from its press
  binding's `page` argument instead of keeping a duplicate setting.
- A Blank card dropped from the Actions tab shows the press action's icon and name when the user set none (designer's
  call), so the key is never empty.
- **Invalid stored values** (designer's call): strict validation applies to card settings and action arguments on save
  (the save RPC returns field errors, the client shows them and refuses Save). A stored value that a later manifest
  makes invalid still loads and is shown flagged in the editor; a gesture whose arguments fail validation does not run
  and raises `action_failed` with the field error.
- **Dynamic choices in argument forms** (designer's call): `plugin_options` takes the owner of the schema being edited,
  so an action's `options_source` is answered by the action's plugin, started lazily as today.
- Run Action (`com.decker.core.runaction`) and Switch (`com.decker.core.switch`) are retired, together with the
  `slots` settings type, `server/switch.py`, `common/slots.py` and `widgets/slots_edit.py`. A stored key that names
  a retired card loads as an empty key (logged); no data migration of stored pages is attempted (early alpha).
- The picker's Actions tab drops an action onto a key as a **Blank** card whose press is bound to that action.

### Built-in actions and the Core process

- **Do Nothing** (`com.decker.core.nothing`, icon X) is the only action the server handles itself: it is never
  dispatched and its gesture is not registered.
- **HTTP Request** moves from `server/builtin_actions.py` into the Core plugin's own code, run as a normal plugin
  process. Built-in plugins with a `runtime` run with the **server's own interpreter and its copy of the SDK** (no uv
  environment), so they work offline on a fresh install. They are supervised, lazily started and restarted like any
  plugin. Core gains `"runtime": {"type": "python", "entry": "plugin.py"}` and a `builtin` flag the loader already
  implies from `builtin_plugins/`.
- Core's actions are `scope: "global"`.

### Action call protocol

`run_action` (server → plugin) payload, protocol 4:

```json
{ "action": "<id>", "request": "<id>", "args": {...},
  "input": "press" | "double_press" | "long_press" | "plugin",
  "called_at": "2026-11-21T10:00:00.123+00:00",
  "card": { "context": "<ctx>", "card": "<card id>", "plugin": "<plugin id>", "device": "<device id>",
            "key": {"index": 0, "column": 0, "row": 0}, "size": [72, 72], "settings": {...},
            "last_paint": "<iso time>" | null } | null,
  "caller": "<plugin id>" | null, "chain": "<id>", "depth": 0 }
```

`action_result` gains `"value": <any JSON>` on success (≤ 1 MiB encoded; larger is an error). Timeout stays 30 s by
default.

**Plugin-initiated calls** (plugin → server): `call_action {"request", "action", "args", "timeout"?, "chain"?,
"depth"?}`, answered with `call_result {"request", "ok", "value" | "error"}`.

- A plugin may call its own actions of either scope and any other plugin's `global` actions; anything else is refused.
- The server validates `args` against the action's schema before the callee sees them.
- The call carries the caller's chain id and depth (an action calling out passes its own; a top-level call starts a
  new chain). The server refuses depth > 8.
- **Device without a card** (designer's call): the chain carries a `device` when it started from a card, so actions
  called inside a gesture's chain resolve `page`/`profile` arguments and templates for that device. `call_action` also
  accepts an explicit `device`. With neither, `page`/`profile` arguments arrive as their unset values and device
  templates expand to empty text.
- `input` is `"plugin"`, `card` is `null`, `caller` is the calling plugin.
- Gesture-triggered runs discard the value; a failure still raises the existing `action_failed` event.

### SDK

```python
from decker_plugin_sdk import ActionCallContext, Input, action, call_action

@action("com.decker.utilities.brightness_step")
def step(ctx: ActionCallContext) -> int:
    ctx.called_at   # datetime (aware)
    ctx.input       # Input.PRESS | DOUBLE_PRESS | LONG_PRESS | PLUGIN
    ctx.card        # CardProxy | None
    ctx.args        # dict, validated
    ctx.caller      # str | None
    return 42       # any JSON-encodable value

value = call_action("com.decker.core.httprequest", {"method": "GET", "url": "..."}, timeout=30)  # raises ActionError
value = await call_action_async(...)
```

- Actions run on the SDK worker pool; a nested `call_action` blocks only its own worker, and the pool grows for
  nested calls so a chain cannot starve itself.
- An exception in the action becomes `ok: false` with its message (as today).
- `@card` classes keep `on_will_appear`, `on_will_disappear`, `on_settings_changed`, `on_global_settings_changed`,
  `on_key_down`, `on_key_up`, and gain `on_paint(ctx, img)` (see Painting). `on_press`, `on_double_press`,
  `on_long_press` and `gestures()` are removed.

**`CardProxy`**:

| Member | Meaning |
|--------|---------|
| `context`, `card_id`, `plugin_id`, `device` | Identity. |
| `key_index`, `column`, `row`, `size` | Physical properties (`size` is the key's pixel size). |
| `settings` | Read-only mapping of the card's configuration (a copy; there is no setter on the proxy). |
| `last_paint` | When the key last received a frame (`datetime | None`). |
| `canvas` | The card's back buffer (below). |
| `queue_paint()` | Ask for the buffer to be sent on the next frame; for the card's own plugin it also asks `on_paint`. |
| `show_ok()`, `show_alert()` | As today. |
| `switch_page(...)`, `go_to_page(...)`, `switch_profile(...)`, `go_to_profile(...)` | Device navigation for the card's device (so the Pages and Profiles actions work as global actions). |

For a card of the **same plugin** the proxy also exposes `instance` (the live `@card` object, so an action can change
the card's state, e.g. Counter's count) and `set_settings(...)` (the owning plugin may configure its own cards). Plugins
may equally keep state keyed by `context`. *(Builder's call, from the rule "only the own plugin can set
configuration".)*

For a card of the same plugin, the proxy wraps the live `CardContext` in-process. For a card of another plugin, it is
built from the `card` payload and its calls go through the server: `set_image` and navigation from the action's plugin
are accepted for that context **only while the call is in flight**, keyed by the request id (user's decision; ongoing
effects belong in the card's own plugin; longer access may come later). Before sending `action_result`, the SDK flushes
any dirty snapshot for foreign cards, so a paint finished inside the action always lands. Config writes are refused.

### Painting

The canvas is only the **plugin image layer**, the layer `set_image` sets today. The server still draws the user's
background under it and the title over it, and the user's icon override still wins.

```python
with card.canvas.paint() as img:   # Pillow Image, RGBA, the key's pixel size
    draw(img)
```

- Each painting process holds a back buffer per key. The first `canvas` access for a key of another plugin fetches the
  key's current plugin image from the server (`get_image {request, context}` → `image_result {request, image,
  format}`); for the plugin's own cards it starts from the last frame it sent (or transparent).
- `paint()` takes a counting lock. Several painters may hold it at once; only when the **last** painter releases does
  the SDK copy the buffer into a **snapshot** and mark the key dirty. Leaving `paint()` counts as `queue_paint()`.
- A per-process **frame tick** (about 30 fps, ~33 ms) sends each dirty key's latest finished snapshot once, as
  `set_image` PNG. It never sends the live buffer, so a half-drawn frame cannot leave, and it never waits for a painter.
  No dirty keys means nothing is sent and the key keeps its current image.
- `queue_paint()` on a card of the plugin's own process calls the card's `on_paint(ctx, img)` inside `paint()` before
  the next tick, coalescing repeated requests into one call.
- The latest frame wins between processes: A's next frame covers B's paint and vice versa.
- The server records the time of every image it accepts for a key (`last_paint`).
- Pillow is imported lazily; using the canvas without Pillow raises a clear `RuntimeError`. `set_image(bytes)` stays
  for plugins without Pillow and for executable plugins. A plugin that paints lists `pillow` in `requirements`;
  built-in plugins use the server's Pillow.

### Client

- **Card editor**: below the card's generated settings, three rows (Press, Double press, Long press), each an action
  dropdown (`[icon] Action name ▼`, as in the brief's mockup) listing Do Nothing, the card's own plugin's actions and
  every global action, followed by that action's generated argument form or "This action has no settings". Changing
  the action resets its arguments to the action's defaults. A row shows the manifest default until the user changes it,
  with a way to reset to default.
- Validation errors from the new rules are shown inline on the field, and Save is refused while any remain.
- The Actions tab lists only global actions (plugin-scoped ones are reachable only from their own cards).
- `action_edit.py` is reused for the dropdown; `slots_edit.py` is removed.

### Migration (decker-plugins and built-ins)

- **Counter**: increment/decrement/reset become plugin-scoped actions bound by default to press/double/long press;
  the card paints on `queue_paint`.
- **Pages**: Next, Previous, Go to page become global actions using `CardProxy` navigation (a plugin call without a
  card is an error for them); the cards bind them by default.
- **Profiles**: same pattern as Pages.
- **Core**: Do Nothing (server), HTTP Request (Core process); Run Action and Switch removed.
- **Blank**: format 3, unchanged otherwise; its cards get bindings like any card.
- decker-plugins pins the final Decker commit of the branch; manifests require `decker >= 0.7`.

### Phases (one branch per repo)

1. Format 3 manifest, action `scope`, validation rules, protocol 4 constants.
2. Bindings: storage, gesture registration, dispatch, Do Nothing, editor rows; Run Action and Switch retired.
3. `ActionCallContext` and `CardProxy` without canvas (identity, config, navigation, show_ok/alert).
4. `call_action`/`call_result`, return values, chain and depth limit.
5. Core as a built-in out-of-process plugin; HTTP Request moved.
6. Canvas, frame tick, `get_image`, `last_paint`, `on_paint`.
7. Migrate Counter, Pages, Profiles; docs, CLAUDE.md, READMEs.

## Left to the builder

- Exact module layout for binding storage and the call builder.
- Frame tick implementation details (thread vs. timer), and the exact worker-pool growth policy.
- Error wording and log levels.
- Test structure, as long as each phase has unit tests and an end-to-end test through a real plugin process.
