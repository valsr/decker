# Decker Server

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE.md)
[![Made with Python](https://img.shields.io/badge/Made%20with-Python-ff7b3f.svg)](https://www.python.org/)

> :warning: Decker Server is currently in early alpha release and doesn't work! Dont use at this point.

**Decker** is an linux application designed for Elgato Stream Deck, inspired by
[StreamController](https://github.com/StreamController/StreamController).

**Decker Server** is the server component/application that handles interacting with the hardware.

## Supported Devices

Decker supports the following Elgato Stream Deck models:

- Stream Deck XL

## Features

| Feature | Status |
|---------|--------|
| Server with monitor (heartbeat, memory limit, automatic restart), multiple clients | Supported |
| Device discovery, stable device ids | Supported |
| Profiles and pages per device, per-key cards and long press | Supported |
| Key appearance: title, fonts, colours, icon override, live preview | Supported |
| Image library (icon packs from plugins, User pack, System Icons on Linux) | Supported |
| Animated GIF key faces | Supported |
| Key background images (none, solid colour or image from the image library; see [docs/key-backgrounds.md](docs/key-backgrounds.md)) | Supported |
| Switch card (one key with Display, Press, Double press and Long press slots that hold cards or actions) | Supported |
| Plugins (out of process, `.deckerplugin` packages, repositories, updates) | Supported |
| Plugin variables (plugins export values that key text uses as `{name}`; plugins that declare them run in the background) | Supported |
| Dynamic choice fields (a plugin fills a settings dropdown with runtime options such as audio devices; saved values survive the list changing) | Supported |
| PyQt6 desktop client | Supported |
| Command line client (`poe cli`) | Supported |
| Server settings from the client (port, memory and image limits, stop on last client), gated by `--allow-client-reconfiguration`; see [docs/server-reconfiguration.md](docs/server-reconfiguration.md) | Supported |
| Daemon mode, system tray on every desktop | Not supported |

Early alpha: the table shows what exists, not that it is stable. Plugins are listed in the
[plugins repository](https://github.com/valsr/decker-plugins).

## Platform support

✅ works (verified by the maintainer or by CI) · ⚠️ partial or not verified · ❌ not supported

| Feature | Linux (Arch/Plasma/Wayland) | Linux (Ubuntu/Debian/Fedora/GNOME) | Linux (other) | Windows | macOS |
|---------|------|------|------|------|------|
| Server and monitor | ✅ | ✅ [13] | ⚠️ | ✅ [1] | ✅ [1] |
| Hardware access (HID) | ✅ | ⚠️ [2] | ⚠️ [2] | ⚠️ [3] | ⚠️ [3] |
| GUI client | ✅ | ✅ [13] | ⚠️ | ✅ [4] | ✅ [4] |
| System tray | ✅ | ⚠️ [5] | ⚠️ | ⚠️ | ⚠️ |
| Plugin runtime (process isolation) | ✅ | ✅ [13] | ⚠️ | ✅ [6] | ✅ [6] |
| Plugin install and repositories | ✅ | ✅ [13] | ⚠️ | ✅ [7] | ✅ [7] |
| Key rendering and fonts | ✅ | ✅ [13] | ⚠️ | ✅ [8] | ✅ [8] |
| System Icons | ✅ | ✅ [13] | ⚠️ | ❌ [9] | ❌ [9] |
| User icon import | ✅ | ✅ [13] | ⚠️ | ✅ [10] | ✅ [10] |
| GIF animations | ✅ | ✅ [13] | ⚠️ | ✅ | ✅ |
| Key background images | ✅ | ✅ [13] | ⚠️ | ✅ | ✅ |
| CLI | ✅ | ✅ [13] | ⚠️ | ✅ [11] | ✅ [11] |
| Server settings from the client | ✅ [14] | ✅ [14] | ⚠️ | ✅ [14] | ✅ [14] |
| Switch profile by focused app | ✅ [15] | ⚠️ [15] | ⚠️ [15] | ⚠️ [15] | ❌ [15] |
| Screensaver and brightness | ✅ [16] | ✅ [13] [16] | ⚠️ | ✅ [16] | ✅ [16] |
| Daemon mode | ❌ [12] | ❌ [12] | ❌ [12] | ❌ [12] | ❌ [12] |

The Linux (Arch/Plasma/Wayland) column is ✅ where the maintainer uses the feature or CI passes, and CI also runs the suite on
Arch Linux (advisory) and, in the next column, on Ubuntu and Fedora. Hardware access and the system tray stay
⚠️ there because the runners have no Stream Deck or desktop. Linux (other) stays ⚠️ because
nobody has tried it.

Footnotes:

1. CI ran the RPC service layer (with real stores on sqlite), the monitor logic and `decker.common.processes` (process
   groups, tree kill, memory read via psutil, so no `/proc` is needed) on all three OSes. On Windows the psutil tree
   walk (the fallback) does not clean up orphaned helpers once the leader has exited (the pid may be recycled). The real
   server child was also started, stopped gracefully (exit code 0) and restarted by the monitor on Linux, Windows and
   macOS (integration tests in `tests/server/test_server_process.py`, run 44460424393; they skip when a Stream Deck is
   attached). Not covered: the graceful shutdown of plugin processes during such a stop end to end (no plugin is
   installed in those tests; the supervisor tests only cover stopping plugin processes at unit level), hardware access,
   a full `poe server` run, memory-limit restarts of a long-lived server, and `poe cli` against a live server.
2. The README documents `libhidapi` setup for Debian/Ubuntu, Fedora and Arch and a conventional udev rule; the udev
   steps are not verified on every distribution, and other distributions need their own package name.
3. The README documents hidapi and Cairo installation for Windows (MSYS2) and macOS (Homebrew) as the CI runners
   needed them, but device permissions and drivers there are unverified. CI never tested hardware access on any OS
   (the runners have no Stream Deck).
4. PyQt6 widget and view tests run offscreen on all three OSes (`QT_QPA_PLATFORM=offscreen`). This is not a human check:
   real window manager behaviour, HiDPI and drag and drop are not covered.
5. GNOME has no system tray by default, and the behaviour there is untested. The tray is not covered by CI on any OS.
6. Plugin children start in their own process group (`CREATE_NEW_PROCESS_GROUP` on Windows, a new session elsewhere).
   They are stopped by a recursive psutil kill on Windows and by `os.killpg` (SIGTERM/SIGKILL) on the process group
   elsewhere; on Windows terminate then kill are both hard kills (`TerminateProcess`). On Windows each plugin process
   (runtime and the `uv`/venv build subprocesses) is also adopted into its own Job Object with `KILL_ON_JOB_CLOSE` right
   after it is spawned (`decker.common._winjob`, private). Killing the tree terminates the job, which reaches helpers of
   an already-exited leader, and the supervisor kills the tree once the leader exits, so helpers die with it. Terminate
   stays a psutil walk (the fallback). The server child itself is not in a job, but its plugins' job handles close when
   the server dies (hard kill included), so the OS kills their trees. If a job cannot be created or assigned this is
   logged at debug level and the psutil walk is the only mechanism. A helper spawned in the short window between spawn
   and assignment can escape the job. The real-job behaviour is covered by `windows_only` tests that ran and passed on
   the Windows runner (run 44551225429, none skipped). The monitor stops the server on every OS by setting a stop event,
   which makes the server run its normal shutdown (including stopping plugins); only if the server is still alive after
   `server.stop_timeout` does it fall back to `terminate()` (SIGTERM on POSIX, `TerminateProcess` on Windows) and then
   `kill()`. Plugins stopped through that fallback on Windows rely on stdin EOF instead of a graceful stop. The launcher
   uses the plugin venv's interpreter (`Scripts\python.exe` on Windows, `bin/python` elsewhere), and on Windows it
   passes only a minimal environment plus the Windows system variables (`SYSTEMROOT`, `TEMP`, ...); a test starts a real
   interpreter with that environment on every OS. CI ran the real `uv venv` build tests
   (`test_build_creates_venv_with_server_python_when_no_spec` and `test_build_installs_requirements`) on Windows and
   macOS as well as Linux (run 44454336780: all passed, none skipped), plus real plugin child processes, the watchdog
   and orphan checks. `test_venv_is_built_relocatable` uses a fake `uv` and only checks the arguments. Builds that fetch
   from a network index are not covered (the requirements test uses a local wheel).
7. CI ran package read/extract (including zip path safety per OS), install, uninstall and repository RPCs against local
   fixtures. Live repository fetches and update checks over the network are not covered.
8. Fonts resolve in this order: `fc-match` when present, an index of the OS font folders, then the bundled DejaVu
   Sans. The full DejaVu family (Sans, Sans Condensed, Sans Mono, Serif, Serif Condensed; regular, bold, italic and
   bold italic) is bundled, and the server resolves those families on every OS even when the OS lacks them. Plugins
   locate the bundled files with `decker_plugin_sdk.fonts.font_path` (the server sets `DECKER_FONT_DIR` for plugin
   processes). For the bundled DejaVu families the server uses an installed copy found in the OS font folders (exact
   style), else the bundled file, and does not consult fontconfig for them, so a user's fontconfig alias or
   substitution for a DejaVu family is ignored. CI run 44563672808 (head ca4e277) ran the bundled-family resolution
   (`tests/server/test_fonts.py`, with only the POSIX-only chmod and symlink cases skipped on Windows), the SDK helper
   (`tests/sdk/test_fonts.py`), the client font registration (`tests/client/test_client_fonts.py`), and the lookup and
   `compose_key_image` on all three OSes. FreeType rasterisation differs per OS, so only Linux asserts a byte-exact
   image hash; Windows and macOS check structure (size, colours, title region). User-installed fonts beyond the
   runners' defaults are not covered.
9. System Icons read the server's freedesktop icon theme, so the feature is Linux-only: on Windows and macOS the library
   is hidden (CI checks that no packs are served there).
10. Icon import, SVG rasterisation through Cairo (installed via MSYS2 on Windows and Homebrew on macOS) and the
    thumbnail cache are tested on all three OSes. A real file dialog is not covered.
11. CLI command tests run against the in-process service; `poe cli` against a live server on Windows and macOS is not
    covered.
12. Not implemented: the server has no `--daemon` option. Supervision (heartbeat, memory limit, restart) is done by the
    server monitor that `poe server` always starts; DAEMON.md describes it.
13. CI runs the full test suite in a `fedora:latest` container (blocking) and an `archlinux:latest` container
    (advisory: `continue-on-error`, because a rolling release can break without a change here) next to the Ubuntu
    runner, offscreen Qt, no Stream Deck, no desktop session (run
    [44573260516](https://github.com/valsr/decker/actions/runs/44573260516), all five test legs green). The cell covers
    Ubuntu and Fedora; a green Arch leg backs the Arch column but does not change its ✅. The GNOME desktop itself
    is not exercised.
14. CI ([run 44584123173](https://github.com/valsr/decker/actions/runs/44584123173), all five test legs green) covers the
    settings validation and persistence, the `server_settings` RPCs and the permission policy, the monitor's reload
    and rollback, the stop-when-last-client logic, the client connection calls and the Server tab (offscreen Qt). The
    real server child tests (restart exit code 75, rollback when the new port is in use, idle stop with exit code 0)
    ran on the Linux, Windows and macOS runners and skip when a Stream Deck is attached. Not covered: a real GUI
    changing the port of a live server end to end, remote clients, and other clients following a port change (they
    keep the old port).
15. Switches a device's profile by the focused window; see [docs/app-profile-switching.md](docs/app-profile-switching.md).
    Linux Plasma 6 (kwin backend): the real KWin script and D-Bus transport were exercised by a live smoke test on
    KWin 6.7.5; Plasma 5 reports unavailable. Ubuntu/Debian/Fedora/GNOME works on X11 sessions only (the X11 backend ran
    against fakes only, no X server was available); GNOME Wayland is not supported yet. Linux (other) is the same as
    that column. The Windows backend ran against fakes only (no Windows runner or desktop), so it is unverified on a
    real desktop; it has no window type or desktop file. macOS is not supported yet.
16. Idle or RPC-triggered screensaver and per-device brightness; see [docs/screensaver.md](docs/screensaver.md). It is
    Pillow plus the same HID writes as key faces, with no OS-specific code, so it is marked like the other
    hardware-only features. The tests use a fake device manager and fake decks: nothing was run against a real Stream
    Deck or on a Windows or macOS desktop. Image and folder paths are server-side and settable only from the server's
    machine. SVG screensaver images are rasterised through Cairo, so they need the Cairo library as in footnote 10.

## System Requirements

**Decker Server** depends on the following libraries to be installed:

- libhidapi
- [uv](https://docs.astral.sh/uv/), needed to install plugins that have a Python runtime (it builds each plugin's
  own environment)

For installation/setup per operating system see the subsections below. Decker also needs the Cairo library (for SVG
icons); the Windows and macOS steps below install it.

What is verified: the CI workflow (`.github/workflows/ci.yml`) runs the test suite on Ubuntu, Windows and macOS runners and in Fedora and Arch Linux containers
using the steps below, and they pass. CI has no Stream Deck attached, so nothing here is verified with real hardware
off Linux (Arch/Manjaro). The steps for installing on your own Windows or macOS machine are what the CI runners
needed; the maintainer has not verified them on a user machine.

### Debian/Ubuntu

`sudo apt-get update && sudo apt-get install -y libhidapi-libusb0`

### Fedora

`sudo dnf install hidapi`

### Arch Linux

`sudo pacman -S hidapi`

### Linux device permissions (udev)

Without a udev rule, only root can open the Stream Deck. The conventional approach (not verified on every
distribution) is a rule for the Elgato vendor id `0fd9`, for example in `/etc/udev/rules.d/70-streamdeck.rules`:

```
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", TAG+="uaccess"
KERNEL=="hidraw*", ATTRS{idVendor}=="0fd9", TAG+="uaccess"
```

Then reload the rules and replug the device:

```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
```

`uaccess` gives the user at the active session access. On systems without logind, use a group instead, for example
`MODE="0660", GROUP="plugdev"` in place of `TAG+="uaccess"`, and add yourself with `sudo usermod -aG plugdev $USER`
(log out and in again).

### Windows

CI uses MSYS2 for the native libraries; install [MSYS2](https://www.msys2.org/), then in an MSYS2 MINGW64 shell:

```bash
pacman -S mingw-w64-x86_64-cairo mingw-w64-x86_64-hidapi
```

Put `<msys2 folder>\mingw64\bin` on `PATH` so Python finds `libcairo-2.dll` (CI proves Cairo is needed: the SVG tests run there). CI installs hidapi too but never
loads it, since no test enumerates devices. Install
[uv](https://docs.astral.sh/uv/). Device permissions and drivers on Windows are unverified.

### macOS

```bash
brew install hidapi cairo
```

Homebrew's libraries are outside the default library search path, so if Python cannot find Cairo (`no library called
"cairo-2" was found`), set `DYLD_FALLBACK_LIBRARY_PATH="$(brew --prefix)/lib"` before starting Decker (CI does
this). Install [uv](https://docs.astral.sh/uv/) as well. Device permissions on macOS are unverified.

## Plugins

Cards (what a key shows), actions (things a key can run) and icon packs come from plugins, shipped as `.deckerplugin` packages. Each plugin runs in its own
process, so a misbehaving plugin cannot take the server down. Plugins run with your user's permissions and are not
sandboxed: only install plugins you trust.

```bash
poe cli plugin new com.example.hello          # scaffold a plugin
poe cli plugin pack com.example.hello         # build com.example.hello-0.1.0.deckerplugin
poe cli plugin install com.example.hello-0.1.0.deckerplugin
poe cli plugin list
poe cli plugin remove com.example.hello
```

You can also install from Settings, Plugins in the client, or by dropping a package on the main window. Installing
is only allowed from the server's own machine unless `server.allow_remote_plugin_install` is `true`.

Plugins can also come from repositories: Settings, Plugins, "Get More Plugins…" lets you search, install and update
plugins from the repositories you have added (the default is `https://valsr.github.io/decker-plugins/index.json`).
Decker checks them for updates daily, weekly (default), monthly or only on demand and shows a notification when updates
are available. A repository can provide any plugin and plugins run with your user's permissions, so only add
repositories you trust. See [plugin repositories](docs/plugins/repositories.md), including how to host your own.

Writing a plugin: see the [plugin documentation](docs/plugins/README.md) (package format and manifest, settings
schema, protocol, Python SDK and limits).

## Contributing

Maybe later

## Note

This application is not affiliated with Elgato and/or any of their products and services.
