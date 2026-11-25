"""Tests for decker.client.card_icons."""

import base64
from pathlib import Path

from assertpy import assert_that
from PyQt6.QtCore import Qt

from decker.client.card_icons import (
    card_pixmap,
    decode_icon,
    icon_tint,
    placeholder_pixmap,
    tab_icon,
)

_COUNTER_PNG = Path(__file__).parents[1] / "fixtures" / "plugins" / "com.decker.testfixture" / "icons" / "counter.png"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class TestDecodeIcon:
    def test_decodes_png(self, qapp):  # pylint: disable=unused-argument
        pixmap = decode_icon(_b64(_COUNTER_PNG.read_bytes()))

        assert pixmap is not None
        assert_that(pixmap.width()).is_greater_than(0)

    def test_decodes_svg(self, qapp):  # pylint: disable=unused-argument
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>'

        assert_that(decode_icon(_b64(svg))).is_not_none()

    def test_empty_invalid_base64_or_non_image_is_none(self, qapp):  # pylint: disable=unused-argument
        assert_that(decode_icon("")).is_none()
        assert_that(decode_icon("not base64!")).is_none()
        assert_that(decode_icon(_b64(b"not an image"))).is_none()


class TestActionPixmap:
    def test_scales_icon_to_size(self, qapp):  # pylint: disable=unused-argument
        pixmap = card_pixmap({"card_name": "Counter", "icon_b64": _b64(_COUNTER_PNG.read_bytes())}, 32)

        assert_that((pixmap.width(), pixmap.height())).is_equal_to((32, 32))

    def test_falls_back_to_letter_tile(self, qapp):  # pylint: disable=unused-argument
        pixmap = card_pixmap({"card_name": "Open URL", "icon_b64": ""}, 40)

        assert_that(pixmap.toImage()).is_equal_to(placeholder_pixmap("Open URL", 40).toImage())


class TestPlaceholder:
    def test_same_name_same_tile_and_corners_transparent(self, qapp):  # pylint: disable=unused-argument
        first = placeholder_pixmap("Play", 48).toImage()

        assert_that(first).is_equal_to(placeholder_pixmap("Play", 48).toImage())
        assert_that(first.pixelColor(0, 0).alpha()).is_equal_to(0)
        assert_that(first.pixelColor(24, 4).alpha()).is_equal_to(255)

    def test_blank_name_still_renders(self, qapp):  # pylint: disable=unused-argument
        pixmap = placeholder_pixmap("  ", 16)

        assert_that(pixmap.isNull()).is_false()
        assert_that(pixmap.hasAlphaChannel()).is_true()
        assert_that(Qt.GlobalColor.transparent).is_not_none()


_CURRENT_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path fill="currentColor" d="M0 0h10v10H0z"/></svg>'
)
_PLAIN_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path fill="#0000ff" d="M0 0h10v10H0z"/></svg>'
)


def _centre(pixmap) -> tuple[int, int, int, int]:
    image = pixmap.toImage()
    pixel = image.pixelColor(image.width() // 2, image.height() // 2)
    return pixel.red(), pixel.green(), pixel.blue(), pixel.alpha()


class TestDecodeIconTint:
    def test_tint_recolours_currentcolor_svg(self, qapp):  # pylint: disable=unused-argument
        pixmap = decode_icon(_b64(_CURRENT_SVG), "#ff0000")
        assert pixmap is not None
        assert_that(_centre(pixmap)).is_equal_to((255, 0, 0, 255))

    def test_rgba_tint_keeps_alpha_order_right(self, qapp):  # pylint: disable=unused-argument
        pixmap = decode_icon(_b64(_CURRENT_SVG), "#ff000080")
        assert pixmap is not None
        red, green, blue, alpha = _centre(pixmap)
        assert_that((red, green, blue)).is_equal_to((255, 0, 0))
        assert_that(alpha).is_between(120, 135)

    def test_tint_leaves_plain_svg_and_raster_alone(self, qapp):  # pylint: disable=unused-argument
        plain = decode_icon(_b64(_PLAIN_SVG), "#ff0000")
        raster = decode_icon(_b64(_COUNTER_PNG.read_bytes()), "#ff0000")
        assert plain is not None and raster is not None
        assert_that(_centre(plain)).is_equal_to((0, 0, 255, 255))
        untinted = decode_icon(_b64(_COUNTER_PNG.read_bytes()))
        assert untinted is not None
        assert_that(raster.toImage()).is_equal_to(untinted.toImage())

    def test_decode_without_tint_is_unchanged(self, qapp):  # pylint: disable=unused-argument
        pixmap = decode_icon(_b64(_CURRENT_SVG))
        assert pixmap is not None
        assert_that(_centre(pixmap)).is_equal_to((0, 0, 0, 255))

    def test_invalid_tint_is_ignored(self, qapp):  # pylint: disable=unused-argument
        pixmap = decode_icon(_b64(_CURRENT_SVG), "not a colour")
        assert pixmap is not None
        assert_that(_centre(pixmap)).is_equal_to((0, 0, 0, 255))


class TestTabIcon:
    def test_a_bundled_reference_gives_an_icon(self, qapp):  # pylint: disable=unused-argument
        assert_that(tab_icon("fas:image").isNull()).is_false()

    def test_an_unknown_reference_gives_an_empty_icon(self, qapp):  # pylint: disable=unused-argument
        assert_that(tab_icon("fas:nope").isNull()).is_true()


class TestIconTint:
    def test_icon_tint_uses_the_theme_colour(self, qapp):
        qapp.setProperty("iconColor", "#123456")
        try:
            assert_that(icon_tint()).is_equal_to("#123456")
        finally:
            qapp.setProperty("iconColor", None)

    def test_icon_tint_falls_back_to_palette_text(self, qapp):
        qapp.setProperty("iconColor", "")

        assert_that(icon_tint()).is_equal_to(qapp.palette().text().color().name())


def _opaque_pixels(pixmap) -> set[tuple[int, int, int]]:
    image = pixmap.toImage()
    colours = (image.pixelColor(x, y) for x in range(image.width()) for y in range(image.height()))
    return {(c.red(), c.green(), c.blue()) for c in colours if c.alpha() == 255}


def test_card_pixmap_tints_currentcolor_svgs(qapp):
    fixed = _CURRENT_SVG.replace(b"currentColor", b"#00ff00")
    qapp.setProperty("iconColor", "#ff0000")
    try:
        tinted = card_pixmap({"icon_b64": _b64(_CURRENT_SVG), "card_name": "A"}, 16)
        kept = card_pixmap({"icon_b64": _b64(fixed), "card_name": "A"}, 16)
        png = card_pixmap({"icon_b64": _b64(_COUNTER_PNG.read_bytes()), "card_name": "A"}, 16)
    finally:
        qapp.setProperty("iconColor", None)
    plain_png = card_pixmap({"icon_b64": _b64(_COUNTER_PNG.read_bytes()), "card_name": "A"}, 16)

    assert_that(_opaque_pixels(tinted)).is_equal_to({(255, 0, 0)})
    assert_that(_opaque_pixels(kept)).is_equal_to({(0, 255, 0)})
    assert_that(png.toImage()).is_equal_to(plain_png.toImage())
