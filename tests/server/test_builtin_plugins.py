import json
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import psutil
import pytest
from assertpy import assert_that
from PIL import Image

from decker.plugin.environment import venv_python
from decker.plugin.loader import BUILTIN_PLUGINS_DIR, InstalledPlugin, PluginLoader
from decker.plugin.manifest import PluginManifest
from decker.plugin.repository_index import IndexVersion
from decker.plugin.runtime.supervisor import SupervisorPolicy
from decker.plugin.settings_store import PluginSettingsStore
from decker.plugin.target import CardTarget
from decker.server.appearance import compose_key_image, resolve_appearance
from decker.server.plugin_installer import PluginInstallError
from decker.server.plugins import PluginManager
from decker.server.service import DeckerService
from tests.server.test_plugin_installer import Env, _manifest, _package
from tests.server.test_plugins import FakeBus, FakeDeviceManager, FakePages

BLANK = "com.decker.blank"
BLANK_ACTION = "com.decker.blank.blank"


@pytest.fixture(name="env")
def env_fixture(tmp_path):
    environment = Env(tmp_path)
    yield environment
    environment.close()


def test_blank_manifest_ships_in_the_package():
    manifest = PluginManifest.load(BUILTIN_PLUGINS_DIR / BLANK / "manifest.json")

    assert_that(manifest.id).is_equal_to(BLANK)
    assert_that(manifest.runtime).is_none()
    assert_that([(a.id, a.static, a.name) for a in manifest.cards]).is_equal_to([(BLANK_ACTION, True, "Blank")])
    assert_that(manifest.cards[0].tooltip).is_equal_to("Draws only the background and title")


def test_blank_version_is_deckers():
    from decker.common.version import decker_version

    manifest = PluginManifest.load(BUILTIN_PLUGINS_DIR / BLANK / "manifest.json")
    assert_that(manifest.version).is_equal_to(decker_version())


def test_blank_icon_resolves():
    manifest = PluginManifest.load(BUILTIN_PLUGINS_DIR / BLANK / "manifest.json")
    from decker.plugin.icons import read_icon

    assert_that(read_icon(BUILTIN_PLUGINS_DIR / BLANK, manifest.cards[0].icon or manifest.icon)).is_not_none()


def test_scan_builtin_finds_blank(tmp_path):
    result = PluginLoader(tmp_path / "empty").scan_builtin()

    assert_that([p.manifest.id for p in result.plugins]).is_equal_to(sorted([BLANK, "com.decker.core"]))
    assert_that(result.failures).is_empty()


def test_blank_is_loaded_and_listed(env):
    assert_that([a["card_uuid"] for a in env.manager.list_cards()]).contains(BLANK_ACTION)
    assert_that(env.manager.is_builtin(BLANK)).is_true()
    assert_that(env.manager.is_builtin("com.example.tool")).is_false()


def test_blank_key_draws_background_and_title_only(env):
    images: list[bytes] = []
    env.dm.set_image_bytes = lambda _d, _k, image: images.append(image)
    appearance = {"background": "#ff0000", "title": {"text": "Hi", "position": "middle"}}

    env.manager.activate_card(CardTarget(BLANK, BLANK_ACTION, "ctx-b", "dev-1", 0), appearance)

    assert_that(images[-1]).is_equal_to(compose_key_image(resolve_appearance({}, appearance), None, ""))


def test_builtin_cannot_be_uninstalled(env):
    with pytest.raises(PluginInstallError, match="com.decker.blank is built into Decker"):
        env.installer.uninstall(BLANK)

    assert_that(env.manager.is_builtin(BLANK)).is_true()


def test_builtin_cannot_be_replaced_by_a_package_with_its_id(env, tmp_path):
    finished = env.install(_package(tmp_path / "x.log", _manifest(plugin_id=BLANK)))

    assert_that(finished["ok"]).is_false()
    assert_that(finished["message"]).contains("built into Decker")
    assert_that(env.plugins_dir.exists() and list(env.plugins_dir.iterdir())).is_false()


def test_repository_install_of_a_builtin_id_is_refused(env):
    version = IndexVersion("9.9.9", None, "x", 1, "0" * 64, "2026-01-01", {"actions": ["a"]})

    with pytest.raises(PluginInstallError, match="built into Decker"):
        env.installer.install_from_repository(BLANK, version)


def test_user_folder_with_a_builtin_id_is_refused_as_duplicate_and_the_builtin_wins(tmp_path):
    folder = tmp_path / "plugins" / BLANK
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text((BUILTIN_PLUGINS_DIR / BLANK / "manifest.json").read_text())
    environment = Env(tmp_path)
    try:
        plugin = next(p for p in environment.manager.plugins() if p.manifest.id == BLANK)

        assert_that(Path(plugin.plugin_dir)).is_equal_to(BUILTIN_PLUGINS_DIR / BLANK)
        assert_that([f.error for f in environment.manager.load_failures()]).contains("duplicate plugin id")
    finally:
        environment.close()


def test_plugins_list_marks_builtin(env):
    DeckerService.configure_context(env.context)

    listed = {p["id"]: p for p in json.loads(DeckerService().plugins_list())}

    assert_that(listed[BLANK]["builtin"]).is_true()


