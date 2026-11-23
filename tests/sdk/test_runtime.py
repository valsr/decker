import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from assertpy import assert_that

import decker_plugin_sdk
from decker_plugin_sdk import run
from decker_plugin_sdk._context import CardContext

FIXTURE = Path(__file__).parent / "fixtures" / "echo_plugin.py"


def _load_fixture() -> Any:
    spec = importlib.util.spec_from_file_location("echo_plugin_fixture", FIXTURE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Harness:
    """Runs the SDK loop on os.pipe streams."""

    def __init__(self) -> None:
        self.module = _load_fixture()
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        self.to_plugin = os.fdopen(in_w, "wb")
        self._plugin_in = os.fdopen(in_r, "rb")
        self._plugin_out = os.fdopen(out_w, "wb")
        self.from_plugin = os.fdopen(out_r, "rb")
        self.code: list[int] = []
        actions = {"test.echo.echo": self.module.Echo}
        self.thread = threading.Thread(
            target=lambda: self.code.append(
                run(actions, self._plugin_in, self._plugin_out, {"test.echo.record": self.module.record})
            )
        )
        self.thread.start()

    def send(self, event: str, context: str | None = None, payload: dict | None = None) -> None:
        data: dict[str, Any] = {"event": event}
        if context:
            data["context"] = context
        if payload is not None:
            data["payload"] = payload
        self.send_raw(json.dumps(data).encode() + b"\n")

    def send_raw(self, raw: bytes) -> None:
        self.to_plugin.write(raw)
        self.to_plugin.flush()

    def recv(self) -> dict:
        line = self.from_plugin.readline()
        assert line, "plugin closed stdout"
        return json.loads(line)

    def recv_until(self, event: str) -> dict:
        while True:
            message = self.recv()
            if message["event"] == event:
                return message

    def hello(self) -> None:
        self.send(
            "hello", payload={"protocol": 1, "decker_version": "x", "plugin_id": "test.echo", "global_settings": {}}
        )
        assert_that(self.recv()["event"]).is_equal_to("ready")

    def appear(self, context: str = "c1", extra: dict[str, Any] | None = None, **settings: Any) -> None:
        self.send(
            "will_appear",
            context,
            {
                "card": "test.echo.echo",
                "device": "d",
                "key": {"index": 0, "column": 0, "row": 0},
                "settings": settings,
                "state": 0,
                **(extra or {}),
            },
        )

    def close(self) -> None:
        self.to_plugin.close()
        self.thread.join(5)
        assert_that(self.thread.is_alive()).is_false()
        for stream in (self._plugin_in, self._plugin_out, self.from_plugin):
            stream.close()


@pytest.fixture(name="harness")
def harness_fixture():
    harness = Harness()
    yield harness
    harness.module.BLOCK.set()
    if harness.thread.is_alive():
        harness.send("shutdown")
    harness.thread.join(5)
    for stream in (harness.to_plugin, harness._plugin_in, harness._plugin_out, harness.from_plugin):
        stream.close()


def test_ready_after_hello(harness: Harness) -> None:
    harness.hello()


def test_ping_answered_while_handler_blocks(harness: Harness) -> None:
    harness.hello()
    harness.appear(mode="block")
    harness.send("key_down", "c1")
    harness.send("ping")
    assert_that(harness.recv()["event"]).is_equal_to("pong")
    harness.module.BLOCK.set()
    assert_that(harness.recv_until("set_title")["payload"]["title"]).is_equal_to("pressed")


def test_key_down_dispatches_handler_and_set_title(harness: Harness) -> None:
    harness.hello()
    harness.appear()
    harness.send("key_down", "c1")
    message = harness.recv()
    assert_that(message).is_equal_to({"event": "set_title", "context": "c1", "payload": {"title": "pressed"}})


def test_press_event_is_ignored_by_the_sdk(harness: Harness) -> None:
    harness.hello()
    harness.appear()
    for gesture in ("press", "double_press", "long_press"):
        harness.send(gesture, "c1")
    harness.send("ping")
    assert_that(harness.recv()["event"]).is_equal_to("pong")
    assert_that(harness.module.SEEN).is_empty()


def test_will_appear_and_settings_changed_fill_bindings(harness: Harness) -> None:
    harness.hello()
    binding = {"action": "a.b", "args": {"n": 1}}
    harness.appear(extra={"bindings": {"press": binding}})
    harness.send("key_down", "c1")
    assert_that(harness.recv_until("set_title")["payload"]["title"]).is_equal_to("pressed")
    assert_that(harness.module.BINDINGS[-1]).is_equal_to({"press": binding})
    harness.send("settings_changed", "c1", {"settings": {}, "bindings": {"long_press": binding}})
    harness.send("key_down", "c1")
    assert_that(harness.recv_until("set_title")["payload"]["title"]).is_equal_to("pressed")
    assert_that(harness.module.BINDINGS[-1]).is_equal_to({"long_press": binding})


def test_will_appear_fills_size(harness: Harness) -> None:
    harness.hello()
    harness.appear(extra={"size": [96, 48]})
    harness.send("key_down", "c1")
    harness.recv_until("set_title")
    assert_that(harness.module.SIZES[-1]).is_equal_to((96, 48))


def test_sdk_exports_no_gesture() -> None:
    assert_that(hasattr(decker_plugin_sdk, "Gesture")).is_false()
    assert_that(decker_plugin_sdk.__all__).does_not_contain("Gesture")


def test_handler_exception_logged_not_fatal(harness: Harness) -> None:
    harness.hello()
    harness.appear(mode="boom")
    harness.send("key_down", "c1")
    log = harness.recv()
    assert_that(log["event"]).is_equal_to("log")
    assert_that(log["payload"]["level"]).is_equal_to("error")
    harness.send("settings_changed", "c1", {"settings": {}})
    harness.send("key_down", "c1")
    assert_that(harness.recv_until("set_title")["payload"]["title"]).is_equal_to("pressed")


def test_events_for_one_context_run_in_order(harness: Harness) -> None:
    harness.hello()
    harness.appear(mode="slow", tag="first")
    harness.send("key_down", "c1")
    harness.send("settings_changed", "c1", {"settings": {"tag": "second"}})
    harness.send("key_down", "c1")
    harness.recv_until("set_title")
    harness.recv_until("set_title")
    assert_that(harness.module.SEEN).is_equal_to(["first", "second"])


def test_malformed_lines_are_skipped(harness: Harness) -> None:
    harness.hello()
    harness.send_raw(b"not json\n[1,2]\n" + b"[" * 100000 + b"\n" + b"\xff\xfe\n" + b'{"event": 5}\n')
    harness.send("ping")
    assert_that(harness.recv()["event"]).is_equal_to("pong")


def test_exit_on_shutdown(harness: Harness) -> None:
    harness.hello()
    harness.send("shutdown")
    harness.thread.join(5)
    assert_that(harness.code).is_equal_to([0])


def test_exit_on_eof(harness: Harness) -> None:
    harness.hello()
    harness.to_plugin.close()
    harness.thread.join(5)
    assert_that(harness.code).is_equal_to([0])


def test_sdk_does_not_import_decker() -> None:
    code = (
        "import decker_plugin_sdk, sys; assert not any(m == 'decker' or m.startswith('decker.') for m in sys.modules)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert_that(result.returncode).described_as(result.stderr).is_equal_to(0)


def test_module_entry_point_runs_plugin_file() -> None:
    root = Path(__file__).parents[2]
    with subprocess.Popen(
        [sys.executable, "-m", "decker_plugin_sdk", str(FIXTURE)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=root,
    ) as proc:
        assert proc.stdin and proc.stdout
        proc.stdin.write(b'{"event":"hello","payload":{"protocol":1,"global_settings":{}}}\n')
        proc.stdin.flush()
        assert_that(json.loads(proc.stdout.readline())["event"]).is_equal_to("ready")
        proc.stdin.write(b'{"event":"shutdown"}\n')
        proc.stdin.flush()
        assert_that(proc.wait(10)).is_equal_to(0)


def _spawn() -> subprocess.Popen:
    return subprocess.Popen(  # pylint: disable=consider-using-with
        [sys.executable, "-m", "decker_plugin_sdk", str(FIXTURE)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=Path(__file__).parents[2],
    )


def _feed(proc: subprocess.Popen, *messages: dict) -> None:
    assert proc.stdin
    for message in messages:
        proc.stdin.write(json.dumps(message).encode() + b"\n")
    proc.stdin.flush()


def _appear(context: str, **settings: Any) -> dict:
    return {
        "event": "will_appear",
        "context": context,
        "payload": {
            "card": "test.echo.echo",
            "device": "d",
            "key": {"index": 0, "column": 0, "row": 0},
            "settings": settings,
            "state": 0,
        },
    }


def test_shutdown_stops_promptly_with_blocked_handler() -> None:
    with _spawn() as proc:
        assert proc.stdout
        _feed(proc, {"event": "hello", "payload": {}})
        assert_that(json.loads(proc.stdout.readline())["event"]).is_equal_to("ready")
        _feed(
            proc,
            _appear("c1", mode="block"),
            {"event": "key_down", "context": "c1"},
            {"event": "key_down", "context": "c1"},
        )
        _feed(proc, {"event": "shutdown"})
        assert_that(proc.wait(2)).is_equal_to(0)
        assert_that(proc.stdout.read()).is_equal_to(b"")  # queued key_down never ran or wrote


def test_stray_fd1_writes_do_not_corrupt_protocol() -> None:
    with _spawn() as proc:
        assert proc.stdout and proc.stderr
        _feed(proc, {"event": "hello", "payload": {}})
        assert_that(json.loads(proc.stdout.readline())["event"]).is_equal_to("ready")
        _feed(proc, _appear("c1", mode="junk"), {"event": "key_down", "context": "c1"})
        assert_that(json.loads(proc.stdout.readline())["payload"]["title"]).is_equal_to("pressed")
        _feed(proc, {"event": "shutdown"})
        assert_that(proc.wait(5)).is_equal_to(0)
        assert_that(proc.stdout.read()).is_equal_to(b"")
        err = proc.stderr.read()
        assert_that(err).contains(b"junk-from-child").contains(b"junk-from-fd1").contains(b"junk-from-print")


def _context(sent: list[tuple[str, str | None, dict[str, Any]]]) -> Any:
    from decker_plugin_sdk import CardContext

    return CardContext(
        context="c1",
        card="a",
        device="d",
        key_index=0,
        column=0,
        row=0,
        _send=lambda event, context, payload: sent.append((event, context, payload)),
    )


def test_set_icon_emits_set_image_with_icon_and_color() -> None:
    sent: list[tuple[str, str | None, dict[str, Any]]] = []

    _context(sent).set_icon("pack:p/a.svg", "#ff0000")

    assert_that(sent).is_equal_to([("set_image", "c1", {"icon": "pack:p/a.svg", "color": "#ff0000"})])


def test_set_icon_without_color_omits_it() -> None:
    sent: list[tuple[str, str | None, dict[str, Any]]] = []

    _context(sent).set_icon("pack:p/a.svg")

    assert_that(sent).is_equal_to([("set_image", "c1", {"icon": "pack:p/a.svg"})])


def _context_with_log() -> tuple[CardContext, list[tuple[str, str | None, dict[str, Any]]]]:
    sent: list[tuple[str, str | None, dict[str, Any]]] = []
    ctx = CardContext("c1", "a", "d", 0, 0, 0, _send=lambda e, c, p: sent.append((e, c, p)))
    return ctx, sent


def test_switch_page_emits_to_and_wrap() -> None:
    ctx, sent = _context_with_log()

    ctx.switch_page("previous", wrap=True)

    assert_that(sent).is_equal_to([("switch_page", "c1", {"to": "previous", "wrap": True})])


def test_switch_page_defaults() -> None:
    ctx, sent = _context_with_log()

    ctx.switch_page()

    assert_that(sent).is_equal_to([("switch_page", "c1", {"to": "next", "wrap": False})])


def test_go_to_page_emits_page_id() -> None:
    ctx, sent = _context_with_log()

    ctx.go_to_page("p2")

    assert_that(sent).is_equal_to([("switch_page", "c1", {"page_id": "p2"})])


def test_set_settings_keeps_the_local_keys_it_was_not_given() -> None:
    ctx, _sent = _context_with_log()
    ctx.settings = {"page": {"page_id": "p1"}, "count": 1}

    ctx.set_settings({"count": 2})

    assert_that(ctx.settings).is_equal_to({"page": {"page_id": "p1"}, "count": 2})


def test_switch_profile_emits_to_and_wrap() -> None:
    ctx, sent = _context_with_log()
    ctx.switch_profile("previous", wrap=True)

    assert_that(sent).is_equal_to([("switch_profile", "c1", {"to": "previous", "wrap": True})])


def test_switch_profile_defaults() -> None:
    ctx, sent = _context_with_log()
    ctx.switch_profile()

    assert_that(sent).is_equal_to([("switch_profile", "c1", {"to": "next", "wrap": False})])


def test_go_to_profile_emits_the_id() -> None:
    ctx, sent = _context_with_log()
    ctx.go_to_profile(2)

    assert_that(sent).is_equal_to([("switch_profile", "c1", {"profile_id": 2})])


class TestActions:
    def test_run_action_calls_the_function_and_reports_ok(self):
        harness = Harness()
        try:
            harness.hello()
            harness.send(
                "run_action", None, {"action": "test.echo.record", "request": "r1", "args": {"n": 3}, "input": "plugin"}
            )

            result = harness.recv_until("action_result")

            assert_that(result["payload"]).is_equal_to({"request": "r1", "ok": True})
            assert_that(harness.module.ACTION_CALLS).contains({"n": 3})
        finally:
            harness.close()

    def test_a_raising_action_reports_the_error(self):
        harness = Harness()
        try:
            harness.hello()
            harness.send(
                "run_action",
                None,
                {"action": "test.echo.record", "request": "r2", "args": {"fail": True}, "input": "plugin"},
            )

            payload = harness.recv_until("action_result")["payload"]

            assert_that(payload["ok"]).is_false()
            assert_that(payload["request"]).is_equal_to("r2")
            assert_that(payload["error"]).contains("ValueError", "nope")
        finally:
            harness.close()

    def test_an_unknown_action_is_answered_not_ignored(self):
        harness = Harness()
        try:
            harness.hello()
            harness.send("run_action", None, {"action": "test.echo.missing", "request": "r3"})

            payload = harness.recv_until("action_result")["payload"]

            assert_that(payload).is_equal_to({"request": "r3", "ok": False, "error": "no such action"})
        finally:
            harness.close()
