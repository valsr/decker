import base64
import io
import json

import pytest
from assertpy import assert_that
from PIL import Image
from PyQt6.QtCore import (
    QBuffer,
    QByteArray,
    QEvent,
    QIODevice,
    QMimeData,
    QPoint,
    QPointF,
    Qt,
)
from PyQt6.QtGui import (
    QColor,
    QContextMenuEvent,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QEnterEvent,
    QFocusEvent,
    QMouseEvent,
    QMovie,
    QPalette,
)
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMenu

import decker.client.widgets.button_grid as button_grid_module
from decker.animation.factory import AnimationFactory
from decker.animation.model import AnimationSpec
from decker.client.themes import BUNDLED, render_stylesheet
from decker.client.widgets.button_grid import ButtonGridWidget, KeyButtonWidget
from decker.common.model import KeyInfo
from decker.server.appearance import resolve_appearance


def _make_keys(count: int, rows: int = 3, cols: int = 5) -> list[KeyInfo]:
    keys = []
    for i in range(count):
        keys.append(KeyInfo(row=i // cols, col=i % cols, pixel_width=72, pixel_height=72, type="key"))
    return keys


class TestButtonGridWidget:
    def test_build_creates_expected_number_of_buttons(self, qapp):  # pylint: disable=unused-argument
        grid = ButtonGridWidget()
        grid.build(_make_keys(15))
        assert_that(grid.key_count).is_equal_to(15)
        assert_that(grid._grid_layout.count()).is_equal_to(15)  # pylint: disable=protected-access

    def test_build_rebuilds_grid(self, qapp):  # pylint: disable=unused-argument
        grid = ButtonGridWidget()
        grid.build(_make_keys(6))
        grid.build(_make_keys(8))
        assert_that(grid._grid_layout.count()).is_equal_to(8)  # pylint: disable=protected-access

    def _button(self, grid: ButtonGridWidget, index: int) -> KeyButtonWidget:
        layout_item = grid._grid_layout.itemAt(index)  # pylint: disable=protected-access
        assert layout_item is not None
        button = layout_item.widget()
        assert isinstance(button, KeyButtonWidget)
        return button

    def test_clicking_key_emits_key_edit_requested_only(self, qapp):  # pylint: disable=unused-argument
        grid = ButtonGridWidget()
        grid.build(_make_keys(6))
        pressed: list[int] = []
        edits: list[int] = []
        grid.key_pressed.connect(pressed.append)
        grid.key_edit_requested.connect(edits.append)

        self._button(grid, 2).click()

        assert_that(edits).is_equal_to([2])
        assert_that(pressed).is_empty()

    def test_clicking_the_play_button_emits_key_pressed_only(self, qapp):  # pylint: disable=unused-argument
        grid = ButtonGridWidget()
        grid.build(_make_keys(6))
        pressed: list[int] = []
        edits: list[int] = []
        grid.key_pressed.connect(pressed.append)
        grid.key_edit_requested.connect(edits.append)

        self._button(grid, 4)._run_button.click()  # pylint: disable=protected-access

        assert_that(pressed).is_equal_to([4])
        assert_that(edits).is_empty()

    def test_every_key_gets_a_play_button(self, qapp):  # pylint: disable=unused-argument
        grid = ButtonGridWidget()
        grid.build(_make_keys(6))

        for index in range(6):
            assert_that(
                self._button(grid, index)._run_button.toolTip()
            ).is_equal_to(  # pylint: disable=protected-access
                "Run this key"
            )

    def test_rebuilding_while_hovered_leaves_no_stale_play_button(self, qapp):  # pylint: disable=unused-argument
        grid = ButtonGridWidget()
        grid.build(_make_keys(6))
        grid.show()
        _enter(self._button(grid, 2))
        grid.build(_make_keys(4))
        qapp.processEvents()

        assert_that(grid.key_count).is_equal_to(4)
        for index in range(4):
            assert_that(
                self._button(grid, index)._run_button.isVisible()
            ).is_false()  # pylint: disable=protected-access

    def test_set_key_label_updates_button_text(self, qapp):  # pylint: disable=unused-argument
        grid = ButtonGridWidget()
        grid.build(_make_keys(6))
        grid.set_key_label(0, "42")
        button = grid._buttons.get(0)  # pylint: disable=protected-access
        assert button is not None
        assert_that(button.text()).is_equal_to("42")

    def test_set_key_label_for_missing_key_does_not_raise(self, qapp):  # pylint: disable=unused-argument
        grid = ButtonGridWidget()
        grid.build(_make_keys(6))
        grid.set_key_label(99, "x")  # should not raise


class TestKeyButtonWidget:
    def test_set_label_updates_display(self, qapp):  # pylint: disable=unused-argument
        btn = KeyButtonWidget(0, KeyInfo(row=0, col=0, pixel_width=72, pixel_height=72, type="key"))
        btn.set_label("99")
        assert_that(btn.text()).is_equal_to("99")

    def test_default_text_is_key_number(self, qapp):  # pylint: disable=unused-argument
        btn = KeyButtonWidget(4, KeyInfo(row=0, col=4, pixel_width=72, pixel_height=72, type="key"))
        assert_that(btn.text()).is_equal_to("5")


def _key(index: int = 3) -> KeyButtonWidget:
    return KeyButtonWidget(index, KeyInfo(row=0, col=0, pixel_width=72, pixel_height=72, type="key"))


def _enter(widget) -> None:
    QApplication.sendEvent(widget, QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))


def _leave(widget) -> None:
    QApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))


