"""Decker-drawn key appearance: resolves appearance settings and composes the final key image."""

import hashlib
import io
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageColor, ImageDraw, ImageFont

from decker.animation.gif import (
    MAX_RASTER_PIXELS,
    GifError,
    check_gif_bounds,
    is_animated_gif,
    is_gif,
)
from decker.animation.model import AnimationSpec
from decker.asset.renderer import AssetRenderer
from decker.common.appearance_schema import APPEARANCE_SCHEMA
from decker.common.background import normalize_background
from decker.common.text_style import fold_legacy_title
from decker.plugin.settings_schema import apply_schema, parse_settings_schema
from decker.server.fonts import BUNDLED_BOLD, font_face, font_file

_BASE_SIZE = 72  # title sizes in settings are relative to a 72 px key
_TITLE_MARGIN = 4
PACK_ICON_SCALE = 0.7  # pack icons are drawn at this fraction of the key, centred, so a title stays readable
_MIN_IMAGE_SCALE = 0.1
_LINE_BREAKS = re.compile(r"\s*[\r\n\v\f\x85\u2028\u2029]+\s*")
_PARSED_SCHEMA = parse_settings_schema(APPEARANCE_SCHEMA, "appearance")
_RENDERER = AssetRenderer()
_SVG_SNIFF_BYTES = 64 * 1024
_REPORTED_BAD_BACKGROUNDS: OrderedDict[str, None] = OrderedDict()
_REPORTED_LOCK = threading.Lock()
_MAX_REPORTED = 256
_LAYER_CACHE: OrderedDict[tuple[str, int], bytes] = OrderedDict()
_LAYER_LOCK = threading.Lock()
_MAX_CACHED_LAYERS = 64
_EXIF_ORIENTATION = 0x0112
_EXIF_TRANSPOSE: dict[int, Image.Transpose] = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}
_INTERNED: OrderedDict[str, bytes] = OrderedDict()
_INTERN_LOCK = threading.Lock()
_MAX_INTERNED = 32
_MAX_INTERNED_BYTES = 64 * 1024 * 1024


def _font(
    family: str, size: int, bold: bool = False, italic: bool = False
) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, bool]:
    """The font and whether it is really bold (so the caller knows when to fake it)."""
    path, real_bold = font_file(family, bold, italic)
    for candidate, is_bold in ((path, real_bold), (str(BUNDLED_BOLD), True)):
        if candidate:
            try:
                return ImageFont.truetype(candidate, size, index=font_face(candidate, family, bold, italic)), is_bold
            except OSError:
                continue
    return ImageFont.load_default(size), False


def resolve_appearance(action_defaults: Mapping[str, Any], stored: Mapping[str, Any]) -> dict[str, Any]:
    """Complete, typed appearance: schema defaults < action defaults < stored values (title merged per field)."""
    defaults = fold_legacy_title(action_defaults)
    values = fold_legacy_title(stored)
    merged = {**defaults, **values}
    if "title" in merged:
        merged["title"] = {**defaults.get("title", {}), **values.get("title", {})}
    if "background" in merged:
        merged["background"] = _merge_background(defaults.get("background"), values.get("background"))
    return apply_schema(_PARSED_SCHEMA, merged)


def _merge_background(default: Any, stored: Any) -> dict[str, Any]:
    """Per-field merge of two background values (either may be a legacy colour string or absent)."""
    base = normalize_background(default) if default is not None else {}
    if stored is None:
        return base
    return normalize_background(stored, base or None)


@dataclass(frozen=True)
class _TitleDraw:
    text: str
    xy: tuple[int, int]
    anchor: str
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    color: str
    outline: str | None
    outline_width: int
    fake_bold: int


def _draw_text_passes(draw: ImageDraw.ImageDraw, t: _TitleDraw) -> None:
    if t.outline:
        draw.text(
            t.xy,
            t.text,
            fill=t.outline,
            font=t.font,
            anchor=t.anchor,
            stroke_width=t.outline_width + t.fake_bold,
            stroke_fill=t.outline,
        )
    draw.text(t.xy, t.text, fill=t.color, font=t.font, anchor=t.anchor, stroke_width=t.fake_bold, stroke_fill=t.color)


