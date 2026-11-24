"""User icon ids and the user-icon store."""

import io
import threading
from pathlib import Path

import pytest
from assertpy import assert_that
from PIL import Image

from decker.asset.iconresize import ImageImportError, ImportOptions
from decker.asset.usericons import ImportResult, UserIcons, normalize_id
from decker.common.icon_limits import max_icon_bytes


def test_normalize_id_lowercases_and_replaces_runs_of_other_characters() -> None:
    assert_that(normalize_id("Ünïcode Cat!")).is_equal_to("n-code-cat")
    assert_that(normalize_id("My  Icon")).is_equal_to("my-icon")


def test_normalize_id_strips_path_traversal() -> None:
    assert_that(normalize_id("../../etc/x")).is_equal_to("etc-x")


def test_normalize_id_returns_empty_for_blank_or_symbols_only() -> None:
    assert_that(normalize_id("   ")).is_equal_to("")
    assert_that(normalize_id("!!!")).is_equal_to("")


def test_normalize_id_truncates_to_64() -> None:
    assert_that(normalize_id("a" * 100)).is_equal_to("a" * 64)
    assert_that(normalize_id("a" * 63 + "!b")).is_equal_to("a" * 63)


def test_exists_is_true_across_extensions(tmp_path: Path) -> None:
    (tmp_path / "cat.gif").write_bytes(b"x")
    icons = UserIcons(tmp_path)
    assert_that(icons.exists("cat")).is_true()
    assert_that(icons.exists("dog")).is_false()


def test_suggest_id_appends_the_first_free_number(tmp_path: Path) -> None:
    (tmp_path / "cat.png").write_bytes(b"x")
    (tmp_path / "cat-2.png").write_bytes(b"x")
    icons = UserIcons(tmp_path)
    assert_that(icons.suggest_id("Cat")).is_equal_to("cat-3")
    assert_that(icons.suggest_id("Dog")).is_equal_to("dog")
    assert_that(icons.suggest_id("!!!")).is_equal_to("icon")


def test_list_icons_ignores_other_files_and_sorts(tmp_path: Path) -> None:
    for name in ("b.png", "a.svg", "c.gif", "d.txt", "e.jpg"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "sub.png").mkdir()
    assert_that(UserIcons(tmp_path).list_icons()).is_equal_to(["a.svg", "b.png", "c.gif"])


def test_list_icons_is_empty_when_directory_is_missing(tmp_path: Path) -> None:
    assert_that(UserIcons(tmp_path / "none").list_icons()).is_empty()


def test_read_icon_refuses_names_outside_the_listing(tmp_path: Path) -> None:
    (tmp_path / "a.png").write_bytes(b"data")
    (tmp_path.parent / "x.png").write_bytes(b"secret")
    icons = UserIcons(tmp_path)
    assert_that(icons.read_icon("a.png")).is_equal_to(b"data")
    assert_that(icons.read_icon("../x.png")).is_none()
    assert_that(icons.read_icon("nope.png")).is_none()


def test_pack_info_counts_icons_and_lists_when_empty(tmp_path: Path) -> None:
    icons = UserIcons(tmp_path)
    info = icons.pack_info()
    assert_that(info).contains_entry({"id": "decker.user"}, {"name": "User"}, {"icon_count": 0})
    (tmp_path / "a.png").write_bytes(b"x")
    assert_that(icons.pack_info()["icon_count"]).is_equal_to(1)
    assert_that(info).contains_key("description", "plugin_id", "plugin_name", "plugin_version")


def test_default_directory_follows_the_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("decker.asset.usericons.user_icons", lambda: tmp_path)
    (tmp_path / "a.png").write_bytes(b"x")
    assert_that(UserIcons().list_icons()).is_equal_to(["a.png"])


def test_read_icon_refuses_oversized_files(tmp_path: Path) -> None:
    (tmp_path / "big.png").write_bytes(b"x" * (max_icon_bytes() + 1))
    assert_that(UserIcons(tmp_path).read_icon("big.png")).is_none()


def test_exists_compares_stems_case_insensitively(tmp_path: Path) -> None:
    (tmp_path / "Cat.png").write_bytes(b"x")
    assert_that(UserIcons(tmp_path).exists("cat")).is_true()


_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'
_OPTIONS = ImportOptions()


