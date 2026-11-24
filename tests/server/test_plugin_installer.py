import hashlib
import io
import json
import stat
import sys
import threading
import time
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from assertpy import assert_that

from decker.common import config
from decker.plugin.environment import EnvironmentBuildError
from decker.plugin.repository_index import IndexVersion
from decker.plugin.settings_store import PluginSettingsStore
from decker.plugin.target import CardTarget
from decker.server import plugin_installer as installer_module
from decker.server.key_assignments import KeyAssignmentStore
from decker.server.page_controller import PageController
from decker.server.pages import PageStore, initial_page_id
from decker.server.plugin_installer import PluginInstaller, PluginInstallError
from decker.server.plugins import PluginManager
from decker.server.server_context import ServerContext
from decker.server.service import DeckerService
from tests.portable import cmd_wrapper, entry_name
from tests.server.http_fixture import FixtureServer

_SCRIPT = f"""#!{sys.executable}
import json, sys
for line in sys.stdin:
    with open({{log!r}}, "a") as log:
        log.write(line)
    event = json.loads(line)["event"]
    if event == "hello":
        print(json.dumps({{{{"event": "ready"}}}}), flush=True)
    elif event == "shutdown":
        break
"""


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.clients: list = []

    def register(self, conn, *, loopback: bool) -> None:
        self.clients.append(conn)

    def unregister(self, conn) -> None:
        if conn in self.clients:
            self.clients.remove(conn)

    def push(self, event_type: str, payload: str) -> None:
        self.events.append((event_type, json.loads(payload)))
        for conn in list(self.clients):
            try:
                conn.root.on_server_event(event_type, payload)
            except ConnectionError:
                self.clients.remove(conn)

    def of(self, event_type: str) -> list[dict]:
        return [p for t, p in list(self.events) if t == event_type]


class FakeDeviceManager:
    def add_open_listener(self, _fn):
        pass

    add_close_listener = remove_open_listener = remove_close_listener = add_open_listener

    def get_device_state(self, _device_id):
        return None

    def open_device(self, _device_id):
        raise RuntimeError("no hardware")

    def list_devices(self):
        return []

    def set_image_bytes(self, *_args) -> None:
        pass

    def clear_key_state(self, *_args) -> None:
        pass

    def delete_stored_key_state(self, *_args) -> None:
        pass


def _wait_for(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


DEVICE = "dev-1"
PAGE_1 = initial_page_id(DEVICE)
ACTION = "com.example.tool.show"


def _manifest(version="1.0.0", plugin_id="com.example.tool", **extra) -> dict:
    data = {
        "format": 3,
        "id": plugin_id,
        "name": "Tool",
        "version": version,
        "author": "Me",
        "runtime": {"type": "executable", "entry": entry_name("tool.py")},
        "global_settings": [{"key": "token", "type": "text", "label": "Token", "default": "none"}],
        "contributes": {"cards": [{"id": f"{plugin_id}.show", "name": "Show"}]},
    }
    data.update(extra)
    return data


def _package(log, manifest=None, script=True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest or _manifest()))
        info = zipfile.ZipInfo("tool.py")
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        zf.writestr(info, _SCRIPT.format(log=str(log)) if script else "")
        if script and sys.platform == "win32":
            zf.writestr("tool.cmd", cmd_wrapper("tool.py"))
    return buf.getvalue()


def _lines(log, event=None) -> list[dict]:
    if not log.exists():
        return []
    messages = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return [m for m in messages if event is None or m["event"] == event]


