import json
import threading
import time
from dataclasses import replace
from typing import Any

import pytest
from assertpy import assert_that

from decker.plugin.bindings import NOTHING_ACTION_ID, Binding
from decker.plugin.gestures import Gesture
from decker.plugin.icons import read_icon
from decker.plugin.loader import InstalledPlugin
from decker.plugin.protocol import Message
from decker.plugin.target import CardTarget
from decker.server.action_calls import ActionCall
from decker.server.actions import ActionRunner, _Pending
from decker.server.appearance import compose_key_image, resolve_appearance
from tests.server.test_plugins import (
    _COUNTER,
    COUNTER_TARGET,
    REC_TARGET,
    STATIC_TARGET,
    Env,
    _target,
    _wait_for,
)

_INCREMENT = "com.decker.testfixture.increment"
_RESET = "com.decker.testfixture.reset"
_BUMP = "com.decker.testfixture.bump"
_BLANK_TARGET = _target("com.decker.blank", "com.decker.blank.blank", "ctx-blank", 3)
_NOTHING = {"action": NOTHING_ACTION_ID}


@pytest.fixture(name="env")
def env_fixture(tmp_path):
    environment = Env(tmp_path)
    yield environment
    environment.manager.destroy()


def _counter_log(env: Env, event: str | None = None) -> list[dict[str, Any]]:
    log = env.plugins_dir / _COUNTER / "received.log"
    if not log.exists():
        return []
    messages = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return [m for m in messages if event is None or m["event"] == event]


def _live(env: Env, context: str = "ctx-1") -> Any:
    return env.manager._live[context]  # pylint: disable=protected-access


def _registered(env: Env, context: str = "ctx-1") -> frozenset[Gesture]:
    return env.manager._registered(_live(env, context))  # pylint: disable=protected-access


def _tap(env: Env, target: CardTarget = COUNTER_TARGET) -> None:
    env.manager.dispatch_key_down(target)
    env.manager.dispatch_key_up(target)


def _counter_ready(env: Env) -> None:
    env.manager.activate_card(COUNTER_TARGET)
    assert _wait_for(env.counter_drawn)


