import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from assertpy import assert_that

from decker.asset.iconpack import IconPackRegistry
from decker.asset.systemicons import SystemIcons
from decker.asset.usericons import USER_PACK_ID, USER_PLUGIN_NAME, UserIcons
from decker.common.icon_limits import max_icon_bytes
from decker.common.icon_ref import DECKER_ICON_REF
from decker.plugin.loader import InstalledPlugin
from decker.plugin.manifest import PluginManifest


def _no_user(packs: list[dict]) -> list[dict]:
    return [pack for pack in packs if pack["id"] != USER_PACK_ID and pack["plugin_id"] != "decker.builtin"]


def _plugin(tmp_path, path="pack", plugin_id="com.t.p", pack_id="com.t.p.icons") -> InstalledPlugin:
    data = {
        "format": 3,
        "id": plugin_id,
        "name": "T",
        "version": "1.0.0",
        "author": "a",
        "description": "d",
        "contributes": {"icon_packs": [{"id": pack_id, "name": "Pack", "path": path}]},
    }
    manifest = PluginManifest.from_dict(data)
    return InstalledPlugin(manifest, tmp_path)


def _pack(tmp_path) -> None:
    pack = tmp_path / "pack"
    (pack / "sub").mkdir(parents=True)
    (pack / "a.png").write_bytes(b"png-a")
    (pack / "sub" / "b.svg").write_text("<svg/>")
    (pack / "notes.txt").write_text("x")
    (tmp_path / "manifest.json").write_text("{}")


def test_list_icons_finds_images(tmp_path):
    _pack(tmp_path)
    registry = IconPackRegistry()
    registry.register(_plugin(tmp_path))
    assert_that(registry.list_icons("com.t.p.icons")).is_equal_to(["a.png", "sub/b.svg"])
    assert_that(_no_user(registry.list_packs())).is_equal_to(
        [
            {
                "id": "com.t.p.icons",
                "name": "Pack",
                "description": "",
                "plugin_id": "com.t.p",
                "plugin_name": "T",
                "plugin_version": "1.0.0",
                "icon_count": 2,
            }
        ]
    )


def test_icons_json_orders_icons(tmp_path):
    _pack(tmp_path)
    (tmp_path / "pack" / "icons.json").write_text(json.dumps([{"path": "sub/b.svg"}, "a.png", "gone.png"]))
    registry = IconPackRegistry()
    registry.register(_plugin(tmp_path))
    assert_that(registry.list_icons("com.t.p.icons")).is_equal_to(["sub/b.svg", "a.png"])


def test_read_icon_returns_bytes(tmp_path):
    _pack(tmp_path)
    registry = IconPackRegistry()
    registry.register(_plugin(tmp_path))
    assert_that(registry.read_icon("com.t.p.icons", "a.png")).is_equal_to(b"png-a")
    assert_that(registry.read_icon("com.t.p.icons", "missing.png")).is_none()
    assert_that(registry.read_icon("unknown", "a.png")).is_none()
    assert_that(registry.read_icon("com.t.p.icons", "notes.txt")).is_none()


def test_read_icon_rejects_path_escape(tmp_path):
    _pack(tmp_path)
    registry = IconPackRegistry()
    registry.register(_plugin(tmp_path))
    assert_that(registry.read_icon("com.t.p.icons", "../manifest.json")).is_none()


