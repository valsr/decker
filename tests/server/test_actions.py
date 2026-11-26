import json
import queue
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from assertpy import assert_that

from decker.plugin.bindings import NOTHING_ACTION_ID
from decker.plugin.loader import InstalledPlugin
from decker.plugin.manifest import PluginManifest
from decker.server.action_calls import ActionCall, BindingRef
from decker.server.actions import (
    MAX_CALL_TIMEOUT_S,
    ActionResult,
    ActionRunner,
    _Pending,
)


def _manifest(actions: list[dict[str, Any]], plugin_id: str = "com.t.act") -> PluginManifest:
    data: dict[str, Any] = {
        "format": 3,
        "id": plugin_id,
        "name": "Act",
        "version": "1.0.0",
        "author": "T",
        "runtime": {"type": "executable", "entry": "x"},
        "contributes": {"actions": actions},
    }
    return PluginManifest.from_dict(data)


def _plugin(actions: list[dict[str, Any]], plugin_id: str = "com.t.act") -> InstalledPlugin:
    return InstalledPlugin(manifest=_manifest(actions, plugin_id), plugin_dir=Path("."))


_ECHO = {"id": "com.t.act.echo", "name": "Echo", "settings": [{"key": "n", "type": "int", "label": "N", "default": 1}]}


def _call(action: str, args: dict[str, Any] | None = None, **kwargs: Any) -> ActionCall:
    return ActionCall(action, args or {}, kwargs.pop("input", "press"), **kwargs)


def _runner(supervisor: Any = None, timeout: float = 2.0, resolve: Any = None) -> tuple[ActionRunner, list]:
    failures: list[tuple[str, str]] = []
    runner = ActionRunner(
        supervisor or MagicMock(),
        lambda a, e: failures.append((a, e)),
        resolve or (lambda _spec, args, _device: dict(args)),
        timeout=timeout,
    )
    return runner, failures


class TestUnknownAndUnregistered:
    def test_an_unknown_action_fails_cleanly(self):
        runner, failures = _runner()

        result = runner.execute(_call("com.t.act.none"))

        assert_that(result.error).contains("unknown action")
        assert_that(failures).is_length(1)

    def test_unregistering_removes_the_actions(self):
        runner, _ = _runner()
        runner.register(_plugin([_ECHO]))
        runner.unregister("com.t.act")

        assert_that(runner.spec("com.t.act.echo")).is_none()


class TestPluginExecution:
    def _supervisor(self, runner_box: list) -> MagicMock:
        supervisor = MagicMock()
        supervisor.is_failed.return_value = False

        def send(plugin_id: str, message: Any) -> None:
            # the plugin answers from another thread, as its process would
            payload = message.payload
            threading.Timer(
                0.01, lambda: runner_box[0].on_result(plugin_id, {"request": payload["request"], "ok": True})
            ).start()

        supervisor.send.side_effect = send
        return supervisor

    def test_the_request_goes_to_the_plugin_and_its_answer_ends_the_run(self):
        box: list = []
        supervisor = self._supervisor(box)
        runner, failures = _runner(supervisor)
        box.append(runner)
        runner.register(_plugin([_ECHO]))

        result = runner.execute(_call("com.t.act.echo", {"n": 4}))

        assert_that(result.ok).is_true()
        sent = supervisor.send.call_args.args[1]
        assert_that((sent.event, sent.payload["action"], sent.payload["args"])).is_equal_to(
            ("run_action", "com.t.act.echo", {"n": 4})
        )
        supervisor.acquire.assert_called_once_with("com.t.act")
        supervisor.release.assert_called_once_with("com.t.act")
        assert_that(failures).is_empty()

    def test_a_plugin_that_never_answers_times_out_and_releases_the_process(self):
        supervisor = MagicMock()
        supervisor.is_failed.return_value = False
        runner, failures = _runner(supervisor, timeout=0.05)
        runner.register(_plugin([_ECHO]))

        result = runner.execute(_call("com.t.act.echo"))

        assert_that(result.error).is_equal_to("timed out")
        supervisor.release.assert_called_once_with("com.t.act")
        assert_that(failures).is_length(1)

    def test_a_failed_plugin_is_not_asked(self):
        supervisor = MagicMock()
        supervisor.is_failed.return_value = True
        runner, _ = _runner(supervisor)
        runner.register(_plugin([_ECHO]))

        assert_that(runner.execute(_call("com.t.act.echo")).error).contains("failed")
        supervisor.send.assert_not_called()

    def test_an_answer_from_another_plugin_or_for_an_unknown_request_is_ignored(self):
        runner, _ = _runner()
        runner.on_result("com.other", {"request": "nope", "ok": True})  # must not raise


