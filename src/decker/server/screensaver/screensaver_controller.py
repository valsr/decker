"""Validates screensaver edits and previews, applies them through the engine and announces changes to clients."""

import io
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

from PIL import Image

from decker.common.manager import Manager
from decker.common.model import DeviceHardwareInfo, ScreensaverSettings
from decker.server.screensaver.compose import CanvasLayout, canvas_layout, preview_image
from decker.server.screensaver.settings import settings_from_json
from decker.server.screensaver.source import (
    GalleryCursor,
    ScreensaverImageError,
    load_image,
)

if TYPE_CHECKING:
    from decker.server.screensaver.screensaver_engine import ScreensaverEngine
    from decker.server.screensaver.screensaver_store import ScreensaverStore
    from decker.server.server_context import ServerContext

SCREENSAVER_CHANGED_EVENT = "screensaver_changed"
SCREENSAVER_SETTINGS_CHANGED_EVENT = "screensaver_settings_changed"
_REMOTE_PATHS_DENIED = "image and folder paths can only be changed from the server's machine"
_FALLBACK_LAYOUT = (1, 1, 72)  # rows, cols, key pixels for a device whose layout is unknown
_PREVIEW_BACKGROUND = (16, 16, 16, 255)
_LOGGER = logging.getLogger(__name__)


def _parse(settings_json: Any) -> ScreensaverSettings:
    if not isinstance(settings_json, str):
        raise ValueError("settings must be a JSON string")
    return settings_from_json(settings_json)