def test_read_icon_rejects_symlink_escape(tmp_path):
    _pack(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-secret.png"
    outside.write_bytes(b"secret")
    (tmp_path / "pack" / "link.png").symlink_to(outside)
    registry = IconPackRegistry()
    registry.register(_plugin(tmp_path))
    assert_that(registry.read_icon("com.t.p.icons", "link.png")).is_none()
    assert_that(registry.list_icons("com.t.p.icons")).does_not_contain("link.png")


def test_read_icon_rejects_oversized(tmp_path):
    _pack(tmp_path)
    (tmp_path / "pack" / "big.png").write_bytes(b"x" * (max_icon_bytes() + 1))
    registry = IconPackRegistry()
    registry.register(_plugin(tmp_path))
    assert_that(registry.read_icon("com.t.p.icons", "big.png")).is_none()


def test_gif_is_listed_and_served_from_a_pack(tmp_path):
    _pack(tmp_path)
    (tmp_path / "pack" / "a.gif").write_bytes(b"GIF89a-bytes")
    registry = IconPackRegistry()
    registry.register(_plugin(tmp_path))
    assert_that(registry.list_icons("com.t.p.icons")).contains("a.gif")
    assert_that(registry.read_icon("com.t.p.icons", "a.gif")).is_equal_to(b"GIF89a-bytes")


def test_icon_of_3_mib_is_served_and_5_mib_is_refused(tmp_path):
    _pack(tmp_path)
    (tmp_path / "pack" / "mid.png").write_bytes(b"x" * (3 * 1024 * 1024))
    (tmp_path / "pack" / "huge.png").write_bytes(b"x" * (5 * 1024 * 1024))
    registry = IconPackRegistry()
    registry.register(_plugin(tmp_path))
    assert_that(registry.read_icon("com.t.p.icons", "mid.png")).is_length(3 * 1024 * 1024)
    assert_that(registry.read_icon("com.t.p.icons", "huge.png")).is_none()


def test_pack_path_escaping_plugin_is_refused(tmp_path):
    plugin_dir = tmp_path / "plugin"
    (tmp_path / "elsewhere").mkdir()
    plugin_dir.mkdir()
    failures: list[tuple[str, str]] = []
    registry = IconPackRegistry(on_failure=lambda pid, err: failures.append((pid, err)))
    registry.register(_plugin(plugin_dir, path="../elsewhere"))
    assert_that(_no_user(registry.list_packs())).is_empty()
    assert_that(failures).is_length(1)
    assert_that(failures[0][1]).contains("outside")


def test_missing_folder_and_duplicate_id_are_refused(tmp_path):
    _pack(tmp_path)
    failures: list[tuple[str, str]] = []
    registry = IconPackRegistry(on_failure=lambda pid, err: failures.append((pid, err)))
    registry.register(_plugin(tmp_path, path="nope"))
    registry.register(_plugin(tmp_path))
    registry.register(_plugin(tmp_path))
    assert_that(failures).is_length(2)
    assert_that(_no_user(registry.list_packs())).is_length(1)


def test_unregister_removes_pack(tmp_path):
    _pack(tmp_path)
    registry = IconPackRegistry()
    registry.register(_plugin(tmp_path))
    registry.unregister("com.t.p")
    assert_that(_no_user(registry.list_packs())).is_empty()
    assert_that(registry.list_icons("com.t.p.icons")).is_empty()


def test_read_icon_with_nul_byte_returns_none(tmp_path):
    _pack(tmp_path)
    registry = IconPackRegistry()
    registry.register(_plugin(tmp_path))
    assert_that(registry.read_icon("com.t.p.icons", "a\0.png")).is_none()


def _registry(tmp_path) -> IconPackRegistry:
    _pack(tmp_path)
    registry = IconPackRegistry()
    registry.register(_plugin(tmp_path))
    return registry


def test_read_ref_returns_icon_bytes(tmp_path):
    registry = _registry(tmp_path)
    assert_that(registry.read_ref("pack:com.t.p.icons/a.png")).is_equal_to(b"png-a")
    assert_that(registry.read_ref("pack:com.t.p.icons/sub/b.svg")).is_equal_to(b"<svg/>")


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "pack:",
        "pack:com.t.p.icons",
        "pack:com.t.p.icons/",
        "pack:com.t.p.icons/missing.png",
        "pack:unknown/a.png",
        "pack:com.t.p.icons/../../etc/passwd",
        "pack:com.t.p.icons//etc/passwd",
        "pack:com.t.p.icons/notes.txt",
        "pack:com.t.p.icons/sub/../../manifest.json",
        "file:com.t.p.icons/a.png",
        None,
        5,
    ],
)
def test_read_ref_rejects_bad_references(tmp_path, ref):
    assert_that(_registry(tmp_path).read_ref(ref)).is_none()


def test_read_ref_respects_size_cap(tmp_path):
    registry = _registry(tmp_path)
    (tmp_path / "pack" / "big.png").write_bytes(b"x" * (max_icon_bytes() + 1))
    assert_that(registry.read_ref("pack:com.t.p.icons/big.png")).is_none()


def test_read_ref_after_unregister_is_none(tmp_path):
    registry = _registry(tmp_path)
    registry.unregister("com.t.p")
    assert_that(registry.read_ref("pack:com.t.p.icons/a.png")).is_none()


