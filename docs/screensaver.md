# Screensaver and brightness

Each device can dim itself and show a picture (or go dark) when idle, and come back untouched on a key press. The
server does all of it, so it works with no client running. Design:
[screensaver spec](superpowers/specs/2026-11-19-screensaver-design.md).

## Settings

Settings are per device and stored by the server. The server validates every save and rejects out-of-range or unknown
values; nothing is clamped silently.

| Setting | Values | Default | Meaning |
|---|---|---|---|
| `brightness` | 1-100 | 100 | Normal brightness, applied when a device opens and when settings change |
| `timeout_minutes` | 0-1440 | 0 | Idle minutes before the screensaver starts; 0 disables the idle trigger |
| `saver_brightness` | 0-100 | 10 | Brightness while the screensaver shows; 0 turns the backlight off |
| `content` | `none`, `image`, `gallery` | `none` | Dark screen, one image, or a folder of images |
| `image_path` | text, at most 1024 chars | empty | Image file on the server's machine |
| `folder_path` | text, at most 1024 chars | empty | Gallery folder on the server's machine |
| `scaling` | `center`, `stretch` | `center` | How the picture fills the deck |
| `bezel` | true, false | false | Cut the gaps between keys out of the picture |
| `key_mm` | 5-60 | 16.0 | Physical key size, used for the bezel geometry |
| `gap_mm` | 0-30 | 4.0 | Physical gap between keys, used for the bezel geometry |
| `gallery_mode` | `activation`, `interval` | `activation` | Advance the gallery on each activation, or every N minutes |
| `gallery_minutes` | 1-1440 | 5 | Interval for `gallery_mode` `interval` |

## Triggers and waking

- **Idle timeout:** a device starts its screensaver once `timeout_minutes` have passed without activity. 0 disables
  it. Saving settings restarts the countdown, and saving never activates the screensaver.
- **RPC:** `screensaver_activate` and `screensaver_deactivate` work at once, even when the timeout is 0.
- **Activity** means key presses only. Focus changes, page or profile switches and plugin writes do not count.
- **Waking:** while the screensaver shows, the first key press wakes the deck and is swallowed, and so is that key's
  release. Neither reaches the page, a plugin or a client, so waking never fires an action. Waking restores normal
  brightness and redraws the active page from stored state.
- A key held down when the screensaver starts still gets its release delivered (a release never wakes the deck), so
  the page and plugins never see a press without its release.
- Profile or page switches (manual, plugin or app rule) while the screensaver shows only change state; waking shows
  whatever is then active.

## Content

- `none`: a dark screen at the screensaver brightness.
- `image`: one file. Formats: `png`, `jpg`, `jpeg`, `svg`; a PNG or JPEG must really be that format (a file whose
  content does not match its extension counts as undecodable). SVG is rasterised at its own aspect ratio, just large
  enough to cover the canvas, and is then scaled like any other picture. SVG needs the Cairo library. Only the first
  frame of an animated file is used (no animated screensavers).
- `gallery`: the image files of `folder_path`, sorted by file name. The folder is rescanned at every advance, so added
  and removed files take effect; the gallery wraps around, and unreadable files are skipped. With `gallery_mode`
  `activation` the next image shows each time the screensaver starts; with `interval` it advances every
  `gallery_minutes` while active.
- Each picture is rendered once per activation and per gallery advance, not per frame. A large picture is shrunk
  while it is decoded, to about twice the size the canvas needs, before anything else touches it.

## Scaling and bezel geometry

The picture is laid across one canvas covering all the keys:

- **center** keeps the aspect ratio, scales the picture to cover the whole canvas and crops the excess (no letterboxing).
- **stretch** distorts the picture to fill the canvas.

With `bezel` off the keys touch and the canvas is `cols * key_px` by `rows * key_px`. With `bezel` on the physical gap is
reproduced in pixels, so the keys look like windows onto the picture and the strips under the plastic gaps are cut out:

```
gap_px = round(key_px * gap_mm / key_mm)
canvas width  = cols * key_px + (cols - 1) * gap_px
canvas height = rows * key_px + (rows - 1) * gap_px
key (r, c) crops at (c * (key_px + gap_px), r * (key_px + gap_px))
```

