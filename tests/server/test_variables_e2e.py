import logging
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
from decker.server.action_calls import ActionCall
from decker.server.plugins import PluginManager
from tests.server.test_plugins import FakePages

# The interpreter link and the hard kill below are POSIX-only and untested on Windows (no machine to verify them).
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: interpreter symlink and SIGKILL")

_FIXTURE = Path(__file__).parent / "fixtures" / "variable_plugin"
_PLUGIN = "com.decker.vartest"
_BUMP = "com.decker.vartest.bump"
_POLICY = SupervisorPolicy(idle_grace_s=30.0, backoff_s=(0.3,), max_crashes=5, ping_interval_s=30.0)
_DEADLINE = 15.0


def _wait(predicate: Callable[[], bool], timeout: float = _DEADLINE) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _Env:
    """A real PluginManager running the fixture plugin in a real child process."""

    def __init__(self, tmp_path: Path, slow: bool = False, timeout: float | None = None) -> None:
        plugins_dir = tmp_path / "plugins"
        self.plugin_dir = plugins_dir / _PLUGIN
        shutil.copytree(_FIXTURE, self.plugin_dir, ignore=shutil.ignore_patterns("__pycache__"))
        python = venv_python(self.plugin_dir)  # a plugin with no requirements needs no uv build: any interpreter does
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        if slow:
            (self.plugin_dir / "slow").write_text("")
        self.manager = PluginManager(
            plugins_dir, _POLICY, PluginSettingsStore(tmp_path / "plugins.db"), variable_timeout=timeout
        )
        self.pages = FakePages()
        context = SimpleNamespace(
            device_manager=MagicMock(),
            event_bus=MagicMock(),
            page_controller=self.pages,
            page_store=self.pages,
            key_assignment_store=MagicMock(),
        )
        try:
            self.manager.create(context)  # type: ignore[arg-type]
        except BaseException:
            self.manager.destroy()
            raise

    def value(self, name: str) -> str:
        return str(self.manager.variables.lookup(name, "<undeclared>"))

    def expand(self, text: str) -> str:
        return self.manager._expanded_appearance("dev-1", {"title": {"text": text}})["title"]["text"]

    def counter(self) -> int:
        text = self.value("test_counter")
        return int(text) if text else 0

    def pid(self) -> int | None:
        entry = self.manager._supervisor._entries[_PLUGIN]  # pylint: disable=protected-access
        return entry.process.pid if entry.process is not None else None

    def wait_value(self, name: str) -> bool:
        return _wait(lambda: self.value(name) not in ("", "<undeclared>"))


@pytest.fixture(name="env")
def env_fixture(tmp_path: Path) -> Iterator[_Env]:
    environment = _Env(tmp_path)
    pids: list[int] = []
    try:
        yield environment
        pid = environment.pid()
        if pid is not None:
            pids.append(pid)
    finally:
        environment.manager.destroy()
        for pid in pids:
            if psutil.pid_exists(pid):
                psutil.Process(pid).kill()


def test_values_flow_from_plugin_to_expansion(env: _Env) -> None:
    assert_that(_wait(lambda: env.counter() > 0)).is_true()
    assert_that(env.wait_value("test_alpha_value")).is_true()

    assert_that(env.value("test_alpha_value")).is_equal_to("value-of-alpha")
    assert_that(env.expand("Alpha {test_alpha_value}")).is_equal_to("Alpha value-of-alpha")
    assert_that(_wait(lambda: env.expand("Count {test_counter}") not in ("Count ", "Count 0"))).is_true()


def test_signal_from_action_updates_expansion(env: _Env) -> None:
    assert_that(_wait(lambda: env.counter() > 0)).is_true()

    env.manager.run_action(ActionCall(_BUMP, {}, "plugin"))

    assert_that(_wait(lambda: env.counter() >= 1000)).is_true()
    assert_that(int(env.expand("{test_counter}"))).is_greater_than_or_equal_to(1000)


def test_killing_the_process_clears_then_restores_values(env: _Env) -> None:
    assert_that(_wait(lambda: env.counter() > 0 and env.value("test_alpha_value") != "")).is_true()
    env.manager.run_action(ActionCall(_BUMP, {}, "plugin"))
    assert_that(_wait(lambda: env.counter() >= 1000)).is_true()
    first = env.pid()
    assert first is not None

    psutil.Process(first).kill()

    assert_that(
        _wait(
            lambda: env.value("test_counter") == ""
            and env.value("test_alpha_value") == ""
            and env.expand("[{test_counter}]") == "[]"
        )
    ).is_true()
    assert_that(_wait(lambda: env.pid() not in (None, first))).is_true()
    assert_that(_wait(lambda: env.counter() > 0 and env.value("test_alpha_value") == "value-of-alpha")).is_true()
    assert_that(env.counter()).is_less_than(1000)  # a fresh process: the bump did not survive


def test_slow_getter_does_not_block_other_variables(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    env = _Env(tmp_path, slow=True, timeout=0.5)
    pid: int | None = None
    try:
        assert_that(_wait(lambda: env.pid() is not None)).is_true()
        pid = env.pid()
        assert_that(_wait(lambda: env.counter() > 0 and env.value("test_alpha_value") != "")).is_true()

        def timed_out() -> list[logging.LogRecord]:
            return [r for r in caplog.records if "test_slow" in r.getMessage() and "timed out" in r.getMessage()]

        assert_that(_wait(lambda: len(timed_out()) > 0)).is_true()
        seen = env.counter()  # the getter (60 s) is still asleep: ticks must keep arriving within seconds
        assert_that(_wait(lambda: env.counter() > seen + 3, timeout=3.0)).is_true()

        assert_that(env.value("test_slow")).is_equal_to("")
        assert_that(timed_out()).is_length(1)
    finally:
        env.manager.destroy()
        if pid is not None and psutil.pid_exists(pid):
            psutil.Process(pid).kill()


def _ticks(env: _Env) -> int:
    ticks = env.plugin_dir / "ticks"
    return ticks.stat().st_size if ticks.exists() else 0


def test_shutdown_stops_main(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    pid: int | None = None
    try:
        assert_that(_wait(lambda: _ticks(env) > 3)).is_true()
        pid = env.pid()
        entry = env.manager._supervisor._entries[_PLUGIN]  # pylint: disable=protected-access
        popen = entry.process._proc  # type: ignore[union-attr]  # pylint: disable=protected-access
        assert popen is not None

        env.manager.destroy()

        assert_that(_wait(lambda: popen.poll() is not None)).is_true()
        assert_that(popen.returncode).is_equal_to(0)  # it exited on its own after "shutdown": not terminated or killed
        final = _ticks(env)
        time.sleep(0.3)  # three main intervals: a still-running main would have ticked
        assert_that(_ticks(env)).is_equal_to(final)
    finally:
        env.manager.destroy()
        if pid is not None and psutil.pid_exists(pid):
            psutil.Process(pid).kill()
