import json
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
from decker.server.appearance import compose_key_image, resolve_appearance
from tests.server.test_plugins import (
    _COUNTER,
    COUNTER_TARGET,
    REC_TARGET,
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


def _live(env: Env, context: str = "ctx-1"):
    return env.manager._live[context]  # pylint: disable=protected-access


def _registered(env: Env, context: str = "ctx-1") -> frozenset[Gesture]:
    return env.manager._registered(_live(env, context))  # pylint: disable=protected-access


def _tap(env: Env, target=COUNTER_TARGET) -> None:
    env.manager.dispatch_key_down(target)
    env.manager.dispatch_key_up(target)


def _counter_ready(env: Env) -> None:
    env.manager.activate_card(COUNTER_TARGET)
    assert _wait_for(env.counter_drawn)


class TestDispatch:
    def test_press_runs_the_default_bound_action_in_the_plugin(self, env):
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

    def test_cards_no_longer_receive_press_events(self, env):
        _counter_ready(env)

        _tap(env)
        assert _wait_for(lambda: _counter_log(env, "run_action"))
        env.manager.dispatch_key_down(COUNTER_TARGET)
        time.sleep(0.7)
        env.manager.dispatch_key_up(COUNTER_TARGET)
        assert _wait_for(lambda: len(_counter_log(env, "run_action")) == 2)

        events = {m["event"] for m in _counter_log(env)}
        assert_that(events).does_not_contain("press", "double_press", "long_press")

    def test_key_down_and_up_still_reach_the_card(self, env):
        _counter_ready(env)

        _tap(env)

        assert _wait_for(lambda: _counter_log(env, "key_up"))
        assert_that(_counter_log(env, "key_down")).is_not_empty()

    def test_nothing_gesture_is_not_registered(self, env):
        _counter_ready(env)

        assert_that(_registered(env)).is_equal_to(frozenset({Gesture.PRESS, Gesture.LONG_PRESS}))

    def test_press_fires_at_once_when_double_press_is_nothing(self, env):
        _counter_ready(env)
        first = env.image()

        _tap(env)

        assert _wait_for(lambda: env.image() != first, timeout=0.15)

    def test_press_is_held_back_when_double_press_is_bound(self, env):
        env.manager.set_bindings(COUNTER_TARGET, {"double_press": {"action": _INCREMENT, "args": {"step": 5}}})
        _counter_ready(env)
        first = env.image()

        _tap(env)
        time.sleep(0.15)
        assert_that(env.image()).is_equal_to(first)

        assert _wait_for(lambda: env.image() != first)

    def test_stored_binding_overrides_default(self, env):
        env.manager.set_bindings(COUNTER_TARGET, {"double_press": {"action": _INCREMENT, "args": {"step": 5}}})
        _counter_ready(env)

        _tap(env)
        _tap(env)

        assert _wait_for(lambda: _counter_log(env, "run_action"))
        payload = _counter_log(env, "run_action")[0]["payload"]
        assert_that(payload["input"]).is_equal_to("double_press")
        assert_that(payload["args"]).is_equal_to({"step": 5})

    def test_invalid_stored_args_raise_action_failed_with_field_error(self, env):
        env.manager.set_bindings(COUNTER_TARGET, {"press": {"action": _INCREMENT, "args": {"step": 0}}})
        _counter_ready(env)

        _tap(env)

        assert _wait_for(lambda: env.bus.of("action_failed"))
        failure = env.bus.of("action_failed")[0]
        assert_that(failure["action_id"]).is_equal_to(_INCREMENT)
        assert_that(failure["error"]).contains("step")
        assert_that(_counter_log(env, "run_action")).is_empty()

    def test_static_card_binding_a_global_action_runs_it(self, env):
        env.manager.set_bindings(_BLANK_TARGET, {"press": {"action": _BUMP}})
        env.manager.activate_card(_BLANK_TARGET)

        _tap(env, _BLANK_TARGET)

        assert _wait_for(lambda: _counter_log(env, "run_action"))
        payload = _counter_log(env, "run_action")[0]["payload"]
        assert_that(payload["action"]).is_equal_to(_BUMP)
        assert_that(payload["card"]["context"]).is_equal_to("ctx-blank")
        assert_that(payload["card"]["plugin"]).is_equal_to("com.decker.blank")

    def test_foreign_plugin_scoped_binding_is_not_registered(self, env):
        env.manager.set_bindings(REC_TARGET, {"press": {"action": _INCREMENT}})
        env.manager.activate_card(REC_TARGET)

        _tap(env, REC_TARGET)

        assert_that(_registered(env)).is_empty()
        time.sleep(0.2)
        assert_that(_counter_log(env)).is_empty()
        assert_that(_live(env).bindings["press"].action).is_equal_to(_INCREMENT)  # kept as stored

    def test_set_gestures_is_ignored(self, env):
        env.manager.activate_card(REC_TARGET)

        env.manager._on_plugin_message(  # pylint: disable=protected-access
            "com.decker.rec", Message("set_gestures", "ctx-1", {"gestures": ["press", "long_press"]})
        )

        assert_that(_registered(env)).is_empty()


class TestBindingChanges:
    def test_set_bindings_on_live_card_reregisters_and_sends_settings_changed_with_bindings(self, env):
        _counter_ready(env)

        env.manager.set_bindings(COUNTER_TARGET, {"press": {"action": _RESET}, "long_press": _NOTHING})

        assert _wait_for(lambda: _counter_log(env, "settings_changed"))
        sent = _counter_log(env, "settings_changed")[-1]["payload"]["bindings"]
        assert_that(sent["press"]).is_equal_to({"action": _RESET, "args": {}})
        assert_that(sent["long_press"]["action"]).is_equal_to(NOTHING_ACTION_ID)
        assert_that(_registered(env)).is_equal_to(frozenset({Gesture.PRESS}))

    def test_binding_to_uninstalled_action_acts_as_nothing_and_survives_reinstall(self, env):
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

    def test_untouched_gesture_follows_new_manifest_default_and_explicit_nothing_stays(self, env):
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
    def test_blank_shows_press_action_icon_and_name(self, env):
        env.manager.activate_card(_BLANK_TARGET)
        plain = env.image(3)
        assert_that(plain).is_equal_to(compose_key_image(resolve_appearance({}, {}), None, ""))

        env.manager.set_bindings(_BLANK_TARGET, {"press": {"action": _BUMP}})

        icon = read_icon(env.plugins_dir / _COUNTER, "icons/counter")
        expected = compose_key_image(resolve_appearance({}, {}), icon, "Bump")
        assert _wait_for(lambda: env.image(3) == expected)

    def test_blank_bound_to_do_nothing_keeps_the_plain_face(self, env):
        env.manager.set_bindings(_BLANK_TARGET, {"press": _NOTHING})
        env.manager.activate_card(_BLANK_TARGET)

        assert_that(env.image(3)).is_equal_to(compose_key_image(resolve_appearance({}, {}), None, ""))

    def test_blank_face_follows_the_action_being_uninstalled(self, env):
        env.manager.set_bindings(_BLANK_TARGET, {"press": {"action": _BUMP}})
        env.manager.activate_card(_BLANK_TARGET)
        plain = compose_key_image(resolve_appearance({}, {}), None, "")
        assert_that(env.image(3)).is_not_equal_to(plain)

        env.manager.remove_plugin(_COUNTER)

        assert_that(env.image(3)).is_equal_to(plain)


class TestWillAppear:
    def test_will_appear_carries_bindings_and_size(self, env):
        env.manager.set_bindings(REC_TARGET, {"press": {"action": _INCREMENT, "args": {"step": 2}}})
        env.manager.activate_card(REC_TARGET)

        assert _wait_for(lambda: env.received("will_appear"))
        payload = env.received("will_appear")[0]["payload"]
        assert_that(payload["size"]).is_equal_to([72, 72])
        assert_that(payload["bindings"]).contains_key("press", "double_press", "long_press")
        assert_that(payload["bindings"]["press"]).is_equal_to({"action": _INCREMENT, "args": {"step": 2}})
        assert_that(payload["bindings"]["long_press"]).is_equal_to({"action": NOTHING_ACTION_ID, "args": {}})
