# Decker 0.7.2 and the Utilities plugin — design

Date: 2026-11-24. Spans two repos: `decker` (release 0.7.2) and `decker-plugins` (new plugin `com.decker.utilities`).

## Intent

Gather the everyday "utility" keys into one first-party plugin — screen and deck brightness, PC sleep, timers, and
the existing Counter, Pages and Profiles — and add the small core pieces that plugin and users need: an About
dialog, Delay and Sequence actions, plugin access to deck brightness, and a fix for a Counter input crash.

## Goals

- Decker 0.7.2: About dialog; Core **Delay** and **Sequence** actions; SDK/protocol access to deck brightness; the
  Counter negative-input crash fixed; version bumped to 0.7.2.
- `com.decker.utilities` 1.0.0: global actions Sleep, Lock screen, Brightness, Deck brightness; cards Brightness Level/Up/Down,
  Deck Brightness Level/Up/Down, Sleep, Timer, Stopwatch, Counter, Page, Profile; variables for the brightness levels.
- All five platform tiers targeted; gaps declared in the manifest and the README platform table.

## Non-goals

- Migrating keys that use `com.decker.counter`, `com.decker.pages` or `com.decker.profiles`. Users re-place those keys
  by hand. The old plugins are not changed or removed from the repository index in this work.
- External-monitor brightness on macOS (no public API).
- A general list/array settings type (Sequence uses fixed numbered fields instead).

## Anti-goals

- Deck brightness set by a plugin must not be undone by the screensaver waking the deck, and must show up in the
  Settings Device tab: one stored brightness, not two.
- A countdown that is meant to be cancellable must never sleep the PC because the press landed on the wrong action.
- Brightness polling must not make the deck or the plugin sluggish (DDC/CI is slow).

## Constraints

- Plugin protocol and manifest rules from `docs/plugins/`; variable names are `[a-z][a-z0-9_]*`, so the requested
  `{brighness-level}`/`{streamdeck-brightness-level}` become `{brightness_level}`/`{deck_brightness_level}`.
- Each gesture runs exactly one action; cards only draw and see raw `key_down`/`key_up`.
- The stored normal deck brightness is range 1–100 (`server/screensaver/settings.py`).
- Call chains: `MAX_DEPTH` 8, default call timeout 30 s, `MAX_CALL_TIMEOUT_S` 300. Today a gesture-run action always
  gets 30 s (`server/plugins.py` builds its `ActionCall` with no timeout; `server/actions.py` falls back to 30 s), and
  the SDK's `call_action` defaults to 30 s.
- Today a nested call carries no card and its `run_action` payload carries no device; the server recovers the device
  only from live pending calls on the same chain. `switch_page`/`switch_profile` are card-context messages, allowed
  for a foreign plugin only while a pending call carries that card. `status` reports no version. Plugins get
  `device_connected` only when they become ready, never on hotplug, and never `device_disconnected`.
- Code style and quality gates from `CLAUDE.md` (black 120, mypy incl. `--platform win32/darwin`, pylint ≥ 9, ≥ 85 %
  coverage).

## Approaches

**Monitor brightness backend.**
- (A, chosen) `screen-brightness-control` for DDC/CI on Linux (via `ddcutil`) and for internal and DDC/CI displays on
  Windows, plus our own Linux laptop-backlight backend through logind `SetBrightness` over D-Bus (`jeepney`, no root),
  plus a small macOS internal-display backend. Less code, proven monitor enumeration.
- (B) Hand-roll everything (`ddcutil` subprocess, logind, Win32 `SetMonitorBrightness`). Full control, much more code
  and monitor edge cases. Rejected.

**Deck brightness access.**
- (A, chosen) New SDK calls and protocol messages (protocol 5) to read and set a device's brightness, plus a change
  event, with the server writing the screensaver's stored normal brightness. Lets cards show the level and keep the
  variable live.
- (B) A server-handled Core action like Do Nothing. Smaller, but cards could not read the level or hear changes, so the
  Level card and variable would not work. Rejected.

## Part 1 — Decker 0.7.2

