import json
import threading
from typing import cast
from unittest.mock import MagicMock, call

import pytest
from assertpy import assert_that

from decker.common.model import AppRule, DefaultRule, MatchCondition
from decker.plugin.target import CardTarget
from decker.server.app_rule_store import AppRuleStore
from decker.server.device_state_store import DeviceStateStore
from decker.server.key_assignments import KeyAssignmentStore
from decker.server.page_controller import (
    PAGES_CHANGED_EVENT,
    PROFILES_CHANGED_EVENT,
    PageController,
)
from decker.server.pages import PageStore, initial_page_id
from decker.server.server_context import ServerContext

DEV = "dev1"
PAGE1 = initial_page_id(DEV)


def _target(key_index: int, ctx: str) -> CardTarget:
    return CardTarget(plugin_uuid="com.p", card_uuid="com.p.a", context_id=ctx, device_id=DEV, key_index=key_index)


@pytest.fixture()
def context(tmp_path):
    pages = PageStore(db_path=tmp_path / "decker.db")
    keys = KeyAssignmentStore(db_path=tmp_path / "decker.db")
    states = DeviceStateStore(data_dir=tmp_path)
    controller = PageController()
    rules = AppRuleStore(db_path=tmp_path / "decker.db")
    device_manager = MagicMock()
    device_manager.show_page.return_value = True
    device_manager.get_device_state.return_value.has_image.return_value = False
    ctx = ServerContext(
        device_manager=device_manager,
        plugin_manager=MagicMock(),
        key_assignment_store=keys,
        device_state_store=states,
        page_store=pages,
        page_controller=controller,
        event_bus=MagicMock(),
        app_rule_store=rules,
    )
    for manager in (pages, keys, states, rules, controller):
        manager.create(ctx)
    yield ctx
    for manager in (controller, rules, states, keys, pages):
        manager.destroy()


def _events(context: ServerContext) -> list[tuple[str, dict]]:
    push = cast(MagicMock, context.event_bus.push)
    return [(c.args[0], json.loads(c.args[1])) for c in push.call_args_list]


class TestActivePage:
    def test_starts_on_default_page(self, context):
        page = context.page_store.add_page(DEV)
        context.page_store.set_default_page(DEV, page.page_id)

        assert_that(context.page_controller.active_page_id(DEV)).is_equal_to(page.page_id)

    def test_recorded_active_page_is_read_without_the_lock(self, context):
        controller = context.page_controller
        expected = controller.active_page_id(DEV)
        result: list[str] = []

        with controller._lock:  # pylint: disable=protected-access
            thread = threading.Thread(target=lambda: result.append(controller.active_page_id(DEV)))
            thread.start()
            thread.join(2)
            finished = not thread.is_alive()

        assert_that(finished).is_true()
        assert_that(result).is_equal_to([expected])

    def test_device_pages_snapshot(self, context):
        page = context.page_controller.add_page(DEV, "Media")
        context.page_controller.activate(DEV, page.page_id)

        snapshot = context.page_controller.device_pages(DEV)

        assert_that([p.name for p in snapshot.pages]).is_equal_to(["Page 1", "Media"])
        assert_that(snapshot.default_page_id).is_equal_to(PAGE1)
        assert_that(snapshot.active_page_id).is_equal_to(page.page_id)


