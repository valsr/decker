import base64
import json
import time
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from assertpy import assert_that
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtWidgets import QApplication, QMessageBox, QStyle, QStyleOptionViewItem

from decker.client.card_icons import decode_icon
from decker.client.icon_cache import IconThumbnailCache
from decker.client.icon_picker import (
    MAX_IMPORT_BYTES,
    NO_TINT,
    IconBrowserDialog,
    IconPickerWidget,
    _kind_badge,
)


class _Allowed:
    def call(self, method, *_args):
        return True if method == "plugin_install_allowed" else None


class TestIconPickerWidget:
    def test_choose_file_opens_the_import_dialog_and_emits_the_pack_icon(
        self, qapp, tmp_path, monkeypatch
    ):  # pylint: disable=unused-argument
        import decker.client.icon_picker as module  # pylint: disable=import-outside-toplevel

        source = tmp_path / "icon.png"
        source.write_bytes(b"x")
        monkeypatch.setattr(module.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(source), "")))
        opened: list[Path] = []

        class FakeImport(QObject):
            imported = pyqtSignal(str)

            def __init__(self, _parent, _cm, name, _data):
                super().__init__()
                opened.append(Path(name))

            def exec(self):
                self.imported.emit("icon.png")

        class Allowed:
            def call(self, method, *_args):
                return True if method == "plugin_install_allowed" else None

        monkeypatch.setattr(module, "IconImportDialog", FakeImport)
        widget = IconPickerWidget(None, Allowed())
        received: list[tuple[str, str]] = []
        widget.pack_icon_selected.connect(lambda p, n: received.append((p, n)))

        widget._choose_file()  # pylint: disable=protected-access

        assert_that(opened).is_equal_to([Path("icon.png")])
        assert_that(received).is_equal_to([("decker.user", "icon.png")])

    def _picker_with_dialog(self, monkeypatch, source: Path):
        import decker.client.icon_picker as module  # pylint: disable=import-outside-toplevel

        monkeypatch.setattr(module.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(source), "")))
        warnings: list[str] = []
        monkeypatch.setattr(module.QMessageBox, "warning", staticmethod(lambda _p, _t, text: warnings.append(text)))
        opened: list[tuple[str, bytes]] = []

        class FakeImport(QObject):
            imported = pyqtSignal(str)

            def __init__(self, _parent, _cm, name, data):
                super().__init__()
                opened.append((name, data))

            def exec(self):
                return 0

        monkeypatch.setattr(module, "IconImportDialog", FakeImport)
        return IconPickerWidget(None, _Allowed()), warnings, opened

    def test_choose_file_warns_and_skips_the_dialog_for_an_oversize_file(
        self, qapp, tmp_path, monkeypatch
    ):  # pylint: disable=unused-argument
        source = tmp_path / "big.png"
        source.write_bytes(b"x" * (MAX_IMPORT_BYTES + 1))
        widget, warnings, opened = self._picker_with_dialog(monkeypatch, source)

        widget._choose_file()  # pylint: disable=protected-access

        assert_that(warnings).is_length(1)
        assert_that(warnings[0]).contains("4 MiB")
        assert_that(opened).is_empty()

    def test_choose_file_opens_the_dialog_for_a_file_of_exactly_the_limit(
        self, qapp, tmp_path, monkeypatch
    ):  # pylint: disable=unused-argument
        source = tmp_path / "edge.png"
        source.write_bytes(b"x" * MAX_IMPORT_BYTES)
        widget, warnings, opened = self._picker_with_dialog(monkeypatch, source)

        widget._choose_file()  # pylint: disable=protected-access

        assert_that(warnings).is_empty()
        assert_that(opened).is_length(1)

    def test_choose_file_refuses_a_non_regular_file(
        self, qapp, tmp_path, monkeypatch
    ):  # pylint: disable=unused-argument
        for target in (Path("/dev/null"), tmp_path):
            widget, warnings, opened = self._picker_with_dialog(monkeypatch, target)

            widget._choose_file()  # pylint: disable=protected-access

            assert_that(warnings).is_length(1)
            assert_that(opened).is_empty()

    def test_choose_file_warns_instead_of_raising_for_an_unreadable_file(
        self, qapp, tmp_path, monkeypatch
    ):  # pylint: disable=unused-argument
        widget, warnings, opened = self._picker_with_dialog(monkeypatch, tmp_path / "gone.png")

        widget._choose_file()  # pylint: disable=protected-access

        assert_that(warnings).is_length(1)
        assert_that(warnings[0]).contains("gone.png")
        assert_that(opened).is_empty()

    def test_choose_file_passes_the_bytes_of_a_normal_file_to_the_dialog(
        self, qapp, tmp_path, monkeypatch
    ):  # pylint: disable=unused-argument
        source = tmp_path / "ok.png"
        source.write_bytes(b"abc")
        widget, warnings, opened = self._picker_with_dialog(monkeypatch, source)

        widget._choose_file()  # pylint: disable=protected-access

        assert_that(warnings).is_empty()
        assert_that(opened).is_equal_to([("ok.png", b"abc")])

    def test_choose_file_explains_when_import_is_not_allowed(
        self, qapp, tmp_path, monkeypatch
    ):  # pylint: disable=unused-argument
        import decker.client.icon_picker as module  # pylint: disable=import-outside-toplevel

        shown: list[str] = []
        monkeypatch.setattr(module.QMessageBox, "information", staticmethod(lambda _p, _t, text: shown.append(text)))

        def fail(*_a, **_k):
            raise AssertionError("file dialog opened")

        monkeypatch.setattr(module.QFileDialog, "getOpenFileName", staticmethod(fail))

        class Remote:
            def call(self, method, *_args):
                return False if method == "plugin_install_allowed" else None

        IconPickerWidget(None, Remote())._choose_file()  # pylint: disable=protected-access

        assert_that(shown).is_length(1)
        assert_that(shown[0]).contains("local connection")

    def test_choose_file_treats_a_failing_allowed_check_as_not_allowed(
        self, qapp, monkeypatch
    ):  # pylint: disable=unused-argument
        import decker.client.icon_picker as module  # pylint: disable=import-outside-toplevel

        shown: list[str] = []
        monkeypatch.setattr(module.QMessageBox, "information", staticmethod(lambda _p, _t, text: shown.append(text)))

        def fail(*_a, **_k):
            raise AssertionError("file dialog opened")

        monkeypatch.setattr(module.QFileDialog, "getOpenFileName", staticmethod(fail))

        class Broken:
            def call(self, *_args):
                raise ConnectionError("gone")

        IconPickerWidget(None, Broken())._choose_file()  # pylint: disable=protected-access

        assert_that(shown).is_length(1)
        assert_that(shown[0]).contains("local connection")

    def test_choose_file_emits_nothing_when_dialog_cancelled(
        self, qapp, monkeypatch
    ):  # pylint: disable=unused-argument
        import decker.client.icon_picker as module  # pylint: disable=import-outside-toplevel

        monkeypatch.setattr(module.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: ("", "")))
        widget = IconPickerWidget(None, _Allowed())
        received: list[str] = []
        widget.icon_selected.connect(received.append)

        widget._choose_file()  # pylint: disable=protected-access

        assert_that(received).is_empty()

    def test_browse_icon_packs_forwards_choice(self, qapp, monkeypatch):  # pylint: disable=unused-argument
        import decker.client.icon_picker as module  # pylint: disable=import-outside-toplevel

        class FakeDialog(QObject):
            icon_chosen = pyqtSignal(str, str)

            def exec(self):
                self.icon_chosen.emit("p", "a.svg")

        monkeypatch.setattr(module, "IconBrowserDialog", lambda *a, **k: FakeDialog())
        widget = IconPickerWidget()
        received: list[tuple[str, str]] = []
        widget.pack_icon_selected.connect(lambda p, n: received.append((p, n)))

        widget._browse_icon_packs()  # pylint: disable=protected-access

        assert_that(received).is_equal_to([("p", "a.svg")])


