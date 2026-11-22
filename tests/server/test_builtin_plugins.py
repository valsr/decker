import json
from pathlib import Path

import pytest
from assertpy import assert_that
from PIL import Image

from decker.plugin.loader import BUILTIN_PLUGINS_DIR, PluginLoader
from decker.plugin.manifest import PluginManifest
from decker.plugin.repository_index import IndexVersion
from decker.plugin.target import CardTarget
from decker.server.appearance import compose_key_image, resolve_appearance
from decker.server.plugin_installer import PluginInstallError
from decker.server.service import DeckerService
from tests.server.test_plugin_installer import Env, _manifest, _package

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
    assert_that([(a.id, a.static, a.name) for a in manifest.cards]).is_equal_to(
        [
            (BLANK_ACTION, True, "Blank"),
            ("com.decker.blank.pagename", True, "Page Name"),
            ("com.decker.blank.pagecount", True, "Page Count"),
        ]
    )
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


def test_blank_ships_page_name_and_page_count_presets() -> None:
    manifest = PluginManifest.from_dict(
        json.loads((BUILTIN_PLUGINS_DIR / "com.decker.blank" / "manifest.json").read_text())
    )
    specs = {a.id: a for a in manifest.cards}

    assert_that(specs["com.decker.blank.pagename"].static).is_true()
    assert_that(specs["com.decker.blank.pagename"].appearance["title"]["text"]).is_equal_to("{page_name}")
    assert_that(specs["com.decker.blank.pagecount"].appearance["title"]["text"]).is_equal_to(
        "{page_index} / {page_count}"
    )


def test_core_ships_no_cards():
    manifest = PluginManifest.load(BUILTIN_PLUGINS_DIR / "com.decker.core" / "manifest.json")

    assert_that(manifest.cards).is_empty()
    assert_that([a.id for a in manifest.actions]).contains("com.decker.core.httprequest")
