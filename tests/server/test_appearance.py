import hashlib
import io
import sys
from typing import Any

import cairosvg  # type: ignore[import-untyped]
import pytest
from assertpy import assert_that
from PIL import Image, ImageChops

from decker.animation import gif as gif_module
from decker.animation.gif import MAX_GIF_FRAMES
from decker.animation.model import AnimationSpec
from decker.common.text_style import TEXT_STYLE_DEFAULTS
from decker.plugin.manifest import ManifestValidationError, PluginManifest
from decker.server import fonts
from decker.server.appearance import (
    compose_key_face,
    compose_key_frames,
    compose_key_image,
    resolve_appearance,
)
from tests.animation.grown_gif import grown_gif
from tests.server.animation_fixture import make_gif


def _png(color: str, size: int = 72) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (size, size), color).save(buf, format="PNG")
    return buf.getvalue()


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def _appearance(**overrides: Any) -> dict[str, Any]:
    return resolve_appearance({}, overrides)


def test_defaults_from_schema() -> None:
    result = resolve_appearance({}, {})
    assert_that(result).is_equal_to(
        {
            "background": {"type": "color", "color": "#000000", "image": {"ref": ""}},
            "title": dict(TEXT_STYLE_DEFAULTS),
            "icon": {"ref": "", "color": "#ffffff"},
        }
    )


def test_icon_is_an_appearance_key_and_survives_resolution() -> None:
    from decker.common.appearance_schema import (
        APPEARANCE_KEYS,  # pylint: disable=import-outside-toplevel
    )

    stored = {"icon": {"ref": "pack:a.b/x.svg", "color": "#ff0000"}}
    assert_that(APPEARANCE_KEYS).contains("icon")
    assert_that(resolve_appearance({}, stored)["icon"]).is_equal_to(stored["icon"])


def test_action_defaults_override_schema_defaults() -> None:
    assert_that(resolve_appearance({"background": "#112233"}, {})["background"]["color"]).is_equal_to("#112233")


def test_stored_overrides_action_defaults() -> None:
    result = resolve_appearance({"background": "#112233"}, {"background": "#445566"})
    assert_that(result["background"]["color"]).is_equal_to("#445566")


def test_background_fills_key() -> None:
    img = _open(compose_key_image(_appearance(background="#112233"), None, ""))
    assert_that(img.size).is_equal_to((144, 144))
    assert_that(img.getpixel((0, 0))).is_equal_to((0x11, 0x22, 0x33))


def test_plugin_image_drawn_over_background() -> None:
    img = _open(compose_key_image(_appearance(background="#0000ff"), _png("#ff0000"), ""))
    assert_that(img.getpixel((72, 72))).is_equal_to((255, 0, 0))


def test_transparent_plugin_image_shows_background() -> None:
    img = _open(compose_key_image(_appearance(background="#0000ff"), _png("#ff000000"), ""))
    assert_that(img.getpixel((72, 72))).is_equal_to((0, 0, 255))


def _has_title_pixels(img: Image.Image) -> bool:
    return any(img.getpixel((x, y)) != (0, 0, 0) for x in range(144) for y in range(100, 144))


def test_user_title_beats_plugin_title() -> None:
    with_user = compose_key_image(_appearance(title="User"), None, "Plugin")
    user_only = compose_key_image(_appearance(title="User"), None, "")
    assert_that(with_user).is_equal_to(user_only)
    assert_that(_has_title_pixels(_open(with_user))).is_true()


def test_plugin_title_used_when_no_user_title() -> None:
    img = _open(compose_key_image(_appearance(), None, "Plugin"))
    assert_that(_has_title_pixels(img)).is_true()
    assert_that(compose_key_image(_appearance(), None, "Plugin")).is_not_equal_to(
        compose_key_image(_appearance(), None, "")
    )


def test_legacy_show_title_false_hides_title() -> None:
    img = _open(compose_key_image(_appearance(show_title=False, title="X"), None, "Plugin"))
    assert_that(_has_title_pixels(img)).is_false()


def test_title_outline_drawn() -> None:
    plain = compose_key_image(_appearance(title="X"), None, "")
    outlined = compose_key_image(_appearance(title="X", title_outline="#ff0000"), None, "")
    assert_that(plain).is_not_equal_to(outlined)


def test_svg_plugin_image_supported() -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="72" height="72"><rect width="72" height="72" fill="#00ff00"/></svg>'
    assert_that(cairosvg).is_not_none()
    img = _open(compose_key_image(_appearance(), svg, ""))
    assert_that(img.getpixel((72, 72))).is_equal_to((0, 255, 0))


