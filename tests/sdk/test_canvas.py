import base64
import io
import json
import os
import subprocess
import sys
import threading
from typing import Any

import pytest
from assertpy import assert_that
from PIL import Image

from decker_plugin_sdk import (
    ActionCallContext,
    CardContext,
    CardProxy,
    DeckerPlugin,
    run,
)
from decker_plugin_sdk._canvas import (
    MAX_FRAMES_PER_TICK,
    Canvas,
    FrameTarget,
    FrameTicker,
)
from decker_plugin_sdk._pool import ElasticPool
from decker_plugin_sdk._runtime import _Runtime

RED = (255, 0, 0, 255)
BLUE = (0, 0, 255, 255)
WAIT = 10.0
NO_PILLOW = "the canvas needs Pillow: add pillow to your plugin's requirements"


class Sink:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str | None, dict[str, Any]]] = []
        self.lock: threading.Lock = threading.Lock()
        self.arrived: threading.Event = threading.Event()
        self.fail_first: bool = False
        self.ticker: FrameTicker | None = None
        self.reply: bool = False
        self.image: str | None = None

    def __call__(self, event: str, context: str | None, payload: dict[str, Any]) -> None:
        if event == "get_image" and self.reply and self.ticker is not None:
            self.ticker.deliver({"request": payload["request"], "image": self.image, "format": "png"})
        if self.fail_first:
            self.fail_first = False
            raise OSError("pipe broke")
        with self.lock:
            self.messages.append((event, context, payload))
        self.arrived.set()

    def images(self) -> list[tuple[str | None, dict[str, Any]]]:
        with self.lock:
            return [(c, p) for e, c, p in self.messages if e == "set_image"]


def _pixel(payload: dict[str, Any], at: tuple[int, int] = (0, 0)) -> Any:
    return Image.open(io.BytesIO(base64.b64decode(payload["image"]))).convert("RGBA").getpixel(at)


def _png(color: tuple[int, int, int, int], size: tuple[int, int] = (8, 8)) -> str:
    buffer = io.BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


@pytest.fixture(name="sink")
def sink_fixture() -> Sink:
    return Sink()


@pytest.fixture(name="ticker")
def ticker_fixture(sink: Sink) -> Any:
    ticker = FrameTicker(sink, interval=3600)  # tests drive ticks by hand
    sink.ticker = ticker
    yield ticker
    ticker.stop()


def _canvas(ticker: FrameTicker, context: str = "c1", request: str | None = None) -> Canvas:
    return Canvas(ticker, FrameTarget(context, request), (8, 8), lambda: None)


def _draw(canvas: Canvas, color: tuple[int, int, int, int] = RED, at: tuple[int, int] = (0, 0)) -> None:
    with canvas.paint() as img:
        img.putpixel(at, color)


def test_snapshot_taken_only_when_last_painter_releases(ticker: FrameTicker, sink: Sink) -> None:
    canvas = _canvas(ticker)
    first, second = canvas.paint(), canvas.paint()
    img = first.__enter__()
    second.__enter__()
    img.putpixel((0, 0), RED)
    first.__exit__(None, None, None)
    ticker.tick()
    assert_that(sink.images()).is_empty()
    img.putpixel((1, 0), BLUE)
    second.__exit__(None, None, None)
    ticker.tick()
    assert_that(sink.images()).is_length(1)
    payload = sink.images()[0][1]
    assert_that(_pixel(payload, (0, 0))).is_equal_to(RED)
    assert_that(_pixel(payload, (1, 0))).is_equal_to(BLUE)
    assert_that(payload["format"]).is_equal_to("png")


def test_half_drawn_frame_is_never_sent(ticker: FrameTicker, sink: Sink) -> None:
    canvas = _canvas(ticker)
    drawing, release = threading.Event(), threading.Event()

    def painter() -> None:
        with canvas.paint() as img:
            img.putpixel((0, 0), RED)
            drawing.set()
            release.wait(WAIT)
            img.putpixel((1, 0), BLUE)

    thread = threading.Thread(target=painter)
    thread.start()
    assert_that(drawing.wait(WAIT)).is_true()
    for _ in range(3):
        ticker.tick()
    assert_that(sink.images()).is_empty()
    release.set()
    thread.join(WAIT)
    ticker.tick()
    assert_that(sink.images()).is_length(1)
    assert_that(_pixel(sink.images()[0][1], (1, 0))).is_equal_to(BLUE)