class TestActivate:
    def test_switch_deactivates_old_shows_new_then_activates_new(self, context):
        page2 = context.page_store.add_page(DEV)
        old, new = _target(0, "old"), _target(0, "new")
        context.key_assignment_store.assign(DEV, PAGE1, 0, old, {})
        context.key_assignment_store.assign(DEV, page2.page_id, 0, new, {"step": 2})
        tracker = MagicMock()
        tracker.attach_mock(context.plugin_manager, "pm")
        tracker.attach_mock(context.device_manager, "dm")

        assert_that(context.page_controller.activate(DEV, page2.page_id)).is_true()

        assert_that(tracker.mock_calls).is_equal_to(
            [
                call.pm.deactivate_card(old),
                call.dm.show_page(DEV, page2.page_id),
                call.dm.get_device_state(DEV),
                call.pm.store_settings(new, {"step": 2}),
                call.pm.set_bindings(new, {}),
                call.dm.get_device_state().has_image(0),
                call.pm.activate_card(new, {}, draw=True),
                call.pm.refresh_page_dependent(DEV),
            ]
        )
        assert_that(context.page_controller.active_page_id(DEV)).is_equal_to(page2.page_id)
        assert_that(_events(context)[-1]).is_equal_to((PAGES_CHANGED_EVENT, {"device_id": DEV}))

    def test_show_passes_stored_bindings_before_activation(self, context) -> None:
        page2 = context.page_store.add_page(DEV)
        target = _target(0, "new")
        bindings = {"press": {"action": "com.p.act", "args": {"x": 1}}}
        context.key_assignment_store.assign(DEV, page2.page_id, 0, target, {}, bindings)
        tracker = MagicMock()
        tracker.attach_mock(context.plugin_manager, "pm")

        context.page_controller.activate(DEV, page2.page_id)

        names = [c[0] for c in tracker.mock_calls]
        assert_that(names.index("pm.set_bindings")).is_less_than(names.index("pm.activate_card"))
        context.plugin_manager.set_bindings.assert_called_once_with(target, bindings)

    def test_activating_current_page_does_not_redraw(self, context):
        assert_that(context.page_controller.activate(DEV, PAGE1)).is_true()

        context.device_manager.show_page.assert_not_called()

    def test_unknown_page_is_refused(self, context):
        assert_that(context.page_controller.activate(DEV, "nope")).is_false()
        context.event_bus.push.assert_not_called()

    def test_unavailable_device_still_switches(self, context):
        page2 = context.page_store.add_page(DEV)
        context.device_manager.show_page.return_value = False

        assert_that(context.page_controller.activate(DEV, page2.page_id)).is_true()
        assert_that(context.page_controller.active_page_id(DEV)).is_equal_to(page2.page_id)


class TestRestore:
    def test_shows_active_page_and_activates_its_actions(self, context):
        target = _target(3, "c3")
        context.key_assignment_store.assign(DEV, PAGE1, 3, target, {"x": 1})

        context.page_controller.restore(DEV)

        context.device_manager.show_page.assert_called_once_with(DEV, PAGE1)
        context.plugin_manager.store_settings.assert_called_once_with(target, {"x": 1})
        context.plugin_manager.activate_card.assert_called_once_with(target, {}, draw=True)


class TestEditing:
    def test_add_rename_set_default_notify(self, context):
        page = context.page_controller.add_page(DEV)

        assert_that(context.page_controller.rename_page(DEV, page.page_id, "Media")).is_true()
        assert_that(context.page_controller.set_default_page(DEV, page.page_id)).is_true()

        assert_that(_events(context)).is_length(3)
        assert_that(context.page_store.default_page_id(DEV)).is_equal_to(page.page_id)
        context.device_manager.show_page.assert_not_called()

    def test_move_page_notifies_only_when_the_order_changed(self, context):
        second = context.page_store.add_page(DEV)

        assert_that(context.page_controller.move_page(DEV, second.page_id, 0)).is_true()
        assert_that([name for name, _ in _events(context)]).is_equal_to(["pages_changed"])

        context.event_bus.push.reset_mock()
        assert_that(context.page_controller.move_page(DEV, second.page_id, 0)).is_true()
        assert_that(context.page_controller.move_page(DEV, "nope", 0)).is_false()
        context.event_bus.push.assert_not_called()

    def test_failed_edits_do_not_notify(self, context):
        assert_that(context.page_controller.rename_page(DEV, "nope", "x")).is_false()
        assert_that(context.page_controller.set_default_page(DEV, "nope")).is_false()

        context.event_bus.push.assert_not_called()


