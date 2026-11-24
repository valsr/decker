"""Resizes imported raster icons (stills and animated GIFs) to the 144x144 key size."""

import io
import json
from dataclasses import dataclass
from typing import Any

from PIL import Image, UnidentifiedImageError

from decker.animation.gif import MAX_RASTER_PIXELS, GifError, decode_gif, is_gif
from decker.common.icon_limits import max_icon_bytes

ICON_SIZE = 144
_FITS = ("center", "stretch")
_FORMATS = ("PNG", "JPEG", "GIF")
_RESAMPLING = {
    "nearest": Image.Resampling.NEAREST,
    "bilinear": Image.Resampling.BILINEAR,
    "lanczos": Image.Resampling.LANCZOS,
}


class ImageImportError(ValueError):
    """Raised when image data cannot be imported."""


@dataclass(frozen=True)
class ImportOptions:
    """How to fit and resample an imported image onto the key."""

    fit: str = "center"
    interpolation: str = "nearest"

    def __post_init__(self) -> None:
        if self.fit not in _FITS:
            raise ValueError(f"unknown fit: {self.fit!r}")
        if self.interpolation not in _RESAMPLING:
            raise ValueError(f"unknown interpolation: {self.interpolation!r}")

    @classmethod
    def from_json(cls, text: str) -> "ImportOptions":
        """Parse options from JSON; empty text gives the defaults and unknown values raise ValueError."""
        if not text.strip():
            return cls()
        raw: Any = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("import options must be a JSON object")
        return cls(str(raw.get("fit", "center")), str(raw.get("interpolation", "nearest")))


def resize_image(image: Image.Image, options: ImportOptions) -> Image.Image:
    """Return image as a 144x144 RGBA, centred on a transparent square or stretched to fill it."""
    rgba = image.convert("RGBA")
    method = _RESAMPLING[options.interpolation]
    if options.fit == "stretch":
        return rgba.resize((ICON_SIZE, ICON_SIZE), method)
    scale = ICON_SIZE / max(rgba.size)
    size = (max(1, round(rgba.width * scale)), max(1, round(rgba.height * scale)))
    scaled = rgba.resize(size, method)
    canvas = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    canvas.paste(scaled, ((ICON_SIZE - size[0]) // 2, (ICON_SIZE - size[1]) // 2))
    return canvas


def _encode(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _check_size(data: bytes) -> None:
    if len(data) > max_icon_bytes():
        raise ImageImportError(f"image is larger than {max_icon_bytes()} bytes")


def _convert_gif(data: bytes, options: ImportOptions) -> tuple[bytes, str]:
    with Image.open(io.BytesIO(data), formats=_FORMATS) as source:
        loop = source.info.get("loop", 0)
    frames = decode_gif(data, transform=lambda frame: resize_image(frame, options))
    if len(frames) < 2:
        return _encode(frames[0].image), "png"
    buffer = io.BytesIO()
    frames[0].image.save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=[frame.image for frame in frames[1:]],
        duration=[frame.delay_ms for frame in frames],
        loop=loop,
        disposal=2,
    )
    return buffer.getvalue(), "gif"


def _open_still(data: bytes) -> Image.Image:
    with Image.open(io.BytesIO(data), formats=_FORMATS) as image:
        if image.width * image.height > MAX_RASTER_PIXELS:
            raise ImageImportError(f"image is too large ({image.width}x{image.height})")
        image.load()
        return image.copy()


def _load(data: bytes, options: ImportOptions, first_frame_only: bool) -> tuple[bytes, str]:
    try:
        _check_size(data)
        if is_gif(data):
            if not first_frame_only:
                return _convert_gif(data, options)
            frames = decode_gif(data, transform=lambda frame: resize_image(frame, options))
            return _encode(frames[0].image), "png"
        return _encode(resize_image(_open_still(data), options)), "png"
    except (GifError, UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError) as exc:
        raise ImageImportError(f"cannot import image: {exc}") from exc


def convert_raster(data: bytes, options: ImportOptions) -> tuple[bytes, str]:
    """Resize an image to 144x144 and return (bytes, extension): png for stills, gif for animations."""
    return _load(data, options, False)


def preview_png(data: bytes, options: ImportOptions) -> bytes:
    """Return a PNG of the image's first frame at 144x144."""
    return _load(data, options, True)[0]