class Env:
    def __init__(self, tmp_path, decker_version="1.0.0") -> None:
        self.tmp = tmp_path
        self.plugins_dir = tmp_path / "plugins"
        self.bus = FakeBus()
        self.dm = FakeDeviceManager()
        self.dm.clear_key_state = MagicMock()  # type: ignore[method-assign]
        self.dm.delete_stored_key_state = MagicMock()  # type: ignore[method-assign]
        db = tmp_path / "decker.db"
        self.key_store = KeyAssignmentStore(db)
        self.page_store = PageStore(db)
        self.page_controller = PageController()
        self.state_store = MagicMock()
        self.manager = PluginManager(self.plugins_dir, settings_store=PluginSettingsStore(tmp_path / "plugins.db"))
        self.context = ServerContext(
            device_manager=self.dm,  # type: ignore[arg-type]
            plugin_manager=self.manager,
            key_assignment_store=self.key_store,
            device_state_store=self.state_store,
            page_store=self.page_store,
            page_controller=self.page_controller,
            event_bus=self.bus,  # type: ignore[arg-type]
        )
        for part in (self.page_store, self.key_store, self.page_controller, self.manager):
            part.create(self.context)
        self.installer = PluginInstaller(self.plugins_dir, self.context, decker_version)
        self.context.plugin_installer = self.installer

    def install(self, data: bytes) -> dict:
        """Install and wait for the finished event."""
        before = len(self.bus.of("plugin_install_finished"))
        job = self.installer.install("tool.deckerplugin", data)
        assert _wait_for(lambda: len(self.bus.of("plugin_install_finished")) > before)
        finished = self.bus.of("plugin_install_finished")[-1]
        assert_that(finished["job_id"]).is_equal_to(job)
        return finished

    def assign(self, key=0, page=PAGE_1, context="ctx-1", plugin="com.example.tool", settings=None) -> CardTarget:
        target = CardTarget(plugin, f"{plugin}.show", context, DEVICE, key)
        self.key_store.assign(DEVICE, page, key, target, settings or {})
        self.manager.store_settings(target, settings or {})
        if page == PAGE_1:
            self.manager.activate_card(target, {})
        return target

    def close(self) -> None:
        self.manager.destroy()
        self.key_store.destroy()
        self.page_store.destroy()


@pytest.fixture(name="env")
def env_fixture(tmp_path):
    environment = Env(tmp_path)
    yield environment
    environment.close()


def test_install_new_plugin_registers_and_announces(env, tmp_path):
    log = tmp_path / "v1.log"

    finished = env.install(_package(log))

    assert_that(finished).contains_entry({"ok": True}, {"plugin_id": "com.example.tool"}, {"version": "1.0.0"})
    assert_that(finished["previous_version"]).is_none()
    assert_that([p.manifest.id for p in env.manager.plugins()]).contains("com.example.tool")
    assert_that((env.plugins_dir / "com.example.tool" / "manifest.json").exists()).is_true()
    assert_that(env.bus.of("plugins_changed")).is_length(1)
    assert_that(env.bus.of("plugin_install_progress")).is_not_empty()
    assert_that([p.name for p in env.plugins_dir.iterdir()]).is_equal_to(["com.example.tool"])


def test_install_refuses_incompatible_decker_version(env, tmp_path):
    finished = env.install(_package(tmp_path / "x.log", _manifest(decker=">=9.0")))

    assert_that(finished["ok"]).is_false()
    assert_that(finished["message"]).contains("requires Decker >=9.0")
    assert_that(env.bus.of("plugins_changed")).is_empty()


def test_install_refuses_invalid_decker_specifier(env, tmp_path):
    finished = env.install(_package(tmp_path / "x.log", _manifest(decker="not a spec")))

    assert_that(finished["ok"]).is_false()
    assert_that(finished["message"]).contains("invalid Decker version requirement")


def test_install_refuses_invalid_package(env):
    finished = env.install(b"not a zip")

    assert_that(finished["ok"]).is_false()


def test_failed_install_leaves_nothing_behind(env, tmp_path, monkeypatch):
    def _fail(*_args, **_kwargs):
        raise EnvironmentBuildError("uv exploded")

    monkeypatch.setattr(installer_module, "build_environment", _fail)
    manifest = _manifest(runtime={"type": "python", "entry": "tool.py"})

    finished = env.install(_package(tmp_path / "x.log", manifest))

    assert_that(finished["ok"]).is_false()
    assert_that(finished["message"]).contains("uv exploded")
    assert_that(list(env.plugins_dir.iterdir())).is_empty()
    assert_that(env.manager.plugins()).extracting("manifest").extracting("id").does_not_contain("com.example.tool")


