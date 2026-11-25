"""Tests for decker.client.widgets.page_tab_bar.PageTabBar."""

import pytest
from assertpy import assert_that
from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel, QTabBar, QToolButton

from decker.client.widgets.page_tab_bar import PageTabBar
from decker.common.model import PageInfo

# pylint: disable=protected-access,redefined-outer-name,unused-argument


class _Pages:
    """Minimal page list for driving the tab bar: "Page 1", "Page 2", first page default."""

    def __init__(self) -> None:
        self.pages: list[PageInfo] = []
        for _ in range(2):
            self.add_page()
        self.default_page_id = self.pages[0].page_id

    def add_page(self) -> PageInfo:
        page = PageInfo(page_id=f"id{len(self.pages) + 1}", name=f"Page {len(self.pages) + 1}")
        self.pages.append(page)
        return page

    def delete_page(self, page_id: str) -> None:
        self.pages = [page for page in self.pages if page.page_id != page_id]

    def set_default_page(self, page_id: str) -> None:
        self.default_page_id = page_id


@pytest.fixture
def model() -> _Pages:
    return _Pages()


@pytest.fixture
def bar(qapp, model) -> PageTabBar:
    widget = PageTabBar()
    widget.set_pages(model.pages, None, model.default_page_id)
    return widget


def _record(signal) -> list:
    calls: list = []
    signal.connect(lambda *args: calls.append(args))
    return calls


def _names(bar: PageTabBar) -> list[str]:
    return [bar.tab_bar.tabText(i) for i in range(bar.tab_bar.count())]


def _close_button(bar: PageTabBar, index: int) -> QToolButton | None:
    button = bar.tab_bar.tabButton(index, QTabBar.ButtonPosition.RightSide)
    return button if isinstance(button, QToolButton) else None


def _menu_action(bar: PageTabBar, index: int, text: str):
    menu = bar.context_menu_for(index)
    assert menu is not None
    return next(action for action in menu.actions() if action.text() == text)


class TestButtons:
    def test_menu_button_emits_menu_before_showing(self, bar):
        calls = _record(bar.menu_about_to_show)

        assert_that(bar._menu_button.text()).is_equal_to("⚙")
        bar.menu.aboutToShow.emit()

        assert_that(calls).is_equal_to([(bar.menu,)])

    def test_add_button_requests_new_page(self, bar):
        calls = _record(bar.add_page_requested)

        bar._add_button.click()

        assert_that(calls).is_length(1)

    def test_add_button_sits_right_of_the_tabs(self, bar):
        layout = bar.layout()
        assert layout is not None

        assert_that(layout.indexOf(bar._add_button)).is_greater_than(layout.indexOf(bar._viewport))


class TestSetPages:
    def test_one_tab_per_page_with_page_ids(self, bar, model):
        assert_that(_names(bar)).is_equal_to(["Page 1", "Page 2"])
        assert_that(bar.tab_bar.tabData(1)).is_equal_to(model.pages[1].page_id)

    def test_selects_default_page_when_no_current(self, qapp, model):
        model.set_default_page(model.pages[1].page_id)
        widget = PageTabBar()

        widget.set_pages(model.pages, None, model.default_page_id)

        assert_that(widget.current_page_id).is_equal_to(model.pages[1].page_id)

    def test_keeps_requested_current_page(self, bar, model):
        bar.set_pages(model.pages, model.pages[1].page_id, model.default_page_id)

        assert_that(bar.current_page_id).is_equal_to(model.pages[1].page_id)

    def test_does_not_emit_page_selected(self, bar, model):
        calls = _record(bar.page_selected)

        bar.set_pages(model.pages, model.pages[1].page_id, model.default_page_id)

        assert_that(calls).is_empty()

    def test_marks_default_page(self, bar, model):
        marker = bar.tab_bar.tabButton(0, QTabBar.ButtonPosition.LeftSide)

        assert_that(marker).is_instance_of(QLabel)
        assert_that(bar.tab_bar.tabButton(1, QTabBar.ButtonPosition.LeftSide)).is_none()

    def test_empty_page_list(self, bar):
        bar.set_pages([])

        assert_that(bar.current_page_id).is_none()
        assert_that(bar.context_menu_for(0)).is_none()


class TestSelection:
    def test_selecting_tab_emits_page_id(self, bar, model):
        calls = _record(bar.page_selected)

        bar.tab_bar.setCurrentIndex(1)

        assert_that(calls).is_equal_to([(model.pages[1].page_id,)])