class TestKeyDeleteButton:  # pylint: disable=protected-access,unused-argument
    def test_a_key_with_a_card_shows_the_delete_button_on_hover_only(self, qapp):
        button = _key()
        button.set_draggable(True)
        button.show()
        assert_that(button._delete_button.isVisible()).is_false()

        _enter(button)
        assert_that(button._delete_button.isVisible()).is_true()
        _leave(button)
        assert_that(button._delete_button.isVisible()).is_false()

    def test_an_empty_key_never_shows_the_delete_button(self, qapp):
        button = _key()
        button.show()

        _enter(button)

        assert_that(button._run_button.isVisible()).is_true()
        assert_that(button._delete_button.isVisible()).is_false()

    def test_the_delete_button_sits_in_the_bottom_right_corner(self, qapp):
        button = _key()
        button.show()

        geometry = button._delete_button.geometry()

        assert_that((geometry.width(), geometry.height())).is_equal_to((30, 30))
        assert_that(button.width() - geometry.right() - 1).is_equal_to(4)
        assert_that(button.height() - geometry.bottom() - 1).is_equal_to(4)

    def test_clicking_it_emits_delete_clicked_and_not_edit_clicked(self, qapp):
        button = _key(6)
        deleted: list[int] = []
        edited: list[int] = []
        button.delete_clicked.connect(deleted.append)
        button.edit_clicked.connect(edited.append)

        button._delete_button.click()

        assert_that(deleted).is_equal_to([6])
        assert_that(edited).is_empty()

    def test_the_grid_forwards_it_as_key_delete_requested(self, qapp):
        grid = ButtonGridWidget()
        grid.build(_make_keys(5))
        requested: list[int] = []
        grid.key_delete_requested.connect(requested.append)

        grid._buttons[2]._delete_button.click()

        assert_that(requested).is_equal_to([2])


class TestKeyPlayButton:  # pylint: disable=protected-access,unused-argument
    def test_play_button_is_hidden_until_the_pointer_enters(self, qapp):
        button = _key()
        button.show()

        assert_that(button._run_button.isVisible()).is_false()

    def test_play_button_shows_on_enter_and_hides_on_leave(self, qapp):
        button = _key()
        button.show()

        _enter(button)
        assert_that(button._run_button.isVisible()).is_true()
        _leave(button)
        assert_that(button._run_button.isVisible()).is_false()

    def test_play_button_sits_in_the_top_right_corner(self, qapp):
        button = _key()
        button.show()

        geometry = button._run_button.geometry()

        assert_that((geometry.width(), geometry.height())).is_equal_to((30, 30))
        assert_that(button.width() - geometry.right() - 1).is_equal_to(4)
        assert_that(geometry.top()).is_equal_to(4)

    def test_play_button_has_a_tooltip_and_something_to_look_at(self, qapp):
        play_button = _key()._run_button

        assert_that(play_button.toolTip()).is_equal_to("Run this key")
        assert_that(play_button.icon().isNull() and not play_button.text()).is_false()

    def test_play_button_click_emits_run_clicked_with_the_index_and_not_edit_clicked(self, qapp):
        button = _key(7)
        runs: list[int] = []
        edits: list[int] = []
        button.run_clicked.connect(runs.append)
        button.edit_clicked.connect(edits.append)

        button._run_button.click()

        assert_that(runs).is_equal_to([7])
        assert_that(edits).is_empty()

    def test_key_click_emits_edit_clicked_and_not_run_clicked(self, qapp):
        button = _key(7)
        runs: list[int] = []
        edits: list[int] = []
        button.run_clicked.connect(runs.append)
        button.edit_clicked.connect(edits.append)

        button.click()

        assert_that(edits).is_equal_to([7])
        assert_that(runs).is_empty()

    def test_pressing_the_play_button_starts_its_long_press_timer(self, qapp):
        button = _key()
        button.show()
        _enter(button)

        QTest.mousePress(button._run_button, Qt.MouseButton.LeftButton)  # type: ignore[call-overload]

        assert_that(button._run_button._long_press_timer.isActive()).is_true()
        QTest.mouseRelease(button._run_button, Qt.MouseButton.LeftButton)  # type: ignore[call-overload]
        assert_that(button._run_button._long_press_timer.isActive()).is_false()

    def test_holding_the_play_button_emits_long_pressed_and_no_run_click(self, qapp):
        button = _key(3)
        button.show()
        _enter(button)
        long_presses: list[int] = []
        runs: list[int] = []
        button.long_pressed.connect(long_presses.append)
        button.run_clicked.connect(runs.append)

        QTest.mousePress(button._run_button, Qt.MouseButton.LeftButton)  # type: ignore[call-overload]
        button._run_button._long_press_timer.timeout.emit()
        QTest.mouseRelease(button._run_button, Qt.MouseButton.LeftButton)  # type: ignore[call-overload]

        assert_that(long_presses).is_equal_to([3])
        assert_that(runs).is_empty()

    def test_a_mouse_press_on_the_key_never_starts_a_long_press(self, qapp):
        button = _key()
        button.show()
        long_presses: list[int] = []
        button.long_pressed.connect(long_presses.append)

        QTest.mousePress(button, Qt.MouseButton.LeftButton)  # type: ignore[call-overload]
        QTest.mouseRelease(button, Qt.MouseButton.LeftButton)  # type: ignore[call-overload]

        assert_that(long_presses).is_empty()

    def test_play_button_does_not_swallow_drops(self, qapp):
        button = _key(4)
        button.show()
        dropped: list[tuple[int, object]] = []
        button.card_dropped.connect(lambda i, d: dropped.append((i, d)))
        mime = QMimeData()
        mime.setData("application/x-decker-card", QByteArray(json.dumps({"plugin_uuid": "p"}).encode()))
        center = QPointF(button._run_button.geometry().center())

        event = QDropEvent(
            center, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
        )
        button.dropEvent(event)

        assert_that(button._run_button.acceptDrops()).is_false()  # so Qt hands the drop to the key
        assert_that(dropped).is_equal_to([(4, {"plugin_uuid": "p"})])


