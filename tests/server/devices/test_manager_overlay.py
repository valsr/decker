import threading
from unittest.mock import MagicMock, patch

from assertpy import assert_that

from decker.animation.factory import AnimationFactory
from decker.server.device_state_store import DeviceStateStore
from decker.server.devices import _UNKNOWN, _KeyTracker
from decker.server.devices.manager import DeviceManager
from tests.server.animation_fixture import make_spec
from tests.server.devices.overlay_fixture import (
    DEVICE_ID,
    make_deck,
    open_manager,
    png,
    press,
    state_of,
)


def _events() -> tuple[list[tuple[str, int, bool]], list[str], list[str]]:
    return [], [], []


def _wired(deck):
    delivered, woke, active = _events()
    manager, device = open_manager(deck, lambda d, k, p: delivered.append((d, k, p)))
    manager.set_wake_hook(woke.append)
    manager.set_activity_hook(active.append)
    return manager, device, delivered, woke, active


def test_wake_press_and_release_are_swallowed():
    deck = make_deck()
    manager, device, delivered, woke, _ = _wired(deck)
    manager.set_overlay(DEVICE_ID, True)
    press(deck, 1, True)
    manager.set_overlay(DEVICE_ID, False)
    press(deck, 1, False)  # the release of the wake press arrives after the overlay is gone
    assert_that(woke).is_equal_to([DEVICE_ID])
    assert_that(delivered).is_empty()
    assert_that(device.get_key_state(1).name).is_equal_to("UP")
    press(deck, 1, True)
    press(deck, 1, False)
    assert_that(delivered).is_equal_to([(DEVICE_ID, 1, True), (DEVICE_ID, 1, False)])
    manager.destroy()


def test_other_key_release_during_overlay_swallowed():
    deck = make_deck()
    manager, _, delivered, woke, _ = _wired(deck)
    manager.set_overlay(DEVICE_ID, True)
    press(deck, 2, False)
    assert_that(woke).is_empty()
    assert_that(delivered).is_empty()
    manager.destroy()


def test_normal_events_pass_through_and_call_activity_hook():
    deck = make_deck()
    manager, _, delivered, woke, active = _wired(deck)
    press(deck, 0, True)
    press(deck, 0, False)
    assert_that(delivered).is_length(2)
    assert_that(active).is_equal_to([DEVICE_ID, DEVICE_ID])
    assert_that(woke).is_empty()
    manager.set_activity_hook(lambda _: 1 / 0)  # a bad hook never breaks delivery
    press(deck, 0, True)
    assert_that(delivered).is_length(3)
    manager.destroy()


def test_writes_during_overlay_update_state_but_not_hardware():
    deck = make_deck()
    manager, device = open_manager(deck)
    manager.set_overlay(DEVICE_ID, True)
    deck.set_key_image.reset_mock()
    manager.set_image_bytes(DEVICE_ID, 0, png("red"))
    manager.blank_key(DEVICE_ID, 1)
    device.clear_key_image(2)
    assert_that(state_of(manager).get_key_state(0).image).is_not_none()
    assert_that(manager.show_page(DEVICE_ID, "p2")).is_true()
    deck.set_key_image.assert_not_called()
    manager.destroy()


def test_write_overlay_bypasses_gate():
    deck = make_deck()
    manager, _ = open_manager(deck)
    manager.set_overlay(DEVICE_ID, True)
    deck.set_key_image.reset_mock()
    assert_that(manager.write_overlay(DEVICE_ID, [png("red"), png("blue"), png("green")])).is_true()
    assert_that(deck.set_key_image.call_count).is_equal_to(3)
    assert_that(manager.write_overlay(DEVICE_ID, None)).is_true()
    assert_that(deck.set_key_image.call_args_list[-1].args[1]).is_none()
    assert_that(manager.write_overlay("unknown", None)).is_false()
    deck.set_key_image.side_effect = RuntimeError("usb")
    assert_that(manager.write_overlay(DEVICE_ID, None)).is_false()
    manager.destroy()


