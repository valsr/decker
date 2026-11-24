"""Tests for decker.client.widgets.icon_edit."""

# pylint: disable=protected-access,unused-argument
import base64
import json
from typing import Any

from assertpy import assert_that
from PyQt6.QtCore import QObject, pyqtSignal

from decker.client.icon_cache import IconThumbnailCache
from decker.client.icon_picker import NO_TINT
from decker.client.widgets.icon_edit import IconEdit

_CURRENT = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path fill="currentColor" d="M0 0h10v10H0z"/></svg>'
)
_REF = "pack:com.x.p/house.svg"


_PACKS = [{"id": "com.x.p", "name": "Pack", "plugin_id": "com.x", "plugin_version": "1.0.0"}]


class FakeRpc:
    def __init__(self, svg: bytes | None = _CURRENT, packs: list | None = None) -> None:
        self.svg = svg
        self.packs = packs
        self.calls: list[tuple[Any, ...]] = []

    def call(self, method: str, *args: Any) -> str | None:
        self.calls.append((method, *args))
        if method == "iconpack_icon" and self.svg is not None:
            return base64.b64encode(self.svg).decode()
        if method == "iconpacks_list" and self.packs is not None:
            return json.dumps(self.packs)
        return None

    def methods(self, name: str) -> list[tuple[Any, ...]]:
        return [c for c in self.calls if c[0] == name]


def _edit(rpc: FakeRpc | None = None, cache: IconThumbnailCache | None = None) -> IconEdit:
    fake = rpc if rpc is not None else FakeRpc()
    return IconEdit(lambda: fake, cache=cache)