def test_reinstall_replaces_and_reactivates_live_keys(env, tmp_path):
    log1, log2 = tmp_path / "v1.log", tmp_path / "v2.log"
    env.install(_package(log1))
    target = env.assign()
    assert _wait_for(lambda: _lines(log1, "will_appear"))

    finished = env.install(_package(log2, _manifest("2.0.0")))

    assert_that(finished).contains_entry({"ok": True}, {"version": "2.0.0"}, {"previous_version": "1.0.0"})
    assert_that(_lines(log1, "will_disappear")).is_length(1)
    assert _wait_for(lambda: _lines(log2, "will_appear"))
    appear = _lines(log2, "will_appear")[0]
    assert_that(appear["context"]).is_equal_to(target.context_id)
    assert_that(env.manager.live_targets("com.example.tool")).is_length(1)
    assert_that([p.name for p in env.plugins_dir.iterdir()]).is_equal_to(["com.example.tool"])
    installed = {p.manifest.id: p for p in env.manager.plugins()}["com.example.tool"]
    assert_that(installed.manifest.version).is_equal_to("2.0.0")


def test_reinstall_keeps_the_key_settings(env, tmp_path):
    log1, log2 = tmp_path / "v1.log", tmp_path / "v2.log"
    manifest = _manifest()
    manifest["contributes"]["cards"][0]["settings"] = [{"key": "n", "type": "int", "label": "N", "default": 1}]
    env.install(_package(log1, manifest))
    env.assign(settings={"n": 7})
    assert _wait_for(lambda: _lines(log1, "will_appear"))
    manifest2 = _manifest("2.0.0")
    manifest2["contributes"]["cards"][0]["settings"] = manifest["contributes"]["cards"][0]["settings"]

    env.install(_package(log2, manifest2))

    assert _wait_for(lambda: _lines(log2, "will_appear"))
    assert_that(_lines(log2, "will_appear")[0]["payload"]["settings"]).is_equal_to({"n": 7})


def test_failed_upgrade_restores_previous_version(env, tmp_path, monkeypatch):
    log1, log2 = tmp_path / "v1.log", tmp_path / "v2.log"
    env.install(_package(log1))
    env.assign()
    assert _wait_for(lambda: _lines(log1, "will_appear"))
    original_add = env.manager.add_plugin

    def _add(plugin):
        if plugin.manifest.version == "2.0.0":
            raise OSError("disk full")
        original_add(plugin)

    monkeypatch.setattr(env.manager, "add_plugin", _add)

    finished = env.install(_package(log2, _manifest("2.0.0")))

    assert_that(finished["ok"]).is_false()
    installed = {p.manifest.id: p for p in env.manager.plugins()}["com.example.tool"]
    assert_that(installed.manifest.version).is_equal_to("1.0.0")
    manifest = json.loads((env.plugins_dir / "com.example.tool" / "manifest.json").read_text())
    assert_that(manifest["version"]).is_equal_to("1.0.0")
    assert_that([p.name for p in env.plugins_dir.iterdir()]).is_equal_to(["com.example.tool"])
    assert_that(env.manager.live_targets("com.example.tool")).is_length(1)
    assert _wait_for(lambda: len(_lines(log1, "will_appear")) == 2)


def test_upgrade_reactivates_only_keys_on_the_page_active_after_the_swap(env, tmp_path, monkeypatch):
    log1, log2 = tmp_path / "v1.log", tmp_path / "v2.log"
    env.install(_package(log1))
    other_page = env.page_store.add_page(DEVICE, "Other").page_id
    env.assign(key=0, context="shown")
    env.assign(key=1, page=other_page, context="hidden")
    assert _wait_for(lambda: _lines(log1, "will_appear"))
    original_add = env.manager.add_plugin

    def _add(plugin):
        env.page_controller._active[DEVICE] = other_page  # pylint: disable=protected-access
        original_add(plugin)

    monkeypatch.setattr(env.manager, "add_plugin", _add)

    finished = env.install(_package(log2, _manifest("2.0.0")))

    assert_that(finished["ok"]).is_true()
    assert_that([t.context_id for t in env.manager.live_targets("com.example.tool")]).is_equal_to(["hidden"])
    assert _wait_for(lambda: _lines(log2, "will_appear"))
    assert_that([m["context"] for m in _lines(log2, "will_appear")]).is_equal_to(["hidden"])


@pytest.mark.parametrize("entry", ["missing.py", "../escape.py"])
def test_upgrade_with_unlaunchable_runtime_keeps_previous_version(env, tmp_path, entry):
    log1, log2 = tmp_path / "v1.log", tmp_path / "v2.log"
    env.install(_package(log1))
    env.assign()
    assert _wait_for(lambda: _lines(log1, "will_appear"))
    manifest = _manifest("2.0.0", runtime={"type": "executable", "entry": entry})

    finished = env.install(_package(log2, manifest))

    assert_that(finished["ok"]).is_false()
    installed = {p.manifest.id: p for p in env.manager.plugins()}["com.example.tool"]
    assert_that(installed.manifest.version).is_equal_to("1.0.0")
    assert_that(env.manager.live_targets("com.example.tool")).is_length(1)
    assert_that(_lines(log1, "will_disappear")).is_empty()
    assert_that([p.name for p in env.plugins_dir.iterdir()]).is_equal_to(["com.example.tool"])


