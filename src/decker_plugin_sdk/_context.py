"""The per-key context object handed to plugin handlers."""

import base64
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._canvas import Canvas, FrameTicker

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
    _frames: "FrameTicker | None" = field(default=None, repr=False, compare=False)
    _paint: Callable[[], None] = field(default=lambda: None, repr=False, compare=False)
    _canvas: "Canvas | None" = field(default=None, init=False, repr=False, compare=False)
    _canvas_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    @property
    def canvas(self) -> "Canvas":
        """The key's drawing surface (needs Pillow); it starts from the last frame this plugin sent."""
        with self._canvas_lock:
            if self._canvas is None:
                from ._canvas import (  # pylint: disable=import-outside-toplevel
                    Canvas,
                    FrameTarget,
                )

                frames = self._frames
                if frames is None:
                    raise RuntimeError("the canvas works only inside a running plugin")
                loader = partial(frames.last_frame, self.context)
                self._canvas = Canvas(frames, FrameTarget(self.context), self.size, loader)
            return self._canvas

    @property
    def last_paint(self) -> datetime | None:
        """When this plugin last sent a frame for the key, if ever."""
        return self._frames.last_paint(self.context) if self._frames is not None else None

    def queue_paint(self) -> None:
        """Ask for ``on_paint`` to run soon; repeated requests before it runs count once."""
        self._paint()

    def _emit(self, event: str, payload: dict[str, Any] | None = None) -> None:
        self._send(event, self.context, payload or {})

    def set_image(self, data: bytes, format: str = "png") -> None:  # pylint: disable=redefined-builtin
        """Show an image (png, jpeg or svg bytes) on the key."""
        self._emit("set_image", {"image": base64.b64encode(data).decode("ascii"), "format": format})
        self._record(data if format == "png" else None)

    def set_icon(self, ref: str, color: str | None = None) -> None:
        """Show an icon by ``pack:<pack id>/<path>`` or built-in ``fas:play`` ref; SVGs are filled with ``color``."""
        payload: dict[str, Any] = {"icon": ref}
        if color is not None:
            payload["color"] = color
        self._emit("set_image", payload)
        self._record(None)

    def clear_image(self) -> None:
        """Remove any plugin-provided image from the key."""
        self._emit("set_image", {"image": None})
        self._record(None)

    def _record(self, png: bytes | None) -> None:
        if self._frames is not None:
            self._frames.record(self.context, png)

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
