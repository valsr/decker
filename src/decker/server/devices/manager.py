"""Stream Deck device lifecycle management."""

import json
import logging
import math
import threading
import zlib
from collections.abc import Callable
from typing import TYPE_CHECKING

from StreamDeck.DeviceManager import DeviceManager as _HardwareDeviceManager
from StreamDeck.Devices.StreamDeck import StreamDeck

from decker.animation.factory import AnimationFactory
from decker.animation.model import AnimationSpec
from decker.common import config
from decker.common.manager import Manager
from decker.common.model import DeviceHardwareInfo
from decker.server import devices
from decker.server.devices import Device
from decker.server.devices.state import DeviceState
from decker.server.pages import initial_page_id

if TYPE_CHECKING:
    from decker.server.device_state_store import DeviceStateStore
    from decker.server.server_context import ServerContext

_KeyEventCallback = Callable[[str, int, bool], None]  # (device_id, key_index, pressed)

_DeviceHook = Callable[[str], None]  # called with the device id

ANIMATION_ENABLED_DEFAULT: bool = True
ANIMATION_BUDGET_PERCENT_DEFAULT: float = 75.0
ANIMATION_CACHE_MB_DEFAULT: int = 64
ANIMATION_BUDGET_PERCENT_MIN: float = 10.0  # below this even the cheapest rung would never fit
ANIMATION_BUDGET_PERCENT_MAX: float = 90.0  # above this the link has no room left for plugin writes and lag
ANIMATION_CACHE_MB_MIN: int = 1


def _clamped(key: str, value: float, low: float, high: float = math.inf) -> float:
    if low <= value <= high:
        return value
    used = min(max(value, low), high)
    logging.warning(
        "DeviceManager: %s %s is out of range (minimum %s, maximum %s); using %s", key, value, low, high, used
    )
    return used


def device_id_for_serial(serial: str) -> str:
    """Return the stable 8-char CRC-32 hex device ID for a given serial number."""
    return format(zlib.crc32(serial.encode()) & 0xFFFFFFFF, "08x")


class NoSuchDeviceError(Exception):
    """Raised when a requested device ID does not correspond to any connected Stream Deck."""

    def __init__(self, path: str):
        super().__init__(f"Device at path {path} does not exist")


class DeviceNotOpened(Exception):
    """Raises when device can't be opened"""

    def __init__(self, path: str):
        super().__init__(f"Unable to open device at {path}")