def test_show_page_after_overlay_off_redraws_cached_state(tmp_path):
    store = DeviceStateStore(data_dir=tmp_path)
    store.create(MagicMock())
    deck = make_deck()
    manager, _ = open_manager(deck, store=store)
    page = state_of(manager).page_id
    manager.set_image_bytes(DEVICE_ID, 0, png("red"))
    manager.set_overlay(DEVICE_ID, True)
    manager.write_overlay(DEVICE_ID, None)
    manager.set_image_bytes(DEVICE_ID, 1, png("blue"))  # a plugin write while the screensaver shows
    deck.set_key_image.reset_mock()
    manager.set_overlay(DEVICE_ID, False)
    manager.show_page(DEVICE_ID, page)
    written = {call.args[0]: call.args[1] for call in deck.set_key_image.call_args_list}
    assert_that(written).contains_key(0, 1)
    assert_that(written[0]).is_not_none()
    assert_that(written[1]).is_not_none()
    assert_that(written[2]).is_none()
    assert_that(manager.overlay_active(DEVICE_ID)).is_false()
    manager.destroy()
    store.destroy()


def test_stale_device_events_do_not_wake():
    deck = make_deck()
    manager, device, delivered, woke, active = _wired(deck)
    manager.set_overlay(DEVICE_ID, True)
    manager.close_device(device)
    press(deck, 0, True)
    assert_that(woke).is_empty()
    assert_that(active).is_empty()
    assert_that(delivered).is_empty()


class _FakeWriter:
    def __init__(self, device) -> None:
        self._device = device
        self.played: list[tuple[int, bool]] = []  # (key, was the gate lock held)

    def play_spec(self, key_index, spec, poster_written=True) -> None:
        self.played.append((key_index, self._device._gate_lock.locked()))

    def submit_static(self, key_index, native) -> bool:
        return True

    def stop_all(self, wait=0.0) -> None:
        pass

    def stop(self, key_index) -> None:
        pass

    def animation_id(self, key_index):
        return None

    def close(self, timeout=0.0) -> None:
        pass


def _animated(store):
    deck = make_deck()
    manager = DeviceManager(on_key_event=lambda *_: None)
    manager._state_store = store
    manager._factory = AnimationFactory(cache_bytes=1024 * 1024)
    with patch("decker.server.devices.manager._HardwareDeviceManager") as hw:
        hw.return_value.enumerate.return_value = [deck]
        device = manager.open_device(DEVICE_ID)
    assert device is not None
    writer = _FakeWriter(device)
    device._writer = writer  # type: ignore[assignment]
    return manager, device, writer


def test_animation_while_gated_is_cached_and_restarts_after_release(tmp_path):
    store = DeviceStateStore(data_dir=tmp_path)
    store.create(MagicMock())
    manager, _, writer = _animated(store)
    page = state_of(manager).page_id
    manager.set_image_bytes(DEVICE_ID, 0, png("red"))
    manager.set_overlay(DEVICE_ID, True)
    assert_that(manager.set_animation(DEVICE_ID, 0, make_spec())).is_true()
    assert_that(writer.played).is_empty()
    assert_that(state_of(manager).get_key_animation(0)).is_not_none()
    manager.set_overlay(DEVICE_ID, False)
    manager.show_page(DEVICE_ID, page)
    assert_that([key for key, _ in writer.played]).is_equal_to([0])
    manager.destroy()
    store.destroy()


def test_play_animation_submits_under_the_gate_lock():
    store = None
    manager, device, writer = _animated(store)
    device.play_animation(1, make_spec())
    assert_that(writer.played).is_equal_to([(1, True)])
    manager.destroy()


def test_in_flight_write_holds_the_gate_lock_until_it_lands():
    deck = make_deck()
    manager, device = open_manager(deck)
    entered, release = threading.Event(), threading.Event()
    held: list[bool] = []

    def blocking_write(key_index, data):
        held.append(device._gate_lock.locked())
        entered.set()
        release.wait(5)

    deck.set_key_image.side_effect = blocking_write
    thread = threading.Thread(target=manager.set_image_bytes, args=(DEVICE_ID, 0, png("red")))
    thread.start()
    assert_that(entered.wait(5)).is_true()
    assert_that(device._gate_lock.acquire(blocking=False)).is_false()  # set_write_gate would have to wait
    release.set()
    thread.join(5)
    assert_that(held).is_equal_to([True])
    manager.destroy()