class TestPlayButtonTheming:  # pylint: disable=protected-access,unused-argument
    @pytest.mark.parametrize("theme", ["dark", "light"])
    def test_glyph_fits_the_play_button_under_the_bundled_theme(self, qapp, theme):
        button = _key()
        button.setStyleSheet(render_stylesheet(BUNDLED[theme]))
        button.show()
        qapp.processEvents()

        hint = button._run_button.sizeHint()

        # The global QToolButton padding used to leave ~6px for a 10px glyph, clipping it to a smudge.
        assert_that(hint.width()).described_as(f"{theme} width").is_less_than_or_equal_to(30)
        assert_that(hint.height()).described_as(f"{theme} height").is_less_than_or_equal_to(30)

    @pytest.mark.parametrize("theme", ["dark", "light"])
    def test_play_button_has_its_own_background_so_it_reads_over_a_key_image(self, qapp, theme):
        button = _key()
        button.setStyleSheet(render_stylesheet(BUNDLED[theme]))
        button.show()
        _enter(button)
        qapp.processEvents()

        plain = button.grab().toImage()
        play_button = button._run_button.geometry()
        corner = plain.pixelColor(play_button.left() + 2, play_button.center().y())
        key_bg = plain.pixelColor(button.width() // 2, 6)

        assert_that(corner.getRgb()).described_as("play_button disc differs from the key face").is_not_equal_to(
            key_bg.getRgb()
        )


class TestKeyboardAccessToEdit:  # pylint: disable=protected-access,unused-argument
    @pytest.mark.parametrize("key", [Qt.Key.Key_F2, Qt.Key.Key_Menu])
    def test_edit_keys_emit_edit_clicked_once_and_do_not_run(self, qapp, key):
        button = _key(6)
        edits: list[int] = []
        runs: list[int] = []
        button.edit_clicked.connect(edits.append)
        button.run_clicked.connect(runs.append)

        QTest.keyClick(button, key)  # type: ignore[call-overload]

        assert_that(edits).is_equal_to([6])
        assert_that(runs).is_empty()

    def test_space_selects_the_key(self, qapp):
        button = _key(6)
        edits: list[int] = []
        runs: list[int] = []
        button.edit_clicked.connect(edits.append)
        button.run_clicked.connect(runs.append)

        QTest.keyClick(button, Qt.Key.Key_Space)  # type: ignore[call-overload]

        assert_that(edits).is_equal_to([6])
        assert_that(runs).is_empty()

    def test_focus_shows_the_play_button_so_keyboard_users_can_see_the_affordance(self, qapp):
        button = _key()
        button.show()

        QApplication.sendEvent(button, QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason))
        assert_that(button._run_button.isVisible()).is_true()
        QApplication.sendEvent(button, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.TabFocusReason))
        assert_that(button._run_button.isVisible()).is_false()

    def test_losing_focus_keeps_the_play_button_while_the_pointer_is_still_on_the_key(self, qapp):
        button = _key()
        button.show()
        _enter(button)
        QApplication.sendEvent(button, QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason))
        button.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)

        QApplication.sendEvent(button, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.TabFocusReason))

        assert_that(button._run_button.isVisible()).is_true()


