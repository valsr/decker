# Catalog Icon Refs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The repository index carries built-in icon refs and the catalog dialog draws them from the client's bundled Font Awesome, theme-tinted.

**Architecture:** `IndexPlugin.icon_ref` parsed beside `icon_url`; the catalog item exposes it; a dialog row draws it synchronously; the plugins repo builder writes the ref instead of rendering a PNG.

**Tech Stack:** Python 3.14, PyQt6, pytest + assertpy (Decker); pytest + ruff (decker-plugins).

**Spec:** `docs/superpowers/specs/2026-11-25-catalog-icon-refs-design.md`

## Global Constraints

- Quality gates (Decker): `poe format`, `poe mypy`, `poe pylint` (≥ 9.0), `poe test` (coverage ≥ 85%), plus `mypy --platform win32 .` and `mypy --platform darwin .`. decker-plugins: `uv run pytest -q`, `uv run ruff check .`.
- Black line length 120; annotate all signatures; max 5 args; methods under 50 lines.
- Index format stays `1`; a PNG URL `icon` keeps working exactly as before.
- Built-in refs are `fas:`/`far:`/`fab:` names accepted by `parse_builtin_ref`.
- The decker-plugins pinned Decker dependency is not bumped in this plan.

## Review Focus

- An index entry with `"icon": "fas:toolbox"` parses with `icon_ref == "fas:toolbox"`, `icon_url is None`, and no warning; a PNG URL still gives `icon_url`; an invalid string still warns and is ignored.
- A ref naming an unknown icon does not crash the dialog; the placeholder shows.
- A ref-only plugin is never queued for the `repository_icon` RPC.
- The builder writes the ref verbatim and creates no `icons/<id>.png` for it, and an unknown ref is not an error.

---

### Task 1: Decker parses, serves and draws icon refs

**Files:**
- Modify: `src/decker/plugin/repository_index.py` (`IndexPlugin` near line 55; icon parsing near lines 148-153)
- Modify: `src/decker/server/service.py` (`_catalog_item`, ~line 120)
- Modify: `src/decker/client/widgets/get_more_plugins.py` (`_PluginRow`, `_add_row` ~line 290)
- Modify (if needed for a shared tint helper): `src/decker/client/card_icons.py`
- Modify: `docs/plugins/repositories.md` (`icon` row ~line 102), `CLAUDE.md` (repositories sentence mentioning the index)
- Test: `tests/plugin/test_repository_index.py`, `tests/server/test_service.py`, `tests/client/widgets/test_get_more_plugins.py`

**Interfaces:**
- Consumes: `decker.common.icon_ref.parse_builtin_ref(ref: Any) -> tuple[str, str] | None`, `decker.asset.builtinicons.read_builtin_ref(ref: Any) -> bytes | None`, `decker.client.card_icons.decode_icon(icon_b64: str, tint: str | None = None) -> QPixmap | None`.
- Produces: `IndexPlugin.icon_ref: str | None`; catalog item key `"icon_ref": str | None`.

- [ ] **Step 1: Write the failing tests.**
  - `test_index_icon_may_be_a_builtin_ref` (test_repository_index.py): an entry with `"icon": "fas:toolbox"` gives `plugin.icon_ref == "fas:toolbox"`, `plugin.icon_url is None`, and `index.warnings` empty. `test_index_png_icon_still_gives_url` keeps the existing assertion (`icon_url` set, `icon_ref is None`). The existing invalid-URL test still gives both None and a warning.
  - `test_catalog_item_carries_icon_ref` (test_service.py): a catalog item for a plugin with an icon ref has `"icon_ref": "fas:toolbox"`; one without has `None`.
  - Client (test_get_more_plugins.py): `test_ref_icon_row_draws_without_rpc`: a plugin dict with `icon_ref: "fas:toolbox"` yields a row whose pixmap is not the placeholder, and the dialog's pending queue does not contain its id (`repository_icon` is never called for it); `test_unknown_ref_falls_back_to_placeholder`: `icon_ref: "fas:no-such-icon"` shows the placeholder and does not raise.