def _png(size: tuple[int, int] = (10, 20)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (255, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_import_png_writes_a_144_png_named_by_id(tmp_path: Path) -> None:
    result = UserIcons(tmp_path).import_icon("My Cat", _png(), _OPTIONS)
    assert_that(result).is_equal_to(ImportResult(ok=True, icon_id="my-cat", file="my-cat.png"))
    with Image.open(tmp_path / "my-cat.png") as image:
        assert_that(image.size).is_equal_to((144, 144))


def test_import_svg_copies_bytes_unchanged(tmp_path: Path) -> None:
    result = UserIcons(tmp_path).import_icon("logo", _SVG, _OPTIONS)
    assert_that(result.file).is_equal_to("logo.svg")
    assert_that((tmp_path / "logo.svg").read_bytes()).is_equal_to(_SVG)


def test_import_refuses_an_unsafe_svg(tmp_path: Path) -> None:
    data = b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'
    result = UserIcons(tmp_path).import_icon("logo", data, _OPTIONS)
    assert_that(result.ok).is_false()
    assert_that(list(tmp_path.iterdir())).is_empty()


def test_import_refuses_a_duplicate_id_and_suggests_the_next(tmp_path: Path) -> None:
    icons = UserIcons(tmp_path)
    assert_that(icons.import_icon("cat", _png(), _OPTIONS).ok).is_true()
    result = icons.import_icon("cat", _png(), _OPTIONS)
    assert_that(result.ok).is_false()
    assert_that(result.error).is_equal_to("id already exists")
    assert_that(result.suggestion).is_equal_to("cat-2")
    assert_that(list(tmp_path.iterdir())).is_length(1)


def test_import_refuses_blank_names(tmp_path: Path) -> None:
    result = UserIcons(tmp_path).import_icon("!!!", _png(), _OPTIONS)
    assert_that(result.ok).is_false()
    assert_that(result.error).is_equal_to("invalid name")


def test_import_refuses_over_cap_and_leaves_no_files(tmp_path: Path) -> None:
    result = UserIcons(tmp_path).import_icon("big", b"x" * (max_icon_bytes() + 1), _OPTIONS)
    assert_that(result.ok).is_false()
    assert_that(list(tmp_path.iterdir())).is_empty()


def test_import_refuses_non_images_and_leaves_no_files(tmp_path: Path) -> None:
    result = UserIcons(tmp_path).import_icon("junk", b"not an image", _OPTIONS)
    assert_that(result.ok).is_false()
    assert_that(result.error).is_not_empty()
    assert_that(list(tmp_path.iterdir())).is_empty()


def test_import_creates_the_folder_when_missing(tmp_path: Path) -> None:
    folder = tmp_path / "a" / "icons"
    assert_that(UserIcons(folder).import_icon("cat", _png(), _OPTIONS).ok).is_true()
    assert_that((folder / "cat.png").is_file()).is_true()


def test_concurrent_imports_of_one_id_have_one_winner(tmp_path: Path) -> None:
    icons = UserIcons(tmp_path)
    results: list[ImportResult] = []
    data = _png()

    def run() -> None:
        results.append(icons.import_icon("cat", data, _OPTIONS))

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert_that([r for r in results if r.ok]).is_length(1)
    assert_that([r for r in results if not r.ok]).is_length(7)
    assert_that(list(tmp_path.iterdir())).is_length(1)


def test_import_leaves_no_temp_files_on_a_failed_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("decker.asset.usericons.os.replace", fail)
    result = UserIcons(tmp_path).import_icon("cat", _png(), _OPTIONS)
    assert_that(result.ok).is_false()
    assert_that(list(tmp_path.iterdir())).is_empty()


def test_import_result_to_json_has_the_documented_keys() -> None:
    text = ImportResult(ok=False, error="e", suggestion="s").to_json()
    assert_that(text).is_equal_to('{"ok": false, "id": "", "file": "", "error": "e", "suggestion": "s"}')


def test_preview_returns_svg_unchanged_and_png_for_rasters(tmp_path: Path) -> None:
    icons = UserIcons(tmp_path)
    assert_that(icons.preview(_SVG, _OPTIONS)).is_equal_to(_SVG)
    with Image.open(io.BytesIO(icons.preview(_png(), _OPTIONS))) as image:
        assert_that(image.size).is_equal_to((144, 144))
    assert_that(list(tmp_path.iterdir())).is_empty()


def test_preview_refuses_bad_data(tmp_path: Path) -> None:
    with pytest.raises(ImageImportError):
        UserIcons(tmp_path).preview(b"junk", _OPTIONS)
    with pytest.raises(ValueError):
        UserIcons(tmp_path).preview(b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>", _OPTIONS)


_UTF16_SVG = '<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'.encode("utf-16-le")


def test_import_refuses_utf16_svg_and_writes_nothing(tmp_path: Path) -> None:
    result = UserIcons(tmp_path).import_icon("cat", _UTF16_SVG, _OPTIONS)
    assert_that(result.ok).is_false()
    assert_that(list(tmp_path.iterdir())).is_empty()


def test_preview_refuses_utf16_svg(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        UserIcons(tmp_path).preview(_UTF16_SVG, _OPTIONS)
    assert_that(list(tmp_path.iterdir())).is_empty()


def test_import_rejects_images_over_the_configured_limit(tmp_path: Path, isolated_config) -> None:
    isolated_config.set("icons.max_file_size", 1024)
    result = UserIcons(tmp_path).import_icon("big", b"x" * 2048, _OPTIONS)

    assert_that(result.ok).is_false()
    assert_that(result.error).contains("1024")


def test_delete_icon_removes_a_listed_icon_only(tmp_path: Path) -> None:
    (tmp_path / "keep.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (tmp_path / "gone.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    store = UserIcons(tmp_path)

    assert_that(store.delete_icon("gone.svg")).is_true()
    assert_that(store.list_icons()).is_equal_to(["keep.svg"])
    assert_that(store.delete_icon("gone.svg")).is_false()
    assert_that(store.delete_icon("../outside.svg")).is_false()
