import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from assertpy import assert_that

from decker.plugin.loader import InstalledPlugin
from decker.plugin.manifest import PluginManifest
from decker.plugin.protocol import Message
from decker.server.options_runner import OptionsResult
from decker.server.plugins import PluginManager
from tests.plugin.runtime.test_supervisor import FakeProcess
from tests.server.test_plugins_variables import (
    _fake_processes,
    _manager,
    _started,
    _wait_until,
)

_PLUGIN_ID = "com.t.opts"


def _choice(source: str) -> dict[str, Any]:
    return {"key": "device", "type": "choice", "label": "Device", "options_source": source}


def _options_plugin(
    tmp_path: Path, card: str | None = None, action: str | None = None, global_: str | None = None
) -> InstalledPlugin:
    contributes: dict[str, Any] = {}
    if card:
        contributes["cards"] = [{"id": f"{_PLUGIN_ID}.card", "name": "C", "settings": [_choice(card)]}]
    if action:
        contributes["actions"] = [{"id": f"{_PLUGIN_ID}.act", "name": "A", "settings": [_choice(action)]}]
    data = {
        "format": 3,
        "id": _PLUGIN_ID,
        "name": "O",
        "version": "1.0.0",
        "author": "a",
        "decker": ">=0.7",
        "runtime": {"type": "python", "entry": "plugin.py"},
        "global_settings": ([_choice(global_)] if global_ else []) + [{"section": "Heading"}],
        "contributes": contributes,
    }
    return InstalledPlugin(PluginManifest.from_dict(data), tmp_path)


def _asks(proc: FakeProcess) -> list[Message]:
    return [m for m in list(proc.sent) if m.event == "get_options"]


def _asked(proc: FakeProcess, count: int) -> Callable[[], bool]:
    return lambda: len(_asks(proc)) >= count


def _fetch_async(manager: PluginManager, results: list[OptionsResult], source: str = "devices") -> threading.Thread:
    thread = threading.Thread(target=lambda: results.append(manager.plugin_options(_PLUGIN_ID, source)), daemon=True)
    thread.start()
    return thread


def _answer(proc: FakeProcess, payload: dict[str, Any]) -> None:
    assert_that(_wait_until(lambda: len(_asks(proc)) > 0)).is_true()
    proc.on_message(Message("options_result", None, {"request": _asks(proc)[0].payload["request"], **payload}))


