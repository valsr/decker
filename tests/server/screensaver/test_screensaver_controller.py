import io
import json
import logging
import sqlite3
import threading
import time
from dataclasses import replace

import pytest
from assertpy import assert_that
from PIL import Image

from decker.common.model import ScreensaverSettings
from decker.server.screensaver.screensaver_controller import ScreensaverController
from decker.server.screensaver.settings import settings_to_json
from tests.server.screensaver.screensaver_fixture import (
    COLS,
    DEV,
    PX,
    ROWS,
    FakeConn,
    FakeDevice,
    hw_info,
    saver_context,
)


@pytest.fixture(name="stack")
def stack_fixture(tmp_path):
    with saver_context(tmp_path) as (ctx, devices):
        yield ctx, devices


def _controller(stack) -> ScreensaverController:
    assert stack[0].screensaver_controller is not None
    return stack[0].screensaver_controller


def _json(**changes) -> str:
    return settings_to_json(ScreensaverSettings(**changes))


def _png(path, color=(200, 30, 30)) -> str:
    Image.new("RGB", (60, 40), color).save(path)
    return str(path)


def test_get_returns_defaults_with_runtime_status(stack):
    result = _controller(stack).get(DEV)

    assert_that(result).contains_entry(**{"brightness": 100, "active": False, "last_error": ""})


def test_unknown_device_rejected(stack):
    controller = _controller(stack)

    for call in (
        lambda: controller.get("nope"),
        lambda: controller.set("nope", _json(), loopback=True),
        lambda: controller.activate("nope"),
        lambda: controller.deactivate("nope"),
        lambda: controller.preview("nope", _json(), loopback=True),
    ):
        with pytest.raises(ValueError, match="unknown device"):
            call()
    assert_that(stack[0].screensaver_store.get("nope")).is_equal_to(ScreensaverSettings())


def test_remote_cannot_change_paths(stack, tmp_path):
    controller = _controller(stack)
    image = _png(tmp_path / "a.png")

    with pytest.raises(PermissionError):
        controller.set(DEV, _json(content="image", image_path=image), loopback=False)
    with pytest.raises(PermissionError):
        controller.set(DEV, _json(content="gallery", folder_path=str(tmp_path)), loopback=False)

    assert_that(controller.get(DEV)["image_path"]).is_empty()


def test_remote_can_change_other_fields(stack, tmp_path):
    controller = _controller(stack)
    image = _png(tmp_path / "a.png")
    controller.set(DEV, _json(content="image", image_path=image), loopback=True)

    controller.set(DEV, _json(content="image", image_path=image, timeout_minutes=7, brightness=40), loopback=False)

    assert_that(controller.get(DEV)).contains_entry(**{"timeout_minutes": 7, "brightness": 40, "image_path": image})


def test_invalid_settings_raise_value_error_and_store_nothing(stack):
    controller = _controller(stack)

    for bad in ("not json", "[]", _json().replace('"brightness": 100', '"brightness": 500'), 5):
        with pytest.raises(ValueError):
            controller.set(DEV, bad, loopback=True)  # type: ignore[arg-type]

    assert_that(controller.get(DEV)["brightness"]).is_equal_to(100)


def test_set_never_activates(stack):
    controller = _controller(stack)

    controller.set(DEV, _json(timeout_minutes=1, content="none"), loopback=True)

    assert_that(controller.get(DEV)["active"]).is_false()
    assert_that([c for c in stack[1].calls if c[0] in ("overlay", "write")]).is_empty()
    assert_that(stack[1].calls).contains(("brightness", DEV, 100))  # normal brightness is re-applied


def test_events_reach_clients_through_the_event_bus(stack):
    conn = FakeConn()
    stack[0].event_bus.register(conn, loopback=False)
    controller = _controller(stack)

    controller.set(DEV, _json(saver_brightness=0), loopback=True)
    controller.activate(DEV)
    controller.deactivate(DEV)

    assert_that(conn.events("screensaver_settings_changed")).is_equal_to([{"device_id": DEV}])
    assert_that(conn.events("screensaver_changed")).is_equal_to(
        [{"device_id": DEV, "active": True}, {"device_id": DEV, "active": False}]
    )