def test_plugin_version_follows_the_manifest(tmp_path):
    _pack(tmp_path)
    plugin = _plugin(tmp_path)
    manifest = PluginManifest.from_dict({**_manifest_data(), "version": "2.3.4"})
    registry = IconPackRegistry()
    registry.register(InstalledPlugin(manifest, tmp_path))

    assert_that(registry.list_packs()[0]["plugin_version"]).is_equal_to("2.3.4")
    assert_that(plugin.manifest.version).is_equal_to("1.0.0")


def _manifest_data():
    return {
        "format": 3,
        "id": "com.t.p",
        "name": "T",
        "version": "1.0.0",
        "author": "a",
        "description": "d",
        "contributes": {"icon_packs": [{"id": "com.t.p.icons", "name": "Pack", "path": "pack"}]},
    }


def test_read_ref_serves_the_builtin_decker_icon():
    registry = IconPackRegistry()

    data = registry.read_ref(DECKER_ICON_REF)

    assert_that(data).is_not_none().contains(b"<svg")


class TestSystemIconPacks:
    @staticmethod
    def _system(tmp_path: Path, enabled: bool = True) -> SystemIcons:
        from tests.asset.test_systemicons import make_theme

        root = tmp_path / "icons"
        make_theme(
            root,
            "T",
            {
                "actions/scalable": {"Context": "Actions", "Size": "48", "Type": "Scalable"},
                "places/48": {"Context": "Places", "Size": "48", "Type": "Fixed"},
            },
            files=("actions/scalable/edit-copy.svg", "places/48/folder.png"),
        )
        make_theme(root, "hicolor", {})
        return SystemIcons(
            search_dirs=[root], home=tmp_path / "home", configured_theme=lambda: "T", recheck_s=0, enabled=enabled
        )

    def test_system_packs_follow_the_plugin_packs(self, tmp_path):
        _pack(tmp_path)
        registry = IconPackRegistry(system_icons=self._system(tmp_path))
        registry.register(_plugin(tmp_path))

        packs = registry.list_packs()

        assert_that([p["id"] for p in _no_user(packs)]).is_equal_to(
            ["com.t.p.icons", "decker.system.actions", "decker.system.places"]
        )
        assert_that(_no_user(packs)[1]["plugin_name"]).is_equal_to("System Icons")

    def test_icons_and_bytes_are_served_for_system_packs(self, tmp_path):
        registry = IconPackRegistry(system_icons=self._system(tmp_path))

        assert_that(registry.list_icons("decker.system.actions")).is_equal_to(["edit-copy"])
        assert_that(registry.read_icon("decker.system.actions", "edit-copy")).is_equal_to(b"<svg/>")
        assert_that(registry.read_icon("decker.system.actions", "missing")).is_none()

    def test_read_ref_resolves_system_refs(self, tmp_path):
        registry = IconPackRegistry(system_icons=self._system(tmp_path))

        assert_that(registry.read_ref("pack:decker.system.actions/edit-copy")).is_equal_to(b"<svg/>")
        assert_that(registry.read_ref("pack:decker.system.places/folder")).is_equal_to(b"png")
        assert_that(registry.read_ref("pack:decker.system.actions/folder")).is_none()
        assert_that(registry.read_ref("pack:decker.system.actions/../places/folder")).is_none()

    def test_a_plugin_pack_cannot_take_a_system_id(self, tmp_path):
        _pack(tmp_path)
        failures: list[tuple[str, str]] = []
        registry = IconPackRegistry(
            on_failure=lambda pid, err: failures.append((pid, err)), system_icons=self._system(tmp_path)
        )

        registry.register(_plugin(tmp_path, plugin_id="decker.system", pack_id="decker.system.actions"))

        assert_that(failures).is_length(1)
        assert_that(failures[0][1]).contains("reserved")
        assert_that(registry.read_icon("decker.system.actions", "a.png")).is_none()
        assert_that(registry.read_icon("decker.system.actions", "edit-copy")).is_equal_to(b"<svg/>")

    def test_unregistering_a_plugin_keeps_the_system_packs(self, tmp_path):
        _pack(tmp_path)
        registry = IconPackRegistry(system_icons=self._system(tmp_path))
        registry.register(_plugin(tmp_path))

        registry.unregister("com.t.p")

        assert_that([p["id"] for p in _no_user(registry.list_packs())]).is_equal_to(
            ["decker.system.actions", "decker.system.places"]
        )

    def test_registry_lists_the_user_pack_but_no_system_packs_when_disabled(self, tmp_path: Path) -> None:
        _pack(tmp_path)
        registry = IconPackRegistry(system_icons=self._system(tmp_path, enabled=False))
        registry.register(_plugin(tmp_path))

        ids = [p["id"] for p in registry.list_packs() if p["plugin_id"] != "decker.builtin"]

        assert_that(ids).is_equal_to(["com.t.p.icons", USER_PACK_ID])
        assert_that(registry.list_icons("decker.system.actions")).is_empty()
        assert_that(registry.read_icon("decker.system.actions", "edit-copy")).is_none()

    def test_plugin_still_cannot_register_a_decker_system_pack_when_disabled(self, tmp_path: Path) -> None:
        _pack(tmp_path)
        failures: list[tuple[str, str]] = []
        registry = IconPackRegistry(
            on_failure=lambda pid, err: failures.append((pid, err)),
            system_icons=self._system(tmp_path, enabled=False),
        )

        registry.register(_plugin(tmp_path, plugin_id="decker.system", pack_id="decker.system.actions"))

        assert_that(failures).is_length(1)
        assert_that(failures[0][1]).contains("reserved")
        assert_that(registry.read_icon("decker.system.actions", "a.png")).is_none()

    def test_the_default_registry_is_hermetic_in_tests(self):
        assert_that(_no_user(IconPackRegistry().list_packs())).is_empty()


