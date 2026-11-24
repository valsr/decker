"""Settings control for an icon: preview, name, Browse…, clear and a fill colour."""

import json
import logging
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from decker.client.card_icons import decode_icon, placeholder_pixmap
from decker.client.context import AppContext
from decker.client.icon_cache import IconThumbnailCache, icon_cache
from decker.client.icon_picker import NO_TINT, IconBrowserDialog, RpcCaller
from decker.client.widgets.color_button import ColorButton
from decker.common.icon_ref import (
    USER_PACK_ID,
    icon_display_name,
    make_icon_ref,
    normalize_icon_value,
    parse_icon_ref,
)

_PREVIEW_SIZE = 24


class _ClickLabel(QLabel):
    """A label that reports a left click."""

    clicked = pyqtSignal()

    def mouseReleaseEvent(self, ev: Any) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Emit ``clicked`` for a left click inside the label."""
        if (
            ev is not None
            and ev.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(ev.position().toPoint())
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(ev)


def _app_rpc() -> RpcCaller | None:
    return AppContext.get().connection_manager


class IconEdit(QWidget):
    """One row: a 24 px preview, the icon's name, **Browse…**, a clear button and the fill colour swatch."""

    value_changed = pyqtSignal()

    def __init__(
        self,
        rpc: Callable[[], RpcCaller | None] | None = None,
        parent: QWidget | None = None,
        cache: IconThumbnailCache | None = None,
    ) -> None:
        super().__init__(parent)
        self._rpc = rpc or _app_rpc
        self._cache = cache if cache is not None else icon_cache()
        self._packs_listed = False
        self._ref = ""
        self._color = "#ffffff"
        self._raw: dict[str, str] = {}
        self._user_files: list[str] | None = None
        self._preview = _ClickLabel()
        self._preview.setFixedSize(_PREVIEW_SIZE, _PREVIEW_SIZE)
        self._preview.clicked.connect(self._on_preview_clicked)
        self._toolbar = False
        self._name = QLabel()
        self._compact = False
        self._show_colour = True
        self._tint_thumbnail = True
        self._placeholder = "No icon"
        self._browse_btn = QPushButton("Browse…")
        self._browse_btn.clicked.connect(self.browse)
        self._clear = QToolButton()
        self._clear.setText("✕")
        self._clear.setToolTip("Remove the icon")
        self._clear.clicked.connect(self.clear_icon)
        self._colour = ColorButton(self._color, alpha=True)
        self._colour.set_show_name(False)
        self._colour.setToolTip("Icon fill colour")
        self._colour.color_changed.connect(self._on_colour_changed)

        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.addWidget(self._preview)
        self._row.addWidget(self._name, 1)
        self._row.addWidget(self._browse_btn)
        self._row.addWidget(self._clear)
        self._row.addWidget(self._colour)
        self._refresh()

    def set_toolbar(self, toolbar: bool) -> None:
        """Dock the thumbnail (click it to choose) and the fill colour of an SVG in a frame, delete button beside it."""
        if toolbar == self._toolbar:
            return
        self._toolbar = toolbar
        if not toolbar:
            return
        self._name.hide()
        self._browse_btn.hide()
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self._preview.setToolTip("Click to choose an image")
        self._preview.setCursor(Qt.CursorShape.PointingHandCursor)
        frame = QFrame()
        frame.setProperty("role", "dock")
        dock = QHBoxLayout(frame)
        dock.setContentsMargins(4, 2, 4, 2)
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        for widget in (self._preview, separator, self._colour):
            dock.addWidget(widget)
        self._row.addWidget(frame)
        self._row.addWidget(self._clear)
        self._clear.setIcon(QIcon.fromTheme("edit-delete"))
        if not self._clear.icon().isNull():
            self._clear.setText("")
        self._separator = separator
        self._refresh()

    def _on_preview_clicked(self) -> None:
        if self._toolbar:
            self.browse()

    def set_compact(self, compact: bool) -> None:
        """Compact: thumbnail, clear and (once there is an icon) the colour; double-click the thumbnail to browse."""
        self._compact = compact
        self.setSizePolicy(
            QSizePolicy.Policy.Maximum if compact else QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )
        self._name.setVisible(not compact)
        self._browse_btn.setVisible(not compact)
        self._refresh()

    def set_texts(self, placeholder: str, clear_tooltip: str) -> None:
        """Change the text shown when empty and the clear button's tooltip (for non-icon uses such as backgrounds)."""
        self._placeholder = placeholder
        self._clear.setToolTip(clear_tooltip)
        self._refresh()

    def set_show_colour(self, show: bool) -> None:
        """Show or hide the fill colour swatch (an icon whose colour is not configurable hides it)."""
        self._show_colour = show
        self._refresh()

    def set_tint_thumbnail(self, tint: bool) -> None:
        """Tint SVG thumbnails with the fill colour (default), or show them as they are (backgrounds are untinted)."""
        self._tint_thumbnail = tint
        self._refresh()

    def thumbnail(self) -> QPixmap:
        """The 24 px preview of the current icon (blank when there is none)."""
        pixmap = self._preview.pixmap()
        return pixmap if pixmap is not None else QPixmap()

    def mouseDoubleClickEvent(self, a0: Any) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """In compact mode a double click opens the browser."""
        if self._compact:
            self.browse()
        else:
            super().mouseDoubleClickEvent(a0)

    def value(self) -> dict[str, Any]:
        """The icon value: ``{"ref": "", "pack:<id>/<path>" or "fas:play", "color": "#rrggbb[aa]"}``."""
        return {"ref": self._ref, "color": self._color}

    def set_value(self, value: Any) -> None:
        """Show ``value`` (normalised) without emitting ``value_changed``."""
        normalized = normalize_icon_value(value)
        self._ref, self._color = normalized["ref"], normalized["color"]
        self._colour.set_color(self._color)
        self._refresh()

    def browse(self) -> None:
        """Open the icon browser; a chosen icon becomes the value."""
        dialog = IconBrowserDialog(self, self._rpc(), None if self._tint_thumbnail else NO_TINT)
        dialog.icon_chosen.connect(self._on_icon_chosen)
        dialog.exec()

    def _on_icon_chosen(self, pack_id: str, path: str) -> None:
        self._user_files = None  # the browser may have imported or deleted User images
        self._ref = make_icon_ref(pack_id, path)
        self._refresh()
        self.value_changed.emit()

    def clear_icon(self) -> None:
        """Remove the icon (the colour is kept)."""
        self._ref = ""
        self._refresh()
        self.value_changed.emit()

    def _on_colour_changed(self, color: str) -> None:
        self._color = color
        self._refresh()
        self.value_changed.emit()

    def _refresh(self) -> None:
        parsed = self._resolve_user(parse_icon_ref(self._ref))
        self._name.setText(icon_display_name(parsed[1]) if parsed is not None else self._placeholder)
        self._clear.setEnabled(parsed is not None)
        self._colour.setVisible(self._show_colour and self._wants_colour(parsed))
        if self._toolbar:
            self._separator.setVisible(self._colour.isVisibleTo(self))
        self._preview.setPixmap(self._preview_pixmap(parsed))

    def _resolve_user(self, parsed: tuple[str, str] | None) -> tuple[str, str] | None:
        """A ``user:<id>`` reference names its file (``logo`` → ``logo.svg``), so the kind of image is known."""
        if parsed is None or parsed[0] != USER_PACK_ID or "." in parsed[1]:
            return parsed
        if self._user_files is None:
            rpc = self._rpc()
            try:
                listed = json.loads(rpc.call("iconpack_icons", USER_PACK_ID) or "null") if rpc is not None else None
            except Exception:  # pylint: disable=broad-exception-caught
                listed = None
            if not isinstance(listed, list):
                return parsed  # not cached: ask again once the server answers
            self._user_files = [str(name) for name in listed]
        icon_id = parsed[1]
        return USER_PACK_ID, next((f for f in self._user_files if f.rsplit(".", 1)[0].lower() == icon_id), icon_id)

    def _wants_colour(self, parsed: tuple[str, str] | None) -> bool:
        if self._toolbar:
            return parsed is not None and parsed[1].lower().endswith(".svg")  # only an SVG takes a fill colour
        return parsed is not None or not self._compact

    def _preview_pixmap(self, parsed: tuple[str, str] | None) -> QPixmap:
        if parsed is None:
            blank = QPixmap(_PREVIEW_SIZE, _PREVIEW_SIZE)
            blank.fill(Qt.GlobalColor.transparent)
            return blank
        pack_id, path = parsed
        self._ensure_versions(pack_id)
        cached = self._cache_get(pack_id, path)
        if cached is not None:
            return cached
        raw = self._raw_icon(parsed)
        pixmap = decode_icon(raw, self._thumb_tint()) if raw else None
        if pixmap is None:
            return placeholder_pixmap(path, _PREVIEW_SIZE)  # a letter tile, never cached
        thumb = pixmap.scaled(
            _PREVIEW_SIZE,
            _PREVIEW_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._cache_put(pack_id, path, thumb)
        return thumb

    def _thumb_tint(self) -> str | None:
        return self._color if self._tint_thumbnail else None

    def _ensure_versions(self, pack_id: str) -> None:
        """Learn the packs' plugin versions (one RPC per control) so thumbnails can be cached by version."""
        if self._packs_listed or self._cache_version(pack_id) is not None:
            return
        rpc = self._rpc()
        if rpc is None:
            return
        self._packs_listed = True
        try:
            packs = json.loads(rpc.call("iconpacks_list") or "null")
            if isinstance(packs, list):
                self._cache.update_packs(packs)
        except Exception:  # pylint: disable=broad-exception-caught
            logging.debug("Could not list icon packs for the thumbnail cache", exc_info=True)

    def _cache_version(self, pack_id: str) -> str | None:
        try:
            return self._cache.version_of(pack_id)
        except Exception:  # pylint: disable=broad-exception-caught
            return None

    def _cache_get(self, pack_id: str, path: str) -> QPixmap | None:
        try:
            return self._cache.get(pack_id, path, _PREVIEW_SIZE, self._thumb_tint())
        except Exception:  # pylint: disable=broad-exception-caught
            logging.warning("Icon thumbnail cache lookup failed", exc_info=True)
            return None

    def _cache_put(self, pack_id: str, path: str, pixmap: QPixmap) -> None:
        try:
            self._cache.put(pack_id, path, _PREVIEW_SIZE, self._thumb_tint(), pixmap)
        except Exception:  # pylint: disable=broad-exception-caught
            logging.warning("Icon thumbnail cache store failed", exc_info=True)

    def _raw_icon(self, parsed: tuple[str, str]) -> str | None:
        ref = make_icon_ref(*parsed)
        if ref not in self._raw:
            rpc = self._rpc()
            raw = rpc.call("iconpack_icon", *parsed) if rpc is not None else None
            if not isinstance(raw, str) or not raw:
                return None  # not cached: retry once the server answers
            self._raw[ref] = raw
        return self._raw[ref]
