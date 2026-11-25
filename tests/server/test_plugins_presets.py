import base64
from pathlib import Path
from typing import Any

import pytest
from assertpy import assert_that

from decker.plugin.loader import InstalledPlugin
from decker.plugin.manifest import PluginManifest
from decker.plugin.settings_store import PluginSettingsStore
from decker.plugin.target import CardTarget
from decker.server.plugins import PluginManager

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
_GLOBAL = "com.t.src.shout"
_PRIVATE = "com.t.src.hush"


def _make(tmp_path: Path, plugin_id: str, contributes: dict[str, Any]) -> InstalledPlugin:
    root = tmp_path / "plugins" / plugin_id
    root.mkdir(parents=True)
    (root / "icon.png").write_bytes(_PNG)
    manifest = PluginManifest.from_dict(
        {
            "format": 3,
            "id": plugin_id,
            "name": plugin_id,
            "version": "1.0.0",
            "author": "a",
            "description": "d",
            "decker": ">=0.7",
            "runtime": {"type": "python", "entry": "p.py"},
            "contributes": contributes,
        }
    )
    return InstalledPlugin(manifest, root)


def _source(tmp_path: Path) -> InstalledPlugin:
    return _make(
        tmp_path,
        "com.t.src",
        {
            "actions": [
                {"id": _GLOBAL, "name": "Shout", "scope": "global"},
                {"id": _PRIVATE, "name": "Hush", "scope": "plugin"},
            ]
        },
    )


def _presets(tmp_path: Path, plugin_id: str, *presets: dict[str, Any]) -> InstalledPlugin:
    return _make(tmp_path, plugin_id, {"presets": list(presets)})


@pytest.fixture(name="manager")
def manager_fixture(tmp_path):
    manager = PluginManager(tmp_path / "plugins", settings_store=PluginSettingsStore(tmp_path / "plugins.db"))
    yield manager
    manager.destroy()


def _ids(manager: PluginManager) -> list[str]:
    return [p["preset_uuid"] for p in manager.list_presets()]


def test_list_presets_shape_and_icon(manager, tmp_path) -> None:
    manager.add_plugin(_source(tmp_path))
    preset = {
        "id": "com.t.p.loud",
        "name": "Loud",
        "tooltip": "Shouts",
        "icon": "icon.png",
        "appearance": {"background": "#112233"},
        "bindings": {"press": {"action": _GLOBAL, "args": {}}},
    }
    manager.add_plugin(_presets(tmp_path, "com.t.p", preset))

    (entry,) = manager.list_presets()

    assert_that(entry).contains_entry({"plugin_uuid": "com.t.p"}, {"plugin_name": "com.t.p"}, {"plugin_builtin": False})
    assert_that(entry).contains_entry({"preset_uuid": "com.t.p.loud"}, {"preset_name": "Loud"}, {"tooltip": "Shouts"})
    assert_that(entry["icon_b64"]).is_not_empty()
    assert_that(entry["appearance"]["background"]).is_equal_to("#112233")
    assert_that(entry["bindings"]["press"]["action"]).is_equal_to(_GLOBAL)
    assert_that(entry["bindings"]["press"]).contains_key("args")


def test_list_presets_hides_preset_with_missing_action(manager, tmp_path) -> None:
    manager.add_plugin(
        _presets(tmp_path, "com.t.p", {"id": "com.t.p.a", "name": "A", "bindings": {"press": {"action": _GLOBAL}}})
    )

    assert_that(_ids(manager)).is_empty()


def test_list_presets_hides_preset_bound_to_foreign_plugin_scoped_action(manager, tmp_path) -> None:
    manager.add_plugin(_source(tmp_path))
    manager.add_plugin(
        _presets(tmp_path, "com.t.p", {"id": "com.t.p.a", "name": "A", "bindings": {"press": {"action": _PRIVATE}}})
    )

    assert_that(_ids(manager)).is_empty()


def test_preset_returns_when_action_plugin_installs(manager, tmp_path) -> None:
    manager.add_plugin(
        _presets(tmp_path, "com.t.p", {"id": "com.t.p.a", "name": "A", "bindings": {"press": {"action": _GLOBAL}}})
    )
    assert_that(_ids(manager)).is_empty()

    manager.add_plugin(_source(tmp_path))

    assert_that(_ids(manager)).is_equal_to(["com.t.p.a"])


def test_preset_without_bindings_is_listed_and_looked_up(manager, tmp_path) -> None:
    manager.add_plugin(_presets(tmp_path, "com.t.p", {"id": "com.t.p.a", "name": "A"}))

    assert_that(manager.preset("com.t.p", "com.t.p.a")).is_not_none()
    assert_that(manager.preset("com.t.p", "com.t.p.nope")).is_none()
    assert_that(manager.preset("com.t.none", "com.t.p.a")).is_none()


def test_preset_bound_to_own_global_action_is_listed_and_valid_on_a_blank_key(manager, tmp_path) -> None:
    own = _make(
        tmp_path,
        "com.t.own",
        {
            "actions": [{"id": "com.t.own.go", "name": "Go", "scope": "global"}],
            "presets": [{"id": "com.t.own.p", "name": "P", "bindings": {"press": {"action": "com.t.own.go"}}}],
        },
    )
    manager.add_plugin(own)

    (entry,) = manager.list_presets()

    target = CardTarget("com.decker.core", "com.decker.core.blank", "ctx", "dev", 0)
    assert_that(manager.validate_bindings(target, entry["bindings"])).is_empty()
    assert_that(manager.preset_usable(manager.preset("com.t.own", "com.t.own.p"))).is_true()
