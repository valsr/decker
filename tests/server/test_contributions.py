from pathlib import Path

import pytest
from assertpy import assert_that

from decker.asset.iconpack import IconPackRegistry
from decker.plugin.loader import InstalledPlugin, PluginLoader
from decker.plugin.manifest import PluginManifest
from decker.plugin.settings_store import PluginSettingsStore
from decker.server.contributions import ContributionRegistry, contributed_kinds
from decker.server.plugins import PluginManager


class _Recorder:
    def __init__(self, kind: str, fail: bool = False) -> None:
        self.kind = kind
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def register(self, plugin: InstalledPlugin) -> None:
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append(("register", plugin.manifest.id))

    def unregister(self, plugin_id: str) -> None:
        self.calls.append(("unregister", plugin_id))


def _plugin_packs(registry):
    return [pack for pack in registry.list_packs() if pack["id"] != "decker.user"]


def _plugin(tmp_path: Path, contributes: dict, runtime: bool = False, decker: bool = False) -> InstalledPlugin:
    data: dict = {
        "format": 3,
        "id": "com.t.p",
        "name": "T",
        "version": "1.0.0",
        "author": "a",
        "description": "d",
        "contributes": contributes,
    }
    if runtime:
        data["runtime"] = {"type": "python", "entry": "plugin.py"}
    if decker:
        data["decker"] = ">=0.6"
    return InstalledPlugin(PluginManifest.from_dict(data), tmp_path)


_PACK = {"icon_packs": [{"id": "com.t.p.icons", "name": "Pack", "path": "pack"}]}
_ACTION = {"id": "com.t.p.a", "name": "A"}


def _pack_dir(tmp_path: Path) -> None:
    (tmp_path / "pack").mkdir()
    (tmp_path / "pack" / "a.png").write_bytes(b"x")


def test_registry_dispatches_by_kind(tmp_path):
    cards, actions, packs = _Recorder("cards"), _Recorder("actions"), _Recorder("icon_packs")
    registry = ContributionRegistry([cards, actions, packs, _Recorder("variables")])

    registry.add(_plugin(tmp_path, _PACK))
    registry.remove("com.t.p")

    assert_that(cards.calls).is_equal_to([("unregister", "com.t.p")])
    assert_that(actions.calls).is_equal_to([("unregister", "com.t.p")])
    assert_that(packs.calls).is_equal_to([("register", "com.t.p"), ("unregister", "com.t.p")])


def test_registry_requires_every_known_kind():
    with pytest.raises(AssertionError):
        ContributionRegistry([_Recorder("cards"), _Recorder("actions"), _Recorder("icon_packs")])


def test_handler_failure_rolls_back(tmp_path):
    actions, packs = _Recorder("cards"), _Recorder("icon_packs", fail=True)
    registry = ContributionRegistry([actions, _Recorder("actions"), packs, _Recorder("variables")])
    plugin = _plugin(tmp_path, {"cards": [_ACTION], **_PACK}, runtime=True)

    with pytest.raises(RuntimeError):
        registry.add(plugin)

    assert_that(actions.calls).is_equal_to([("register", "com.t.p"), ("unregister", "com.t.p")])


def test_contributed_kinds_includes_variables(tmp_path):
    plugin = _plugin(tmp_path, {"variables": [{"name": "media_volume"}]}, runtime=True, decker=True)

    assert_that(contributed_kinds(plugin)).is_equal_to(frozenset({"variables"}))


def test_registry_dispatches_variables_only_to_the_variables_handler(tmp_path):
    cards, variables = _Recorder("cards"), _Recorder("variables")
    registry = ContributionRegistry([cards, _Recorder("actions"), _Recorder("icon_packs"), variables])

    registry.add(_plugin(tmp_path, {"variables": [{"name": "media_volume"}]}, runtime=True, decker=True))

    assert_that(cards.calls).is_empty()
    assert_that(variables.calls).is_equal_to([("register", "com.t.p")])


def _manager(tmp_path: Path) -> PluginManager:
    return PluginManager(tmp_path / "plugins", settings_store=PluginSettingsStore(tmp_path / "plugins.db"))


def test_icon_pack_only_plugin_starts_no_process(tmp_path):
    _pack_dir(tmp_path)
    manager = _manager(tmp_path)
    try:
        manager.add_plugin(_plugin(tmp_path, _PACK))
        assert_that(manager.icon_packs.list_icons("com.t.p.icons")).is_equal_to(["a.png"])
        assert_that(manager._supervisor._entries).is_empty()  # pylint: disable=protected-access
    finally:
        manager.destroy()


def test_uninstall_removes_pack(tmp_path):
    _pack_dir(tmp_path)
    manager = _manager(tmp_path)
    try:
        manager.add_plugin(_plugin(tmp_path, _PACK))
        manager.remove_plugin("com.t.p")
        assert_that(_plugin_packs(manager.icon_packs)).is_empty()
        assert_that(manager.plugins()).is_empty()
    finally:
        manager.destroy()


def test_bad_pack_path_is_a_load_failure_but_keeps_actions(tmp_path):
    manager = _manager(tmp_path)
    plugin = _plugin(
        tmp_path,
        {"cards": [_ACTION], "icon_packs": [{"id": "com.t.p.icons", "name": "P", "path": "../out"}]},
        runtime=True,
    )
    try:
        manager.add_plugin(plugin)
        assert_that(_plugin_packs(manager.icon_packs)).is_empty()
        assert_that(manager.load_failures()).extracting("error").contains(
            "icon pack com.t.p.icons: path points outside the plugin folder"
        )
        assert_that(manager.list_cards()).extracting("card_uuid").contains("com.t.p.a")
    finally:
        manager.destroy()


def test_fixture_contributes_example_pack(tmp_path):
    scan = PluginLoader(Path(__file__).parents[1] / "fixtures" / "plugins").scan()
    counter = next(p for p in scan.plugins if p.manifest.id == "com.decker.testfixture")
    registry = IconPackRegistry()

    registry.register(counter)

    assert_that(_plugin_packs(registry)).extracting("id").is_equal_to(["com.decker.testfixture.icons"])
    icons = registry.list_icons("com.decker.testfixture.icons")
    assert_that(icons).is_not_empty()
    data = registry.read_icon("com.decker.testfixture.icons", icons[0])
    assert data is not None
    assert_that(data[:4]).is_equal_to(b"\x89PNG")
