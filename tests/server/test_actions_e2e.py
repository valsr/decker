import json
import shutil
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import psutil
import pytest
from assertpy import assert_that

from decker.plugin.environment import venv_python
from decker.plugin.runtime.supervisor import SupervisorPolicy
from decker.plugin.settings_store import PluginSettingsStore
from decker.plugin.target import CardTarget
from decker.server.plugins import PluginManager
from tests.server.test_plugins import FakeBus, FakeDeviceManager, FakePages

# The interpreter link below is POSIX-only and untested on Windows (no machine to verify it).
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: interpreter symlink")

_FIXTURE = Path(__file__).parent / "fixtures" / "actions_plugin"
_PLUGIN = "com.decker.acttest"
_MARK = "com.decker.acttest.mark"
_PUBLIC = "com.decker.acttest.public"
_NAV = "com.decker.acttest.nav"
_POLICY = SupervisorPolicy(idle_grace_s=30.0, backoff_s=(0.3,), max_crashes=5, ping_interval_s=30.0)
_DEADLINE = 15.0
_TILE = CardTarget(_PLUGIN, "com.decker.acttest.tile", "ctx-tile", "dev-1", 0)
_BLANK = CardTarget("com.decker.blank", "com.decker.blank.blank", "ctx-blank", "dev-1", 1)


def _wait(predicate: Callable[[], bool], timeout: float = _DEADLINE) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _Env:

    def __init__(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        self.plugin_dir = plugins_dir / _PLUGIN
        shutil.copytree(_FIXTURE, self.plugin_dir, ignore=shutil.ignore_patterns("__pycache__"))
        python = venv_python(self.plugin_dir)  # a plugin with no requirements needs no uv build: any interpreter does
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        self.manager = PluginManager(plugins_dir, _POLICY, PluginSettingsStore(tmp_path / "plugins.db"))
        self.bus = FakeBus()
        self.pages = FakePages()
        context = SimpleNamespace(
            device_manager=FakeDeviceManager(),
            event_bus=self.bus,
            page_controller=self.pages,
            page_store=self.pages,
            key_assignment_store=MagicMock(),
        )
        try:
            self.manager.create(context)  # type: ignore[arg-type]
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        pid = self.pid()
        try:
            self.manager.destroy()
        finally:
            if pid is not None and psutil.pid_exists(pid):
                psutil.Process(pid).kill()

    def pid(self) -> int | None:
        entry = self.manager._supervisor._entries.get(_PLUGIN)  # pylint: disable=protected-access
        return entry.process.pid if entry is not None and entry.process is not None else None

    def ready(self) -> bool:
        return self.manager._supervisor.is_ready(_PLUGIN)  # pylint: disable=protected-access

    def activate_tile(self) -> None:
        self.manager.activate_card(_TILE)
        assert_that(_wait(self.ready)).is_true()

    def press(self, target: CardTarget, hold: float = 0.0) -> None:
        self.manager.dispatch_key_down(target)
        if hold:
            time.sleep(hold)
        self.manager.dispatch_key_up(target)

    def read(self, name: str) -> str:
        path = self.plugin_dir / name
        return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.fixture(name="env")
def env_fixture(tmp_path: Path) -> Iterator[_Env]:
    environment = _Env(tmp_path)
    try:
        yield environment
    finally:
        environment.close()


def test_press_runs_bound_plugin_action_in_the_process(env: _Env) -> None:
    env.activate_tile()

    env.press(_TILE)

    assert_that(_wait(lambda: env.read("marks.log") == "p")).is_true()


def test_unbound_double_press_does_not_delay_press(env: _Env) -> None:
    env.activate_tile()

    started = time.monotonic()
    env.press(_TILE)
    arrived = _wait(lambda: env.read("marks.log") == "p")
    elapsed = time.monotonic() - started

    assert_that(arrived).is_true()
    assert_that(elapsed).is_less_than(0.35)


def test_long_press_runs_its_binding(env: _Env) -> None:
    env.activate_tile()

    env.press(_TILE, hold=0.7)

    assert_that(_wait(lambda: env.read("marks.log") == "l")).is_true()


def test_blank_bound_to_global_action_resolves_page_args_for_the_device(env: _Env) -> None:
    env.manager.activate_card(_BLANK)
    env.manager.set_bindings(_BLANK, {"press": {"action": _PUBLIC, "args": {"page": "p1"}}})

    env.press(_BLANK)

    assert_that(_wait(lambda: env.read("public.log") != "")).is_true()
    received = json.loads(env.read("public.log"))
    assert_that(received["page"]).contains_entry({"page_id": "p1"}, {"name": "Home"})


def test_invalid_stored_args_never_reach_the_plugin_and_announce_action_failed(env: _Env) -> None:
    env.activate_tile()
    env.manager.set_bindings(_TILE, {"press": {"action": _MARK, "args": {"tag": "NOPE1"}}})

    env.press(_TILE)

    assert_that(_wait(lambda: bool(env.bus.of("action_failed")))).is_true()
    failed = env.bus.of("action_failed")[0]
    assert_that(failed["action_id"]).is_equal_to(_MARK)
    assert_that(failed["error"]).contains("invalid arguments").contains("tag")
    assert_that(env.read("marks.log")).is_equal_to("")


def test_global_action_navigates_the_blank_card_device(env: _Env) -> None:
    env.manager.activate_card(_BLANK)
    env.manager.set_bindings(_BLANK, {"press": {"action": _NAV, "args": {"page": "p1"}}})

    env.press(_BLANK)

    assert_that(_wait(lambda: env.pages.activations == [("dev-1", "p1")])).is_true()