def test_warm_warms_the_system_icons():
    class Spy:
        warmed = 0

        def warm(self):
            Spy.warmed += 1

        def owns(self, _pack_id):
            return False

        def list_packs(self):
            return []

    IconPackRegistry(system_icons=Spy()).warm()  # type: ignore[arg-type]

    assert_that(Spy.warmed).is_equal_to(1)


def _user_registry(tmp_path) -> IconPackRegistry:
    return IconPackRegistry(user_icons=UserIcons(tmp_path / "user"))


def test_user_pack_is_listed_even_when_empty(tmp_path) -> None:
    registry = _user_registry(tmp_path)
    packs = registry.list_packs()
    user = [pack for pack in packs if pack["id"] == USER_PACK_ID]
    assert_that(user).is_length(1)
    assert_that(user[0]["icon_count"]).is_equal_to(0)
    assert_that(user[0]["plugin_name"]).is_equal_to(USER_PLUGIN_NAME)


def test_user_pack_icons_are_served_by_name(tmp_path) -> None:
    registry = _user_registry(tmp_path)
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "cat.svg").write_bytes(b"<svg/>")
    assert_that(registry.list_icons(USER_PACK_ID)).is_equal_to(["cat.svg"])
    assert_that(registry.read_icon(USER_PACK_ID, "cat.svg")).is_equal_to(b"<svg/>")
    assert_that(registry.read_ref(f"pack:{USER_PACK_ID}/cat.svg")).is_equal_to(b"<svg/>")
    assert_that(registry.read_icon(USER_PACK_ID, "../x.svg")).is_none()


def test_user_pack_sits_between_plugin_and_system_packs(tmp_path) -> None:
    _pack(tmp_path)
    registry = _user_registry(tmp_path)
    registry.register(_plugin(tmp_path))
    ids = [pack["id"] for pack in registry.list_packs()]
    assert_that(ids[:2]).is_equal_to(["com.t.p.icons", USER_PACK_ID])


def test_a_plugin_cannot_register_the_user_pack_id(tmp_path) -> None:
    _pack(tmp_path)
    failures: list[tuple[str, str]] = []
    registry = IconPackRegistry(
        on_failure=lambda pid, err: failures.append((pid, err)), user_icons=UserIcons(tmp_path / "user")
    )
    plugin = MagicMock()  # the manifest validator cannot produce this id, so the registry guard is tested directly
    plugin.manifest.id = "com.t.p"
    plugin.plugin_dir = tmp_path
    plugin.manifest.icon_packs = [MagicMock(id=USER_PACK_ID, path="pack")]
    registry.register(plugin)
    assert_that(failures).is_length(1)
    assert_that(failures[0][1]).contains("reserved")
    assert_that([p for p in registry.list_packs() if p["id"] == USER_PACK_ID]).is_length(1)