def test_events_never_carry_last_error(stack):
    conn = FakeConn()
    stack[0].event_bus.register(conn, loopback=True)
    controller = _controller(stack)
    controller.set(DEV, _json(content="image", image_path="/definitely/missing.png"), loopback=True)

    controller.activate(DEV)

    assert_that(controller.get(DEV)["last_error"]).is_not_empty()
    assert_that(json.dumps(conn.received)).does_not_contain("missing.png")


def test_activate_and_deactivate_flip_active_and_report_result(stack):
    controller = _controller(stack)

    assert_that(controller.deactivate(DEV)).is_false()
    assert_that(controller.activate(DEV)).is_true()
    assert_that(controller.get(DEV)["active"]).is_true()
    assert_that(controller.deactivate(DEV)).is_true()
    assert_that(controller.get(DEV)["active"]).is_false()


def test_corrupt_stored_row_falls_back_to_defaults_with_a_warning(stack, tmp_path, caplog):
    with sqlite3.connect(tmp_path / "decker.db") as db:
        db.execute("INSERT INTO screensaver_settings VALUES (?, ?)", (DEV, '{"brightness": "x"}'))

    with caplog.at_level(logging.WARNING):
        result = _controller(stack).get(DEV)

    assert_that(result["brightness"]).is_equal_to(100)
    assert_that(caplog.text).contains("invalid")