_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" height="8" fill="red"/></svg>'
_CURRENT = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path fill="currentColor" d="M0 0h10v10H0z"/></svg>'
)

_PACKS = [
    {
        "id": "com.x.p1",
        "plugin_version": "1.0.0",
        "name": "Pack A",
        "description": "",
        "plugin_id": "com.x",
        "plugin_name": "X Plugin",
    },
    {
        "id": "com.x.p2",
        "plugin_version": "1.0.0",
        "name": "Pack B",
        "description": "",
        "plugin_id": "com.x",
        "plugin_name": "X Plugin",
    },
    {
        "id": "com.y.p",
        "plugin_version": "1.0.0",
        "name": "Pack C",
        "description": "",
        "plugin_id": "com.y",
        "plugin_name": "Y Plugin",
    },
]
_ICONS = {
    "com.x.p1": ["a.svg", "b.svg"],
    "com.x.p2": ["house.svg", "home-alt.svg"],
    "com.y.p": ["Star.svg"],
}


class FakeConnectionManager:
    def __init__(self, packs=None, icons=None, svg=_SVG):
        self.packs = packs if packs is not None else []
        self.icons = icons or {}
        self.svg = svg
        self.calls: list[tuple] = []

    def call(self, method, *args):
        self.calls.append((method, *args))
        if method == "iconpacks_list":
            return json.dumps(self.packs)
        if method == "iconpack_icons":
            return json.dumps(self.icons.get(args[0], []))
        if method == "iconpack_icon":
            return base64.b64encode(self.svg).decode()
        return None

    def icon_calls(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "iconpack_icon"]