class TestPlayButtonIcon:  # pylint: disable=protected-access,unused-argument
    def _ink(self, play_button) -> list[tuple[int, int, int]]:
        image = play_button.icon().pixmap(14, 14).toImage()
        pixels = []
        for y in range(image.height()):
            for x in range(image.width()):
                colour = image.pixelColor(x, y)
                if colour.alpha() > 200:
                    pixels.append((colour.red(), colour.green(), colour.blue()))
        return pixels

    def test_the_shipped_svg_is_the_play_circle_icon(self, qapp):
        assert_that(button_grid_module._PLAY_ICON.name).is_equal_to("play-circle.svg")
        assert_that(button_grid_module._PLAY_ICON.is_file()).is_true()
        assert_that(button_grid_module._PLAY_ICON.read_bytes()).contains(b"<svg")

    def test_play_button_drawing_is_21_px_inside_its_30_px_button(self, qapp):
        play_button = _key()._run_button

        assert_that((play_button.iconSize().width(), play_button.iconSize().height())).is_equal_to((21, 21))
        assert_that((play_button.width(), play_button.height())).is_equal_to((30, 30))

    def test_play_button_uses_the_svg_not_a_glyph(self, qapp):
        play_button = _key()._run_button

        assert_that(play_button.icon().isNull()).is_false()
        assert_that(play_button.text()).is_equal_to("")

    def test_play_button_icon_is_filled_with_the_palette_text_colour(self, qapp):
        button = _key()
        palette = QPalette(button.palette())
        palette.setColor(QPalette.ColorRole.ButtonText, QColor("#00ff00"))
        button.setPalette(palette)

        ink = self._ink(button._run_button)

        assert_that(ink).is_not_empty()
        assert_that(set(ink)).is_equal_to({(0, 255, 0)})

    def test_play_button_icon_follows_a_palette_change(self, qapp):
        button = _key()
        for colour, expected in (("#ff0000", (255, 0, 0)), ("#0000ff", (0, 0, 255))):
            palette = QPalette(button.palette())
            palette.setColor(QPalette.ColorRole.ButtonText, QColor(colour))
            button.setPalette(palette)

            assert_that(set(self._ink(button._run_button))).described_as(colour).is_equal_to({expected})

    def test_missing_svg_falls_back_to_the_glyph(self, qapp, monkeypatch, tmp_path):
        monkeypatch.setattr(button_grid_module, "_PLAY_ICON", tmp_path / "nope.svg")

        play_button = _key()._run_button

        assert_that(play_button.text()).is_equal_to("▶")
        assert_that(play_button.icon().isNull()).is_true()

    def test_unreadable_svg_falls_back_to_the_glyph(self, qapp, monkeypatch, tmp_path):
        broken = tmp_path / "edit.svg"
        broken.write_bytes(b"this is not an svg")
        monkeypatch.setattr(button_grid_module, "_PLAY_ICON", broken)

        play_button = _key()._run_button

        assert_that(play_button.text()).is_equal_to("▶")

    def test_palette_change_after_a_fallback_does_not_crash(self, qapp, monkeypatch, tmp_path):
        monkeypatch.setattr(button_grid_module, "_PLAY_ICON", tmp_path / "nope.svg")
        button = _key()

        palette = QPalette(button.palette())
        palette.setColor(QPalette.ColorRole.ButtonText, QColor("#ff0000"))
        button.setPalette(palette)

        assert_that(button._run_button.text()).is_equal_to("▶")

    @pytest.mark.parametrize("theme", ["dark", "light"])
    def test_the_svg_play_button_still_fits_its_box_under_the_bundled_themes(self, qapp, theme):
        button = _key()
        button.setStyleSheet(render_stylesheet(BUNDLED[theme]))
        button.show()
        qapp.processEvents()

        hint = button._run_button.sizeHint()

        assert_that(hint.width()).is_less_than_or_equal_to(30)
        assert_that(hint.height()).is_less_than_or_equal_to(30)


class TestSelectedKey:
    def _selected(self, grid: ButtonGridWidget) -> list[int]:
        buttons = grid._buttons  # pylint: disable=protected-access
        return [i for i, b in buttons.items() if b.property("selected") is True]

    def test_set_selected_marks_only_that_key(self, qapp):  # pylint: disable=unused-argument
        grid = ButtonGridWidget()
        grid.build(_make_keys(6))

        grid.set_selected(2)
        assert_that(self._selected(grid)).is_equal_to([2])
        grid.set_selected(4)
        assert_that(self._selected(grid)).is_equal_to([4])

    def test_none_clears_the_selection(self, qapp):  # pylint: disable=unused-argument
        grid = ButtonGridWidget()
        grid.build(_make_keys(6))
        grid.set_selected(2)

        grid.set_selected(None)

        assert_that(self._selected(grid)).is_empty()

    def test_unknown_index_selects_nothing(self, qapp):  # pylint: disable=unused-argument
        grid = ButtonGridWidget()
        grid.build(_make_keys(3))
        grid.set_selected(1)

        grid.set_selected(99)

        assert_that(self._selected(grid)).is_empty()

    def test_rebuilding_the_grid_clears_the_selection(self, qapp):  # pylint: disable=unused-argument
        grid = ButtonGridWidget()
        grid.build(_make_keys(6))
        grid.set_selected(2)

        grid.build(_make_keys(6))

        assert_that(self._selected(grid)).is_empty()

    def test_theme_outlines_the_selected_key(self, qapp):  # pylint: disable=unused-argument
        stylesheet = render_stylesheet(next(iter(BUNDLED.values())))
        assert_that(stylesheet).contains('KeyButtonWidget[selected="true"]')


_KEY_MIME = "application/x-decker-key"
_GREEN = (46, 204, 113)


