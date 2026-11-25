"""Tests for decker.client.themes."""

import dataclasses
import re
from dataclasses import replace

import pytest
from assertpy import assert_that
from PyQt6.QtGui import QColor, QPalette

from decker.client.themes import (
    BUNDLED,
    DARK,
    LIGHT,
    ThemeColors,
    apply_theme,
    build_palette,
    render_stylesheet,
)

_AA_NORMAL_TEXT = 4.5


def _luminance(hex_color: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    color = QColor(hex_color)
    return 0.2126 * channel(color.red()) + 0.7152 * channel(color.green()) + 0.0722 * channel(color.blue())


def _contrast(foreground: str, background: str) -> float:
    lighter, darker = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


# Every foreground/background pairing the stylesheet actually renders text with.
_TEXT_PAIRS = [
    ("text", "window"),
    ("text", "surface"),
    ("text", "base"),
    ("text", "raised"),
    ("text", "hover"),
    ("text", "pressed"),
    ("text_muted", "window"),
    ("text_muted", "surface"),
    ("text_muted", "base"),
    ("error", "window"),
    ("error", "surface"),
    ("success", "window"),
    ("success", "surface"),
    ("accent_text", "accent"),
    ("key_text", "key_bg"),
    ("key_text", "key_hover"),
    ("key_text", "key_pressed"),
    ("toast_text", "toast_bg"),
]


@pytest.mark.parametrize("theme", sorted(BUNDLED))
@pytest.mark.parametrize("foreground,background", _TEXT_PAIRS)
def test_text_meets_wcag_aa_contrast(theme, foreground, background):
    colors = BUNDLED[theme]
    ratio = _contrast(getattr(colors, foreground), getattr(colors, background))

    assert_that(ratio).described_as(f"{theme}: {foreground} on {background}").is_greater_than_or_equal_to(
        _AA_NORMAL_TEXT
    )


class TestRenderStylesheet:
    @pytest.mark.parametrize("colors", [DARK, LIGHT])
    def test_substitutes_every_placeholder(self, colors):
        sheet = render_stylesheet(colors)

        assert_that(re.findall(r"\$\w+", sheet)).is_empty()
        assert_that(sheet).contains(colors.window).contains(colors.text)

    def test_every_palette_colour_is_used(self):
        names = [name for name in _fields() if name != "icon"]  # icon is not a stylesheet token
        sheet = render_stylesheet(ThemeColors(**{name: f"#{i:06x}" for i, name in enumerate(names)}))

        for i, name in enumerate(names):
            assert_that(sheet).described_as(name).contains(f"#{i:06x}")

    def test_icon_colour_defaults_to_text(self):
        assert_that(DARK.icon_color).is_equal_to(DARK.text)

    def test_explicit_icon_colour_wins(self):
        assert_that(replace(DARK, icon="#ff0000").icon_color).is_equal_to("#ff0000")

    def test_dark_and_light_differ(self):
        assert_that(render_stylesheet(DARK)).is_not_equal_to(render_stylesheet(LIGHT))


class TestPalette:
    def test_build_palette_maps_core_roles(self):
        palette = build_palette(DARK)

        assert_that(palette.color(QPalette.ColorRole.Window).name()).is_equal_to(DARK.window)
        assert_that(palette.color(QPalette.ColorRole.Text).name()).is_equal_to(DARK.text)
        assert_that(palette.color(QPalette.ColorRole.Highlight).name()).is_equal_to(DARK.accent)
        assert_that(palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text).name()).is_equal_to(
            DARK.text_disabled
        )

    def test_apply_theme_sets_palette_and_sheet(self, qapp):
        original = (qapp.styleSheet(), qapp.palette())
        try:
            apply_theme(qapp, "QWidget {}", LIGHT)
            assert_that(qapp.styleSheet()).is_equal_to("QWidget {}")
            assert_that(qapp.palette().color(QPalette.ColorRole.Window).name()).is_equal_to(LIGHT.window)

            apply_theme(qapp, "QLabel {}", None)
            assert_that(qapp.palette().color(QPalette.ColorRole.Window).name()).is_not_equal_to(LIGHT.window)
        finally:
            qapp.setStyleSheet(original[0])
            qapp.setPalette(original[1])


def test_apply_theme_publishes_the_icon_colour(qapp):
    original = (qapp.styleSheet(), qapp.palette(), qapp.property("iconColor"))
    try:
        apply_theme(qapp, "", replace(DARK, icon="#ff0000"))

        assert_that(qapp.property("iconColor")).is_equal_to("#ff0000")
    finally:
        qapp.setStyleSheet(original[0])
        qapp.setPalette(original[1])
        qapp.setProperty("iconColor", original[2])


def test_apply_theme_custom_sheet_clears_the_icon_colour(qapp):
    original = (qapp.styleSheet(), qapp.palette(), qapp.property("iconColor"))
    try:
        apply_theme(qapp, "", replace(DARK, icon="#ff0000"))
        apply_theme(qapp, "", None)

        assert_that(qapp.property("iconColor")).is_false()
    finally:
        qapp.setStyleSheet(original[0])
        qapp.setPalette(original[1])
        qapp.setProperty("iconColor", original[2])


def _fields() -> list[str]:
    return [field.name for field in dataclasses.fields(ThemeColors)]


def test_unsaved_changes_highlight_the_save_button() -> None:
    from decker.client.themes import (  # pylint: disable=import-outside-toplevel
        BUNDLED,
        render_stylesheet,
    )

    for colors in BUNDLED.values():
        assert 'QPushButton[dirty="true"]' in render_stylesheet(colors)


def test_unsaved_save_button_still_reacts_to_hover_and_press() -> None:
    from decker.client.themes import (  # pylint: disable=import-outside-toplevel
        BUNDLED,
        render_stylesheet,
    )

    for colors in BUNDLED.values():
        sheet = render_stylesheet(colors)
        assert 'QPushButton[dirty="true"]:hover' in sheet
        assert 'QPushButton[dirty="true"]:pressed' in sheet
