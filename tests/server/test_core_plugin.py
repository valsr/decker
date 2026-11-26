import http.client
import http.server
import importlib.util
import json
import os
import queue
import random
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from assertpy import assert_that

from decker_plugin_sdk import run
from decker_plugin_sdk._actions import encode_value
from tests.server.test_actions_e2e import _TILE, _Env, _wait

_PLUGIN = Path(__file__).parents[2] / "src" / "decker" / "builtin_plugins" / "com.decker.core" / "plugin.py"
_HTTP = "com.decker.core.httprequest"
_DELAY = "com.decker.core.delay"
_MANIFEST = _PLUGIN.with_name("manifest.json")
_WAIT = 10.0


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("core_plugin_under_test", _PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Host:
    """Plays the server over os.pipe streams and runs Core's actions through the SDK."""

    def __init__(self) -> None:
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        self._to_plugin = os.fdopen(in_w, "wb")
        self._in = os.fdopen(in_r, "rb")
        self._out = os.fdopen(out_w, "wb")
        self._from_plugin = os.fdopen(out_r, "rb")
        self._messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        module = _load()
        actions = {_HTTP: module.http_request, _DELAY: module.delay}
        self._thread = threading.Thread(target=lambda: run({}, self._in, self._out, actions))
        self._thread.start()
        self._reader = threading.Thread(target=self._read)
        self._reader.start()
        self._send(
            "hello", {"protocol": 4, "decker_version": "x", "plugin_id": "com.decker.core", "global_settings": {}}
        )
        self._until("ready")

    def _read(self) -> None:
        for line in self._from_plugin:
            self._messages.put(json.loads(line))
        self._messages.put(None)

    def _send(self, event: str, payload: dict[str, Any]) -> None:
        self._to_plugin.write(json.dumps({"event": event, "payload": payload}).encode() + b"\n")
        self._to_plugin.flush()

    def _until(self, event: str) -> dict[str, Any]:
        for _ in range(100):
            message = self._messages.get(timeout=_WAIT)
            assert message is not None, "plugin closed stdout"
            if message["event"] == event:
                return message.get("payload") or {}
        raise AssertionError(f"no {event} message")

    def call(self, args: dict[str, Any], action_id: str = _HTTP) -> dict[str, Any]:
        self._send(
            "run_action",
            {
                "action": action_id,
                "request": "r1",
                "args": args,
                "input": "plugin",
                "called_at": "2026-11-21T10:00:00+00:00",
                "card": None,
                "caller": None,
            },
        )
        return self._until("action_result")

    def close(self) -> None:
        self._to_plugin.close()
        self._thread.join(_WAIT)
        self._out.close()
        self._reader.join(_WAIT)
        for stream in (self._in, self._from_plugin):
            stream.close()


_BODIES: dict[str, bytes] = {
    "/big": b"hello \xff" + b"x" * 600_000,
    "/binary": random.Random(1).randbytes(512 * 1024),
    "/japanese": "あ".encode("utf-8") * 400_000,
}


class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list[tuple[str, str, bytes, str | None]] = []

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        _Handler.seen.append((self.command, self.path, self.rfile.read(length), self.headers.get("Content-Type")))
        status = int(self.path[1:]) if self.path[1:].isdigit() else 200
        self.send_response(status)
        self.end_headers()
        self.wfile.write(_BODIES.get(self.path, b"hello"))

    do_GET = do_POST = _answer

    def log_message(self, *_args: Any) -> None:
        pass


@pytest.fixture(name="server")
def server_fixture() -> Iterator[str]:
    _Handler.seen = []
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(_WAIT)


@pytest.fixture(name="host")
def host_fixture() -> Iterator[_Host]:
    host = _Host()
    try:
        yield host
    finally:
        host.close()


def test_post_sends_the_body_and_returns_status(host: _Host, server: str) -> None:
    reply = host.call(
        {"method": "POST", "url": f"{server}/hook", "body": '{"a": 1}', "content_type": "application/json"}
    )

    assert_that(reply).is_equal_to({"request": "r1", "ok": True, "value": {"status": 200, "body": "hello"}})
    assert_that(_Handler.seen).is_equal_to([("POST", "/hook", b'{"a": 1}', "application/json")])


def test_get_returns_body(host: _Host, server: str) -> None:
    reply = host.call({"method": "GET", "url": f"{server}/ping"})

    assert_that(reply["value"]).is_equal_to({"status": 200, "body": "hello"})
    assert_that(_Handler.seen[0][:2]).is_equal_to(("GET", "/ping"))


def test_the_body_is_decoded_leniently_and_truncated_to_512_kib(host: _Host, server: str) -> None:
    body = host.call({"method": "GET", "url": f"{server}/big"})["value"]["body"]

    assert_that(body).starts_with("hello \ufffd")
    assert_that(len(body)).is_equal_to(512 * 1024 - len(b"hello \xff") + len("hello \ufffd"))


@pytest.mark.parametrize("path", ["/binary", "/japanese"])
def test_a_body_that_escapes_to_a_large_encoding_still_fits_the_value_limit(
    host: _Host, server: str, path: str
) -> None:
    reply = host.call({"method": "GET", "url": f"{server}{path}"})

    assert_that(reply["ok"]).is_true()
    assert_that(encode_value(reply["value"])).is_none()
    assert_that(reply["value"]["body"]).is_not_empty()


def test_the_timeout_is_clamped_to_25_seconds(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []

    def fake(_request: Any, timeout: float) -> Any:
        seen.append(timeout)
        raise OSError("stop")

    monkeypatch.setattr(urllib.request, "urlopen", fake)

    host.call({"method": "GET", "url": "http://x/", "timeout": 120})

    assert_that(seen).is_equal_to([25.0])


def test_a_truncated_response_is_an_action_error(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        status = 200

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_exc: Any) -> None:
            return None

        def read(self, _size: int) -> bytes:
            raise http.client.IncompleteRead(b"ab", 10)

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: _Response())

    reply = host.call({"method": "GET", "url": "http://x/"})

    assert_that(reply["ok"]).is_false()
    assert_that(reply["error"]).starts_with("IncompleteRead").contains("bytes read")


def test_error_status_raises_action_error(host: _Host, server: str) -> None:
    reply = host.call({"method": "GET", "url": f"{server}/404"})

    assert_that(reply).is_equal_to({"request": "r1", "ok": False, "error": "HTTP 404"})


def test_only_http_urls_are_allowed(host: _Host) -> None:
    reply = host.call({"method": "GET", "url": "file:///etc/passwd"})

    assert_that(reply["ok"]).is_false()
    assert_that(reply["error"]).is_equal_to("the URL must start with http:// or https://")


def test_unreachable_host_is_a_failure(host: _Host) -> None:
    reply = host.call({"method": "GET", "url": "http://127.0.0.1:9/", "timeout": 1})

    assert_that(reply["ok"]).is_false()
    assert_that(reply["error"]).is_not_empty().does_not_contain("ActionError")


def _manifest_action(action_id: str) -> dict[str, Any]:
    actions = json.loads(_MANIFEST.read_text(encoding="utf-8"))["contributes"]["actions"]
    return next(entry for entry in actions if entry["id"] == action_id)


def test_delay_waits_the_given_seconds(host: _Host) -> None:
    started = time.monotonic()
    result = host.call({"seconds": 0.2}, _DELAY)
    elapsed = time.monotonic() - started

    assert_that(result["ok"]).is_true()
    assert_that(elapsed).is_greater_than_or_equal_to(0.2)


def test_delay_manifest_bounds() -> None:
    entry = _manifest_action(_DELAY)
    field = entry["settings"][0]

    assert_that(entry["timeout"]).is_equal_to(65)
    assert_that(entry["icon"]).is_equal_to("fas:hourglass-half")
    assert_that(field).contains_entry(
        {"key": "seconds"}, {"type": "float"}, {"min": 0}, {"max": 60}, {"default": 1.0}, {"decimals": 2}
    )


def test_sequence_manifest_declares_ten_steps() -> None:
    entry = _manifest_action("com.decker.core.sequence")
    keys = [field["key"] for field in entry["settings"] if "key" in field]

    assert_that(entry["timeout"]).is_equal_to(300)
    assert_that(entry["icon"]).is_equal_to("fas:list-ol")
    assert_that(keys).contains(*[f"step_{n}" for n in range(1, 11)], "stop_on_error")
    params = next(field for field in entry["settings"] if field.get("key") == "step_3_params")
    assert_that(params).contains_entry({"type": "action_params"}, {"action_field": "step_3"})


def test_steps_lists_set_steps_in_order_and_skips_empty() -> None:
    module = _load()
    args = {"step_1": "a.b", "step_1_params": {"x": 1}, "step_2": "", "step_3": "c.d", "step_3_params": "bad"}

    assert_that(module._steps(args)).is_equal_to(
        [(1, "a.b", {"x": 1}), (3, "c.d", {})]
    )  # pylint: disable=protected-access


def test_http_request_url_is_a_template_field_and_body_is_not() -> None:
    fields = {field["key"]: field for field in _manifest_action(_HTTP)["settings"] if "key" in field}

    assert_that(fields["url"]).contains_entry({"template": True})
    assert_that(fields["body"]).does_not_contain_key("template")


@pytest.fixture(name="env")
def env_fixture(tmp_path: Path) -> Iterator[_Env]:
    environment = _Env(tmp_path)
    try:
        yield environment
    finally:
        environment.close()


def _press_vars(env: _Env) -> dict[str, Any]:
    return env.manager.card_variables.snapshot(_TILE.context_id).get("press", {})


def _run_sequence(env: _Env, args: dict[str, Any]) -> dict[str, Any]:
    env.activate_tile()
    env.manager.set_bindings(_TILE, {"press": {"action": "com.decker.core.sequence", "args": args}})
    env.press(_TILE)
    assert_that(_wait(lambda: "status" in _press_vars(env) and "step_2" in _press_vars(env))).is_true()
    return _press_vars(env)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: interpreter symlink")
class TestSequenceSlots:
    def test_each_step_gets_a_slot_status(self, env: _Env, server: str) -> None:
        binding = _run_sequence(
            env,
            {
                "step_1": _DELAY,
                "step_1_params": {"seconds": 0},
                "step_2": _HTTP,
                "step_2_params": {"method": "GET", "url": f"{server}/ok"},
            },
        )

        assert_that(_wait(lambda: _press_vars(env).get("status") == "SUCCESS")).is_true()
        assert_that(binding["step_1"]["status"]).is_equal_to("SUCCESS")
        assert_that(binding["step_2"]["status"]).is_equal_to("SUCCESS")

    def test_a_later_step_reads_an_earlier_steps_status(self, env: _Env, server: str) -> None:
        binding = _run_sequence(
            env,
            {
                "step_1": _HTTP,
                "step_1_params": {"method": "GET", "url": f"{server}/500"},
                "step_2": _HTTP,
                "step_2_params": {"method": "GET", "url": f"{server}/{{press.step_1.status}}"},
                "stop_on_error": False,
            },
        )

        assert_that(binding["step_1"]["status"]).is_equal_to("FAILURE")
        assert_that(_wait(lambda: any(seen[1] == "/FAILURE" for seen in _Handler.seen))).is_true()

    def test_a_json_body_is_sent_unchanged(self, env: _Env, server: str) -> None:
        body = '{"a": {"b": "{press.step_1.status}"}, "c": [{{"d": 1}}]}'
        _run_sequence(
            env,
            {
                "step_1": _DELAY,
                "step_1_params": {"seconds": 0},
                "step_2": _HTTP,
                "step_2_params": {"method": "POST", "url": f"{server}/post", "body": body},
            },
        )

        assert_that(_wait(lambda: any(seen[1] == "/post" for seen in _Handler.seen))).is_true()
        assert_that(next(seen[2] for seen in _Handler.seen if seen[1] == "/post")).is_equal_to(body.encode())