def test_non_overlay_press_clears_a_stale_swallow_entry():
    deck = make_deck()
    manager, _, delivered, _, _ = _wired(deck)
    manager.set_overlay(DEVICE_ID, True)
    press(deck, 1, True)  # wake press whose release never arrives
    manager.set_overlay(DEVICE_ID, False)
    press(deck, 1, True)
    press(deck, 1, False)
    assert_that(delivered).is_equal_to([(DEVICE_ID, 1, True), (DEVICE_ID, 1, False)])
    manager.destroy()


def test_closing_or_reopening_a_device_forgets_swallowed_releases():
    deck = make_deck()
    manager, device, delivered, _, _ = _wired(deck)
    manager.set_overlay(DEVICE_ID, True)
    press(deck, 1, True)
    manager.set_overlay(DEVICE_ID, False)
    manager.close_device(device)
    assert_that(manager._swallow_release).is_empty()
    manager.set_overlay(DEVICE_ID, True)
    press_key = (DEVICE_ID, 2)
    manager._swallow_release.add(press_key)
    with patch("decker.server.devices.manager._HardwareDeviceManager") as hw:
        hw.return_value.enumerate.return_value = [deck]
        manager.open_device(DEVICE_ID)
    assert_that(manager._swallow_release).is_empty()
    assert_that(delivered).is_empty()
    manager.destroy()


def test_reopened_device_is_gated_while_overlay_is_active():
    deck = make_deck()
    manager, device = open_manager(deck)
    manager.set_overlay(DEVICE_ID, True)
    manager.close_device(device)
    with patch("decker.server.devices.manager._HardwareDeviceManager") as hw:
        hw.return_value.enumerate.return_value = [deck]
        reopened = manager.open_device(DEVICE_ID)
    assert reopened is not None and reopened is not device
    deck.set_key_image.reset_mock()
    manager.set_image_bytes(DEVICE_ID, 0, png("red"))
    deck.set_key_image.assert_not_called()
    manager.set_overlay(DEVICE_ID, False)
    manager.set_image_bytes(DEVICE_ID, 0, png("red"))
    assert_that(deck.set_key_image.call_count).is_equal_to(1)
    manager.destroy()


def test_gate_matches_overlay_state_after_interleaved_set_overlay():
    deck = make_deck()
    manager, device = open_manager(deck)
    threads = [threading.Thread(target=manager.set_overlay, args=(DEVICE_ID, active)) for active in (True, False) * 20]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert_that(device._gated).is_equal_to(manager.overlay_active(DEVICE_ID))
    manager.destroy()


def test_set_write_gate_survives_concurrent_tracker_creation():
    deck = make_deck()
    manager, device = open_manager(deck)
    stop = threading.Event()

    def churn() -> None:
        for index in range(100, 3000):
            if stop.is_set():
                return
            device._key_trackers.setdefault(index, _KeyTracker())

    thread = threading.Thread(target=churn)
    thread.start()
    try:
        for _ in range(50):
            device.set_write_gate(True)
            device.set_write_gate(False)
    finally:
        stop.set()
        thread.join(5)
    assert_that(device._gated).is_false()
    for tracker in device._key_trackers.values():
        tracker.shown = b"drawn"
    device.set_write_gate(True)
    assert_that(device._gated).is_true()
    assert_that(len(device._key_trackers)).is_greater_than(3)
    assert_that({id(t.shown) for t in device._key_trackers.values()}).is_equal_to({id(_UNKNOWN)})
    manager.destroy()


def test_release_of_a_key_held_when_the_overlay_starts_is_delivered():
    deck = make_deck()
    manager, device, delivered, woke, _ = _wired(deck)
    press(deck, 0, True)
    manager.set_overlay(DEVICE_ID, True)

    press(deck, 0, False)

    assert_that(delivered).is_equal_to([(DEVICE_ID, 0, True), (DEVICE_ID, 0, False)])
    assert_that(woke).is_empty()  # a release never wakes
    assert_that(device.get_key_state(0).name).is_equal_to("UP")
    press(deck, 0, True)  # the next press wakes and is swallowed with its release
    press(deck, 0, False)
    assert_that(delivered).is_length(2)
    assert_that(woke).is_equal_to([DEVICE_ID])
    manager.destroy()