def test_ticker_sends_each_dirty_key_once(ticker: FrameTicker, sink: Sink) -> None:
    first, second = _canvas(ticker, "a"), _canvas(ticker, "b")
    _draw(first, RED)
    _draw(first, BLUE)
    _draw(second, RED)
    ticker.tick()
    ticker.tick()
    sent = sink.images()
    assert_that([context for context, _ in sent]).is_equal_to(["a", "b"])
    assert_that(_pixel(sent[0][1])).is_equal_to(BLUE)


def test_no_dirty_keys_sends_nothing(ticker: FrameTicker, sink: Sink) -> None:
    ticker.tick()
    assert_that(sink.messages).is_empty()


def test_running_ticker_sends_without_manual_ticks(sink: Sink) -> None:
    ticker = FrameTicker(sink, interval=0.01)
    try:
        _draw(_canvas(ticker), RED)
        assert_that(sink.arrived.wait(WAIT)).is_true()
        assert_that(sink.images()).is_length(1)
    finally:
        ticker.stop()


def test_ticker_thread_is_daemon_and_stops(sink: Sink) -> None:
    ticker = FrameTicker(sink, interval=0.01)
    _draw(_canvas(ticker), RED)
    thread = next(t for t in threading.enumerate() if t.name == "decker-frame-ticker")
    assert_that(thread.daemon).is_true()
    ticker.stop()
    assert_that(thread.is_alive()).is_false()


def test_ticker_survives_a_failing_send(sink: Sink) -> None:
    ticker = FrameTicker(sink, interval=0.01)
    try:
        sink.fail_first = True
        _draw(_canvas(ticker, "a"), RED)
        for _ in range(500):  # the failed frame is dropped; wait until the thread has consumed it
            if not sink.fail_first:
                break
            threading.Event().wait(0.01)
        assert_that(sink.fail_first).is_false()
        _draw(_canvas(ticker, "b"), BLUE)
        assert_that(sink.arrived.wait(WAIT)).is_true()
        assert_that([context for context, _ in sink.images()]).is_equal_to(["b"])
    finally:
        ticker.stop()


def test_own_buffer_starts_from_last_sent_frame(ticker: FrameTicker, sink: Sink) -> None:
    def context() -> CardContext:
        return CardContext("c1", "card", "dev", 0, 0, 0, size=(8, 8), _send=sink, _frames=ticker)

    first = context()
    assert_that(first.last_paint).is_none()
    with first.canvas.paint() as img:
        img.putpixel((2, 2), RED)
    ticker.tick()
    assert_that(first.last_paint).is_not_none()
    with context().canvas.paint() as img:
        assert_that(img.getpixel((2, 2))).is_equal_to(RED)
        assert_that(img.getpixel((0, 0))).is_equal_to((0, 0, 0, 0))
    with CardContext("other", "card", "dev", 0, 0, 0, size=(8, 8), _frames=ticker).canvas.paint() as img:
        assert_that(img.getpixel((2, 2))).is_equal_to((0, 0, 0, 0))


def _foreign_payload() -> dict[str, Any]:
    return {"context": "ctx1", "plugin": "other", "key": {"index": 0}, "size": [8, 8], "settings": {}}


def _foreign_proxy(ticker: FrameTicker, sink: Sink, image: str | None) -> CardProxy:
    sink.reply, sink.image = True, image
    return CardProxy(_foreign_payload(), "req1", sink, ticker)


def test_foreign_canvas_fetches_current_image_first(ticker: FrameTicker, sink: Sink) -> None:
    proxy = _foreign_proxy(ticker, sink, _png(RED))
    with proxy.canvas.paint() as img:
        assert_that(img.getpixel((3, 3))).is_equal_to(RED)
        img.putpixel((0, 0), BLUE)
    request = sink.messages[0]
    assert_that(request[0]).is_equal_to("get_image")
    assert_that(request[2]).contains_entry({"context": "ctx1"}, {"call": "req1"})
    ticker.flush("req1")
    (context, payload), *rest = sink.images()
    assert_that(rest).is_empty()
    assert_that((context, payload["request"])).is_equal_to(("ctx1", "req1"))
    assert_that(_pixel(payload, (0, 0))).is_equal_to(BLUE)


def test_foreign_canvas_is_transparent_when_there_is_no_image(ticker: FrameTicker, sink: Sink) -> None:
    proxy = _foreign_proxy(ticker, sink, None)
    with proxy.canvas.paint() as img:
        assert_that(img.getpixel((3, 3))).is_equal_to((0, 0, 0, 0))
        assert_that(img.size).is_equal_to((8, 8))