def test_uninstall_clears_keys_on_all_pages_and_removes_folder(env, tmp_path):
    env.install(_package(tmp_path / "v1.log"))
    other_page = env.page_store.add_page(DEVICE, "Other").page_id
    env.assign(key=0, context="a")
    env.assign(key=1, page=other_page, context="b")
    env.manager.store_settings(CardTarget("com.example.tool", ACTION, "a", DEVICE, 0), {"n": 1})
    env.manager.update_global_settings("com.example.tool", {"token": "abc"})
    env.bus.events.clear()

    env.installer.uninstall("com.example.tool")

    assert_that(env.key_store.assignments_for_plugin("com.example.tool")).is_empty()
    assert_that((env.plugins_dir / "com.example.tool").exists()).is_false()
    assert_that(env.manager.plugins()).extracting("manifest").extracting("id").does_not_contain("com.example.tool")
    env.dm.clear_key_state.assert_called_once_with(DEVICE, 0)
    env.dm.delete_stored_key_state.assert_called_once_with(DEVICE, other_page, 1)
    assert_that(env.bus.of("plugins_changed")).is_length(1)
    assert_that(env.bus.of("pages_changed")).is_equal_to([{"device_id": DEVICE}])
    store = env.manager._settings_store  # pylint: disable=protected-access
    assert_that(store.get_global_settings("com.example.tool")).is_empty()
    assert_that(store.get_settings("com.example.tool", ACTION, "a")).is_empty()


def test_uninstall_unknown_refused(env):
    with pytest.raises(PluginInstallError, match="not installed"):
        env.installer.uninstall("com.example.missing")


def test_usage_counts_keys_across_pages(env, tmp_path):
    env.install(_package(tmp_path / "v1.log"))
    other_page = env.page_store.add_page(DEVICE, "Other").page_id
    env.assign(key=0, context="a")
    env.assign(key=1, page=other_page, context="b")
    env.assign(key=2, page=other_page, context="c")

    assert_that(env.installer.usage("com.example.tool")).is_equal_to(3)
    assert_that(env.installer.usage("com.example.none")).is_equal_to(0)


def test_loader_ignores_dot_folders(env, tmp_path):
    env.install(_package(tmp_path / "v1.log"))
    (env.plugins_dir / ".staging-x").mkdir()
    (env.plugins_dir / ".staging-x" / "manifest.json").write_text("{}")

    scan = env.manager._user_loader.scan()  # pylint: disable=protected-access

    assert_that(scan.failures).is_empty()
    assert_that([p.manifest.id for p in scan.plugins]).is_equal_to(["com.example.tool"])


def test_client_disconnect_mid_install_still_completes(env, tmp_path):
    gone = SimpleNamespace(root=SimpleNamespace(on_server_event=MagicMock(side_effect=ConnectionError)))
    DeckerService.configure_context(env.context)
    service = DeckerService()
    service._peer_host = "127.0.0.1"  # pylint: disable=protected-access
    env.bus.register(gone, loopback=True)
    job = service.plugin_install("tool.deckerplugin", _package(tmp_path / "v1.log"))
    service.on_disconnect(gone)

    assert _wait_for(lambda: any(p["job_id"] == job for p in env.bus.of("plugin_install_finished")))
    assert_that(env.bus.of("plugin_install_finished")[-1]["ok"]).is_true()
    assert_that((env.plugins_dir / "com.example.tool").exists()).is_true()
    DeckerService._context = None  # pylint: disable=protected-access