class TestDispatch:
    def test_press_runs_the_default_bound_action_in_the_plugin(self, env) -> None:
        _counter_ready(env)
        first = env.image()

        _tap(env)

        assert _wait_for(lambda: env.image() != first)
        payload = _counter_log(env, "run_action")[0]["payload"]
        assert_that(payload["action"]).is_equal_to(_INCREMENT)
        assert_that(payload["input"]).is_equal_to("press")
        assert_that(payload["card"]["context"]).is_equal_to("ctx-1")
        assert_that(payload["card"]["plugin"]).is_equal_to(_COUNTER)
        assert_that(payload["card"]["key"]).is_equal_to({"index": 0, "column": 0, "row": 0})
        assert_that(payload["card"]["size"]).is_equal_to([72, 72])

    def test_cards_no_longer_receive_press_events(self, env) -> None:
        _counter_ready(env)

        _tap(env)
        assert _wait_for(lambda: _counter_log(env, "run_action"))
        env.manager.dispatch_key_down(COUNTER_TARGET)
        time.sleep(0.7)
        env.manager.dispatch_key_up(COUNTER_TARGET)
        assert _wait_for(lambda: len(_counter_log(env, "run_action")) == 2)

        events = {m["event"] for m in _counter_log(env)}
        assert_that(events).does_not_contain("press", "double_press", "long_press")

    def test_key_down_and_up_still_reach_the_card(self, env) -> None:
        _counter_ready(env)

        _tap(env)

        assert _wait_for(lambda: _counter_log(env, "key_up"))
        assert_that(_counter_log(env, "key_down")).is_not_empty()

    def test_nothing_gesture_is_not_registered(self, env) -> None:
        _counter_ready(env)

        assert_that(_registered(env)).is_equal_to(frozenset({Gesture.PRESS, Gesture.LONG_PRESS}))

    def test_press_fires_at_once_when_double_press_is_nothing(self, env) -> None:
        _counter_ready(env)
        first = env.image()

        _tap(env)

        assert _wait_for(lambda: env.image() != first, timeout=0.15)

    def test_press_is_held_back_when_double_press_is_bound(self, env) -> None:
        env.manager.set_bindings(COUNTER_TARGET, {"double_press": {"action": _INCREMENT, "args": {"step": 5}}})
        _counter_ready(env)
        first = env.image()

        _tap(env)
        time.sleep(0.15)
        assert_that(env.image()).is_equal_to(first)

        assert _wait_for(lambda: env.image() != first)

    def test_stored_binding_overrides_default(self, env) -> None:
        env.manager.set_bindings(COUNTER_TARGET, {"double_press": {"action": _INCREMENT, "args": {"step": 5}}})
        _counter_ready(env)

        _tap(env)
        _tap(env)

        assert _wait_for(lambda: _counter_log(env, "run_action"))
        payload = _counter_log(env, "run_action")[0]["payload"]
        assert_that(payload["input"]).is_equal_to("double_press")
        assert_that(payload["args"]).is_equal_to({"step": 5})

    def test_invalid_stored_args_raise_action_failed_with_field_error(self, env) -> None:
        env.manager.set_bindings(COUNTER_TARGET, {"press": {"action": _INCREMENT, "args": {"step": 0}}})
        _counter_ready(env)

        _tap(env)

        assert _wait_for(lambda: env.bus.of("action_failed"))
        failure = env.bus.of("action_failed")[0]
        assert_that(failure["action_id"]).is_equal_to(_INCREMENT)
        assert_that(failure["error"]).contains("step")
        assert_that(_counter_log(env, "run_action")).is_empty()

    def test_static_card_binding_a_global_action_runs_it(self, env) -> None:
        env.manager.set_bindings(_BLANK_TARGET, {"press": {"action": _BUMP}})
        env.manager.activate_card(_BLANK_TARGET)

        _tap(env, _BLANK_TARGET)

        assert _wait_for(lambda: _counter_log(env, "run_action"))
        payload = _counter_log(env, "run_action")[0]["payload"]
        assert_that(payload["action"]).is_equal_to(_BUMP)
        assert_that(payload["card"]["context"]).is_equal_to("ctx-blank")
        assert_that(payload["card"]["plugin"]).is_equal_to("com.decker.blank")

    def test_foreign_plugin_scoped_binding_is_not_registered(self, env) -> None:
        env.manager.set_bindings(REC_TARGET, {"press": {"action": _INCREMENT}})
        env.manager.activate_card(REC_TARGET)

        _tap(env, REC_TARGET)

        assert_that(_registered(env)).is_empty()
        time.sleep(0.2)
        assert_that(_counter_log(env)).is_empty()
        assert_that(_live(env).bindings["press"].action).is_equal_to(_INCREMENT)  # kept as stored

    def test_set_gestures_is_ignored(self, env) -> None:
        _counter_ready(env)
        first = env.image()

        env.manager._on_plugin_message(  # pylint: disable=protected-access
            _COUNTER, Message("set_gestures", "ctx-1", {"gestures": ["double_press"]})
        )

        assert_that(_registered(env)).is_equal_to(frozenset({Gesture.PRESS, Gesture.LONG_PRESS}))
        _tap(env)
        assert _wait_for(lambda: env.image() != first, timeout=0.3)