def _key_drop(
    source: int, modifiers=Qt.KeyboardModifier.NoModifier, payload: bytes | None = None, scope: str = ""
) -> QDropEvent:
    mime = QMimeData()
    body = json.dumps({"key_index": source, "scope": scope}).encode()
    mime.setData(_KEY_MIME, QByteArray(payload if payload is not None else body))
    event = QDropEvent(QPointF(5, 5), Qt.DropAction.MoveAction, mime, Qt.MouseButton.LeftButton, modifiers)
    event.keep_alive = mime  # type: ignore[attr-defined]  # the event only borrows the mime data
    return event


def _drag_move(widget, modifiers=Qt.KeyboardModifier.NoModifier, mime_type: str = _KEY_MIME) -> None:
    mime = QMimeData()
    mime.setData(mime_type, QByteArray(b"{}"))
    event = QDragMoveEvent(QPoint(5, 5), Qt.DropAction.MoveAction, mime, Qt.MouseButton.LeftButton, modifiers)
    event.keep_alive = mime  # type: ignore[attr-defined]
    widget.dragMoveEvent(event)


def _badge_ink(badge) -> list[tuple[int, int, int]]:
    pixmap = badge.pixmap()
    assert pixmap is not None
    image = pixmap.toImage()
    return [
        (c.red(), c.green(), c.blue())
        for y in range(image.height())
        for x in range(image.width())
        if (c := image.pixelColor(x, y)).alpha() > 200
    ]


class TestDragSource:  # pylint: disable=protected-access,unused-argument
    @pytest.fixture(autouse=True)
    def _capture_drags(self, monkeypatch):
        self.drags: list = []
        monkeypatch.setattr(button_grid_module, "_exec_drag", self.drags.append)

    @staticmethod
    def _press_and_move(button, distance: int) -> None:
        QTest.mousePress(button, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(10, 40))  # type: ignore[call-overload]
        move = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(10 + distance, 40),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(button, move)

    def test_moving_past_the_drag_distance_starts_a_drag_with_the_key_payload(self, qapp):
        button = _key(5)
        button.set_draggable(True)

        self._press_and_move(button, QApplication.startDragDistance() + 5)

        assert_that(self.drags).is_length(1)
        mime = self.drags[0].mimeData()
        assert_that(mime.hasFormat(_KEY_MIME)).is_true()
        assert_that(json.loads(bytes(mime.data(_KEY_MIME)).decode())).is_equal_to({"key_index": 5, "scope": ""})
        assert_that(self.drags[0].pixmap().isNull()).is_false()

    def test_a_short_move_is_still_a_click(self, qapp):
        button = _key(5)
        button.set_draggable(True)
        edits: list[int] = []
        button.edit_clicked.connect(edits.append)

        self._press_and_move(button, 1)
        QTest.mouseRelease(button, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(11, 40))  # type: ignore[call-overload]

        assert_that(self.drags).is_empty()
        assert_that(edits).is_equal_to([5])

    def test_a_key_that_is_not_draggable_never_drags(self, qapp):
        button = _key(5)

        self._press_and_move(button, 50)

        assert_that(self.drags).is_empty()

    def test_a_drag_does_not_also_select_on_release(self, qapp):
        button = _key(5)
        button.set_draggable(True)
        edits: list[int] = []
        button.edit_clicked.connect(edits.append)

        self._press_and_move(button, 50)
        QTest.mouseRelease(button, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(60, 40))  # type: ignore[call-overload]

        assert_that(self.drags).is_length(1)
        assert_that(edits).is_empty()

    def test_only_one_drag_starts_per_press(self, qapp):
        button = _key(5)
        button.set_draggable(True)

        self._press_and_move(button, 50)
        QApplication.sendEvent(
            button,
            QMouseEvent(
                QEvent.Type.MouseMove,
                QPointF(80, 40),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )

        assert_that(self.drags).is_length(1)


class TestKeyDropTarget:  # pylint: disable=protected-access,unused-argument
    def test_a_key_drop_emits_the_source_target_and_no_copy(self, qapp):
        button = _key(7)
        dropped: list[tuple[int, int, bool]] = []
        button.key_dropped.connect(lambda a, b, c: dropped.append((a, b, c)))

        button.dropEvent(_key_drop(2))

        assert_that(dropped).is_equal_to([(2, 7, False)])

    def test_ctrl_makes_it_a_copy(self, qapp):
        button = _key(7)
        dropped: list[tuple[int, int, bool]] = []
        button.key_dropped.connect(lambda a, b, c: dropped.append((a, b, c)))

        button.dropEvent(_key_drop(2, Qt.KeyboardModifier.ControlModifier))

        assert_that(dropped).is_equal_to([(2, 7, True)])

    def test_dropping_a_key_on_itself_does_nothing(self, qapp):
        button = _key(7)
        dropped: list[tuple[int, int, bool]] = []
        button.key_dropped.connect(lambda a, b, c: dropped.append((a, b, c)))

        button.dropEvent(_key_drop(7))

        assert_that(dropped).is_empty()

    @pytest.mark.parametrize(
        "payload",
        [b"not json", b"{}", b'{"key_index": "2"}', b'{"key_index": true}', b'{"key_index": 1.5}', b"[2]", b"\xff"],
    )
    def test_garbage_payloads_emit_nothing(self, qapp, payload):
        button = _key(7)
        dropped: list[tuple[int, int, bool]] = []
        button.key_dropped.connect(lambda a, b, c: dropped.append((a, b, c)))

        button.dropEvent(_key_drop(2, payload=payload))

        assert_that(dropped).is_empty()

    def test_action_drops_still_work(self, qapp):
        button = _key(4)
        dropped: list[tuple[int, object]] = []
        button.card_dropped.connect(lambda i, d: dropped.append((i, d)))
        mime = QMimeData()
        mime.setData("application/x-decker-card", QByteArray(json.dumps({"plugin_uuid": "p"}).encode()))

        button.dropEvent(
            QDropEvent(
                QPointF(5, 5), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
            )
        )

        assert_that(dropped).is_equal_to([(4, {"plugin_uuid": "p"})])

    def test_a_key_drag_is_accepted_on_enter(self, qapp):
        button = _key(7)
        mime = QMimeData()
        mime.setData(_KEY_MIME, QByteArray(b"{}"))
        event = QDragEnterEvent(
            QPoint(5, 5), Qt.DropAction.MoveAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
        )
        event.keep_alive = mime  # type: ignore[attr-defined]

        button.dragEnterEvent(event)

        assert_that(event.isAccepted()).is_true()


class TestCopyBadge:  # pylint: disable=protected-access,unused-argument
    def test_the_badge_is_hidden_until_a_ctrl_drag_is_over_the_key(self, qapp):
        button = _key(7)
        button.show()

        assert_that(button._copy_badge.isVisible()).is_false()

    def test_ctrl_drag_over_shows_the_badge_bottom_left(self, qapp):
        button = _key(7)
        button.show()

        _drag_move(button, Qt.KeyboardModifier.ControlModifier)

        badge = button._copy_badge
        assert_that(badge.isVisible()).is_true()
        geometry = badge.geometry()
        assert_that(geometry.left()).is_equal_to(4)
        assert_that(button.height() - geometry.bottom() - 1).is_equal_to(4)
        assert_that((geometry.width(), geometry.height())).is_equal_to((30, 30))

    def test_releasing_ctrl_hides_it_and_pressing_it_again_shows_it(self, qapp):
        button = _key(7)
        button.show()

        _drag_move(button, Qt.KeyboardModifier.ControlModifier)
        _drag_move(button)
        assert_that(button._copy_badge.isVisible()).is_false()
        _drag_move(button, Qt.KeyboardModifier.ControlModifier)

        assert_that(button._copy_badge.isVisible()).is_true()

    def test_leaving_and_dropping_hide_it(self, qapp):
        button = _key(7)
        button.show()
        _drag_move(button, Qt.KeyboardModifier.ControlModifier)
        button.dragLeaveEvent(QDragLeaveEvent())
        assert_that(button._copy_badge.isVisible()).is_false()

        _drag_move(button, Qt.KeyboardModifier.ControlModifier)
        button.dropEvent(_key_drop(2, Qt.KeyboardModifier.ControlModifier))
        assert_that(button._copy_badge.isVisible()).is_false()

    def test_an_action_drag_with_ctrl_shows_no_badge(self, qapp):
        button = _key(7)
        button.show()

        _drag_move(button, Qt.KeyboardModifier.ControlModifier, "application/x-decker-card")

        assert_that(button._copy_badge.isVisible()).is_false()

    def test_the_badge_is_the_green_plus_circle_and_ignores_the_mouse(self, qapp):
        badge = _key(7)._copy_badge

        ink = _badge_ink(badge)

        assert_that(ink).is_not_empty()
        assert_that(all(max(abs(a - b) for a, b in zip(pixel, _GREEN)) <= 4 for pixel in ink)).is_true()
        assert_that(badge.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)).is_true()
        assert_that(button_grid_module._PLUS_ICON.name).is_equal_to("plus-circle.svg")