def test_a_swallowed_press_while_held_drops_the_held_record():
    deck = make_deck()
    manager, _, delivered, woke, _ = _wired(deck)
    press(deck, 1, True)
    manager.set_overlay(DEVICE_ID, True)
    press(deck, 1, True)  # a repeated press report while the overlay shows

    press(deck, 1, False)

    assert_that(delivered).is_equal_to([(DEVICE_ID, 1, True)])
    assert_that(woke).is_equal_to([DEVICE_ID])
    manager.destroy()


def test_reopening_a_device_forgets_held_keys():
    deck = make_deck()
    manager, device, delivered, _, _ = _wired(deck)
    press(deck, 2, True)
    manager.close_device(device)
    with patch("decker.server.devices.manager._HardwareDeviceManager") as hw:
        hw.return_value.enumerate.return_value = [deck]
        assert_that(manager.open_device(DEVICE_ID)).is_not_none()
    manager.set_overlay(DEVICE_ID, True)

    press(deck, 2, False)

    assert_that(delivered).is_equal_to([(DEVICE_ID, 2, True)])
    manager.destroy()


def test_open_and_close_listeners_fire_outside_the_lock():
    deck = make_deck()
    manager, device = open_manager(deck)
    opened: list[str] = []
    closed: list[str] = []
    lock_free: list[bool] = []

    def on_close(device_id: str) -> None:
        got = manager._lock.acquire(blocking=False)  # pylint: disable=protected-access
        lock_free.append(got)
        if got:
            manager._lock.release()  # pylint: disable=protected-access
        closed.append(device_id)

    manager.add_open_listener(opened.append)
    manager.add_close_listener(on_close)
    manager._on_device_closed(device)  # pylint: disable=protected-access
    assert_that(closed).is_equal_to([DEVICE_ID])
    assert_that(lock_free).is_equal_to([True])
    with patch("decker.server.devices.manager._HardwareDeviceManager") as hw:
        hw.return_value.enumerate.return_value = [deck]
        manager.open_device(DEVICE_ID)
    assert_that(opened).is_equal_to([DEVICE_ID])
    manager.destroy()


def test_a_failing_listener_does_not_break_open():
    deck = make_deck()
    manager = DeviceManager(on_key_event=lambda *_: None)
    later: list[str] = []

    def boom(_device_id: str) -> None:
        raise RuntimeError("listener bug")

    manager.add_open_listener(boom)
    manager.add_open_listener(later.append)
    with patch("decker.server.devices.manager._HardwareDeviceManager") as hw:
        hw.return_value.enumerate.return_value = [deck]
        device = manager.open_device(DEVICE_ID)
    assert_that(device).is_not_none()
    assert_that(later).is_equal_to([DEVICE_ID])
    manager.destroy()


def test_a_stale_device_closing_does_not_fire_close_listeners():
    deck = make_deck()
    manager, device = open_manager(deck)
    closed: list[str] = []
    manager.add_close_listener(closed.append)
    manager.remove_close_listener(closed.append)
    manager.add_close_listener(closed.append)
    manager._on_device_closed(device)  # pylint: disable=protected-access
    manager._on_device_closed(device)  # pylint: disable=protected-access
    assert_that(closed).is_equal_to([DEVICE_ID])
    manager.destroy()


def test_close_listeners_fire_before_the_slow_close_so_a_reopen_cannot_overtake_them():
    deck = make_deck()
    manager, device = open_manager(deck)
    order: list[str] = []
    real_close = device.close

    def slow_close() -> None:
        order.append("close")
        real_close()

    device.close = slow_close  # type: ignore[method-assign]
    manager.add_close_listener(lambda _device_id: order.append("listener"))
    manager._on_device_closed(device)  # pylint: disable=protected-access
    assert_that(order).is_equal_to(["listener", "close"])
    manager.destroy()
