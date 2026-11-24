import http.client
import http.server
import importlib.util
import json
import os
import queue
import random
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from assertpy import assert_that

from decker_plugin_sdk import run
from decker_plugin_sdk._actions import encode_value

_PLUGIN = Path(__file__).parents[2] / "src" / "decker" / "builtin_plugins" / "com.decker.core" / "plugin.py"
_HTTP = "com.decker.core.httprequest"
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
        actions = {_HTTP: _load().http_request}
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

    def call(self, args: dict[str, Any]) -> dict[str, Any]:
        self._send(
            "run_action",
            {
                "action": _HTTP,
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