def test_undecodable_plugin_image_skipped() -> None:
    img = _open(compose_key_image(_appearance(background="#112233"), b"garbage", ""))
    assert_that(img.getpixel((72, 72))).is_equal_to((0x11, 0x22, 0x33))


def test_unknown_appearance_key_in_manifest_rejected() -> None:
    data = {
        "format": 3,
        "id": "com.example.p",
        "name": "P",
        "version": "1.0.0",
        "author": "A",
        "description": "d",
        "decker": ">=0.7",
        "runtime": {"type": "python", "entry": "plugin.py", "python": ">=3.12", "requirements": []},
        "contributes": {"cards": [{"id": "com.example.p.a", "name": "A", "appearance": {"background": "#112233"}}]},
    }
    assert_that(PluginManifest.from_dict(data).cards[0].appearance).is_equal_to({"background": "#112233"})
    data["contributes"]["cards"][0]["appearance"] = {"bogus": 1}  # type: ignore[index]
    with pytest.raises(ManifestValidationError, match="com.example.p.a.*bogus"):
        PluginManifest.from_dict(data)


def test_truncated_png_plugin_image_skipped() -> None:
    truncated = _png("#ff0000", 256)[:60]
    img = _open(compose_key_image(_appearance(background="#112233"), truncated, ""))
    assert_that(img.getpixel((72, 72))).is_equal_to((0x11, 0x22, 0x33))


def test_renderer_failure_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("cairo failure")

    monkeypatch.setattr("decker.server.appearance._RENDERER.render_bytes", boom)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    img = _open(compose_key_image(_appearance(background="#112233"), svg, ""))
    assert_that(img.getpixel((72, 72))).is_equal_to((0x11, 0x22, 0x33))


def test_legacy_flat_keys_fold_into_title() -> None:
    title = resolve_appearance({}, {"title": "Hi", "title_position": "top", "show_title": False})["title"]
    assert_that(title).is_equal_to({**TEXT_STYLE_DEFAULTS, "text": "Hi", "position": "top", "visible": False})


def test_nested_title_wins_over_legacy() -> None:
    stored = {"title": {"size": 20}, "title_size": 9}
    assert_that(resolve_appearance({}, stored)["title"]["size"]).is_equal_to(20)


def test_stored_title_merges_field_wise_over_action_defaults() -> None:
    title = resolve_appearance({"title_position": "top", "title_size": 20}, {"title": {"size": 30}})["title"]
    assert_that(title["position"]).is_equal_to("top")
    assert_that(title["size"]).is_equal_to(30)


def test_legacy_keys_removed_from_resolved_appearance() -> None:
    result = resolve_appearance({}, {"title_size": 9, "show_title": True})
    assert_that(result).contains_only("background", "icon", "title")


def test_manifest_default_legacy_keys_still_accepted() -> None:
    data = _manifest_data({"title_position": "top", "show_title": False})
    assert_that(PluginManifest.from_dict(data).cards[0].appearance).is_equal_to(
        {"title_position": "top", "show_title": False}
    )


def test_manifest_default_nested_title_accepted() -> None:
    data = _manifest_data({"title": {"position": "top"}})
    assert_that(PluginManifest.from_dict(data).cards[0].appearance).is_equal_to({"title": {"position": "top"}})


def test_visible_false_hides_plugin_title() -> None:
    img = _open(compose_key_image(_appearance(title={"visible": False}), None, "Plugin"))
    assert_that(_has_title_pixels(img)).is_false()


def test_empty_text_without_plugin_title_draws_nothing() -> None:
    img = _open(compose_key_image(_appearance(), None, ""))
    assert_that(_has_title_pixels(img)).is_false()


def test_long_unicode_text_does_not_crash() -> None:
    data = compose_key_image(_appearance(title={"text": "W" * 200 + "🙂"}), None, "")
    assert_that(_open(data).size).is_equal_to((144, 144))


def test_unavailable_font_falls_back() -> None:
    img = _open(compose_key_image(_appearance(title={"text": "X", "font": "No Such Family 123"}), None, ""))
    assert_that(_has_title_pixels(img)).is_true()


