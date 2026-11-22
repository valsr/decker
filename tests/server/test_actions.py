import http.server
import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from assertpy import assert_that

from decker.plugin.loader import InstalledPlugin
from decker.plugin.manifest import PluginManifest
from decker.server.actions import ActionRunner
from decker.server.builtin_actions import ActionError, http_request


def _manifest(runtime: bool, actions: list[dict[str, Any]]) -> PluginManifest:
    data: dict[str, Any] = {
        "format": 2,
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


def _runner(supervisor: Any = None, builtin: Any = None, timeout: float = 2.0) -> tuple[ActionRunner, list]:
    failures: list[tuple[str, str]] = []
    runner = ActionRunner(
        supervisor or MagicMock(), lambda a, e: failures.append((a, e)), builtin=builtin or {}, timeout=timeout
    )
    return runner, failures


class TestBuiltinExecution:
    def test_a_builtin_action_gets_validated_settings_and_succeeds(self):
        calls: list[Any] = []
        runner, failures = _runner(builtin={"com.t.act.echo": calls.append})
        runner.register(_plugin(False, [_ECHO]))

        result = runner.execute("com.t.act.echo", {"n": "7"})

        assert_that(result.ok).is_true()
        assert_that(calls).is_equal_to([{"n": 7}])
        assert_that(failures).is_empty()

    def test_a_failing_builtin_reports_its_message(self):
        def boom(_settings: Any) -> None:
            raise ActionError("no route")

        runner, failures = _runner(builtin={"com.t.act.echo": boom})
        runner.register(_plugin(False, [_ECHO]))

        result = runner.execute("com.t.act.echo", {})

        assert_that((result.ok, result.error)).is_equal_to((False, "no route"))
        assert_that(failures).is_equal_to([("com.t.act.echo", "no route")])

    def test_an_unknown_action_fails_cleanly(self):
        runner, failures = _runner()

        result = runner.execute("com.t.act.none", {})

        assert_that(result.error).contains("unknown action")
        assert_that(failures).is_length(1)

    def test_a_plugin_without_code_and_without_a_builtin_fails(self):
        runner, _ = _runner()
        runner.register(_plugin(False, [_ECHO]))

        assert_that(runner.execute("com.t.act.echo", {}).error).contains("no code")

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

        result = runner.execute("com.t.act.echo", {"n": 4})

        assert_that(result.ok).is_true()
        sent = supervisor.send.call_args.args[1]
        assert_that((sent.event, sent.payload["action"], sent.payload["settings"])).is_equal_to(
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

        result = runner.execute("com.t.act.echo", {})

        assert_that(result.error).is_equal_to("timed out")
        supervisor.release.assert_called_once_with("com.t.act")
        assert_that(failures).is_length(1)

    def test_a_failed_plugin_is_not_asked(self):
        supervisor = MagicMock()
        supervisor.is_failed.return_value = True
        runner, _ = _runner(supervisor)
        runner.register(_plugin(True, [_ECHO]))

        assert_that(runner.execute("com.t.act.echo", {}).error).contains("failed")
        supervisor.send.assert_not_called()

    def test_an_answer_from_another_plugin_or_for_an_unknown_request_is_ignored(self):
        runner, _ = _runner()
        runner.on_result("com.other", {"request": "nope", "ok": True})  # must not raise


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
            "format": 2,
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
