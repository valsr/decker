import io
import json
import stat
import sys
import zipfile
from pathlib import Path

import pytest
from assertpy import assert_that

from decker.plugin import package
from decker.plugin.package import PackageError, extract, read_manifest

_MANIFEST = {
    "format": 3,
    "id": "com.decker.test",
    "name": "Test",
    "version": "1.0.0",
    "author": "Decker",
    "runtime": {"type": "python", "entry": "plugin.py"},
    "contributes": {"cards": [{"id": "com.decker.test.show", "name": "Show"}]},
}


def _zip(files=None, manifest=None, extra=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if files is None:
            zf.writestr("manifest.json", json.dumps(manifest or _MANIFEST))
            zf.writestr("plugin.py", "print('hi')\n")
        for name, content in (files or {}).items():
            zf.writestr(name, content)
        for info, content in extra or []:
            zf.writestr(info, content)
    return buf.getvalue()


def test_valid_package_reads_manifest():
    assert_that(read_manifest(_zip()).id).is_equal_to("com.decker.test")


def test_extract_writes_files_under_dest(tmp_path):
    dest = tmp_path / "out"
    manifest = extract(_zip(extra=[("sub/", ""), ("sub/a.txt", "a")]), dest)
    assert_that(manifest.id).is_equal_to("com.decker.test")
    assert_that((dest / "plugin.py").read_text()).contains("hi")
    assert_that((dest / "sub" / "a.txt").read_text()).is_equal_to("a")


def test_extract_refuses_existing_dest(tmp_path):
    with pytest.raises(PackageError):
        extract(_zip(), tmp_path)
    assert_that(tmp_path.exists()).is_true()


def test_extract_leaves_nothing_on_failure(tmp_path):
    dest = tmp_path / "out"
    with pytest.raises(PackageError):
        extract(_zip({"a.txt": "x"}), dest)
    assert_that(dest.exists()).is_false()


def test_rejects_oversized_upload(monkeypatch):
    monkeypatch.setattr(package, "MAX_PACKAGE_BYTES", 10)
    with pytest.raises(PackageError, match="too large"):
        read_manifest(_zip())


def test_rejects_zip_bomb_by_uncompressed_total(monkeypatch):
    monkeypatch.setattr(package, "MAX_UNCOMPRESSED_BYTES", 100)
    with pytest.raises(PackageError, match="uncompressed"):
        read_manifest(_zip(extra=[("big.bin", b"0" * 1000)]))


def test_extract_enforces_real_size_when_header_lies(tmp_path, monkeypatch):
    data = _zip(extra=[("big.bin", b"0" * 5000)])
    real_validate = package._validate_entries  # pylint: disable=protected-access

    def lying(zf):
        infos = real_validate(zf)
        for info in infos:
            info.file_size = 1
        return infos

    monkeypatch.setattr(package, "_validate_entries", lying)
    monkeypatch.setattr(package, "MAX_UNCOMPRESSED_BYTES", 3000)
    dest = tmp_path / "out"
    with pytest.raises(PackageError, match="uncompressed"):
        extract(data, dest)
    assert_that(dest.exists()).is_false()


def test_rejects_too_many_entries(monkeypatch):
    monkeypatch.setattr(package, "MAX_ENTRIES", 1)
    with pytest.raises(PackageError, match="too many"):
        read_manifest(_zip())


@pytest.mark.parametrize("name", ["../evil.txt", "a/../../evil.txt"])
def test_rejects_parent_traversal(name):
    with pytest.raises(PackageError, match="traversal"):
        read_manifest(_zip(extra=[(name, "x")]))


def test_rejects_absolute_path():
    with pytest.raises(PackageError, match="absolute"):
        read_manifest(_zip(extra=[("/etc/evil", "x")]))


def test_rejects_backslash_names(tmp_path: Path) -> None:
    info = zipfile.ZipInfo("placeholder.txt")
    info.filename = "a\\b.txt"  # ZipInfo() itself rewrites os.sep to "/" on Windows
    if sys.platform == "win32":  # zipfile reads "\\" as "/" there, so the entry is an ordinary nested file
        dest = tmp_path / "out"
        extract(_zip(extra=[(info, "x")]), dest)
        assert_that((dest / "a" / "b.txt").read_text()).is_equal_to("x")
        return
    with pytest.raises(PackageError, match="backslash"):
        read_manifest(_zip(extra=[(info, "x")]))


@pytest.mark.windows_only("plugin install (docs/platform-support.md row 'Plugin install and repositories')")
def test_rejects_backslash_traversal_on_windows() -> None:
    info = zipfile.ZipInfo("placeholder.txt")
    info.filename = "..\\evil.txt"
    with pytest.raises(PackageError, match="parent traversal"):
        read_manifest(_zip(extra=[(info, "x")]))


def test_rejects_symlink_entry():
    info = zipfile.ZipInfo("link")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(PackageError, match="symlink"):
        read_manifest(_zip(extra=[(info, "/etc/passwd")]))


def test_rejects_duplicate_names():
    with pytest.warns(UserWarning):
        data = _zip(extra=[("plugin.py", "again")])
    with pytest.raises(PackageError, match="duplicate"):
        read_manifest(data)


def test_rejects_encrypted_entry():
    data = bytearray(_zip())
    # set the encryption flag bit in the central directory header of the first entry
    idx = data.index(b"PK\x01\x02")
    data[idx + 8] |= 0x1
    with pytest.raises(PackageError, match="encrypted"):
        read_manifest(bytes(data))


def test_rejects_missing_root_manifest():
    with pytest.raises(PackageError, match="manifest.json"):
        read_manifest(_zip({"sub/manifest.json": json.dumps(_MANIFEST)}))


def test_rejects_non_zip():
    with pytest.raises(PackageError, match="zip"):
        read_manifest(b"not a zip")


def test_rejects_non_json_manifest():
    with pytest.raises(PackageError, match="JSON"):
        read_manifest(_zip({"manifest.json": "{nope"}))


def test_rejects_non_object_manifest():
    with pytest.raises(PackageError, match="object"):
        read_manifest(_zip({"manifest.json": "[]"}))


def test_manifest_error_message_preserved():
    with pytest.raises(PackageError, match="unsupported manifest format 99"):
        read_manifest(_zip(manifest={**_MANIFEST, "format": 99}))


def test_rejects_directory_only_manifest_entry(tmp_path):
    data = _zip({"manifest.json/": ""})
    with pytest.raises(PackageError, match="manifest.json"):
        read_manifest(data)
    dest = tmp_path / "out"
    with pytest.raises(PackageError, match="manifest.json"):
        extract(data, dest)
    assert_that(dest.exists()).is_false()


def test_unexpected_error_mid_extraction_cleans_up(tmp_path, monkeypatch):
    real = package._extract_member  # pylint: disable=protected-access
    calls = []

    def flaky(zf, info, root, written):
        calls.append(info.filename)
        if len(calls) == 2:
            raise ValueError("boom")
        return real(zf, info, root, written)

    monkeypatch.setattr(package, "_extract_member", flaky)
    dest = tmp_path / "out"
    with pytest.raises(PackageError, match="boom"):
        extract(_zip(), dest)
    assert_that(dest.exists()).is_false()


def test_keyboard_interrupt_cleans_up_and_propagates(tmp_path, monkeypatch):
    def interrupt(*_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(package, "_extract_member", interrupt)
    dest = tmp_path / "out"
    with pytest.raises(KeyboardInterrupt):
        extract(_zip(), dest)
    assert_that(dest.exists()).is_false()


def test_oversized_manifest_is_rejected():
    big = json.dumps({**_MANIFEST, "description": "x" * (package.MAX_MANIFEST_BYTES + 10)})
    with pytest.raises(PackageError, match="too large"):
        read_manifest(_zip({"manifest.json": big}))
