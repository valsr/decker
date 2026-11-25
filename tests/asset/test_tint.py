import io
import re

from assertpy import assert_that
from PIL import Image

from decker.asset.renderer import AssetRenderer
from decker.asset.tint import is_unstyled_svg, tint_svg

_FA = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><!--! comment --><path fill="currentColor" d="M0 0h10v10H0z"/></svg>'
_PLAIN = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path d="M0 0h10v10H0z"/></svg>'


def _root(data: bytes) -> str:
    match = re.search(rb"<svg\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*>", data, re.IGNORECASE)
    assert match is not None
    return match.group(0).decode()


def test_sets_color_and_default_fill() -> None:
    root = _root(tint_svg(_FA, "#ff0000"))
    assert_that(root).contains('color="#ff0000"').contains('fill="#ff0000"')


def test_keeps_existing_fill() -> None:
    data = b'<svg xmlns="http://www.w3.org/2000/svg" fill="blue"><path d="M0 0h1v1z"/></svg>'
    root = _root(tint_svg(data, "#ff0000"))
    assert_that(root).contains('fill="blue"').contains('color="#ff0000"')
    assert_that(root.count("fill=")).is_equal_to(1)


def test_alpha_becomes_opacity() -> None:
    assert_that(_root(tint_svg(_FA, "#ff000080"))).contains('opacity="0.502"')
    assert_that(_root(tint_svg(_FA, "#ff0000"))).does_not_contain("opacity")
    assert_that(_root(tint_svg(_FA, "#ff000000"))).contains('opacity="0"')


def test_replaces_existing_color_and_opacity() -> None:
    data = b'<svg xmlns="http://www.w3.org/2000/svg" color="green" opacity="0.2"><path d="M0 0h1v1z"/></svg>'
    root = _root(tint_svg(data, "#ff0000"))
    assert_that(root.count("color=")).is_equal_to(1)
    assert_that(root).contains('color="#ff0000"').does_not_contain("green").does_not_contain("0.2")


def test_uppercase_tag_and_prolog_and_comment() -> None:
    data = b'<?xml version="1.0"?>\n<!-- <svg fake="1"> -->\n<!DOCTYPE svg>\n<SVG xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1v1z"/></SVG>'
    out = tint_svg(data, "#00ff00")
    assert_that(out.decode()).contains('color="#00ff00"')
    assert_that(out.decode()).contains('fake="1"')  # the commented-out tag is untouched
    assert_that(out.decode().count('color="#00ff00"')).is_equal_to(1)


def test_self_closing_root() -> None:
    out = tint_svg(b'<svg xmlns="http://www.w3.org/2000/svg"/>', "#00ff00")
    assert_that(out.decode()).ends_with("/>").contains('color="#00ff00"')


def test_no_svg_root_returns_input() -> None:
    assert_that(tint_svg(b"not svg at all", "#ff0000")).is_equal_to(b"not svg at all")


def test_invalid_color_returns_input() -> None:
    for bad in ("red", "#12", "", "#1234567"):
        assert_that(tint_svg(_FA, bad)).is_equal_to(_FA)


def test_tint_changes_only_the_root_tag() -> None:
    out = tint_svg(_FA, "#ff0000")
    assert_that(out.split(b">", 1)[1]).is_equal_to(_FA.split(b">", 1)[1])


def _centre(data: bytes, color: str | None) -> tuple[int, ...]:
    image = AssetRenderer().render_bytes(data, 20, 20, color)
    assert image is not None
    pixel = image.getpixel((10, 10))
    assert isinstance(pixel, tuple)
    return pixel


def test_currentcolor_icon_renders_in_given_colour() -> None:
    assert_that(_centre(_FA, "#ff0000")).is_equal_to((255, 0, 0, 255))


def test_currentcolor_icon_without_colour_renders_black() -> None:
    assert_that(_centre(_FA, None)).is_equal_to((0, 0, 0, 255))


def test_unstyled_svg_takes_default_fill() -> None:
    assert_that(_centre(_PLAIN, "#00ff00")).is_equal_to((0, 255, 0, 255))


def test_translucent_colour_has_partial_alpha() -> None:
    red, green, blue, alpha = _centre(_FA, "#ff000080")
    assert_that((red, green, blue)).is_equal_to((255, 0, 0))
    assert_that(alpha).is_between(120, 135)


def test_fully_transparent_colour_renders_invisible_without_crashing() -> None:
    assert_that(_centre(_FA, "#ff000000")[3]).is_equal_to(0)


def test_svg_without_viewbox_does_not_crash() -> None:
    data = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><path fill="currentColor" d="M0 0h10v10H0z"/></svg>'
    assert_that(_centre(data, "#0000ff")).is_equal_to((0, 0, 255, 255))


def test_png_ignores_colour() -> None:
    buf = io.BytesIO()
    Image.new("RGBA", (4, 4), "#336699").save(buf, format="PNG")
    assert_that(_centre(buf.getvalue(), "#ff0000")).is_equal_to((0x33, 0x66, 0x99, 255))


def _svg(body: str, root: str = "") -> bytes:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"{root}>{body}</svg>'.encode()


def test_unstyled_when_no_colour_anywhere() -> None:
    assert_that(is_unstyled_svg(_PLAIN)).is_true()


def test_currentcolor_and_fill_none_count_as_unstyled() -> None:
    assert_that(is_unstyled_svg(_FA)).is_true()
    assert_that(is_unstyled_svg(_svg('<path fill="none" d="M0 0h1v1z"/>', ' fill="currentColor"'))).is_true()


def test_child_fill_makes_it_styled() -> None:
    assert_that(is_unstyled_svg(_svg('<path fill="#00ff00" d="M0 0h1v1z"/>'))).is_false()


def test_stroke_attribute_makes_it_styled() -> None:
    assert_that(is_unstyled_svg(_svg("<path stroke='red' d='M0 0h1v1z'/>"))).is_false()


def test_style_attribute_colour_makes_it_styled() -> None:
    assert_that(is_unstyled_svg(_svg('<path style="fill:#f00" d="M0 0h1v1z"/>'))).is_false()


def test_style_element_makes_it_styled() -> None:
    assert_that(is_unstyled_svg(_svg("<style>path{fill:red}</style><path d='M0 0h1v1z'/>"))).is_false()


def test_png_is_not_unstyled() -> None:
    assert_that(is_unstyled_svg(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)).is_false()


def test_garbage_bytes_are_not_unstyled() -> None:
    assert_that(is_unstyled_svg(b"\xff\xfe not an svg")).is_false()
    assert_that(is_unstyled_svg(b"")).is_false()
