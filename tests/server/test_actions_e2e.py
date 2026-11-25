import http.server
import io
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
from PIL import Image

from decker.plugin.environment import venv_python
from decker.plugin.loader import InstalledPlugin
from decker.plugin.manifest import PluginManifest
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
_CHAIN = "com.decker.acttest.chain"
_RECURSE = "com.decker.acttest.recurse"
_RELAY = "com.decker.acttest.relay"
_PAINT = "com.decker.acttest.paint"
_PROBE = "com.decker.acttest.probe"
_CORE = "com.decker.core"
_STEP = "com.decker.acttest.step"
_NEXT = "com.decker.acttest.next"
_SEQUENCE = "com.decker.core.sequence"
_DELAY = "com.decker.core.delay"
_HTTP = "com.decker.core.httprequest"
_POLICY = SupervisorPolicy(idle_grace_s=30.0, backoff_s=(0.3,), max_crashes=5, ping_interval_s=30.0)
_DEADLINE = 15.0
_TILE = CardTarget(_PLUGIN, "com.decker.acttest.tile", "ctx-tile", "dev-1", 0)
_BLANK = CardTarget("com.decker.core", "com.decker.core.blank", "ctx-blank", "dev-1", 1)


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
        self._expose_pillow(python.parent.parent)
        self.manager = PluginManager(plugins_dir, _POLICY, PluginSettingsStore(tmp_path / "plugins.db"))
        self.bus = FakeBus()
        self.pages = FakePages()
        self.devices = FakeDeviceManager()
        context = SimpleNamespace(
            device_manager=self.devices,
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

    @staticmethod
    def _expose_pillow(venv: Path) -> None:
        # Lends the plugin only Pillow (the PIL package, its bundled libs and dist-info) from the test venv, by symlink.
        (venv / "pyvenv.cfg").write_text(f"home = {Path(sys.executable).resolve().parent}\n", encoding="utf-8")
        site = venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
        site.mkdir(parents=True)
        source = Path(Image.__file__).resolve().parent.parent
        for entry in (source / "PIL", source / "pillow.libs", *source.glob("pillow-*.dist-info")):
            if entry.exists():
                (site / entry.name).symlink_to(entry)

    def close(self) -> None:
        pids = [pid for pid in (self.pid(_PLUGIN), self.pid(_CORE)) if pid is not None]
        try:
            self.manager.destroy()
        finally:
            for pid in pids:
                if psutil.pid_exists(pid):
                    psutil.Process(pid).kill()

    def pid(self, plugin_id: str = _PLUGIN) -> int | None:
        entry = self.manager._supervisor._entries.get(plugin_id)  # pylint: disable=protected-access
        return entry.process.pid if entry is not None and entry.process is not None else None

    def has_process(self, plugin_id: str) -> bool:
        return self.manager._supervisor.has_process(plugin_id)  # pylint: disable=protected-access

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

    def image_count(self, key: int) -> int:
        return len([1 for index, _ in self.devices.hardware_images if index == key])

    def key_image(self, key: int) -> Image.Image | None:
        images = [data for index, data in self.devices.hardware_images if index == key]
        return Image.open(io.BytesIO(images[-1])).convert("RGB") if images else None

    def key_pixel(self, key: int, x: int, y: int) -> tuple[int, ...] | None:
        image = self.key_image(key)
        pixel = image.getpixel((x, y)) if image is not None else None
        return pixel if isinstance(pixel, tuple) else None

    def centre(self, key: int) -> int:
        assert_that(_wait(lambda: self.key_image(key) is not None)).described_as(
            f"no image was written for key {key}"
        ).is_true()
        image = self.key_image(key)
        assert image is not None
        return image.size[0] // 2

    def last_paint(self, target: CardTarget) -> object:
        return self.manager._live[target.context_id].last_paint  # pylint: disable=protected-access

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


def test_action_calls_own_plugin_action_and_gets_value(env: _Env) -> None:
    env.activate_tile()
    env.manager.set_bindings(_TILE, {"press": {"action": _CHAIN, "args": {}}})

    env.press(_TILE)

    assert_that(_wait(lambda: env.read("chain.log") != "")).described_as("chain.log was never written").is_true()
    assert_that(env.read("chain.log")).is_equal_to("42")


def test_a_nested_call_is_capped_by_the_limit_of_the_action_that_made_it(env: _Env) -> None:
    env.activate_tile()
    env.manager.set_bindings(_TILE, {"press": {"action": "com.decker.acttest.capped", "args": {}}})

    env.press(_TILE)

    assert_that(_wait(lambda: env.read("capped.log") != "", 15)).described_as("capped.log was never written").is_true()
    elapsed, outcome = env.read("capped.log").split("|", 1)
    assert_that(float(elapsed)).is_less_than(3.5)
    assert_that(outcome).is_equal_to("timed out")


def test_recursion_stops_at_depth_8_with_an_error(env: _Env) -> None:
    env.activate_tile()
    env.manager.set_bindings(_TILE, {"press": {"action": _RECURSE, "args": {"n": 0}}})

    env.press(_TILE)

    assert_that(_wait(lambda: env.read("recurse.err") != "")).described_as("recursion never refused").is_true()
    assert_that(env.read("recurse.log")).is_equal_to("8")
    assert_that(env.read("recurse.err")).contains("depth limit (8)")


def test_call_inside_a_gesture_chain_resolves_page_args_for_the_card_device(env: _Env) -> None:
    env.manager.activate_card(_BLANK)
    env.manager.set_bindings(_BLANK, {"press": {"action": _RELAY, "args": {}}})

    env.press(_BLANK)

    assert_that(_wait(lambda: env.read("public.log") != "")).described_as("public.log was never written").is_true()
    received = json.loads(env.read("public.log"))
    assert_that(received["page"]).contains_entry({"page_id": "p1"}, {"name": "Home"})


class _Hook(http.server.BaseHTTPRequestHandler):
    seen: list[tuple[str, str]] = []

    def do_GET(self) -> None:  # pylint: disable=invalid-name
        _Hook.seen.append((self.command, self.path))
        self.send_response(404 if self.path == "/missing" else 200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: object) -> None:  # pylint: disable=redefined-builtin
        pass


@pytest.fixture(name="hook")
def hook_fixture() -> Iterator[str]:
    _Hook.seen = []
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Hook)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(_DEADLINE)