class TestBindingChanges:
    def test_set_bindings_on_live_card_reregisters_and_sends_settings_changed_with_bindings(self, env) -> None:
        _counter_ready(env)

        env.manager.set_bindings(COUNTER_TARGET, {"press": {"action": _RESET}, "long_press": _NOTHING})

        assert _wait_for(lambda: _counter_log(env, "settings_changed"))
        sent = _counter_log(env, "settings_changed")[-1]["payload"]["bindings"]
        assert_that(sent["press"]).is_equal_to({"action": _RESET, "args": {}})
        assert_that(sent["long_press"]["action"]).is_equal_to(NOTHING_ACTION_ID)
        assert_that(_registered(env)).is_equal_to(frozenset({Gesture.PRESS}))

    def test_binding_to_uninstalled_action_acts_as_nothing_and_survives_reinstall(self, env) -> None:
        env.manager.set_bindings(_BLANK_TARGET, {"press": {"action": _BUMP}})
        env.manager.activate_card(_BLANK_TARGET)
        assert_that(_registered(env, "ctx-blank")).is_equal_to(frozenset({Gesture.PRESS}))
        plugin = env.manager._plugins[_COUNTER]  # pylint: disable=protected-access

        env.manager.remove_plugin(_COUNTER)
        _tap(env, _BLANK_TARGET)

        assert_that(_registered(env, "ctx-blank")).is_empty()
        assert_that(_live(env, "ctx-blank").bindings["press"].action).is_equal_to(_BUMP)
        time.sleep(0.2)
        assert_that(_counter_log(env, "run_action")).is_empty()

        env.manager.add_plugin(plugin)
        assert_that(_registered(env, "ctx-blank")).is_equal_to(frozenset({Gesture.PRESS}))
        _tap(env, _BLANK_TARGET)

        assert _wait_for(lambda: _counter_log(env, "run_action"))

    def test_untouched_gesture_follows_new_manifest_default_and_explicit_nothing_stays(self, env) -> None:
        env.manager.set_bindings(COUNTER_TARGET, {"long_press": _NOTHING})
        _counter_ready(env)
        plugin = env.manager._plugins[_COUNTER]  # pylint: disable=protected-access
        card = plugin.manifest.cards[0]
        changed_card = replace(
            card,
            bindings={
                "press": Binding(_RESET),
                "double_press": Binding(NOTHING_ACTION_ID),
                "long_press": Binding(_INCREMENT),
            },
        )
        changed = InstalledPlugin(replace(plugin.manifest, cards=(changed_card,)), plugin.plugin_dir)

        env.manager.deactivate_card(COUNTER_TARGET)
        env.manager.remove_plugin(_COUNTER)
        env.manager.add_plugin(changed)
        env.manager.activate_card(COUNTER_TARGET)

        bindings = _live(env).bindings
        assert_that(bindings["press"].action).is_equal_to(_RESET)
        assert_that(bindings["long_press"].is_nothing).is_true()
        assert_that(_registered(env)).is_equal_to(frozenset({Gesture.PRESS}))


class TestBlankFace:
    def test_blank_shows_press_action_icon_and_name(self, env) -> None:
        env.manager.activate_card(_BLANK_TARGET)
        plain = env.image(3)
        assert_that(plain).is_equal_to(compose_key_image(resolve_appearance({}, {}), None, ""))

        env.manager.set_bindings(_BLANK_TARGET, {"press": {"action": _BUMP}})

        icon = read_icon(env.plugins_dir / _COUNTER, "icons/counter")
        expected = compose_key_image(resolve_appearance({}, {}), icon, "Bump")
        assert _wait_for(lambda: env.image(3) == expected)

    def test_blank_bound_to_do_nothing_keeps_the_plain_face(self, env) -> None:
        env.manager.set_bindings(_BLANK_TARGET, {"press": _NOTHING})
        env.manager.activate_card(_BLANK_TARGET)

        assert_that(env.image(3)).is_equal_to(compose_key_image(resolve_appearance({}, {}), None, ""))

    def test_blank_face_follows_the_action_being_uninstalled(self, env) -> None:
        env.manager.set_bindings(_BLANK_TARGET, {"press": {"action": _BUMP}})
        env.manager.activate_card(_BLANK_TARGET)
        plain = compose_key_image(resolve_appearance({}, {}), None, "")
        assert_that(env.image(3)).is_not_equal_to(plain)

        env.manager.remove_plugin(_COUNTER)

        assert_that(env.image(3)).is_equal_to(plain)


class TestWillAppear:
    def test_will_appear_carries_bindings_and_size(self, env) -> None:
        env.manager.set_bindings(REC_TARGET, {"press": {"action": _INCREMENT, "args": {"step": 2}}})
        env.manager.activate_card(REC_TARGET)

        assert _wait_for(lambda: env.received("will_appear"))
        payload = env.received("will_appear")[0]["payload"]
        assert_that(payload["size"]).is_equal_to([72, 72])
        assert_that(payload["bindings"]).contains_key("press", "double_press", "long_press")
        assert_that(payload["bindings"]["press"]).is_equal_to({"action": _INCREMENT, "args": {"step": 2}})
        assert_that(payload["bindings"]["long_press"]).is_equal_to({"action": NOTHING_ACTION_ID, "args": {}})


