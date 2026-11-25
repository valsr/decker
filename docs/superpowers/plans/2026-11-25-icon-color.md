# Theme Icon Colour Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Themes define an optional icon colour (default: the text colour) and every UI SVG icon uses it.

**Architecture:** `ThemeColors.icon` + `icon_color` property; `apply_theme` publishes it on the QApplication; `icon_tint()` reads it with a palette-text fallback; the tint-less call sites call `icon_tint()`.

**Tech Stack:** Python 3.14, PyQt6, pytest + assertpy.

**Spec:** `docs/superpowers/specs/2026-11-25-icon-color-design.md`

## Global Constraints

- Quality gates: `poe format`, `poe mypy`, `poe pylint` (≥ 9.0), `poe test` (coverage ≥ 85%), plus `mypy --platform win32 .` and `mypy --platform darwin .`.
- Black 120; annotate all signatures; max 5 args; methods under 50 lines; pytest + assertpy.
- Bundled DARK/LIGHT leave `icon` unset (icons use `text`); stylesheet output is unchanged for them.
- Only `currentColor` SVG fills are recoloured (existing `decode_icon` behaviour); PNG and fixed-fill icons are untouched.
- Custom stylesheets (no `ThemeColors`) fall back to the palette text colour.

## Review Focus

- `ThemeColors(**all fields except icon)` still constructs (default), and `icon_color` equals `text` then, `icon` when set.
- `apply_theme` with a custom stylesheet clears a previously set icon colour (no stale colour after switching themes).
- `icon_tint()` works with no `QApplication` property and with a property set to an empty string.
- A black-filled `currentColor` SVG in the sidebar is drawn in the icon colour on the dark theme, and an SVG with its own fixed fill keeps it.

---

### Task 1: Theme icon colour and `icon_tint()`

**Files:**
- Modify: `src/decker/client/themes.py` (`ThemeColors`, `apply_theme`)
- Modify: `src/decker/client/card_icons.py` (new `icon_tint`; `builtin_pixmap` and `tab_icon` use it)
- Test: `tests/client/test_themes.py`, `tests/client/test_card_icons.py`

**Interfaces:**
- Produces: `ThemeColors.icon: str = ""`; `ThemeColors.icon_color -> str` (property); `decker.client.card_icons.icon_tint() -> str` (`#rrggbb`).

- [ ] **Step 1: Write the failing tests.**
  - themes: `test_icon_colour_defaults_to_text` (`DARK.icon_color == DARK.text`); `test_explicit_icon_colour_wins` (`replace(DARK, icon="#ff0000").icon_color == "#ff0000"`); `test_apply_theme_publishes_the_icon_colour` (`apply_theme(app, sheet, replace(DARK, icon="#ff0000"))` then `app.property("iconColor") == "#ff0000"`); `test_apply_theme_custom_sheet_clears_the_icon_colour` (after the above, `apply_theme(app, "", None)` leaves `app.property("iconColor")` falsy); adjust `test_every_palette_colour_is_used` to skip the `icon` field with a comment that it is not a stylesheet token.
  - card_icons: `test_icon_tint_uses_the_theme_colour` (property set → returned) and `test_icon_tint_falls_back_to_palette_text` (property unset or empty → palette text name).
- [ ] **Step 2: Run** `tests/client/test_themes.py tests/client/test_card_icons.py`. Expected: new tests FAIL.
- [ ] **Step 3: Implement** the interfaces above; `tab_icon` and `builtin_pixmap` replace `QApplication.palette().text().color().name()` with `icon_tint()`.
- [ ] **Step 4: Run** the gates (`poe format`, `poe mypy`, `poe pylint`, focused tests, `poe test`). Expected: PASS.
- [ ] **Step 5: Commit** `feat: themes can define an icon colour that defaults to the text colour`.

---

### Task 2: Tint the UI icon call sites, docs

**Files:**
- Modify: `src/decker/client/card_icons.py` (`card_pixmap`)
- Modify: `src/decker/client/widgets/action_edit.py` (`_action_icon`, ~line 65)
- Modify: `src/decker/client/icon_picker.py` (default `_tint`, ~line 229)
- Modify: `CLAUDE.md`
- Test: `tests/client/test_card_icons.py`, `tests/client/widgets/test_action_edit.py`, `tests/client/test_icon_picker.py`, `tests/client/widgets/test_sidebar.py`

**Interfaces:**
- Consumes: `icon_tint() -> str` from Task 1.

- [ ] **Step 1: Write the failing tests.**
  - `test_card_pixmap_tints_currentcolor_svgs`: with the app's `iconColor` property set to `#ff0000`, a card whose `icon_b64` is an SVG filled with `currentColor` gives a pixmap whose opaque pixels are red; with a fixed `fill="#00ff00"` SVG they stay green; with a PNG they are unchanged.
  - `test_action_icon_is_tinted` (action_edit): same property, an SVG action icon → red pixels.
  - `test_icon_picker_default_tint_follows_the_icon_colour` (icon_picker): a picker built with no explicit tint uses `icon_tint()` (property `#ff0000` → `_tint == "#ff0000"`), while an explicit `tint=""` still disables tinting.
  - `test_sidebar_tile_for_a_black_svg_is_visible_on_dark`: with the dark theme applied, the sidebar tile of an SVG preset whose default fill is `currentColor` is not black (its opaque pixels equal the theme's icon colour).
- [ ] **Step 2: Run** those tests. Expected: FAIL.
- [ ] **Step 3: Implement.** `card_pixmap` passes `icon_tint()` as the `tint` to `decode_icon`; `_action_icon` likewise; `IconPicker.__init__` default tint uses `icon_tint()` instead of `self.palette().text()`. Update CLAUDE.md's client/UI text: themes carry an optional icon colour (default text), `icon_tint()` is what UI icons use.
- [ ] **Step 4: Run** all gates incl. both `mypy --platform` runs. Expected: PASS.
- [ ] **Step 5: Commit** `fix: tint UI SVG icons with the theme icon colour`.
