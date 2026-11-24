import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from assertpy import assert_that

from decker.common.variables import MAX_REGISTERED
from decker.plugin.loader import InstalledPlugin
from decker.plugin.manifest import ManifestValidationError, PluginManifest, VariableSpec
from decker.plugin.protocol import Message
from decker.plugin.settings_store import PluginSettingsStore
from decker.plugin.target import CardTarget
from decker.server import plugins as plugins_module
from decker.server.page_vars import VariableChain
from decker.server.plugins import PluginManager
from decker.server.variables import VariableConflict
from tests.plugin.runtime.test_supervisor import FakeProcess
from tests.server.test_plugins import FakePages


def _plugin(
    tmp_path: Path, plugin_id: str, variables: list[dict[str, Any]], cards: list[dict[str, Any]] | None = None
) -> InstalledPlugin:
    data = {
        "format": 3,
        "id": plugin_id,
        "name": "V",
        "version": "1.0.0",
        "author": "a",
        "decker": ">=0.7",
        "runtime": {"type": "python", "entry": "plugin.py"},
        "contributes": {"variables": variables, **({"cards": cards} if cards else {})},
    }
    return InstalledPlugin(PluginManifest.from_dict(data), tmp_path)


def _manager(tmp_path: Path) -> PluginManager:
    manager = PluginManager(tmp_path / "plugins", settings_store=PluginSettingsStore(tmp_path / "plugins.db"))
    manager._sdk_path = tmp_path  # pylint: disable=protected-access  # no real process: see _fake_processes
    _fake_processes(manager)
    return manager


