"""Recolour SVG icons by rewriting the root element: ``currentColor`` and unstyled paths follow the colour."""

import re

from decker.common.text_style import is_color

_ATTRIBUTE = rb"\s[\w:.-]+\s*=\s*(?:\"[^\"]*\"|'[^']*')"
_ROOT = re.compile(rb"<svg\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*>", re.IGNORECASE)
_PROLOG = re.compile(rb"\s*(?:<\?.*?\?>|<!--.*?-->|<![^>]*>)", re.DOTALL)
_COLOR_OR_OPACITY = re.compile(rb"\s(?:color|opacity)\s*=\s*(?:\"[^\"]*\"|'[^']*')", re.IGNORECASE)
_FILL = re.compile(rb"\sfill\s*=", re.IGNORECASE)


def _root_span(data: bytes) -> tuple[int, int] | None:
    pos = 0
    while (skipped := _PROLOG.match(data, pos)) is not None:
        pos = skipped.end()
    pos += len(data[pos:]) - len(data[pos:].lstrip())
    match = _ROOT.match(data, pos)
    return (match.start(), match.end()) if match is not None else None


def _opacity(alpha: int) -> str:
    return f"{alpha / 255:.3f}".rstrip("0").rstrip(".")


def tint_svg(data: bytes, color: str) -> bytes:
    """``data`` with the root ``<svg>`` given ``color``, a default ``fill`` (unless set) and the alpha as ``opacity``.

    Returns ``data`` unchanged when ``color`` is not ``#rrggbb[aa]`` or no root element is found.
    """
    span = _root_span(data) if is_color(color) else None
    if span is None:
        return data
    start, end = span
    tag = _COLOR_OR_OPACITY.sub(b"", data[start:end])
    closing = b"/>" if tag.endswith(b"/>") else b">"
    head = tag[: -len(closing)]
    extra = f' color="{color[:7]}"'
    if _FILL.search(head) is None:
        extra += f' fill="{color[:7]}"'
    alpha = int(color[7:9], 16) if len(color) == 9 else 255
    if alpha < 255:
        extra += f' opacity="{_opacity(alpha)}"'
    return data[:start] + head + extra.encode() + closing + data[end:]


_STYLE_ELEMENT = re.compile(rb"<style\b", re.IGNORECASE)
_STYLE_ATTRIBUTE = re.compile(rb"\sstyle\s*=", re.IGNORECASE)
_PAINT_ATTRIBUTE = re.compile(rb"\s(?:fill|stroke)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.IGNORECASE)
_NEUTRAL_PAINT = {b"none", b"currentcolor"}


def is_unstyled_svg(data: bytes) -> bool:
    """True for an SVG that sets no colour anywhere, so its paths draw in the default colour (black)."""
    if _root_span(data) is None or _STYLE_ELEMENT.search(data) or _STYLE_ATTRIBUTE.search(data):
        return False
    for match in _PAINT_ATTRIBUTE.finditer(data):
        if (match.group(1) or match.group(2) or b"").strip().lower() not in _NEUTRAL_PAINT:
            return False
    return True
