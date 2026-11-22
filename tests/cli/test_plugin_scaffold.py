import json
import zipfile

import pytest
from assertpy import assert_that

from decker.cli import plugin as plugin_cli
from decker.cli import plugin_scaffold
from decker.plugin.manifest import PluginManifest
from decker.plugin.package import read_manifest


def test_scaffold_creates_valid_plugin(tmp_path):
    folder = plugin_scaffold.scaffold("com.example.hello-world", tmp_path)
    assert_that(folder).is_equal_to(tmp_path / "com.example.hello-world")
    manifest = json.loads((folder / "manifest.json").read_text())
    assert_that(manifest["name"]).is_equal_to("Hello World")
    assert_that(manifest["format"]).is_equal_to(3)
    assert_that(PluginManifest.load(folder / "manifest.json").format).is_equal_to(3)
    assert_that(manifest["contributes"]["cards"][0]["id"]).is_equal_to("com.example.hello-world.card")
    action_id = "com.example.hello-world.hello"
    assert_that(manifest["contributes"]["actions"][0]["id"]).is_equal_to(action_id)
    assert_that(manifest["contributes"]["actions"][0]["scope"]).is_equal_to("plugin")
    assert_that(manifest["contributes"]["cards"][0]["bindings"]["press"]["action"]).is_equal_to(action_id)
    assert_that((folder / "plugin.py").read_text()).contains(f'@action("{action_id}")')
    assert_that((folder / "plugin.py").read_text()).contains('@card("com.example.hello-world.card")')
    assert_that((folder / "icons" / "action.png").read_bytes()[:4]).is_equal_to(b"\x89PNG")

    package = plugin_scaffold.pack(folder, tmp_path / "out")
    assert_that(package.name).is_equal_to("com.example.hello-world-0.1.0.deckerplugin")
    assert_that(read_manifest(package.read_bytes()).id).is_equal_to("com.example.hello-world")


def test_scaffold_refuses_existing_folder(tmp_path):
    (tmp_path / "com.example.x").mkdir()
    with pytest.raises(plugin_cli.PluginCliError, match="already exists"):
        plugin_scaffold.scaffold("com.example.x", tmp_path)


def test_scaffold_rejects_invalid_id(tmp_path):
    with pytest.raises(plugin_cli.PluginCliError, match="invalid plugin id"):
        plugin_scaffold.scaffold("NotValid", tmp_path)
    assert_that(list(tmp_path.iterdir())).is_empty()


def test_pack_excludes_venv_and_pycache(tmp_path):
    folder = plugin_scaffold.scaffold("com.example.p", tmp_path)
    (folder / ".venv").mkdir()
    (folder / ".venv" / "x.py").write_text("x")
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "plugin.pyc").write_bytes(b"x")
    (folder / ".hidden").write_text("x")
    (folder / "sub").mkdir()
    (folder / "sub" / "keep.txt").write_text("k")
    package = plugin_scaffold.pack(folder, tmp_path)
    with zipfile.ZipFile(package) as zf:
        names = set(zf.namelist())
    assert_that(names).is_equal_to({"manifest.json", "plugin.py", "icons/action.png", "sub/keep.txt"})


def test_pack_rejects_invalid_manifest(tmp_path):
    folder = tmp_path / "bad"
    folder.mkdir()
    (folder / "manifest.json").write_text("{}")
    with pytest.raises(plugin_cli.PluginCliError):
        plugin_scaffold.pack(folder, tmp_path)
    assert_that(list(tmp_path.glob("*.deckerplugin"))).is_empty()


def test_pack_rejects_non_folder(tmp_path):
    with pytest.raises(plugin_cli.PluginCliError, match="not a folder"):
        plugin_scaffold.pack(tmp_path / "nope")


def test_pack_default_out_dir_is_cwd_and_overwrites(tmp_path, monkeypatch):
    folder = plugin_scaffold.scaffold("com.example.q", tmp_path)
    monkeypatch.chdir(tmp_path)
    first = plugin_scaffold.pack(folder)
    second = plugin_scaffold.pack(folder)
    assert_that(first).is_equal_to(second)
    assert_that(first.parent.samefile(tmp_path)).is_true()


def test_pack_rejects_file_symlink(tmp_path):
    folder = plugin_scaffold.scaffold("com.example.s", tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (folder / "link.txt").symlink_to(outside)
    out = tmp_path / "out"
    with pytest.raises(plugin_cli.PluginCliError, match="link.txt"):
        plugin_scaffold.pack(folder, out)
    assert_that(out.exists()).is_false()


def test_pack_rejects_directory_symlink(tmp_path):
    folder = plugin_scaffold.scaffold("com.example.d", tmp_path)
    outside = tmp_path / "outdir"
    outside.mkdir()
    (outside / "f.txt").write_text("x")
    (folder / "linkdir").symlink_to(outside, target_is_directory=True)
    with pytest.raises(plugin_cli.PluginCliError, match="linkdir"):
        plugin_scaffold.pack(folder, tmp_path / "out")
    assert_that((tmp_path / "out").exists()).is_false()


def test_scaffold_rejects_trailing_newline_id(tmp_path):
    with pytest.raises(plugin_cli.PluginCliError, match="invalid plugin id"):
        plugin_scaffold.scaffold("a.b\n", tmp_path)
