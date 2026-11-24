# Limits, lifecycle and the launch environment

## Package and data limits

| Limit | Value |
|-------|-------|
| Package file size | 64 MiB |
| Uncompressed size | 256 MiB (checked from the headers and again while streaming) |
| Entries in the archive | 10 000 |
| `manifest.json` size | 1 MiB |
| Protocol line size | 8 MiB; longer lines are discarded |
| stderr | read in 8 KiB chunks and logged; never buffered unbounded |
| Variable declarations (`contributes.variables`) | 256 per plugin |
| Registered concrete names (`register_variable`) | 256 per plugin; further `register_variable` calls are refused |
| Variable name | 64 characters |
| Variable value | 1024 characters; a longer value is refused and the old one stays |
| Variable ask (`get_variable`) | the plugin has 5 s to answer; on a timeout the old value stays |
| Dynamic choice options (`options_source`) | at most 256 options per source; values and labels are cut to 256 characters; a stored value is at most 256 characters |
| Options source name | 64 characters |
| Options ask (`get_options`) | the plugin has 5 s to answer; on a timeout the client gets an error and no options |
| Action or call result value (`action_result`, `call_result`) | 1 MiB encoded as JSON (`MAX_VALUE_BYTES = 1024 * 1024`); a larger value is an error |
| Action call timeout | the action's manifest `timeout`, else 30 s; `call_action` may ask for another, capped at 300 s and at the calling action's remaining time |
| Action call depth | a chain of calls may nest 8 deep; deeper calls are refused (the server counts it from the `chain` the caller forwards) |
| Canvas frame tick | about 30 per second; at most 2 frames per tick (about 60 per second), so the canvas stays under the flood limit; a key not sent stays dirty for the next tick |
| Canvas image fetch (`get_image`) | the plugin waits up to 5 s for `image_result` |
| HTTP Request action | body up to 512 KiB read and decoded, then trimmed so the encoded result fits 1 MiB; the request timeout setting allows 1 to 120 s but Core clamps it to 25 s, because a gesture call waits 30 s |
| Delay and Sequence actions | Delay waits 0 to 60 s (manifest `timeout` 65); Sequence runs up to 10 steps within 300 s of starting (manifest `timeout` 300), each step called with the time left |
| Raster image from `set_image` | at most 4096 x 4096 pixels |
| Icon file (pack icon or card or action `icon`) | at most 4 MiB; larger files are ignored |
| Animated GIF (`set_image` bytes or pack icon) | at most 4 MiB, 300 frames, 4096 x 4096 pixels per frame, and 96 million pixels in total (frames x width x height, so 300 frames of 480 x 480 fit but 100 frames of 1024 x 1024 do not; both are enforced from the frame headers and again as frames are read, counting a frame that is bigger than the header's screen at its grown size); frame delays under 20 ms play as 100 ms; a GIF over any limit stays a still first frame |

## Process lifecycle

- A plugin process starts when its first key appears and stops about 10 s after its last key disappears (so
  flipping pages does not respawn it). On stop it gets `shutdown`, then SIGTERM after 3 s, then SIGKILL.
- A plugin that declares [variables](package-and-manifest.md#variables) starts with the server and stays running with no
  key on screen; it still gets crash restarts, back-off and the watchdog. Its values are dropped when the process stops
  and return after a restart. Plugins without variables keep the lazy start above.
- `get_options` (dynamic choice fields) starts a lazy plugin for the request and releases it afterwards, like an action
  run. The `plugin_options` RPC is read-only and not restricted to loopback clients, so a remote client may call it; it
  can start a lazy plugin and holds an RPC thread for up to 5 s. Sustained high rates can also hit the plugin's message
  rate limit (see Flood below): a plugin that stays above about 100 messages per second for 5 s is killed, and three
  kills within 60 s mark it failed.
- The canvas needs Pillow in the plugin's environment: add `pillow` to `runtime.requirements` (built-in plugins use the
  server's Pillow). Without it, using the canvas raises a `RuntimeError`; the SDK itself needs no packages.
- A plugin with actions starts when one of its actions runs (a gesture or a `call_action`) and, like one with cards, stops about
  10 s after its last use. Built-in plugins with a `runtime` (Core) run on the server's own interpreter and SDK, with no uv
  environment.
- Each plugin runs in its own process group; stopping or killing it also stops helper processes it started.
- **Crashes**: restart with backoff (0.5 s, 2 s, 5 s). Three crashes within 60 s mark the plugin failed: its keys
  are cleared and labelled "Plugin failed", and clients are notified. Re-saving a key's settings retries it; so
  does a server restart. A plugin with only variables that is marked failed has no key whose re-save would retry
  it, so it recovers on reinstall or a server restart.
- **Hangs**: the server pings every 5 s; no `pong` within 10 s kills the process. A process that sends no `ready`
  within 10 s is killed too.
- **Memory**: the server samples each plugin's resident set size and kills it above the limit, default 256 MiB
  (config key `plugin.memory_limit`, bytes).
- **Flood**: more than 100 messages per second sustained (burst 200) are dropped; staying over budget for 5 s
  continuously kills the plugin (counted as a crash).
- Installing an id that is already installed replaces it; live keys are stopped and re-appear with the new version.
  A failed upgrade restores the previous version.

## Launch environment

The plugin is started with its folder as the working directory and only these variables:

| Variable | Value |
|----------|-------|
| `PATH` | the server's `PATH` |
| `HOME` | the server's `HOME` |
| `LANG` | the server's `LANG` (default `C.UTF-8`) |
| `PYTHONUNBUFFERED` | `1` |
| `PYTHONPATH` | the directory holding the staged SDK (installed Python plugins only) |

Nothing else from the server's environment is passed on. In particular `DISPLAY`, `WAYLAND_DISPLAY`,
`XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` are absent, so desktop-session integrations will not work out of the
box (see the known limitations in the [overview](README.md)).

## Trust model

Plugins and their dependency builds run as your user; there is no sandbox in v1. The only user-facing safeguard on
plugin actions is the confirmation shown before an `open_url` request opens a link.