def test_blank_ships_only_the_blank_card() -> None:
    manifest = PluginManifest.load(BUILTIN_PLUGINS_DIR / "com.decker.blank" / "manifest.json")

    assert_that([c.id for c in manifest.cards]).is_equal_to([BLANK_ACTION])
    assert_that(manifest.description).is_equal_to("A key that shows only a background and a title.")


_CORE_PRESETS = {
    "com.decker.core.preset-httprequest": ("HTTP Request", "com.decker.core.httprequest"),
    "com.decker.core.preset-delay": ("Delay", "com.decker.core.delay"),
    "com.decker.core.preset-sequence": ("Sequence", "com.decker.core.sequence"),
}


def test_core_ships_presets_for_httprequest_delay_and_sequence(env) -> None:
    manifest = PluginManifest.load(BUILTIN_PLUGINS_DIR / "com.decker.core" / "manifest.json")
    actions = {a.id: a for a in manifest.actions}
    specs = {p.id: p for p in manifest.presets}

    assert_that(specs).is_length(3)
    for preset_id, (name, action_id) in _CORE_PRESETS.items():
        spec = specs[preset_id]
        assert_that(spec.name).is_equal_to(name)
        assert_that(spec.icon).is_equal_to(actions[action_id].icon)
        assert_that(spec.appearance).is_empty()
        assert_that(spec.bindings["press"].action).is_equal_to(action_id)
        assert_that(spec.bindings["press"].args).is_empty()
        assert_that(actions[action_id].scope).is_equal_to("global")
    listed = {p["preset_uuid"] for p in env.manager.list_presets()}
    assert_that(set(_CORE_PRESETS)).is_subset_of(listed)


def test_core_presets_not_listed_for_nothing(env) -> None:
    pressed = [p["bindings"]["press"]["action"] for p in env.manager.list_presets()]

    assert_that(pressed).is_not_empty()
    assert_that(pressed).does_not_contain("com.decker.core.nothing")


def test_core_ships_no_cards() -> None:
    manifest = PluginManifest.load(BUILTIN_PLUGINS_DIR / "com.decker.core" / "manifest.json")

    assert_that(manifest.cards).is_empty()
    assert_that([a.id for a in manifest.actions]).contains("com.decker.core.httprequest")


_ACT_FIXTURE = Path(__file__).parent / "fixtures" / "actions_plugin"
_ACT_ID = "com.decker.acttest"
_ACT_TILE = CardTarget(_ACT_ID, "com.decker.acttest.tile", "ctx-tile", "dev-1", 0)


def _act_manager(tmp_path: Path) -> PluginManager:
    manager = PluginManager(
        tmp_path / "plugins",
        SupervisorPolicy(idle_grace_s=30.0, backoff_s=(0.3,), max_crashes=5, ping_interval_s=30.0),
        PluginSettingsStore(tmp_path / "plugins.db"),
    )
    pages = FakePages()
    context = SimpleNamespace(
        device_manager=FakeDeviceManager(),
        event_bus=FakeBus(),
        page_controller=pages,
        page_store=pages,
        key_assignment_store=MagicMock(),
    )
    manager.create(context)  # type: ignore[arg-type]
    return manager


def _wait_for(predicate: Callable[[], bool], timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _command(manager: PluginManager, plugin_id: str) -> list[str]:
    return manager._supervisor._entries[plugin_id].command  # pylint: disable=protected-access


def test_builtin_plugin_starts_without_a_venv(tmp_path: Path) -> None:
    folder = tmp_path / "builtin" / _ACT_ID
    shutil.copytree(_ACT_FIXTURE, folder, ignore=shutil.ignore_patterns("__pycache__"))
    manager = _act_manager(tmp_path)
    try:
        manager.add_plugin(InstalledPlugin(PluginManifest.load(folder / "manifest.json"), folder, builtin=True))
        assert_that(_command(manager, _ACT_ID)[0]).is_equal_to(sys.executable)

        manager.activate_card(_ACT_TILE)
        ready = _wait_for(lambda: manager._supervisor.is_ready(_ACT_ID))  # pylint: disable=protected-access
        assert_that(ready).is_true()
        manager.dispatch_key_down(_ACT_TILE)
        manager.dispatch_key_up(_ACT_TILE)

        marks = folder / "marks.log"
        assert_that(_wait_for(lambda: marks.exists() and marks.read_text(encoding="utf-8") == "p")).is_true()
        assert_that(list(folder.rglob(".venv"))).is_empty()
        assert_that(venv_python(folder).exists()).is_false()
    finally:
        pid = _pid(manager)
        manager.destroy()
        if pid is not None and psutil.pid_exists(pid):
            psutil.Process(pid).kill()


def _pid(manager: PluginManager) -> int | None:
    entry = manager._supervisor._entries.get(_ACT_ID)  # pylint: disable=protected-access
    return entry.process.pid if entry is not None and entry.process is not None else None


def test_user_plugin_mimicking_a_builtin_still_launches_via_its_venv(tmp_path: Path) -> None:
    folder = tmp_path / "plugins" / _ACT_ID
    shutil.copytree(_ACT_FIXTURE, folder, ignore=shutil.ignore_patterns("__pycache__"))
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    manifest["builtin"] = True
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    manager = _act_manager(tmp_path)
    try:
        assert_that(_command(manager, _ACT_ID)[0]).is_equal_to(str(venv_python(folder)))
        assert_that(manager._plugins[_ACT_ID].builtin).is_false()  # pylint: disable=protected-access
    finally:
        manager.destroy()