Worked example: 72 px keys, 16 mm keys and 4 mm gaps give `gap_px = round(72 * 4 / 16) = 18`. A 5-column deck has a
canvas width of `5 * 72 + 4 * 18 = 432`. Key (row 1, column 2) crops its 72 x 72 box from origin
`(2 * (72 + 18), 1 * (72 + 18)) = (180, 90)`.

The 16 mm and 4 mm defaults apply to every model; edit them per device if your deck differs.

## Limits

- Paths: 1024 characters.
- Image file: 20 MiB.
- Decoded size: 24 megapixels.

A file over a limit counts as unreadable (see failure behaviour).

## Who may set paths

`image_path` and `folder_path` live on the server's machine. Only clients on the server's machine (loopback) can change
them with `screensaver_set`. A remote client gets an error; it can still change every other setting. The same rule
applies to `screensaver_preview`: a remote client can preview with the stored paths, and is refused only when its draft
names a different `image_path` or `folder_path`. Images are never uploaded.

## Brightness

Normal brightness (1-100) is applied when a device opens or reopens and whenever settings change while awake.
Plugins can read and change the normal brightness (clamped to 1-100, stored as `brightness`) with the SDK's
`get_deck_brightness`/`set_deck_brightness`, see [the SDK quickstart](plugins/sdk-quickstart.md#deck-brightness).
The screensaver brightness (0-100) is applied only while the screensaver shows; 0 turns the backlight off.

## Failure behaviour

A missing, unreadable, oversized or undecodable image, a missing folder, or a gallery with no readable image does not
break the deck. The screen goes dark at the screensaver brightness, and the reason is reported as `last_error` by
`screensaver_get`. The Device tab shows it below the settings; it refetches `last_error` whenever the
screensaver starts or stops.

## Hot-plug and shutdown

- **Reopen while active** (hot-plug): the deck comes back awake with normal brightness, and the reopen counts as
  activity.
- **Normal server stop:** every active device is woken (normal brightness, active page).
- **Crash:** the deck keeps showing the last thing written, at screensaver brightness, until a server starts again.
- The active state is never persisted: a server always starts with every device awake.

## How the deck stores it

The hardware keeps only the last images and brightness written to it, plus a built-in standby logo. The screensaver is
drawn by the server and does not travel with the deck to another machine.

## The Device tab

Settings has a **Device** tab. A drop-down at the top picks the device being edited (the client's active device by
default), with its type, serial number, hardware ids and firmware below it, then the device's brightness. Under a
**Screensaver** separator come the start delay, the saver brightness and a **None | Image | Gallery** selector; the rows
below it (image or folder pickers, scaling, bezel with key and gap sizes, gallery mode and interval) show only what the
selection needs, followed by a preview. There is no Activate or Wake button.

- Changes on this tab apply live, a moment after you edit; the dialog's Apply button does not affect them.
- The pickers are disabled with a hint when the client is not on the server's machine.
- The preview shows the composed canvas with the gaps dark. It is rendered by the server on request.

## RPC and events

| RPC | Purpose |
|---|---|
| `screensaver_get(device_id)` | The settings plus `active` and `last_error`, or none for an unknown device |
| `screensaver_set(device_id, settings_json)` | Validate, store and apply; returns `{"ok", "error"}`; never activates |
| `screensaver_activate(device_id)` | Show the screensaver now; `true` or `false` |
| `screensaver_deactivate(device_id)` | Wake the device; `true` or `false` |
| `screensaver_preview(device_id, settings_json)` | `{"png": base64}` of the composed canvas, or `{"error"}` |

`screensaver_activate` returns `false` for an unknown or unopened device, or when a wake cancelled the activation.
`screensaver_deactivate` returns `false` for an unknown device or one that was not showing the screensaver. Like other
bool results, the RPC layer replies them as the JSON text `"true"` or `"false"`.

Events: `screensaver_changed` with `{"device_id", "active"}` when a screensaver starts or stops (no error text; read
`last_error` with `screensaver_get`), and `screensaver_settings_changed` with `{"device_id"}` after a save.

## Known limitations

- Settings saved while the screensaver shows (screensaver brightness, content, image, scaling, bezel) apply at the
  next activation; only an interval gallery's timer is restarted at once.
- Decks without key screens (such as the Stream Deck Pedal) have no screensaver: activating returns `false` and the
  idle timeout never starts one. Their brightness and settings can still be read and saved.
- The client renders the preview request on the GUI thread, so the dialog can stall briefly on a slow server.