def _manifest_data(appearance: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": 3,
        "id": "com.example.p",
        "name": "P",
        "version": "1.0.0",
        "author": "A",
        "description": "d",
        "decker": ">=0.7",
        "runtime": {"type": "python", "entry": "plugin.py", "python": ">=3.12", "requirements": []},
        "contributes": {"cards": [{"id": "com.example.p.a", "name": "A", "appearance": appearance}]},
    }


def _pixel(img: Image.Image, x: int, y: int) -> tuple[int, ...]:
    value = img.getpixel((x, y))
    assert isinstance(value, tuple)
    return value


def _brightest(img: Image.Image) -> int:
    return max(max(_pixel(img, x, y)) for x in range(144) for y in range(144))


def test_text_color_alpha_blends_with_background() -> None:
    solid = _open(compose_key_image(_appearance(title={"text": "WWWW", "position": "middle", "size": 40}), None, ""))
    half = _open(
        compose_key_image(
            _appearance(title={"text": "WWWW", "position": "middle", "size": 40, "color": "#ffffff80"}), None, ""
        )
    )
    assert_that(_brightest(solid)).is_equal_to(255)
    assert_that(_brightest(half)).is_between(100, 160)


def test_outline_alpha_blends_with_background() -> None:
    style = {"text": "WWWW", "position": "middle", "size": 40, "color": "#00000000", "outline_width": 3}
    solid = _open(compose_key_image(_appearance(title={**style, "outline": "#ff0000"}), None, ""))
    half = _open(compose_key_image(_appearance(title={**style, "outline": "#ff000080"}), None, ""))
    assert_that(_brightest(solid)).is_equal_to(255)
    assert_that(_brightest(half)).is_between(100, 160)


_STYLE: dict[str, Any] = {"text": "WWWW", "position": "middle", "size": 24}


def _styled(**overrides: Any) -> Image.Image:
    return _open(compose_key_image(_appearance(title={**_STYLE, **overrides}), None, ""))


def _lit(img: Image.Image) -> int:
    return sum(1 for x in range(144) for y in range(144) if img.getpixel((x, y)) != (0, 0, 0))


def _ink_bounds(img: Image.Image) -> tuple[int, int, int, int]:
    box = img.getbbox()
    assert box is not None
    return box


def _long_run_rows(img: Image.Image, min_run: int, color: tuple[int, int, int] = (255, 255, 255)) -> list[int]:
    rows = []
    for y in range(144):
        run = best = 0
        for x in range(144):
            run = run + 1 if img.getpixel((x, y)) == color else 0
            best = max(best, run)
        if best >= min_run:
            rows.append(y)
    return rows


def test_bold_differs_from_regular() -> None:
    regular, bold = _styled(), _styled(bold=True)
    assert_that(bold.tobytes()).is_not_equal_to(regular.tobytes())
    assert_that(_lit(bold)).is_greater_than(_lit(regular))


def test_italic_differs_from_regular() -> None:
    assert_that(_styled(italic=True).tobytes()).is_not_equal_to(_styled().tobytes())


def test_underline_adds_a_horizontal_run() -> None:
    left, _, right, _ = _ink_bounds(_styled())
    width = right - left
    assert_that(_long_run_rows(_styled(), int(width * 0.9))).is_empty()
    assert_that(_long_run_rows(_styled(underline=True), int(width * 0.9))).is_not_empty()


def test_strike_row_is_through_the_text_middle() -> None:
    plain = _styled()
    left, top, right, bottom = _ink_bounds(plain)
    rows = _long_run_rows(_styled(strike=True), int((right - left) * 0.9))
    assert_that(rows).is_not_empty()
    assert_that(rows[0]).is_greater_than(top)
    assert_that(rows[-1]).is_less_than(bottom)


def test_underline_sits_below_strike() -> None:
    width = _ink_bounds(_styled())[2] - _ink_bounds(_styled())[0]
    under = _long_run_rows(_styled(underline=True), int(width * 0.9))
    strike = _long_run_rows(_styled(strike=True), int(width * 0.9))
    assert_that(min(under)).is_greater_than(max(strike))


def test_underline_outlined_only_when_outline_set() -> None:
    width = _ink_bounds(_styled())[2] - _ink_bounds(_styled())[0]
    styled = _styled(underline=True, outline="#ff0000", outline_width=2)
    rows = _long_run_rows(styled, int(width * 0.9))
    assert_that(rows).is_not_empty()
    above, below = _pixel(styled, 72, rows[0] - 1), _pixel(styled, 72, rows[-1] + 1)
    assert_that(above[0] > 200 and above[1] < 50).is_true()
    assert_that(below[0] > 200 and below[1] < 50).is_true()
    plain = _styled(underline=True)
    assert_that(any(p[0] > 200 and p[1] < 50 for p in (_pixel(plain, 72, y) for y in range(144)))).is_false()