class TestProtocol4:
    def _plugin_runner(self, resolve: Any = None) -> tuple[ActionRunner, MagicMock, list]:
        box: list = []
        supervisor = TestPluginExecution()._supervisor(box)
        runner, failures = _runner(supervisor, resolve=resolve)
        box.append(runner)
        runner.register(_plugin([_ECHO]))
        return runner, supervisor, failures

    def test_nothing_runs_nothing_and_succeeds(self):
        runner, supervisor, failures = self._plugin_runner()

        result = runner.execute(_call(NOTHING_ACTION_ID))

        assert_that(result.ok).is_true()
        supervisor.send.assert_not_called()
        assert_that(failures).is_empty()

    def test_invalid_args_fail_without_reaching_the_plugin(self):
        runner, supervisor, failures = self._plugin_runner()

        result = runner.execute(_call("com.t.act.echo", {"n": "abc"}))

        assert_that(result.ok).is_false()
        assert_that(result.error).starts_with("invalid arguments: n: ")
        supervisor.send.assert_not_called()
        assert_that(failures).is_length(1)

    def test_args_are_resolved_for_the_device_before_sending(self):
        seen: list[Any] = []

        def resolve(spec: Any, args: Any, call: Any) -> dict[str, Any]:
            seen.append((spec.id, dict(args), call.device))
            return {"n": 99}

        runner, supervisor, _ = self._plugin_runner(resolve)

        runner.execute(_call("com.t.act.echo", {"n": 5}, device="dev-1"))

        assert_that(seen).is_equal_to([("com.t.act.echo", {"n": 5}, "dev-1")])
        assert_that(supervisor.send.call_args.args[1].payload["args"]).is_equal_to({"n": 99})

    def test_run_action_message_carries_input_card_and_chain(self):
        runner, supervisor, _ = self._plugin_runner()
        call = _call("com.t.act.echo", input="long_press", card={"id": "k"}, caller="com.o", depth=3)

        runner.execute(call)

        payload = supervisor.send.call_args.args[1].payload
        assert_that(
            (payload["input"], payload["card"], payload["chain"], payload["caller"], payload["depth"])
        ).is_equal_to(("long_press", {"id": "k"}, call.chain, "com.o", 3))

    def test_listing_reports_scope_and_returns(self):
        runner, _ = _runner()
        spec = {**_ECHO, "scope": "global", "returns": "number"}
        runner.register(_plugin([spec]))

        listed = runner.listing(lambda _p, _s: "")

        assert_that((listed[0]["scope"], listed[0]["returns"])).is_equal_to(("global", "number"))
        assert_that(runner.scope_of("com.t.act.echo")).is_equal_to(("com.t.act", "global"))
        assert_that(runner.scope_of("nope")).is_none()

    def test_listing_reports_variables(self):
        runner, _ = _runner()
        runner.register(_plugin([{**_ECHO, "variables": [{"name": "pid", "description": "process id"}]}]))

        listed = runner.listing(lambda _p, _s: "")

        assert_that(listed[0]["variables"]).is_equal_to([{"name": "pid", "description": "process id"}])


def _carded_plugin(actions: list[dict[str, Any]], cards: list[dict[str, Any]]) -> InstalledPlugin:
    data: dict[str, Any] = {
        "format": 3,
        "id": "com.t.act",
        "name": "Act",
        "version": "1.0.0",
        "author": "T",
        "runtime": {"type": "executable", "entry": "x"},
        "contributes": {"actions": actions, "cards": cards},
    }
    return InstalledPlugin(manifest=PluginManifest.from_dict(data), plugin_dir=Path("."))