class TestDeletePage:
    def test_deleting_inactive_page_removes_its_data(self, context):
        page2 = context.page_store.add_page(DEV)
        context.key_assignment_store.assign(DEV, page2.page_id, 0, _target(0, "c"), {})
        context.device_state_store.save_key_label(DEV, page2.page_id, 0, "x")

        assert_that(context.page_controller.delete_page(DEV, page2.page_id)).is_true()

        assert_that(context.key_assignment_store.all_for_page(DEV, page2.page_id)).is_empty()
        assert_that(context.device_state_store.get_all_key_labels(DEV, page2.page_id)).is_empty()
        assert_that(context.page_store.get(DEV, page2.page_id)).is_none()
        context.plugin_manager.deactivate_card.assert_not_called()
        context.device_manager.show_page.assert_not_called()

    def test_deleting_active_page_moves_to_next_page(self, context):
        page2 = context.page_store.add_page(DEV)
        page3 = context.page_store.add_page(DEV)
        context.page_controller.activate(DEV, page2.page_id)
        live = _target(0, "live")
        context.key_assignment_store.assign(DEV, page2.page_id, 0, live, {})

        context.page_controller.delete_page(DEV, page2.page_id)

        context.plugin_manager.deactivate_card.assert_called_once_with(live)
        assert_that(context.page_controller.active_page_id(DEV)).is_equal_to(page3.page_id)

    def test_deleting_active_last_page_moves_to_previous_page(self, context):
        page2 = context.page_store.add_page(DEV)
        context.page_controller.activate(DEV, page2.page_id)

        context.page_controller.delete_page(DEV, page2.page_id)

        assert_that(context.page_controller.active_page_id(DEV)).is_equal_to(PAGE1)
        context.device_manager.show_page.assert_called_with(DEV, PAGE1)

    def test_cannot_delete_last_or_unknown_page(self, context):
        assert_that(context.page_controller.delete_page(DEV, PAGE1)).is_false()
        assert_that(context.page_controller.delete_page(DEV, "nope")).is_false()
        context.event_bus.push.assert_not_called()

    def test_active_page_falls_back_if_deleted_elsewhere(self, context):
        page2 = context.page_store.add_page(DEV)
        context.page_controller.activate(DEV, page2.page_id)
        context.page_store.delete_page(DEV, page2.page_id)

        assert_that(context.page_controller.active_page_id(DEV)).is_equal_to(PAGE1)


class TestNotifyRefreshesPlugins:
    @pytest.mark.parametrize("operation", ["add", "rename", "delete", "activate"])
    def test_notify_refreshes_the_plugin_manager(self, context, operation):
        second = context.page_store.add_page(DEV)
        manager = cast(MagicMock, context.plugin_manager)
        manager.refresh_page_dependent.reset_mock()
        lock_free: list[bool] = []

        def _probe(_device_id: str) -> None:
            def _try() -> None:
                lock = context.page_controller._lock  # pylint: disable=protected-access
                got = lock.acquire(blocking=False)
                lock_free.append(got)
                if got:
                    lock.release()

            thread = threading.Thread(target=_try)
            thread.start()
            thread.join()

        manager.refresh_page_dependent.side_effect = _probe
        controller = context.page_controller
        if operation == "add":
            controller.add_page(DEV)
        elif operation == "rename":
            controller.rename_page(DEV, second.page_id, "Renamed")
        elif operation == "delete":
            controller.delete_page(DEV, second.page_id)
        else:
            controller.activate(DEV, second.page_id)

        assert_that(manager.refresh_page_dependent.call_args_list).is_equal_to([call(DEV)])
        assert_that(lock_free).is_equal_to([True])


def _names(events):
    return [name for name, _ in events]