def test_outline_none_ignores_outline_width() -> None:
    assert_that(_styled(outline_width=1).tobytes()).is_equal_to(_styled(outline_width=5).tobytes())


def test_fake_bold_when_family_has_no_bold_face(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("decker.server.appearance.font_file", lambda *_a, **_k: (str(fonts.BUNDLED_REGULAR), False))
    regular, bold = _styled(), _styled(bold=True)
    assert_that(bold.tobytes()).is_not_equal_to(regular.tobytes())
    assert_that(_lit(bold)).is_greater_than(_lit(regular))


def test_all_styles_together_do_not_crash() -> None:
    img = _styled(bold=True, italic=True, underline=True, strike=True, outline="#ff000080", outline_width=3)
    assert_that(img.size).is_equal_to((144, 144))


def test_translucent_background_blends_over_black() -> None:
    img = _open(compose_key_image(_appearance(background="#ffffff80"), None, ""))
    assert_that(_pixel(img, 0, 0)).is_equal_to((128, 128, 128))


def test_fully_transparent_background_is_not_rendered() -> None:
    img = _open(compose_key_image(_appearance(background="#ff000000"), None, ""))
    assert_that(_pixel(img, 0, 0)).is_equal_to((0, 0, 0))


def test_transparent_background_still_renders_plugin_image_and_title() -> None:
    appearance = _appearance(background="#ff000000", title={"text": "WWWW", "position": "middle", "size": 24})
    img = _open(compose_key_image(appearance, _png("#00ff00"), ""))
    assert_that(_pixel(img, 5, 5)).is_equal_to((0, 255, 0))
    assert_that(_lit(img)).is_greater_than(0)


def test_translucent_background_sits_under_plugin_image() -> None:
    img = _open(compose_key_image(_appearance(background="#ffffff40"), _png("#0000ff"), ""))
    assert_that(_pixel(img, 72, 72)).is_equal_to((0, 0, 255))


def test_background_keeps_alpha_when_resolved() -> None:
    assert_that(resolve_appearance({}, {"background": "#11223380"})["background"]["color"]).is_equal_to("#11223380")


def test_invalid_background_falls_back_to_default() -> None:
    assert_that(resolve_appearance({}, {"background": "#1122"})["background"]["color"]).is_equal_to("#000000")


@pytest.mark.parametrize("position", ["top", "middle", "bottom"])
def test_newline_in_plugin_title_does_not_crash_and_renders_as_a_space(position: str) -> None:
    appearance = _appearance(title={"position": position})
    assert_that(compose_key_image(appearance, None, "a\nb")).is_equal_to(compose_key_image(appearance, None, "a b"))


@pytest.mark.parametrize("raw", ["a\r\nb", "a\rb", "a \n\n b", "a b"])
def test_any_line_break_in_title_renders_as_a_single_space(raw: str) -> None:
    appearance = _appearance(title={"position": "top"})
    assert_that(compose_key_image(appearance, None, raw)).is_equal_to(compose_key_image(appearance, None, "a b"))


def test_newline_in_user_title_does_not_crash() -> None:
    img = _open(compose_key_image(_appearance(title={"text": "a\nb", "position": "bottom"}), None, ""))
    assert_that(_has_title_pixels(img)).is_true()


def test_title_of_only_line_breaks_draws_nothing() -> None:
    img = _open(compose_key_image(_appearance(title={"text": "\n\n"}), None, ""))
    assert_that(_has_title_pixels(img)).is_false()


_ICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path fill="currentColor" d="M0 0h10v10H0z"/></svg>'
)


def test_plugin_icon_colour_tints_the_plugin_svg() -> None:
    img = _open(compose_key_image(_appearance(background="#0000ff"), _ICON_SVG, "", plugin_image_color="#ff0000"))
    assert_that(_pixel(img, 72, 72)).is_equal_to((255, 0, 0))


def test_plugin_icon_without_colour_draws_white() -> None:
    img = _open(compose_key_image(_appearance(background="#0000ff"), _ICON_SVG, ""))
    assert_that(_pixel(img, 72, 72)).is_equal_to((255, 255, 255))