class TestGridDragWiring:  # pylint: disable=protected-access,unused-argument
    def test_set_draggable_keys_marks_exactly_those_keys(self, qapp):
        grid = ButtonGridWidget()
        grid.build(_make_keys(6))

        grid.set_draggable_keys({1, 4})

        assert_that([i for i, b in grid._buttons.items() if b._draggable]).is_equal_to([1, 4])
        grid.set_draggable_keys(set())
        assert_that([i for i, b in grid._buttons.items() if b._draggable]).is_empty()

    def test_the_draggable_set_survives_a_rebuild(self, qapp):
        grid = ButtonGridWidget()
        grid.build(_make_keys(6))
        grid.set_draggable_keys({2})

        grid.build(_make_keys(6))

        assert_that([i for i, b in grid._buttons.items() if b._draggable]).is_equal_to([2])

    def test_a_key_drop_is_forwarded_as_key_moved(self, qapp):
        grid = ButtonGridWidget()
        grid.build(_make_keys(6))
        moved: list[tuple[int, int, bool]] = []
        grid.key_moved.connect(lambda a, b, c: moved.append((a, b, c)))

        grid._buttons[3].dropEvent(_key_drop(1, Qt.KeyboardModifier.ControlModifier))

        assert_that(moved).is_equal_to([(1, 3, True)])