class TestServiceRules:
    @pytest.fixture(autouse=True)
    def _clean(self):
        yield
        DeckerService._context = None  # pylint: disable=protected-access
        config.unset("server.allow_remote_plugin_install")

    def _service(self, env, host) -> DeckerService:
        DeckerService.configure_context(env.context)
        service = DeckerService()
        sock = SimpleNamespace(getpeername=lambda: (host, 55555))
        conn = SimpleNamespace(_channel=SimpleNamespace(stream=SimpleNamespace(sock=sock)))
        env.context.event_bus = MagicMock()
        service.on_connect(conn)
        return service

    def test_remote_peer_cannot_install_by_default(self, env):
        service = self._service(env, "192.168.1.20")

        with pytest.raises(PermissionError, match="only allowed from the server's own machine"):
            service.plugin_install("x.deckerplugin", b"")
        with pytest.raises(PermissionError):
            service.plugin_uninstall("com.example.tool")

    def test_install_status_rpc_returns_json_and_is_gated(self, env, tmp_path):
        remote = self._service(env, "192.168.1.20")
        with pytest.raises(PermissionError):
            remote.plugin_install_status("x")
        service = self._service(env, "127.0.0.1")

        assert_that(json.loads(service.plugin_install_status("x"))["state"]).is_equal_to("unknown")

    def test_remote_install_allowed_by_config(self, env, tmp_path):
        config.set("server.allow_remote_plugin_install", True)
        service = self._service(env, "192.168.1.20")

        assert_that(service.plugin_install("x.deckerplugin", _package(tmp_path / "x.log"))).is_length(32)

    @pytest.mark.parametrize(
        ("host", "allowed"), [("127.0.0.1", True), ("::1", True), ("::ffff:127.0.0.1", True), ("10.0.0.5", False)]
    )
    def test_install_allowed_reports_decision(self, env, host, allowed):
        assert_that(self._service(env, host).plugin_install_allowed()).is_equal_to(allowed)

    def test_unknown_peer_is_not_loopback(self, env):
        DeckerService.configure_context(env.context)
        service = DeckerService()
        service.on_connect(MagicMock())

        assert_that(service.plugin_install_allowed()).is_false()

    def test_plugins_list_describes_plugins(self, env, tmp_path):
        env.install(_package(tmp_path / "v1.log"))
        service = self._service(env, "127.0.0.1")

        listed = {p["id"]: p for p in json.loads(service.plugins_list())}

        tool = listed["com.example.tool"]
        assert_that(tool).contains_entry({"name": "Tool"}, {"version": "1.0.0"}, {"cards": 1})
        assert_that(tool).contains_entry({"icon_packs": 0}, {"icon_b64": ""}, {"global_settings": {"token": "none"}})
        assert_that(tool["global_settings_schema"][0]["key"]).is_equal_to("token")

    def test_global_settings_validated_persisted_and_sent(self, env, tmp_path):
        log = tmp_path / "v1.log"
        env.install(_package(log))
        env.assign()
        assert _wait_for(lambda: _lines(log, "hello"))
        service = self._service(env, "127.0.0.1")

        assert_that(
            service.plugin_update_global_settings("com.example.tool", json.dumps({"token": "abc", "x": 1}))
        ).is_true()

        assert _wait_for(lambda: _lines(log, "global_settings_changed"))
        assert_that(_lines(log, "global_settings_changed")[0]["payload"]).is_equal_to(
            {"global_settings": {"token": "abc"}}
        )
        assert_that(env.manager.global_settings("com.example.tool")).is_equal_to({"token": "abc"})
        listed = {p["id"]: p for p in json.loads(service.plugins_list())}
        assert_that(listed["com.example.tool"]["global_settings"]).is_equal_to({"token": "abc"})
        assert_that(service.plugin_update_global_settings("com.example.missing", "{}")).is_false()
        assert_that(service.plugin_update_global_settings("com.example.tool", "[]")).is_false()

    def test_hello_carries_stored_global_settings(self, env, tmp_path):
        env.manager._settings_store.set_global_settings(
            "com.example.tool", {"token": "stored"}
        )  # pylint: disable=protected-access
        log = tmp_path / "v1.log"
        env.install(_package(log))
        env.assign()

        assert _wait_for(lambda: _lines(log, "hello"))
        assert_that(_lines(log, "hello")[0]["payload"]["global_settings"]).is_equal_to({"token": "stored"})

    def test_usage_rpc(self, env, tmp_path):
        env.install(_package(tmp_path / "v1.log"))
        env.assign()

        assert_that(self._service(env, "127.0.0.1").plugin_usage("com.example.tool")).is_equal_to(1)