def test_foreign_canvas_is_transparent_when_the_image_never_arrives(
    ticker: FrameTicker, sink: Sink, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("decker_plugin_sdk._canvas.IMAGE_TIMEOUT", 0.05)
    proxy = CardProxy(_foreign_payload(), "req1", sink, ticker)
    with proxy.canvas.paint() as img:
        assert_that(img.getpixel((3, 3))).is_equal_to((0, 0, 0, 0))


def test_closed_proxy_refuses_queue_paint_and_canvas(ticker: FrameTicker, sink: Sink) -> None:
    proxy = _foreign_proxy(ticker, sink, None)
    proxy.close()
    with pytest.raises(RuntimeError, match="card access ended with the action call"):
        proxy.queue_paint()
    with pytest.raises(RuntimeError, match="card access ended with the action call"):
        _ = proxy.canvas


def test_foreign_queue_paint_resends_the_current_buffer(ticker: FrameTicker, sink: Sink) -> None:
    proxy = _foreign_proxy(ticker, sink, _png(RED))
    proxy.queue_paint()  # nothing painted or fetched yet
    ticker.flush("req1")
    assert_that(sink.images()).is_empty()
    _draw(proxy.canvas, BLUE)
    ticker.flush("req1")
    proxy.queue_paint()
    ticker.flush("req1")
    assert_that(sink.images()).is_length(2)


def _events(proxy: CardProxy, ticker: FrameTicker) -> None:
    proxy.show_ok()
    proxy.show_alert()
    proxy.switch_page("next", True)
    proxy.go_to_page("p1")
    proxy.switch_profile("previous")
    proxy.go_to_profile(2)
    _draw(proxy.canvas)
    ticker.flush("req1")


def test_every_card_event_carries_context_and_request(ticker: FrameTicker, sink: Sink) -> None:
    proxy = _foreign_proxy(ticker, sink, None)
    _events(proxy, ticker)
    sent = [m for m in sink.messages if m[0] != "get_image"]
    assert_that([e for e, _, _ in sent]).is_equal_to(
        ["show_ok", "show_alert", "switch_page", "switch_page", "switch_profile", "switch_profile", "set_image"]
    )
    for _, context, payload in sent:
        assert_that(context).is_equal_to("ctx1")
        assert_that(payload["request"]).is_equal_to("req1")


def test_canvas_without_pillow_raises_clear_runtime_error(ticker: FrameTicker, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(RuntimeError) as caught:
        with _canvas(ticker).paint():
            pass
    assert_that(str(caught.value)).is_equal_to(NO_PILLOW)


def test_sdk_import_does_not_import_pil() -> None:
    code = "import decker_plugin_sdk, sys; assert 'PIL' not in sys.modules"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert_that(result.returncode).described_as(result.stderr).is_equal_to(0)


class Painter(DeckerPlugin):
    paints: list[str] = []

    def on_will_appear(self, ctx: CardContext) -> None:
        ctx.set_title("up")

    def on_key_down(self, ctx: CardContext) -> None:
        for _ in range(3):
            ctx.queue_paint()
        ctx.set_title("queued")

    def on_key_up(self, ctx: CardContext) -> None:
        ctx.set_title("done")

    def on_paint(self, ctx: CardContext, img: Image.Image) -> None:
        Painter.paints.append(ctx.context)
        img.putpixel((0, 0), RED)


def _paint_foreign(ctx: ActionCallContext) -> None:
    assert ctx.card is not None
    _draw(ctx.card.canvas, BLUE)


def _paint_foreign_then_fail(ctx: ActionCallContext) -> None:
    _paint_foreign(ctx)
    raise ValueError("after painting")


class _Host:
    def __init__(self) -> None:
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        self.to_plugin = os.fdopen(in_w, "wb")
        self._in = os.fdopen(in_r, "rb")
        self._out = os.fdopen(out_w, "wb")
        self.from_plugin = os.fdopen(out_r, "rb")
        actions = {"a.paint": _paint_foreign, "a.paint_fail": _paint_foreign_then_fail}
        self.thread = threading.Thread(target=lambda: run({"test.canvas.c": Painter}, self._in, self._out, actions))
        self.thread.start()
        self._watchdog = threading.Timer(WAIT * 2, self._abort)
        self._watchdog.start()
        self.send("hello", {"protocol": 4, "decker_version": "x", "plugin_id": "test.canvas", "global_settings": {}})
        self.recv_until("ready")

    def _abort(self) -> None:
        self.to_plugin.close()
        self._out.close()

    def send(self, event: str, payload: dict[str, Any], context: str | None = None) -> None:
        data: dict[str, Any] = {"event": event, "payload": payload}
        if context:
            data["context"] = context
        self.to_plugin.write(json.dumps(data).encode() + b"\n")
        self.to_plugin.flush()

    def recv(self) -> dict[str, Any]:
        line = self.from_plugin.readline()
        assert line, "plugin closed stdout (watchdog or crash)"
        return json.loads(line)

    def recv_until(self, event: str) -> dict[str, Any]:
        while True:
            message = self.recv()
            if message["event"] == event:
                return message

    def close(self) -> None:
        self._watchdog.cancel()
        self.to_plugin.close()
        self.thread.join(WAIT)
        for stream in (self._in, self._out, self.from_plugin):
            stream.close()


@pytest.fixture(name="host")
def host_fixture() -> Any:
    host = _Host()
    yield host
    host.close()


def test_queue_paint_coalesces_into_one_on_paint(host: _Host) -> None:
    Painter.paints.clear()
    host.send(
        "will_appear",
        {"card": "test.canvas.c", "device": "d", "key": {"index": 0}, "size": [8, 8], "settings": {}},
        "c1",
    )
    host.recv_until("set_title")
    host.send("key_down", {}, "c1")
    host.recv_until("set_title")  # "queued": the paint item is already on the context's queue
    host.send("key_up", {}, "c1")  # runs after the paint item
    assert_that(host.recv_until("set_title")["payload"]["title"]).is_equal_to("done")
    assert_that(Painter.paints).is_equal_to(["c1"])
    image = host.recv_until("set_image")
    assert_that(_pixel(image["payload"])).is_equal_to(RED)


def _call(action: str, request: str) -> dict[str, Any]:
    card = {"context": "other-ctx", "plugin": "other.plugin", "key": {"index": 0}, "size": [8, 8], "settings": {}}
    return {
        "action": action,
        "request": request,
        "input": "plugin",
        "called_at": "2026-11-21T10:00:00+00:00",
        "card": card,
    }


def _round_trip(host: _Host, action: str, request: str) -> list[dict[str, Any]]:
    host.send("run_action", _call(action, request))
    get_image = host.recv_until("get_image")["payload"]
    assert_that(get_image).contains_entry({"context": "other-ctx"}, {"call": request})
    host.send("image_result", {"request": get_image["request"], "image": None, "format": "png"})
    seen = [host.recv()]
    while seen[-1]["event"] != "action_result":
        seen.append(host.recv())
    return seen


def test_foreign_paint_inside_action_lands_before_action_result(host: _Host) -> None:
    seen = _round_trip(host, "a.paint", "r1")
    assert_that([m["event"] for m in seen]).is_equal_to(["set_image", "action_result"])
    assert_that(seen[0]["context"]).is_equal_to("other-ctx")
    assert_that(seen[0]["payload"]["request"]).is_equal_to("r1")
    assert_that(_pixel(seen[0]["payload"])).is_equal_to(BLUE)
    assert_that(seen[1]["payload"]).is_equal_to({"request": "r1", "ok": True})


def test_foreign_paint_lands_before_a_failed_action_result(host: _Host) -> None:
    seen = _round_trip(host, "a.paint_fail", "r2")
    assert_that([m["event"] for m in seen]).is_equal_to(["log", "set_image", "action_result"])
    assert_that(seen[2]["payload"]["ok"]).is_false()


def test_bad_call_field_does_not_leave_a_proxy_unclosed(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _Runtime({}, io.BytesIO())
    made: list[Any] = []
    original = runtime._card_proxy

    def spy(data: Any, request: str) -> Any:
        made.append(original(data, request))
        return made[-1]

    monkeypatch.setattr(runtime, "_card_proxy", spy)
    payload = _call("a.paint", "r3")
    payload["depth"] = "not a number"
    with pytest.raises(ValueError):
        runtime._action_context(payload)
    assert_that(made).is_empty()


def test_ticks_send_at_most_the_budget_and_spread_the_rest(ticker: FrameTicker, sink: Sink) -> None:
    canvases = {name: _canvas(ticker, name) for name in "abcde"}
    for index, canvas in enumerate(canvases.values()):
        _draw(canvas, (index, 0, 0, 255))
    _draw(canvases["e"], BLUE)  # the latest snapshot replaces the older one
    per_tick: list[list[str | None]] = []
    for _ in range(4):
        before = len(sink.images())
        ticker.tick()
        per_tick.append([context for context, _ in sink.images()[before:]])
    assert_that(MAX_FRAMES_PER_TICK).is_equal_to(2)
    assert_that(per_tick).is_equal_to([["a", "b"], ["c", "d"], ["e"], []])
    assert_that(_pixel(sink.images()[4][1])).is_equal_to(BLUE)


def test_repeatedly_dirtied_key_is_not_starved(ticker: FrameTicker, sink: Sink) -> None:
    first, second, third = _canvas(ticker, "a"), _canvas(ticker, "b"), _canvas(ticker, "c")
    for canvas in (first, second, third):
        _draw(canvas)
    ticker.tick()  # a and b
    for _ in range(3):
        _draw(first)  # a is dirtied again and again, yet c (older) goes first
        _draw(second)
    ticker.tick()
    assert_that([context for context, _ in sink.images()][2]).is_equal_to("c")
    assert_that([context for context, _ in sink.images()][3]).is_equal_to("a")


def test_foreign_keys_go_before_own_keys_and_flush_ignores_the_budget(ticker: FrameTicker, sink: Sink) -> None:
    for name in "abc":
        _draw(_canvas(ticker, name))
    for name in "xyz":
        _draw(_canvas(ticker, name, "req1"))
    ticker.tick()
    assert_that([context for context, _ in sink.images()]).is_equal_to(["x", "y"])
    ticker.flush("req1")
    assert_that([context for context, _ in sink.images()][2:]).is_equal_to(["z"])
    ticker.flush("req1")
    assert_that(sink.images()).is_length(3)


def test_set_image_is_recorded_as_the_last_sent_frame(ticker: FrameTicker, sink: Sink) -> None:
    ctx = CardContext("c1", "card", "dev", 0, 0, 0, size=(8, 8), _send=sink, _frames=ticker)
    ctx.set_image(base64.b64decode(_png(RED)))
    assert_that(ctx.last_paint).is_not_none()
    with ctx.canvas.paint() as img:
        assert_that(img.getpixel((3, 3))).is_equal_to(RED)
    other = CardContext("c2", "card", "dev", 0, 0, 0, size=(8, 8), _send=sink, _frames=ticker)
    other.set_image(b"<svg/>", "svg")
    assert_that(other.last_paint).is_not_none()
    with other.canvas.paint() as img:
        assert_that(img.getpixel((3, 3))).is_equal_to((0, 0, 0, 0))


def test_pending_foreign_fetches_do_not_block_the_pool(sink: Sink, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("decker_plugin_sdk._canvas.IMAGE_TIMEOUT", 60.0)
    pool = ElasticPool()
    ticker = FrameTicker(sink, interval=3600, blocking=pool.blocking)
    finished, other_ran = threading.Barrier(5, timeout=WAIT), threading.Event()
    try:
        for index in range(4):  # as many fetches as the pool has workers
            pool.submit(lambda i=index: (ticker.fetch(f"c{i}", "req"), finished.wait()))
        deadline = threading.Event()
        for _ in range(int(WAIT * 100)):
            with sink.lock:
                if len(sink.messages) == 4:
                    break
            deadline.wait(0.01)
        pool.submit(other_ran.set)
        assert_that(other_ran.wait(WAIT)).described_as("other work ran while fetches wait").is_true()
        for _, _, payload in list(sink.messages):
            ticker.deliver({"request": payload["request"], "image": None, "format": "png"})
        finished.wait()
    finally:
        pool.shutdown()


def test_concurrent_first_canvas_access_yields_one_canvas(ticker: FrameTicker, sink: Sink) -> None:
    ctx = CardContext("c1", "card", "dev", 0, 0, 0, size=(8, 8), _send=sink, _frames=ticker)
    proxy = CardProxy(_foreign_payload(), "req1", sink, ticker)
    for owner in (ctx, proxy):
        seen: list[Canvas] = []
        barrier = threading.Barrier(8, timeout=WAIT)

        def grab(owner: Any = owner, seen: list[Canvas] = seen, barrier: threading.Barrier = barrier) -> None:
            barrier.wait()
            seen.append(owner.canvas)

        threads = [threading.Thread(target=grab) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(WAIT)
        assert_that({id(c) for c in seen}).is_length(1)
        assert_that(seen).is_length(8)
