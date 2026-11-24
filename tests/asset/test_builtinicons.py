from pathlib import Path

from assertpy import assert_that

from decker.asset.builtinicons import BuiltinIcons, read_builtin_ref

_SOLID = "decker.builtin.fontawesome.solid"


def test_three_packs_with_exact_ids_names_and_counts() -> None:
    packs = BuiltinIcons().list_packs()

    assert_that([(p["id"], p["name"], p["icon_count"]) for p in packs]).is_equal_to(
        [
            ("decker.builtin.fontawesome.brands", "FontAwesome Brands", 609),
            ("decker.builtin.fontawesome.regular", "FontAwesome Regular", 273),
            (_SOLID, "FontAwesome Solids", 2001),
        ]
    )
    assert_that({p["plugin_id"] for p in packs}).is_equal_to({"decker.builtin"})
    assert_that({p["plugin_name"] for p in packs}).is_equal_to({"Built-In"})
    assert_that({p["plugin_version"] for p in packs}).is_equal_to({"7.3.1"})
    assert_that([p["description"] for p in packs]).is_equal_to(
        ["Font Awesome Free 7.3.1: Brands", "Font Awesome Free 7.3.1: Regular", "Font Awesome Free 7.3.1: Solid"]
    )


def test_list_icons_is_sorted_svg_names() -> None:
    names = BuiltinIcons().list_icons(_SOLID)

    assert_that(names).is_equal_to(sorted(names))
    assert_that(names).contains("play.svg")
    assert_that(BuiltinIcons().list_icons("com.x.y")).is_empty()


def test_read_icon_returns_the_svg_bytes() -> None:
    data = BuiltinIcons().read_icon(_SOLID, "play.svg")

    assert_that(data).is_not_none()
    assert data is not None
    assert_that(data).contains(b"<svg")
    assert_that(read_builtin_ref("fas:play")).is_equal_to(data)
    assert_that(read_builtin_ref("pack:x/y.svg")).is_none()


def test_traversal_non_svg_and_unknown_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "fa"
    (root / "solid").mkdir(parents=True)
    (root / "solid" / "a.svg").write_text("<svg/>")
    (root / "solid" / "b.png").write_bytes(b"png")
    (root / "secret.svg").write_text("<svg/>")
    icons = BuiltinIcons(root)

    assert_that(icons.read_icon(_SOLID, "a.svg")).is_equal_to(b"<svg/>")
    assert_that(icons.read_icon(_SOLID, "../secret.svg")).is_none()
    assert_that(icons.read_icon(_SOLID, "b.png")).is_none()
    assert_that(icons.read_icon(_SOLID, "missing.svg")).is_none()
    assert_that(icons.read_icon(_SOLID, None)).is_none()
    assert_that(icons.read_icon("com.x.y", "a.svg")).is_none()