def _decode(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("RGB")


def _pixel(image: Image.Image, x: int, y: int) -> tuple[int, ...]:
    pixel = image.getpixel((x, y))
    assert isinstance(pixel, tuple)
    return pixel


def test_preview_returns_png(stack, tmp_path):
    image = _png(tmp_path / "a.png", (200, 30, 30))

    png = _controller(stack).preview(DEV, _json(content="image", image_path=image), loopback=True)

    decoded = _decode(png)
    assert_that(png[:8]).is_equal_to(b"\x89PNG\r\n\x1a\n")
    assert_that(decoded.size).is_equal_to((COLS * PX, ROWS * PX))
    assert_that(_pixel(decoded, PX // 2, PX // 2)[0]).is_greater_than(150)


def test_preview_without_image_is_a_dark_canvas(stack):
    decoded = _decode(_controller(stack).preview(DEV, _json(), loopback=False))

    assert_that(decoded.size).is_equal_to((COLS * PX, ROWS * PX))
    assert_that(max(_pixel(decoded, 3, 3))).is_less_than(40)


def test_preview_uses_the_first_gallery_image(stack, tmp_path):
    folder = tmp_path / "gallery"
    folder.mkdir()
    _png(folder / "a.png", (30, 200, 30))
    _png(folder / "b.png", (30, 30, 200))

    decoded = _decode(_controller(stack).preview(DEV, _json(content="gallery", folder_path=str(folder)), loopback=True))

    assert_that(_pixel(decoded, PX // 2, PX // 2)[1]).is_greater_than(150)


def test_remote_preview_with_a_path_is_refused(stack, tmp_path):
    controller = _controller(stack)

    with pytest.raises(PermissionError):
        controller.preview(DEV, _json(content="image", image_path=_png(tmp_path / "a.png")), loopback=False)
    with pytest.raises(PermissionError):
        controller.preview(DEV, _json(content="gallery", folder_path=str(tmp_path)), loopback=False)


def test_preview_of_an_unreadable_image_is_a_clear_error(stack):
    with pytest.raises(ValueError, match="not found"):
        _controller(stack).preview(DEV, _json(content="image", image_path="/missing/x.png"), loopback=True)


def test_remote_preview_with_the_stored_paths_is_allowed(stack, tmp_path):
    controller = _controller(stack)
    image = _png(tmp_path / "a.png", (200, 30, 30))
    controller.set(DEV, _json(content="image", image_path=image), loopback=True)

    decoded = _decode(controller.preview(DEV, _json(content="image", image_path=image, bezel=True), loopback=False))

    assert_that(_pixel(decoded, PX // 2, PX // 2)[0]).is_greater_than(150)
    with pytest.raises(PermissionError):
        controller.preview(DEV, _json(content="image", image_path=_png(tmp_path / "b.png")), loopback=False)


def test_get_and_preview_work_for_a_deck_without_key_screens(stack, monkeypatch):
    ctx, devices = stack
    blind = replace(hw_info(), keys=[replace(key, pixel_width=0, pixel_height=0) for key in hw_info().keys])
    monkeypatch.setattr(devices, "list_devices", lambda: [blind])
    monkeypatch.setattr(FakeDevice, "get_hw_info", lambda self: blind)
    controller = _controller(stack)

    assert_that(controller.get(DEV)["active"]).is_false()
    assert_that(_decode(controller.preview(DEV, _json(), loopback=True)).size).is_equal_to((72, 72))
    assert_that(ctx.screensaver_engine.activate(DEV)).is_false()


def test_set_brightness_clamps_stores_and_announces(stack):
    controller = _controller(stack)
    conn = FakeConn()
    stack[0].event_bus.register(conn, loopback=True)

    assert_that(controller.set_brightness(DEV, 0)).is_equal_to(1)
    assert_that(controller.set_brightness(DEV, 250)).is_equal_to(100)

    assert_that(controller.get(DEV)["brightness"]).is_equal_to(100)
    assert_that(controller.brightness(DEV)).is_equal_to(100)
    assert_that(conn.events("screensaver_settings_changed")).is_not_empty()


def test_set_brightness_keeps_other_settings_and_rejects_bad_input(stack):
    controller = _controller(stack)
    controller.set(DEV, _json(timeout_minutes=7), loopback=True)

    controller.set_brightness(DEV, 30)

    assert_that(controller.get(DEV)).contains_entry(brightness=30, timeout_minutes=7)
    for bad in ("x", True, None):
        with pytest.raises(ValueError, match="integer"):
            controller.set_brightness(DEV, bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown device"):
        controller.set_brightness("nope", 5)


def test_listener_fires_only_on_change_from_set_and_set_brightness(stack):
    controller = _controller(stack)
    seen: list[tuple[str, int]] = []
    controller.add_brightness_listener(lambda device, percent: seen.append((device, percent)))

    controller.set_brightness(DEV, 100)  # unchanged
    controller.set(DEV, _json(brightness=60), loopback=True)
    controller.set(DEV, _json(brightness=60, timeout_minutes=3), loopback=True)  # unchanged
    controller.set_brightness(DEV, 20)

    assert_that(seen).is_equal_to([(DEV, 60), (DEV, 20)])


def test_a_failing_listener_does_not_break_the_edit(stack):
    controller = _controller(stack)

    def boom(_device: str, _percent: int) -> None:
        raise RuntimeError("x")

    controller.add_brightness_listener(boom)

    assert_that(controller.set_brightness(DEV, 10)).is_equal_to(10)


def test_brightness_survives_wake(stack):
    controller = _controller(stack)
    devices = stack[1]
    controller.activate(DEV)

    controller.set_brightness(DEV, 35)
    controller.deactivate(DEV)

    assert_that([c for c in devices.calls if c[0] == "brightness"][-1]).is_equal_to(("brightness", DEV, 35))


def test_a_user_edit_during_a_brightness_change_is_not_lost(stack):
    controller = _controller(stack)
    store = stack[0].screensaver_store
    real_get = store.get
    first = threading.Event()

    def slow_get(device_id):
        result = real_get(device_id)
        if threading.current_thread().name == "brightness" and not first.is_set():
            first.set()
            time.sleep(0.4)  # the window between the read and the write
        return result

    store.get = slow_get
    worker = threading.Thread(target=controller.set_brightness, args=(DEV, 30), name="brightness")
    worker.start()
    first.wait(2)
    controller.set(DEV, _json(timeout_minutes=7), loopback=True)
    worker.join()

    assert_that(controller.get(DEV)["timeout_minutes"]).is_equal_to(7)