def test_plugin_options_asks_the_declaring_plugin_and_returns_cleaned_options(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        proc = _started(manager, procs, _options_plugin(tmp_path, card="devices"))
        results: list[OptionsResult] = []
        thread = _fetch_async(manager, results)

        _answer(proc, {"ok": True, "options": [{"label": "Speakers", "value": "spk"}, {"bad": 1}]})
        thread.join(5.0)

        assert_that(_asks(proc)[0].payload["source"]).is_equal_to("devices")
        assert_that(results).is_equal_to([OptionsResult(True, ({"label": "Speakers", "value": "spk"},))])
    finally:
        manager.destroy()


def test_source_declared_in_action_and_global_settings_is_accepted(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        proc = _started(manager, procs, _options_plugin(tmp_path, action="from_action", global_="from_global"))
        for index, source in enumerate(("from_action", "from_global")):
            results: list[OptionsResult] = []
            thread = _fetch_async(manager, results, source)
            assert_that(_wait_until(_asked(proc, index + 1))).is_true()
            request = _asks(proc)[index].payload["request"]
            proc.on_message(Message("options_result", None, {"request": request, "ok": True, "options": []}))
            thread.join(5.0)
            assert_that(results).is_equal_to([OptionsResult(True, ())])
    finally:
        manager.destroy()


def test_undeclared_source_is_refused_without_a_request(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        proc = _started(manager, procs, _options_plugin(tmp_path, card="devices"))

        result = manager.plugin_options(_PLUGIN_ID, "other")

        assert_that(result).is_equal_to(OptionsResult(False, error="unknown options source"))
        assert_that(_asks(proc)).is_empty()
    finally:
        manager.destroy()


def test_undeclared_source_does_not_start_a_lazy_plugin(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        manager.add_plugin(_options_plugin(tmp_path, card="devices"))

        result = manager.plugin_options(_PLUGIN_ID, "other")

        assert_that(result.error).is_equal_to("unknown options source")
        assert_that(procs).is_empty()
    finally:
        manager.destroy()


def test_unknown_plugin_is_refused(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        assert_that(manager.plugin_options("com.t.none", "devices")).is_equal_to(
            OptionsResult(False, error="unknown plugin")
        )
    finally:
        manager.destroy()


def test_options_result_routes_to_the_runner(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        proc = _started(manager, procs, _options_plugin(tmp_path, card="devices"))
        results: list[OptionsResult] = []
        thread = _fetch_async(manager, results)

        _answer(proc, {"ok": False, "error": "no audio server"})
        thread.join(5.0)

        assert_that(results).is_equal_to([OptionsResult(False, error="no audio server")])
    finally:
        manager.destroy()


def test_plugin_exit_fails_a_pending_fetch_at_once(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        proc = _started(manager, procs, _options_plugin(tmp_path, card="devices"))
        results: list[OptionsResult] = []
        thread = _fetch_async(manager, results)
        assert_that(_wait_until(lambda: len(_asks(proc)) > 0)).is_true()

        proc.crash()
        thread.join(5.0)

        assert_that(thread.is_alive()).is_false()
        assert_that(results).is_equal_to([OptionsResult(False, error="the plugin stopped")])
    finally:
        manager.destroy()


def test_destroy_wakes_pending_fetches(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    proc = _started(manager, procs, _options_plugin(tmp_path, card="devices"))
    results: list[OptionsResult] = []
    thread = _fetch_async(manager, results)
    assert_that(_wait_until(lambda: len(_asks(proc)) > 0)).is_true()

    manager.destroy()
    thread.join(5.0)

    assert_that(thread.is_alive()).is_false()
    assert_that(results[0].ok).is_false()


class _AlwaysEqual:
    __hash__ = None  # type: ignore[assignment]

    def __eq__(self, other: object) -> bool:
        return True


def test_a_source_that_equals_everything_is_refused_without_a_request(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        proc = _started(manager, procs, _options_plugin(tmp_path, card="devices"))

        result = manager.plugin_options(_PLUGIN_ID, _AlwaysEqual())  # type: ignore[arg-type]

        assert_that(result).is_equal_to(OptionsResult(False, error="unknown options source"))
        assert_that(_asks(proc)).is_empty()
    finally:
        manager.destroy()


def test_an_unhashable_plugin_id_is_refused_without_raising(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        result = manager.plugin_options(["x"], "devices")  # type: ignore[arg-type]

        assert_that(result).is_equal_to(OptionsResult(False, error="unknown plugin"))
    finally:
        manager.destroy()


def test_a_malformed_source_name_is_refused(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        proc = _started(manager, procs, _options_plugin(tmp_path, card="devices"))

        result = manager.plugin_options(_PLUGIN_ID, "Bad-Name")

        assert_that(result).is_equal_to(OptionsResult(False, error="unknown options source"))
        assert_that(_asks(proc)).is_empty()
    finally:
        manager.destroy()


def test_a_plugin_cannot_be_asked_for_another_plugins_source(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        proc_a = _started(manager, procs, _options_plugin(tmp_path, card="only_a"))
        other_id = "com.t.other"
        data = {
            "format": 3,
            "id": other_id,
            "name": "P",
            "version": "1.0.0",
            "author": "a",
            "decker": ">=0.7",
            "runtime": {"type": "python", "entry": "plugin.py"},
            "contributes": {"cards": [{"id": f"{other_id}.card", "name": "C", "settings": [_choice("only_b")]}]},
        }
        proc_b = _started(manager, procs, InstalledPlugin(PluginManifest.from_dict(data), tmp_path))

        result = manager.plugin_options(other_id, "only_a")

        assert_that(result).is_equal_to(OptionsResult(False, error="unknown options source"))
        assert_that(_asks(proc_a)).is_empty()
        assert_that(_asks(proc_b)).is_empty()
    finally:
        manager.destroy()