class TestDragScope:  # pylint: disable=protected-access,unused-argument
    @pytest.fixture(autouse=True)
    def _capture_drags(self, monkeypatch):
        self.drags: list = []
        monkeypatch.setattr(button_grid_module, "_exec_drag", self.drags.append)

    def test_the_drag_payload_carries_the_scope(self, qapp):
        button = _key(5)
        button.set_draggable(True)
        button.set_scope("dev1/p1")

        TestDragSource._press_and_move(button, 50)

        payload = json.loads(bytes(self.drags[0].mimeData().data(_KEY_MIME)).decode())
        assert_that(payload).is_equal_to({"key_index": 5, "scope": "dev1/p1"})

    def test_a_drop_from_another_scope_is_ignored(self, qapp):
        button = _key(7)
        button.set_scope("dev1/p2")
        dropped: list[tuple[int, int, bool]] = []
        button.key_dropped.connect(lambda a, b, c: dropped.append((a, b, c)))

        button.dropEvent(_key_drop(2, scope="dev1/p1"))

        assert_that(dropped).is_empty()

    def test_a_drop_from_the_same_scope_goes_through(self, qapp):
        button = _key(7)
        button.set_scope("dev1/p1")
        dropped: list[tuple[int, int, bool]] = []
        button.key_dropped.connect(lambda a, b, c: dropped.append((a, b, c)))

        button.dropEvent(_key_drop(2, scope="dev1/p1"))

        assert_that(dropped).is_equal_to([(2, 7, False)])

    def test_the_grid_applies_its_scope_to_every_key_and_across_rebuilds(self, qapp):
        grid = ButtonGridWidget()
        grid.build(_make_keys(4))

        grid.set_drag_scope("dev1/p1")
        assert_that({b._scope for b in grid._buttons.values()}).is_equal_to({"dev1/p1"})
        grid.build(_make_keys(4))

        assert_that({b._scope for b in grid._buttons.values()}).is_equal_to({"dev1/p1"})


class TestDragEndsThePress:  # pylint: disable=protected-access,unused-argument
    def test_moves_and_the_release_after_a_drag_do_not_select(self, qapp, monkeypatch):
        monkeypatch.setattr(button_grid_module, "_exec_drag", lambda _drag: None)
        button = _key(5)
        button.set_draggable(True)
        edits: list[int] = []
        button.edit_clicked.connect(edits.append)

        TestDragSource._press_and_move(button, 50)
        QApplication.sendEvent(
            button,
            QMouseEvent(
                QEvent.Type.MouseMove,
                QPointF(20, 40),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )
        QTest.mouseRelease(button, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(20, 40))  # type: ignore[call-overload]

        assert_that(edits).is_empty()
        assert_that(button.isDown()).is_false()

    def test_the_next_press_clicks_normally_again(self, qapp, monkeypatch):
        monkeypatch.setattr(button_grid_module, "_exec_drag", lambda _drag: None)
        button = _key(5)
        button.set_draggable(True)
        edits: list[int] = []
        button.edit_clicked.connect(edits.append)
        TestDragSource._press_and_move(button, 50)
        QTest.mouseRelease(button, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(60, 40))  # type: ignore[call-overload]

        QTest.mouseClick(button, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(10, 40))  # type: ignore[call-overload]

        assert_that(edits).is_equal_to([5])


def _animation_bytes(delay: int = 40) -> bytes:
    """A small animated GIF with two clearly different frames, for the Qt playback tests."""
    frames = [Image.new("RGB", (16, 16), color) for color in ((255, 0, 0), (0, 0, 255))]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], duration=delay, loop=0)
    return buffer.getvalue()


def _wait(ms: int) -> None:
    QTest.qWait(ms)  # type: ignore[call-arg,arg-type]  # the PyQt6 stub types it as a method


def _png_b64(color: tuple[int, int, int]) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def _icon_rgb(button: KeyButtonWidget) -> tuple[int, int, int]:
    pixel = button.icon().pixmap(16, 16).toImage().pixelColor(8, 8)
    return pixel.red(), pixel.green(), pixel.blue()


@pytest.fixture(name="key_button")
def _key_button(qapp):  # pylint: disable=unused-argument
    button = KeyButtonWidget(0, KeyInfo(row=0, col=0, pixel_width=72, pixel_height=72, type="key"))
    yield button
    button.set_animation(None)
    button.deleteLater()


def test_qt_can_play_the_server_produced_animation(qapp):  # pylint: disable=unused-argument
    appearance = resolve_appearance({}, {"background": "#202020"})
    spec = AnimationSpec(gif=_animation_bytes(), appearance=appearance, title="", scale=1.0)
    factory = AnimationFactory(cache_bytes=8 * 1024 * 1024)
    try:
        produced = factory.preview_webp(spec)
    finally:
        factory.close()
    assert produced is not None
    buffer = QBuffer()
    buffer.setData(QByteArray(produced))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    movie = QMovie()
    movie.setDevice(buffer)

    assert_that(movie.isValid()).is_true()
    assert_that(movie.frameCount()).is_greater_than(1)


