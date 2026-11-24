"""Renders resolved asset paths or runtime image payloads to PIL Images."""

import io
from pathlib import Path
from xml.etree.ElementTree import ParseError

import cairosvg  # type: ignore[import-untyped]
from PIL import Image, UnidentifiedImageError

from decker.animation.gif import MAX_RASTER_PIXELS, GifError, check_gif_bounds, is_gif
from decker.asset.tint import tint_svg
from decker.common.icon_limits import max_icon_bytes

_SVG_SNIFF_BYTES = 64 * 1024


class ImageTooLargeError(ValueError):
    """Raised when a raster image exceeds the pixel budget."""


def _guard_gif(source: Path | io.BytesIO) -> None:
    """Refuse a GIF whose block structure would stall Pillow's open (a comment flood); other inputs pass through."""
    if isinstance(source, Path):
        with source.open("rb") as handle:
            data = handle.read(max_icon_bytes() + 1)
    else:
        data = source.getvalue()
    if not is_gif(data):
        return
    try:
        if len(data) > max_icon_bytes():
            raise GifError("GIF is too large to read")
        check_gif_bounds(data)
    except GifError as exc:
        raise UnidentifiedImageError(str(exc)) from exc


def _open_raster(source: Path | io.BytesIO) -> Image.Image:
    _guard_gif(source)
    image = Image.open(source)
    if image.width * image.height > MAX_RASTER_PIXELS:
        raise ImageTooLargeError(f"image is too large ({image.width}x{image.height})")
    return image


def _looks_like_svg(data: bytes) -> bool:
    head = data[:_SVG_SNIFF_BYTES].lstrip(b"\xef\xbb\xbf \t\r\n")
    return head.startswith(b"<") and b"<svg" in head


class AssetRenderer:
    """Converts SVG/PNG/JPEG/GIF/WEBP sources to a PIL Image at a target size.

    This renderer is for static output: GIF/WEBP inputs render their first frame only (Pillow's default
    Image.open() behavior). Animated key faces are decoded and played by decker.animation instead.
    """

    def render_path(self, path: Path, width: int, height: int) -> Image.Image:
        """Render the asset at path to a width x height RGBA image."""
        if path.suffix.lower() == ".svg":
            return self._render_svg(path.read_bytes(), width, height)
        return _open_raster(path).convert("RGBA").resize((width, height))

    def render_bytes(self, data: bytes, width: int, height: int, color: str | None = None) -> Image.Image | None:
        """Render raw image bytes (SVG or raster) to a width x height RGBA image; ``color`` tints SVG input only.

        Returns None if data is not a recognizable image format — callers should treat
        that as already-native hardware bytes and pass them through unchanged.
        """
        if _looks_like_svg(data):
            try:
                return self._render_svg(tint_svg(data, color) if color else data, width, height)
            except ParseError:
                return None
        try:
            return _open_raster(io.BytesIO(data)).convert("RGBA").resize((width, height))
        except UnidentifiedImageError:
            return None

    @staticmethod
    def _render_svg(data: bytes, width: int, height: int) -> Image.Image:
        png_bytes = cairosvg.svg2png(bytestring=data, output_width=width, output_height=height)
        return Image.open(io.BytesIO(png_bytes)).convert("RGBA")
