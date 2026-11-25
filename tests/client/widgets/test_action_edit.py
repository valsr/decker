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
        "cards": [],
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
            [
                "Do Nothing",
                "Card actions",
                "Own Thing",
                "Built-in",
                "Delay",
                "Global",
                "Other",
                "Web",
                "Owner",
                "Own Shared",
            ]
        )
        model = combo.model()
        assert isinstance(model, QStandardItemModel)
        headers = [
            i
            for i in range(combo.count())
            if combo.itemText(i) in ("Card actions", "Built-in", "Global", "Other", "Owner")
        ]
        item_flags = [model.item(i).isEnabled() for i in headers]  # type: ignore[union-attr]
        assert_that(item_flags).is_equal_to([False] * 5)

    def test_other_plugins_scoped_actions_are_not_listed(self) -> None:
        assert_that(_texts(ActionEdit(owner=_OWNER))).does_not_contain("Private")

    def test_owner_global_action_is_listed_once(self) -> None:
        texts = _texts(ActionEdit(owner=_OWNER))

        assert_that(texts.count("Own Shared")).is_equal_to(1)
        assert_that(texts.index("Own Shared")).is_greater_than(texts.index("Global"))
        assert_that(texts.index("Own Thing")).is_less_than(texts.index("Built-in"))

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


_CARD_A = f"{_OWNER}.card-a"
_CARD_B = f"{_OWNER}.card-b"
_ONLY_A = {**_action(_OWNER, "Owner", f"{_OWNER}.only-a", "Only A", "plugin"), "cards": [_CARD_A]}
_ONLY_B = {**_action(_OWNER, "Owner", f"{_OWNER}.only-b", "Only B", "plugin"), "cards": [_CARD_B]}
_BOTH = {**_action(_OWNER, "Owner", f"{_OWNER}.both", "Both", "plugin"), "cards": [_CARD_A, _CARD_B]}
_UNLISTED = _OWNER_PLUGIN


def _card_actions(combo: ActionEdit) -> list[str]:
    texts = _texts(combo)
    start = texts.index("Card actions") + 1 if "Card actions" in texts else len(texts)
    stop = next((i for i in range(start, len(texts)) if texts[i] in ("Built-in", "Global")), len(texts))
    return texts[start:stop]


class TestCardFilter:
    @pytest.fixture(autouse=True)
    def _served(self) -> None:
        _serve([_NOTHING, _DELAY, _UNLISTED, _ONLY_A, _ONLY_B, _BOTH])

    def test_card_actions_limited_to_the_card(self) -> None:
        combo = ActionEdit(owner=_OWNER)
        combo.set_owner(_OWNER, _CARD_A)
        assert_that(_card_actions(combo)).does_not_contain("Only B")
        assert_that(_card_actions(combo)).contains("Only A")

    def test_unlisted_plugin_actions_visible_on_every_card(self) -> None:
        combo = ActionEdit(owner=_OWNER)
        for card in (_CARD_A, _CARD_B):
            combo.set_owner(_OWNER, card)
            assert_that(_card_actions(combo)).contains("Own Thing")

    def test_shared_action_on_both_cards(self) -> None:
        combo = ActionEdit(owner=_OWNER)
        for card in (_CARD_A, _CARD_B):
            combo.set_owner(_OWNER, card)
            assert_that(_card_actions(combo)).contains("Both")

    def test_bound_action_stays_listed_when_not_offered(self) -> None:
        combo = ActionEdit(owner=_OWNER)
        combo.set_value(f"{_OWNER}.only-b")
        combo.set_owner(_OWNER, _CARD_A)
        assert_that(_card_actions(combo)).contains("Only B")
        assert_that(_texts(combo)).does_not_contain("(missing action)")
        assert_that(combo.value()).is_equal_to(f"{_OWNER}.only-b")

    def test_empty_card_id_lists_all_plugin_actions(self) -> None:
        combo = ActionEdit(owner=_OWNER)
        combo.set_owner(_OWNER, "")
        assert_that(_card_actions(combo)).contains("Only A", "Only B", "Both", "Own Thing")

    def test_unknown_card_id_lists_unlisted_only(self) -> None:
        combo = ActionEdit(owner=_OWNER)
        combo.set_owner(_OWNER, f"{_OWNER}.nope")
        assert_that(_card_actions(combo)).is_equal_to(["Own Thing"])
