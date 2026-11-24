"""Button grid matching the active device's key layout."""

import base64
import json
from collections.abc import Collection
from pathlib import Path
from typing import Any

from PyQt6.QtCore import (
    QBuffer,
    QByteArray,
    QEvent,
    QIODevice,
    QMimeData,
    QPoint,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QDrag,
    QEnterEvent,
    QFocusEvent,
    QHideEvent,
    QIcon,
    QKeyEvent,
    QMouseEvent,
    QMovie,
    QPainter,
    QPen,
    QPixmap,
    QResizeEvent,
    QShowEvent,
)
from PyQt6.QtWidgets import (
    QApplication,
    QGridLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from decker.client.card_icons import decode_icon
from decker.client.widgets.sidebar import CARD_MIME_TYPE
from decker.common.model import KeyInfo

_LONG_PRESS_MS = 500
_RUN_SIZE = 30  # px, the hover play button
_RUN_ICON_PX = 21  # px, the play-circle drawing inside its 30 px button
_PLAY_ICON = Path(__file__).resolve().parents[2] / "assets" / "play-circle.svg"
_RUN_MARGIN = 4  # px from the key's top-right corner
_KEY_MIME_TYPE = "application/x-decker-key"
_PLUS_ICON = Path(__file__).resolve().parents[2] / "assets" / "plus-circle.svg"
_BADGE_COLOR = "#2ecc71"  # the copy badge's green
_ICON_PX = 68  # px, the key image inside its button
_BADGE_SIZE = 30  # px, the label holding the badge
_DELETE_ICON_PX = 18  # px, the red delete circle inside its 30 px button (the image library draws the same badge)


def _exec_drag(drag: QDrag) -> None:
    """Run a drag (a seam the tests replace; a real drag blocks until the drop)."""
    drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction, Qt.DropAction.MoveAction)


def _badge_pixmap() -> QPixmap:
    """The green plus-circle (blank if the SVG is unusable)."""
    try:
        raw = _PLUS_ICON.read_bytes()
    except OSError:
        return QPixmap()
    pixmap = decode_icon(base64.b64encode(raw).decode("ascii"), _BADGE_COLOR)
    if pixmap is None:
        return QPixmap()
    return pixmap.scaled(
        _RUN_ICON_PX, _RUN_ICON_PX, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
    )


def _play_icon(color: str) -> QIcon | None:
    """The shipped play-circle drawing filled with ``color`` (rendered at 2x for crisp edges), or None if unusable."""
    try:
        raw = _PLAY_ICON.read_bytes()
    except OSError:
        return None
    pixmap = decode_icon(base64.b64encode(raw).decode("ascii"), color)
    if pixmap is None:
        return None
    size = _RUN_ICON_PX * 2
    return QIcon(
        pixmap.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    )


def _delete_icon() -> QIcon:
    """A red circle with a white cross, like the image library's delete badge (drawn at 2x for crisp edges)."""
    side = _DELETE_ICON_PX * 2
    pixmap = QPixmap(side, side)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen_width = max(2.0, side / 14)
    painter.setPen(QPen(QColor(255, 255, 255), pen_width))
    painter.setBrush(QBrush(QColor(190, 40, 40, 230)))
    painter.drawEllipse(QRectF(pen_width / 2, pen_width / 2, side - pen_width, side - pen_width))
    inset = side * 0.32
    painter.drawLine(QPointF(inset, inset), QPointF(side - inset, side - inset))
    painter.drawLine(QPointF(side - inset, inset), QPointF(inset, side - inset))
    painter.end()
    return QIcon(pixmap)