class TestClose:
    def test_close_button_requests_delete_of_its_page(self, bar, model):
        calls = _record(bar.delete_page_requested)
        button = _close_button(bar, 1)
        assert button is not None

        button.click()

        assert_that(calls).is_equal_to([(model.pages[1].page_id,)])

    def test_single_page_has_no_close_button(self, bar, model):
        model.delete_page(model.pages[1].page_id)

        bar.set_pages(model.pages, None, model.default_page_id)

        assert_that(_close_button(bar, 0)).is_none()


class TestRename:
    def test_double_click_opens_editor_with_name(self, bar):
        bar.tab_bar.tabBarDoubleClicked.emit(1)

        assert_that(bar._rename_edit).is_not_none()
        assert_that(bar._rename_edit.text()).is_equal_to("Page 2")
        assert_that(bar._rename_edit.parent()).is_same_as(bar.tab_bar)

    def test_enter_commits_new_name(self, bar, model):
        calls = _record(bar.rename_page_requested)
        bar.start_rename(model.pages[1].page_id)
        bar._rename_edit.setText("  Media ")

        bar._rename_edit.editingFinished.emit()

        assert_that(calls).is_equal_to([(model.pages[1].page_id, "Media")])
        assert_that(bar._rename_edit).is_none()

    def test_escape_cancels(self, bar, model):
        calls = _record(bar.rename_page_requested)
        bar.start_rename(model.pages[1].page_id)
        edit = bar._rename_edit
        edit.setText("Media")

        edit.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))

        assert_that(calls).is_empty()
        assert_that(bar._rename_edit).is_none()

    def test_other_keys_still_type(self, bar, model):
        bar.start_rename(model.pages[1].page_id)
        edit = bar._rename_edit
        edit.clear()

        edit.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, "a"))

        assert_that(edit.text()).is_equal_to("a")

    @pytest.mark.parametrize("text", ["", "   ", "Page 2"])
    def test_blank_or_unchanged_name_is_not_emitted(self, bar, model, text):
        calls = _record(bar.rename_page_requested)
        bar.start_rename(model.pages[1].page_id)
        bar._rename_edit.setText(text)

        bar._rename_edit.editingFinished.emit()

        assert_that(calls).is_empty()

    def test_commit_fires_once_even_if_editing_finishes_twice(self, bar, model):
        calls = _record(bar.rename_page_requested)
        bar.start_rename(model.pages[1].page_id)
        edit = bar._rename_edit
        edit.setText("Media")

        edit.editingFinished.emit()
        edit.editingFinished.emit()

        assert_that(calls).is_length(1)

    def test_unknown_page_does_nothing(self, bar):
        bar.start_rename("nope")

        assert_that(bar._rename_edit).is_none()

    def test_set_pages_discards_open_editor(self, bar, model):
        calls = _record(bar.rename_page_requested)
        bar.start_rename(model.pages[1].page_id)
        bar._rename_edit.setText("Media")

        bar.set_pages(model.pages, None, model.default_page_id)

        assert_that(bar._rename_edit).is_none()
        assert_that(calls).is_empty()


class TestContextMenu:
    def test_menu_offers_rename_delete_set_default(self, bar):
        menu = bar.context_menu_for(1)
        assert menu is not None

        texts = [action.text() for action in menu.actions() if not action.isSeparator()]

        assert_that(texts).is_equal_to(["Rename Page", "Delete Page", "Set as Default"])

    def test_rename_action_opens_editor(self, bar):
        _menu_action(bar, 1, "Rename Page").trigger()

        assert_that(bar._rename_edit.text()).is_equal_to("Page 2")

    def test_delete_action_requests_delete(self, bar, model):
        calls = _record(bar.delete_page_requested)

        _menu_action(bar, 1, "Delete Page").trigger()

        assert_that(calls).is_equal_to([(model.pages[1].page_id,)])

    def test_delete_disabled_for_last_page(self, bar, model):
        model.delete_page(model.pages[1].page_id)
        bar.set_pages(model.pages, None, model.default_page_id)

        assert_that(_menu_action(bar, 0, "Delete Page").isEnabled()).is_false()

    def test_set_default_action_requests_default(self, bar, model):
        calls = _record(bar.set_default_page_requested)

        _menu_action(bar, 1, "Set as Default").trigger()

        assert_that(calls).is_equal_to([(model.pages[1].page_id,)])

    def test_set_default_checked_and_disabled_on_default_page(self, bar):
        action = _menu_action(bar, 0, "Set as Default")

        assert_that(action.isChecked()).is_true()
        assert_that(action.isEnabled()).is_false()


