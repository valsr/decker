"""Per-card variables, status and slots through a real plugin process."""

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from assertpy import assert_that

from decker.plugin.target import CardTarget
from tests.server.test_actions_e2e import _TILE, _Env, _wait

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: interpreter symlink")

_SETVAR = "com.decker.acttest.setvar"
_ECHO = "com.decker.acttest.echo"
_BOOM = "com.decker.acttest.boom"
_SLOTS = "com.decker.acttest.slots"
_NOTHING = "com.decker.core.nothing"


@pytest.fixture(name="env")
def env_fixture(tmp_path: Path) -> Iterator[_Env]:
    environment = _Env(tmp_path)
    try:
        yield environment
    finally:
        environment.close()


def _vars(env: _Env, target: CardTarget = _TILE) -> dict[str, Any]:
    return env.manager.card_variables.snapshot(target.context_id)


def _bind(env: _Env, **bindings: dict[str, Any]) -> None:
    env.activate_tile()
    env.manager.set_bindings(_TILE, bindings)


def _set(name: str = "pid", value: str = "1", **extra: Any) -> dict[str, Any]:
    return {"action": _SETVAR, "args": {"name": name, "value": value, **extra}}


def _log(env: _Env, name: str) -> list[str]:
    return [line for line in env.read(name).splitlines() if line]


def _run_and_wait_status(env: _Env, binding: str = "press") -> dict[str, Any]:
    env.press(_TILE, hold=0.7 if binding == "long_press" else 0.0)
    assert_that(_wait(lambda: binding in _vars(env) and "status" in _vars(env)[binding])).is_true()
    return _vars(env)[binding]


class TestStatus:
    def test_a_returning_action_is_success(self, env: _Env) -> None:
        _bind(env, press=_set())

        assert_that(_run_and_wait_status(env)["status"]).is_equal_to("SUCCESS")

    def test_a_raising_action_is_failure(self, env: _Env) -> None:
        _bind(env, press={"action": _BOOM, "args": {}})

        assert_that(_run_and_wait_status(env)["status"]).is_equal_to("FAILURE")

    def test_refused_arguments_are_failure(self, env: _Env) -> None:
        _bind(env, press={"action": _ECHO, "args": {"text": 5}})

        assert_that(_run_and_wait_status(env)["status"]).is_equal_to("FAILURE")

    def test_do_nothing_leaves_no_status(self, env: _Env) -> None:
        _bind(env, press={"action": _NOTHING}, long_press=_set())

        env.press(_TILE, hold=0.7)

        assert_that(_wait(lambda: "long_press" in _vars(env))).is_true()
        assert_that(_vars(env)).does_not_contain_key("press")


class TestSetVariable:
    def test_a_declared_name_is_stored_and_acknowledged(self, env: _Env) -> None:
        _bind(env, press=_set("pid", "42"))

        binding = _run_and_wait_status(env)

        assert_that(binding["pid"]).is_equal_to("42")
        assert_that(_log(env, "setvar.log")).is_equal_to(["ok"])

    @pytest.mark.parametrize(
        ("args", "reason"),
        [
            ({"name": "nope"}, "not a variable"),
            ({"name": "status"}, "reserved"),
            ({"big": True}, "larger than 64 KiB"),
            ({"nan": True}, "not JSON"),
        ],
    )
    def test_refusals_store_nothing(self, env: _Env, args: dict[str, Any], reason: str) -> None:
        _bind(env, press={"action": _SETVAR, "args": {"value": "1", **args}})

        binding = _run_and_wait_status(env)

        assert_that(_log(env, "setvar.log")[0]).starts_with("refused").contains(reason)
        assert_that(binding).is_equal_to({"status": "SUCCESS"})

    def test_an_unknown_or_foreign_request_is_refused(self, env: _Env) -> None:
        env.activate_tile()
        runner = env.manager._action_runner  # pylint: disable=protected-access

        assert_that(
            env.manager._store_variable(  # pylint: disable=protected-access
                "com.decker.acttest", {"request": "nope", "name": "pid", "value": 1}
            )
        ).contains("no running call")
        assert_that(runner.variable_target("com.decker.acttest", None)).is_none()