class TestValidation:
    def test_card_settings_errors_are_keyed_by_field(self, env) -> None:
        errors = env.manager.validate_card_settings(COUNTER_TARGET, {"step": "many"})

        assert_that(errors).contains_key("step")
        assert_that(env.manager.validate_card_settings(COUNTER_TARGET, {"step": 3})).is_empty()

    def test_bad_args_are_keyed_by_gesture_and_arg(self, env) -> None:
        errors = env.manager.validate_bindings(COUNTER_TARGET, {"press": {"action": _INCREMENT, "args": {"step": "x"}}})

        assert_that(errors).contains_key("press.step")

    def test_a_foreign_plugin_scoped_action_is_refused_but_global_is_fine(self, env) -> None:
        stored = {"press": {"action": _INCREMENT}, "long_press": {"action": _BUMP}, "double_press": _NOTHING}

        errors = env.manager.validate_bindings(REC_TARGET, stored)

        assert_that(errors).contains_key("press").does_not_contain_key("long_press", "double_press")

    def test_an_uninstalled_action_and_do_nothing_are_not_errors(self, env) -> None:
        stored = {"press": {"action": "com.gone.action"}, "long_press": _NOTHING}

        assert_that(env.manager.validate_bindings(COUNTER_TARGET, stored)).is_empty()


class TestListings:
    def test_cards_carry_their_default_bindings(self, env) -> None:
        card = next(c for c in env.manager.list_cards() if c["card_uuid"] == COUNTER_TARGET.card_uuid)

        assert_that(card).does_not_contain_key("action")
        assert_that(card["bindings"]["press"]).is_equal_to({"action": _INCREMENT, "args": {}})
        assert_that(card["bindings"]["double_press"]).is_equal_to({"action": NOTHING_ACTION_ID, "args": {}})

    def test_actions_carry_scope_and_returns(self, env) -> None:
        listed = {a["action_uuid"]: a for a in env.manager.list_actions()}

        assert_that(listed[_BUMP]["scope"]).is_equal_to("global")
        assert_that(listed[_INCREMENT]["scope"]).is_equal_to("plugin")
        assert_that(listed[_INCREMENT]).contains_key("returns")


class _QuietSupervisor:
    """Takes requests and never answers them, so a call stays in flight until its timeout."""

    def is_failed(self, _plugin_id: str) -> bool:
        return False

    def acquire(self, _plugin_id: str) -> None:
        pass

    def release(self, _plugin_id: str) -> None:
        pass

    def send(self, _plugin_id: str, _message: Message) -> None:
        pass


def _foreign_call(env: Env, timeout: float | None = None) -> ActionCall:
    """A call to the counter plugin carrying the card REC_TARGET (owned by another plugin)."""
    card = {"context": REC_TARGET.context_id, "plugin": REC_TARGET.plugin_uuid}
    return ActionCall(_BUMP, {}, "plugin", card=card, device="dev-1", timeout=timeout)


def _in_flight(env: Env, request: str = "req-1") -> None:
    runner = env.manager._action_runner  # pylint: disable=protected-access
    runner._pending[request] = _Pending(_COUNTER, _foreign_call(env))  # pylint: disable=protected-access


def _foreign_send(env: Env, event: str, payload: dict[str, Any]) -> None:
    env.manager._route_context_message(  # pylint: disable=protected-access
        _COUNTER, Message(event, REC_TARGET.context_id, payload)
    )


