"""Proxies for the card an action was called from."""

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from ._canvas import Canvas, FrameTarget, FrameTicker
from ._context import CardContext, Sender

if TYPE_CHECKING:
    from ._runtime import DeckerPlugin


class CardProxy:
    """The card an action was called from; usable only while the call is in flight."""

    def __init__(
        self, payload: Mapping[str, Any], request: str, send: Sender, frames: FrameTicker | None = None
    ) -> None:
        key: Mapping[str, Any] = payload.get("key") or {}
        size = payload.get("size") or (72, 72)
        last_paint = payload.get("last_paint")
        self._context: str = payload["context"]
        self._card_id: str = payload.get("card", "")
        self._plugin_id: str = payload.get("plugin", "")
        self._device: str = payload.get("device", "")
        self._key_index: int = key.get("index", 0)
        self._column: int = key.get("column", 0)
        self._row: int = key.get("row", 0)
        self._size: tuple[int, int] = (int(size[0]), int(size[1]))
        self._settings: Mapping[str, Any] = MappingProxyType(dict(payload.get("settings") or {}))
        self._last_paint: datetime | None = datetime.fromisoformat(last_paint) if last_paint else None
        self._request: str = request
        self._send: Sender = send
        self._frames: FrameTicker | None = frames
        self._canvas: Canvas | None = None
        self._closed: bool = False

    @property
    def context(self) -> str:
        """The card's context id."""
        return self._context

    @property
    def card_id(self) -> str:
        """The card's id."""
        return self._card_id

    @property
    def plugin_id(self) -> str:
        """The id of the plugin that owns the card."""
        return self._plugin_id

    @property
    def device(self) -> str:
        """The device the card is on."""
        return self._device

    @property
    def key_index(self) -> int:
        """The key index on the device."""
        return self._key_index

    @property
    def column(self) -> int:
        """The key column."""
        return self._column

    @property
    def row(self) -> int:
        """The key row."""
        return self._row

    @property
    def size(self) -> tuple[int, int]:
        """The key size in pixels."""
        return self._size

    @property
    def settings(self) -> Mapping[str, Any]:
        """A read-only copy of the card's settings."""
        return self._settings

    @property
    def last_paint(self) -> datetime | None:
        """When the card was last painted by a plugin, if ever."""
        return self._last_paint

    @property
    def canvas(self) -> Canvas:
        """The card's drawing surface (needs Pillow); the first access fetches the key's current image."""
        self._check_open()
        if self._canvas is None:
            frames = self._frames
            if frames is None:
                raise RuntimeError("the canvas works only inside a running plugin")
            context, request = self._context, self._request
            self._canvas = Canvas(
                frames, FrameTarget(context, request), self._size, lambda: frames.fetch(context, request)
            )
        return self._canvas

    def queue_paint(self) -> None:
        """Send the canvas buffer again; the card belongs to another plugin, so it has no ``on_paint``."""
        self._check_open()
        if self._canvas is not None:
            self._canvas.queue_paint()

    def close(self) -> None:
        """End access to the card; later calls raise."""
        self._closed = True
        self._canvas = None

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("card access ended with the action call")

    def _emit(self, event: str, payload: dict[str, Any] | None = None) -> None:
        self._check_open()
        self._send(event, self._context, {**(payload or {}), "request": self._request})

    def show_ok(self) -> None:
        """Flash the key to signal success."""
        self._emit("show_ok")

    def show_alert(self) -> None:
        """Flash the key to signal a failure."""
        self._emit("show_alert")

    def switch_page(self, to: str = "next", wrap: bool = False) -> None:
        """Switch the device to the ``next``/``previous``/``first``/``last`` page."""
        self._emit("switch_page", {"to": to, "wrap": wrap})

    def go_to_page(self, page_id: str) -> None:
        """Switch the device to the page with this id."""
        self._emit("switch_page", {"page_id": page_id})

    def switch_profile(self, to: str = "next", wrap: bool = False) -> None:
        """Switch the device to the ``next``/``previous``/``first``/``last`` profile."""
        self._emit("switch_profile", {"to": to, "wrap": wrap})

    def go_to_profile(self, profile_id: int) -> None:
        """Switch the device to the profile with this id."""
        self._emit("switch_profile", {"profile_id": profile_id})


class OwnCardProxy(CardProxy):
    """The plugin's own card: calls go through the live card context."""

    def __init__(self, payload: Mapping[str, Any], ctx: CardContext, instance: "DeckerPlugin") -> None:
        super().__init__(payload, "", ctx._send)  # pylint: disable=protected-access
        self._ctx: CardContext = ctx
        self._instance: "DeckerPlugin" = instance

    @property
    def instance(self) -> "DeckerPlugin":
        """The plugin instance that owns the card."""
        return self._instance

    @property
    def settings(self) -> Mapping[str, Any]:
        """A read-only copy of the card's current settings."""
        return MappingProxyType(dict(self._ctx.settings))

    @property
    def last_paint(self) -> datetime | None:
        """When the card was last painted by this plugin, if ever."""
        return super().last_paint or self._ctx.last_paint

    @property
    def canvas(self) -> Canvas:
        """The card's drawing surface (needs Pillow)."""
        return self._ctx.canvas

    def queue_paint(self) -> None:
        """Ask for ``on_paint`` to run soon; repeated requests before it runs count once."""
        self._ctx.queue_paint()

    def set_settings(self, settings: dict[str, Any]) -> None:
        """Store settings for the card; send only the keys you changed."""
        self._ctx.set_settings(settings)

    def close(self) -> None:
        """Do nothing; the plugin's own card stays usable."""

    def show_ok(self) -> None:
        """Flash the key to signal success."""
        self._ctx.show_ok()

    def show_alert(self) -> None:
        """Flash the key to signal a failure."""
        self._ctx.show_alert()

    def switch_page(self, to: str = "next", wrap: bool = False) -> None:
        """Switch the device to the ``next``/``previous``/``first``/``last`` page."""
        self._ctx.switch_page(to, wrap)

    def go_to_page(self, page_id: str) -> None:
        """Switch the device to the page with this id."""
        self._ctx.go_to_page(page_id)

    def switch_profile(self, to: str = "next", wrap: bool = False) -> None:
        """Switch the device to the ``next``/``previous``/``first``/``last`` profile."""
        self._ctx.switch_profile(to, wrap)

    def go_to_profile(self, profile_id: int) -> None:
        """Switch the device to the profile with this id."""
        self._ctx.go_to_profile(profile_id)