def test_status_reports_the_job_outcome(env, tmp_path):
    job = env.installer.install("tool.deckerplugin", _package(tmp_path / "v1.log"))
    assert _wait_for(lambda: env.installer.status(job)["state"] != "running")

    status = env.installer.status(job)

    assert_that(status).contains_entry({"state": "succeeded"}, {"plugin_id": "com.example.tool"}, {"version": "1.0.0"})
    failed = env.install(b"not a zip")
    assert_that(env.installer.status(failed["job_id"])["state"]).is_equal_to("failed")
    assert_that(env.installer.status("nope")["state"]).is_equal_to("unknown")


def test_second_install_while_one_runs_is_rejected_and_uninstall_reports_busy(env, tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def _slow(*_args, **_kwargs):
        started.set()
        release.wait(10)

    monkeypatch.setattr(installer_module, "build_environment", _slow)
    monkeypatch.setattr(installer_module, "_UNINSTALL_WAIT_S", 0.05)
    manifest = _manifest(runtime={"type": "python", "entry": "tool.py"})
    job = env.installer.install("tool.deckerplugin", _package(tmp_path / "x.log", manifest))
    assert started.wait(10)

    with pytest.raises(PluginInstallError, match="busy"):
        env.installer.install("again.deckerplugin", b"x")
    with pytest.raises(PluginInstallError, match="busy"):
        env.installer.uninstall("com.example.tool")
    assert_that(env.installer.status(job)["state"]).is_equal_to("running")
    release.set()
    assert _wait_for(lambda: env.installer.status(job)["state"] != "running")
    env.installer.install("later.deckerplugin", b"x")


def test_uninstall_removes_a_folder_whose_manifest_failed_to_load(env):
    broken = env.plugins_dir / "broken-plugin"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("{not json")

    env.installer.uninstall("broken-plugin")

    assert_that(broken.exists()).is_false()
    with pytest.raises(PluginInstallError, match="not installed"):
        env.installer.uninstall("../escape")


# ----------------------------------------------------------------------
# Install from a repository
# ----------------------------------------------------------------------


@pytest.fixture(name="repo")
def repo_fixture(tmp_path):
    root = tmp_path / "served"
    root.mkdir()
    server = FixtureServer(root).start()
    yield root, server
    server.stop()


def _index_version(root, server, data: bytes, version="1.0.0", **overrides) -> IndexVersion:
    (root / "tool.deckerplugin").write_bytes(data)
    fields = {
        "version": version,
        "decker": None,
        "url": f"{server.base_url}/tool.deckerplugin",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "released": "2026-01-01",
        "contributes": {},
    }
    fields.update(overrides)
    return IndexVersion(**fields)


def _install_from_repo(env, version: IndexVersion) -> dict:
    before = len(env.bus.of("plugin_install_finished"))
    job = env.installer.install_from_repository("com.example.tool", version)
    assert _wait_for(lambda: len(env.bus.of("plugin_install_finished")) > before)
    finished = env.bus.of("plugin_install_finished")[-1]
    assert_that(finished["job_id"]).is_equal_to(job)
    return finished


def test_repository_install_succeeds(env, repo, tmp_path):
    root, server = repo

    finished = _install_from_repo(env, _index_version(root, server, _package(tmp_path / "v.log")))

    assert_that(finished).contains_entry({"ok": True}, {"plugin_id": "com.example.tool"}, {"version": "1.0.0"})
    assert_that([p.manifest.id for p in env.manager.plugins()]).contains("com.example.tool")


def test_download_progress_events(env, repo, tmp_path):
    root, server = repo

    _install_from_repo(env, _index_version(root, server, _package(tmp_path / "v.log")))

    messages = [p["message"] for p in env.bus.of("plugin_install_progress")]
    assert_that([m for m in messages if m.startswith("Downloading com.example.tool 1.0.0 (")]).is_not_empty()


def test_sha_mismatch_fails_and_keeps_installed_version(env, repo, tmp_path):
    root, server = repo
    env.install(_package(tmp_path / "v1.log"))
    data = _package(tmp_path / "v2.log", _manifest("2.0.0"))

    finished = _install_from_repo(env, _index_version(root, server, data, "2.0.0", sha256="0" * 64))

    assert_that(finished["ok"]).is_false()
    assert_that(finished["message"]).contains("checksum")
    installed = {p.manifest.id: p for p in env.manager.plugins()}["com.example.tool"]
    assert_that(installed.manifest.version).is_equal_to("1.0.0")


def test_truncated_download_fails(env, repo, tmp_path):
    root, server = repo
    data = _package(tmp_path / "v.log")

    finished = _install_from_repo(env, _index_version(root, server, data, size=len(data) + 100))

    assert_that(finished["ok"]).is_false()
    assert_that(finished["message"]).contains("bytes")
    assert_that(env.manager.plugins()).extracting("manifest").extracting("id").does_not_contain("com.example.tool")


def test_download_total_deadline_fails(env, repo, tmp_path, monkeypatch):
    root, server = repo
    monkeypatch.setattr(installer_module, "_DOWNLOAD_DEADLINE_S", -1.0)

    finished = _install_from_repo(env, _index_version(root, server, _package(tmp_path / "v.log")))

    assert_that(finished["ok"]).is_false()
    assert_that(finished["message"]).contains("took longer")


def test_manifest_mismatch_fails(env, repo, tmp_path):
    root, server = repo
    data = _package(tmp_path / "v.log", _manifest("1.0.0"))

    finished = _install_from_repo(env, _index_version(root, server, data, "2.0.0"))

    assert_that(finished["ok"]).is_false()
    assert_that(finished["message"]).is_equal_to("package is com.example.tool 1.0.0, index says com.example.tool 2.0.0")


def test_busy_while_job_running(env, repo, tmp_path):
    root, server = repo
    server.delay = 0.5
    version = _index_version(root, server, _package(tmp_path / "v.log"))
    before = len(env.bus.of("plugin_install_finished"))
    env.installer.install_from_repository("com.example.tool", version)

    with pytest.raises(PluginInstallError, match="busy"):
        env.installer.install_from_repository("com.example.tool", version)
    with pytest.raises(PluginInstallError, match="busy"):
        env.installer.install("tool.deckerplugin", b"")

    assert _wait_for(lambda: len(env.bus.of("plugin_install_finished")) > before)
    assert_that(env.bus.of("plugin_install_finished")[-1]["ok"]).is_true()


class TestUpdateReportRefresh:
    @pytest.fixture(autouse=True)
    def _checker(self, env, tmp_path):
        from decker.common import (  # pylint: disable=import-outside-toplevel
            config,
            paths,
        )
        from decker.server.repositories import (
            RepositoryStore,  # pylint: disable=import-outside-toplevel
        )
        from decker.server.update_checker import (
            UpdateChecker,  # pylint: disable=import-outside-toplevel
        )
        from tests.server.test_update_checker import (  # pylint: disable=import-outside-toplevel
            _version,
            _write_index,
        )

        config.configure_prefix(tmp_path / "config" / "config.yaml")
        url = _write_index(tmp_path / "index.json", {"com.example.tool": [_version("2.0.0")]})
        config.set("plugin.repositories", [url])
        store = RepositoryStore(tmp_path / "cache")
        store.refresh_all()
        env.checker = UpdateChecker(
            store, env.manager, env.key_store, lambda name, payload: env.bus.push(name, json.dumps(payload))
        )
        env.context.update_checker = env.checker
        yield
        config.configure_prefix(paths.config_yaml())

    def test_install_of_newer_version_clears_the_update(self, env, tmp_path):
        env.install(_package(tmp_path / "v1.log"))
        assert_that(env.bus.of("plugin_updates_available")[-1]["updates"]).extracting("available").is_equal_to(
            ["2.0.0"]
        )

        env.install(_package(tmp_path / "v2.log", _manifest("2.0.0")))

        assert_that(env.bus.of("plugin_updates_available")[-1]["updates"]).is_empty()
        assert_that(env.checker.report().is_empty()).is_true()

    def test_uninstall_refreshes_the_report(self, env, tmp_path):
        env.install(_package(tmp_path / "v1.log"))
        assert_that(env.checker.report().updates).is_length(1)

        env.installer.uninstall("com.example.tool")

        assert_that(env.bus.of("plugin_updates_available")[-1]["updates"]).is_empty()

    def test_refresh_failure_does_not_break_install(self, env, tmp_path, monkeypatch):
        monkeypatch.setattr(env.checker, "refresh_local", MagicMock(side_effect=RuntimeError("boom")))

        finished = env.install(_package(tmp_path / "v1.log"))

        assert_that(finished["ok"]).is_true()
