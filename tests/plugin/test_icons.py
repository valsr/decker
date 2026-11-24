import sys
from pathlib import Path

import pytest
from assertpy import assert_that

from decker.common.icon_limits import max_icon_bytes
from decker.plugin.icons import read_icon, resolve_icon


class TestResolveIcon:
    def test_unset_icon(self, tmp_path):
        assert_that(resolve_icon(tmp_path, None)).is_none()
        assert_that(resolve_icon(tmp_path, "")).is_none()

    def test_exact_path_with_extension(self, tmp_path):
        (tmp_path / "icon.png").write_bytes(b"png")

        assert_that(resolve_icon(tmp_path, "icon.png")).is_equal_to((tmp_path / "icon.png").resolve())

    def test_elgato_style_path_without_extension_prefers_svg_then_2x(self, tmp_path):
        (tmp_path / "imgs").mkdir()
        for name in ("a.png", "a@2x.png", "b.png", "b@2x.png", "b.svg"):
            (tmp_path / "imgs" / name).write_bytes(b"x")

        assert_that(resolve_icon(tmp_path, "imgs/a")).is_equal_to((tmp_path / "imgs" / "a@2x.png").resolve())
        assert_that(resolve_icon(tmp_path, "imgs/b")).is_equal_to((tmp_path / "imgs" / "b.svg").resolve())

    def test_missing_file(self, tmp_path):
        assert_that(resolve_icon(tmp_path, "nope")).is_none()

    def test_rejects_paths_outside_plugin_dir(self, tmp_path):
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (tmp_path / "secret.png").write_bytes(b"x")

        assert_that(resolve_icon(plugin_dir, "../secret.png")).is_none()
        assert_that(resolve_icon(plugin_dir, str(tmp_path / "secret"))).is_none()


class TestReadIcon:
    def test_reads_bytes(self, tmp_path):
        (tmp_path / "icon.png").write_bytes(b"png-bytes")

        assert_that(read_icon(tmp_path, "icon")).is_equal_to(b"png-bytes")

    def test_missing_icon(self, tmp_path):
        assert_that(read_icon(tmp_path, "icon")).is_none()

    def test_oversized_icon_is_skipped(self, tmp_path):
        (tmp_path / "big.png").write_bytes(b"x" * (max_icon_bytes() + 1))

        assert_that(read_icon(tmp_path, "big")).is_none()

    def test_icon_of_3_mib_is_served_and_5_mib_is_refused(self, tmp_path):
        (tmp_path / "mid.png").write_bytes(b"x" * (3 * 1024 * 1024))
        (tmp_path / "huge.png").write_bytes(b"x" * (5 * 1024 * 1024))

        assert_that(read_icon(tmp_path, "mid")).is_length(3 * 1024 * 1024)
        assert_that(read_icon(tmp_path, "huge")).is_none()

    def test_unreadable_icon_is_skipped(self, tmp_path, monkeypatch):
        (tmp_path / "icon.png").write_bytes(b"png")

        def _fail(_self):
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "read_bytes", _fail)

        assert_that(read_icon(tmp_path, "icon")).is_none()


def test_max_icon_bytes_default_is_4_mib() -> None:
    assert_that(max_icon_bytes()).is_equal_to(4 * 1024 * 1024)


def test_max_icon_bytes_follows_config(isolated_config) -> None:
    isolated_config.set("icons.max_file_size", 1024)

    assert_that(max_icon_bytes()).is_equal_to(1024)


class TestBuiltinReference:
    def test_short_reference_reads_the_built_in_icon(self, tmp_path: Path) -> None:
        assert_that(read_icon(tmp_path, "fas:play")).contains(b"<svg")

    def test_plugin_relative_icons_still_work(self, tmp_path: Path) -> None:
        (tmp_path / "icon.png").write_bytes(b"png")

        assert_that(read_icon(tmp_path, "icon.png")).is_equal_to(b"png")

    def test_an_invalid_short_ref_is_not_a_built_in_icon(self, tmp_path: Path) -> None:
        assert_that(read_icon(tmp_path, "fas:Play")).is_none()

    @pytest.mark.skipif(sys.platform == "win32", reason="a colon in a file name is an NTFS alternate data stream")
    def test_an_invalid_short_ref_resolves_plugin_relative(self, tmp_path: Path) -> None:
        (tmp_path / "fas:Play").write_bytes(b"mine")

        assert_that(read_icon(tmp_path, "fas:Play")).is_equal_to(b"mine")
