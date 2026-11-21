import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from assertpy import assert_that

from decker.common.options import MAX_OPTIONS, MAX_TEXT
from decker.plugin.protocol import Message
from decker.plugin.runtime.supervisor import PluginSupervisor, SupervisorPolicy
from decker.server.options_runner import OptionsResult, OptionsRunner
from tests.plugin.runtime.test_supervisor import FakeProcess


def _wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class Rig:
    def __init__(self, timeout: float = 2.0) -> None:
        self.procs: dict[str, FakeProcess] = {}
        self.sup = PluginSupervisor(SupervisorPolicy(), self._factory, self._on_message, lambda _pid, _why: None)
        self.runner = OptionsRunner(self.sup, timeout=timeout)
        self.threads: list[threading.Thread] = []
        self.results: dict[str, OptionsResult] = {}

    def _factory(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, pid: str, _cmd: list[str], _cwd: Path, _env: dict[str, str], on_message: Any, on_exit: Any
    ) -> Any:  # a FakeProcess standing in for PluginProcess
        proc = FakeProcess(on_message, on_exit)
        self.procs[pid] = proc
        return proc

    def _on_message(self, plugin_id: str, message: Message) -> None:
        if message.event == "options_result":
            self.runner.on_result(plugin_id, message.payload)

    def plugin(self, plugin_id: str, ready: bool = True) -> None:
        self.sup.register(plugin_id, ["cmd"], Path("."), {}, {"protocol": 2})
        if ready:
            self.sup.acquire(plugin_id)  # a card keeps it running so the process exists for the test
            self.procs[plugin_id].emit("ready")

    def fetch_async(self, plugin_id: str, source: str = "src", key: str = "") -> str:
        key = key or plugin_id

        def run() -> None:
            self.results[key] = self.runner.fetch(plugin_id, source)

        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        return key

    def request(self, plugin_id: str, index: int = 0) -> str:
        def asks() -> list[Message]:
            return [m for m in list(self.procs[plugin_id].sent) if m.event == "get_options"]

        assert_that(_wait_until(lambda: len(asks()) > index)).is_true()
        return str(asks()[index].payload["request"])

    def answer(self, plugin_id: str, payload: dict[str, Any]) -> None:
        self.procs[plugin_id].on_message(Message("options_result", None, payload))

    def finish(self) -> None:
        for thread in self.threads:
            thread.join(5.0)
            assert_that(thread.is_alive()).is_false()

    def refs(self, plugin_id: str) -> int:
        return int(self.sup._entries[plugin_id].refs)


def test_fetch_returns_cleaned_options() -> None:
    rig = Rig()
    rig.plugin("p")
    key = rig.fetch_async("p")

    rig.answer("p", {"request": rig.request("p"), "ok": True, "options": [{"label": "A", "value": "a"}]})
    rig.finish()

    assert_that(rig.results[key]).is_equal_to(OptionsResult(True, ({"label": "A", "value": "a"},)))


def test_fetch_sends_get_options_with_request_and_source() -> None:
    rig = Rig()
    rig.plugin("p")
    rig.fetch_async("p", "devices")

    request = rig.request("p")
    message = [m for m in rig.procs["p"].sent if m.event == "get_options"][0]
    rig.answer("p", {"request": request, "ok": True, "options": []})
    rig.finish()

    assert_that(message.payload).is_equal_to({"request": request, "source": "devices"})
    assert_that(request).is_not_empty()


def test_ok_false_returns_the_error() -> None:
    rig = Rig()
    rig.plugin("p")
    long_error = rig.fetch_async("p", key="long")
    rig.answer("p", {"request": rig.request("p"), "ok": False, "error": "x" * 1000})
    short = rig.fetch_async("p", key="short")
    rig.answer("p", {"request": rig.request("p", 1), "ok": False, "error": "boom"})
    odd = rig.fetch_async("p", key="odd")
    rig.answer("p", {"request": rig.request("p", 2), "ok": False, "error": 5})
    rig.finish()

    assert_that(long_error).is_equal_to("long")
    assert_that(rig.results["long"]).is_equal_to(OptionsResult(False, error="x" * 256))
    assert_that(rig.results[short]).is_equal_to(OptionsResult(False, error="boom"))
    assert_that(rig.results[odd]).is_equal_to(OptionsResult(False, error="the options source failed"))


def test_timeout_returns_timed_out_and_releases_the_plugin() -> None:
    rig = Rig(timeout=0.1)
    rig.plugin("p", ready=False)
    rig.sup.acquire("p")
    before = rig.refs("p")

    result = rig.runner.fetch("p", "src")

    assert_that(result).is_equal_to(OptionsResult(False, error="timed out"))
    assert_that(rig.refs("p")).is_equal_to(before)
    assert_that(rig.runner._pending).is_empty()


def test_lazy_plugin_is_started_and_released_after_the_answer() -> None:
    rig = Rig()
    rig.plugin("p", ready=False)
    key = rig.fetch_async("p")

    assert_that(_wait_until(lambda: "p" in rig.procs and rig.refs("p") == 1)).is_true()
    rig.procs["p"].emit("ready")
    rig.answer("p", {"request": rig.request("p"), "ok": True, "options": []})
    rig.finish()

    assert_that(rig.results[key].ok).is_true()
    assert_that(rig.refs("p")).is_equal_to(0)


def test_failed_plugin_returns_at_once() -> None:
    rig = Rig(timeout=30.0)
    rig.plugin("p", ready=False)
    rig.sup._entries["p"].failed = True

    started = time.monotonic()
    result = rig.runner.fetch("p", "src")

    assert_that(time.monotonic() - started).is_less_than(1.0)
    assert_that(result).is_equal_to(OptionsResult(False, error="the plugin has failed"))
    assert_that(rig.refs("p")).is_equal_to(0)