class TestBindingLifecycle:
    def test_a_changed_action_resets_only_that_binding(self, env: _Env) -> None:
        _bind(env, press=_set("pid", "1"), long_press=_set("pid", "2"))
        env.press(_TILE, hold=0.7)
        env.press(_TILE)
        assert_that(_wait(lambda: {"press", "long_press"} <= set(_vars(env)))).is_true()

        env.manager.set_bindings(_TILE, {"press": {"action": _ECHO, "args": {}}, "long_press": _set("pid", "2")})

        assert_that(_vars(env)).does_not_contain_key("press")
        assert_that(_vars(env)["long_press"]["pid"]).is_equal_to("2")

    def test_new_arguments_keep_the_values(self, env: _Env) -> None:
        _bind(env, press=_set("pid", "1"))
        _run_and_wait_status(env)

        env.manager.set_bindings(_TILE, {"press": _set("pid", "9")})

        assert_that(_vars(env)["press"]["pid"]).is_equal_to("1")

    def test_values_survive_a_page_switch_and_drop_clears_them(self, env: _Env) -> None:
        _bind(env, press=_set("pid", "5"))
        _run_and_wait_status(env)

        env.manager.deactivate_card(_TILE)
        env.manager.activate_card(_TILE)
        assert_that(_vars(env)["press"]["pid"]).is_equal_to("5")

        env.manager.drop_card_variables(_TILE.context_id)
        assert_that(_vars(env)).is_empty()


class TestReading:
    def _echoed(self, env: _Env, text: str) -> str:
        env.manager.set_bindings(_TILE, {"press": _set(), "long_press": {"action": _ECHO, "args": {"text": text}}})
        env.manager.card_variables.set(_TILE.context_id, "press", "pid", "123")
        env.press(_TILE, hold=0.7)
        assert_that(_wait(lambda: _log(env, "echo.log") != [])).is_true()
        return _log(env, "echo.log")[0]

    def test_a_reference_expands_with_the_stored_value(self, env: _Env) -> None:
        env.activate_tile()

        assert_that(self._echoed(env, "pid={press.pid}")).is_equal_to("pid=123")

    def test_unset_reads_empty(self, env: _Env) -> None:
        env.activate_tile()
        env.manager.set_bindings(
            _TILE, {"press": _set(), "long_press": {"action": _ECHO, "args": {"text": "[{press.other}]"}}}
        )

        env.press(_TILE, hold=0.7)

        assert_that(_wait(lambda: _log(env, "echo.log") != [])).is_true()
        assert_that(_log(env, "echo.log")[0]).is_equal_to("[]")

    def test_an_escaped_reference_arrives_as_typed_text(self, env: _Env) -> None:
        env.activate_tile()

        assert_that(self._echoed(env, "{{press.pid}}")).is_equal_to("{press.pid}")

    def test_a_typo_arrives_as_typed(self, env: _Env) -> None:
        env.activate_tile()

        assert_that(self._echoed(env, "{press.pdi}")).is_equal_to("{press.pdi}")


class TestSlots:
    def test_nested_calls_get_slots_of_their_own(self, env: _Env) -> None:
        _bind(env, press={"action": _SLOTS, "args": {}})

        binding = _run_and_wait_status(env)

        assert_that(_wait(lambda: "step_5" in _vars(env).get("press", {}))).is_true()
        binding = _vars(env)["press"]
        assert_that(binding["step_1"]).is_equal_to({"pid": "7", "status": "SUCCESS"})
        assert_that(_log(env, "echo.log")).is_equal_to(["saw 7"])
        assert_that(binding["step_2"]["status"]).is_equal_to("SUCCESS")
        assert_that(binding["step_4"]["status"]).is_equal_to("FAILURE")
        assert_that(binding["status"]).is_equal_to("SUCCESS")

    def test_a_call_without_a_slot_cannot_set_variables(self, env: _Env) -> None:
        _bind(env, press={"action": _SLOTS, "args": {}})
        _run_and_wait_status(env)

        assert_that(_wait(lambda: any("refused" in line for line in _log(env, "setvar.log")))).is_true()
        assert_that(_vars(env)["press"]).does_not_contain_key("pid")

    def test_bad_slots_fail_the_call(self, env: _Env) -> None:
        _bind(env, press={"action": _SLOTS, "args": {}})
        _run_and_wait_status(env)

        assert_that(_wait(lambda: len(_log(env, "slots.log")) == 3)).is_true()
        assert_that(all("invalid slot" in line for line in _log(env, "slots.log"))).is_true()

    def test_a_slotted_call_cannot_hand_out_slots(self, env: _Env) -> None:
        _bind(env, press={"action": _SLOTS, "args": {}})
        _run_and_wait_status(env)

        assert_that(_wait(lambda: "step_5" in _vars(env).get("press", {}))).is_true()
        assert_that(_vars(env)["press"]["step_5"]).does_not_contain_key("deep")
        assert_that(_wait(lambda: any("refused" in line for line in _log(env, "setvar.log")))).is_true()
