import json
from typing import Any

import pytest
from assertpy import assert_that
from PyQt6.QtGui import QStandardItemModel

from decker.client.context import AppContext
from decker.client.widgets.action_edit import ActionEdit
from decker.plugin.bindings import NOTHING_ACTION_ID

_CORE = "com.decker.core"
_OWNER = "com.x.owner"
_OTHER = "com.y.other"


def _action(plugin: str, plugin_name: str, uuid: str, name: str, scope: str) -> dict[str, Any]:
    return {
        "plugin_uuid": plugin,
        "plugin_name": plugin_name,
        "action_uuid": uuid,
        "action_name": name,
        "scope": scope,
        "icon_b64": "",
    }


_NOTHING = _action(_CORE, "Core", NOTHING_ACTION_ID, "Do Nothing", "global")
_DELAY = _action(_CORE, "Core", f"{_CORE}.delay", "Delay", "global")
_OWNER_PLUGIN = _action(_OWNER, "Owner", f"{_OWNER}.own", "Own Thing", "plugin")
_OWNER_GLOBAL = _action(_OWNER, "Owner", f"{_OWNER}.shared", "Own Shared", "global")
_OTHER_GLOBAL = _action(_OTHER, "Other", f"{_OTHER}.web", "Web", "global")
_OTHER_PLUGIN = _action(_OTHER, "Other", f"{_OTHER}.private", "Private", "plugin")
_ALL = [_NOTHING, _DELAY, _OWNER_PLUGIN, _OWNER_GLOBAL, _OTHER_GLOBAL, _OTHER_PLUGIN]


def _serve(actions: list[dict[str, Any]]) -> None:
    class _Rpc:  # pylint: disable=too-few-public-methods
        def call(self, method: str, *_args: Any) -> Any:
            return json.dumps(actions) if method == "plugins_actions_list" else None

    AppContext.get().connection_manager = _Rpc()  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _context(qapp):  # pylint: disable=unused-argument
    AppContext._instance = None  # pylint: disable=protected-access
    _serve(_ALL)
    yield
    AppContext._instance = None  # pylint: disable=protected-access


def _texts(combo: ActionEdit) -> list[str]:
    return [t for t in (combo.itemText(i) for i in range(combo.count())) if t]


class TestActionEditSections:
    def test_sections_are_nothing_card_builtin_global(self) -> None:
        combo = ActionEdit(owner=_OWNER)

        assert_that(_texts(combo)).is_equal_to(
            ["Do Nothing", "Owner", "Own Shared", "Own Thing", "Built-in", "Delay", "Global", "Other", "Web"]
        )
        model = combo.model()
        assert isinstance(model, QStandardItemModel)
        headers = [i for i in range(combo.count()) if combo.itemText(i) in ("Owner", "Built-in", "Global", "Other")]
        item_flags = [model.item(i).isEnabled() for i in headers]  # type: ignore[union-attr]
        assert_that(item_flags).is_equal_to([False] * 4)

    def test_other_plugins_scoped_actions_are_not_listed(self) -> None:
        assert_that(_texts(ActionEdit(owner=_OWNER))).does_not_contain("Private")

    def test_owner_global_action_is_listed_once(self) -> None:
        texts = _texts(ActionEdit(owner=_OWNER))

        assert_that(texts.count("Own Shared")).is_equal_to(1)
        assert_that(texts.index("Own Shared")).is_less_than(texts.index("Built-in"))

    def test_empty_sections_have_no_header(self) -> None:
        _serve([_NOTHING, _DELAY])
        assert_that(_texts(ActionEdit(owner=_OWNER))).is_equal_to(["Do Nothing", "Built-in", "Delay"])

        _serve([_NOTHING])
        assert_that(_texts(ActionEdit(owner=_OWNER))).is_equal_to(["Do Nothing"])

    def test_missing_value_still_shown_disabled(self) -> None:
        combo = ActionEdit(owner=_OWNER)
        combo.set_value("com.gone.thing")

        assert_that(combo.currentText()).is_equal_to("(missing action)")
        model = combo.model()
        assert isinstance(model, QStandardItemModel)
        item = model.item(combo.currentIndex())
        assert item is not None
        assert_that(item.isEnabled()).is_false()
        assert_that(_texts(combo)[-1]).is_equal_to("(missing action)")

    def test_ownerless_has_no_card_section(self) -> None:
        assert_that(_texts(ActionEdit())).is_equal_to(
            ["(choose an action)", "Built-in", "Delay", "Global", "Other", "Web", "Owner", "Own Shared"]
        )