class TestListingCards:
    _SHARED = {"id": "com.t.act.shared", "name": "Shared"}
    _SOLO = {"id": "com.t.act.solo", "name": "Solo"}
    _GLOBAL_ACTION = {"id": "com.t.act.open", "name": "Open", "scope": "global"}
    _CARDS = [
        {"id": "com.t.act.first", "name": "First", "actions": ["com.t.act.shared"]},
        {"id": "com.t.act.second", "name": "Second", "actions": ["com.t.act.shared"]},
    ]

    def _cards_by_action(self) -> dict[str, list[str]]:
        runner, _ = _runner()
        runner.register(_carded_plugin([self._SHARED, self._SOLO, self._GLOBAL_ACTION], self._CARDS))
        return {item["action_uuid"]: item["cards"] for item in runner.listing(lambda _p, _s: "")}

    def test_listing_cards_empty_when_unlisted(self):
        assert_that(self._cards_by_action()["com.t.act.solo"]).is_empty()

    def test_listing_cards_for_shared_action(self):
        assert_that(self._cards_by_action()["com.t.act.shared"]).is_equal_to(["com.t.act.first", "com.t.act.second"])

    def test_listing_cards_global_action_is_empty(self):
        assert_that(self._cards_by_action()["com.t.act.open"]).is_empty()


_GLOBAL = {"id": "com.t.act.shared", "name": "Shared", "scope": "global"}
_OTHER_PRIVATE = {"id": "com.t.other.secret", "name": "Secret"}
_OTHER_GLOBAL = {"id": "com.t.other.open", "name": "Open", "scope": "global"}


