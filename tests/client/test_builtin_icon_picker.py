"""The icon picker shows the built-in Font Awesome packs and yields short references."""

# pylint: disable=protected-access,unused-argument
import json

from assertpy import assert_that
from PyQt6.QtWidgets import QApplication

from decker.asset.iconpack import IconPackRegistry
from decker.asset.systemicons import SystemIcons
from decker.client.icon_picker import IconBrowserDialog
from decker.client.widgets.icon_edit import IconEdit


class _RegistryRpc:
    """Answers the picker's RPCs from a real registry."""

    def __init__(self) -> None:
        self.registry = IconPackRegistry(system_icons=SystemIcons(enabled=False))

    def call(self, method: str, *args: str) -> str | None:
        if method == "iconpacks_list":
            return json.dumps(self.registry.list_packs())
        if method == "iconpack_icons":
            return json.dumps(self.registry.list_icons(args[0]))
        return None


def test_tree_has_a_built_in_node_with_three_named_packs(qapp: QApplication) -> None:
    dialog = IconBrowserDialog(None, _RegistryRpc())

    tree = dialog._tree
    nodes = [item for item in (tree.topLevelItem(i) for i in range(tree.topLevelItemCount())) if item is not None]
    built_in = next(n for n in nodes if n.text(0) == "Built-In")
    children = [built_in.child(i) for i in range(built_in.childCount())]

    assert_that([c.text(0) for c in children if c is not None]).is_equal_to(
        ["FontAwesome Brands", "FontAwesome Regular", "FontAwesome Solids"]
    )


def test_choosing_a_built_in_icon_yields_a_short_reference(qapp: QApplication) -> None:
    rpc = _RegistryRpc()
    edit = IconEdit(lambda: rpc)

    edit._on_icon_chosen("decker.builtin.fontawesome.solid", "play.svg")

    assert_that(edit.value()["ref"]).is_equal_to("fas:play")
    edit.set_value({"ref": "fas:play", "color": "#ffffff"})
    assert_that(edit.value()["ref"]).is_equal_to("fas:play")