class TestProfiles:
    def test_activate_profile_shows_its_default_page_and_hides_old_actions(self, context):
        profile = context.page_controller.add_profile(DEV)
        page = context.page_store.list_pages(DEV, profile.profile_id)[0]
        old, new = _target(0, "old"), _target(0, "new")
        context.key_assignment_store.assign(DEV, PAGE1, 0, old, {})
        context.key_assignment_store.assign(DEV, page.page_id, 0, new, {"n": 1})
        context.event_bus.push.reset_mock()

        assert_that(context.page_controller.activate_profile(DEV, profile.profile_id)).is_true()

        context.plugin_manager.deactivate_card.assert_called_once_with(old)
        context.device_manager.show_page.assert_called_with(DEV, page.page_id)
        context.plugin_manager.activate_card.assert_called_once_with(new, {}, draw=True)
        assert_that(context.page_controller.active_profile_id(DEV)).is_equal_to(profile.profile_id)
        assert_that(context.page_controller.active_page_id(DEV)).is_equal_to(page.page_id)
        assert_that(_names(_events(context))).contains(PAGES_CHANGED_EVENT, PROFILES_CHANGED_EVENT)

    def test_activating_the_active_profile_is_a_no_op(self, context):
        context.event_bus.push.reset_mock()

        assert_that(context.page_controller.activate_profile(DEV, 0)).is_true()

        context.device_manager.show_page.assert_not_called()
        context.event_bus.push.assert_not_called()

    def test_activate_unknown_profile_returns_false(self, context):
        assert_that(context.page_controller.activate_profile(DEV, 9)).is_false()
        context.event_bus.push.assert_not_called()

    def test_activate_page_in_other_profile_switches_profile(self, context):
        profile = context.page_controller.add_profile(DEV)
        page = context.page_store.add_page(DEV, "Second", profile_id=profile.profile_id)

        assert_that(context.page_controller.activate(DEV, page.page_id)).is_true()

        assert_that(context.page_controller.active_profile_id(DEV)).is_equal_to(profile.profile_id)
        assert_that(context.page_controller.active_page_id(DEV)).is_equal_to(page.page_id)
        assert_that(_names(_events(context))).contains(PROFILES_CHANGED_EVENT)

    def test_device_pages_snapshot_is_the_active_profiles(self, context):
        profile = context.page_controller.add_profile(DEV)
        context.page_controller.activate_profile(DEV, profile.profile_id)

        snapshot = context.page_controller.device_pages(DEV)

        assert_that(snapshot.profile_id).is_equal_to(profile.profile_id)
        assert_that(len(snapshot.pages)).is_equal_to(1)
        assert_that(snapshot.active_page_id).is_equal_to(snapshot.pages[0].page_id)

    def test_device_profiles_snapshot(self, context):
        profile = context.page_controller.add_profile(DEV, "Media")

        snapshot = context.page_controller.device_profiles(DEV)

        assert_that([p.name for p in snapshot.profiles]).is_equal_to(["Default", "Media"])
        assert_that(snapshot.active_profile_id).is_equal_to(0)
        assert_that(profile.profile_id).is_equal_to(1)

    def test_add_rename_icon_notify_both_events_and_refresh(self, context):
        profile = context.page_controller.add_profile(DEV)
        assert_that(context.page_controller.rename_profile(DEV, profile.profile_id, "Work")).is_true()
        assert_that(context.page_controller.set_profile_icon(DEV, profile.profile_id, "decker:logo")).is_true()

        names = _names(_events(context))
        assert_that(names.count(PAGES_CHANGED_EVENT)).is_equal_to(3)
        assert_that(names.count(PROFILES_CHANGED_EVENT)).is_equal_to(3)
        assert_that(context.plugin_manager.refresh_page_dependent.call_count).is_equal_to(3)

    def test_failed_profile_edits_do_not_notify(self, context):
        assert_that(context.page_controller.rename_profile(DEV, 9, "x")).is_false()
        assert_that(context.page_controller.set_profile_icon(DEV, 9, "")).is_false()
        context.event_bus.push.assert_not_called()

    def test_delete_active_profile_switches_to_next_and_cleans_up(self, context):
        second = context.page_controller.add_profile(DEV)
        third = context.page_controller.add_profile(DEV)
        context.page_controller.activate_profile(DEV, second.profile_id)
        page = context.page_store.list_pages(DEV, second.profile_id)[0]
        live = _target(0, "live")
        context.key_assignment_store.assign(DEV, page.page_id, 0, live, {})
        context.device_state_store.save_key_label(DEV, page.page_id, 0, "x")

        assert_that(context.page_controller.delete_profile(DEV, second.profile_id)).is_true()

        context.plugin_manager.deactivate_card.assert_called_once_with(live)
        assert_that(context.page_controller.active_profile_id(DEV)).is_equal_to(third.profile_id)
        assert_that(context.key_assignment_store.all_for_page(DEV, page.page_id)).is_empty()
        assert_that(context.device_state_store.get_all_key_labels(DEV, page.page_id)).is_empty()
        assert_that(context.page_store.get(DEV, page.page_id)).is_none()

    def test_delete_active_last_profile_switches_to_previous(self, context):
        second = context.page_controller.add_profile(DEV)
        context.page_controller.activate_profile(DEV, second.profile_id)

        context.page_controller.delete_profile(DEV, second.profile_id)

        assert_that(context.page_controller.active_profile_id(DEV)).is_equal_to(0)
        assert_that(context.page_controller.active_page_id(DEV)).is_equal_to(PAGE1)

    def test_deleting_an_inactive_profile_leaves_the_active_page_alone(self, context):
        second = context.page_controller.add_profile(DEV)
        context.device_manager.show_page.reset_mock()

        assert_that(context.page_controller.delete_profile(DEV, second.profile_id)).is_true()

        context.device_manager.show_page.assert_not_called()
        context.plugin_manager.deactivate_card.assert_not_called()
        assert_that(context.page_controller.active_page_id(DEV)).is_equal_to(PAGE1)

    def test_cannot_delete_last_or_unknown_profile(self, context):
        assert_that(context.page_controller.delete_profile(DEV, 0)).is_false()
        context.page_controller.add_profile(DEV)
        assert_that(context.page_controller.delete_profile(DEV, 7)).is_false()

    def test_clone_copies_keys_with_new_contexts_and_does_not_activate(self, context):
        original = _target(0, "orig")
        context.key_assignment_store.assign(DEV, PAGE1, 0, original, {"n": 1})
        context.device_state_store.save_key_label(DEV, PAGE1, 0, "lbl")
        context.device_manager.show_page.reset_mock()

        clone = context.page_controller.clone_profile(DEV, 0)

        assert_that(clone.name).is_equal_to("Default copy")
        copied_page = context.page_store.list_pages(DEV, clone.profile_id)[0]
        copied = context.key_assignment_store.all_for_page(DEV, copied_page.page_id)
        assert_that(copied[0][1].settings).is_equal_to({"n": 1})
        assert_that(copied[0][1].target.context_id).is_not_equal_to("orig")
        assert_that(context.device_state_store.get_key_label(DEV, copied_page.page_id, 0)).is_equal_to("lbl")
        assert_that(context.page_controller.active_profile_id(DEV)).is_equal_to(0)
        context.device_manager.show_page.assert_not_called()
        assert_that(context.key_assignment_store.all_for_page(DEV, PAGE1)[0][1].target.context_id).is_equal_to("orig")

    def test_clone_unknown_profile_returns_none(self, context):
        assert_that(context.page_controller.clone_profile(DEV, 9)).is_none()

    def test_restore_shows_active_profiles_default_page(self, context):
        profile = context.page_store.add_profile(DEV)
        context.page_store.set_active_profile(DEV, profile.profile_id)
        page = context.page_store.list_pages(DEV, profile.profile_id)[0]

        context.page_controller.restore(DEV)

        context.device_manager.show_page.assert_called_once_with(DEV, page.page_id)

    def test_deleting_pages_in_an_inactive_profile_keeps_the_active_page(self, context):
        profile = context.page_controller.add_profile(DEV)
        extra = context.page_store.add_page(DEV, profile_id=profile.profile_id)

        assert_that(context.page_controller.delete_page(DEV, extra.page_id)).is_true()

        assert_that(context.page_controller.active_page_id(DEV)).is_equal_to(PAGE1)

    def test_clone_points_page_settings_at_the_copied_pages(self, context):
        second = context.page_store.add_page(DEV, "Second")
        context.key_assignment_store.assign(DEV, PAGE1, 0, _target(0, "go"), {"where": second.page_id})
        other = context.page_controller.add_profile(DEV)
        foreign = context.page_store.list_pages(DEV, other.profile_id)[0]
        context.key_assignment_store.assign(DEV, PAGE1, 1, _target(1, "far"), {"where": foreign.page_id})

        clone = context.page_controller.clone_profile(DEV, 0)

        pages = context.page_store.list_pages(DEV, clone.profile_id)
        copied = dict(context.key_assignment_store.all_for_page(DEV, pages[0].page_id))
        assert_that(copied[0].settings["where"]).is_equal_to(pages[1].page_id)
        assert_that(copied[1].settings["where"]).is_equal_to(foreign.page_id)

    def test_active_page_is_read_without_the_lock_during_a_profile_switch(self, context):
        controller = context.page_controller
        profile = context.page_store.add_profile(DEV)
        expected = controller.active_page_id(DEV)
        context.page_store.set_active_profile(DEV, profile.profile_id)  # the window inside a switch
        result: list[str] = []

        with controller._lock:  # pylint: disable=protected-access
            thread = threading.Thread(target=lambda: result.append(controller.active_page_id(DEV)))
            thread.start()
            thread.join(2)
            finished = not thread.is_alive()

        assert_that(finished).is_true()
        assert_that(result).is_equal_to([expected])