class TestOverflow:
    def _many_pages(self, count: int) -> list[PageInfo]:
        model = _Pages()
        for _ in range(count):
            model.add_page()
        return model.pages

    def test_no_arrows_when_tabs_fit(self, qapp, model):
        widget = PageTabBar()
        widget.resize(900, 40)
        widget.show()
        widget.set_pages(model.pages, None, model.default_page_id)
        QApplication.processEvents()

        assert_that(widget._scroll_left.isHidden()).is_true()
        assert_that(widget._scroll_right.isHidden()).is_true()
        widget.close()

    def test_arrows_at_both_sides_when_tabs_overflow(self, qapp):
        widget = PageTabBar()
        widget.resize(300, 40)
        widget.show()
        pages = self._many_pages(10)
        widget.set_pages(pages, pages[-1].page_id)
        QApplication.processEvents()

        layout = widget.layout()
        assert layout is not None
        assert_that(widget._scroll_left.isHidden()).is_false()
        assert_that(widget._scroll_right.isHidden()).is_false()
        assert_that(layout.indexOf(widget._scroll_left)).is_less_than(layout.indexOf(widget._viewport))
        assert_that(layout.indexOf(widget._scroll_right)).is_greater_than(layout.indexOf(widget._viewport))
        widget.close()

    def test_current_tab_scrolled_into_view_and_arrows_scroll(self, qapp):
        widget = PageTabBar()
        widget.resize(300, 40)
        widget.show()
        pages = self._many_pages(10)
        widget.set_pages(pages, pages[-1].page_id)
        QApplication.processEvents()
        scroll_bar = widget._viewport.horizontalScrollBar()
        assert scroll_bar is not None

        assert_that(scroll_bar.value()).is_equal_to(scroll_bar.maximum())
        assert_that(widget._scroll_right.isEnabled()).is_false()

        widget._scroll_left.click()

        assert_that(scroll_bar.value()).is_less_than(scroll_bar.maximum())
        assert_that(widget._scroll_right.isEnabled()).is_true()
        widget.close()

    def test_arrows_hide_again_when_space_grows(self, qapp):
        widget = PageTabBar()
        widget.resize(300, 40)
        widget.show()
        pages = self._many_pages(3)
        widget.set_pages(pages)
        QApplication.processEvents()
        assert_that(widget._scroll_left.isHidden()).is_false()

        widget.resize(1600, 40)
        QApplication.processEvents()

        assert_that(widget._scroll_left.isHidden()).is_true()
        widget.close()


class TestClickOutsideCommits:
    @pytest.fixture(name="shown")
    def shown_fixture(self, bar, qapp):
        from PyQt6.QtWidgets import QWidget

        bar.show()
        other = QWidget()
        other.resize(40, 40)
        other.show()
        qapp.processEvents()
        yield bar, other
        other.close()
        bar.close()

    @staticmethod
    def _click(widget) -> None:
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton)  # type: ignore[call-overload]

    def test_clicking_another_widget_commits_the_rename(self, shown, model):
        bar, other = shown
        calls = _record(bar.rename_page_requested)
        bar.start_rename(model.pages[1].page_id)
        bar._rename_edit.setText("Media")

        self._click(other)

        assert_that(calls).is_equal_to([(model.pages[1].page_id, "Media")])
        assert_that(bar._rename_edit).is_none()

    def test_clicking_a_button_of_the_bar_commits_too(self, shown, model):
        bar, _other = shown
        calls = _record(bar.rename_page_requested)
        bar.start_rename(model.pages[1].page_id)
        bar._rename_edit.setText("Media")

        self._click(bar._add_button)

        assert_that(calls).is_equal_to([(model.pages[1].page_id, "Media")])

    def test_clicking_inside_the_editor_keeps_editing(self, shown, model):
        bar, _other = shown
        calls = _record(bar.rename_page_requested)
        bar.start_rename(model.pages[1].page_id)
        bar._rename_edit.setText("Media")

        self._click(bar._rename_edit)

        assert_that(calls).is_empty()
        assert_that(bar._rename_edit).is_not_none()

    @pytest.mark.parametrize("text", ["", "   ", "Page 2"])
    def test_blank_or_unchanged_names_are_not_emitted_on_an_outside_click(self, shown, model, text):
        bar, other = shown
        calls = _record(bar.rename_page_requested)
        bar.start_rename(model.pages[1].page_id)
        bar._rename_edit.setText(text)

        self._click(other)

        assert_that(calls).is_empty()
        assert_that(bar._rename_edit).is_none()

    def test_the_outside_click_listener_is_removed_afterwards(self, shown, model):
        bar, other = shown
        calls = _record(bar.rename_page_requested)
        bar.start_rename(model.pages[1].page_id)
        bar._rename_edit.setText("Media")
        self._click(other)

        bar.start_rename(model.pages[0].page_id)
        bar._rename_edit.setText("Home")
        bar._rename_edit.editingFinished.emit()  # finishing normally removes the listener as well
        self._click(other)

        assert_that(calls).is_equal_to([(model.pages[1].page_id, "Media"), (model.pages[0].page_id, "Home")])

    def test_escape_still_cancels_without_a_commit_on_later_clicks(self, shown, model):
        bar, other = shown
        calls = _record(bar.rename_page_requested)
        bar.start_rename(model.pages[1].page_id)
        edit = bar._rename_edit
        edit.setText("Media")
        edit.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))

        self._click(other)

        assert_that(calls).is_empty()