def _drain(qapp):
    for _ in range(10):
        qapp.processEvents()


def _wait(qapp, ms=250):
    deadline = time.monotonic() + ms / 1000
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)


def _wait_until(qapp: QApplication, condition: Callable[[], object], timeout_ms: int = 5000) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline and not condition():
        qapp.processEvents()
        time.sleep(0.005)


def _manager(**kwargs):
    return FakeConnectionManager(_PACKS, _ICONS, **kwargs)


def _top(dialog):  # pylint: disable=protected-access
    tree = dialog._tree
    return [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]


def _select(dialog, *path):  # pylint: disable=protected-access
    item = dialog._tree.topLevelItem(path[0])
    for index in path[1:]:
        item = item.child(index)
    dialog._tree.setCurrentItem(item)
    return item


def _names(dialog):  # pylint: disable=protected-access
    model = dialog._model
    return [model.index(r, 0).data(Qt.ItemDataRole.DisplayRole) for r in range(model.rowCount())]


def _request(dialog, row):  # pylint: disable=protected-access
    """What painting a row does: ask the model for its decoration."""
    model = dialog._model
    return model.index(row, 0).data(Qt.ItemDataRole.DecorationRole)


class TestIconBrowserTree:
    def test_tree_groups_packs_under_plugin_names(self, qapp):  # pylint: disable=unused-argument
        dialog = IconBrowserDialog(None, _manager())

        top = _top(dialog)

        assert_that([i.text(0) for i in top]).is_equal_to(["All Images", "X Plugin", "Y Plugin"])
        assert_that([top[1].child(i).text(0) for i in range(top[1].childCount())]).is_equal_to(["Pack A", "Pack B"])
        assert_that(top[2].childCount()).is_equal_to(1)

    def test_selecting_a_pack_lists_its_icons(self, qapp):  # pylint: disable=unused-argument
        dialog = IconBrowserDialog(None, _manager())

        _select(dialog, 1, 0)

        assert_that(_names(dialog)).is_equal_to(["a", "b"])

    def test_selecting_a_plugin_lists_all_its_packs(self, qapp):  # pylint: disable=unused-argument
        dialog = IconBrowserDialog(None, _manager())

        _select(dialog, 1)

        assert_that(_names(dialog)).is_equal_to(["a", "b", "house", "home-alt"])

    def test_all_icons_node_lists_every_pack(self, qapp):  # pylint: disable=unused-argument
        dialog = IconBrowserDialog(None, _manager())

        _select(dialog, 0)

        assert_that(_names(dialog)).is_equal_to(["a", "b", "house", "home-alt", "Star"])

    def test_all_icons_is_selected_initially(self, qapp):  # pylint: disable=unused-argument
        dialog = IconBrowserDialog(None, _manager())

        current = dialog._tree.currentItem()  # pylint: disable=protected-access
        assert current is not None
        assert_that(current.text(0)).is_equal_to("All Images")
        assert_that(_names(dialog)).is_length(5)

    def test_empty_pack_shows_message(self, qapp):  # pylint: disable=unused-argument
        dialog = IconBrowserDialog(None, FakeConnectionManager(_PACKS, {}))

        _select(dialog, 1, 0)

        assert_that(dialog._status.text()).contains("no images")  # pylint: disable=protected-access


class TestIconBrowserSearch:
    def test_search_filters_by_name_case_insensitively(self, qapp):
        dialog = IconBrowserDialog(None, _manager())

        dialog._search.setText("HOUSE")  # pylint: disable=protected-access
        _wait(qapp)

        assert_that(_names(dialog)).is_equal_to(["house"])
        dialog._search.setText("star")  # pylint: disable=protected-access
        _wait(qapp)
        assert_that(_names(dialog)).is_equal_to(["Star"])

    def test_search_applies_within_the_selected_pack(self, qapp):
        dialog = IconBrowserDialog(None, _manager())
        _select(dialog, 1, 0)

        dialog._search.setText("a")  # pylint: disable=protected-access
        _wait(qapp)

        assert_that(_names(dialog)).is_equal_to(["a"])

    def test_search_no_match_shows_message(self, qapp):
        dialog = IconBrowserDialog(None, _manager())

        dialog._search.setText("zzz")  # pylint: disable=protected-access
        _wait(qapp)

        assert_that(_names(dialog)).is_empty()
        assert_that(dialog._status.text()).is_equal_to("No images match.")  # pylint: disable=protected-access

    def test_empty_search_restores_everything(self, qapp):
        dialog = IconBrowserDialog(None, _manager())
        dialog._search.setText("zzz")  # pylint: disable=protected-access
        _wait(qapp)

        dialog._search.setText("")  # pylint: disable=protected-access
        _wait(qapp)

        assert_that(_names(dialog)).is_length(5)
        assert_that(dialog._status.text()).is_equal_to("")  # pylint: disable=protected-access

    def test_search_survives_changing_selection(self, qapp):
        dialog = IconBrowserDialog(None, _manager())
        dialog._search.setText("a")  # pylint: disable=protected-access
        _wait(qapp)

        _select(dialog, 2, 0)

        assert_that(_names(dialog)).is_equal_to(["Star"])