class DeviceManager(Manager):  # pylint: disable=too-many-instance-attributes,too-many-public-methods
    """Opens Stream Deck devices on first request and caches one Device instance per device ID."""

    def __init__(self, on_key_event: _KeyEventCallback) -> None:
        self._on_key_event_cb = on_key_event
        self._devices: dict[str, Device] = {}
        self._states: dict[str, DeviceState] = {}
        self._lock = threading.Lock()
        self._context: "ServerContext | None" = None
        self._watcher: threading.Thread | None = None
        self._stop_watch = threading.Event()
        self._unopenable: set[str] = set()  # device paths whose failure to open has been logged
        self._state_store: "DeviceStateStore | None" = None
        self._page_resolver: Callable[[str], str] = initial_page_id
        self._factory: AnimationFactory | None = None
        self._animation_enabled: bool = ANIMATION_ENABLED_DEFAULT
        self._animation_budget: float = ANIMATION_BUDGET_PERCENT_DEFAULT / 100
        self._activity_hook: _DeviceHook | None = None
        self._wake_hook: _DeviceHook | None = None
        self._open_hook: _DeviceHook | None = None
        self._open_listeners: list[_DeviceHook] = []
        self._close_listeners: list[_DeviceHook] = []
        self._overlay: set[str] = set()  # devices whose hardware is held back for the screensaver
        self._overlay_lock = threading.Lock()  # serialises set_overlay so manager and device gates cannot disagree
        self._swallow_release: set[tuple[str, int]] = set()  # keys whose press woke the deck
        self._held: set[tuple[str, int]] = set()  # keys whose press was delivered and whose release is still due

    def create(self, context: "ServerContext") -> None:
        """Wire the state store, active-page lookup and animation settings; devices are opened by open_device()."""
        self._context = context
        self._state_store = context.device_state_store
        self._page_resolver = context.page_controller.active_page_id
        self._animation_enabled = bool(config.get_as_or_default("animation.enabled", bool, ANIMATION_ENABLED_DEFAULT))
        budget_percent = config.get_as_or_default("animation.budget_percent", float, ANIMATION_BUDGET_PERCENT_DEFAULT)
        self._animation_budget = (
            _clamped(
                "animation.budget_percent",
                float(budget_percent),
                ANIMATION_BUDGET_PERCENT_MIN,
                ANIMATION_BUDGET_PERCENT_MAX,
            )
            / 100
        )
        if self._factory is None:
            cache_mb = config.get_as_or_default("animation.cache_mb", int, ANIMATION_CACHE_MB_DEFAULT)
            cache_mb = int(_clamped("animation.cache_mb", int(cache_mb), ANIMATION_CACHE_MB_MIN))
            self._factory = AnimationFactory(cache_bytes=cache_mb * 1024 * 1024)

    @property
    def animation_factory(self) -> AnimationFactory | None:
        """The animation factory shared by all devices, or None before create()."""
        return self._factory

    # ------------------------------------------------------------------
    # Hot-plug and sleep/wake
    # ------------------------------------------------------------------

    def start_watching(self, interval_s: float = 2.0) -> None:
        """Start a thread that notices devices that vanish (e.g. in PC sleep) or appear, and reopens them."""
        if self._watcher is not None and self._watcher.is_alive():
            return
        self._stop_watch.clear()
        self._watcher = threading.Thread(target=self._watch, args=(interval_s,), name="device-watch", daemon=True)
        self._watcher.start()

    def stop_watching(self) -> None:
        """Stop the watcher thread."""
        self._stop_watch.set()
        watcher = self._watcher
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(timeout=5)
        self._watcher = None

    def _watch(self, interval_s: float) -> None:
        while not self._stop_watch.wait(interval_s):
            try:
                self.check_devices()
            except Exception:  # pylint: disable=broad-except
                logging.exception("DeviceManager: device check failed")

    def check_devices(self) -> None:
        """One watcher pass: drop devices that stopped answering, open (or reopen) any that are present.

        A device that was lost and is back is opened afresh, its page is redrawn and its actions reactivated
        (`PageController.restore`), and clients are told with a `devices_changed` event.
        """
        lost = self._drop_lost_devices()
        opened = self._open_present_devices()
        for device_id in lost:
            self._forget_rules(device_id)
            self._announce(device_id)
        for device_id in opened:
            self._restore(device_id)
            self._evaluate_rules(device_id)
            self._announce(device_id)

    def _drop_lost_devices(self) -> list[str]:
        with self._lock:
            known = list(self._devices.values())
        lost: list[str] = []
        for device in known:
            if device.try_validate():  # a dead one also removes and closes itself (see _on_device_closed)
                continue
            logging.warning(
                "DeviceManager: device %s stopped answering; it will be reopened when it is back", device.id
            )
            lost.append(device.device_id)
            with self._lock:
                if self._devices.get(device.device_id) is device:
                    del self._devices[device.device_id]
                    self._forget_swallowed(device.device_id)
            device.close()  # releases the stale handle; errors are logged by Device.close
        return lost

    def _open_present_devices(self) -> list[str]:
        try:
            decks = _HardwareDeviceManager().enumerate()
        except Exception:  # pylint: disable=broad-except
            logging.debug("DeviceManager: could not enumerate devices", exc_info=True)
            return []
        with self._lock:
            open_paths = {d.path for d in self._devices.values()}
        opened: list[str] = []
        for deck in decks:
            try:
                path = deck.id()
                if path in open_paths:
                    continue
                opened.append(self._open_device(deck).device_id)
                self._unopenable.discard(path)
                logging.info("DeviceManager: opened device at %s", path)
            except Exception as exc:  # pylint: disable=broad-except
                self._log_unopenable(deck, exc)
        return opened

    def _log_unopenable(self, deck: StreamDeck, exc: Exception) -> None:
        try:
            path = deck.id()
        except Exception:  # pylint: disable=broad-except
            path = "?"
        level = logging.DEBUG if path in self._unopenable else logging.WARNING
        self._unopenable.add(path)
        logging.log(level, "DeviceManager: could not open device at %s: %s", path, exc)

    def _restore(self, device_id: str) -> None:
        if self._context is None:
            return
        try:
            self._context.page_controller.restore(device_id)
        except Exception:  # pylint: disable=broad-except
            logging.exception("DeviceManager: could not restore device %s after it was opened", device_id)

    def _evaluate_rules(self, device_id: str) -> None:
        engine = self._context.app_rule_engine if self._context is not None else None
        if engine is not None:
            engine.forget_device(device_id)  # a reopened deck shows its restored page, not the rule's target
            engine.evaluate_device(device_id)

    def _forget_rules(self, device_id: str) -> None:
        engine = self._context.app_rule_engine if self._context is not None else None
        if engine is not None:
            engine.forget_device(device_id)

    def _announce(self, device_id: str) -> None:
        if self._context is None:
            return
        try:
            self._context.event_bus.push("devices_changed", json.dumps({"device_id": device_id}))
        except Exception:  # pylint: disable=broad-except
            logging.debug("DeviceManager: could not announce the device change", exc_info=True)

    def destroy(self) -> None:
        """Reset and close all open devices, then stop the animation factory."""
        self.stop_watching()
        with self._lock:
            devices_to_close = list(self._devices.values())
            self._devices.clear()
            self._states.clear()
        for device in devices_to_close:
            device.close()
        if self._factory is not None:
            self._factory.close()
            self._factory = None
        logging.info("DeviceManager: closed all devices")

    def get_device_state(self, device_id: str) -> DeviceState | None:
        """Return the in-memory state tracker for an open device, or None if not open."""
        return self._states.get(device_id)

    def clear_key_state(self, device_id: str, key_index: int) -> None:
        """Remove in-memory and persisted key state for a slot and stop its animation on the open device."""
        state = self._states.get(device_id)
        if state is not None:
            state.clear_key(key_index)
        elif self._state_store is not None:
            self._state_store.delete_key_state(device_id, self._page_resolver(device_id), key_index)
        with self._lock:
            device = self._devices.get(device_id)
        if device is not None:
            device.stop_animation(key_index)

    def delete_stored_key_state(self, device_id: str, page_id: str, key_index: int) -> None:
        """Delete a key's persisted state on a page that is not shown, and forget its in-memory animation."""
        state = self._states.get(device_id)
        if state is not None:
            state.forget_animation(page_id, key_index)
        if self._state_store is not None:
            self._state_store.delete_key_state(device_id, page_id, key_index)

    def blank_key(self, device_id: str, key_index: int) -> None:
        """Forget a slot's cached state and blank its hardware key (no-op if the device cannot be opened)."""
        device = self.open_device(device_id)
        if device is None:
            return
        self.clear_key_state(device_id, key_index)
        device.clear_key_image(key_index)

    def show_page(self, device_id: str, page_id: str) -> bool:
        """Stop the old page's animations, redraw every key from page_id's cached state and restart its animations.

        Keys with no cached image are blanked. Returns False if the device cannot be opened.
        """
        device = self.open_device(device_id)
        state = self._states.get(device_id)
        if device is None or state is None:
            return False
        device.stop_animations()
        state.switch_page(page_id)
        images = dict(state.get_all_images())
        for key_index in range(device.key_count):
            image = images.get(key_index)
            if image is not None:
                device.set_image_bytes(key_index, image)
            else:
                device.clear_key_image(key_index)
        for key_index, spec in state.get_all_animations().items():  # only keys that hold a poster
            device.play_animation(key_index, spec)
        return True

    def set_animation(self, device_id: str, key_index: int, spec: AnimationSpec) -> bool:
        """Animate a key of the open device (its poster drawn) and remember it for its page; False if not animated."""
        device = self.open_device(device_id)
        if device is None:
            logging.debug("DeviceManager.set_animation: device %s not open", device_id)
            return False
        if not device.animation_supported:
            return False
        state = self._states.get(device_id)
        if state is not None:
            state.set_key_animation(key_index, spec)
        device.play_animation(key_index, spec)
        return True

    def playing_animation_id(self, device_id: str, key_index: int) -> str | None:
        """Id of the animation a key of an already open device plays or waits to play, or None; opens nothing."""
        with self._lock:
            device = self._devices.get(device_id)
        return device.animation_id(key_index) if device is not None else None

    def _on_hw_key_event(self, device: Device, key_index: int, pressed: bool) -> None:
        with self._lock:
            current = self._devices.get(device.device_id) is device
        if not current:
            return  # a stale handle of a dropped Device must not deliver presses for its reopened successor
        device_id = device.device_id
        self._call_hook(self._activity_hook, device_id, "activity")
        if self._gate_key_event(device_id, key_index, pressed):
            return
        device.on_key_event(key_index, pressed)
        self._on_key_event_cb(device_id, key_index, pressed)

    def _gate_key_event(self, device_id: str, key_index: int, pressed: bool) -> bool:
        """Return True when the event is swallowed: a press while the overlay shows, or a release whose press was.

        The release of a press that was delivered before the overlay started still goes through (it never wakes), so
        the page and plugins never see a keyDown without its keyUp.
        """
        key = (device_id, key_index)
        with self._lock:
            overlay = device_id in self._overlay
            if pressed:
                swallowed = overlay
                if overlay:
                    self._swallow_release.add(key)
                    self._held.discard(key)
                else:
                    self._swallow_release.discard(key)
                    self._held.add(key)
            else:  # any release ends both records, so a stale entry can never decide a later release
                swallowed = key in self._swallow_release or (overlay and key not in self._held)
                self._swallow_release.discard(key)
                self._held.discard(key)
        if pressed and overlay:
            self._call_hook(self._wake_hook, device_id, "wake")
        return swallowed

    @staticmethod
    def _call_hook(hook: _DeviceHook | None, device_id: str, name: str) -> None:
        if hook is None:
            return
        try:
            hook(device_id)
        except Exception:  # pylint: disable=broad-except
            logging.exception("DeviceManager: %s hook failed for %s", name, device_id)

    def set_activity_hook(self, hook: _DeviceHook | None) -> None:
        """Call hook(device_id) for every key event of a current device, swallowed or not."""
        self._activity_hook = hook

    def set_wake_hook(self, hook: _DeviceHook | None) -> None:
        """Call hook(device_id) on the first press of a key while the overlay is active."""
        self._wake_hook = hook

    def set_open_hook(self, hook: _DeviceHook | None) -> None:
        """Call hook(device_id) after a device is opened or reopened, on the opening thread and outside the lock."""
        self._open_hook = hook

    def add_open_listener(self, listener: _DeviceHook) -> None:
        """Call listener(device_id) after every open or reopen, on the opening thread and outside the lock."""
        with self._lock:
            self._open_listeners.append(listener)

    def add_close_listener(self, listener: _DeviceHook) -> None:
        """Call listener(device_id) when the current device of an id closes, outside the lock."""
        with self._lock:
            self._close_listeners.append(listener)

    def remove_open_listener(self, listener: _DeviceHook) -> None:
        with self._lock:
            if listener in self._open_listeners:
                self._open_listeners.remove(listener)

    def remove_close_listener(self, listener: _DeviceHook) -> None:
        with self._lock:
            if listener in self._close_listeners:
                self._close_listeners.remove(listener)

    def _fire(self, listeners: list[_DeviceHook], device_id: str, name: str) -> None:
        with self._lock:
            snapshot = list(listeners)
        for listener in snapshot:
            self._call_hook(listener, device_id, name)

    def set_overlay(self, device_id: str, active: bool) -> None:
        """Hold back (or release) hardware content writes and key events for a device; caches keep updating."""
        with self._overlay_lock:
            with self._lock:
                if active:
                    self._overlay.add(device_id)
                else:
                    self._overlay.discard(device_id)
                device = self._devices.get(device_id)
            if device is not None:
                device.set_write_gate(active)

    def overlay_active(self, device_id: str) -> bool:
        """Whether the device is currently covered by the overlay."""
        with self._lock:
            return device_id in self._overlay

    def set_brightness(self, device_id: str, percent: int) -> None:
        """Set a device's brightness (0-100), opening it first if needed; a no-op when it cannot be opened."""
        device = self.open_device(device_id)
        if device is not None:
            device.set_brightness(percent)

    def write_overlay(self, device_id: str, images: list[bytes] | None) -> bool:
        """Write one PNG per key (None blanks every key) ignoring the gate; False if the device or a write fails."""
        with self._lock:
            device = self._devices.get(device_id)
        return device is not None and device.write_overlay(images)

    def _forget_swallowed(self, device_id: str) -> None:  # caller holds _lock
        self._swallow_release = {key for key in self._swallow_release if key[0] != device_id}
        self._held = {key for key in self._held if key[0] != device_id}

    def _on_device_closed(self, device: Device) -> None:
        # Identity, not id: a stale Device reporting late must never evict the reopened Device for the same serial.
        with self._lock:
            evicted = self._devices.get(device.device_id) is device
            if evicted:
                del self._devices[device.device_id]
                self._forget_swallowed(device.device_id)
        device.close()  # outside the lock; idempotent, and never joins its own writer thread
        if evicted:
            self._fire(self._close_listeners, device.device_id, "close")

    def opened_device_ids(self) -> list[str]:
        """Ids of the devices that are open right now; touches no hardware."""
        with self._lock:
            return list(self._devices)

    def list_devices(self) -> list[DeviceHardwareInfo]:
        """Return hardware info for all connected Stream Deck devices, even if not yet opened."""
        with self._lock:
            opened_by_path = {d.path: d for d in self._devices.values()}

        hardware_info: list[DeviceHardwareInfo] = []
        deck: StreamDeck
        for deck in _HardwareDeviceManager().enumerate():
            try:
                path = deck.id()
                existing = opened_by_path.get(path)
                if existing and existing.try_validate():
                    hardware_info.append(existing.get_hw_info())
                    continue

                # open the device temporarily and read the information
                if not devices.try_open_device(deck):
                    logging.warning(f"Unable to open device at {deck.id()}")
                    continue

                serial = deck.get_serial_number()
                id = device_id_for_serial(serial)
                tmp = Device(id, serial, deck)
                hardware_info.append(tmp.get_hw_info())
                deck.reset()
                logging.debug("Closing deck %s" % deck.id())
                deck.close()
            except Exception as exc:  # pylint: disable=broad-except
                logging.error("DeviceManager.list_devices failed for deck %s: %s", deck.id(), exc)

        return hardware_info

    def _close_device(self, device: Device) -> bool:
        with self._lock:
            if self._devices.get(device.device_id) is device:
                device.close()
                del self._devices[device.device_id]
                self._forget_swallowed(device.device_id)
                return True
        return False

    def close_device(self, device: str | Device) -> bool:
        """Close and remove a device by instance or ID/path."""
        if isinstance(device, Device):
            return self._close_device(device)
        with self._lock:
            match = next(
                (d for d in self._devices.values() if d.path == device or d.device_id == device),
                None,
            )
        if match:
            return self._close_device(match)
        return False

    def _open_device(self, deck: StreamDeck) -> Device:
        with self._lock:
            existing = next((d for d in self._devices.values() if d.path == deck.id()), None)
            if existing:
                return existing

            # try open the device
            if not devices.try_open_device(deck):
                raise DeviceNotOpened(deck.id())

            serial = deck.get_serial_number()
            id = device_id_for_serial(serial)
            device = Device(id, serial, deck, self._on_device_closed)
            if self._factory is not None:
                device.set_animation_support(self._factory, self._animation_enabled, self._animation_budget)
            deck.set_key_callback(
                lambda _, key_index, pressed, dev=device: self._on_hw_key_event(dev, key_index, pressed)
            )
            self._forget_swallowed(device.device_id)
            if device.device_id in self._overlay:
                device.set_write_gate(True)  # before it is published, so no write can slip in
            self._devices[device.device_id] = device

        state = DeviceState(
            device.device_id, self._state_store, device.get_key_state, self._page_resolver(device.device_id)
        )
        state.restore_from_store()
        self._states[device.device_id] = state

        if self._state_store is not None:
            self._state_store.save_device(device.get_hw_info())

        self._call_hook(self._open_hook, device.device_id, "open")  # outside _lock, on the opener's thread
        self._fire(self._open_listeners, device.device_id, "open")
        return device

    def open_device(self, device_id: str) -> Device | None:
        """Return the Device for this id, opening and caching it lazily on first request."""
        with self._lock:
            match = next(
                (d for d in self._devices.values() if d.path == device_id or d.device_id == device_id),
                None,
            )
        if match:
            return match

        for hw_info in self.list_devices():
            if hw_info.id == device_id:
                deck = next((d for d in _HardwareDeviceManager().enumerate() if d.id() == hw_info.path), None)
                if not deck:
                    logging.warning("DeviceManager: unable to open device at path %s", hw_info.path)
                    return None
                return self._open_device(deck)

        logging.warning("DeviceManager: unable to find device %s", device_id)
        return None

    def set_image_bytes(self, device_id: str, key_index: int, image: bytes) -> None:
        """Push image bytes to the given device's key (ending its animation) and persist the rendered result."""
        device = self.open_device(device_id)
        if device is None:
            logging.debug("DeviceManager.set_image_bytes: device %s not open", device_id)
            return
        state = self._states.get(device_id)
        if state is not None:
            state.set_key_animation(key_index, None)
        device.set_image_bytes(key_index, image)
        rendered = device.get_key_image(key_index)
        if rendered is not None:
            if state is not None:
                state.update_key_image(key_index, rendered)
            elif self._state_store is not None:
                self._state_store.save_key_image(device_id, self._page_resolver(device_id), key_index, rendered)