class TestKeyButtonAnimation:
    def test_set_animation_advances_the_icon_between_frames(self, key_button):
        key_button.show()
        key_button.set_animation(_animation_bytes(delay=40))
        seen: set[tuple[int, int, int]] = set()
        for _ in range(40):
            _wait(25)
            seen.add(_icon_rgb(key_button))
            if len(seen) > 1:
                break

        assert_that(len(seen)).is_greater_than(1)

    def test_set_animation_none_stops_the_movie_and_restores_the_poster(self, key_button):
        key_button.show()
        key_button.set_image(_png_b64((0, 255, 0)))
        key_button.set_animation(_animation_bytes())
        _wait(100)
        movie = key_button._movie  # pylint: disable=protected-access
        assert movie is not None

        key_button.set_animation(None)

        assert_that(key_button._movie).is_none()  # pylint: disable=protected-access
        assert_that(movie.state()).is_equal_to(QMovie.MovieState.NotRunning)
        assert_that(_icon_rgb(key_button)).is_equal_to((0, 255, 0))
        _wait(100)
        assert_that(_icon_rgb(key_button)).is_equal_to((0, 255, 0))

    def test_set_animation_none_without_a_poster_clears_the_icon(self, key_button):
        key_button.show()
        key_button.set_animation(_animation_bytes())
        _wait(100)

        key_button.set_animation(None)

        assert_that(key_button.icon().isNull()).is_true()

    def test_hidden_button_pauses_its_movie_and_showing_resumes_it(self, key_button):
        key_button.show()
        key_button.set_animation(_animation_bytes())
        movie = key_button._movie  # pylint: disable=protected-access
        assert movie is not None
        assert_that(movie.state()).is_equal_to(QMovie.MovieState.Running)

        key_button.hide()
        assert_that(movie.state()).is_equal_to(QMovie.MovieState.Paused)
        key_button.show()
        assert_that(movie.state()).is_equal_to(QMovie.MovieState.Running)

    def test_animation_set_on_a_hidden_button_starts_paused(self, key_button):
        key_button.set_animation(_animation_bytes())

        movie = key_button._movie  # pylint: disable=protected-access
        assert movie is not None
        assert_that(movie.state()).is_equal_to(QMovie.MovieState.Paused)

    def test_invalid_animation_data_falls_back_to_the_poster(self, key_button):
        key_button.set_image(_png_b64((0, 255, 0)))

        key_button.set_animation(b"not an image")

        assert_that(key_button._movie).is_none()  # pylint: disable=protected-access
        assert_that(_icon_rgb(key_button)).is_equal_to((0, 255, 0))

    def test_grid_set_key_animation_decodes_and_clears(self, qapp):  # pylint: disable=unused-argument
        grid = ButtonGridWidget()
        grid.build(_make_keys(2))
        button = grid._buttons[1]  # pylint: disable=protected-access

        grid.set_key_animation(1, base64.b64encode(_animation_bytes()).decode())
        assert_that(button._movie).is_not_none()  # pylint: disable=protected-access
        grid.set_key_animation(1, "")
        assert_that(button._movie).is_none()  # pylint: disable=protected-access
        grid.set_key_animation(9, None)  # unknown keys are ignored
        grid.deleteLater()


class TestContextMenu:
    def test_context_menu_lists_gestures_and_delete(self, qapp):  # pylint: disable=unused-argument
        button = _key()
        button.set_draggable(True)

        menu = button._build_context_menu()  # pylint: disable=protected-access

        assert menu is not None
        actions = menu.actions()
        texts = [a.text() for a in actions if not a.isSeparator()]
        assert_that(texts).is_equal_to(["Press", "Double Press", "Long Press", "Delete Card"])
        assert_that([a.isSeparator() for a in actions]).is_equal_to([False, False, False, True, False])

    def test_context_menu_actions_emit(self, qapp):  # pylint: disable=unused-argument
        button = _key(7)
        button.set_draggable(True)
        gestures: list[tuple[int, str]] = []
        deleted: list[int] = []
        button.gesture_edit_requested.connect(lambda i, g: gestures.append((i, g)))
        button.delete_clicked.connect(deleted.append)
        menu = button._build_context_menu()  # pylint: disable=protected-access
        assert menu is not None
        by_text = {a.text(): a for a in menu.actions()}

        by_text["Long Press"].trigger()
        by_text["Delete Card"].trigger()

        assert_that(gestures).is_equal_to([(7, "long_press")])
        assert_that(deleted).is_equal_to([7])

    def test_empty_key_has_no_context_menu(self, qapp):  # pylint: disable=unused-argument
        assert_that(_key()._build_context_menu()).is_none()  # pylint: disable=protected-access

    def test_grid_forwards_gesture_edit_requests(self, qapp):  # pylint: disable=unused-argument
        grid = ButtonGridWidget()
        grid.build(_make_keys(6))
        seen: list[tuple[int, str]] = []
        grid.key_gesture_edit_requested.connect(lambda i, g: seen.append((i, g)))

        grid._buttons[2].gesture_edit_requested.emit(2, "press")  # pylint: disable=protected-access

        assert_that(seen).is_equal_to([(2, "press")])

    def test_context_menu_is_released_after_it_closes(self, qapp, monkeypatch):  # pylint: disable=unused-argument
        button = _key()
        button.set_draggable(True)
        monkeypatch.setattr(QMenu, "exec", lambda *_a, **_k: None)
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(1, 1), QPoint(1, 1))

        button.contextMenuEvent(event)
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

        assert_that(button.findChildren(QMenu)).is_empty()