def test_blank_bound_to_http_request_hits_the_server_through_the_core_process(env: _Env, hook: str) -> None:
    env.manager.activate_card(_BLANK)
    env.manager.set_bindings(_BLANK, {"press": {"action": _HTTP, "args": {"method": "GET", "url": f"{hook}/ping"}}})

    env.press(_BLANK)

    assert_that(_wait(lambda: _Hook.seen == [("GET", "/ping")])).described_as("the server was never hit").is_true()
    assert_that(env.has_process(_CORE)).is_true()
    assert_that(env.bus.of("action_failed")).is_empty()


def test_a_failing_http_request_announces_its_status(env: _Env, hook: str) -> None:
    env.manager.activate_card(_BLANK)
    env.manager.set_bindings(_BLANK, {"press": {"action": _HTTP, "args": {"method": "GET", "url": f"{hook}/missing"}}})

    env.press(_BLANK)

    assert_that(_wait(lambda: bool(env.bus.of("action_failed")))).is_true()
    assert_that(env.bus.of("action_failed")[0]["error"]).is_equal_to("HTTP 404")


def test_core_is_started_lazily(env: _Env, hook: str) -> None:
    env.manager.activate_card(_BLANK)
    env.manager.set_bindings(_BLANK, {"press": {"action": _HTTP, "args": {"method": "GET", "url": f"{hook}/ping"}}})
    assert_that(env.has_process(_CORE)).is_false()

    env.press(_BLANK)

    assert_that(_wait(lambda: env.has_process(_CORE))).is_true()


