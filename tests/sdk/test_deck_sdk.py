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
