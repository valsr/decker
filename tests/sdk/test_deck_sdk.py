import threading

import pytest
from assertpy import assert_that

import decker_plugin_sdk as sdk
from decker_plugin_sdk import _deck
from tests.sdk.test_variables_sdk import Rig

WAIT = 5.0


@pytest.fixture(name="hooks")
def hooks_fixture():
    saved = {key: list(value) for key, value in _deck._HOOKS.items()}  # pylint: disable=protected-access
    _deck._HOOKS.clear()  # pylint: disable=protected-access
    yield
    _deck._HOOKS.clear()  # pylint: disable=protected-access
    _deck._HOOKS.update(saved)  # pylint: disable=protected-access


def _stop(rig: Rig) -> None:
    rig.send("shutdown")
    rig.thread.join(WAIT)


def _poll_devices(rig: Rig, expected: list[str]) -> list[str]:
    # connected_devices() is process-wide, so ask through a hook-free probe after a ping round trip
    rig.send("ping")
    rig.recv_until("pong")
    assert_that(sdk.connected_devices()).is_equal_to(expected)
    return sdk.connected_devices()


def test_sdk_tracks_devices_and_calls_hooks(hooks):
    seen: list[tuple[str, str]] = []
    done = threading.Event()

    @sdk.on_device_connected
    def _up(device: str) -> None:
        seen.append(("up", device))

    @sdk.on_device_disconnected
    def _down(device: str) -> None:
        seen.append(("down", device))
        done.set()

    rig = Rig()
    try:
        rig.send("device_connected", {"device": "dev1"})
        rig.send("device_connected", {"device": "dev1"})
        _poll_devices(rig, ["dev1"])
        rig.send("device_disconnected", {"device": "dev1"})
        assert_that(done.wait(WAIT)).is_true()
        _poll_devices(rig, [])
        assert_that(seen).contains(("up", "dev1"), ("down", "dev1"))
        assert_that([s for s in seen if s[0] == "up"]).is_length(2)  # once per message
    finally:
        _stop(rig)
    assert_that(sdk.connected_devices()).is_equal_to([])


def test_reopen_during_brightness_write_ends_connected(hooks):
    rig = Rig()
    try:
        rig.send("device_connected", {"device": "dev1"})
        rig.send("device_disconnected", {"device": "dev1"})
        rig.send("device_connected", {"device": "dev1"})
        _poll_devices(rig, ["dev1"])
    finally:
        _stop(rig)


def test_a_failing_hook_is_logged_not_fatal(hooks):
    @sdk.on_device_connected
    def _boom(_device: str) -> None:
        raise ValueError("nope")

    rig = Rig()
    try:
        rig.send("device_connected", {"device": "dev1"})
        message = rig.recv_until("log")
        assert_that(message["payload"]["message"]).contains("nope")
        _poll_devices(rig, ["dev1"])
    finally:
        _stop(rig)


def _serve_brightness(rig: Rig, percent: int | None) -> dict:
    request = rig.recv_until("get_deck_brightness")["payload"]
    reply = {"request": request["request"], "device": request["device"]}
    reply.update({"percent": percent} if percent is not None else {"error": "unknown device"})
    rig.send("deck_brightness", reply)
    return request


def test_sdk_get_set_and_hook(hooks):
    seen: list[tuple[str, int]] = []
    done = threading.Event()

    @sdk.on_deck_brightness_changed
    def _changed(device: str, percent: int) -> None:
        seen.append((device, percent))
        done.set()

    rig = Rig()
    try:
        rig.send("hello", {"protocol": 6, "decker_version": "x", "plugin_id": "t", "global_settings": {}})
        rig.recv_until("ready")
        result: list[int] = []
        getter = threading.Thread(target=lambda: result.append(sdk.get_deck_brightness("dev1")))
        getter.start()
        assert_that(_serve_brightness(rig, 70)["device"]).is_equal_to("dev1")
        getter.join(WAIT)
        assert_that(result).is_equal_to([70])

        sdk.set_deck_brightness(30, "dev1")
        assert_that(rig.recv_until("set_deck_brightness")["payload"]).is_equal_to({"device": "dev1", "percent": 30})

        rig.send("deck_brightness_changed", {"device": "dev1", "percent": 30})
        assert_that(done.wait(WAIT)).is_true()
        assert_that(seen).is_equal_to([("dev1", 30)])
    finally:
        _stop(rig)


def test_sdk_brightness_errors(hooks):
    rig = Rig()
    try:
        rig.send("hello", {"protocol": 6, "decker_version": "x", "plugin_id": "t", "global_settings": {}})
        rig.recv_until("ready")
        with pytest.raises(sdk.ActionError, match="no device"):
            sdk.get_deck_brightness()
        with pytest.raises(sdk.ActionError, match="timed out"):
            sdk.get_deck_brightness("dev1", timeout=0.2)
        rig.recv_until("get_deck_brightness")  # the timed-out request
        errors: list[str] = []
        getter = threading.Thread(target=lambda: _capture(errors))
        getter.start()
        _serve_brightness(rig, None)
        getter.join(WAIT)
        assert_that(errors).is_equal_to(["unknown device"])
    finally:
        _stop(rig)


def _capture(errors: list[str]) -> None:
    try:
        sdk.get_deck_brightness("nodev")
    except sdk.ActionError as exc:
        errors.append(str(exc))
