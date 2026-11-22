"""The per-key context object handed to plugin handlers."""

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

Sender = Callable[[str, str | None, dict[str, Any]], None]

MAX_LINE_BYTES = 8 * 1024 * 1024


def encode_line(event: str, context: str | None = None, payload: dict[str, Any] | None = None) -> bytes:
    """Serialise one protocol message to a compact UTF-8 JSON line."""
    data: dict[str, Any] = {"event": event}
    if context is not None:
        data["context"] = context
    if payload:
        data["payload"] = payload
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


@dataclass
class CardContext:
    """One key instance of a card: its identity, current settings and the calls that act on it."""

    context: str
    card: str
    device: str
    key_index: int
    column: int
    row: int
    settings: dict[str, Any] = field(default_factory=dict)
    global_settings: dict[str, Any] = field(default_factory=dict)
    bindings: dict[str, Any] = field(default_factory=dict)
    size: tuple[int, int] = (72, 72)
    state: int = 0
    _send: Sender = field(default=lambda event, context, payload: None, repr=False, compare=False)

    def _emit(self, event: str, payload: dict[str, Any] | None = None) -> None:
        self._send(event, self.context, payload or {})

    def set_image(self, data: bytes, format: str = "png") -> None:  # pylint: disable=redefined-builtin
        """Show an image (png, jpeg or svg bytes) on the key."""
        self._emit("set_image", {"image": base64.b64encode(data).decode("ascii"), "format": format})

    def set_icon(self, ref: str, color: str | None = None) -> None:
        """Show an icon from an installed icon pack (``pack:<pack id>/<path>``); SVGs are filled with ``color``."""
        payload: dict[str, Any] = {"icon": ref}
        if color is not None:
            payload["color"] = color
        self._emit("set_image", payload)

    def clear_image(self) -> None:
        """Remove any plugin-provided image from the key."""
        self._emit("set_image", {"image": None})

    def set_title(self, title: str) -> None:
        """Set the key title."""
        self._emit("set_title", {"title": title})

    def set_state(self, state: int) -> None:
        """Switch the key to another manifest state."""
        self.state = state
        self._emit("set_state", {"state": state})

    def show_alert(self) -> None:
        """Flash the key to signal a failure."""
        self._emit("show_alert")

    def show_ok(self) -> None:
        """Flash the key to signal success."""
        self._emit("show_ok")

    def set_settings(self, settings: dict[str, Any]) -> None:
        """Store settings for this key; send only the keys you changed (others are kept)."""
        self.settings = {**self.settings, **settings}
        self._emit("set_settings", {"settings": settings})

    def set_global_settings(self, settings: dict[str, Any]) -> None:
        """Replace the plugin's global settings."""
        self.global_settings = dict(settings)
        self._emit("set_global_settings", {"settings": settings})

    def switch_page(self, to: str = "next", wrap: bool = False) -> None:
        """Switch this key's device to the ``next``/``previous``/``first``/``last`` page; ``wrap`` loops at the ends."""
        self._emit("switch_page", {"to": to, "wrap": wrap})

    def go_to_page(self, page_id: str) -> None:
        """Switch this key's device to the page with this id (a ``page`` setting value's ``page_id``)."""
        self._emit("switch_page", {"page_id": page_id})

    def switch_profile(self, to: str = "next", wrap: bool = False) -> None:
        """Switch this key's device to the ``next``/``previous``/``first``/``last`` profile; ``wrap`` loops at the ends."""
        self._emit("switch_profile", {"to": to, "wrap": wrap})

    def go_to_profile(self, profile_id: int) -> None:
        """Switch this key's device to the profile with this id (a ``profile`` setting value's ``profile_id``)."""
        self._emit("switch_profile", {"profile_id": profile_id})

    def open_url(self, url: str) -> None:
        """Ask the server to open an http(s) URL."""
        self._emit("open_url", {"url": url})

    def log(self, message: str, level: str = "info") -> None:
        """Send a log line to the server."""
        self._emit("log", {"level": level, "message": message})
