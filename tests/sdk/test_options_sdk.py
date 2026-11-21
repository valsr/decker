import json
import os
import queue
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from assertpy import assert_that

import decker_plugin_sdk as sdk
from decker_plugin_sdk import _options, _variables, run

WAIT = 5.0
READ_DEADLINE = 5.0


class Rig:
    """Runs the SDK loop on os.pipe streams."""

    def __init__(self) -> None:
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        self.to_plugin = os.fdopen(in_w, "wb")
        self._plugin_in = os.fdopen(in_r, "rb")
        self._plugin_out = os.fdopen(out_w, "wb")
        self.from_plugin = os.fdopen(out_r, "rb")
        self.code: list[int] = []
        self.thread = threading.Thread(target=lambda: self.code.append(run({}, self._plugin_in, self._plugin_out, {})))
        self.thread.start()
        self._lines: queue.Queue[bytes] = queue.Queue()
        self._reader = threading.Thread(target=self._read_lines, daemon=True)
        self._reader.start()

    def send(self, event: str, payload: dict | None = None) -> None:
        data: dict[str, Any] = {"event": event}
        if payload is not None:
            data["payload"] = payload
        self.to_plugin.write(json.dumps(data).encode() + b"\n")
        self.to_plugin.flush()

    def _read_lines(self) -> None:
        while True:
            line = self.from_plugin.readline()
            self._lines.put(line)
            if not line:
                return

    def recv_until(self, event: str) -> dict:
        deadline = time.monotonic() + READ_DEADLINE
        while True:
            try:
                line = self._lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise AssertionError(f"no {event} within {READ_DEADLINE}s") from None
            assert line, f"plugin closed stdout before {event}"
            message = json.loads(line)
            if message["event"] == event:
                return message

    def ask(self, request: Any, source: Any) -> dict:
        self.send("get_options", {"request": request, "source": source})
        return self.recv_until("options_result")["payload"]

    def stop(self) -> None:
        try:
            if self.thread.is_alive():
                self.send("shutdown")
        except OSError:
            pass
        self.thread.join(WAIT)
        self._plugin_out.close()  # EOF for the reader thread
        self._reader.join(WAIT)
        for stream in (self.to_plugin, self._plugin_in, self.from_plugin):
            stream.close()


@pytest.fixture(name="rig")
def rig_fixture() -> Iterator[Rig]:
    _variables.reset()
    _options.reset()
    made = Rig()
    yield made
    made.stop()
    _variables.reset()
    _options.reset()


def test_get_options_answers_with_dicts(rig: Rig) -> None:
    @sdk.options("sinks")
    def sinks() -> list[dict[str, str]]:
        return [{"label": "Speakers", "value": "spk"}, {"label": "Mic", "value": "mic"}]

    assert_that(rig.ask("r1", "sinks")).is_equal_to(
        {
            "request": "r1",
            "ok": True,
            "options": [{"label": "Speakers", "value": "spk"}, {"label": "Mic", "value": "mic"}],
        }
    )


def test_tuples_and_strings_are_normalised(rig: Rig) -> None:
    @sdk.options("mixed")
    def mixed() -> list[Any]:
        return [("A", "a"), ["B", "b"], "c", {"label": "D", "value": "d"}, 5, None, ("x",), ("x", 1)]

    assert_that(rig.ask("r2", "mixed")["options"]).is_equal_to(
        [
            {"label": "A", "value": "a"},
            {"label": "B", "value": "b"},
            {"label": "c", "value": "c"},
            {"label": "D", "value": "d"},
        ]
    )


def test_async_function_is_awaited(rig: Rig) -> None:
    @sdk.options("later")
    async def later() -> list[str]:
        return ["one", "two"]

    payload = rig.ask("r3", "later")
    assert_that(payload["ok"]).is_true()
    assert_that([o["value"] for o in payload["options"]]).is_equal_to(["one", "two"])


def test_unknown_source_answers_not_ok(rig: Rig) -> None:
    assert_that(rig.ask("r4", "ghost")).is_equal_to({"request": "r4", "ok": False, "error": "unknown options source"})
    assert_that(rig.ask("r5", 7)).is_equal_to({"request": "r5", "ok": False, "error": "unknown options source"})


def test_exception_answers_not_ok_and_logs(rig: Rig) -> None:
    @sdk.options("broken")
    def broken() -> list[str]:
        raise RuntimeError("x" * 400)

    rig.send("get_options", {"request": "r6", "source": "broken"})
    payload = rig.recv_until("options_result")["payload"]
    assert_that(payload["ok"]).is_false()
    assert_that(payload["error"]).starts_with("RuntimeError: xxx")
    assert_that(len(payload["error"])).is_equal_to(256)
    log = rig.recv_until("log")["payload"]
    assert_that(log["level"]).is_equal_to("error")
    assert_that(log["message"]).contains("RuntimeError")
    assert_that(rig.ask("r7", "ghost")["ok"]).is_false()  # the runtime survived


def test_non_list_result_answers_not_ok(rig: Rig) -> None:
    @sdk.options("bad")
    def bad() -> Any:
        return {"label": "a", "value": "b"}

    assert_that(rig.ask("r8", "bad")).is_equal_to({"request": "r8", "ok": False, "error": "options must be a list"})


