# Theme icon colour — design

## Intent
SVG icons shown in the client (sidebar card tiles, action dropdowns, the icon picker) are decoded without a colour, so
`currentColor` renders black and disappears on the dark theme (Delay and Sequence in the sidebar). Themes should
define an icon colour, defaulting to the text colour, and every UI icon should use it.

## Goals / non-goals / anti-goals
- Goals: `ThemeColors` gains an optional `icon` colour that defaults to `text`; one helper returns the current icon
  colour; the UI call sites that render SVG icons without a tint use it; the icon picker's default tint and the
  editor tab icons use it instead of the raw text colour.
- Non-goals: live re-tint of icons already on screen when the theme changes (icons tint when built); recolouring SVGs
  that carry their own fills (only `currentColor` is replaced, as `decode_icon` already does); the key face the server
  draws; the play/delete badges on the key grid and the user's explicit icon colour in the icon editor.
- Anti-goals: changing the bundled DARK/LIGHT look (they leave `icon` unset, so icons use `text`); breaking custom
  stylesheets (they have no `ThemeColors`, so icons fall back to the palette text colour).

## Approaches
1. **Optional `ThemeColors.icon` plus one `icon_tint()` helper** (chosen): themes may set it, the default keeps today's
   text colour, call sites need one function. The theme stays defined by `theme.qss` and `ThemeColors`.
2. Add `$icon` to `theme.qss` and read it back from the stylesheet: Qt has no role for it, so the client would have to
   parse its own QSS. Rejected.
3. Tint everything with the palette text colour at each call site: fixes the bug but gives themes no way to choose an
   icon colour, which was asked for. Rejected.

## Design
- `themes.py`: `ThemeColors.icon: str = ""` (last field, defaulted) and a property `icon_color -> str` returning `icon`
  or `text`. `apply_theme(app, stylesheet, colors)` stores the resolved colour on the application
  (`app.setProperty("iconColor", colors.icon_color)`), and clears it (sets an empty value) for a custom stylesheet; `icon_tint()` treats empty and unset alike. `icon` is not a
  stylesheet token, so the "every palette colour is used" test excludes it with a one-line reason.
- Helper `icon_tint() -> str` in `client/card_icons.py`: the application's `iconColor` property if set, else
  `QApplication.palette().text().color().name()`.
- Call sites: `card_pixmap` (sidebar tiles and the drag pixmap) passes `icon_tint()` to `decode_icon`;
  `action_edit._action_icon` likewise; `builtin_pixmap`/`tab_icon` use `icon_tint()` instead of the palette text
  colour; `IconPicker`'s default `_tint` uses `icon_tint()`. The selected sidebar tile keeps its existing "untinted
  on selection" behaviour.
- Docs: CLAUDE.md (client/UI themes mention) and the docs of custom themes if they list `ThemeColors` fields.

## Left to the builder
Where `icon_tint` lives if `card_icons` would create an import cycle with `themes`; test layout; whether the sidebar
drag pixmap needs the tint (it should look like the tile).
