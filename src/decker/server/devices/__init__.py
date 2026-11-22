"""Stream Deck device abstraction and per-key gesture tracking."""

import io
import logging
import threading
import time
from collections.abc import Callable

from StreamDeck.Devices.StreamDeck import StreamDeck
from StreamDeck.ImageHelpers import PILHelper

from decker.animation.controller import LevelController, ladder_for
from decker.animation.encode import KeyFormat
from decker.animation.factory import AnimationFactory
from decker.animation.load import LoadMonitor
from decker.animation.model import AnimationSpec
from decker.animation.writer import AdaptiveConfig, KeyWriter
from decker.asset.renderer import AssetRenderer
from decker.common.error import DeckNotOpenedError
from decker.common.model import DeviceHardwareInfo, KeyInfo, KeyPressState

DeviceCloseCallback = Callable[["Device"], None]  # called with the device when it is detected as closed

_HELD_THRESHOLD_S: float = 0.5
_MULTI_TAP_WINDOW_S: float = 0.35


_UNKNOWN: object = object()  # what a key shows before Decker has drawn on it

_DEFAULT_REPORT_PAYLOAD: int = 1016  # HID image report payload of current decks, for a deck that does not say
# Device.close waits at most this long for a write already under way (a lost deck can block one).
_WRITER_CLOSE_TIMEOUT_S: float = 0.5
# Device.stop_animations waits at most this long for a frame write already under way.
_STOP_WAIT_S: float = 1.0


class _KeyTracker:
    """Per-key state machine tracking press gestures and the last rendered image."""

    def __init__(self) -> None:
        self.state: KeyPressState = KeyPressState.UP
        self.image: bytes | None = None  # cached PNG of the last image pushed to this key
        self.source: bytes | None = None  # the bytes that PNG was rendered from
        self.shown: object = _UNKNOWN  # what the hardware shows: the PNG sent, None when blanked, or _UNKNOWN
        self._tap_count: int = 0
        self._last_up: float = 0.0
        self._held_timer: threading.Timer | None = None
        self._latch_timer: threading.Timer | None = None

    def on_down(self) -> None:
        """Handle a key-down hardware event."""
        self._cancel_latch()
        self._cancel_held()
        if time.monotonic() - self._last_up > _MULTI_TAP_WINDOW_S:
            self._tap_count = 0
        self.state = KeyPressState.DOWN
        self._held_timer = threading.Timer(_HELD_THRESHOLD_S, self._fire_held)
        self._held_timer.daemon = True
        self._held_timer.start()

    def on_up(self) -> None:
        """Handle a key-up hardware event, updating gesture state."""
        self._cancel_held()
        if self.state == KeyPressState.HELD:
            self._tap_count = 0
            self.state = KeyPressState.UP
            return
        self._last_up = time.monotonic()
        self._tap_count += 1
        if self._tap_count >= 3:
            self.state = KeyPressState.TRIPLE_PRESSED
            self._start_latch()
        elif self._tap_count >= 2:
            self.state = KeyPressState.DOUBLE_PRESSED
            self._start_latch()
        else:
            self.state = KeyPressState.UP
            # tap_count intentionally kept at 1 so a second press within the
            # window can still be detected as a double-press

    def _fire_held(self) -> None:
        self.state = KeyPressState.HELD
        self._tap_count = 0

    def _expire_latch(self) -> None:
        if self.state in (KeyPressState.DOUBLE_PRESSED, KeyPressState.TRIPLE_PRESSED):
            self.state = KeyPressState.UP
        self._tap_count = 0

    def _start_latch(self) -> None:
        self._latch_timer = threading.Timer(_MULTI_TAP_WINDOW_S, self._expire_latch)
        self._latch_timer.daemon = True
        self._latch_timer.start()

    def _cancel_held(self) -> None:
        if self._held_timer:
            self._held_timer.cancel()
            self._held_timer = None

    def _cancel_latch(self) -> None:
        if self._latch_timer:
            self._latch_timer.cancel()
            self._latch_timer = None


