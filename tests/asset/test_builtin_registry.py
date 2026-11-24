from pathlib import Path
from unittest.mock import MagicMock

from assertpy import assert_that

from decker.asset.iconpack import IconPackRegistry
from decker.asset.systemicons import SystemIcons
from decker.asset.usericons import USER_PACK_ID, UserIcons
from tests.asset.test_iconpack import _pack, _plugin


def _registry(tmp_path: Path, failures: list[tuple[str, str]] | None = None) -> IconPackRegistry:
    return IconPackRegistry(
        on_failure=(lambda pid, err: failures.append((pid, err))) if failures is not None else None,
        system_icons=SystemIcons(enabled=False),
        user_icons=UserIcons(tmp_path / "user"),
    )


def test_list_packs_orders_plugin_user_builtin_then_system(tmp_path: Path) -> None:
    _pack(tmp_path)
    registry = _registry(tmp_path)
    registry.register(_plugin(tmp_path))

    ids = [p["id"] for p in registry.list_packs()]

    assert_that(ids).is_equal_to(
        [
            "com.t.p.icons",
            USER_PACK_ID,
            "decker.builtin.fontawesome.brands",
            "decker.builtin.fontawesome.regular",
            "decker.builtin.fontawesome.solid",
        ]
    )
    assert_that(registry.list_icons("decker.builtin.fontawesome.solid")).contains("play.svg")
    assert_that(registry.read_icon("decker.builtin.fontawesome.solid", "play.svg")).is_not_none()


def test_a_plugin_pack_cannot_take_a_builtin_id(tmp_path: Path) -> None:
    _pack(tmp_path)
    failures: list[tuple[str, str]] = []
    registry = _registry(tmp_path, failures)

    plugin = MagicMock()  # the manifest validator cannot produce this id, so the registry guard is tested directly
    plugin.manifest.id = "com.t.p"
    plugin.plugin_dir = tmp_path
    plugin.manifest.icon_packs = [MagicMock(id="decker.builtin.x", path="pack")]

    registry.register(plugin)

    assert_that(failures).is_length(1)
    assert_that(failures[0][1]).contains("reserved")


def test_read_ref_serves_short_references(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    for ref in ("fas:play", "far:paper-plane", "fab:font-awesome", "pack:decker.builtin.fontawesome.solid/play.svg"):
        assert_that(registry.read_ref(ref)).described_as(ref).is_not_none()
    for ref in ("fas:", "fas:../x", "fas:Play", "fas:play.svg", "fas:a/b", "fax:play"):
        assert_that(registry.read_ref(ref)).described_as(ref).is_none()