class _Rig:
    """A runner whose plugin processes are fake: it answers run_action through ``answer`` and records the rest."""

    def __init__(self, answer: Any = None, resolve: Any = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.replies: queue.Queue = queue.Queue()
        self.devices: list[Any] = []
        self.answer = answer
        supervisor = MagicMock()
        supervisor.is_failed.return_value = False
        supervisor.send.side_effect = self._send
        self.runner, self.failures = _runner(supervisor, resolve=resolve or self._resolve)
        self.runner.register(_plugin([_ECHO, _GLOBAL]))
        self.runner.register(_plugin([_OTHER_PRIVATE, _OTHER_GLOBAL], "com.t.other"))

    def _resolve(self, _spec: Any, args: Any, call: Any) -> dict[str, Any]:
        self.devices.append(call.device)
        return dict(args)

    def _send(self, plugin_id: str, message: Any) -> None:
        if message.event == "call_result":
            self.replies.put(message.payload)
            return
        payload = message.payload
        self.sent.append(payload)
        outcome = self.answer(payload) if self.answer is not None else None
        if outcome is not None:
            threading.Timer(
                0.01, lambda: self.runner.on_result(plugin_id, {"request": payload["request"], **outcome})
            ).start()

    def call(self, plugin_id: str, **fields: Any) -> dict[str, Any]:
        payload = {"request": "r1", "action": "com.t.act.echo", "args": {}, **fields}
        self.runner.on_plugin_call(plugin_id, payload)
        return self.replies.get(timeout=3)


def _wait_sent(rig: "_Rig") -> None:
    for _ in range(400):
        if rig.sent:
            return
        threading.Event().wait(0.01)
    raise AssertionError("the outer call never reached the plugin")


class TestCallAction:
    def test_call_result_returns_the_callee_value(self) -> None:
        rig = _Rig(lambda _p: {"ok": True, "value": {"n": 5}})

        reply = rig.call("com.t.act")

        assert_that(reply).is_equal_to({"request": "r1", "ok": True, "value": {"n": 5}})
        assert_that(rig.sent[0]["input"]).is_equal_to("plugin")
        assert_that(rig.sent[0]["caller"]).is_equal_to("com.t.act")
        assert_that(rig.sent[0]["card"]).is_none()

    def test_a_failing_callee_replies_with_its_error(self) -> None:
        rig = _Rig(lambda _p: {"ok": False, "error": "boom"})

        assert_that(rig.call("com.t.act")).is_equal_to({"request": "r1", "ok": False, "error": "boom"})

    def test_own_plugin_scoped_action_is_callable(self) -> None:
        rig = _Rig(lambda _p: {"ok": True})

        assert_that(rig.call("com.t.other", action="com.t.other.secret")["ok"]).is_true()

    def test_foreign_plugin_scoped_action_is_refused(self) -> None:
        rig = _Rig(lambda _p: {"ok": True})

        reply = rig.call("com.t.act", action="com.t.other.secret")

        assert_that(reply["ok"]).is_false()
        assert_that(reply["error"]).is_equal_to("action com.t.other.secret is not callable from com.t.act")
        assert_that(rig.sent).is_empty()

    def test_global_action_of_another_plugin_is_callable(self) -> None:
        rig = _Rig(lambda _p: {"ok": True, "value": 1})

        assert_that(rig.call("com.t.act", action="com.t.other.open")["ok"]).is_true()

    def test_unknown_action_and_invalid_args_are_refused(self) -> None:
        rig = _Rig(lambda _p: {"ok": True})

        assert_that(rig.call("com.t.act", action="nope")["error"]).is_equal_to("unknown action nope")
        assert_that(rig.call("com.t.act", args={"n": "abc"})["error"]).starts_with("invalid arguments: n: ")
        assert_that(rig.sent).is_empty()

    def test_depth_9_is_refused_and_8_is_allowed(self) -> None:
        rig = _Rig(lambda _p: {"ok": True})

        refused = rig.call("com.t.act", depth=9)
        allowed = rig.call("com.t.act", depth=8)

        assert_that(refused).is_equal_to({"request": "r1", "ok": False, "error": "call depth limit (8) exceeded"})
        assert_that(allowed["ok"]).is_true()

    def test_chain_device_is_inherited_by_nested_calls(self) -> None:
        rig = _Rig(lambda p: {"ok": True} if p["depth"] == 4 else None)
        outer = _call("com.t.act.echo", chain="c1", depth=3, device="dev-1", input="press")
        thread = threading.Thread(target=rig.runner.execute, args=(outer,))
        thread.start()
        _wait_sent(rig)

        reply = rig.call("com.t.act", chain="c1", depth=0)

        assert_that(reply["ok"]).is_true()
        assert_that((rig.sent[1]["chain"], rig.sent[1]["depth"])).is_equal_to(("c1", 4))
        assert_that(rig.devices).is_equal_to(["dev-1", "dev-1"])
        rig.runner.on_result("com.t.act", {"request": rig.sent[0]["request"], "ok": True})
        thread.join(3)

    def test_explicit_device_wins(self) -> None:
        rig = _Rig(lambda p: {"ok": True} if p["depth"] == 1 else None)
        outer = _call("com.t.act.echo", chain="c2", depth=0, device="dev-1")
        thread = threading.Thread(target=rig.runner.execute, args=(outer,))
        thread.start()
        _wait_sent(rig)

        rig.call("com.t.act", chain="c2", device="dev-2")

        assert_that(rig.devices[-1]).is_equal_to("dev-2")
        rig.runner.on_result("com.t.act", {"request": rig.sent[0]["request"], "ok": True})
        thread.join(3)

    def test_unmatched_chain_uses_the_payload_depth(self) -> None:
        rig = _Rig(lambda _p: {"ok": True})

        rig.call("com.t.act", chain="zzz", depth=-4)
        rig.call("com.t.act", depth=2)

        assert_that([p["depth"] for p in rig.sent]).is_equal_to([0, 2])

    def test_oversized_value_is_an_error(self) -> None:
        rig = _Rig(lambda _p: {"ok": True, "value": "x" * (1024 * 1024)})

        reply = rig.call("com.t.act")

        assert_that(reply).is_equal_to({"request": "r1", "ok": False, "error": "return value larger than 1 MiB"})

    def test_gesture_run_discards_the_value_and_failure_still_announces(self) -> None:
        answers = iter([{"ok": True, "value": 3}, {"ok": False, "error": "bad"}])
        rig = _Rig(lambda _p: next(answers))

        assert_that(rig.runner.execute(_call("com.t.act.echo")).value).is_equal_to(3)
        rig.runner.run(_call("com.t.act.echo"))
        for _ in range(300):
            if rig.failures:
                break
            threading.Event().wait(0.01)

        assert_that(rig.failures).is_equal_to([("com.t.act.echo", "bad")])

    @pytest.mark.parametrize(
        "fields",
        [
            {"args": []},
            {"timeout": "soon"},
            {"timeout": 0},
            {"timeout": float("nan")},
            {"timeout": float("inf")},
            {"depth": "3"},
            {"depth": True},
            {"chain": 5},
            {"device": 5},
            {"parent": 5},
            {"action": 7},
        ],
    )
    def test_malformed_fields_get_an_error_reply(self, fields: dict[str, Any]) -> None:
        rig = _Rig(lambda _p: {"ok": True})

        reply = rig.call("com.t.act", **fields)

        assert_that(reply["ok"]).is_false()
        assert_that(reply["request"]).is_equal_to("r1")
        assert_that(rig.sent).is_empty()

    def test_a_valid_timeout_reaches_the_call_and_is_clamped(self) -> None:
        rig = _Rig()
        seen: list[Any] = []

        def record(call: ActionCall) -> ActionResult:
            seen.append(call)
            return ActionResult(True)

        rig.runner._execute = record  # type: ignore[method-assign]  # pylint: disable=protected-access

        rig.call("com.t.act", timeout=5)
        rig.call("com.t.act", timeout=10_000)
        rig.call("com.t.act")

        assert_that([c.timeout for c in seen]).is_equal_to([5.0, MAX_CALL_TIMEOUT_S, None])

    def test_a_result_from_another_plugin_leaves_the_pending_call_alone(self) -> None:
        rig = _Rig()
        pending = _Pending("com.t.act", _call("com.t.act.echo"))
        rig.runner._pending["req"] = pending  # pylint: disable=protected-access

        rig.runner.on_result("com.t.other", {"request": "req", "ok": True})

        assert_that(rig.runner._pending).contains_key("req")  # pylint: disable=protected-access
        assert_that(pending.done.is_set()).is_false()

    def test_a_call_without_a_string_request_is_dropped(self) -> None:
        rig = _Rig()

        rig.runner.on_plugin_call("com.t.act", {"request": 5, "action": "com.t.act.echo"})
        rig.runner.on_plugin_call("com.t.act", {"action": "com.t.act.echo"})

        with pytest.raises(queue.Empty):
            rig.replies.get(timeout=0.2)

    def test_an_exception_while_running_becomes_an_error_reply(self) -> None:
        def explode(*_a: Any) -> dict[str, Any]:
            raise RuntimeError("resolver broke")

        rig = _Rig(lambda _p: {"ok": True}, resolve=explode)

        reply = rig.call("com.t.act")

        assert_that(reply["ok"]).is_false()
        assert_that(reply["error"]).contains("resolver broke")

    def test_an_exception_in_a_gesture_run_still_announces_failure(self) -> None:
        def explode(*_a: Any) -> dict[str, Any]:
            raise RuntimeError("resolver broke")

        rig = _Rig(resolve=explode)

        rig.runner.run(_call("com.t.act.echo"))
        for _ in range(300):
            if rig.failures:
                break
            threading.Event().wait(0.01)

        assert_that(rig.failures).is_length(1)
        assert_that(rig.failures[0][1]).contains("resolver broke")


class TestManifestCardsAndActions:
    def _data(self, **contributes: Any) -> dict[str, Any]:
        return {
            "format": 3,
            "id": "com.t.audio",
            "name": "Audio",
            "version": "1.0.0",
            "author": "T",
            "runtime": {"type": "executable", "entry": "x"},
            "contributes": contributes,
        }

    def test_an_action_alone_is_a_valid_contribution(self):
        manifest = PluginManifest.from_dict(self._data(actions=[{"id": "com.t.audio.mute", "name": "Mute"}]))

        assert_that(manifest.cards).is_empty()
        assert_that([a.id for a in manifest.actions]).is_equal_to(["com.t.audio.mute"])

    def test_action_settings_are_validated_like_card_settings(self):
        from decker.plugin.manifest import (
            ManifestValidationError,  # pylint: disable=import-outside-toplevel
        )

        with pytest.raises(ManifestValidationError):
            PluginManifest.from_dict(
                self._data(actions=[{"id": "com.t.audio.x", "name": "X", "settings": [{"key": "a", "type": "nope"}]}])
            )


_SLOW = {"id": "com.t.act.slow", "name": "Slow", "timeout": 2}
_LONG = {"id": "com.t.act.long", "name": "Long", "timeout": 120}


class TestTimeLimit:
    def _silent(self, actions: list[dict[str, Any]]) -> tuple[ActionRunner, MagicMock]:
        supervisor = MagicMock()
        supervisor.is_failed.return_value = False
        runner, _ = _runner(supervisor, timeout=30.0)
        runner.register(_plugin(actions))
        return runner, supervisor

    def test_a_gesture_waits_the_declared_timeout(self) -> None:
        runner, _ = self._silent([_SLOW])
        started = time.monotonic()

        result = runner.execute(_call("com.t.act.slow"))

        assert_that(result.error).is_equal_to("timed out")
        assert_that(time.monotonic() - started).is_between(1.5, 5)

    def test_explicit_call_timeout_wins_over_declared(self) -> None:
        runner, _ = self._silent([_LONG])
        started = time.monotonic()

        result = runner.execute(_call("com.t.act.long", timeout=1))

        assert_that(result.error).is_equal_to("timed out")
        assert_that(time.monotonic() - started).is_between(0.8, 3)

    def test_the_runner_default_applies_without_a_declared_timeout(self) -> None:
        runner, _ = self._silent([_ECHO, _LONG])

        assert_that(runner.timeout_for(_call("com.t.act.echo"))).is_equal_to(30.0)
        assert_that(runner.timeout_for(_call("com.t.act.long"))).is_equal_to(120.0)
        assert_that(runner.timeout_for(_call("com.t.act.long", timeout=10_000))).is_equal_to(MAX_CALL_TIMEOUT_S)

    def _parent(self, runner: ActionRunner, remaining: float, request: str = "parent") -> str:
        parent = _call("com.t.act.echo")
        pending = _Pending("com.t.act", parent)
        pending.deadline = time.monotonic() + remaining
        runner._pending[request] = pending  # pylint: disable=protected-access
        return parent.chain

    @staticmethod
    def _nested(runner: ActionRunner, chain: str, **fields: Any) -> ActionResult:
        payload = {
            "request": "n1",
            "action": "com.t.act.long",
            "args": {},
            "chain": chain,
            "parent": "parent",
            **fields,
        }
        return runner._plugin_call("com.t.act", payload)  # pylint: disable=protected-access

    def _ended_parent(self, runner: ActionRunner, supervisor: MagicMock, how: str) -> str:
        if how == "timeout":
            runner.execute(_call("com.t.act.echo", timeout=1))
        else:
            thread = threading.Thread(target=runner.execute, args=(_call("com.t.act.echo"),))
            thread.start()
            while not supervisor.send.called:
                time.sleep(0.01)
            request = supervisor.send.call_args[0][1].payload["request"]
            runner.on_result("com.t.act", {"request": request, "ok": True})
            thread.join()
        return supervisor.send.call_args[0][1].payload["request"]

    @pytest.mark.parametrize("how", ["timeout", "result"])
    def test_a_nested_call_after_the_parent_ended_is_refused(self, how: str) -> None:
        runner, supervisor = self._silent([_ECHO, _LONG])
        parent = self._ended_parent(runner, supervisor, how)
        supervisor.send.reset_mock()

        result = self._nested(runner, "chain", parent=parent)

        assert_that(result.error).is_equal_to("the calling action ran out of time")
        supervisor.send.assert_not_called()

    def test_a_foreign_ended_parent_gives_no_cap(self) -> None:
        runner, supervisor = self._silent([_ECHO, _LONG])
        parent = self._ended_parent(runner, supervisor, "timeout")
        foreign = ActionCall("com.t.act.long", {}, "press", caller="com.other", parent=parent)

        assert_that(runner.timeout_for(foreign)).is_equal_to(120.0)

    def test_the_ended_runs_are_remembered_only_up_to_a_bound(self) -> None:
        runner, _ = self._silent([_ECHO])
        for index in range(1100):
            with runner._lock:  # pylint: disable=protected-access
                runner._remember_ended(f"r{index}", "com.t.act")  # pylint: disable=protected-access

        assert_that(runner._ended).is_length(1024)  # pylint: disable=protected-access

    def test_nested_call_is_capped_by_parent_remaining_time(self) -> None:
        runner, _ = self._silent([_ECHO, _LONG])
        chain = self._parent(runner, 1.0)
        started = time.monotonic()

        result = self._nested(runner, chain)

        assert_that(result.error).is_equal_to("timed out")
        assert_that(time.monotonic() - started).is_between(0.5, 3)

    def test_nested_call_with_no_time_left_is_not_sent(self) -> None:
        runner, supervisor = self._silent([_ECHO, _LONG])
        chain = self._parent(runner, -1.0)

        result = self._nested(runner, chain)

        assert_that(result.error).is_equal_to("the calling action ran out of time")
        supervisor.send.assert_not_called()

    def test_a_same_plugin_sibling_does_not_cap_another_nested_call(self) -> None:
        runner, _ = self._silent([_ECHO, _SLOW, _LONG])
        chain = self._parent(runner, 60.0)
        seen: list[ActionCall] = []
        runner._execute = lambda call: seen.append(call) or ActionResult(True)  # type: ignore[method-assign,func-returns-value]  # pylint: disable=protected-access
        self._nested(runner, chain, action="com.t.act.slow", timeout=2)
        sibling = _Pending("com.t.act", seen[0])
        sibling.deadline = time.monotonic() + 0.01
        runner._pending["sibling"] = sibling  # pylint: disable=protected-access

        self._nested(runner, chain)

        assert_that(runner.timeout_for(seen[1])).is_between(55, 61)

    def test_an_unknown_or_foreign_parent_gives_no_cap(self) -> None:
        runner, _ = self._silent([_ECHO, _LONG])
        chain = self._parent(runner, 0.5)
        seen: list[ActionCall] = []
        runner._execute = lambda call: seen.append(call) or ActionResult(True)  # type: ignore[method-assign,func-returns-value]  # pylint: disable=protected-access
        self._nested(runner, chain, parent="nope")
        runner._pending["parent"].plugin_id = "com.other"  # pylint: disable=protected-access
        self._nested(runner, chain)

        assert_that([runner.timeout_for(c) for c in seen]).is_equal_to([120.0, 120.0])


class TestMayDrive:
    def _runner_with(self, device: str | None) -> tuple[ActionRunner, _Pending]:
        runner, _ = _runner()
        pending = _Pending("com.t.act", _call("com.t.act.echo", device=device))
        runner._pending["r1"] = pending  # pylint: disable=protected-access
        return runner, pending

    def test_only_while_own_call_for_that_device_runs(self):
        runner, pending = self._runner_with("dev1")

        assert_that(runner.may_drive("com.t.act", "r1", "dev1")).is_true()
        assert_that(runner.may_drive("com.t.other", "r1", "dev1")).is_false()
        assert_that(runner.may_drive("com.t.act", "r1", "dev2")).is_false()
        assert_that(runner.may_drive("com.t.act", "nope", "dev1")).is_false()
        assert_that(runner.may_drive("com.t.act", 5, "dev1")).is_false()
        pending.done.set()
        assert_that(runner.may_drive("com.t.act", "r1", "dev1")).is_false()

    def test_a_call_without_a_device_drives_nothing(self):
        runner, _ = self._runner_with(None)

        assert_that(runner.may_drive("com.t.act", "r1", "dev1")).is_false()


_VARS = {
    "id": "com.t.act.vars",
    "name": "Vars",
    "variables": [{"name": "pid", "description": ""}],
}


class TestFinishedAndSlots:
    def _rig(self, answer: Any = None) -> tuple[_Rig, list[tuple[ActionCall, ActionResult]]]:
        finished: list[tuple[ActionCall, ActionResult]] = []
        rig = _Rig(answer)
        rig.runner._on_finished = lambda c, r: finished.append((c, r))  # pylint: disable=protected-access
        rig.runner.register(_plugin([_VARS]))
        return rig, finished

    def test_a_call_with_a_ref_reports_when_it_ends(self) -> None:
        rig, finished = self._rig(lambda _p: {"ok": True})

        rig.runner.execute(_call("com.t.act.echo", ref=BindingRef("c", "press")))

        assert_that([(c.ref, r.ok) for c, r in finished]).is_equal_to([(BindingRef("c", "press"), True)])

    def test_a_call_without_a_ref_or_do_nothing_does_not_report(self) -> None:
        rig, finished = self._rig(lambda _p: {"ok": True})

        rig.runner.execute(_call("com.t.act.echo"))
        rig.runner.execute(_call(NOTHING_ACTION_ID, ref=BindingRef("c", "press")))

        assert_that(finished).is_empty()

    def test_a_timeout_and_a_stopped_plugin_report_failure(self) -> None:
        rig, finished = self._rig()
        rig.runner._timeout = 0.05  # pylint: disable=protected-access
        rig.runner.execute(_call("com.t.act.echo", ref=BindingRef("c", "press")))
        threading.Timer(0.05, lambda: rig.runner.plugin_stopped("com.t.act")).start()
        rig.runner._timeout = 5  # pylint: disable=protected-access
        rig.runner.execute(_call("com.t.act.echo", ref=BindingRef("c", "press")))

        assert_that([r.error for _, r in finished]).is_equal_to(["timed out", "the plugin stopped"])

    def test_variable_target_needs_the_owner_and_a_live_call(self) -> None:
        rig, _ = self._rig()
        call = _call("com.t.act.vars", ref=BindingRef("c", "press"))
        threading.Thread(target=rig.runner.execute, args=(call,), daemon=True).start()
        _wait_sent(rig)
        request = rig.sent[0]["request"]

        found = rig.runner.variable_target("com.t.act", request)

        assert found is not None
        assert_that(found[0].ref).is_equal_to(BindingRef("c", "press"))
        assert_that(found[1].id).is_equal_to("com.t.act.vars")
        assert_that(rig.runner.variable_target("com.t.other", request)).is_none()
        assert_that(rig.runner.variable_target("com.t.act", "nope")).is_none()
        assert_that(rig.runner.variable_target("com.t.act", 5)).is_none()
        rig.runner.on_result("com.t.act", {"request": request, "ok": True})
        assert_that(rig.runner.variable_target("com.t.act", request)).is_none()

    def test_a_slotted_child_gets_the_parents_binding_and_a_grandchild_gets_none(self) -> None:
        rig, finished = self._rig(lambda p: {"ok": True} if p["action"] != "com.t.act.vars" else None)
        parent = _call("com.t.act.vars", ref=BindingRef("c", "press"))
        threading.Thread(target=rig.runner.execute, args=(parent,), daemon=True).start()
        _wait_sent(rig)
        outer = rig.sent[0]["request"]

        rig.call("com.t.act", request="r1", parent=outer, slot="step_1")
        rig.call("com.t.act", request="r2", parent=outer)
        rig.call("com.t.act", request="r3", parent="missing", slot="step_3")

        refs = [c.ref for c, _ in finished]
        assert_that(refs).is_equal_to([BindingRef("c", "press", "step_1")])
        rig.runner.on_result("com.t.act", {"request": outer, "ok": True})

    @pytest.mark.parametrize("slot", ["status", "Bad", "x" * 33, 5])
    def test_a_bad_slot_fails_the_call(self, slot: Any) -> None:
        rig, finished = self._rig(lambda _p: {"ok": True})

        reply = rig.call("com.t.act", slot=slot)

        assert_that(reply["error"]).is_equal_to("invalid slot")
        assert_that(finished).is_empty()


class TestRefusedSlottedCalls:
    def _parent(self) -> tuple[_Rig, list[tuple[ActionCall, ActionResult]], str]:
        finished: list[tuple[ActionCall, ActionResult]] = []
        rig = _Rig(lambda p: {"ok": True} if p["action"] != "com.t.act.vars" else None)
        rig.runner._on_finished = lambda c, r: finished.append((c, r))  # pylint: disable=protected-access
        rig.runner.register(_plugin([_VARS]))
        parent = _call("com.t.act.vars", ref=BindingRef("c", "press"))
        threading.Thread(target=rig.runner.execute, args=(parent,), daemon=True).start()
        _wait_sent(rig)
        return rig, finished, rig.sent[0]["request"]

    def _refused(self, **fields: Any) -> list[tuple[ActionCall, ActionResult]]:
        rig, finished, outer = self._parent()
        reply = rig.call("com.t.act", parent=outer, slot="step_2", **fields)
        assert_that(reply["ok"]).is_false()
        rig.runner.on_result("com.t.act", {"request": outer, "ok": True})
        return [(c, r) for c, r in finished if c.ref == BindingRef("c", "press", "step_2")]

    def test_an_unknown_action_records_failure_for_the_slot(self) -> None:
        records = self._refused(action="com.t.nowhere.gone")

        assert_that([r.ok for _, r in records]).is_equal_to([False])

    def test_another_plugins_private_action_records_failure_for_the_slot(self) -> None:
        records = self._refused(action="com.t.other.secret")

        assert_that([r.ok for _, r in records]).is_equal_to([False])

    def test_a_call_over_the_depth_limit_records_failure_for_the_slot(self) -> None:
        records = self._refused(depth=99)

        assert_that([r.ok for _, r in records]).is_equal_to([False])

    def test_an_invalid_slot_records_nothing(self) -> None:
        rig, finished, outer = self._parent()

        rig.call("com.t.act", parent=outer, slot="Bad", action="com.t.nowhere.gone")

        assert_that(finished).is_empty()
        rig.runner.on_result("com.t.act", {"request": outer, "ok": True})