def test_translucent_plugin_icon_colour_blends_with_background() -> None:
    img = _open(compose_key_image(_appearance(background="#000000"), _ICON_SVG, "", plugin_image_color="#ffffff80"))
    assert_that(_pixel(img, 72, 72)[0]).is_between(120, 135)


def test_plugin_image_scale_shrinks_and_centres_the_image() -> None:
    img = _open(
        compose_key_image(
            _appearance(background="#0000ff"), _ICON_SVG, "", plugin_image_color="#ff0000", plugin_image_scale=0.5
        )
    )
    assert_that(_pixel(img, 72, 72)).is_equal_to((255, 0, 0))  # centre: the icon
    assert_that(_pixel(img, 40, 40)).is_equal_to((255, 0, 0))  # inside the 72 px square at offset 36
    assert_that(_pixel(img, 20, 20)).is_equal_to((0, 0, 255))  # margin: background
    assert_that(_pixel(img, 130, 130)).is_equal_to((0, 0, 255))


def test_plugin_image_scale_defaults_to_full_bleed() -> None:
    img = _open(compose_key_image(_appearance(background="#0000ff"), _ICON_SVG, "", plugin_image_color="#ff0000"))
    assert_that(_pixel(img, 2, 2)).is_equal_to((255, 0, 0))


def test_plugin_image_scale_is_clamped_to_something_sensible() -> None:
    for bad in (0.0, -1.0, 5.0):
        img = _open(compose_key_image(_appearance(background="#0000ff"), _ICON_SVG, "", plugin_image_scale=bad))
        assert_that(img.size).is_equal_to((144, 144))


def test_appearance_title_opts_in_to_templates() -> None:
    from decker.common.appearance_schema import APPEARANCE_SCHEMA

    title = next(e for e in APPEARANCE_SCHEMA if e.get("key") == "title")
    assert_that(title["template"]).is_true()


def test_first_composed_frame_equals_static_composition() -> None:
    appearance = _appearance(background="#112233", title="T")
    static = _open(compose_key_image(appearance, _png("#ff0000", 144), ""))
    frame = Image.new("RGBA", (144, 144), "#ff0000")
    composed = compose_key_frames(appearance, [frame], "")[0]
    assert_that(composed.mode).is_equal_to("RGB")
    assert_that(composed.tobytes()).is_equal_to(static.tobytes())


def test_title_and_background_are_drawn_on_every_frame() -> None:
    appearance = _appearance(background="#0000ff", title="X")
    frames = [Image.new("RGBA", (144, 144), (0, 0, 0, 0)) for _ in range(2)]
    first, second = compose_key_frames(appearance, frames, "")
    plain = Image.new("RGB", (144, 144), (0, 0, 255)).tobytes()
    assert_that(first.getpixel((0, 0))).is_equal_to((0, 0, 255))
    assert_that(first.tobytes()).is_equal_to(second.tobytes())
    assert_that(first.tobytes()).is_not_equal_to(plain)


_TITLE_REGION = (32, 96, 112, 144)  # bottom-centre third of a 144 px key, where the default title sits
_TOLERANCE = 8


def _assert_title_drawn(img: Image.Image, background: tuple[int, int, int]) -> None:
    """Structure of a default white bottom title on a flat background; holds for any FreeType rasterisation."""
    assert_that(img.size).is_equal_to((144, 144))
    assert_that(img.mode).is_equal_to("RGB")
    solid = Image.new("RGB", img.size, background)
    box = ImageChops.difference(img, solid).getbbox()
    assert_that(box).described_as("the title is drawn").is_not_none()
    left, top, right, bottom = box  # type: ignore[misc]
    assert_that(left >= _TITLE_REGION[0] and top >= _TITLE_REGION[1]).is_true()
    assert_that(right <= _TITLE_REGION[2] and bottom <= _TITLE_REGION[3]).is_true()
    pixels: list[tuple[int, int, int]] = list(img.get_flattened_data())  # type: ignore[arg-type]
    changed = [px for px in pixels if px != background]
    for px in changed:  # anti-aliased pixels lie between the background and the white title colour
        for channel, base in zip(px, background):
            assert_that(channel).is_greater_than_or_equal_to(base - _TOLERANCE)
    assert_that(max(min(px) for px in changed)).described_as("some pixels carry the full title colour").is_greater_than(
        230
    )