def _decoration_rects(
    draw: ImageDraw.ImageDraw, t: _TitleDraw, px: int, style: Mapping[str, Any]
) -> list[tuple[float, float, float, float]]:
    """Underline / strike rectangles (left, top, right, bottom) derived from the measured text box."""
    left, top, right, _ = draw.textbbox(t.xy, t.text, font=t.font, anchor=t.anchor, stroke_width=t.fake_bold)
    baseline = top - t.font.getbbox(t.text, anchor="ls")[1]
    thickness = max(1, round(px / 14))
    rects = []
    if style["underline"]:
        rects.append((left, baseline + thickness, right, baseline + 2 * thickness - 1))
    if style["strike"]:
        middle = baseline - round(px * 0.3)
        rects.append((left, middle - thickness // 2, right, middle - thickness // 2 + thickness - 1))
    return rects


def _title_draw(text: str, style: Mapping[str, Any], size: int) -> tuple[_TitleDraw, int]:
    """Resolve the style into what to draw, plus the font size in px."""
    scale = size / _BASE_SIZE
    margin = round(_TITLE_MARGIN * scale)
    anchor, y = {"top": ("mt", margin), "middle": ("mm", size // 2), "bottom": ("mb", size - margin)}[style["position"]]
    px = max(1, round(style["size"] * scale))
    font, real_bold = _font(style["font"] or "DejaVu Sans", px, style["bold"], style["italic"])
    outline = style["outline"] or None
    return (
        _TitleDraw(
            text=text,
            xy=(size // 2, y),
            anchor=anchor,
            font=font,
            color=style["color"],
            outline=outline,
            outline_width=round(style["outline_width"] * scale) if outline else 0,
            fake_bold=max(1, round(px / 24)) if style["bold"] and not real_bold else 0,
        ),
        px,
    )


def _draw_title(draw: ImageDraw.ImageDraw, text: str, style: Mapping[str, Any], size: int) -> None:
    title, px = _title_draw(text, style, size)
    rects = _decoration_rects(draw, title, px, style)
    if title.outline:
        grow = title.outline_width
        for left, top, right, bottom in rects:
            draw.rectangle((left - grow, top - grow, right + grow, bottom + grow), fill=title.outline)
    _draw_text_passes(draw, title)
    for rect in rects:
        draw.rectangle(rect, fill=title.color)


def _single_line(text: str) -> str:
    """Titles are one line (Pillow can't anchor multi-line text at the top or bottom): breaks become one space."""
    return _LINE_BREAKS.sub(" ", text)


def cover_fit(image: Image.Image, size: int) -> Image.Image:
    """Centre-crop image to a square in source coordinates, then resize it once to size x size (RGBA)."""
    side = min(image.width, image.height)
    left, top = (image.width - side) // 2, (image.height - side) // 2
    square = image.crop((left, top, left + side, top + side)).convert("RGBA")
    return square.resize((size, size), Image.Resampling.LANCZOS)


def _report_bad_background(data: bytes, reason: object) -> None:
    key = hashlib.sha256(data).hexdigest()[:12]
    with _REPORTED_LOCK:
        if key in _REPORTED_BAD_BACKGROUNDS:
            return
        _REPORTED_BAD_BACKGROUNDS[key] = None
        while len(_REPORTED_BAD_BACKGROUNDS) > _MAX_REPORTED:
            _REPORTED_BAD_BACKGROUNDS.popitem(last=False)
    logging.debug("background image %s is unusable, using black: %s", key, reason)


def _is_svg(data: bytes) -> bool:
    head = data[:_SVG_SNIFF_BYTES].lstrip(b"\xef\xbb\xbf \t\r\n")
    return head.startswith(b"<") and b"<svg" in head


def _background_image(data: bytes, size: int) -> Image.Image | None:
    """The cover-fitted background image, or None (reported once per image) when it cannot be read.

    SVG backgrounds are rendered at size x size, so they are expected to be authored square to fill the key.
    """
    try:
        if _is_svg(data):
            rendered = _RENDERER.render_bytes(data, size, size)
            if rendered is None:
                _report_bad_background(data, "unparseable SVG")
            return rendered
        if is_gif(data):
            check_gif_bounds(data)
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > MAX_RASTER_PIXELS:
                raise GifError(f"image is too large ({image.width}x{image.height})")
            orientation = _EXIF_TRANSPOSE.get(image.getexif().get(_EXIF_ORIENTATION, 1))
            fitted = cover_fit(image, size)
        # A centred square crop commutes with these flips and quarter turns, so the small result is turned.
        return fitted.transpose(orientation) if orientation is not None else fitted
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _report_bad_background(data, exc)
        return None


def _cached_background_layer(data: bytes, size: int) -> Image.Image | None:
    """The cover-fitted layer from a bounded LRU keyed by the image content hash (never stale on change)."""
    key = (hashlib.sha256(data).hexdigest(), size)
    with _LAYER_LOCK:
        raw = _LAYER_CACHE.get(key)
        if raw is not None:
            _LAYER_CACHE.move_to_end(key)
    if raw is not None:
        return Image.frombytes("RGBA", (size, size), raw)
    layer = _background_image(data, size)
    if layer is None:
        return None
    layer = layer.convert("RGBA")
    with _LAYER_LOCK:
        _LAYER_CACHE[key] = layer.tobytes()
        while len(_LAYER_CACHE) > _MAX_CACHED_LAYERS:
            _LAYER_CACHE.popitem(last=False)
    return layer


def intern_background(data: bytes | None) -> bytes | None:
    """The one shared bytes object for this background content (bounded LRU), so specs never hold copies."""
    if data is None:
        return None
    key = hashlib.sha256(data).hexdigest()
    with _INTERN_LOCK:
        shared = _INTERNED.get(key)
        if shared is not None:
            _INTERNED.move_to_end(key)
            return shared
        if len(data) > _MAX_INTERNED_BYTES:
            return data
        _INTERNED[key] = data
        total = sum(len(value) for value in _INTERNED.values())
        while len(_INTERNED) > _MAX_INTERNED or total > _MAX_INTERNED_BYTES:
            _, dropped = _INTERNED.popitem(last=False)
            total -= len(dropped)
    return data


def _background_layer(appearance: Mapping[str, Any], size: int, background_image: bytes | None = None) -> Image.Image:
    """Opaque black with the appearance background composited on top (the hardware has no transparency)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 255))
    background = appearance["background"]
    if background["type"] == "color" and ImageColor.getcolor(background["color"], "RGBA")[3]:  # type: ignore[index]
        img.alpha_composite(Image.new("RGBA", (size, size), background["color"]))
    elif background["type"] == "image" and background_image:
        layer = _cached_background_layer(background_image, size)
        if layer is not None:
            img.alpha_composite(layer)
    return img


def inner_size(size: int, plugin_image_scale: float) -> int:
    """The side of the square a plugin image is drawn in on a key of this size."""
    return max(1, round(size * min(1.0, max(_MIN_IMAGE_SCALE, plugin_image_scale))))


def _title_layer(appearance: Mapping[str, Any], plugin_title: str, size: int) -> Image.Image | None:
    """The transparent title layer, or None when no title is shown."""
    style = appearance["title"]
    text = _single_line(style["text"] or plugin_title)
    if not (style["visible"] and text.strip()):
        return None
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    _draw_title(ImageDraw.Draw(layer), text, style, size)
    return layer


def compose_key_image(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    appearance: Mapping[str, Any],
    plugin_image: bytes | None,
    plugin_title: str,
    size: int = 144,
    plugin_image_color: str | None = None,
    plugin_image_scale: float = 1.0,
    background_image: bytes | None = None,
) -> bytes:
    """Compose background, plugin image and title into a size x size PNG."""
    img = _background_layer(appearance, size, background_image)
    if plugin_image:
        try:
            inner = inner_size(size, plugin_image_scale)
            layer = _RENDERER.render_bytes(plugin_image, inner, inner, plugin_image_color)
            if layer is not None:
                offset = (size - inner) // 2
                img.alpha_composite(layer, dest=(offset, offset))
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.debug("skipping unreadable plugin image: %s", exc)
    title = _title_layer(appearance, plugin_title, size)
    if title is not None:
        img.alpha_composite(title)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


@dataclass(frozen=True)
class Composed:
    """A composed key face: the PNG (the poster when animated) and its animation, if the image is an animated GIF."""

    png: bytes
    animation: AnimationSpec | None


def compose_key_face(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    appearance: Mapping[str, Any],
    image: bytes | None,
    title: str,
    image_color: str | None = None,
    image_scale: float = 1.0,
    poster: bool = True,
    background_image: bytes | None = None,
) -> Composed:
    """Compose a key PNG; an animated GIF image (or, failing that, background) also yields the AnimationSpec.

    An animated foreground GIF wins over an animated background. With poster=False the PNG is not composed
    (it is empty): the caller only wants to know the animation.
    """
    # The renderer draws a GIF's first frame, so for an animated GIF this PNG is already the poster.
    png = (
        compose_key_image(
            appearance,
            image,
            title,
            plugin_image_color=image_color,
            plugin_image_scale=image_scale,
            background_image=background_image,
        )
        if poster
        else b""
    )
    # is_animated_gif never raises. A GIF that fails only when fully decoded keeps this poster; the factory warns once.
    image_background = background_image if background_image and appearance["background"]["type"] == "image" else None
    if image is not None and is_animated_gif(image):
        return Composed(png, AnimationSpec(image, appearance, title, image_scale, background=image_background))
    if image_background is not None and is_animated_gif(image_background):
        spec = AnimationSpec(
            image_background,
            appearance,
            title,
            image_scale,
            layer="background",
            overlay=image,
            overlay_color=image_color,
        )
        return Composed(png, spec)
    return Composed(png, None)


def _compose_background_frame(
    background: Image.Image, frame: Image.Image, overlay: Image.Image | None, offset: int
) -> Image.Image:
    img = background.copy()
    img.alpha_composite(cover_fit(frame, img.width))
    if overlay is not None:
        img.alpha_composite(overlay, dest=(offset, offset))
    return img


def _overlay_layer(overlay: bytes | None, inner: int, color: str | None) -> Image.Image | None:
    if not overlay:
        return None
    try:
        return _RENDERER.render_bytes(overlay, inner, inner, color)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.debug("skipping unreadable plugin image: %s", exc)
        return None


def compose_key_frames(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    appearance: Mapping[str, Any],
    frames: Sequence[Image.Image],
    plugin_title: str,
    size: int = 144,
    plugin_image_scale: float = 1.0,
    *,
    layer: str = "foreground",
    overlay: bytes | None = None,
    overlay_color: str | None = None,
    background_image: bytes | None = None,
) -> list[Image.Image]:
    """Compose background, each animation frame and the title into size x size RGB images.

    With layer="background" the frames are the key's background and the static overlay is drawn over them;
    otherwise background_image (its first frame, for a GIF) is the static base under every frame.
    """
    inner = inner_size(size, plugin_image_scale)
    offset = (size - inner) // 2
    background = (
        Image.new("RGBA", (size, size), (0, 0, 0, 255))
        if layer == "background"
        else _background_layer(appearance, size, background_image)
    )
    overlay_image = _overlay_layer(overlay, inner, overlay_color) if layer == "background" else None
    title = _title_layer(appearance, plugin_title, size)
    composed = []
    for frame in frames:
        if layer == "background":
            img = _compose_background_frame(background, frame, overlay_image, offset)
        else:
            img = background.copy()
            scaled = frame.convert("RGBA").resize((inner, inner), Image.Resampling.LANCZOS)
            img.alpha_composite(scaled, dest=(offset, offset))
        if title is not None:
            img.alpha_composite(title)
        composed.append(img.convert("RGB"))
    return composed