def test_garbage_options_are_cleaned_not_raised() -> None:
    rig = Rig()
    rig.plugin("p")
    cases: list[Any] = [
        "not a list",
        [{"label": 1, "value": 2}, 5, None, {"label": "x"}],
        [{"label": str(i), "value": str(i)} for i in range(10_000)],
        [{"label": "a", "value": "same"}, {"label": "b", "value": "same"}],
        [{"label": "L" * 100_000, "value": "V" * 100_000}],
    ]
    for index, options in enumerate(cases):
        rig.fetch_async("p", key=str(index))
        rig.answer("p", {"request": rig.request("p", index), "ok": True, "options": options})
        rig.finish()

    assert_that(rig.results["0"]).is_equal_to(OptionsResult(True, ()))
    assert_that(rig.results["1"].options).is_empty()
    assert_that(rig.results["2"].options).is_length(MAX_OPTIONS)
    assert_that(rig.results["3"].options).is_equal_to(({"label": "a", "value": "same"},))
    assert_that(rig.results["4"].options[0]["label"]).is_length(MAX_TEXT)
    assert_that(rig.results["4"].options[0]["value"]).is_length(MAX_TEXT)


def test_result_from_another_plugin_or_unknown_request_is_ignored() -> None:
    rig = Rig(timeout=0.3)
    rig.plugin("p")
    rig.plugin("q")
    key = rig.fetch_async("p")
    request = rig.request("p")

    rig.answer("q", {"request": request, "ok": True, "options": [{"label": "A", "value": "a"}]})
    rig.answer("p", {"request": "nope", "ok": True, "options": []})
    rig.answer("p", {"request": 5, "ok": True})
    rig.runner.on_result("p", [1, 2])  # type: ignore[arg-type]
    rig.finish()

    assert_that(rig.results[key]).is_equal_to(OptionsResult(False, error="timed out"))


def test_duplicate_answer_is_ignored_first_wins() -> None:
    rig = Rig()
    rig.plugin("p")
    key = rig.fetch_async("p")
    request = rig.request("p")

    rig.answer("p", {"request": request, "ok": True, "options": [{"label": "A", "value": "a"}]})
    rig.answer("p", {"request": request, "ok": False, "error": "late"})
    rig.finish()

    assert_that(rig.results[key].options).is_equal_to(({"label": "A", "value": "a"},))


def test_plugin_stopped_wakes_pending_fetches() -> None:
    rig = Rig(timeout=30.0)
    rig.plugin("p")
    rig.plugin("q")
    p_key = rig.fetch_async("p")
    q_key = rig.fetch_async("q")
    rig.request("p")
    rig.request("q")

    rig.runner.plugin_stopped("p")

    assert_that(_wait_until(lambda: p_key in rig.results)).is_true()
    assert_that(rig.results[p_key]).is_equal_to(OptionsResult(False, error="the plugin stopped"))
    assert_that(q_key in rig.results).is_false()
    rig.runner.plugin_stopped("q")
    rig.finish()


def test_stop_all_wakes_pending_and_refuses_new() -> None:
    rig = Rig(timeout=30.0)
    rig.plugin("p")
    key = rig.fetch_async("p")
    rig.request("p")

    rig.runner.stop_all()
    rig.finish()
    later = rig.runner.fetch("p", "src")

    assert_that(rig.results[key]).is_equal_to(OptionsResult(False, error="the server is stopping"))
    assert_that(later).is_equal_to(OptionsResult(False, error="the server is stopping"))
    assert_that(rig.runner._pending).is_empty()


def test_pending_map_is_empty_after_every_outcome() -> None:
    rig = Rig(timeout=0.2)
    rig.plugin("p")
    rig.fetch_async("p", key="ok")
    rig.answer("p", {"request": rig.request("p", 0), "ok": True, "options": []})
    rig.finish()
    rig.fetch_async("p", key="bad")
    rig.answer("p", {"request": rig.request("p", 1), "ok": False, "error": "e"})
    rig.finish()
    rig.fetch_async("p", key="timeout")
    rig.finish()
    rig.fetch_async("p", key="stopped")
    rig.request("p", 3)
    rig.runner.plugin_stopped("p")
    rig.finish()

    assert_that(rig.runner._pending).is_empty()
    assert_that(rig.results["timeout"].error).is_equal_to("timed out")
    assert_that(rig.refs("p")).is_equal_to(1)  # only the rig's own acquire remains


class _RaisingAcquireSupervisor:
    def __init__(self) -> None:
        self.acquired: int = 0
        self.released: int = 0

    def is_failed(self, _plugin_id: str) -> bool:
        return False

    def acquire(self, _plugin_id: str) -> None:
        self.acquired += 1
        raise RuntimeError("acquire failed")

    def release(self, _plugin_id: str) -> None:
        self.released += 1


def test_a_raising_acquire_releases_once_and_leaves_nothing_pending() -> None:
    supervisor = _RaisingAcquireSupervisor()
    runner = OptionsRunner(supervisor, timeout=1.0)  # type: ignore[arg-type]

    try:
        runner.fetch("p", "src")
        raised = False
    except RuntimeError:
        raised = True

    assert_that(raised).is_true()
    assert_that(supervisor.acquired).is_equal_to(1)
    assert_that(supervisor.released).is_equal_to(1)
    assert_that(runner._pending).is_empty()  # pylint: disable=protected-access