class TestForeignAccess:
    def test_foreign_navigation_accepted_during_the_call(self, env) -> None:
        env.manager.activate_card(REC_TARGET)
        _in_flight(env)

        _foreign_send(env, "switch_page", {"page_id": "p1", "request": "req-1"})

        assert_that(env.pages.activations).is_equal_to([("dev-1", "p1")])

    def test_foreign_navigation_without_request_is_dropped(self, env) -> None:
        env.manager.activate_card(REC_TARGET)
        _in_flight(env)

        _foreign_send(env, "switch_page", {"page_id": "p1"})
        _foreign_send(env, "switch_page", {"page_id": "p1", "request": "other"})

        assert_that(env.pages.activations).is_empty()

    def test_foreign_access_ends_when_the_call_times_out(self, env) -> None:
        env.manager.activate_card(REC_TARGET)
        runner = ActionRunner(_QuietSupervisor(), lambda _a, _e: None, lambda _s, v, _d: dict(v))  # type: ignore[arg-type]
        runner._actions = env.manager._action_runner._actions  # pylint: disable=protected-access
        env.manager._action_runner = runner  # pylint: disable=protected-access
        outcome: list[Any] = []
        worker = threading.Thread(target=lambda: outcome.append(runner.execute(_foreign_call(env, 0.2))))
        worker.start()
        assert _wait_for(lambda: runner._pending)  # pylint: disable=protected-access
        request = next(iter(runner._pending))  # pylint: disable=protected-access

        _foreign_send(env, "switch_page", {"page_id": "p1", "request": request})
        worker.join(5)
        _foreign_send(env, "switch_page", {"page_id": "p3", "request": request})

        assert_that(outcome[0].error).is_equal_to("timed out")
        assert_that(env.pages.activations).is_equal_to([("dev-1", "p1")])

    def test_foreign_set_settings_is_always_refused(self, env) -> None:
        env.manager.activate_card(REC_TARGET)
        _in_flight(env)

        _foreign_send(env, "set_settings", {"settings": {"x": 1}, "request": "req-1"})

        assert_that(_live(env).settings).does_not_contain_key("x")

    def test_foreign_access_is_limited_to_the_card_the_call_carried(self, env) -> None:
        env.manager.activate_card(REC_TARGET)
        env.manager.activate_card(STATIC_TARGET)
        _in_flight(env)

        env.manager._route_context_message(  # pylint: disable=protected-access
            _COUNTER, Message("switch_page", STATIC_TARGET.context_id, {"page_id": "p1", "request": "req-1"})
        )

        assert_that(env.pages.activations).is_empty()

    def test_a_plugin_that_stopped_loses_its_access(self, env) -> None:
        env.manager.activate_card(REC_TARGET)
        _in_flight(env)

        env.manager._action_runner.plugin_stopped(_COUNTER)  # pylint: disable=protected-access
        _foreign_send(env, "switch_page", {"page_id": "p1", "request": "req-1"})

        assert_that(env.pages.activations).is_empty()

    def test_foreign_access_ends_when_the_result_arrives(self, env) -> None:
        env.manager.activate_card(REC_TARGET)
        _in_flight(env)

        env.manager._action_runner.on_result(
            _COUNTER, {"request": "req-1", "ok": True}
        )  # pylint: disable=protected-access
        _foreign_send(env, "switch_page", {"page_id": "p1", "request": "req-1"})

        assert_that(env.pages.activations).is_empty()

    def test_a_real_exit_ends_a_waiting_call_promptly(self, env) -> None:
        runner = ActionRunner(_QuietSupervisor(), lambda _a, _e: None, lambda _s, v, _d: dict(v))  # type: ignore[arg-type]
        runner._actions = env.manager._action_runner._actions  # pylint: disable=protected-access
        env.manager._action_runner = runner  # pylint: disable=protected-access
        outcome: list[Any] = []
        worker = threading.Thread(target=lambda: outcome.append(runner.execute(_foreign_call(env, 20.0))))
        worker.start()
        assert _wait_for(lambda: runner._pending)  # pylint: disable=protected-access

        env.manager._on_plugin_exit(_COUNTER)  # pylint: disable=protected-access
        worker.join(5)

        assert_that(outcome[0].error).is_equal_to("the plugin stopped")

    def test_a_late_exit_notification_spares_a_call_on_a_newer_process(self, env) -> None:
        _counter_ready(env)
        env.manager.activate_card(REC_TARGET)
        _in_flight(env)

        env.manager._on_plugin_exit(_COUNTER)  # pylint: disable=protected-access
        _foreign_send(env, "switch_page", {"page_id": "p1", "request": "req-1"})

        assert_that(env.pages.activations).is_equal_to([("dev-1", "p1")])

    def test_unknown_events_from_a_plugin_keep_the_warning_set_bounded(self, env) -> None:
        env.manager.activate_card(REC_TARGET)

        for number in range(50):
            _foreign_send(env, f"made_up_{number}", {})

        assert_that(env.manager._foreign_warned).is_equal_to({_COUNTER})  # pylint: disable=protected-access


class TestCallActionRouting:
    def test_call_action_from_a_plugin_reaches_the_runner(self, env) -> None:
        seen: list[tuple[str, Any]] = []
        runner = env.manager._action_runner  # pylint: disable=protected-access
        runner.on_plugin_call = lambda plugin_id, payload: seen.append((plugin_id, payload))

        env.manager._route_message(  # pylint: disable=protected-access
            _COUNTER, Message("call_action", None, {"request": "r", "action": _INCREMENT})
        )

        assert_that(seen).is_equal_to([(_COUNTER, {"request": "r", "action": _INCREMENT})])
