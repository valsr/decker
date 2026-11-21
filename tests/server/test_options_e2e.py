import json
import shutil
import sys
import threading
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
from decker.plugin.settings_schema import apply_schema
from decker.plugin.settings_store import PluginSettingsStore
from decker.server.options_runner import OptionsResult, OptionsRunner
from decker.server.plugins import PluginManager
from decker.server.service import DeckerService
from tests.server.test_plugins import FakePages

# The interpreter link below is POSIX-only and untested on Windows (no machine to verify it).
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: interpreter symlink")

_FIXTURE = Path(__file__).parent / "fixtures" / "options_plugin"
_PLUGIN = "com.decker.opttest"
_CARD = "com.decker.opttest.picker"
_GRACE_S = 0.5
_POLICY = SupervisorPolicy(idle_grace_s=_GRACE_S, backoff_s=(0.3,), max_crashes=5, ping_interval_s=30.0)
_DEADLINE = 15.0


def _wait(predicate: Callable[[], bool], timeout: float = _DEADLINE) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _Env:
    """A real PluginManager around the fixture plugin, whose process starts lazily on the first request."""

    def __init__(self, tmp_path: Path, timeout: float | None = None) -> None:
        plugins_dir = tmp_path / "plugins"
        self.plugin_dir = plugins_dir / _PLUGIN
        shutil.copytree(_FIXTURE, self.plugin_dir, ignore=shutil.ignore_patterns("__pycache__"))
        python = venv_python(self.plugin_dir)  # a plugin with no requirements needs no uv build: any interpreter does
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        self.store = PluginSettingsStore(tmp_path / "plugins.db")
        self.manager = PluginManager(plugins_dir, _POLICY, self.store)
        if timeout is not None:
            # PluginManager.__init__ is at the argument limit: swap the runner (the manager reads it at call time)
            self.manager._options_runner = OptionsRunner(  # pylint: disable=protected-access
                self.manager._supervisor, timeout=timeout  # pylint: disable=protected-access
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
            self.close()
            raise

    def close(self) -> None:
        pid = self.pid()
        self.manager.destroy()
        if pid is not None and psutil.pid_exists(pid):
            psutil.Process(pid).kill()

    def pid(self) -> int | None:
        entry = self.manager._supervisor._entries.get(_PLUGIN)  # pylint: disable=protected-access
        return entry.process.pid if entry is not None and entry.process is not None else None

    def started_file(self) -> Path:
        return self.plugin_dir / "started"

    def options(self, source: str) -> OptionsResult:
        return self.manager.plugin_options(_PLUGIN, source)


@pytest.fixture(name="env")
def env_fixture(tmp_path: Path) -> Iterator[_Env]:
    environment = _Env(tmp_path)
    seen: list[int] = []
    try:
        yield environment
    finally:
        pid = environment.pid()
        if pid is not None:
            seen.append(pid)
        environment.close()
        for old in seen:
            if psutil.pid_exists(old):
                psutil.Process(old).kill()


@pytest.fixture(name="short_env")
def short_env_fixture(tmp_path: Path) -> Iterator[_Env]:
    environment = _Env(tmp_path, timeout=0.5)
    try:
        yield environment
    finally:
        environment.close()


def _values(result: OptionsResult) -> list[str]:
    return [o["value"] for o in result.options]


def test_options_flow_from_plugin_to_rpc_result(env: _Env) -> None:
    result = env.options("devices")

    assert_that(result.ok).is_true()
    assert_that(result.error).is_equal_to("")
    assert_that(_values(result)).is_equal_to(["speakers", "headset", "hdmi", "long"])
    assert_that(list(result.options[:3])).is_equal_to(
        [
            {"label": "Speakers", "value": "speakers"},
            {"label": "Headset", "value": "headset"},
            {"label": "hdmi", "value": "hdmi"},
        ]
    )
    assert_that(result.options[3]["label"]).is_equal_to("L" * 256)

    DeckerService.configure_context(SimpleNamespace(plugin_manager=env.manager))  # type: ignore[arg-type]
    try:
        answer = json.loads(DeckerService().plugin_options(_PLUGIN, "devices"))
    finally:
        DeckerService._context = None  # pylint: disable=protected-access
    assert_that(answer).is_equal_to({"ok": True, "options": [dict(o) for o in result.options], "error": ""})


def test_async_and_global_sources_answer(env: _Env) -> None:
    asynchronous = env.options("actions_list")
    global_source = env.options("globals")

    assert_that(asynchronous.ok).is_true()
    assert_that(list(asynchronous.options)).is_equal_to(
        [{"label": "First", "value": "first"}, {"label": "Second", "value": "second"}]
    )
    assert_that(global_source.ok).is_true()
    assert_that(list(global_source.options)).is_equal_to(
        [{"label": "eu", "value": "eu"}, {"label": "us", "value": "us"}]
    )


def test_lazy_plugin_is_started_for_the_request_and_released_after(env: _Env) -> None:
    assert_that(env.pid()).is_none()
    assert_that(env.started_file().exists()).is_false()

    assert_that(env.options("devices").ok).is_true()

    pid = env.pid()
    assert_that(pid).is_not_none()
    assert pid is not None
    assert_that(env.started_file().read_text()).is_equal_to(str(pid))
    assert_that(
        _wait(lambda: not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE)
    ).is_true()
    assert_that(_wait(lambda: env.pid() is None)).is_true()

    assert_that(env.options("globals").ok).is_true()  # a stopped lazy plugin starts again for the next request


def test_undeclared_source_is_refused_and_starts_nothing(env: _Env) -> None:
    result = env.options("boom")

    assert_that(result).is_equal_to(OptionsResult(False, error="unknown options source"))
    assert_that(env.pid()).is_none()
    assert_that(env.started_file().exists()).is_false()


def test_unknown_plugin_is_refused(env: _Env) -> None:
    result = env.manager.plugin_options("com.decker.nothing", "devices")

    assert_that(result).is_equal_to(OptionsResult(False, error="unknown plugin"))
    assert_that(env.started_file().exists()).is_false()


def test_raising_source_returns_the_error_and_the_plugin_stays_usable(env: _Env) -> None:
    failed = env.options("raises")

    assert_that(failed.ok).is_false()
    assert_that(failed.error).contains("RuntimeError").contains("the source broke")
    assert_that(env.options("devices").ok).is_true()


def test_slow_source_times_out_and_the_plugin_stays_usable(short_env: _Env) -> None:
    started = time.monotonic()
    slow = short_env.options("slow")
    elapsed = time.monotonic() - started

    assert_that(slow).is_equal_to(OptionsResult(False, error="timed out"))
    assert_that(elapsed).is_less_than(5.0)  # the 60 s source was abandoned after about the 0.5 s timeout
    answer = short_env.options("devices")  # the slow call still pins one SDK pool worker; others serve this
    assert_that(answer.ok).is_true()
    assert_that(_values(answer)).is_equal_to(["speakers", "headset", "hdmi", "long"])


def test_stale_exit_notice_does_not_fail_a_request_served_by_a_new_process(short_env: _Env) -> None:
    runner = short_env.manager._options_runner  # pylint: disable=protected-access
    answers: list[OptionsResult] = []
    worker = threading.Thread(target=lambda: answers.append(short_env.options("slow")), daemon=True)
    worker.start()
    try:
        assert_that(_wait(lambda: bool(runner._pending))).is_true()  # pylint: disable=protected-access

        # The idle stop of the previous process reports its exit after a new request already started a new one.
        short_env.manager._on_plugin_exit(_PLUGIN)  # pylint: disable=protected-access

        worker.join(timeout=_DEADLINE)
        assert_that(answers).is_equal_to([OptionsResult(False, error="timed out")])
    finally:
        worker.join(timeout=_DEADLINE)


def test_saved_value_outside_the_list_survives_apply_schema(env: _Env) -> None:
    card = next(c for p in env.manager.plugins() for c in p.manifest.cards if c.id == _CARD)
    env.store.set_settings(_PLUGIN, _CARD, "ctx-1", {"device": "usb_headset"})
    listed = _values(env.options("devices"))
    assert_that(listed).does_not_contain("usb_headset")

    stored = env.store.get_settings(_PLUGIN, _CARD, "ctx-1")
    applied = apply_schema(card.settings, stored)

    assert_that(applied["device"]).is_equal_to("usb_headset")
    assert_that(applied["lazy"]).is_equal_to("default")