### Version

`pyproject.toml` `version = "0.7.2"`; Core and Blank built-in manifests to `0.7.2`; anything else that reports the
version (CLI, `status`) reads it from package metadata already or is updated.

### About dialog

- Entry "About Decker…" at the bottom of the dashboard ⚙ menu (`views/dashboard.py`, `_populate_view_menu`) and in the
  tray menu (`tray.py`) above Quit.
- Modal dialog: Decker logo, "Decker", client version, server version when connected (from `status`; "not connected"
  otherwise; `status` gains a `version` field for this), authors (from package metadata), repository link (`https://github.com/valsr/decker`, clickable),
  license name, and a one-line credit for bundled Font Awesome Free and DejaVu fonts. Close button.

### Core: Delay

`com.decker.core.delay`, global, icon `fas:hourglass-half`. Setting `seconds`: `float`, default 1.0, min 0, max 60,
2 decimals, manifest `timeout` 65. Sleeps that long in the Core process and returns nothing.

### Per-action time limit

Actions may declare `timeout` (int seconds, 1–`MAX_CALL_TIMEOUT_S` 300) in the manifest. A gesture-run call uses the
bound action's declared `timeout` instead of the 30 s default. A nested `call_action` uses the caller's explicit timeout
when given, else the callee's declared `timeout`, else 30 s; always capped at 300 and, inside a chain, at the time the
outer call has left (so a step cannot outlive its parent). Manifest validation, `package-and-manifest.md` and
`limits.md` updated. Actions without `timeout` behave exactly as today.

### Core: Sequence

