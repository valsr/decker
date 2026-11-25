# Catalog icons as built-in refs — design

## Intent
Plugins now use Font Awesome refs (`fas:toolbox`) as icons. The plugin catalog ("Get More Plugins") shows icons for
plugins that are not installed, so the index must carry something the client can draw. Instead of the index builder
rendering a flat-grey PNG, the index carries the ref and the client draws it from its bundled Font Awesome, tinted
to the theme like the editor's tab icons.

## Goals / non-goals / anti-goals
- Goals: the index `icon` field may be a built-in ref; the catalog item exposes it; the dialog row draws it with the
  palette text colour, without an RPC; the builder writes the ref and no PNG for such plugins.
- Non-goals: an index format bump (format stays 1, the change is additive); live re-tint on a theme change while the
  dialog is open; changes to how installed plugins show icons.
- Anti-goals: breaking older Decker clients (they must still list the plugin, with the placeholder icon).

## Approaches
1. **Index carries the ref, the client draws it** (chosen): theme-correct, no PNG, small client change.
2. Builder renders a flat-grey PNG (the branch as it stood): no format or client change, but the icon is muted
   next to theme icons and cannot follow the theme.
3. A new index field (`icon_ref`) beside `icon`: explicit, but a second field for one concept and older clients
   would still ignore the plugin's icon anyway. Rejected.

## Design
- Index `icon`: a PNG URL (relative or http(s), as today) or a built-in ref accepted by
  `decker.common.icon_ref.parse_builtin_ref` (`fas:`/`far:`/`fab:` + name). Older clients reject the `fas` scheme in
  `_resolve_url`, log "Ignored icon" and keep the plugin (placeholder shown); verified in `repository_index.py`.
- `IndexPlugin` gains `icon_ref: str | None`; `icon_url` stays None for a ref. Parsing tries `parse_builtin_ref`
  first, then the URL path. A ref naming an icon Decker does not ship is kept as a ref; the client falls back to the
  placeholder.
- `_catalog_item` (server/service.py) adds `"icon_ref": plugin.icon_ref`. `repository_icon` returns `""` for a
  ref-only plugin (it has no `icon_url`, unchanged code path).
- Client `get_more_plugins.py`: a row whose plugin has a resolvable `icon_ref` sets its pixmap at construction from
  `read_builtin_ref`, tinted with `QApplication.palette().text().color()` (same helper path as `tab_icon`), and is
  not added to the pending RPC fetch queue. Other rows are unchanged.
- decker-plugins `tools/build_index.py`: a manifest icon that is a built-in ref is written to `index.json` as the ref
  and no PNG is made. `_write_builtin_icon`, `ICON_COLOR` and its tests are removed. File and SVG icons still become
  PNGs under `icons/`. The builder's pinned Decker dependency is not bumped here.
- Docs: `docs/plugins/repositories.md` (`icon` row), `CLAUDE.md` repositories sentence.

## Left to the builder
Exact name of the shared tint helper (reuse or extract from `tab_icon`), test layout.