def test_existing_compose_key_image_output_is_unchanged() -> None:
    appearance = _appearance(background="#112233", title="X")
    # Pixel hashes (not PNG bytes, which vary with the zlib/Pillow version), taken from the pre-refactor 399bca1 code.
    with_image = _open(compose_key_image(appearance, _png("#00ff00"), "Plugin"))
    without_image = _open(compose_key_image(appearance, None, "Plugin"))
    _assert_title_drawn(with_image, background=(0, 255, 0))
    _assert_title_drawn(without_image, background=(17, 34, 51))
    assert_that(with_image.getpixel((10, 10))).described_as("the plugin image fills the key").is_not_equal_to(
        without_image.getpixel((10, 10))
    )
    if sys.platform == "linux":  # glyph rasterisation differs between FreeType builds, so the hash is Linux-only
        assert_that(hashlib.sha256(with_image.tobytes()).hexdigest()).is_equal_to(
            "9e1380e915cb77e1fbf10f0ccab905bd4f226ab51394ccaf13b08d7578479557"
        )
        assert_that(hashlib.sha256(without_image.tobytes()).hexdigest()).is_equal_to(
            "f15b622974dc7fb55301c3c8fd4b3d7e86922e23037e538b2e76538000ebc2ca"
        )


def test_compose_key_face_of_an_animated_gif_carries_the_spec_that_produced_the_poster() -> None:
    appearance = _appearance(background="#112233", title="X")
    gif = make_gif(3)

    face = compose_key_face(appearance, gif, "P", "#ff0000", 0.7)

    expected = compose_key_image(appearance, gif, "P", plugin_image_color="#ff0000", plugin_image_scale=0.7)
    assert_that(face.png).is_equal_to(expected)
    assert_that(face.animation).is_equal_to(AnimationSpec(gif, appearance, "P", 0.7))


@pytest.mark.parametrize(
    "image",
    [
        None,
        _png("#00ff00"),
        make_gif(1),
        b"GIF89a" + b"\x00\xff" * 40,
        b"GIF89a",
        make_gif(MAX_GIF_FRAMES + 1),
        grown_gif((2, 2), [(2, 2)] + [(8000, 8000)] * 3),
    ],
    ids=["none", "png", "single-frame-gif", "corrupt-gif", "header-only-gif", "over-frame-cap-gif", "grown-canvas-gif"],
)
def test_compose_key_face_of_anything_but_an_animated_gif_is_static(image: bytes | None) -> None:
    appearance = _appearance(background="#112233")

    face = compose_key_face(appearance, image, "")

    assert_that(face.png).is_equal_to(compose_key_image(appearance, image, ""))
    assert_that(face.animation).is_none()


def test_compose_key_face_of_gif_bytes_over_the_icon_size_cap_is_static(monkeypatch: pytest.MonkeyPatch) -> None:
    gif = make_gif(3)
    monkeypatch.setattr(gif_module, "max_icon_bytes", lambda: len(gif) - 1)

    face = compose_key_face(_appearance(), gif, "")

    assert_that(face.animation).is_none()


def test_compose_key_face_without_a_poster_skips_the_png_but_still_finds_the_animation() -> None:
    appearance = _appearance(background="#112233")
    gif = make_gif(3)

    face = compose_key_face(appearance, gif, "", poster=False)

    assert_that(face.png).is_empty()
    assert_that(face.animation).is_equal_to(AnimationSpec(gif, appearance, "", 1.0))
    assert_that(compose_key_face(appearance, _png("#00ff00"), "", poster=False).animation).is_none()


_BLACK_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M0 0h24v24H0z"/></svg>'


def test_unstyled_svg_icon_draws_white() -> None:
    img = _open(compose_key_image(_appearance(background="#000000"), _BLACK_SVG, ""))
    assert_that(img.getpixel((72, 72))).is_equal_to((255, 255, 255))


def test_explicit_icon_colour_beats_white_default() -> None:
    img = _open(compose_key_image(_appearance(background="#000000"), _BLACK_SVG, "", plugin_image_color="#ff0000"))
    assert_that(img.getpixel((72, 72))).is_equal_to((255, 0, 0))


def test_styled_svg_icon_keeps_its_colour() -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#00ff00" d="M0 0h24v24H0z"/></svg>'
    img = _open(compose_key_image(_appearance(background="#000000"), svg, ""))
    assert_that(img.getpixel((72, 72))).is_equal_to((0, 255, 0))


def test_png_icon_is_not_whitened() -> None:
    img = _open(compose_key_image(_appearance(background="#000000"), _png("#ff0000"), ""))
    assert_that(img.getpixel((72, 72))).is_equal_to((255, 0, 0))