`com.decker.core.sequence`, global, icon `fas:list-ol`. Settings: ten numbered pairs `step_1`…`step_10`, each an
`action` field plus an `action_params` field bound to it (existing types, no new schema type), then `stop_on_error`
(`bool`, default true). Manifest `timeout` 300. Runs the non-empty steps in order with `call_action`, forwarding the
chain (so the device travels, see below) and passing each step the time the Sequence has left. Empty steps are
skipped. A failing step stops the run when `stop_on_error` is set
(the failure is raised as the Sequence's own `ActionError`, naming the step), otherwise it is logged and the run
continues. Returns nothing. Sequence may contain Sequence; the existing depth limit bounds nesting. The Actions tab
form for ten pairs is long; collapse empty steps after the first empty one in the client form if the schema form allows
it cheaply, otherwise leave it (builder's call).

The `action` settings type lists only actions the schema's owner may call: global actions of any plugin plus the
owner's own plugin-scoped ones (today it lists every action). For Sequence that hides other plugins' plugin-scoped
actions, which Core would be refused at run time.

### Device on nested calls

A nested `run_action` payload carries `device` (the device id the server resolved for the chain, or none). The SDK
exposes it on `ActionCallContext` (e.g. `ctx.device_id`) and uses it as the default device for the deck brightness
calls and device-scoped navigation below. The server keeps resolving it from the chain as today and puts it in the
payload, so it survives without a live card.

### Device-scoped navigation

New plugin → server messages `switch_page {device_id, to, wrap}`, `go_to_page {device_id, page_id}`,
`switch_profile {device_id, to, wrap}`, `go_to_profile {device_id, profile_id}` with no card context, routed through
`PageController`. A plugin may send them for a device only while it has an in-flight `run_action` whose chain carries
that device (otherwise refused like today's foreign navigation). This gives navigation inside a Sequence without
handing other plugins the key itself. SDK: the same methods on `ActionCallContext` (use `ctx.device_id`); existing
card-context navigation is unchanged.

### Deck brightness for plugins (protocol 5)

- `PROTOCOL_VERSION` 5. Protocol doc, SDK quickstart and limits doc updated.
- Plugin → server: `get_deck_brightness {device_id}` → `deck_brightness {device_id, percent}`;
  `set_deck_brightness {device_id, percent}`. The SDK fills `device_id` from the card context or from the action
  call's `ctx.device_id` when the caller omits it. No device → error reply.
- Server sets through `ScreensaverController`/`ScreensaverEngine` so the stored normal brightness changes, the deck is
  written only when awake (asleep: the new value applies on wake), and `screensaver_settings_changed` is announced so
  open Settings dialogs update. `percent` is clamped to 1–100, matching Settings.
- Server → plugin: `deck_brightness_changed {device_id, percent}` broadcast to all running plugins whenever the stored
  normal brightness changes from any source (Settings, a plugin, the CLI). (The server cannot tell a plugin's protocol
  version; older SDKs ignore unknown events.)
- Device hotplug: the server also sends `device_connected` when a deck connects while plugins run, and a new
  `device_disconnected {device_id}` when one goes away, so plugins can keep a live device list.
- SDK: `get_deck_brightness(device_id=None) -> int`, `set_deck_brightness(percent, device_id=None)` on the plugin
  context and on `CardContext`/`CardProxy` (card versions use the key's device), and an `@deck_brightness_changed`
  hook. Exact SDK placement follows existing patterns (builder's call).
- No loopback gate: plugins already run with the user's permissions.

### Counter negative-input crash

Reproduce first (Counter card → Actions tab → Double press "Count down" step field → type `-`; also try the card's
`max` field). Suspected in the client schema form int handling (`client/widgets/schema_form.py`) or server-side
arg coercion (`int(ctx.args["step"])`). Fix at the cause, with a test that reproduces it. Rule from the spec: a value
that cannot be parsed falls back to the field's default rather than crashing. If the cause is in the plugin, the fix
goes into the Utilities Counter (the old Counter plugin is left alone).

## Part 2 — `com.decker.utilities`

Folder `decker-plugins/plugins/com.decker.utilities/`, version 1.0.0, `"decker": ">=0.7.2"`, category Utilities,
Python runtime with `pillow`, `screen-brightness-control` (Linux/Windows markers) and `jeepney` (Linux). It declares
variables, so it starts with the server and stays running: timers and countdowns keep going across page switches and
are kept per key instance (context id) in memory; a plugin or server restart resets them.

### Global actions

| Id (`com.decker.utilities.…`) | Settings | Behaviour |
|---|---|---|
| `sleep` | none | Suspends the PC now. Linux: logind `Suspend(false)` over D-Bus. Windows: `SetSuspendState(False, False, False)` via ctypes. macOS: `pmset sleepnow`. |
| `lock` | none | Locks the screen now (icon `fas:lock`). Linux: logind `Session.Lock` on the caller's session over D-Bus (Plasma, GNOME and other logind-aware lockers react). Windows: `LockWorkStation` via ctypes. macOS: `osascript` sending Ctrl+Cmd+Q (needs Accessibility permission; failure is a clear error). No card: users drag the action onto a key (Blank card). |
| `brightness` | `mode` choice increase/decrease/set (default increase); `value` int 0–100 (default 10); `monitor` choice with `options_source` `monitors` ("All monitors" or one monitor) | Changes display brightness; increase/decrease clamp at 0/100; "all" applies the same operation to each monitor. Returns the new level (int, the first monitor's for "all"). |
| `deck_brightness` | `mode` as above; `value` int 0–100 (default 10) | Changes the calling deck's brightness via the SDK (device from the card or `ctx.device_id`, so it works inside a Sequence); server clamps to 1–100. No device → clear error. Returns the new level. |

`monitors` options list detected displays by name (and index when names repeat); the stored value is a stable id
(serial/EDID-derived when available, else index). An unlisted stored monitor shows as unavailable and the action
fails with a clear error.

### Brightness backends

`Backend` interface: `list_monitors()`, `get(monitor)`, `set(monitor, percent)`. Linux combines the logind backlight
backend (internal panels under `/sys/class/backlight`, set via `org.freedesktop.login1.Session.SetBrightness`) with
`screen-brightness-control`'s ddcutil method for external monitors; if `ddcutil`/`i2c-dev` is missing, external
monitors are simply not listed and the README explains the setup (`i2c-dev` module, `i2c` group). Windows uses
`screen-brightness-control`. macOS uses an internal-display backend only. Brightness reads are cached and refreshed by
a background poll every 5 s (configurable is not needed); writes update the cache immediately. All hardware calls run
off the message loop.

### Cards

Defaults below are manifest bindings; users can rebind any gesture as usual. "Level tint" means the icon colour is grey
at the level: 100 % → `#ffffff`, 0 % → `#000000`, linear, no minimum.

| Card | Icon | Press | Double press | Long press | Card settings | Display |
|---|---|---|---|---|---|---|
| Brightness Level | `fas:sun`, level tint | brightness +10 | brightness −10 | brightness set 75 | `monitor` (as above, default all) | icon tinted by the chosen monitor's level ("all": average); title fallback `NN%` |
| Brightness Up | `fas:sun` | +10 | +5 | set 75 | — | static icon |
| Brightness Down | `fas:sun` | −10 | −5 | set 25 | — | static icon |
| Deck Brightness Level | `decker:logo`, level tint | deck +10 | deck −10 | deck set 75 | — | icon tinted by the key's deck level, live via `deck_brightness_changed`; title fallback `NN%` |
| Deck Brightness Up | `decker:logo` | +10 | +5 | set 75 | — | static |
| Deck Brightness Down | `decker:logo` | −10 | −5 | set 25 | — | static |
| Sleep | `fas:moon` | `sleep_press` (plugin action) | `sleep_after` 1 min | `sleep_after` 10 min | — | idle: icon; counting: icon plus remaining `m:ss` |
| Timer | `fas:stopwatch` | `timer_toggle` | `timer_reset` | `timer_reset` | see below | see below |
| Stopwatch | `fas:stopwatch` | `stopwatch_toggle` | `stopwatch_reset` | `stopwatch_reset` | — | elapsed `m:ss`, `h:mm:ss` past an hour |
| Counter | as today's Counter | increment 1 | decrement 1 | reset | `max` | as today's Counter |
| Page | `fas:file` | `page` action, mode next | (none) | (none) | `color` | arrow or target page name by press mode |
| Profile | `fas:layer-group` | `profile` action, mode next | (none) | (none) | `color` | target profile's icon when press mode is "profile", arrow otherwise |

Brightness Up/Down cards bind to the global `brightness`/`deck_brightness` actions with `monitor` "all".

**Sleep card.** Plugin-scoped actions:
- `sleep_press`: if this key has a countdown running, cancel it (key shows a brief OK); otherwise sleep now.
- `sleep_after {minutes: int 1–1440}`: start (or restart) this key's countdown; at zero, sleep the PC and clear the
  countdown.
A countdown survives page switches and is lost on plugin/server restart (never fires late after a restart).

**Timer card.** Settings: `minutes` int 0–1440 (default 5) and `seconds` int 0–59 (default 0) together form the
duration (0:00 is invalid, validation error); `show_time` bool (default true); `show_progress` bool (default true);
`progress_type` choice circle/bar (default circle); `color_mode` choice solid/bicolor/gradient (default solid);
`color` color (default `#2ecc71`); `color_2` color (default `#e74c3c`, used by bicolor and gradient); `line_width` int
1–20 px (default 6). Bicolor: `color` until the last 20 % of the duration, then `color_2`. Gradient: interpolates
`color` → `color_2` over elapsed time. Circle: an arc around the key that shrinks as time runs out; bar: a bar along
the bottom edge. Behaviour: starts idle at the full duration; press starts, pauses, resumes; at zero it shows `0:00`,
flashes (show_alert) and stays finished; press when finished restarts; double/long press reset to idle. Changing the
duration while running resets it.

**Page and Profile cards** ("one card to rule them all"). Each gesture binds to the global action and its own args, so
press/double/long can each target something different. Plugin keeps global actions:
- `page {mode: page|next|previous, profile: profile, page: page (profile_field: profile), wrap: bool}`; `page`/`profile`
  are used only in mode `page`, `wrap` only in next/previous.
- `profile {mode: profile|next|previous, profile: profile, wrap: bool}`.

Both navigate through the card when the call carries one, else through device-scoped navigation (`ctx.device_id`), so
they work inside a Sequence.

**Counter.** Same card, actions and behaviour as `com.decker.counter` 2.0.0 under the new id, including its icon pack,
plus the crash fix if it lives in the plugin.

### Variables

| Variable | Value |
|---|---|
| `brightness_level` | first monitor's brightness, `0`–`100` (empty if no monitor) |
| `brightness_level_{monitor}` | per monitor, `{monitor}` = 1-based index (`brightness_level_1`, …) |
| `deck_brightness_level` | brightness of the first connected deck |
| `deck_brightness_level_{device}` | per deck, `{device}` = the deck's 8-char device id |
| `sleep_in` | remaining time of the soonest running sleep countdown, `m:ss`; empty when none |

Values update from the 5 s poll, after every write, on `deck_brightness_changed`, and on `device_connected`/
`device_disconnected` (a disconnected deck's per-device variable is unregistered; "first" follows the live list).

### Platforms

| | Linux tiers 1–3 | Windows | macOS |
|---|---|---|---|
| Sleep | ✅ logind | ✅ | ✅ |
| Lock screen | ✅ logind (⚠️ on tier 3 if no logind-aware locker) | ✅ | ⚠️ needs Accessibility permission |
| Internal display brightness | ✅ logind | ✅ WMI | ⚠️ internal only |
| External (DDC/CI) | ⚠️ needs ddcutil + i2c-dev | ✅ | ❌ |
| Deck brightness, timers, counter, pages, profiles | ✅ | ✅ | ✅ |

Manifest `platforms` lists all three; README platform tables in both repos updated.

## Release order

1. Decker 0.7.2 (Part 1), merged and tagged.
2. Bump the decker-plugins pinned Decker commit; add `com.decker.utilities` 1.0.0.

## Testing

- Decker: per-action `timeout` (gesture uses it; nested capped by parent's remaining time); device in nested payloads;
  device-scoped navigation allowed with a live chain on that device and refused otherwise; hotplug events; `action`
  field filtering; `status` version;
  unit tests for Delay and Sequence (ordering, skip empty, stop/continue on error, nesting depth, a page switch step,
  a >30 s Sequence from a gesture), protocol 5
  deck brightness (set → stored value, screensaver interaction asleep/awake, broadcast on change from Settings and from
  a plugin, missing device error), the Counter crash regression, About dialog smoke test.
- Plugin: backend layer behind an interface with a fake backend for tests; tests for each action's clamping, monitor
  selection, Sleep press-cancels-countdown, Timer state machine and colour modes (pure functions), Stopwatch, Page and
  Profile action modes, variables. Platform-specific code paths tested with the D-Bus/ctypes/subprocess calls injected.

## Left to the builder

- SDK placement and naming details for deck brightness, consistent with existing SDK style.
- Exact drawing (fonts, arc geometry, layout of time + progress) within the settings above.
- Whether the Sequence form collapses unused steps.
- Monitor id derivation details per backend.

## Calls made in this doc (not explicitly decided by the user)

User decisions after review: actions may declare their own time limit (Sequence 300 s); navigation inside a Sequence
works through device-scoped navigation, not by passing the key down the chain.

- Deck brightness from plugins is clamped to 1–100 like Settings (0 % stays a valid *icon tint* only).
- Delay max 60 s (manifest `timeout` 65); Sequence has `stop_on_error` (default on).
- Nested calls carry the device; protocol 5 adds hotplug `device_connected`/`device_disconnected`; `status` reports
  the version; `deck_brightness_changed` goes to every plugin; `action` fields list only actions the owner may call.
- Sleep: press during a countdown cancels; double/long press during a countdown restart it with their duration.
- Timer finished state, bicolor threshold (last 20 %), start-on-first-press, duration as minutes + seconds.
- Brightness Level card shows the average for "all"; variables use the first monitor/deck plus per-item patterns.
- Brightness polling every 5 s; timers/countdowns are lost on plugin restart.
- About dialog in both the ⚙ menu and the tray menu.