def test_adding_plugin_declares_variables(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        manager.add_plugin(_plugin(tmp_path, "com.t.one", [{"name": "media_volume"}, {"pattern": "m_{x}_v"}]))

        assert_that(manager.variables.owner_of("media_volume")).is_equal_to("com.t.one")
        assert_that(manager.variables.owner_of("m_a_v")).is_equal_to("com.t.one")
        assert_that(manager.load_failures()).is_empty()
    finally:
        manager.destroy()


def test_second_plugin_with_same_name_fails_to_load_and_first_keeps_working(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        manager.add_plugin(_plugin(tmp_path, "com.t.one", [{"name": "media_volume"}]))
        manager.add_plugin(_plugin(tmp_path, "com.t.two", [{"name": "other_one"}, {"name": "media_volume"}]))

        assert_that(manager.variables.owner_of("media_volume")).is_equal_to("com.t.one")
        assert_that(manager.variables.owner_of("other_one")).is_none()
        failures = manager.load_failures()
        assert_that([f.plugin_uuid for f in failures]).is_equal_to(["com.t.two"])
        assert_that(failures[0].error).contains("media_volume")
        assert_that(manager._supervisor._entries).contains_key("com.t.one").does_not_contain_key("com.t.two")
    finally:
        manager.destroy()


def test_removing_plugin_frees_its_names(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        manager.add_plugin(_plugin(tmp_path, "com.t.one", [{"name": "media_volume"}]))
        manager.remove_plugin("com.t.one")

        assert_that(manager.variables.owner_of("media_volume")).is_none()
        manager.add_plugin(_plugin(tmp_path, "com.t.two", [{"name": "media_volume"}]))
        assert_that(manager.variables.owner_of("media_volume")).is_equal_to("com.t.two")
    finally:
        manager.destroy()


def _fake_processes(manager: PluginManager) -> dict[str, FakeProcess]:
    procs: dict[str, FakeProcess] = {}

    def factory(pid: str, _cmd: list[str], _cwd: Path, _env: dict[str, str], on_message: Any, on_exit: Any) -> Any:
        procs[pid] = FakeProcess(on_message, on_exit)
        return procs[pid]

    manager._supervisor._factory = factory  # pylint: disable=protected-access
    return procs


def _started(manager: PluginManager, procs: dict[str, FakeProcess], plugin: InstalledPlugin) -> FakeProcess:
    manager.add_plugin(plugin)
    manager._supervisor.acquire(plugin.manifest.id)  # pylint: disable=protected-access
    proc = procs[plugin.manifest.id]
    proc.emit("ready")
    return proc


def _wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _asks(proc: FakeProcess, name: str) -> list[Message]:
    return [m for m in list(proc.sent) if m.event == "get_variable" and m.payload.get("name") == name]


def _pushes(proc: FakeProcess) -> list[dict[str, Any]]:
    return [m.payload for m in list(proc.sent) if m.event == "variables_changed"]


def _send(proc: FakeProcess, event: str, payload: dict[str, Any]) -> None:
    proc.on_message(Message(event, None, payload))


def _answer(proc: FakeProcess, name: str, value: Any, index: int = 0) -> None:
    assert_that(_wait_until(lambda: len(_asks(proc, name)) > index)).is_true()
    request = _asks(proc, name)[index].payload["request"]
    _send(proc, "variable_value", {"request": request, "ok": True, "value": value})


def test_hello_protocol_is_5(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        proc = _started(manager, procs, _plugin(tmp_path, "com.t.one", [{"name": "media_volume"}]))

        assert_that(proc.sent[0].event).is_equal_to("hello")
        assert_that(proc.sent[0].payload["protocol"]).is_equal_to(5)
    finally:
        manager.destroy()


def test_ready_sends_snapshot_then_asks_registered_names(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        one = _started(manager, procs, _plugin(tmp_path, "com.t.one", [{"name": "a_vol"}]))
        _answer(one, "a_vol", 5)
        assert_that(_wait_until(lambda: manager.variables.snapshot() == {"a_vol": "5"})).is_true()

        two = _started(manager, procs, _plugin(tmp_path, "com.t.two", [{"name": "b_x"}, {"name": "b_y"}]))

        assert_that(_wait_until(lambda: len(_asks(two, "b_x")) == 1 and len(_asks(two, "b_y")) == 1)).is_true()
        events = [m.event for m in two.sent]
        assert_that(events.index("variables_changed")).is_less_than(events.index("get_variable"))
        assert_that(_pushes(two)[0]).is_equal_to({"set": {"a_vol": "5"}, "removed": []})
    finally:
        manager.destroy()


def test_register_concrete_name_triggers_ask(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        proc = _started(manager, procs, _plugin(tmp_path, "com.t.one", [{"pattern": "m_{x}_v"}]))

        _send(proc, "register_variable", {"name": "m_a_v"})
        _answer(proc, "m_a_v", "loud")

        assert_that(_wait_until(lambda: manager.variables.snapshot() == {"m_a_v": "loud"})).is_true()
        assert_that(manager.variables.registered_names("com.t.one")).is_equal_to(["m_a_v"])
    finally:
        manager.destroy()


def test_rejected_register_is_ignored(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        proc = _started(manager, procs, _plugin(tmp_path, "com.t.one", [{"pattern": "m_{x}_v"}]))

        _send(proc, "register_variable", {"name": "other_name"})
        time.sleep(0.1)

        assert_that(_asks(proc, "other_name")).is_empty()
        assert_that(manager.variables.registered_names("com.t.one")).is_empty()
    finally:
        manager.destroy()


def test_register_over_the_limit_starts_no_ask(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        proc = _started(manager, procs, _plugin(tmp_path, "com.t.one", [{"pattern": "m_{x}_v"}]))
        for index in range(MAX_REGISTERED):
            assert_that(manager.variables.register("com.t.one", f"m_{index}_v")).is_true()

        _send(proc, "register_variable", {"name": "m_over_v"})
        time.sleep(0.1)

        assert_that(_asks(proc, "m_over_v")).is_empty()
        assert_that(manager.variables.registered_names("com.t.one")).does_not_contain("m_over_v")
    finally:
        manager.destroy()


def test_variable_updated_triggers_ask(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        proc = _started(manager, procs, _plugin(tmp_path, "com.t.one", [{"name": "a_vol"}]))
        _answer(proc, "a_vol", 1)

        _send(proc, "variable_updated", {"name": "a_vol"})
        _answer(proc, "a_vol", 2, index=1)

        assert_that(_wait_until(lambda: manager.variables.snapshot() == {"a_vol": "2"})).is_true()
    finally:
        manager.destroy()


def test_update_from_non_owner_is_ignored(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        one = _started(manager, procs, _plugin(tmp_path, "com.t.one", [{"name": "a_vol"}, {"pattern": "m_{x}_v"}]))
        two = _started(manager, procs, _plugin(tmp_path, "com.t.two", [{"name": "b_x"}]))
        assert_that(_wait_until(lambda: len(_asks(one, "a_vol")) == 1)).is_true()

        _send(two, "variable_updated", {"name": "a_vol"})
        _send(one, "variable_updated", {"name": "m_q_v"})  # its own pattern, but never registered
        time.sleep(0.1)

        assert_that(_asks(one, "a_vol")).is_length(1)
        assert_that(_asks(two, "a_vol")).is_empty()
        assert_that(_asks(one, "m_q_v")).is_empty()
    finally:
        manager.destroy()


def test_change_pushes_variables_changed_to_ready_plugins(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        one = _started(manager, procs, _plugin(tmp_path, "com.t.one", [{"name": "a_vol"}]))
        two = _started(manager, procs, _plugin(tmp_path, "com.t.two", [{"name": "b_x"}]))
        manager.add_plugin(_plugin(tmp_path, "com.t.idle", [{"name": "c_x"}]))  # started, never ready

        _answer(one, "a_vol", 42)

        expected = {"set": {"a_vol": "42"}, "removed": []}
        assert_that(_wait_until(lambda: expected in _pushes(one) and expected in _pushes(two))).is_true()
        assert_that(_pushes(procs["com.t.idle"])).is_empty()
    finally:
        manager.destroy()


def test_unregister_variable_pushes_removal(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        proc = _started(manager, procs, _plugin(tmp_path, "com.t.one", [{"pattern": "m_{x}_v"}]))
        _send(proc, "register_variable", {"name": "m_a_v"})
        _answer(proc, "m_a_v", "loud")
        assert_that(_wait_until(lambda: manager.variables.snapshot() == {"m_a_v": "loud"})).is_true()

        _send(proc, "unregister_variable", {"name": "m_a_v"})

        assert_that(manager.variables.snapshot()).is_empty()
        assert_that(_pushes(proc)[-1]).is_equal_to({"set": {}, "removed": ["m_a_v"]})
    finally:
        manager.destroy()


def test_malformed_variable_payloads_are_ignored(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        proc = _started(manager, procs, _plugin(tmp_path, "com.t.one", [{"name": "a_vol"}]))
        with caplog.at_level(logging.ERROR):
            for event in ("register_variable", "unregister_variable", "variable_updated"):
                for payload in ({}, {"name": 5}, {"name": None}, {"name": ["a_vol"]}):
                    _send(proc, event, payload)
            values: list[dict[str, Any]] = [{}, {"request": 3, "ok": True, "value": "x"}, {"request": "r", "ok": "yes"}]
            for value in values:
                _send(proc, "variable_value", value)

        assert_that([r for r in caplog.records if r.levelno >= logging.ERROR]).is_empty()
        assert_that(_asks(proc, "a_vol")).is_length(1)
    finally:
        manager.destroy()


def test_removing_plugin_releases_its_pending_asks(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        proc = _started(manager, procs, _plugin(tmp_path, "com.t.one", [{"name": "a_vol"}]))
        assert_that(_wait_until(lambda: len(_asks(proc, "a_vol")) == 1)).is_true()

        manager.remove_plugin("com.t.one")

        runner = manager._variable_runner  # pylint: disable=protected-access
        assert_that(_wait_until(lambda: not runner._pending and not runner._inflight, timeout=2.0)).is_true()
    finally:
        manager.destroy()


def test_destroy_releases_pending_asks(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    proc = _started(manager, procs, _plugin(tmp_path, "com.t.one", [{"name": "a_vol"}]))
    assert_that(_wait_until(lambda: len(_asks(proc, "a_vol")) == 1)).is_true()
    runner = manager._variable_runner  # pylint: disable=protected-access

    manager.destroy()

    assert_that(_wait_until(lambda: not runner._pending and not runner._inflight, timeout=1.0)).is_true()


_STATIC_ID = "com.t.static"
_LABEL_CARD = {
    "id": "com.t.one.k",
    "name": "K",
    "settings": [{"key": "label", "type": "text", "label": "L", "default": "", "template": True}],
}


def _static_plugin(tmp_path: Path) -> InstalledPlugin:
    data = {
        "format": 3,
        "id": _STATIC_ID,
        "name": "S",
        "version": "1.0.0",
        "author": "a",
        "contributes": {"cards": [{"id": f"{_STATIC_ID}.k", "name": "K", "static": True}]},
    }
    return InstalledPlugin(PluginManifest.from_dict(data), tmp_path)


def _key(context: str, key_index: int, plugin_id: str = _STATIC_ID, card: str = f"{_STATIC_ID}.k") -> CardTarget:
    return CardTarget(plugin_id, card, context, "dev-1", key_index)


def _with_pages(manager: PluginManager, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Give the manager one device's pages (active page "Settings") and record every composed title text."""
    pages = FakePages()
    manager._page_store = pages  # pylint: disable=protected-access
    manager._page_controller = pages  # pylint: disable=protected-access
    titles: list[str] = []
    real = plugins_module.compose_key_face

    def recording(appearance: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        titles.append(appearance["title"]["text"])
        return real(appearance, *args, **kwargs)

    monkeypatch.setattr(plugins_module, "compose_key_face", recording)
    return titles


def _titled(manager: PluginManager, context: str, key_index: int, text: str) -> None:
    manager.activate_card(_key(context, key_index), {"title": {"text": text}})


def _volume_rig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[PluginManager, dict[str, FakeProcess], list[str], FakeProcess]:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    titles = _with_pages(manager, monkeypatch)
    manager.add_plugin(_static_plugin(tmp_path))
    manager.add_plugin(_plugin(tmp_path, "com.t.one", [{"name": "media_volume"}, {"name": "battery"}], [_LABEL_CARD]))
    proc = procs["com.t.one"]
    proc.emit("ready")
    return manager, procs, titles, proc


def _settings_sent(proc: FakeProcess, context: str) -> list[dict[str, Any]]:
    events = ("will_appear", "settings_changed")
    return [m.payload["settings"] for m in list(proc.sent) if m.event in events and m.context == context]


def test_plugin_with_variables_starts_without_a_card(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    try:
        manager.add_plugin(_plugin(tmp_path, "com.t.one", [{"name": "media_volume"}]))

        assert_that(procs).contains_key("com.t.one")
        assert_that(procs["com.t.one"].running).is_true()
        assert_that(manager.live_targets("com.t.one")).is_empty()
    finally:
        manager.destroy()


def test_plugin_with_variables_starts_with_create(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugins" / "com.t.disk"
    plugin_dir.mkdir(parents=True)
    manifest = {
        "format": 3,
        "id": "com.t.disk",
        "name": "D",
        "version": "1.0.0",
        "author": "a",
        "decker": ">=0.7",
        "runtime": {"type": "python", "entry": "plugin.py"},
        "contributes": {"variables": [{"name": "disk_free"}]},
    }
    (plugin_dir / "manifest.json").write_text(json.dumps(manifest))
    (plugin_dir / "plugin.py").write_text("")
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    context = SimpleNamespace(device_manager=None, event_bus=None, page_controller=None, page_store=None)
    try:
        manager.create(context)  # type: ignore[arg-type]

        assert_that(procs).contains_key("com.t.disk")
        procs["com.t.disk"].emit("ready")
        assert_that(_wait_until(lambda: len(_asks(procs["com.t.disk"], "disk_free")) == 1)).is_true()
    finally:
        manager.destroy()
    assert_that(procs["com.t.disk"].running).is_false()


def test_plugin_without_variables_stays_lazy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    _with_pages(manager, monkeypatch)
    try:
        manager.add_plugin(_plugin(tmp_path, "com.t.cards", [], [{**_LABEL_CARD, "id": "com.t.cards.k"}]))
        assert_that(procs).does_not_contain_key("com.t.cards")

        manager.activate_card(_key("ctx-c", 0, "com.t.cards", "com.t.cards.k"))

        assert_that(procs).contains_key("com.t.cards")
    finally:
        manager.destroy()


def test_value_appears_in_key_title_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _procs, titles, proc = _volume_rig(tmp_path, monkeypatch)
    try:
        _titled(manager, "ctx-a", 0, "Vol {media_volume}")
        _answer(proc, "media_volume", 40)

        assert_that(_wait_until(lambda: titles[-1:] == ["Vol 40"])).is_true()
    finally:
        manager.destroy()


def test_value_change_redraws_only_keys_using_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _procs, titles, proc = _volume_rig(tmp_path, monkeypatch)
    try:
        _titled(manager, "ctx-a", 0, "Vol {media_volume}")
        _titled(manager, "ctx-b", 1, "Bat {battery}")
        _titled(manager, "ctx-c", 2, "On {page_name}")
        for context, key_index, label in (("ctx-d", 3, "L {media_volume}"), ("ctx-e", 4, "L {battery}")):
            manager.store_settings(_key(context, key_index, "com.t.one", "com.t.one.k"), {"label": label})
            manager.activate_card(_key(context, key_index, "com.t.one", "com.t.one.k"))
        _answer(proc, "battery", 7)
        assert_that(_wait_until(lambda: "Bat 7" in titles)).is_true()
        titles.clear()
        before_e = len(_settings_sent(proc, "ctx-e"))

        _answer(proc, "media_volume", 40)

        assert_that(_wait_until(lambda: titles == ["Vol 40"])).is_true()
        assert_that(_wait_until(lambda: _settings_sent(proc, "ctx-d")[-1:] == [{"label": "L 40"}])).is_true()
        time.sleep(0.1)
        assert_that(titles).is_equal_to(["Vol 40"])
        assert_that(_settings_sent(proc, "ctx-e")).is_length(before_e)
    finally:
        manager.destroy()


def test_unset_declared_variable_expands_to_empty_string(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _procs, titles, proc = _volume_rig(tmp_path, monkeypatch)
    try:
        _titled(manager, "ctx-a", 0, "Vol [{media_volume}]")
        assert_that(titles[-1]).is_equal_to("Vol []")

        manager.store_settings(_key("ctx-d", 3, "com.t.one", "com.t.one.k"), {"label": "L[{battery}]"})
        manager.activate_card(_key("ctx-d", 3, "com.t.one", "com.t.one.k"))

        assert_that(_settings_sent(proc, "ctx-d")).is_equal_to([{"label": "L[]"}])
        assert_that(manager.variables.lookup("media_volume", "?")).is_equal_to("")
    finally:
        manager.destroy()


def test_undeclared_name_stays_as_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _procs, titles, _proc = _volume_rig(tmp_path, monkeypatch)
    try:
        _titled(manager, "ctx-a", 0, "X {nobody_owns} {page_name}")

        assert_that(titles[-1]).is_equal_to("X {nobody_owns} Settings")
        assert_that(manager.variables.lookup("nobody_owns", "?")).is_equal_to("?")
    finally:
        manager.destroy()


def test_page_variable_wins_over_plugin_variable_of_same_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _procs, titles, _proc = _volume_rig(tmp_path, monkeypatch)
    try:
        with pytest.raises(ManifestValidationError):
            _plugin(tmp_path, "com.t.thief", [{"name": "page_name"}])
        with pytest.raises(VariableConflict):
            manager.variables.declare("com.t.thief", [VariableSpec(name="page_name", pattern=None)])
        _titled(manager, "ctx-a", 0, "{page_name}")

        assert_that(manager.variables.owner_of("page_name")).is_none()
        assert_that(VariableChain({"x": "page"}, {"x": "global"}).get("x")).is_equal_to("page")
        assert_that(titles[-1]).is_equal_to("Settings")
    finally:
        manager.destroy()


def test_value_with_braces_not_reexpanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _procs, titles, proc = _volume_rig(tmp_path, monkeypatch)
    try:
        _titled(manager, "ctx-a", 0, "Vol {media_volume}")
        _answer(proc, "media_volume", "{page_name} {{x}}")

        assert_that(_wait_until(lambda: titles[-1:] == ["Vol {page_name} {{x}}"])).is_true()
    finally:
        manager.destroy()


def test_process_exit_drops_values_and_redraws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, procs, titles, proc = _volume_rig(tmp_path, monkeypatch)
    try:
        other = _started(manager, procs, _plugin(tmp_path, "com.t.two", [{"name": "b_x"}]))
        _titled(manager, "ctx-a", 0, "Vol {media_volume}")
        _answer(proc, "media_volume", 40)
        assert_that(_wait_until(lambda: titles[-1:] == ["Vol 40"])).is_true()
        _send(proc, "variable_updated", {"name": "media_volume"})
        assert_that(_wait_until(lambda: len(_asks(proc, "media_volume")) == 2)).is_true()

        proc.crash()

        assert_that(manager.variables.snapshot()).is_empty()
        assert_that(titles[-1]).is_equal_to("Vol ")
        assert_that(_pushes(other)[-1]).is_equal_to({"set": {}, "removed": ["media_volume"]})
        runner = manager._variable_runner  # pylint: disable=protected-access
        mine = lambda: [p for p in list(runner._pending.values()) if p.plugin_id == "com.t.one"]  # noqa: E731
        assert_that(_wait_until(lambda: not mine(), timeout=1.0)).is_true()  # ended at once, not by the 5 s timeout
    finally:
        manager.destroy()


def test_restart_restores_values_after_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, procs, titles, proc = _volume_rig(tmp_path, monkeypatch)
    try:
        _titled(manager, "ctx-a", 0, "Vol {media_volume}")
        _answer(proc, "media_volume", 40)
        assert_that(_wait_until(lambda: manager.variables.snapshot().get("media_volume") == "40")).is_true()
        proc.crash()
        assert_that(manager.variables.snapshot()).is_empty()

        manager._supervisor.tick(time.monotonic() + 60.0)  # pylint: disable=protected-access  # past the back-off
        restarted = procs["com.t.one"]
        assert_that(restarted).is_not_same_as(proc)
        restarted.emit("ready")
        _answer(restarted, "media_volume", 41)

        assert_that(_wait_until(lambda: titles[-1:] == ["Vol 41"])).is_true()
    finally:
        manager.destroy()


def test_removing_plugin_drops_values_and_stops_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _procs, titles, proc = _volume_rig(tmp_path, monkeypatch)
    try:
        _titled(manager, "ctx-a", 0, "Vol {media_volume}")
        _answer(proc, "media_volume", 40)
        assert_that(_wait_until(lambda: titles[-1:] == ["Vol 40"])).is_true()

        manager.remove_plugin("com.t.one")

        assert_that(proc.running).is_false()
        assert_that(manager.variables.snapshot()).is_empty()
        assert_that(titles[-1]).is_equal_to("Vol {media_volume}")  # no longer declared: stays as typed
    finally:
        manager.destroy()


def test_late_answer_of_a_stopped_process_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, procs, titles, proc = _volume_rig(tmp_path, monkeypatch)
    store = manager.variables
    real_set_value = store.set_value
    raced: list[bool] = []

    def stop_then_write(*args: Any, **kwargs: Any) -> bool:
        # The ask thread popped its pending and passed the stopped check; the process dies before it writes.
        if not raced:
            raced.append(True)
            proc.running = False
            manager._on_plugin_exit("com.t.one")  # pylint: disable=protected-access  # plugin_stopped + drop_values
        return real_set_value(*args, **kwargs)

    monkeypatch.setattr(store, "set_value", stop_then_write)
    try:
        _titled(manager, "ctx-a", 0, "Vol {media_volume}")
        _answer(proc, "media_volume", 40)

        assert_that(_wait_until(lambda: bool(raced))).is_true()
        time.sleep(0.05)
        assert_that(store.snapshot()).is_empty()
        assert_that(titles).does_not_contain("Vol 40")

        proc.on_exit(1)  # the supervisor sees the crash and restarts it after the back-off
        manager._supervisor.tick(time.monotonic() + 60.0)  # pylint: disable=protected-access
        restarted = procs["com.t.one"]
        assert_that(restarted).is_not_same_as(proc)
        restarted.emit("ready")
        _answer(restarted, "media_volume", 41)
        assert_that(_wait_until(lambda: store.snapshot() == {"media_volume": "41"})).is_true()
        assert_that(_wait_until(lambda: titles[-1:] == ["Vol 41"])).is_true()
    finally:
        manager.destroy()


def test_declared_unset_variable_expands_without_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(tmp_path)
    procs = _fake_processes(manager)
    titles = _with_pages(manager, monkeypatch)
    manager._page_store = manager._page_controller = None  # pylint: disable=protected-access  # no pages at all
    try:
        manager.add_plugin(_static_plugin(tmp_path))
        manager.add_plugin(_plugin(tmp_path, "com.t.one", [{"name": "media_volume"}], [_LABEL_CARD]))
        procs["com.t.one"].emit("ready")

        _titled(manager, "ctx-a", 0, "A{media_volume}B {{x}}")
        assert_that(titles[-1]).is_equal_to("AB {x}")

        manager.store_settings(_key("ctx-d", 3, "com.t.one", "com.t.one.k"), {"label": "L{media_volume} {{"})
        manager.activate_card(_key("ctx-d", 3, "com.t.one", "com.t.one.k"))
        assert_that(_settings_sent(procs["com.t.one"], "ctx-d")).is_equal_to([{"label": "L {"}])
    finally:
        manager.destroy()
