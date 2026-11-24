# Plugin repositories

A repository is a JSON index that lists plugins and where to download each version. Decker reads the indexes of every
configured repository, merges them into one catalog (Settings, Plugins, "Get More Plugins…") and tells you when an
installed plugin has a newer version. Any static web host can serve a repository; only the index URL matters.

The default repository is `https://valsr.github.io/decker-plugins/index.json` (source:
<https://github.com/valsr/decker-plugins>). You can remove it.

## For users

- **Add / remove**: Settings, Plugins, Repositories section, "Add…" asks for the index URL, shows the trust warning
  below, then fetches and validates the index before saving it. "Remove" deletes the repository and its cache.
- **Get More Plugins…**: the button in the Plugins tab opens a dialog with a search box, a category filter and
  "Refresh". Each row shows the plugin's icon, name, short description and installed and available version, with an
  Install, Update, Installed or "Requires Decker …" button. A details pane shows the full description, author, homepage,
  what the plugin contributes (Cards, Actions, Icon packs, Variables), size, repository and required Decker version.
- **Update check**: the "Update check" selector picks Daily, Weekly (default), Monthly or Manual. "Check for updates"
  refreshes every repository now in the background; the button stays disabled until the server reports that the
  check finished.
- **Toast**: when updates are found, the client shows "N plugin update(s) available" with "Update all" (installs one
  after another, since the server runs one install job at a time). If nothing needs updating but keys use plugins that
  are not installed and a repository offers them, it shows "N missing plugin(s) available" with "Install".
- If two repositories offer the same plugin id, the one earlier in the configured list wins. The other entry is hidden
  from the Get More Plugins list; the winning entry's details pane notes "Also offered by <repository> (not used)".
  The hidden one is never used for installs or updates.
- Install, Update and the toast's "Update all" / "Install" are disabled or hidden when the client is not allowed to
  install plugins (see Trust).
- Keys that use a plugin that is not installed show "Plugin missing", or "Plugin failed" when the plugin is installed
  but failed to load. The label goes away once the plugin is installed and running.

### Trust

> A repository can provide any plugin, and plugins run with your user's permissions. Only add repositories you trust.

Each download is verified against the index's `size` and `sha256`. That detects corruption or a swapped file, not a
malicious index. There is no package signing. The manifest inside the package must also match the index entry's plugin
id and version, otherwise the install fails.

Adding or removing a repository, changing the update schedule, and installing or updating from a repository are only
allowed for loopback clients, exactly like `plugin_install`, unless the server setting
`server.allow_remote_plugin_install` is `true`. Listing, the catalog, icons and "Check for updates" are open to every
client.

### Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `plugin.repositories` | `["https://valsr.github.io/decker-plugins/index.json"]` | Index URLs, highest priority first |
| `plugin.update_check` | `weekly` | `daily`, `weekly`, `monthly` or `manual` |

## Index format (v1)

```json
{
  "format": 1,
  "name": "Decker Plugins",
  "generated": "2026-11-16T19:00:00Z",
  "plugins": [
    {
      "id": "com.decker.counter",
      "name": "Counter",
      "author": "Decker",
      "description": "Counts key presses.",
      "category": "Utilities",
      "homepage": "https://github.com/valsr/decker-plugins",
      "icon": "icons/com.decker.counter.png",
      "versions": [
        {
          "version": "1.0.0",
          "decker": ">=0.7",
          "url": "https://github.com/valsr/decker-plugins/releases/download/com.decker.counter-v1.0.0/com.decker.counter-1.0.0.deckerplugin",
          "size": 12345,
          "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
          "released": "2026-11-16T19:00:00Z",
          "contributes": { "cards": ["Counter"], "icon_packs": ["Counter icons"] }
        }
      ]
    }
  ]
}
```

### Top level

| Field | Rules |
|-------|-------|
| `format` | Must be the integer `1`. Anything else marks the whole repository as in error. |
| `name` | Display name, trimmed to 100 characters; "Repository" if missing |
| `generated` | Free text timestamp, trimmed to 100 characters |
| `plugins` | Required list |

### Plugin entry

| Field | Rules |
|-------|-------|
| `id` | Required; same rules as the manifest `id` |
| `name` | Trimmed to 100 characters; defaults to the id |
| `author`, `category` | Optional, trimmed to 100 characters |
| `description` | Optional, trimmed to 2000 characters |
| `homepage` | Optional, trimmed to 500 characters |
| `icon` | Optional URL of a PNG; an invalid icon URL is ignored (the plugin stays) |
| `versions` | Required non-empty list; Decker sorts it newest first itself |

### Version entry

| Field | Rules |
|-------|-------|
| `version` | Required; same rules as the manifest `version` and a valid PEP 440 version |
| `decker` | Optional specifier such as `>=0.7`; the version is listed but not installable when it excludes the running Decker (prereleases count) |
| `url` | Required download URL of the `.deckerplugin` |
| `size` | Required positive integer, bytes |
| `sha256` | Required, 64 hex characters |
| `released` | Optional, free text timestamp |
| `contributes` | Optional map of kind (`cards`, `actions`, `icon_packs`, `variables`) to a list of names, shown in the details pane |
| `platforms`, `linux` | Optional; same fields and validation as the [manifest](package-and-manifest.md#platforms). Absent means all platforms. An invalid value skips that version entry with a warning |

### Platform filter

`repository_catalog` entries carry `platforms` and `linux` (from the latest compatible version, or the first listed
one; `null` when absent), the same two keys on each item of `versions`, and a server-computed `supported` boolean
for the server's platform (`platform_info`). The plugin browser shows a checkbox "Only show plugins that support this
system (<platform label>)", on by default; the choice is remembered in the client's UI state (`plugins.platform_filter`).
With it off, unsupported plugins show a "Not supported on <platform>" badge, and installing one asks for confirmation
and then proceeds (warn, never block). Installed plugins that do not match show the same badge.

### Validation and URLs

- `icon` and `url` may be relative; they resolve against the index URL. Allowed schemes are `http` and `https`, plus
  `file` only when the index URL itself is a `file:` URL (useful for testing). Other schemes are rejected.
- A bad plugin or version entry is skipped with a warning; the rest of the index is still used. A plugin whose
  versions are all invalid is dropped, and a repeated plugin id keeps only its first entry.
- A bad index as a whole (not JSON, not an object, unsupported `format`, no `plugins` list, too large) marks that
  repository as in error. Other repositories, the Plugins tab and installs keep working, and the last good cached copy
  stays in use.
- A published version must never change after release: the same id and version must always be the same bytes.

### Limits

| Limit | Value |
|-------|-------|
| Index size | 4 MiB |
| Request timeout | 20 s per network operation |
| Total time per index or icon fetch | 60 s |
| Total time per package download | 600 s |
| Package download | 64 MiB, the same as a local package (see [limits](limits.md)) |
| Icon | 512 KiB |

## Update schedule

`plugin.update_check` sets the minimum time between automatic refreshes: `daily` 1 day, `weekly` 7 days, `monthly` 30
days; `manual` never refreshes on its own.

- The time of the last successful check is stored in `~/.decker/repositories/state.json`, so the schedule survives
  restarts. A check counts as successful when at least one repository answered (or none is configured).
- At server start, updates are first computed from the cached indexes without any network access, and announced if
  there are any. A background thread then checks right away when the interval has elapsed (or no check has ever been
  made, as after a fresh install), and afterwards wakes hourly and refreshes only when the interval has elapsed. Server
  start never waits for the network.
- "Check for updates" refreshes immediately regardless of the schedule. It returns at once and runs in the
  background; the result arrives as the `repositories_check_finished` event.
- Network access never blocks other requests: a slow or unreachable repository only delays its own refresh. A
  repository that answers with garbage is recorded as in error and the remaining repositories are still refreshed.
- The cache lives in `~/.decker/repositories/`, one folder per repository holding its last good `index.json`, the
  status of the last refresh and cached icons.
- An installed plugin is updatable when the first repository offering its id has a greater version compatible with
  the running Decker, regardless of how it was installed. A local version that is equal or newer is up to date.
- A plugin is reported as missing when keys are assigned to it, it is not installed and a repository offers a
  compatible version.
- Updating is always your action; nothing is installed automatically. An update uses the normal installer: the running
  plugin is stopped, replaced and started again, its settings are kept, and a failed update restores the previous
  version.

### Server RPCs and event

| RPC | Purpose | Loopback-gated |
|-----|---------|----------------|
| `repositories_list` | Configured repositories with name, plugin count, last checked and last error | no |
| `repository_add(url)` / `repository_remove(url)` | Add (fetches and validates) or remove | yes |
| `repositories_check` | Start a background refresh of everything. Returns `{"started": true}` or `{"started": false, "reason": ...}` when a check is already running | no |
| `repository_catalog` | Every repository plugin with versions, installed state and platform support | no |
| `platform_info` | The server's platform as JSON `{os, desktop, session, label}` | no |
| `repository_icon(plugin_id)` | Icon as base64 | no |
| `plugin_install_from_repository(plugin_id, version)` | Install or update; empty `version` means the latest compatible. Returns a job id or an `error:` message | yes |
| `plugin_updates` | Current update report | no |
| `update_check_schedule` / `update_check_set_schedule(name)` | Read or set the schedule | set only |

Install progress and the result use the existing `plugin_install_progress` and `plugin_install_finished` events.
When a check started with `repositories_check` ends, the server pushes `repositories_check_finished` with
`{"repositories": [...]}`, the same entries `repositories_list` returns. When the set of updates changes, and once on
server start if there are any, the server pushes `plugin_updates_available`:

```json
{
  "updates": [{ "id": "com.decker.counter", "name": "Counter", "installed": "1.0.0", "available": "1.1.0" }],
  "missing": [{ "id": "com.decker.clock", "name": "Clock", "version": "1.0.0", "keys": 2 }]
}
```

## Hosting your own repository

The `decker-plugins` repository uses GitHub Releases for the packages and GitHub Pages for the index. You can do the
same, or serve `index.json` and the packages from any web server.

1. **Package** each plugin with `poe cli plugin pack <plugin-id>` (see the [quick start](sdk-quickstart.md)). This
   produces `<plugin-id>-<version>.deckerplugin`.
2. **Release** it: create a GitHub Release tagged `<plugin-id>-v<version>` (for example `com.decker.counter-v1.0.0`)
   and attach the `.deckerplugin`. Never modify or replace a published release. Attach a `<package>.json` sidecar
   next to it that holds the index version entry for that
   package (`version`, `decker`, `url`, `size`, `sha256`, `released`, `contributes`), so the index can be rebuilt from
   the releases alone.
3. **Index**: collect the plugin metadata (id, name, author, description, category, homepage, icon) and every
   release's version entry into one `index.json` in the format above, with icons next to it (for example
   `icons/<plugin-id>.png`), and publish it with GitHub Pages. Rebuild and redeploy on every new release.
4. **Share** the URL, for example `https://<user>.github.io/<repo>/index.json`. Users add it under Settings, Plugins,
   Repositories.

CI for such a repository should run each plugin's tests on every pull request, fail when a plugin changed without a
higher `version`, and on merge to the main branch publish releases for versions that have none yet and then redeploy
the index. Download URLs must stay stable, so keep old releases.

Check your index before sharing it: serve it locally (`python -m http.server`) or use a `file:` URL, add it in Decker
and check that the repository shows its plugin count and no error.
