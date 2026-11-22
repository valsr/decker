import http.server
import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from assertpy import assert_that

from decker.plugin.bindings import NOTHING_ACTION_ID
from decker.plugin.loader import InstalledPlugin
from decker.plugin.manifest import PluginManifest
from decker.server import actions as actions_module
from decker.server.action_calls import ActionCall
from decker.server.actions import ActionRunner
from decker.server.builtin_actions import ActionError, http_request


def _manifest(runtime: bool, actions: list[dict[str, Any]]) -> PluginManifest:
    data: dict[str, Any] = {
        "format": 3,
        "id": "com.t.act",
        "name": "Act",
        "version": "1.0.0",
        "author": "T",
        "contributes": {"actions": actions},
    }
    if runtime:
        data["runtime"] = {"type": "executable", "entry": "x"}
    return PluginManifest.from_dict(data)


def _plugin(runtime: bool, actions: list[dict[str, Any]]) -> InstalledPlugin:
    return InstalledPlugin(manifest=_manifest(runtime, actions), plugin_dir=Path("."))


_ECHO = {"id": "com.t.act.echo", "name": "Echo", "settings": [{"key": "n", "type": "int", "label": "N", "default": 1}]}


def _call(action: str, args: dict[str, Any] | None = None, **kwargs: Any) -> ActionCall:
    return ActionCall(action, args or {}, kwargs.pop("input", "press"), **kwargs)


def _runner(
    supervisor: Any = None, builtin: Any = None, timeout: float = 2.0, resolve: Any = None, monkeypatch: Any = None
) -> tuple[ActionRunner, list]:
    failures: list[tuple[str, str]] = []
    if builtin is not None:
        monkeypatch.setattr(actions_module, "BUILTIN_ACTIONS", builtin)
    runner = ActionRunner(
        supervisor or MagicMock(),
        lambda a, e: failures.append((a, e)),
        resolve or (lambda _spec, args, _device: dict(args)),
        timeout=timeout,
    )
    return runner, failures


class TestBuiltinExecution:
    def test_a_builtin_action_gets_validated_settings_and_succeeds(self, monkeypatch):
        calls: list[Any] = []
        runner, failures = _runner(builtin={"com.t.act.echo": calls.append}, monkeypatch=monkeypatch)
        runner.register(_plugin(False, [_ECHO]))

        result = runner.execute(_call("com.t.act.echo", {"n": "7"}))

        assert_that(result.ok).is_true()
        assert_that(calls).is_equal_to([{"n": 7}])
        assert_that(failures).is_empty()

    def test_a_failing_builtin_reports_its_message(self, monkeypatch):
        def boom(_settings: Any) -> None:
            raise ActionError("no route")

        runner, failures = _runner(builtin={"com.t.act.echo": boom}, monkeypatch=monkeypatch)
        runner.register(_plugin(False, [_ECHO]))

        result = runner.execute(_call("com.t.act.echo"))

        assert_that((result.ok, result.error)).is_equal_to((False, "no route"))
        assert_that(failures).is_equal_to([("com.t.act.echo", "no route")])

    def test_an_unknown_action_fails_cleanly(self):
        runner, failures = _runner()

        result = runner.execute(_call("com.t.act.none"))

        assert_that(result.error).contains("unknown action")
        assert_that(failures).is_length(1)

    def test_a_plugin_without_code_and_without_a_builtin_fails(self):
        runner, _ = _runner()
        runner.register(_plugin(False, [_ECHO]))

        assert_that(runner.execute(_call("com.t.act.echo")).error).contains("no code")

    def test_unregistering_removes_the_actions(self):
        runner, _ = _runner()
        runner.register(_plugin(False, [_ECHO]))
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
        runner.register(_plugin(True, [_ECHO]))

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
        runner.register(_plugin(True, [_ECHO]))

        result = runner.execute(_call("com.t.act.echo"))

        assert_that(result.error).is_equal_to("timed out")
        supervisor.release.assert_called_once_with("com.t.act")
        assert_that(failures).is_length(1)

    def test_a_failed_plugin_is_not_asked(self):
        supervisor = MagicMock()
        supervisor.is_failed.return_value = True
        runner, _ = _runner(supervisor)
        runner.register(_plugin(True, [_ECHO]))

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
        runner.register(_plugin(True, [_ECHO]))
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
        assert_that(result.error).starts_with("invalid arguments:").contains("n")
        supervisor.send.assert_not_called()
        assert_that(failures).is_length(1)

    def test_args_are_resolved_for_the_device_before_sending(self):
        seen: list[Any] = []

        def resolve(spec: Any, args: Any, device: Any) -> dict[str, Any]:
            seen.append((spec.id, dict(args), device))
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

    def test_a_builtin_runs_resolved_args_in_process(self, monkeypatch):
        calls: list[Any] = []
        runner, _ = _runner(
            builtin={"com.t.act.echo": calls.append}, resolve=lambda *_: {"n": 8}, monkeypatch=monkeypatch
        )
        runner.register(_plugin(False, [_ECHO]))

        assert_that(runner.execute(_call("com.t.act.echo")).ok).is_true()
        assert_that(calls).is_equal_to([{"n": 8}])

    def test_listing_reports_scope_and_returns(self):
        runner, _ = _runner()
        spec = {**_ECHO, "scope": "global", "returns": "number"}
        runner.register(_plugin(True, [spec]))

        listed = runner.listing(lambda _p, _s: "")

        assert_that((listed[0]["scope"], listed[0]["returns"])).is_equal_to(("global", "number"))
        assert_that(runner.scope_of("com.t.act.echo")).is_equal_to(("com.t.act", "global"))
        assert_that(runner.scope_of("nope")).is_none()


class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list[tuple[str, str, bytes]] = []

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        _Handler.seen.append((self.command, self.path, self.rfile.read(length)))
        self.send_response(500 if self.path == "/boom" else 200)
        self.end_headers()

    do_GET = do_POST = _answer

    def log_message(self, *_args: Any) -> None:
        pass


@pytest.fixture()
def server():
    _Handler.seen = []
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


class TestHttpRequest:
    def test_post_sends_the_body(self, server):
        http_request(
            {"method": "POST", "url": f"{server}/hook", "body": '{"a": 1}', "content_type": "application/json"}
        )

        assert_that(_Handler.seen).is_equal_to([("POST", "/hook", b'{"a": 1}')])

    def test_get_succeeds(self, server):
        http_request({"method": "GET", "url": f"{server}/ping"})

        assert_that(_Handler.seen[0][:2]).is_equal_to(("GET", "/ping"))

    def test_an_error_status_is_a_failure(self, server):
        with pytest.raises(ActionError, match="HTTP 500"):
            http_request({"method": "GET", "url": f"{server}/boom"})

    def test_only_http_urls_are_allowed(self):
        with pytest.raises(ActionError, match="http"):
            http_request({"method": "GET", "url": "file:///etc/passwd"})

    def test_unreachable_host_is_a_failure(self):
        with pytest.raises(ActionError):
            http_request({"method": "GET", "url": "http://127.0.0.1:9/", "timeout": 1})


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