class TestShowWithoutBareRedraw:
    def test_keys_with_a_cached_image_are_activated_without_drawing(self, context):
        cached, fresh = _target(0, "cached"), _target(1, "fresh")
        context.key_assignment_store.assign(DEV, PAGE1, 0, cached, {})
        context.key_assignment_store.assign(DEV, PAGE1, 1, fresh, {})
        context.device_manager.get_device_state.return_value.has_image.side_effect = lambda key: key == 0

        context.page_controller.restore(DEV)

        assert_that(context.plugin_manager.activate_card.call_args_list).is_equal_to(
            [call(cached, {}, draw=False), call(fresh, {}, draw=True)]
        )

    def test_an_unavailable_device_always_draws(self, context):
        target = _target(0, "c")
        context.key_assignment_store.assign(DEV, PAGE1, 0, target, {})
        context.device_manager.show_page.return_value = False
        context.device_manager.get_device_state.return_value.has_image.return_value = True

        context.page_controller.restore(DEV)

        context.plugin_manager.activate_card.assert_called_once_with(target, {}, draw=True)


class TestAppRuleCascade:
    @staticmethod
    def _rule(profile_id: int, page_id: str = "") -> AppRule:
        return AppRule("", "R", True, [MatchCondition("executable", "contains", "x")], profile_id, page_id)

    def test_delete_profile_removes_rules_and_pushes_app_rules_changed(self, context):
        profile = context.page_controller.add_profile(DEV, "Other")
        store = context.app_rule_store
        store.add_rule(DEV, self._rule(profile.profile_id))
        store.set_default_rule(DEV, DefaultRule(True, profile.profile_id))
        cast(MagicMock, context.event_bus.push).reset_mock()

        assert_that(context.page_controller.delete_profile(DEV, profile.profile_id)).is_true()

        assert_that(store.list_rules(DEV).rules).is_empty()
        assert_that(store.list_rules(DEV).default_rule).is_equal_to(DefaultRule())
        assert_that(_events(context)).contains(("app_rules_changed", {"device_id": DEV}))

    def test_deleting_profile_zero_resets_default_rule_to_the_lowest_remaining_profile(self, context):
        second = context.page_controller.add_profile(DEV, "Second")
        context.page_controller.add_profile(DEV, "Third")
        store = context.app_rule_store
        store.set_default_rule(DEV, DefaultRule(True, 0))

        assert_that(context.page_controller.delete_profile(DEV, 0)).is_true()

        assert_that(store.list_rules(DEV).default_rule).is_equal_to(DefaultRule(False, second.profile_id, ""))

    def test_deleting_another_profile_leaves_a_default_rule_alone(self, context):
        second = context.page_controller.add_profile(DEV, "Second")
        third = context.page_controller.add_profile(DEV, "Third")
        store = context.app_rule_store
        store.set_default_rule(DEV, DefaultRule(True, third.profile_id))

        context.page_controller.delete_profile(DEV, second.profile_id)

        assert_that(store.list_rules(DEV).default_rule).is_equal_to(DefaultRule(True, third.profile_id, ""))

    def test_delete_profile_without_rules_pushes_no_app_rules_changed(self, context):
        profile = context.page_controller.add_profile(DEV, "Other")

        context.page_controller.delete_profile(DEV, profile.profile_id)

        assert_that([name for name, _ in _events(context)]).does_not_contain("app_rules_changed")

    def test_delete_page_clears_rule_page_ids(self, context):
        page2 = context.page_store.add_page(DEV)
        store = context.app_rule_store
        store.add_rule(DEV, self._rule(0, page2.page_id))
        cast(MagicMock, context.event_bus.push).reset_mock()

        assert_that(context.page_controller.delete_page(DEV, page2.page_id)).is_true()

        assert_that(store.list_rules(DEV).rules[0].page_id).is_equal_to("")
        assert_that(_events(context)).contains(("app_rules_changed", {"device_id": DEV}))

    def test_deleting_works_without_a_rule_store(self, context):
        context.app_rule_store = None
        profile = context.page_controller.add_profile(DEV, "Other")
        page = context.page_store.add_page(DEV)

        assert_that(context.page_controller.delete_page(DEV, page.page_id)).is_true()
        assert_that(context.page_controller.delete_profile(DEV, profile.profile_id)).is_true()