def test_non_string_request_is_ignored(rig: Rig) -> None:
    @sdk.options("fine")
    def fine() -> list[str]:
        return ["a"]

    rig.send("get_options", {"request": 9.5, "source": "fine"})
    rig.send("get_options", {"source": "fine"})
    assert_that(rig.ask("r10", "fine")["request"]).is_equal_to("r10")


def test_garbage_entries_are_dropped_and_capped(rig: Rig) -> None:
    @sdk.options("big")
    def big() -> list[Any]:
        entries: list[Any] = [("L" * 1000, "v" * 1000), ("dup", "v" * 1000), None, 3, {"label": 1, "value": "z"}]
        entries += [(f"n{i}", f"n{i}") for i in range(10_000)]
        return entries

    options = rig.ask("r11", "big")["options"]
    assert_that(options).is_length(256)
    assert_that(options[0]).is_equal_to({"label": "L" * 256, "value": "v" * 256})
    assert_that({o["value"] for o in options}).is_length(256)
    assert_that(options[1]["value"]).is_equal_to("n0")


def test_decorator_registers_per_source_and_reset_clears(rig: Rig) -> None:
    @sdk.options("a")
    def first() -> list[str]:
        return ["1"]

    @sdk.options("a")
    def second() -> list[str]:
        return ["2"]

    @sdk.options("b")
    def other() -> list[str]:
        return ["3"]

    assert_that(rig.ask("r1", "a")["options"][0]["value"]).is_equal_to("2")
    assert_that(rig.ask("r2", "b")["options"][0]["value"]).is_equal_to("3")
    _options.reset()
    assert_that(rig.ask("r3", "a")["ok"]).is_false()


def test_unencodable_entries_are_dropped(rig: Rig) -> None:
    @sdk.options("files")
    def files() -> list[Any]:
        return ["ok", "bad\udcff", ("fine", "x\udcff")]

    payload = rig.ask("s1", "files")
    assert_that(payload).is_equal_to({"request": "s1", "ok": True, "options": [{"label": "ok", "value": "ok"}]})


def test_all_unencodable_answers_empty(rig: Rig) -> None:
    @sdk.options("files")
    def files() -> list[str]:
        return ["a\udcff", "\udc80"]

    assert_that(rig.ask("s2", "files")).is_equal_to({"request": "s2", "ok": True, "options": []})


def test_exception_with_surrogate_answers_sanitised_error(rig: Rig) -> None:
    @sdk.options("broken")
    def broken() -> list[str]:
        raise RuntimeError("bad\udcffname")

    payload = rig.ask("s3", "broken")
    assert_that(payload["ok"]).is_false()
    assert_that(payload["error"]).contains("RuntimeError").contains("bad")
    payload["error"].encode("utf-8")
    assert_that(rig.ask("s4", "ghost")["ok"]).is_false()


def test_exception_whose_str_raises_still_answers(rig: Rig) -> None:
    class Nasty(Exception):
        def __str__(self) -> str:
            raise ValueError("no str")

    @sdk.options("nasty")
    def nasty() -> list[str]:
        raise Nasty()

    payload = rig.ask("s5", "nasty")
    assert_that(payload).is_equal_to({"request": "s5", "ok": False, "error": "Nasty: Nasty"})


def test_final_send_failure_falls_back_to_not_ok(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    @sdk.options("slip")
    def slip() -> list[str]:
        return ["a"]

    monkeypatch.setattr(_options, "normalise", lambda result: [{"label": "x\udcff", "value": "y"}])
    assert_that(rig.ask("s6", "slip")).is_equal_to(
        {"request": "s6", "ok": False, "error": "options could not be encoded"}
    )


def test_non_encodable_request_id_is_ignored() -> None:
    sent: list[tuple[str, Any]] = []
    host = _options.OptionsHost(lambda event, context, payload: sent.append((event, payload)), lambda message: None)
    _options.reset()

    @sdk.options("fine")
    def fine() -> list[str]:
        return ["a"]

    host.answer({"request": "\udcff", "source": "fine"})

    assert_that(sent).is_empty()


def test_a_raising_supplier_sends_one_result_and_logs_once() -> None:
    sent: list[tuple[str, Any]] = []
    logged: list[str] = []
    host = _options.OptionsHost(lambda event, context, payload: sent.append((event, payload)), logged.append)
    _options.reset()

    @sdk.options("broken")
    def broken() -> list[str]:
        raise RuntimeError("boom")

    host.answer({"request": "r1", "source": "broken"})

    assert_that(sent).is_length(1)
    assert_that(sent[0][0]).is_equal_to("options_result")
    assert_that(sent[0][1]).is_equal_to({"request": "r1", "ok": False, "error": "RuntimeError: boom"})
    assert_that(logged).is_length(1)


def test_reply_is_sent_even_when_logging_raises() -> None:
    sent: list[tuple[str, Any]] = []

    def log_error(message: str) -> None:
        raise OSError("stderr is broken")

    host = _options.OptionsHost(lambda event, context, payload: sent.append((event, payload)), log_error)
    _options.reset()

    @sdk.options("broken")
    def broken() -> list[str]:
        raise RuntimeError("boom")

    try:
        host.answer({"request": "s8", "source": "broken"})
    finally:
        _options.reset()
    assert_that(sent).is_length(1)
    assert_that(sent[0][1]).is_equal_to({"request": "s8", "ok": False, "error": "RuntimeError: boom"})