- [ ] **Step 2: Run** the three test files. Expected: new tests FAIL.

- [ ] **Step 3: Implement.**
  - `repository_index.py`: add `icon_ref: str | None = None` to `IndexPlugin`; when `raw.get("icon")` is a string and `parse_builtin_ref` accepts it, set `icon_ref` to the stripped ref and skip `_resolve_url`; otherwise the existing URL path.
  - `service.py` `_catalog_item`: add `"icon_ref": plugin.icon_ref`.
  - `get_more_plugins.py`: in `_PluginRow.__init__` (or `_add_row`) when `plugin.get("icon_ref")` resolves through `read_builtin_ref`, set the pixmap from `decode_icon(base64 of the bytes, palette text colour name)` and mark the row as having a local icon; `_add_row` then skips `self._pending.append(plugin_id)` for it (record the pixmap in `self._icons` like a loaded one). Unknown refs fall through to the placeholder and the normal URL queue (no URL means nothing is fetched).
  - Docs: the `icon` row says "Optional: a PNG URL, or a built-in icon ref such as `fas:toolbox` that Decker draws from its bundled Font Awesome (older Decker versions ignore a ref and show a placeholder); an invalid value is ignored (the plugin stays)". Add one sentence to the `CLAUDE.md` repositories text.

- [ ] **Step 4: Run** `poe format`, `poe mypy`, `poe pylint`, `poe test`, and the two `mypy --platform` runs. Expected: PASS.

- [ ] **Step 5: Commit** `feat: catalog icons can be built-in refs drawn by the client`.

---

### Task 2: decker-plugins builder writes the ref

**Files:**
- Modify: `/work/decker/decker-plugins/tools/build_index.py` (`_write_plugin_icon` and `build_index`; remove `_write_builtin_icon`, `ICON_COLOR`, and any now-unused import)
- Modify: `/work/decker/decker-plugins/README.md` or `docs/` only if they describe the index icon
- Test: `/work/decker/decker-plugins/tests/tools/test_build_index.py`

**Interfaces:**
- Consumes: `_icon_source(folder, icon)` and `_is_builtin_ref(ref)` already in `build_index.py`.
- Produces: `index.json` plugin `"icon"` equal to the manifest ref for built-in refs, `icons/<id>.png` for file icons.

- [ ] **Step 1: Write the failing tests** in `tests/tools/test_build_index.py`: `test_font_awesome_manifest_icon_is_written_as_a_ref` — a plugin whose manifest icon is `fas:toolbox` gives `index["plugins"][0]["icon"] == "fas:toolbox"` and `(out_dir / "icons" / f"{plugin_id}.png")` does not exist; `test_unknown_font_awesome_ref_is_still_written` (the builder does not validate names; the client falls back); replace the existing render and grey-pixel tests (`test_font_awesome_manifest_icon_is_rendered_to_png`, `test_unknown_font_awesome_icon_is_skipped_with_warning`) with these. File and SVG icon tests stay unchanged. Adjust `test_build_index_parses_with_decker` if it assumed a PNG for a built-in-ref plugin.

- [ ] **Step 2: Run** `uv run pytest -q tests/tools`. Expected: new tests FAIL.

- [ ] **Step 3: Implement.** Make `_write_plugin_icon(folder, icon_ref, out_dir, plugin_id)` return the string to store as the plugin's `icon` (the relative PNG path, the ref, or `None`) instead of a bool, and have `build_index` set `plugin["icon"]` from it; delete `_write_builtin_icon` and `ICON_COLOR`. Keep it under 50 lines per function.

- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check .`. Expected: PASS.

- [ ] **Step 5: Commit** (decker-plugins branch `quick-fixes` is already pushed or not — check; if unpushed, commit on it) `refactor: write built-in icon refs into the index instead of rendering them`.