def _centre(edit: IconEdit) -> tuple[int, int, int, int]:
    pixmap = edit._preview.pixmap()
    assert pixmap is not None and not pixmap.isNull()
    image = pixmap.toImage()
    pixel = image.pixelColor(image.width() // 2, image.height() // 2)
    return pixel.red(), pixel.green(), pixel.blue(), pixel.alpha()


class TestIconEdit:
    def test_value_defaults(self, qapp):
        assert_that(_edit().value()).is_equal_to({"ref": "", "color": "#ffffff"})

    def test_set_value_round_trips_and_emits_nothing(self, qapp):
        edit = _edit()
        emitted: list[None] = []
        edit.value_changed.connect(lambda: emitted.append(None))

        edit.set_value({"ref": _REF, "color": "#11223380"})

        assert_that(edit.value()).is_equal_to({"ref": _REF, "color": "#11223380"})
        assert_that(emitted).is_empty()

    def test_garbage_value_uses_defaults(self, qapp):
        edit = _edit()
        edit.set_value("garbage")
        assert_that(edit.value()).is_equal_to({"ref": "", "color": "#ffffff"})
        edit.set_value({"ref": "pack:x", "color": "red"})
        assert_that(edit.value()).is_equal_to({"ref": "", "color": "#ffffff"})

    def test_browse_choice_sets_ref_and_emits(self, qapp, monkeypatch):
        import decker.client.widgets.icon_edit as module

        class FakeDialog(QObject):
            icon_chosen = pyqtSignal(str, str)

            def exec(self) -> None:
                self.icon_chosen.emit("com.x.p", "house.svg")

        monkeypatch.setattr(module, "IconBrowserDialog", lambda *a, **k: FakeDialog())
        edit = _edit()
        emitted: list[None] = []
        edit.value_changed.connect(lambda: emitted.append(None))

        edit.browse()

        assert_that(edit.value()["ref"]).is_equal_to(_REF)
        assert_that(emitted).is_length(1)

    def test_browse_leaves_thumbnail_tint_to_the_theme_not_the_fill_colour(self, qapp, monkeypatch):
        import decker.client.widgets.icon_edit as module

        seen: dict[str, Any] = {}

        class FakeDialog(QObject):
            icon_chosen = pyqtSignal(str, str)

            def __init__(self, _parent: Any, _rpc: Any, tint: str | None = None) -> None:
                super().__init__()
                seen["tint"] = tint

            def exec(self) -> None:
                pass

        monkeypatch.setattr(module, "IconBrowserDialog", FakeDialog)
        edit = _edit()
        edit.set_value({"color": "#000000"})  # a black fill must not make the thumbnails vanish on a dark theme

        edit.browse()

        assert_that(seen["tint"]).is_none()

    def test_clear_resets_ref_keeps_colour_and_emits(self, qapp):
        edit = _edit()
        edit.set_value({"ref": _REF, "color": "#ff0000"})
        emitted: list[None] = []
        edit.value_changed.connect(lambda: emitted.append(None))

        edit._clear.click()

        assert_that(edit.value()).is_equal_to({"ref": "", "color": "#ff0000"})
        assert_that(emitted).is_length(1)

    def test_clear_is_disabled_without_an_icon(self, qapp):
        edit = _edit()
        assert_that(edit._clear.isEnabled()).is_false()
        edit.set_value({"ref": _REF})
        assert_that(edit._clear.isEnabled()).is_true()

    def test_colour_change_emits_and_updates_value(self, qapp):
        edit = _edit()
        emitted: list[None] = []
        edit.value_changed.connect(lambda: emitted.append(None))

        edit._colour.color_changed.emit("#00ff00")
        edit._colour.set_color("#00ff00")

        assert_that(edit.value()["color"]).is_equal_to("#00ff00")
        assert_that(emitted).is_length(1)

    def test_colour_swatch_shows_no_hex_name(self, qapp):
        edit = _edit()
        assert_that(edit._colour.text()).is_equal_to("")

    def test_name_label_shows_stem_or_no_icon(self, qapp):
        edit = _edit()
        assert_that(edit._name.text()).is_equal_to("No icon")
        edit.set_value({"ref": "pack:com.x.p/sub/house-alt.svg"})
        assert_that(edit._name.text()).is_equal_to("house-alt")

    def test_name_label_shows_a_dotted_system_icon_name_whole(self, qapp):
        edit = _edit()
        edit.set_value({"ref": "pack:decker.system.mimetypes/org.gnome.Foo"})
        assert_that(edit._name.text()).is_equal_to("org.gnome.Foo")

    def test_preview_uses_fill_colour(self, qapp):
        edit = _edit()

        edit.set_value({"ref": _REF, "color": "#ff0000"})

        assert_that(_centre(edit)).is_equal_to((255, 0, 0, 255))

    def test_colour_change_retints_the_preview_without_another_fetch(self, qapp):
        rpc = FakeRpc()
        edit = _edit(rpc)
        edit.set_value({"ref": _REF, "color": "#ff0000"})
        fetches = len(rpc.calls)

        edit._colour.set_color("#0000ff")
        edit._on_colour_changed("#0000ff")

        assert_that(_centre(edit)).is_equal_to((0, 0, 255, 255))
        assert_that(rpc.calls).is_length(fetches)

    def test_missing_icon_shows_placeholder_and_keeps_name(self, qapp):
        edit = _edit(FakeRpc(svg=None))

        edit.set_value({"ref": _REF})

        assert_that(edit._name.text()).is_equal_to("house")
        assert_that(edit._preview.pixmap().isNull()).is_false()
        assert_that(edit.value()["ref"]).is_equal_to(_REF)

    def test_no_connection_does_not_crash(self, qapp, monkeypatch):
        import decker.client.widgets.icon_edit as module

        class NoopDialog(QObject):
            icon_chosen = pyqtSignal(str, str)

            def __init__(self, *_a: Any, **_k: Any) -> None:
                super().__init__()

            def exec(self) -> None:
                pass

        monkeypatch.setattr(module, "IconBrowserDialog", NoopDialog)
        edit = IconEdit(lambda: None)

        edit.set_value({"ref": _REF})
        edit.browse()

        assert_that(edit._name.text()).is_equal_to("house")

    def test_default_rpc_resolves_the_app_context_lazily(self, qapp):
        from decker.client.context import AppContext

        rpc = FakeRpc()
        AppContext.get().connection_manager = rpc  # type: ignore[assignment]
        try:
            edit = IconEdit()
            edit.set_value({"ref": _REF})
            assert_that(rpc.calls).is_not_empty()
        finally:
            AppContext.get().connection_manager = None


class TestIconEditCache:
    def _cache(self, root, versioned: bool = True) -> IconThumbnailCache:
        cache = IconThumbnailCache(root)
        if versioned:
            cache.update_packs(_PACKS)
        return cache

    def test_cached_preview_makes_no_rpc_at_all(self, qapp, tmp_path):
        warm = self._cache(tmp_path / "c")
        first = _edit(FakeRpc(packs=_PACKS), warm)
        first.set_value({"ref": _REF, "color": "#ff0000"})
        rpc = FakeRpc(packs=_PACKS)

        second = _edit(rpc, warm)
        second.set_value({"ref": _REF, "color": "#ff0000"})

        assert_that(rpc.calls).is_empty()
        assert_that(_centre(second)).is_equal_to((255, 0, 0, 255))

    def test_first_preview_lists_packs_once_then_caches(self, qapp, tmp_path):
        rpc = FakeRpc(packs=_PACKS)
        edit = _edit(rpc, self._cache(tmp_path / "c", versioned=False))

        edit.set_value({"ref": _REF, "color": "#ff0000"})
        edit.set_value({"ref": "pack:com.x.p/other.svg", "color": "#ff0000"})

        assert_that(rpc.methods("iconpacks_list")).is_length(1)
        assert_that(rpc.methods("iconpack_icon")).is_length(2)
        assert_that(list((tmp_path / "c").rglob("*.png"))).is_length(2)

    def test_second_edit_in_a_new_process_hits_the_disk_cache(self, qapp, tmp_path):
        first = _edit(FakeRpc(packs=_PACKS), self._cache(tmp_path / "c", versioned=False))
        first.set_value({"ref": _REF, "color": "#ff0000"})
        rpc = FakeRpc(packs=_PACKS)

        second = _edit(rpc, IconThumbnailCache(tmp_path / "c"))  # knows no versions yet
        second.set_value({"ref": _REF, "color": "#ff0000"})

        assert_that(rpc.methods("iconpack_icon")).is_empty()
        assert_that(rpc.methods("iconpacks_list")).is_length(1)
        assert_that(_centre(second)).is_equal_to((255, 0, 0, 255))

    def test_colour_change_is_a_separate_cache_entry_without_refetching_the_icon(self, qapp, tmp_path):
        rpc = FakeRpc(packs=_PACKS)
        edit = _edit(rpc, self._cache(tmp_path / "c", versioned=False))
        edit.set_value({"ref": _REF, "color": "#ff0000"})

        edit._colour.set_color("#0000ff")
        edit._on_colour_changed("#0000ff")

        assert_that(rpc.methods("iconpack_icon")).is_length(1)
        assert_that(_centre(edit)).is_equal_to((0, 0, 255, 255))
        assert_that(list((tmp_path / "c").rglob("*.png"))).is_length(2)

    def test_older_server_without_plugin_version_still_previews_and_writes_nothing(self, qapp, tmp_path):
        legacy = [{k: v for k, v in pack.items() if k != "plugin_version"} for pack in _PACKS]
        edit = _edit(FakeRpc(packs=legacy), self._cache(tmp_path / "c", versioned=False))

        edit.set_value({"ref": _REF, "color": "#ff0000"})

        assert_that(_centre(edit)).is_equal_to((255, 0, 0, 255))
        assert_that(list((tmp_path / "c").rglob("*"))).is_empty()

    def test_failed_pack_list_does_not_break_the_preview(self, qapp, tmp_path):
        edit = _edit(FakeRpc(packs=None), self._cache(tmp_path / "c", versioned=False))  # iconpacks_list -> None

        edit.set_value({"ref": _REF, "color": "#ff0000"})

        assert_that(_centre(edit)).is_equal_to((255, 0, 0, 255))
        assert_that(list((tmp_path / "c").rglob("*.png"))).is_empty()

    def test_unreadable_icon_is_not_cached(self, qapp, tmp_path):
        edit = _edit(FakeRpc(svg=None, packs=_PACKS), self._cache(tmp_path / "c", versioned=False))

        edit.set_value({"ref": _REF, "color": "#ff0000"})

        assert_that(edit._name.text()).is_equal_to("house")
        assert_that(list((tmp_path / "c").rglob("*.png"))).is_empty()

    def test_cache_failure_does_not_break_the_preview(self, qapp, tmp_path):
        class Broken(IconThumbnailCache):
            def get(self, *args, **kwargs):
                raise OSError("disk on fire")

            def put(self, *args, **kwargs):
                raise OSError("disk on fire")

            def update_packs(self, *args, **kwargs):
                raise OSError("disk on fire")

        edit = _edit(FakeRpc(packs=_PACKS), Broken(tmp_path / "c"))

        edit.set_value({"ref": _REF, "color": "#ff0000"})

        assert_that(_centre(edit)).is_equal_to((255, 0, 0, 255))

    def test_no_connection_does_not_list_packs_or_crash(self, qapp, tmp_path):
        edit = IconEdit(lambda: None, cache=self._cache(tmp_path / "c", versioned=False))

        edit.set_value({"ref": _REF, "color": "#ff0000"})

        assert_that(edit._name.text()).is_equal_to("house")


def test_clear_icon_is_public_and_emits_value_changed(qapp, tmp_path) -> None:
    edit = _edit(None, IconThumbnailCache(tmp_path))
    edit.set_value({"ref": _REF, "color": "#ffffff"})
    seen: list[bool] = []
    edit.value_changed.connect(lambda: seen.append(True))

    edit.clear_icon()

    assert_that(edit.value()["ref"]).is_equal_to("")
    assert_that(seen).is_equal_to([True])


def test_browse_is_public(qapp, monkeypatch, tmp_path) -> None:
    opened: list[bool] = []

    class FakeDialog:
        icon_chosen = type("S", (), {"connect": lambda *_a: None})()

        def __init__(self, *_a: Any) -> None:
            pass

        def exec(self) -> None:
            opened.append(True)

    monkeypatch.setattr("decker.client.widgets.icon_edit.IconBrowserDialog", FakeDialog)
    edit = _edit(None, IconThumbnailCache(tmp_path))

    edit.browse()

    assert_that(opened).is_equal_to([True])


class TestCompact:
    def test_compact_hides_the_name_and_browse_and_shows_colour_only_with_an_icon(self, qapp, tmp_path):
        edit = _edit(None, IconThumbnailCache(tmp_path))
        edit.show()
        edit.set_compact(True)

        assert_that(edit._name.isVisible()).is_false()
        assert_that(edit._browse_btn.isVisible()).is_false()
        assert_that(edit._colour.isVisible()).is_false()

        edit.set_value({"ref": _REF, "color": "#ffffff"})
        assert_that(edit._colour.isVisible()).is_true()

        edit.clear_icon()
        assert_that(edit._colour.isVisible()).is_false()

    def test_regular_mode_keeps_everything_visible(self, qapp, tmp_path):
        edit = _edit(None, IconThumbnailCache(tmp_path))
        edit.show()

        assert_that(edit._name.isVisible()).is_true()
        assert_that(edit._browse_btn.isVisible()).is_true()
        assert_that(edit._colour.isVisible()).is_true()

    def test_double_clicking_the_compact_thumbnail_opens_the_browser(self, qapp, tmp_path, monkeypatch):
        edit = _edit(None, IconThumbnailCache(tmp_path))
        edit.set_compact(True)
        opened: list[bool] = []
        monkeypatch.setattr(edit, "browse", lambda: opened.append(True))

        edit.mouseDoubleClickEvent(None)

        assert_that(opened).is_equal_to([True])


def test_colour_swatch_can_be_hidden(qapp):
    edit = IconEdit(rpc=lambda: None)
    edit.set_value({"ref": "pack:a/b.svg", "color": "#ffffff"})
    assert_that(edit._colour.isVisibleTo(edit)).is_true()

    edit.set_show_colour(False)

    assert_that(edit._colour.isVisibleTo(edit)).is_false()


def test_thumbnail_is_the_preview_pixmap(qapp):
    edit = IconEdit(rpc=lambda: None)

    assert_that(edit.thumbnail().isNull()).is_false()


class TestUntintedThumbnail:
    def test_untinted_thumbnail_ignores_the_fill_colour(self, qapp, tmp_path):
        tinted = _edit(None, IconThumbnailCache(tmp_path / "a"))
        untinted = _edit(None, IconThumbnailCache(tmp_path / "b"))
        untinted.set_tint_thumbnail(False)

        tinted.set_value({"ref": _REF, "color": "#ff0000"})
        untinted.set_value({"ref": _REF, "color": "#ff0000"})

        assert_that(_centre(tinted)).is_equal_to((255, 0, 0, 255))
        assert_that(_centre(untinted)[:3]).is_not_equal_to((255, 0, 0))
        assert_that(_centre(untinted)[3]).is_equal_to(255)

    def test_untinted_cache_entry_does_not_depend_on_the_colour(self, qapp, tmp_path):
        cache = IconThumbnailCache(tmp_path)
        edit = _edit(FakeRpc(packs=_PACKS), cache)
        edit.set_tint_thumbnail(False)
        edit.set_value({"ref": _REF, "color": "#ff0000"})

        assert_that(cache.get("com.x.p", "house.svg", 24, None)).is_not_none()
        assert_that(cache.get("com.x.p", "house.svg", 24, "#ff0000")).is_none()


def _browse_tint(edit: IconEdit, monkeypatch) -> list[Any]:
    seen: list[Any] = []

    class FakeDialog:
        icon_chosen = type("S", (), {"connect": lambda *_a: None})()

        def __init__(self, *args: Any) -> None:
            seen.append(args[2] if len(args) > 2 else None)

        def exec(self) -> None:
            pass

    monkeypatch.setattr("decker.client.widgets.icon_edit.IconBrowserDialog", FakeDialog)
    edit.browse()
    return seen


def test_browser_tint_follows_the_thumbnail_option(qapp, monkeypatch, tmp_path) -> None:
    edit = _edit(None, IconThumbnailCache(tmp_path))
    assert_that(_browse_tint(edit, monkeypatch)).is_equal_to([None])

    edit.set_tint_thumbnail(False)

    assert_that(_browse_tint(edit, monkeypatch)).is_equal_to([NO_TINT])


class _UserRpc(FakeRpc):
    def call(self, method: str, *args: Any) -> str | None:
        if method == "iconpack_icons" and args == ("decker.user",):
            self.calls.append((method, *args))
            return json.dumps(["logo.svg", "photo.png"])
        return super().call(method, *args)


class TestUserReferences:
    def test_choosing_a_user_image_stores_the_short_reference(self, qapp, tmp_path):
        edit = _edit(_UserRpc(), IconThumbnailCache(tmp_path))

        edit._on_icon_chosen("decker.user", "logo.svg")

        assert_that(edit.value()["ref"]).is_equal_to("user:logo")
        assert_that(edit._name.text()).is_equal_to("logo")

    def test_a_user_svg_takes_a_fill_colour_and_a_user_png_does_not(self, qapp, tmp_path):
        edit = _edit(_UserRpc(), IconThumbnailCache(tmp_path))
        edit.set_toolbar(True)

        edit.set_value({"ref": "user:logo", "color": "#ffffff"})
        assert_that(edit._colour.isVisibleTo(edit)).is_true()
        edit.set_value({"ref": "user:photo", "color": "#ffffff"})
        assert_that(edit._colour.isVisibleTo(edit)).is_false()

    def test_the_user_listing_is_asked_once(self, qapp, tmp_path):
        rpc = _UserRpc()
        edit = _edit(rpc, IconThumbnailCache(tmp_path))

        edit.set_value({"ref": "user:logo", "color": "#ffffff"})
        edit.set_value({"ref": "user:photo", "color": "#ffffff"})

        assert_that(rpc.methods("iconpack_icons")).is_length(1)