class TestIconBrowserLoading:
    def test_nothing_loads_until_an_icon_is_requested(self, qapp):
        cm = _manager()
        IconBrowserDialog(None, cm)

        _drain(qapp)

        assert_that(cm.icon_calls()).is_empty()

    def test_requested_icons_load_without_blocking(self, qapp):
        cm = _manager()
        dialog = IconBrowserDialog(None, cm)

        _request(dialog, 0)
        _request(dialog, 1)

        assert_that(cm.icon_calls()).is_empty()
        _drain(qapp)
        assert_that(cm.icon_calls()).is_length(2)

    def test_loaded_icons_replace_placeholders(self, qapp):
        dialog = IconBrowserDialog(None, _manager())
        before = _request(dialog, 0).toImage()

        _drain(qapp)

        assert_that(_request(dialog, 0).toImage()).is_not_equal_to(before)

    def test_icons_are_cached_per_dialog(self, qapp):
        cm = _manager()
        dialog = IconBrowserDialog(None, cm)
        _request(dialog, 0)
        _drain(qapp)

        _select(dialog, 1, 0)
        _request(dialog, 0)
        _drain(qapp)

        assert_that(cm.icon_calls()).is_length(1)

    def test_changing_selection_cancels_pending_loads(self, qapp):
        cm = _manager()
        dialog = IconBrowserDialog(None, cm)
        _request(dialog, 0)

        _select(dialog, 2, 0)
        _drain(qapp)

        assert_that(cm.icon_calls()).is_empty()

    def test_typing_in_search_cancels_pending_loads(self, qapp):
        cm = _manager()
        dialog = IconBrowserDialog(None, cm)
        _request(dialog, 0)

        dialog._search.setText("zzz")  # pylint: disable=protected-access
        _drain(qapp)
        _wait(qapp)

        assert_that(cm.icon_calls()).is_empty()

    def test_closing_dialog_abandons_pending_loads(self, qapp):
        cm = _manager()
        dialog = IconBrowserDialog(None, cm)
        _request(dialog, 0)

        dialog.reject()
        _drain(qapp)

        assert_that(cm.icon_calls()).is_empty()

    def test_unreadable_icon_falls_back_to_placeholder(self, qapp):
        cm = _manager()
        cm.call = lambda m, *a: ("!!" if m == "iconpack_icon" else FakeConnectionManager.call(cm, m, *a))
        dialog = IconBrowserDialog(None, cm)

        _request(dialog, 0)
        _drain(qapp)

        assert_that(_request(dialog, 0).isNull()).is_false()

    def test_thumbnails_are_tinted(self, qapp):
        cm = _manager(svg=_CURRENT)
        dialog = IconBrowserDialog(None, cm, tint="#ff0000")

        _request(dialog, 0)
        _drain(qapp)

        image = _request(dialog, 0).toImage()
        pixel = image.pixelColor(image.width() // 2, image.height() // 2)
        assert_that((pixel.red(), pixel.green(), pixel.blue())).is_equal_to((255, 0, 0))

    def test_no_tint_shows_svg_thumbnails_as_drawn(self, qapp):
        cm = _manager(svg=_CURRENT)
        dialog = IconBrowserDialog(None, cm, tint=NO_TINT)
        tinted = IconBrowserDialog(None, _manager(svg=_CURRENT))

        _request(dialog, 0)
        _request(tinted, 0)
        _drain(qapp)

        assert_that(dialog._tint).is_none()  # pylint: disable=protected-access
        assert_that(tinted._tint).is_not_none()  # pylint: disable=protected-access
        expected = decode_icon(base64.b64encode(_CURRENT).decode())
        assert expected is not None
        want = expected.toImage().pixelColor(expected.width() // 2, expected.height() // 2)
        image = _request(dialog, 0).toImage()
        pixel = image.pixelColor(image.width() // 2, image.height() // 2)
        assert_that((pixel.red(), pixel.green(), pixel.blue())).is_equal_to((want.red(), want.green(), want.blue()))

    def test_large_pack_does_not_load_every_icon_up_front(self, qapp):
        names = [f"icon-{i:04d}.svg" for i in range(2001)]
        cm = FakeConnectionManager(_PACKS[:1], {"com.x.p1": names})
        dialog = IconBrowserDialog(None, cm)
        dialog.resize(640, 480)
        dialog.show()

        _wait(qapp, 400)

        assert_that(cm.icon_calls()).is_not_empty()
        assert_that(len(cm.icon_calls())).is_less_than(400)
        dialog.reject()

    def test_visible_icons_load_before_off_screen_ones(self, qapp):
        names = [f"icon-{i:04d}.svg" for i in range(2001)]
        cm = FakeConnectionManager(_PACKS[:1], {"com.x.p1": names})
        dialog = IconBrowserDialog(None, cm)
        dialog.resize(640, 480)
        dialog.show()
        _wait_until(qapp, lambda: any(c[2] == "icon-0000.svg" for c in cm.icon_calls()))
        asked = {c[2] for c in cm.icon_calls()}
        assert_that(asked).contains("icon-0000.svg")
        assert_that(asked).does_not_contain("icon-1000.svg")  # layout may probe the last item; nothing mid-list

        dialog._icon_list.scrollToBottom()  # pylint: disable=protected-access
        _wait(qapp, 400)

        later = {c[2] for c in cm.icon_calls()}
        assert_that(any(name >= "icon-1900.svg" for name in later)).is_true()
        assert_that(later).does_not_contain("icon-1000.svg")
        assert_that(len(cm.icon_calls())).is_less_than(900)
        dialog.reject()


class TestIconBrowserChoice:
    def test_activating_an_icon_emits_pack_and_path_and_accepts(self, qapp):  # pylint: disable=unused-argument
        dialog = IconBrowserDialog(None, _manager())
        received: list[tuple[str, str]] = []
        dialog.icon_chosen.connect(lambda p, n: received.append((p, n)))
        _select(dialog, 1, 0)

        dialog._on_activated(dialog._model.index(1, 0))  # pylint: disable=protected-access

        assert_that(received).is_equal_to([("com.x.p1", "b.svg")])
        assert_that(dialog.result()).is_equal_to(1)

    def test_activating_in_all_icons_reports_the_icons_own_pack(self, qapp):  # pylint: disable=unused-argument
        dialog = IconBrowserDialog(None, _manager())
        received: list[tuple[str, str]] = []
        dialog.icon_chosen.connect(lambda p, n: received.append((p, n)))

        dialog._on_activated(dialog._model.index(4, 0))  # pylint: disable=protected-access

        assert_that(received).is_equal_to([("com.y.p", "Star.svg")])

    def test_works_without_local_pack_files(self, qapp, monkeypatch):  # pylint: disable=unused-argument
        def fail(*_a, **_k):
            raise AssertionError("local filesystem touched")

        monkeypatch.setattr(Path, "iterdir", fail)
        monkeypatch.setattr(Path, "is_dir", fail)

        dialog = IconBrowserDialog(None, _manager())

        assert_that(_names(dialog)).is_length(5)

    def test_shows_error_when_server_unreachable(self, qapp):  # pylint: disable=unused-argument
        class Down:
            def call(self, *_a):
                return None

        dialog = IconBrowserDialog(None, Down())

        assert_that(dialog._tree.topLevelItemCount()).is_zero()  # pylint: disable=protected-access
        assert_that(dialog._status.text()).contains("Couldn't load")  # pylint: disable=protected-access

    def test_shows_empty_state_without_packs(self, qapp):  # pylint: disable=unused-argument
        dialog = IconBrowserDialog(None, FakeConnectionManager([]))

        assert_that(dialog._status.text()).contains("image library is empty")  # pylint: disable=protected-access

    def test_works_without_a_connection_manager(self, qapp):  # pylint: disable=unused-argument
        dialog = IconBrowserDialog(None, None)

        assert_that(dialog._tree.topLevelItemCount()).is_zero()  # pylint: disable=protected-access


class TestIconBrowserTooltips:
    def test_tooltip_names_the_pack_so_duplicates_can_be_told_apart(self, qapp):  # pylint: disable=unused-argument
        dialog = IconBrowserDialog(None, _manager())

        tips = [
            dialog._model.index(r, 0).data(Qt.ItemDataRole.ToolTipRole)  # pylint: disable=protected-access
            for r in range(dialog._model.rowCount())  # pylint: disable=protected-access
        ]

        assert_that(tips[0]).is_equal_to("Pack A: a.svg")
        assert_that(tips[2]).is_equal_to("Pack B: house.svg")
        assert_that(tips[4]).is_equal_to("Pack C: Star.svg")


def _red(pixmap) -> bool:
    image = pixmap.toImage()
    colour = image.pixelColor(image.width() // 2, image.height() // 2)
    return colour.alpha() > 0 and colour.red() > 200 and colour.green() < 60


def _versioned(version: str):
    return [{**pack, "plugin_version": version} for pack in _PACKS]


class TestIconBrowserCache:
    def _dialog(self, root, cm=None, **kwargs):
        return IconBrowserDialog(None, cm or _manager(), cache=IconThumbnailCache(root), **kwargs)

    def test_second_dialog_shows_cached_thumbnails_without_any_icon_rpc(self, qapp, tmp_path):
        first = self._dialog(tmp_path / "c")
        _request(first, 0)
        _request(first, 1)
        _drain(qapp)
        cm = _manager()

        second = self._dialog(tmp_path / "c", cm)
        thumb = _request(second, 0)
        _drain(qapp)

        assert_that(_red(thumb)).described_as("served from the cache at once").is_true()
        assert_that(cm.icon_calls()).is_empty()

    def test_cache_hit_survives_a_new_process(self, qapp, tmp_path):
        first = self._dialog(tmp_path / "c")
        _request(first, 0)
        _drain(qapp)
        cm = _manager()

        fresh = IconBrowserDialog(None, cm, cache=IconThumbnailCache(tmp_path / "c"))
        _request(fresh, 0)
        _drain(qapp)

        assert_that(cm.icon_calls()).is_empty()

    def test_plugin_update_refetches_and_replaces_the_cache(self, qapp, tmp_path):
        first = self._dialog(tmp_path / "c")
        _request(first, 0)
        _drain(qapp)
        assert_that((tmp_path / "c" / "com.x.p1" / "1.0.0").is_dir()).is_true()
        cm = FakeConnectionManager(_versioned("2.0.0"), _ICONS)

        updated = IconBrowserDialog(None, cm, cache=IconThumbnailCache(tmp_path / "c"))
        _request(updated, 0)
        _drain(qapp)

        assert_that(cm.icon_calls()).is_not_empty()
        assert_that((tmp_path / "c" / "com.x.p1" / "1.0.0").exists()).is_false()
        assert_that((tmp_path / "c" / "com.x.p1" / "2.0.0").is_dir()).is_true()

    def test_unreadable_icon_is_not_cached(self, qapp, tmp_path):
        cm = _manager()
        cm.call = lambda m, *a: ("!!" if m == "iconpack_icon" else FakeConnectionManager.call(cm, m, *a))
        dialog = self._dialog(tmp_path / "c", cm)

        _request(dialog, 0)
        _drain(qapp)

        assert_that([p for p in (tmp_path / "c").rglob("*.png")]).is_empty()

    def test_different_tint_is_a_different_cache_entry(self, qapp, tmp_path):
        first = self._dialog(tmp_path / "c", tint="#ff0000")
        _request(first, 0)
        _drain(qapp)
        cm = _manager()

        other = self._dialog(tmp_path / "c", cm, tint="#00ff00")
        _request(other, 0)
        _drain(qapp)

        assert_that(cm.icon_calls()).is_length(1)

    def test_server_without_plugin_version_works_and_writes_no_files(self, qapp, tmp_path):
        legacy = [{k: v for k, v in pack.items() if k != "plugin_version"} for pack in _PACKS]
        cm = FakeConnectionManager(legacy, _ICONS)
        dialog = IconBrowserDialog(None, cm, cache=IconThumbnailCache(tmp_path / "c"))

        thumb = _request(dialog, 0)
        _drain(qapp)

        assert_that(len(cm.icon_calls())).is_equal_to(1)
        assert_that(_red(_request(dialog, 0))).is_true()  # the dialog's own memory still serves it
        assert_that(thumb).is_not_none()
        assert_that(list((tmp_path / "c").rglob("*"))).is_empty()

    def test_cache_failure_does_not_break_the_browser(self, qapp, tmp_path):
        class Broken(IconThumbnailCache):
            def get(self, *args, **kwargs):
                raise OSError("disk on fire")

            def put(self, *args, **kwargs):
                raise OSError("disk on fire")

            def update_packs(self, *args, **kwargs):
                raise OSError("disk on fire")

        cm = _manager()
        dialog = IconBrowserDialog(None, cm, cache=Broken(tmp_path / "c"))

        _request(dialog, 0)
        _drain(qapp)

        assert_that(_names(dialog)).is_length(5)
        assert_that(_red(_request(dialog, 0))).is_true()
        assert_that(cm.icon_calls()).is_length(1)

    def test_a_hit_is_not_requested_over_the_network_again_within_the_dialog(self, qapp, tmp_path):
        cm = _manager()
        dialog = IconBrowserDialog(None, cm, cache=IconThumbnailCache(tmp_path / "c"))
        _request(dialog, 0)
        _drain(qapp)

        _request(dialog, 0)
        _request(dialog, 0)
        _drain(qapp)

        assert_that(cm.icon_calls()).is_length(1)


_SYSTEM_PACKS = [
    {
        "id": f"decker.system.{key}",
        "plugin_version": "T-0a1b2c3d",
        "name": label,
        "description": f"System icons: {label}",
        "plugin_id": "decker.system",
        "plugin_name": "System Icons",
        "icon_count": 1,
    }
    for key, label in (("actions", "Actions"), ("mimetypes", "Mimetypes"), ("other", "Other"))
]
_SYSTEM_ICONS = {
    "decker.system.actions": ["edit-copy", "zoom-in"],
    "decker.system.mimetypes": ["org.gnome.Foo", "text-plain"],
    "decker.system.other": ["weird"],
}


class TestSystemIconsInTheLibrary:
    @staticmethod
    def _dialog():
        return IconBrowserDialog(None, FakeConnectionManager(_PACKS + _SYSTEM_PACKS, {**_ICONS, **_SYSTEM_ICONS}))

    def test_the_tree_has_a_system_icons_section_with_its_categories(self, qapp):  # pylint: disable=unused-argument
        dialog = self._dialog()

        top = _top(dialog)

        assert_that([i.text(0) for i in top]).is_equal_to(["All Images", "System Icons", "X Plugin", "Y Plugin"])
        system = top[1]
        assert_that([system.child(i).text(0) for i in range(system.childCount())]).is_equal_to(
            ["Actions", "Mimetypes", "Other"]
        )

    def test_selecting_a_category_lists_only_its_icons(self, qapp):  # pylint: disable=unused-argument
        dialog = self._dialog()

        _select(dialog, 1, 1)

        assert_that(_names(dialog)).is_equal_to(["org.gnome.Foo", "text-plain"])

    def test_selecting_the_section_lists_every_system_icon(self, qapp):  # pylint: disable=unused-argument
        dialog = self._dialog()

        _select(dialog, 1)

        assert_that(_names(dialog)).is_equal_to(["edit-copy", "zoom-in", "org.gnome.Foo", "text-plain", "weird"])

    def test_all_icons_includes_the_system_icons(self, qapp):  # pylint: disable=unused-argument
        dialog = self._dialog()

        _select(dialog, 0)

        assert_that(_names(dialog)).contains("edit-copy", "org.gnome.Foo", "weird")

    def test_search_matches_a_dotted_name_by_its_full_text(self, qapp):  # pylint: disable=unused-argument
        dialog = self._dialog()
        _select(dialog, 1)

        dialog._search.setText("gnome.foo")  # pylint: disable=protected-access
        dialog._refresh()  # pylint: disable=protected-access

        assert_that(_names(dialog)).is_equal_to(["org.gnome.Foo"])

    def test_a_dotted_name_is_shown_whole(self, qapp):  # pylint: disable=unused-argument
        dialog = self._dialog()
        _select(dialog, 1, 1)

        assert_that(_names(dialog)[0]).is_equal_to("org.gnome.Foo")


class TestKindBadge:
    @staticmethod
    def _base() -> QPixmap:
        pixmap = QPixmap(60, 60)
        pixmap.fill(QColor(255, 255, 255))
        return pixmap

    def test_plain_images_get_no_badge(self, qapp):  # pylint: disable=unused-argument
        base = self._base()
        for name in ("a.png", "a.JPG", "noext"):
            assert_that(_kind_badge(base, name).toImage() == base.toImage()).is_true()

    def test_gif_and_svg_badges_fill_the_top_right_third_only(self, qapp):  # pylint: disable=unused-argument
        for name in ("a.gif", "B.SVG"):
            image = _kind_badge(self._base(), name).toImage()
            assert_that(image.pixelColor(2, 2)).is_equal_to(QColor(255, 255, 255))
            assert_that(image.pixelColor(30, 30)).is_equal_to(QColor(255, 255, 255))
            assert_that(image.pixelColor(50, 50)).is_equal_to(QColor(255, 255, 255))  # the delete badge's corner
            assert_that(image.pixelColor(43, 10)).is_not_equal_to(QColor(255, 255, 255))


_USER_PACK = {
    "id": "decker.user",
    "plugin_version": "1",
    "name": "User",
    "description": "",
    "plugin_id": "decker.user",
    "plugin_name": "User",
}


class _UserManager(FakeConnectionManager):
    def __init__(self):
        super().__init__([*_PACKS, _USER_PACK], {**_ICONS, "decker.user": ["mine.png"]})
        self.deleted = False

    def call(self, method, *args):
        if method == "icon_delete":
            self.calls.append((method, *args))
            self.icons["decker.user"] = []
            return True
        return super().call(method, *args)


_BUILTIN_PACK = {
    "id": "decker.builtin.fontawesome.solid",
    "plugin_version": "7",
    "name": "FontAwesome Solids",
    "description": "",
    "plugin_id": "decker.builtin",
    "plugin_name": "Built-In",
}
_SYSTEM_PACK = {
    "id": "decker.system.actions",
    "plugin_version": "T",
    "name": "Actions",
    "description": "",
    "plugin_id": "decker.system",
    "plugin_name": "System Icons",
}
_LOWER_PLUGIN_PACK = {
    "id": "com.a.p",
    "plugin_version": "1",
    "name": "Zed pack",
    "description": "",
    "plugin_id": "com.a",
    "plugin_name": "alpha plugin",
}


def test_the_tree_lists_user_built_in_system_then_plugins_alphabetically(qapp):  # pylint: disable=unused-argument
    packs = [*_PACKS, _SYSTEM_PACK, _LOWER_PLUGIN_PACK, _BUILTIN_PACK, _USER_PACK]
    dialog = IconBrowserDialog(None, FakeConnectionManager(packs, {p["id"]: [] for p in packs}))

    assert_that([i.text(0) for i in _top(dialog)]).is_equal_to(
        ["All Images", "User", "Built-In", "System Icons", "alpha plugin", "X Plugin", "Y Plugin"]
    )


class TestUserImages:
    def test_user_pack_is_flat_right_after_all_images(self, qapp):  # pylint: disable=unused-argument
        dialog = IconBrowserDialog(None, _UserManager())

        top = _top(dialog)

        assert_that([i.text(0) for i in top]).is_equal_to(["All Images", "User", "X Plugin", "Y Plugin"])
        assert_that(top[1].childCount()).is_zero()

    def test_import_button_shows_only_for_the_user_pack(self, qapp):  # pylint: disable=unused-argument
        dialog = IconBrowserDialog(None, _UserManager())

        _select(dialog, 1)
        assert_that(dialog._import_btn.isHidden()).is_false()
        _select(dialog, 2, 0)
        assert_that(dialog._import_btn.isHidden()).is_true()

    def test_delete_confirms_calls_the_server_and_refreshes(self, qapp):  # pylint: disable=unused-argument
        manager = _UserManager()
        dialog = IconBrowserDialog(None, manager)
        _select(dialog, 1)
        assert_that(_names(dialog)).is_equal_to(["mine"])

        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            dialog._on_delete_clicked(dialog._model.index(0, 0))

        assert_that(manager.calls).contains(("icon_delete", "mine.png"))
        assert_that(_names(dialog)).is_empty()

    def test_delete_declined_keeps_the_image(self, qapp):  # pylint: disable=unused-argument
        manager = _UserManager()
        dialog = IconBrowserDialog(None, manager)
        _select(dialog, 1)

        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            dialog._on_delete_clicked(dialog._model.index(0, 0))

        assert_that([c for c in manager.calls if c[0] == "icon_delete"]).is_empty()
        assert_that(_names(dialog)).is_equal_to(["mine"])

    def test_the_delete_badge_sits_in_the_bottom_right_third(self, qapp):  # pylint: disable=unused-argument
        dialog = IconBrowserDialog(None, _UserManager())
        _select(dialog, 1)
        view = dialog._icon_list
        index = dialog._model.index(0, 0)
        option = QStyleOptionViewItem()
        option.initFrom(view)
        option.rect = view.visualRect(index)
        option.widget = view
        dialog._delegate.initStyleOption(option, index)
        style = view.style()
        assert style is not None
        icon = style.subElementRect(QStyle.SubElement.SE_ItemViewItemDecoration, option, view)

        badge = dialog._delegate._badge_rect(option, index)

        assert_that(badge.width()).is_close_to(icon.width() / 3, 0.01)
        assert_that(badge.left() + badge.width()).is_close_to(icon.left() + icon.width(), 0.01)
        assert_that(badge.top() + badge.height()).is_close_to(icon.top() + icon.height(), 0.01)

    def test_only_user_images_get_a_delete_badge(self, qapp):  # pylint: disable=unused-argument
        dialog = IconBrowserDialog(None, _UserManager())

        _select(dialog, 1)
        assert_that(dialog._is_user_entry(dialog._model.index(0, 0))).is_true()
        _select(dialog, 2, 0)
        assert_that(dialog._is_user_entry(dialog._model.index(0, 0))).is_false()


def test_icon_picker_default_tint_follows_the_icon_colour(qapp):
    qapp.setProperty("iconColor", "#ff0000")
    try:
        default = IconBrowserDialog(None, _manager())
        disabled = IconBrowserDialog(None, _manager(), tint=NO_TINT)

        assert_that(default._tint).is_equal_to("#ff0000")  # pylint: disable=protected-access
        assert_that(disabled._tint).is_none()  # pylint: disable=protected-access
    finally:
        qapp.setProperty("iconColor", None)