class TestMovableTabs:
    def test_tabs_are_movable(self, bar) -> None:
        assert_that(bar.tab_bar.isMovable()).is_true()

    def test_moving_a_tab_emits_page_move_requested(self, bar, model) -> None:
        model.add_page()
        bar.set_pages(model.pages, None, model.default_page_id)
        moves = _record(bar.page_move_requested)

        bar.tab_bar.moveTab(0, 2)

        assert_that(moves).is_equal_to([("id1", 2)])

    def test_a_real_drag_across_tabs_emits_one_move_with_the_final_index(self, bar, model) -> None:
        for _ in range(2):
            model.add_page()
        bar.set_pages(model.pages, None, model.default_page_id)
        bar.show()
        moves = _record(bar.page_move_requested)

        _drag(bar, 0, 3)

        assert_that(moves).is_equal_to([("id1", 3)])
        assert_that(bar.tab_bar.tabData(3)).is_equal_to("id1")

    def test_a_rebuild_requested_mid_drag_does_not_cancel_the_drag(self, bar, model) -> None:
        for _ in range(2):
            model.add_page()
        bar.set_pages(model.pages, None, model.default_page_id)
        bar.show()
        moves = _record(bar.page_move_requested)
        tabs = bar.tab_bar
        end_x = tabs.tabRect(3).right() - 2
        _drag_to(tabs, tabs.tabRect(0).center(), end_x)

        bar.set_pages(model.pages, None, model.default_page_id)  # a server push arrives mid-drag
        QTest.mouseRelease(tabs, Qt.MouseButton.LeftButton, pos=QPoint(end_x, tabs.tabRect(3).center().y()))  # type: ignore[call-overload]

        assert_that(moves).is_equal_to([("id1", 3)])

    def test_a_move_during_a_rebuild_is_not_reported(self, bar, model) -> None:
        moves = _record(bar.page_move_requested)
        bar._updating = True
        bar.tab_bar.moveTab(0, 1)
        bar._updating = False

        assert_that(moves).is_empty()

    def test_move_while_renaming_commits_or_cancels_the_edit(self, bar, model) -> None:
        renames = _record(bar.rename_page_requested)
        bar.start_rename("id1")
        assert bar._rename_edit is not None
        bar._rename_edit.setText("Other")

        bar.tab_bar.moveTab(0, 1)

        assert_that(bar._rename_edit).is_none()
        assert_that(renames).is_empty()


def _drag_to(tabs: QTabBar, start: QPoint, end_x: int) -> None:
    """Press at start and move the mouse in small steps to end_x, leaving the button held."""
    QTest.mousePress(tabs, Qt.MouseButton.LeftButton, pos=start)  # type: ignore[call-overload]
    step = 4 if end_x > start.x() else -4
    for x in range(start.x() + step, end_x + step, step):
        QTest.mouseMove(tabs, QPoint(x, start.y()))  # type: ignore[call-overload]  # stubs lack the widget overload


def _drag(bar: PageTabBar, from_index: int, to_index: int) -> None:
    """Drag a tab with real mouse events, crossing each neighbour on the way, and release."""
    tabs = bar.tab_bar
    end_x = tabs.tabRect(to_index).right() - 2
    _drag_to(tabs, tabs.tabRect(from_index).center(), end_x)
    QTest.mouseRelease(tabs, Qt.MouseButton.LeftButton, pos=QPoint(end_x, tabs.tabRect(from_index).center().y()))  # type: ignore[call-overload]