class _RunButton(QToolButton):
    """The play button: a click runs the key, holding it for half a second is a long press (and no click)."""

    long_pressed = pyqtSignal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._held = False
        self._long_press_timer = QTimer(self)
        self._long_press_timer.setSingleShot(True)
        self._long_press_timer.setInterval(_LONG_PRESS_MS)
        self._long_press_timer.timeout.connect(self._on_long_press_fired)

    def mousePressEvent(self, e: QMouseEvent | None) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Start timing the hold."""
        self._held = False
        self._long_press_timer.start()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e: QMouseEvent | None) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """A normal release clicks; the release after a long press does not."""
        self._long_press_timer.stop()
        if self._held:
            self._held = False
            self.setDown(False)
            self.update()
            return
        super().mouseReleaseEvent(e)

    def _on_long_press_fired(self) -> None:
        self._held = True
        self.long_pressed.emit()


class KeyButtonWidget(QPushButton):  # pylint: disable=too-many-instance-attributes,too-many-public-methods
    """Square button for one key: a click selects it, its play button runs it, and it accepts card drops."""

    card_dropped = pyqtSignal(int, object)
    long_pressed = pyqtSignal(int)
    edit_clicked = pyqtSignal(int)
    run_clicked = pyqtSignal(int)
    delete_clicked = pyqtSignal(int)
    key_dropped = pyqtSignal(int, int, bool)  # from index, to index (this key), copy

    def __init__(self, index: int, key_info: KeyInfo) -> None:
        super().__init__()
        self._info = key_info
        self.setFixedSize(self._info.pixel_width, self._info.pixel_height)
        self.index = index
        self._label: str = ""
        self._refresh_text()
        self.setAcceptDrops(True)
        self.clicked.connect(lambda _checked=False: self.edit_clicked.emit(self.index))
        self._run_button = self._build_run_button()
        self._delete_button = self._build_delete_button()
        self._draggable = False
        self._drag_start: QPoint | None = None
        self._dragged = False  # a drag began during this press, so its remaining moves and release are not clicks
        self._scope = ""
        self._copy_badge = self._build_copy_badge()
        self._poster: QPixmap | None = None  # the static image, shown while no animation plays
        self._movie: QMovie | None = None
        self._movie_buffer: QBuffer | None = None  # the movie reads from it, so it must outlive the movie
        self.setProperty("selected", False)

    def set_selected(self, selected: bool) -> None:
        """Mark this key as the one being edited (outlined by the theme)."""
        self.setProperty("selected", selected)
        if (style := self.style()) is not None:
            style.unpolish(self)
            style.polish(self)

    def _build_copy_badge(self) -> QLabel:
        badge = QLabel(self)
        badge.setObjectName("keyCopyBadge")
        badge.setPixmap(_badge_pixmap())
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(_BADGE_SIZE, _BADGE_SIZE)
        badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        badge.move(_RUN_MARGIN, self.height() - _BADGE_SIZE - _RUN_MARGIN)
        badge.hide()
        return badge

    def set_scope(self, scope: str) -> None:
        """Tag drags from this key with the device and page they belong to; drops from another scope are ignored."""
        self._scope = scope

    def set_draggable(self, draggable: bool) -> None:
        """Whether this key can be dragged and cleared (keys with a card can; empty ones cannot)."""
        self._draggable = draggable
        if not draggable:
            self._delete_button.hide()
        elif self.underMouse() or self.hasFocus():
            self._show_hover_buttons()

    def _build_delete_button(self) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName("keyDeleteButton")
        button.setIcon(_delete_icon())
        button.setIconSize(QSize(_DELETE_ICON_PX, _DELETE_ICON_PX))
        button.setToolTip("Clear this key")
        button.setAutoRaise(True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setFixedSize(_RUN_SIZE, _RUN_SIZE)
        button.setAcceptDrops(False)
        button.clicked.connect(lambda _checked=False: self.delete_clicked.emit(self.index))
        button.hide()
        button.move(self._delete_position())
        return button

    def _delete_position(self) -> QPoint:
        return QPoint(self.width() - _RUN_SIZE - _RUN_MARGIN, self.height() - _RUN_SIZE - _RUN_MARGIN)

    def _show_hover_buttons(self) -> None:
        self._run_button.show()
        self._run_button.raise_()
        if self._draggable:
            self._delete_button.show()
            self._delete_button.raise_()

    def _hide_hover_buttons(self) -> None:
        self._run_button.hide()
        self._delete_button.hide()

    def _build_run_button(self) -> _RunButton:
        button = _RunButton(self)
        button.setObjectName("keyRunButton")
        self._apply_play_icon(button)
        button.setToolTip("Run this key")
        button.setAutoRaise(True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setFixedSize(_RUN_SIZE, _RUN_SIZE)
        button.setAcceptDrops(False)  # a drop on the play button falls through to the key
        button.clicked.connect(lambda _checked=False: self.run_clicked.emit(self.index))
        button.long_pressed.connect(lambda: self.long_pressed.emit(self.index))
        button.hide()
        button.move(self._run_position())
        return button

    def _apply_play_icon(self, button: QToolButton) -> None:
        """Draw the play-circle from the shipped SVG in the palette's text colour; a "▶" glyph if it is unusable."""
        icon = _play_icon(self.palette().buttonText().color().name())
        if icon is None:
            button.setIcon(QIcon())
            button.setText("▶")
        else:
            button.setText("")
            button.setIcon(icon)
            button.setIconSize(QSize(_RUN_ICON_PX, _RUN_ICON_PX))

    def changeEvent(self, e: QEvent | None) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Redraw the play icon when the theme's palette changes."""
        super().changeEvent(e)
        if e is not None and e.type() == QEvent.Type.PaletteChange and hasattr(self, "_run_button"):
            self._apply_play_icon(self._run_button)

    def _run_position(self) -> QPoint:
        return QPoint(self.width() - _RUN_SIZE - _RUN_MARGIN, _RUN_MARGIN)

    def resizeEvent(self, a0: QResizeEvent | None) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Keep the play button in the top-right corner and the delete button in the bottom-right one."""
        super().resizeEvent(a0)
        self._run_button.move(self._run_position())
        self._delete_button.move(self._delete_position())
        self._copy_badge.move(_RUN_MARGIN, self.height() - _BADGE_SIZE - _RUN_MARGIN)

    def enterEvent(self, event: QEnterEvent | None) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Show the play (and, on a key with a card, delete) button while the pointer is over the key."""
        super().enterEvent(event)
        self._show_hover_buttons()

    def keyPressEvent(self, e: QKeyEvent | None) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """F2 (or the Menu key) selects the key for editing, as clicking it does."""
        if e is not None and e.key() in (Qt.Key.Key_F2, Qt.Key.Key_Menu):
            self.edit_clicked.emit(self.index)
            return
        super().keyPressEvent(e)

    def focusInEvent(self, e: QFocusEvent | None) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Show the hover buttons while the key has keyboard focus, so they are visible without a pointer."""
        super().focusInEvent(e)
        self._show_hover_buttons()

    def focusOutEvent(self, e: QFocusEvent | None) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Hide the hover buttons again unless the pointer is still on the key."""
        super().focusOutEvent(e)
        if not self.underMouse():
            self._hide_hover_buttons()

    def leaveEvent(self, a0: QEvent | None) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Hide the hover buttons once the pointer is outside the key and its buttons."""
        super().leaveEvent(a0)
        if not self.underMouse():
            self._hide_hover_buttons()

    def set_label(self, label: str) -> None:
        """Update the display label (shows label if set, otherwise key number)."""
        self._label = label
        if not self.icon().isNull():
            return  # image takes precedence; label shown as overlay only if no image
        self._refresh_text()

    def set_image(self, image_b64: str | None) -> None:
        """Set or clear the key image from a base64-encoded PNG."""
        if not image_b64:
            self._poster = None
            self.setIcon(QIcon())
            self.setIconSize(QSize(0, 0))
            self._refresh_text()
            return
        raw = base64.b64decode(image_b64)
        pixmap = QPixmap()
        pixmap.loadFromData(QByteArray(raw))
        self.setIcon(QIcon(pixmap))
        self.setIconSize(QSize(_ICON_PX, _ICON_PX))
        self.setText("")
        self._poster = pixmap

    def set_animation(self, data: bytes | None) -> None:
        """Play an animated image (WebP or GIF bytes) as the key face; None or unreadable data shows the poster."""
        self._release_movie()
        if not data:
            return
        buffer = QBuffer(self)
        buffer.setData(QByteArray(data))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        movie = QMovie(self)
        movie.setDevice(buffer)
        if not movie.isValid():
            movie.deleteLater()
            buffer.deleteLater()
            return
        self._movie, self._movie_buffer = movie, buffer
        movie.frameChanged.connect(self._on_movie_frame)
        movie.start()
        if not self.isVisible():
            movie.setPaused(True)

    def _release_movie(self) -> None:
        movie, buffer = self._movie, self._movie_buffer
        self._movie = self._movie_buffer = None
        if movie is None or buffer is None:
            return
        movie.stop()
        movie.deleteLater()
        buffer.close()
        buffer.deleteLater()
        self._restore_poster()

    def _restore_poster(self) -> None:
        if self._poster is None:
            self.setIcon(QIcon())
            self.setIconSize(QSize(0, 0))
            self._refresh_text()
            return
        self.setIcon(QIcon(self._poster))
        self.setIconSize(QSize(_ICON_PX, _ICON_PX))

    def _on_movie_frame(self) -> None:
        if self._movie is None:
            return
        self.setIcon(QIcon(self._movie.currentPixmap()))
        self.setIconSize(QSize(_ICON_PX, _ICON_PX))
        self.setText("")

    def hideEvent(self, a0: QHideEvent | None) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Pause the animation while the key is not on screen."""
        super().hideEvent(a0)
        if self._movie is not None:
            self._movie.setPaused(True)

    def showEvent(self, a0: QShowEvent | None) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Resume the animation when the key is on screen again."""
        super().showEvent(a0)
        if self._movie is not None:
            self._movie.setPaused(False)

    # ------------------------------------------------------------------
    # Drag source
    # ------------------------------------------------------------------

    def mousePressEvent(self, e: QMouseEvent | None) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Remember where a left press began, so a move can become a drag."""
        self._dragged = False
        if e is not None and e.button() == Qt.MouseButton.LeftButton:
            self._drag_start = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e: QMouseEvent | None) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Start dragging this key once the pointer has moved past the platform's drag distance."""
        if self._dragged:
            return
        start = self._drag_start
        if (
            e is not None
            and start is not None
            and self._draggable
            and e.buttons() & Qt.MouseButton.LeftButton
            and (e.position().toPoint() - start).manhattanLength() >= QApplication.startDragDistance()
        ):
            self._drag_start = None
            self._start_drag()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e: QMouseEvent | None) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """End a press; the release that ends a drag is not a click."""
        self._drag_start = None
        if self._dragged:
            self._dragged = False
            self.setDown(False)
            return
        super().mouseReleaseEvent(e)

    def _start_drag(self) -> None:
        mime = QMimeData()
        payload = {"key_index": self.index, "scope": self._scope}
        mime.setData(_KEY_MIME_TYPE, QByteArray(json.dumps(payload).encode()))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(self.grab())
        drag.setHotSpot(QPoint(self.width() // 2, self.height() // 2))
        self._dragged = True
        self.setDown(False)  # the release that ends the drag must not click the key
        _exec_drag(drag)
        self.setDown(False)

    # ------------------------------------------------------------------
    # Drag-and-drop target
    # ------------------------------------------------------------------

    def dragEnterEvent(self, a0: Any) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Accept card drops and key drags."""
        mime = a0.mimeData()
        if mime.hasFormat(CARD_MIME_TYPE) or mime.hasFormat(_KEY_MIME_TYPE):
            a0.acceptProposedAction()
            self._show_copy_badge(a0)

    def dragMoveEvent(self, a0: Any) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Show the copy badge while Ctrl is held over a key drag."""
        mime = a0.mimeData()
        if mime.hasFormat(CARD_MIME_TYPE) or mime.hasFormat(_KEY_MIME_TYPE):
            a0.acceptProposedAction()
            self._show_copy_badge(a0)

    def dragLeaveEvent(self, a0: Any) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Hide the copy badge when the drag leaves."""
        self._copy_badge.hide()
        super().dragLeaveEvent(a0)

    def dropEvent(self, a0: Any) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Assign a dropped card, or move/copy a dropped key onto this one."""
        self._copy_badge.hide()
        mime = a0.mimeData()
        if mime.hasFormat(_KEY_MIME_TYPE):
            self._drop_key(a0)
            return
        raw: bytes = bytes(mime.data(CARD_MIME_TYPE))
        try:
            data: dict[str, Any] = json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError):
            return
        self.card_dropped.emit(self.index, data)
        a0.acceptProposedAction()

    def _drop_key(self, event: Any) -> None:
        try:
            payload = json.loads(bytes(event.mimeData().data(_KEY_MIME_TYPE)).decode())
            source, scope = payload.get("key_index"), payload.get("scope", "")
        except (ValueError, UnicodeDecodeError, AttributeError):
            return
        if scope != self._scope:
            return  # dragged from another page or device (the page may have changed mid-drag)
        if isinstance(source, bool) or not isinstance(source, int) or source == self.index:
            return
        copy = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        event.setDropAction(Qt.DropAction.CopyAction if copy else Qt.DropAction.MoveAction)
        event.accept()
        self.key_dropped.emit(source, self.index, copy)

    def _show_copy_badge(self, event: Any) -> None:
        is_key = event.mimeData().hasFormat(_KEY_MIME_TYPE)
        copy = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        self._copy_badge.setVisible(is_key and copy)
        if self._copy_badge.isVisible():
            self._copy_badge.raise_()

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _refresh_text(self) -> None:
        self.setText(self._label or str(self.index + 1))


class ButtonGridWidget(QWidget):
    """Renders a placeholder key grid sized from the active device's key count."""

    key_pressed = pyqtSignal(int)
    key_edit_requested = pyqtSignal(int)
    key_delete_requested = pyqtSignal(int)
    key_long_pressed = pyqtSignal(int)
    card_dropped = pyqtSignal(int, object)
    key_moved = pyqtSignal(int, int, bool)  # from index, to index, copy

    def __init__(self) -> None:
        super().__init__()
        self._draggable: frozenset[int] = frozenset()
        self._scope = ""
        self._grid_layout = QGridLayout()
        self._grid_layout.setSpacing(4)
        self._key_info: list[KeyInfo] = []
        self._buttons: dict[int, KeyButtonWidget] = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.addLayout(self._grid_layout)
        outer.addStretch()

    @property
    def key_count(self) -> int:
        """The number of keys currently rendered."""
        return len(self._key_info)

    def build(self, key_information: list[KeyInfo]):
        self._key_info = key_information

        # clear existing widgets
        while (item := self._grid_layout.takeAt(0)) is not None:
            if widget := item.widget():
                widget.deleteLater()
        self._buttons = {}

        for index in range(self.key_count):
            key = self._key_info[index]
            row, col = key.row, key.col
            button = KeyButtonWidget(index, key)
            button.run_clicked.connect(self.key_pressed.emit)
            button.edit_clicked.connect(self.key_edit_requested.emit)
            button.delete_clicked.connect(self.key_delete_requested.emit)
            button.long_pressed.connect(self.key_long_pressed.emit)
            button.card_dropped.connect(self.card_dropped.emit)
            button.key_dropped.connect(self.key_moved.emit)
            button.set_draggable(index in self._draggable)
            button.set_scope(self._scope)
            self._grid_layout.addWidget(button, row, col)
            self._buttons[index] = button

    def set_drag_scope(self, scope: str) -> None:
        """Tag drags with the active device and page, so a drop after the page changed is refused."""
        self._scope = scope
        for button in self._buttons.values():
            button.set_scope(scope)

    def set_draggable_keys(self, indexes: Collection[int]) -> None:
        """Make exactly these keys draggable (remembered across rebuilds)."""
        self._draggable = frozenset(indexes)
        for index, button in self._buttons.items():
            button.set_draggable(index in self._draggable)

    def set_selected(self, key_index: int | None) -> None:
        """Outline the key being edited; None (or an unknown index) outlines nothing."""
        for index, button in self._buttons.items():
            button.set_selected(index == key_index)

    def set_key_label(self, key_index: int, label: str) -> None:
        """Update the display label for a single key button."""
        if button := self._buttons.get(key_index):
            button.set_label(label)

    def set_key_image(self, key_index: int, image_b64: str | None) -> None:
        """Update the image for a single key button from a base64-encoded PNG."""
        if button := self._buttons.get(key_index):
            button.set_image(image_b64)

    def set_key_animation(self, key_index: int, animation_b64: str | None) -> None:
        """Play a base64-encoded animated image on a key; None or an empty string goes back to its static image."""
        if button := self._buttons.get(key_index):
            button.set_animation(base64.b64decode(animation_b64) if animation_b64 else None)