_DOT_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
    b'<path fill="currentColor" d="M0 0h10v10H0z"/></svg>'
)
_DOT_REF = "pack:com.t.ip.icons/dot.svg"


def _is_red(pixel: tuple[int, ...] | None) -> bool:
    return pixel is not None and pixel[0] > 200 and pixel[1] < 60 and pixel[2] < 60


def _is_blue(pixel: tuple[int, ...] | None) -> bool:
    return pixel is not None and pixel[2] > 200 and pixel[0] < 60 and pixel[1] < 60


def _is_green(pixel: tuple[int, ...] | None) -> bool:
    return pixel is not None and pixel[1] > 100 and pixel[0] < 60 and pixel[2] < 60


def _install_dot_pack(env: _Env, tmp_path: Path) -> None:
    root = tmp_path / "ip"
    (root / "pack").mkdir(parents=True)
    (root / "pack" / "dot.svg").write_bytes(_DOT_SVG)
    manifest = PluginManifest.from_dict(
        {
            "format": 3,
            "id": "com.t.ip",
            "name": "IP",
            "version": "1.0.0",
            "author": "a",
            "description": "d",
            "contributes": {"icon_packs": [{"id": "com.t.ip.icons", "name": "Pack", "path": "pack"}]},
        }
    )
    env.manager.icon_packs.register(InstalledPlugin(manifest, root))


def test_queue_paint_lands_on_the_key(env: _Env) -> None:
    env.activate_tile()

    painted = _wait(lambda: _is_red(env.key_pixel(0, 36, 36)))

    assert_that(painted).described_as("no red frame reached the key within the deadline").is_true()
    assert_that(env.last_paint(_TILE)).is_not_none()


def test_global_action_paints_a_foreign_blank_card_before_its_result(env: _Env) -> None:
    env.manager.activate_card(_BLANK, {"background": {"type": "color", "color": "#00ff00"}})
    env.manager.set_bindings(_BLANK, {"press": {"action": _PAINT, "args": {}}})
    centre = env.centre(1)

    env.press(_BLANK)

    assert_that(_wait(lambda: _is_blue(env.key_pixel(1, centre, centre)))).described_as(
        "centre never turned blue"
    ).is_true()
    assert_that(_is_green(env.key_pixel(1, 2, 2))).described_as(
        "the user's background should show at the edge"
    ).is_true()
    assert_that(env.bus.of("action_failed")).is_empty()


def test_user_icon_override_still_wins_over_canvas_frames(env: _Env, tmp_path: Path) -> None:
    _install_dot_pack(env, tmp_path)
    env.manager.activate_card(_TILE, {"icon": {"ref": _DOT_REF, "color": "#00ff00"}})
    drawn = env.image_count(0)
    assert_that(_wait(env.ready)).is_true()

    assert_that(_wait(lambda: env.last_paint(_TILE) is not None)).described_as("no frame was accepted").is_true()
    assert_that(_wait(lambda: env.image_count(0) > drawn)).described_as("the frame never recomposed the key").is_true()

    centre = env.centre(0)
    assert_that(_is_green(env.key_pixel(0, centre, centre))).described_as("override lost to the canvas").is_true()


def test_plugin_process_sees_pillow_but_not_the_test_venv(env: _Env) -> None:
    env.activate_tile()
    env.manager.set_bindings(_TILE, {"press": {"action": _PROBE, "args": {}}})

    env.press(_TILE)

    assert_that(_wait(lambda: env.read("probe.log") != "")).described_as("probe.log was never written").is_true()
    found = json.loads(env.read("probe.log"))
    assert_that(found).is_equal_to({"PIL": True, "psutil": False, "yaml": False, "rpyc": False, "decker": False})


def test_plugin_set_icon_with_a_built_in_short_reference_lands_on_the_key(env: _Env) -> None:
    target = CardTarget(_PLUGIN, "com.decker.acttest.iconcard", "ctx-icon", "dev-1", 2)
    env.manager.activate_card(target)

    centre = env.centre(2)

    assert_that(_wait(lambda: _is_green(env.key_pixel(2, centre, centre)))).described_as(
        "the built-in play icon never appeared"
    ).is_true()


