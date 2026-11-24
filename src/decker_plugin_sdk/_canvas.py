"""Double-buffered key canvas and the frame ticker that sends its snapshots."""

import base64
import io
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ._context import Sender

if TYPE_CHECKING:
    from PIL.Image import Image

FRAME_INTERVAL = 1 / 30
# The server drops messages above 100 msg/s (burst 200) and kills a plugin that keeps flooding; 2 frames per
# 1/30 s tick is about 60 frames/s, which leaves room for every other message. Keys over budget stay dirty.
MAX_FRAMES_PER_TICK = 2
IMAGE_TIMEOUT = 5.0
NO_PILLOW = "the canvas needs Pillow: add pillow to your plugin's requirements"


@dataclass(frozen=True)
class FrameTarget:
    """A key a canvas paints; ``request`` is the in-flight call id for a card of another plugin."""

    context: str
    request: str | None = None


def _warn(text: str) -> None:
    print(f"decker_plugin_sdk: {text}", file=sys.stderr, flush=True)


class _ImageWait:
    def __init__(self) -> None:
        self.done: threading.Event = threading.Event()
        self.payload: dict[str, Any] = {}


class FrameTicker:
    """Sends the latest snapshot of every dirty key from one daemon thread, about 30 times a second."""

    def __init__(
        self,
        send: Sender,
        interval: float = FRAME_INTERVAL,
        blocking: Callable[[], AbstractContextManager[None]] = nullcontext,
    ) -> None:
        self._send: Sender = send
        self._blocking: Callable[[], AbstractContextManager[None]] = blocking
        self._interval: float = interval
        self._lock: threading.Lock = threading.Lock()
        self._send_lock: threading.Lock = threading.Lock()
        self._dirty: dict[FrameTarget, "Image"] = {}
        self._frames: dict[str, bytes] = {}
        self._painted: dict[str, datetime] = {}
        self._waits: dict[str, _ImageWait] = {}
        self._stop: threading.Event = threading.Event()
        self._thread: threading.Thread | None = None

    def mark_dirty(self, target: FrameTarget, snapshot: "Image") -> None:
        """Queue a finished snapshot; a newer one for the same key replaces it."""
        with self._lock:
            if self._stop.is_set():
                return
            self._dirty[target] = snapshot
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="decker-frame-ticker", daemon=True)
                self._thread.start()

    def tick(self) -> None:
        """Send up to MAX_FRAMES_PER_TICK dirty snapshots: cards of other plugins first, then the oldest dirty."""
        with self._send_lock:
            with self._lock:
                ordered = sorted(self._dirty, key=lambda target: target.request is None)  # stable: oldest first
                items = {target: self._dirty.pop(target) for target in ordered[:MAX_FRAMES_PER_TICK]}
            self._send_items(items)

    def flush(self, request: str) -> None:
        """Send the dirty snapshots of the card of the call ``request`` now."""
        with self._send_lock:
            with self._lock:
                items = {t: s for t, s in self._dirty.items() if t.request == request}
                for target in items:
                    del self._dirty[target]
            self._send_items(items)

    def stop(self) -> None:
        """Stop the thread; later snapshots are ignored."""
        with self._lock:
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)

    def last_frame(self, context: str) -> bytes | None:
        """The PNG of the last frame sent for the context, if any."""
        with self._lock:
            return self._frames.get(context)

    def last_paint(self, context: str) -> datetime | None:
        """When the last frame for the context was sent, if ever."""
        with self._lock:
            return self._painted.get(context)

    def fetch(self, context: str, call: str | None) -> bytes | None:
        """Ask the server for the key's current plugin image (PNG); None on failure or timeout."""
        request = uuid.uuid4().hex
        wait = _ImageWait()
        with self._lock:
            self._waits[request] = wait
        payload: dict[str, Any] = {"request": request, "context": context}
        if call is not None:
            payload["call"] = call
        try:
            self._send("get_image", None, payload)
            with self._blocking():
                arrived = wait.done.wait(IMAGE_TIMEOUT)
        finally:
            with self._lock:
                self._waits.pop(request, None)
        image = wait.payload.get("image") if arrived else None
        try:
            return base64.b64decode(image, validate=True) if isinstance(image, str) else None
        except ValueError:
            return None

    def deliver(self, payload: dict[str, Any]) -> None:
        """Hand an image_result to the waiting fetch; unknown requests are ignored."""
        with self._lock:
            wait = self._waits.get(str(payload.get("request")))
        if wait is not None:
            wait.payload = payload
            wait.done.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.tick()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                _warn(f"frame tick failed: {type(exc).__name__}: {exc}")

    def _send_items(self, items: dict[FrameTarget, "Image"]) -> None:
        for target, snapshot in items.items():
            try:
                buffer = io.BytesIO()
                snapshot.save(buffer, format="PNG")
                png = buffer.getvalue()
                payload: dict[str, Any] = {"image": base64.b64encode(png).decode("ascii"), "format": "png"}
                if target.request is not None:
                    payload["request"] = target.request
                self._send("set_image", target.context, payload)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                _warn(f"frame for {target.context[:40]} not sent: {type(exc).__name__}: {exc}")
                continue
            if target.request is None:
                self.record(target.context, png)

    def record(self, context: str, png: bytes | None) -> None:
        """Note that a frame was sent for the context; ``png`` is kept as the canvas seed (None: not a PNG)."""
        with self._lock:
            if png is None:
                self._frames.pop(context, None)
            else:
                self._frames[context] = png
            self._painted[context] = datetime.now(timezone.utc)


class Canvas:
    """A key's drawing surface: painters draw on one buffer, and the last one out publishes a snapshot."""

    def __init__(
        self, ticker: FrameTicker, target: FrameTarget, size: tuple[int, int], loader: Callable[[], bytes | None]
    ) -> None:
        self._ticker: FrameTicker = ticker
        self._target: FrameTarget = target
        self._size: tuple[int, int] = size
        self._loader: Callable[[], bytes | None] = loader
        self._lock: threading.Lock = threading.Lock()
        self._buffer: "Image | None" = None
        self._painters: int = 0

    @contextmanager
    def paint(self) -> Iterator["Image"]:
        """Draw on the key's RGBA buffer; the frame is queued when the last painter leaves."""
        pil = _pillow()
        with self._lock:
            if self._buffer is None:
                self._buffer = self._initial(pil)
            buffer = self._buffer
            self._painters += 1
        try:
            yield buffer
        finally:
            with self._lock:
                self._painters -= 1
                snapshot = buffer.copy() if self._painters == 0 else None
            if snapshot is not None:
                self._ticker.mark_dirty(self._target, snapshot)

    def queue_paint(self) -> None:
        """Queue the current buffer again; nothing happens before the first paint."""
        with self._lock:
            snapshot = self._buffer.copy() if self._buffer is not None and self._painters == 0 else None
        if snapshot is not None:
            self._ticker.mark_dirty(self._target, snapshot)

    def _initial(self, pil: Any) -> "Image":
        blank = pil.new("RGBA", self._size, (0, 0, 0, 0))
        try:
            data = self._loader()
            if data is None:
                return blank
            with pil.open(io.BytesIO(data)) as loaded:
                frame = loaded.convert("RGBA")
        except Exception:  # pylint: disable=broad-exception-caught
            return blank
        if frame.size != self._size:
            frame = frame.resize(self._size)
        return frame


def _pillow() -> Any:
    try:
        from PIL import Image  # pylint: disable=import-outside-toplevel
    except ImportError as exc:
        raise RuntimeError(NO_PILLOW) from exc
    return Image
