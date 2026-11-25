"""Pixmaps for plugin cards: the icon the server sent, or a generated letter tile."""

import base64
import binascii
import zlib
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QByteArray, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication

from decker.asset.builtinicons import read_builtin_ref
from decker.client.widgets.color_tool_button import hex_to_qcolor

# Muted tile colours that keep white text readable (WCAG AA); chosen per card so tiles stay stable.
_PLACEHOLDER_COLORS = ("#3b6ea8", "#7a4fa0", "#2f7d68", "#a8532f", "#8a6d1f", "#4f5b70")


def card_pixmap(card: dict[str, str], size: int) -> QPixmap:
    """Return the card's icon scaled to size×size, or a letter tile if it has no usable icon."""
    pixmap = decode_icon(card.get("icon_b64", ""), icon_tint())
    if pixmap is None:
        return placeholder_pixmap(card.get("card_name", ""), size)
    return pixmap.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)


def asset_pixmap(name: str, size: int, tint: str | None = None) -> QPixmap:
    """A bundled icon from the assets folder (an SVG using ``currentColor`` is filled with ``tint``), size×size."""
    try:
        raw = (Path(__file__).resolve().parents[1] / "assets" / name).read_bytes()
    except OSError:
        return placeholder_pixmap(name, size)
    pixmap = decode_icon(base64.b64encode(raw).decode("ascii"), tint)
    if pixmap is None:
        return placeholder_pixmap(name, size)
    return pixmap.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)


def icon_tint() -> str:
    """The ``#rrggbb`` colour icons are tinted with: the theme's icon colour, else the palette text colour."""
    app = QApplication.instance()
    value = app.property("iconColor") if app is not None else None
    return value if isinstance(value, str) and value else QApplication.palette().text().color().name()


def builtin_pixmap(ref: Any) -> QPixmap | None:
    """A bundled Font Awesome icon (``fas:image``) tinted with ``icon_tint()``; None if unknown."""
    raw = read_builtin_ref(ref)
    if raw is None:
        return None
    return decode_icon(base64.b64encode(raw).decode("ascii"), icon_tint())


def tab_icon(ref: str, size: int = 16) -> QIcon:
    """A bundled Font Awesome icon (``fas:image``) tinted with ``icon_tint()``; empty if unknown."""
    pixmap = builtin_pixmap(ref)
    if pixmap is None:
        return QIcon()
    return QIcon(
        pixmap.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    )


def decode_icon(icon_b64: str, tint: str | None = None) -> QPixmap | None:
    """Decode a base64 image (PNG, JPEG, SVG, …); None if it is empty or not a readable image.

    ``tint`` (``#rrggbb[aa]``) recolours SVGs that use ``currentColor``; any other image is returned as is.
    """
    if not icon_b64:
        return None
    try:
        data = base64.b64decode(icon_b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    pixmap = QPixmap()
    if not pixmap.loadFromData(QByteArray(data)) or pixmap.isNull():
        return None
    color = hex_to_qcolor(tint) if tint and b"currentColor" in data else None
    return _tinted(pixmap, color) if color is not None else pixmap


def _tinted(pixmap: QPixmap, color: QColor) -> QPixmap:
    """The pixmap's shape filled with ``color`` (the alpha of the source is kept)."""
    result = QPixmap(pixmap.size())
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.drawPixmap(0, 0, pixmap)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(result.rect(), color)
    painter.end()
    return result


def placeholder_pixmap(name: str, size: int) -> QPixmap:
    """A rounded tile showing the first letter of name, coloured consistently per name."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    color = QColor(_PLACEHOLDER_COLORS[zlib.crc32(name.encode()) % len(_PLACEHOLDER_COLORS)])
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    rect = QRectF(0, 0, size, size)
    painter.drawRoundedRect(rect, size * 0.18, size * 0.18)
    font = QFont()
    font.setBold(True)
    font.setPixelSize(max(1, int(size * 0.5)))
    painter.setFont(font)
    painter.setPen(QColor("#ffffff"))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, (name.strip()[:1] or "?").upper())
    painter.end()
    return pixmap
