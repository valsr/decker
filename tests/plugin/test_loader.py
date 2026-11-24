import json
from pathlib import Path

from assertpy import assert_that

from decker.plugin.loader import PluginLoader

_MANIFEST = {
    "format": 3,
    "id": "com.decker.test",
    "name": "Test",
    "version": "1.0.0",
    "author": "Decker",
    "runtime": {"type": "python", "entry": "plugin.py"},
    "contributes": {"cards": [{"id": "com.decker.test.show", "name": "Show"}]},
}


def _make_plugin(plugins_dir, manifest=None, name="com.decker.test"):
    plugin_dir = plugins_dir / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "manifest.json").write_text(json.dumps(manifest or _MANIFEST))
    return plugin_dir


class TestPluginLoader:
    def test_missing_directory_returns_empty_result(self, tmp_path):
        result = PluginLoader(tmp_path / "plugins").scan()

        assert_that(result.plugins).is_empty()
        assert_that(result.failures).is_empty()

    def test_scans_plugin_folders(self, tmp_path):
        plugin_dir = _make_plugin(tmp_path)
        (tmp_path / "stray-file.txt").write_text("ignored")

        result = PluginLoader(tmp_path).scan()

        assert_that(result.plugins).is_length(1)
        assert_that(result.plugins[0].manifest.id).is_equal_to("com.decker.test")
        assert_that(result.plugins[0].plugin_dir).is_equal_to(plugin_dir)
        assert_that(result.failures).is_empty()

    def test_scan_builtin_marks_plugins_builtin(self, tmp_path: Path) -> None:
        result = PluginLoader(tmp_path).scan_builtin()

        assert_that(result.plugins).is_not_empty()
        assert_that([p.builtin for p in result.plugins]).contains_only(True)

    def test_user_scan_never_marks_plugins_builtin(self, tmp_path: Path) -> None:
        _make_plugin(tmp_path)

        result = PluginLoader(tmp_path).scan()

        assert_that([p.builtin for p in result.plugins]).contains_only(False)

    def test_invalid_manifest_reported_as_failure(self, tmp_path):
        broken = tmp_path / "com.decker.broken"
        broken.mkdir()
        (broken / "manifest.json").write_text("{not valid json")
        _make_plugin(tmp_path)

        result = PluginLoader(tmp_path).scan()

        assert_that(result.plugins).extracting("manifest").extracting("id").contains_only("com.decker.test")
        assert_that(result.failures).is_length(1)
        assert_that(result.failures[0].plugin_uuid).is_equal_to("com.decker.broken")
        assert_that(result.failures[0].error).contains("malformed JSON")

    def test_null_variable_entry_is_a_failure_and_siblings_still_load(self, tmp_path):
        bad = dict(_MANIFEST, id="com.decker.nullvar", format=3, decker=">=0.1")
        bad["contributes"] = {"variables": [{"name": None}]}
        _make_plugin(tmp_path, bad, name="com.decker.nullvar")
        _make_plugin(tmp_path)

        result = PluginLoader(tmp_path).scan()

        assert_that(result.plugins).extracting("manifest").extracting("id").contains_only("com.decker.test")
        assert_that(result.failures).is_length(1)
        assert_that(result.failures[0].plugin_uuid).is_equal_to("com.decker.nullvar")

    def test_old_format_manifest_is_a_failure(self, tmp_path):
        _make_plugin(tmp_path, {"UUID": "com.decker.old", "Name": "Old"}, name="com.decker.old")

        result = PluginLoader(tmp_path).scan()

        assert_that(result.plugins).is_empty()
        assert_that(result.failures).is_length(1)


def test_recover_drops_staging_and_restores_a_moved_aside_version(tmp_path):
    import json

    from decker.plugin.loader import recover_interrupted_installs

    (tmp_path / ".staging-abc").mkdir()
    (tmp_path / ".staging-abc" / "junk").write_text("x")
    old = tmp_path / ".old-job1"
    old.mkdir()
    (old / "manifest.json").write_text(json.dumps({"id": "com.example.tool"}))
    stale = tmp_path / ".old-job2"
    stale.mkdir()
    (stale / "manifest.json").write_text(json.dumps({"id": "com.example.kept"}))
    (tmp_path / "com.example.kept").mkdir()

    recover_interrupted_installs(tmp_path)

    assert (tmp_path / "com.example.tool" / "manifest.json").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["com.example.kept", "com.example.tool"]