def try_open_device(device: StreamDeck) -> bool:
    """Attempt to open a StreamDeck device if not already open. Returns True if open."""
    if not device.is_open():
        try:
            logging.debug("Opening deck %s", device.id())
            device.open()
        except Exception:  # pylint: disable=broad-except
            pass
    return device.is_open()


class Device:  # pylint: disable=too-many-instance-attributes,too-many-public-methods
    """Maintains an open connection to a single Stream Deck and routes image pushes."""

    def __init__(
        self, id: str, serial: str, deck: StreamDeck, notify_on_closed: DeviceCloseCallback | None = None
    ) -> None:
        self._device: StreamDeck = deck
        self.serial: str = serial
        self.id: str = id
        self._renderer = AssetRenderer()
        self._notify_on_closed = notify_on_closed
        self._key_trackers: dict[int, _KeyTracker] = {}
        self._layout: tuple[int, int] | None = None
        self._writer_lock: threading.Lock = threading.Lock()
        self._writer: KeyWriter | None = None
        self._adaptive: AdaptiveConfig | None = None  # set when animation is enabled; writes then use the writer
        self._closed: bool = False
        self._gated: bool = False  # while set, content writes only update the caches; the hardware is left alone
        # Held across "check the gate, then submit", so no write slips onto the hardware after the gate closes.
        self._gate_lock: threading.Lock = threading.Lock()

    @property
    def key_count(self) -> int:
        """How many keys the device has."""
        return int(self._device.key_count())

    @property
    def key_size(self) -> tuple[int, int]:
        """Width and height of a key image in pixels."""
        return int(self._device.KEY_PIXEL_WIDTH), int(self._device.KEY_PIXEL_HEIGHT)

    def key_position(self, key_index: int) -> tuple[int, int] | None:
        """``(column, row)`` of a key from the cached layout (no hardware query), or None outside the layout."""
        if self._layout is None:
            self._layout = tuple(self._device.key_layout())
        rows, cols = self._layout
        if not 0 <= key_index < rows * cols:
            return None
        return key_index % cols, key_index // cols

    def validate(self) -> None:
        """Raise DeckNotOpenedError if the device can no longer be reached."""
        if not self._try_open():
            raise DeckNotOpenedError(self.path)
        try:
            self._device.get_serial_number()
        except Exception as exc:
            self._report_closed()
            raise DeckNotOpenedError(self.path) from exc

    def try_validate(self) -> bool:
        """Return True if the device is reachable, False otherwise (always False, untouched, once closed)."""
        if self._closed:
            return False
        try:
            self._device.get_serial_number()
            return True
        except Exception:  # pylint: disable=broad-except
            self._report_closed()
            return False

    def get_hw_info(self) -> DeviceHardwareInfo:
        """Return hardware metadata for this device."""
        rows, cols = self._device.key_layout()
        fmt = self._device.key_image_format()
        w, h = fmt["size"]
        keys = [
            KeyInfo(row=r, col=c, pixel_width=w, pixel_height=h, type="key") for r in range(rows) for c in range(cols)
        ]
        try:
            firmware: str | None = self._device.get_firmware_version()
        except Exception:  # pylint: disable=broad-except
            firmware = None
        return DeviceHardwareInfo(
            id=self.id,
            type=self._device.deck_type(),
            vendor_id=self._device.vendor_id(),
            product_id=self._device.product_id(),
            num_keys=self._device.key_count(),
            keys=keys,
            serial=self.serial,
            path=self.path,
            firmware=firmware,
        )

    def on_key_event(self, key_index: int, pressed: bool) -> None:
        """Update the key state machine from a hardware callback."""
        tracker = self._key_trackers.setdefault(key_index, _KeyTracker())
        if pressed:
            tracker.on_down()
        else:
            tracker.on_up()

    def get_key_state(self, key_index: int) -> KeyPressState:
        """Return the current gesture state for a key."""
        tracker = self._key_trackers.get(key_index)
        return tracker.state if tracker else KeyPressState.UP

    def get_key_image(self, key_index: int) -> bytes | None:
        """Return cached PNG bytes of the last image pushed to this key, or None."""
        tracker = self._key_trackers.get(key_index)
        return tracker.image if tracker else None

    @property
    def path(self) -> str:
        """USB path used as a stable physical identifier."""
        return self._device.id()

    @property
    def device_id(self) -> str:
        """Stable 8-char CRC-32 hex ID derived from the serial number."""
        return self.id

    def _try_open(self) -> bool:
        # A closed Device (also one closed by the report inside try_validate) must never open its deck again:
        # nothing would release that handle, and its key callback would still be wired.
        if self._closed:
            return False
        if not self._device.is_open() or not self.try_validate():
            if self._closed:
                return False
            try_open_device(self._device)

        is_open = self._device.is_open() and self.try_validate()
        if not is_open:
            self._report_closed()
        return is_open

    def _report_closed(self) -> None:
        if self._notify_on_closed and not self._closed:
            self._notify_on_closed(self)

    def set_animation_support(self, factory: AnimationFactory, enabled: bool, budget: float) -> None:
        """Let this device animate keys from factory within budget (busy fraction); disabled keeps posters only."""
        if not enabled:
            return
        fmt = self._device.key_image_format()
        payload = getattr(self._device, "IMAGE_REPORT_PAYLOAD_LENGTH", _DEFAULT_REPORT_PAYLOAD)
        key_format = KeyFormat.from_deck(fmt, int(payload))
        controller = LevelController(ladder_for(key_format.image_format.upper() == "BMP"), budget)
        with self._writer_lock:
            if self._writer is None:
                self._adaptive = AdaptiveConfig(controller, LoadMonitor(), factory, key_format)

    @property
    def animation_supported(self) -> bool:
        """Whether this device can play animations (animation enabled and the device open)."""
        with self._writer_lock:
            return self._adaptive is not None and not self._closed

    def set_write_gate(self, gated: bool) -> None:
        """Hold back (True) or allow (False) content writes; closing the gate leaves every key to be redrawn."""
        with self._gate_lock:  # waits for a write that passed the gate check; later ones see the new value
            self._gated = gated
            for tracker in list(self._key_trackers.values()):
                tracker.shown = _UNKNOWN  # whatever the hardware shows is no longer what was last drawn here
        if gated:
            self.stop_animations()

    def set_brightness(self, percent: int) -> None:
        """Set the deck brightness (0-100), reopening a closed deck like other writes."""
        try:
            self._try_open()
            if self._closed:
                raise DeckNotOpenedError(self.path)
            self._device.set_brightness(max(0, min(100, int(percent))))
        except Exception as exc:  # pylint: disable=broad-except
            logging.error("Device.set_brightness failed for %s: %s", self.id, exc)

    def play_animation(self, key_index: int, spec: AnimationSpec) -> None:
        """Animate the key with spec once its frames are built; the poster already on the key stays until then."""
        with self._gate_lock:
            if self._gated:
                return  # the spec is cached by the caller; show_page restarts it once the gate opens
            writer = self._key_writer()
            if writer is None:
                return
            self._key_trackers.setdefault(key_index, _KeyTracker()).shown = _UNKNOWN  # frames replace the poster
            writer.play_spec(key_index, spec, poster_written=True)

    def stop_animations(self) -> None:
        """Stop every key's animation (each keeps its last frame), waiting up to 1 s for a frame write under way.

        A frame write still blocked after that wait may land after this returns.
        """
        with self._writer_lock:
            writer = self._writer
        if writer is not None:
            writer.stop_all(wait=_STOP_WAIT_S)

    def animation_id(self, key_index: int) -> str | None:
        """Id of the animation the key plays or waits to play on this device, or None."""
        with self._writer_lock:
            writer = self._writer
        return writer.animation_id(key_index) if writer is not None else None

    def stop_animation(self, key_index: int) -> None:
        """Stop one key's animation, leaving its last frame on the key."""
        with self._writer_lock:
            writer = self._writer
        if writer is not None:
            writer.stop(key_index)

    def _key_writer(self) -> KeyWriter | None:
        with self._writer_lock:
            if self._adaptive is None or self._closed:
                return None
            if self._writer is None:
                # Only set_key_image runs on the writer: frame writes must add no HID reads to the measured busy time.
                self._writer = KeyWriter(self._device.set_key_image, adaptive=self._adaptive)
                self._writer.start()
            return self._writer

    def _write_key(self, key_index: int, native: bytes | None) -> None:
        self._try_open()
        if self._closed:
            raise DeckNotOpenedError(self.path)
        self._device.set_key_image(key_index, native)

    def _write_static(self, key_index: int, native: bytes | None) -> bool:
        writer = self._key_writer()
        if writer is None:
            self._write_key(key_index, native)
            return True
        self._try_open()  # as before a direct write: reopen a deck that was closed, or report it lost
        # False: not confirmed in time; the write may still land, so the caller must not trust what the key shows.
        return writer.submit_static(key_index, native)

    def _render(self, image: bytes) -> tuple[bytes | None, bytes]:
        """Return (PNG of the scaled key image or None when the bytes are not renderable, bytes for the deck)."""
        pil_image = self._renderer.render_bytes(image, self._device.KEY_PIXEL_WIDTH, self._device.KEY_PIXEL_HEIGHT)
        if pil_image is None:
            return None, image
        scaled = PILHelper.create_scaled_key_image(self._device, pil_image)
        buf = io.BytesIO()
        scaled.save(buf, format="PNG")
        return buf.getvalue(), PILHelper.to_native_key_format(self._device, scaled)

    def set_image_bytes(self, key_index: int, image: bytes) -> None:
        """Render image bytes (SVG/PNG/JPEG) and push to the hardware key."""
        try:
            tracker = self._key_trackers.setdefault(key_index, _KeyTracker())
            if image == tracker.source and tracker.image is not None and tracker.shown == tracker.image:
                return  # the key already shows exactly this: skip rendering it again
            png, native = self._render(image)
            tracker.source = image if png is not None else None
            if png is not None:
                tracker.image = png
            shown: object = _UNKNOWN if png is None else png
            with self._gate_lock:
                if self._gated or (png is not None and shown == tracker.shown):
                    return  # held back, or the key already shows exactly this
                tracker.shown = _UNKNOWN
                if self._write_static(key_index, native):
                    tracker.shown = shown
        except Exception as exc:  # pylint: disable=broad-except
            logging.error("Device.set_image_bytes failed for %s[%d]: %s", self.id, key_index, exc)

    def write_overlay(self, images: list[bytes] | None) -> bool:
        """Write one image per key (None blanks all) ignoring the gate; True only if every write was confirmed."""
        try:
            confirmed = True
            for key_index in range(self.key_count):
                tracker = self._key_trackers.setdefault(key_index, _KeyTracker())
                native = None if images is None else self._render(images[key_index])[1]
                tracker.shown = _UNKNOWN  # an overlay is not page content: the page must be redrawn over it
                confirmed = self._write_static(key_index, native) and confirmed
            return confirmed
        except Exception as exc:  # pylint: disable=broad-except
            logging.error("Device.write_overlay failed for %s: %s", self.id, exc)
            return False

    def clear_key_image(self, key_index: int) -> None:
        """Blank the hardware key and forget its cached rendered image."""
        try:
            tracker = self._key_trackers.setdefault(key_index, _KeyTracker())
            tracker.image = None
            tracker.source = None
            with self._gate_lock:
                if self._gated or tracker.shown is None:
                    return  # held back, or already blank
                tracker.shown = _UNKNOWN
                if self._write_static(key_index, None):
                    tracker.shown = None
        except Exception as exc:  # pylint: disable=broad-except
            logging.error("Device.clear_key_image failed for %s[%d]: %s", self.id, key_index, exc)

    def close(self) -> None:
        """Stop the key writer, then reset and close the underlying hardware connection; later calls do nothing."""
        with self._writer_lock:
            if self._closed:
                return
            self._closed = True
            writer, self._writer = self._writer, None
        if writer is not None:
            writer.close(timeout=_WRITER_CLOSE_TIMEOUT_S)
        try:
            logging.debug("Closing device %s", self.id)
            self._device.reset()
            self._device.close()
        except Exception:  # pylint: disable=broad-except
            logging.warning("Failed to close device %s cleanly", self.id)