def _steps(*steps: tuple[str, dict[str, object]], **extra: object) -> dict[str, object]:
    args: dict[str, object] = {}
    for number, (action_id, params) in enumerate(steps, start=1):
        if action_id:
            args[f"step_{number}"] = action_id
            args[f"step_{number}_params"] = params
    return {**args, **extra}


def _run_sequence(env: _Env, args: dict[str, object]) -> None:
    env.manager.activate_card(_BLANK)
    env.manager.set_bindings(_BLANK, {"press": {"action": _SEQUENCE, "args": args}})
    env.press(_BLANK)


def test_sequence_runs_steps_in_order_and_skips_empty(env: _Env) -> None:
    _run_sequence(env, _steps((_STEP, {"n": 1}), ("", {}), (_STEP, {"n": 3})))

    assert_that(_wait(lambda: env.read("steps.log") == "13")).described_as(env.read("steps.log")).is_true()
    assert_that(env.bus.of("action_failed")).is_empty()


def test_sequence_stops_on_error_naming_the_step(env: _Env) -> None:
    _run_sequence(env, _steps((_STEP, {"n": 1}), (_STEP, {"n": 2, "fail": True}), (_STEP, {"n": 3})))

    assert_that(_wait(lambda: bool(env.bus.of("action_failed")))).is_true()
    assert_that(env.bus.of("action_failed")[0]["error"]).starts_with("step 2 (").contains("boom")
    assert_that(env.read("steps.log")).is_equal_to("12")


def test_sequence_continues_when_stop_on_error_is_off(env: _Env) -> None:
    args = _steps((_STEP, {"n": 1, "fail": True}), (_STEP, {"n": 2}), stop_on_error=False)
    _run_sequence(env, args)

    assert_that(_wait(lambda: env.read("steps.log") == "12")).described_as(env.read("steps.log")).is_true()
    assert_that(env.bus.of("action_failed")).is_empty()


def test_sequence_with_invalid_step_params_fails_that_step(env: _Env) -> None:
    _run_sequence(env, _steps((_STEP, {"n": 1}), (_STEP, {"n": "abc"}), (_STEP, {"n": 3})))

    assert_that(_wait(lambda: bool(env.bus.of("action_failed")))).is_true()
    assert_that(env.bus.of("action_failed")[0]["error"]).starts_with("step 2 (").contains("invalid arguments")
    assert_that(env.read("steps.log")).is_equal_to("1")


def test_sequence_step_switches_page_on_the_gesture_device(env: _Env) -> None:
    _run_sequence(env, _steps((_NEXT, {})))

    assert_that(_wait(lambda: env.pages.activations == [("dev-1", "p3")])).described_as("no page switch").is_true()


def test_a_sequence_longer_than_the_default_timeout_is_not_cut_off(env: _Env) -> None:
    env.manager._action_runner._timeout = 0.5  # pylint: disable=protected-access
    args = _steps((_DELAY, {"seconds": 0.4}), (_DELAY, {"seconds": 0.4}), (_STEP, {"n": 7}))
    _run_sequence(env, args)

    assert_that(_wait(lambda: env.read("steps.log") == "7")).described_as("sequence was cut off").is_true()
    assert_that(env.bus.of("action_failed")).is_empty()


def test_nested_sequence_respects_depth_limit(env: _Env) -> None:
    args: dict[str, object] = _steps((_STEP, {"n": 9}))
    for _ in range(10):
        args = _steps((_SEQUENCE, args))
    _run_sequence(env, args)

    assert_that(_wait(lambda: bool(env.bus.of("action_failed")))).is_true()
    assert_that(env.bus.of("action_failed")[0]["error"]).contains("depth limit (8)")
    assert_that(env.read("steps.log")).is_equal_to("")