class ScreensaverController(Manager):
    """Gatekeeper for screensaver edits: unknown devices and invalid or forbidden settings are rejected."""

    def __init__(self) -> None:
        self._context: "ServerContext | None" = None
        self._brightness_listeners: list[Callable[[str, int], None]] = []

    def create(self, context: "ServerContext") -> None:
        """Bind the server context."""
        self._context = context

    def destroy(self) -> None:
        """Release the server context."""
        self._context = None

    @property
    def _ctx(self) -> "ServerContext":
        assert self._context is not None, "ScreensaverController.create() must be called before use"
        return self._context

    @property
    def _store(self) -> "ScreensaverStore":
        store = self._ctx.screensaver_store
        assert store is not None, "ScreensaverController requires a ScreensaverStore"
        return store

    @property
    def _engine(self) -> "ScreensaverEngine":
        engine = self._ctx.screensaver_engine
        assert engine is not None, "ScreensaverController requires a ScreensaverEngine"
        return engine

    def get(self, device_id: str) -> dict[str, Any]:
        """The device's settings plus its runtime `active` flag and `last_error`."""
        self._require_known(device_id)
        status = self._engine.status(device_id)
        return {**asdict(self._stored(device_id)), "active": status.active, "last_error": status.last_error}

    def set(self, device_id: str, settings_json: str, *, loopback: bool) -> None:
        """Validate and store settings and apply them; never activates the screensaver."""
        self._require_known(device_id)
        settings = _parse(settings_json)
        self._check_paths(device_id, settings, loopback)
        self._commit(device_id, settings)

    def brightness(self, device_id: str) -> int:
        """The device's normal brightness in percent."""
        self._require_known(device_id)
        return self._stored(device_id).brightness

    def set_brightness(self, device_id: str, percent: int) -> int:
        """Store a normal brightness clamped to 1-100, apply it and return the stored value."""
        self._require_known(device_id)
        if isinstance(percent, bool) or not isinstance(percent, int):
            raise ValueError("percent must be an integer")
        value = max(1, min(100, percent))
        self._commit(device_id, replace(self._stored(device_id), brightness=value))
        return value

    def add_brightness_listener(self, listener: Callable[[str, int], None]) -> None:
        """Call ``listener(device_id, percent)`` after an edit changed a device's normal brightness."""
        self._brightness_listeners.append(listener)

    def _commit(self, device_id: str, settings: ScreensaverSettings) -> None:
        previous = self._stored(device_id).brightness
        self._store.set(device_id, settings)
        self._engine.apply_settings(device_id)
        self._ctx.event_bus.push(SCREENSAVER_SETTINGS_CHANGED_EVENT, json.dumps({"device_id": device_id}))
        if settings.brightness != previous:
            for listener in list(self._brightness_listeners):
                try:
                    listener(device_id, settings.brightness)
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("brightness listener failed for %s", device_id)

    def activate(self, device_id: str) -> bool:
        """Show the screensaver now; False when the device is not open or the activation was cancelled."""
        self._require_known(device_id)
        return self._engine.activate(device_id)

    def deactivate(self, device_id: str) -> bool:
        """Wake the device; False when it was not showing the screensaver."""
        self._require_known(device_id)
        return self._engine.deactivate(device_id)

    def preview(self, device_id: str, settings_json: str, *, loopback: bool) -> bytes:
        """A PNG of the draft settings composed at the device's canvas layout; a dark canvas without an image."""
        self._require_known(device_id)
        settings = _parse(settings_json)
        self._check_paths(device_id, settings, loopback)
        layout = self._layout(device_id, settings)
        image = self._source_image(settings, layout)
        if image is None:
            image = Image.new("RGBA", (layout.width, layout.height), _PREVIEW_BACKGROUND)
        else:
            image = preview_image(image, layout, settings.scaling)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def notify_changed(self, device_id: str, active: bool) -> None:
        """Announce that a device's screensaver started or stopped; carries no error text."""
        self._ctx.event_bus.push(SCREENSAVER_CHANGED_EVENT, json.dumps({"device_id": device_id, "active": active}))

    def _check_paths(self, device_id: str, settings: ScreensaverSettings, loopback: bool) -> None:
        current = self._stored(device_id)
        if not loopback and (settings.image_path != current.image_path or settings.folder_path != current.folder_path):
            raise PermissionError(_REMOTE_PATHS_DENIED)

    def _stored(self, device_id: str) -> ScreensaverSettings:
        try:
            return self._store.get(device_id)
        except ValueError as exc:
            _LOGGER.warning("screensaver settings of %s are invalid, using defaults: %s", device_id, exc)
            return ScreensaverSettings()

    def _require_known(self, device_id: str) -> None:
        manager = self._ctx.device_manager
        known = isinstance(device_id, str) and (
            device_id in manager.opened_device_ids()
            or self._ctx.device_state_store.get_device(device_id) is not None
            or any(info.id == device_id for info in manager.list_devices())
        )
        if not known:
            raise ValueError("unknown device")

    def _layout(self, device_id: str, settings: ScreensaverSettings) -> CanvasLayout:
        info = self._hw_info(device_id)
        keys = [key for key in info.keys if key.type == "key" and key.pixel_width > 0] if info is not None else []
        rows, cols, key_px = _FALLBACK_LAYOUT
        if keys:
            rows, cols, key_px = max(k.row for k in keys) + 1, max(k.col for k in keys) + 1, keys[0].pixel_width
        return canvas_layout(rows, cols, key_px, settings.bezel, settings.key_mm, settings.gap_mm)

    def _hw_info(self, device_id: str) -> DeviceHardwareInfo | None:
        info = self._ctx.device_state_store.get_device(device_id)
        if info is None:
            info = next((i for i in self._ctx.device_manager.list_devices() if i.id == device_id), None)
        return info

    @staticmethod
    def _source_image(settings: ScreensaverSettings, layout: CanvasLayout) -> Image.Image | None:
        path = ""
        if settings.content == "image":
            path = settings.image_path
        elif settings.content == "gallery" and settings.folder_path:
            path = GalleryCursor().next_path(settings.folder_path) or ""
        if not path:
            return None
        try:
            return load_image(path, layout.width, layout.height)
        except ScreensaverImageError as exc:
            raise ValueError(str(exc)) from exc
