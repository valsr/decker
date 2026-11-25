"""Page tab strip: editor-style page tabs, add button, overflow arrows and the view menu button."""

from typing import Any

from PyQt6.QtCore import QEvent, QObject, QPoint, QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QTabBar,
    QToolButton,
    QWidget,
)

from decker.client.widgets.profile_selector import ProfileSelector
from decker.client.widgets.rename_edit import RenameEdit
from decker.common.model import PageInfo

_SCROLL_STEP_PX = 120
_VISIBLE_MARGIN_PX = 8


class _TabViewport(QScrollArea):
    """Clips the tab bar horizontally; sized to the tabs' full width when space allows."""

    def __init__(self, tabs: QTabBar) -> None:
        super().__init__()
        self.setWidget(tabs)
        self.setWidgetResizable(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")

    def sizeHint(self) -> QSize:  # pylint: disable=invalid-name
        """The tab bar's full width, so the strip only scrolls when space runs out."""
        tabs = self.widget()
        return tabs.sizeHint() if tabs is not None else QSize(0, 0)

    def minimumSizeHint(self) -> QSize:  # pylint: disable=invalid-name
        """Allow shrinking to nothing; the overflow arrows take over."""
        return QSize(0, self.sizeHint().height())


class PageTabBar(QWidget):  # pylint: disable=too-many-instance-attributes
    """Browser-style page tabs.

    Drag a tab to reorder pages. Double-click a tab to rename it in place, ✕ deletes the page, + adds one, and right-click opens
    Rename / Delete / Set as Default. Arrows appear at the sides when the tabs overflow.
    The ⚙ button at the far right opens a menu that the owner fills via menu_about_to_show.
    """

    menu_about_to_show = pyqtSignal(QMenu)
    page_selected = pyqtSignal(str)
    add_page_requested = pyqtSignal()
    delete_page_requested = pyqtSignal(str)
    rename_page_requested = pyqtSignal(str, str)  # page_id, new name
    set_default_page_requested = pyqtSignal(str)
    page_move_requested = pyqtSignal(str, int)  # page_id, new index

    def __init__(self) -> None:
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._names: dict[str, str] = {}
        self._default_page_id: str | None = None
        self._rename_page_id: str | None = None
        self._rename_edit: RenameEdit | None = None
        self._updating: bool = False

        self._menu = QMenu(self)
        self._menu.aboutToShow.connect(lambda: self.menu_about_to_show.emit(self._menu))
        self._menu_button = _tool_button("⚙", "view-menu", "Menu")
        self._menu_button.setMenu(self._menu)
        self._menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

        self._profile_selector = ProfileSelector()
        self._profile_selector.hide()

        self._tabs = QTabBar()
        self._tabs.setObjectName("page-tabs")
        self._tabs.setUsesScrollButtons(False)
        self._tabs.setExpanding(False)
        self._tabs.setDrawBase(False)
        self._tabs.setMovable(True)
        self._tabs.setElideMode(Qt.TextElideMode.ElideNone)
        self._tabs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tabs.currentChanged.connect(self._on_current_changed)
        self._tabs.tabBarDoubleClicked.connect(self._on_tab_double_clicked)
        self._tabs.tabMoved.connect(self._on_tab_moved)
        self._tabs.customContextMenuRequested.connect(self._show_context_menu)

        self._viewport = _TabViewport(self._tabs)
        self._viewport.installEventFilter(self)
        scroll_bar = self._viewport.horizontalScrollBar()
        if scroll_bar is not None:
            scroll_bar.valueChanged.connect(self._update_scroll_buttons)
            scroll_bar.rangeChanged.connect(self._on_scroll_range_changed)

        self._scroll_left = _tool_button("‹", "tab-scroll-left", "Scroll tabs left")
        self._scroll_left.clicked.connect(lambda: self._scroll_by(-_SCROLL_STEP_PX))
        self._scroll_right = _tool_button("›", "tab-scroll-right", "Scroll tabs right")
        self._scroll_right.clicked.connect(lambda: self._scroll_by(_SCROLL_STEP_PX))
        self._scroll_left.hide()
        self._scroll_right.hide()

        self._add_button = _tool_button("+", "tab-add", "Add page")
        self._add_button.clicked.connect(self.add_page_requested.emit)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 1)  # keep the strip's bottom border visible
        layout.setSpacing(0)
        for widget in (self._scroll_left, self._viewport, self._scroll_right, self._add_button):
            layout.addWidget(widget)
        layout.addStretch()
        layout.addWidget(self._profile_selector)
        layout.addWidget(self._menu_button)

    @property
    def tab_bar(self) -> QTabBar:
        """The underlying QTabBar; tab data holds each tab's page id."""
        return self._tabs

    @property
    def profile_selector(self) -> ProfileSelector:
        """The profile dropdown left of the ⚙ button; hidden until the owner has profiles to show."""
        return self._profile_selector

    @property
    def menu(self) -> QMenu:
        """The ⚙ button's menu; cleared and refilled by the owner on menu_about_to_show."""
        return self._menu

    @property
    def current_page_id(self) -> str | None:
        """The id of the selected page, or None when there are no pages."""
        return self._page_id_at(self._tabs.currentIndex())

    def set_pages(
        self, pages: list[PageInfo], current_page_id: str | None = None, default_page_id: str | None = None
    ) -> None:
        """Replace all tabs. Selects current_page_id, else the default page, else the first page."""
        self._finish_rename(commit=False)
        self._updating = True
        try:
            while self._tabs.count():
                self._tabs.removeTab(0)
            self._names = {page.page_id: page.name for page in pages}
            self._default_page_id = default_page_id
            for page in pages:
                self._add_tab(page, closable=len(pages) > 1)
            target = current_page_id if current_page_id in self._names else default_page_id
            self._tabs.setCurrentIndex(max(self._index_of(target), 0))
        finally:
            self._updating = False
        self._tabs.resize(self._tabs.sizeHint())
        self._viewport.updateGeometry()
        self._update_scroll_buttons()
        self._ensure_current_visible()

    def start_rename(self, page_id: str) -> None:
        """Open the in-place editor over page_id's tab."""
        index = self._index_of(page_id)
        if index < 0:
            return
        self._finish_rename(commit=False)
        edit = RenameEdit(self._names[page_id], self._tabs)
        edit.setGeometry(self._tabs.tabRect(index).adjusted(4, 4, -4, -4))
        edit.selectAll()
        edit.editingFinished.connect(lambda: self._finish_rename(commit=True))
        edit.cancelled.connect(lambda: self._finish_rename(commit=False))
        self._rename_page_id = page_id
        self._rename_edit = edit
        edit.show()
        edit.setFocus()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)  # a click anywhere else commits the rename (not every click moves focus)

    def context_menu_for(self, index: int) -> QMenu | None:
        """Build the right-click menu for the tab at index, or None if there is no tab there."""
        page_id = self._page_id_at(index)
        if page_id is None:
            return None
        menu = QMenu(self)
        menu.addAction("Rename Page", lambda: self.start_rename(page_id))
        delete_action = menu.addAction("Delete Page", lambda: self.delete_page_requested.emit(page_id))
        if delete_action is not None:
            delete_action.setEnabled(self._tabs.count() > 1)
        menu.addSeparator()
        default_action = menu.addAction("Set as Default", lambda: self.set_default_page_requested.emit(page_id))
        if default_action is not None:
            default_action.setCheckable(True)
            default_action.setChecked(page_id == self._default_page_id)
            default_action.setEnabled(page_id != self._default_page_id)
        return menu

    def eventFilter(self, a0: QObject | None, a1: QEvent | None) -> bool:  # pylint: disable=invalid-name
        """Re-evaluate the overflow arrows when the viewport resizes; commit a rename when something else is clicked."""
        if a0 is self._viewport and a1 is not None and a1.type() == QEvent.Type.Resize:
            self._update_scroll_buttons()
        elif a1 is not None and a1.type() == QEvent.Type.MouseButtonPress and self._is_outside_editor(a0):
            self._finish_rename(commit=True)
        return super().eventFilter(a0, a1)

    def _is_outside_editor(self, receiver: QObject | None) -> bool:
        edit = self._rename_edit
        if edit is None or not isinstance(receiver, QWidget):
            return False
        return receiver is not edit and not edit.isAncestorOf(receiver)

    def resizeEvent(self, a0: Any) -> None:  # pylint: disable=invalid-name
        """Re-evaluate the overflow arrows when the strip itself is resized."""
        super().resizeEvent(a0)
        self._update_scroll_buttons()

    def _add_tab(self, page: PageInfo, closable: bool) -> None:
        index = self._tabs.addTab(page.name)
        self._tabs.setTabData(index, page.page_id)
        self._tabs.setTabToolTip(index, page.name)
        if page.page_id == self._default_page_id:
            marker = QLabel("★")
            marker.setObjectName("default-page-marker")
            marker.setToolTip("Default page")
            self._tabs.setTabButton(index, QTabBar.ButtonPosition.LeftSide, marker)
        if closable:
            close = _tool_button("✕", "tab-close", "Delete page")
            close.clicked.connect(lambda _checked=False, page_id=page.page_id: self.delete_page_requested.emit(page_id))
            self._tabs.setTabButton(index, QTabBar.ButtonPosition.RightSide, close)

    def _finish_rename(self, commit: bool) -> None:
        edit, page_id = self._rename_edit, self._rename_page_id
        if edit is None or page_id is None:
            return
        self._rename_edit = None
        self._rename_page_id = None
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        name = edit.text().strip()
        edit.hide()
        edit.deleteLater()
        if commit and name and name != self._names.get(page_id):
            self.rename_page_requested.emit(page_id, name)

    def _on_current_changed(self, index: int) -> None:
        if self._updating:
            return
        self._ensure_current_visible()
        page_id = self._page_id_at(index)
        if page_id is not None:
            self.page_selected.emit(page_id)

    def _on_tab_moved(self, _from_index: int, to_index: int) -> None:
        if self._updating:
            return
        self._finish_rename(commit=False)
        page_id = self._page_id_at(to_index)
        if page_id is not None:
            self.page_move_requested.emit(page_id, to_index)

    def _on_tab_double_clicked(self, index: int) -> None:
        page_id = self._page_id_at(index)
        if page_id is not None:
            self.start_rename(page_id)

    def _show_context_menu(self, pos: QPoint) -> None:
        menu = self.context_menu_for(self._tabs.tabAt(pos))
        if menu is not None:
            menu.exec(self._tabs.mapToGlobal(pos))

    def _on_scroll_range_changed(self) -> None:
        self._update_scroll_buttons()
        self._ensure_current_visible()

    def _scroll_by(self, delta: int) -> None:
        scroll_bar = self._viewport.horizontalScrollBar()
        if scroll_bar is not None:
            scroll_bar.setValue(scroll_bar.value() + delta)

    def _ensure_current_visible(self) -> None:
        index = self._tabs.currentIndex()
        if index < 0:
            return
        rect = self._tabs.tabRect(index)
        self._viewport.ensureVisible(rect.right(), 0, _VISIBLE_MARGIN_PX, 0)
        self._viewport.ensureVisible(rect.left(), 0, _VISIBLE_MARGIN_PX, 0)

    def _update_scroll_buttons(self) -> None:
        arrows_shown = not self._scroll_left.isHidden()
        arrows_width = self._scroll_left.sizeHint().width() + self._scroll_right.sizeHint().width()
        available = self._viewport.width() + (arrows_width if arrows_shown else 0)
        overflow = self._tabs.sizeHint().width() > available
        self._scroll_left.setVisible(overflow)
        self._scroll_right.setVisible(overflow)
        scroll_bar = self._viewport.horizontalScrollBar()
        if scroll_bar is not None:
            self._scroll_left.setEnabled(scroll_bar.value() > scroll_bar.minimum())
            self._scroll_right.setEnabled(scroll_bar.value() < scroll_bar.maximum())

    def _page_id_at(self, index: int) -> str | None:
        if index < 0:
            return None
        data = self._tabs.tabData(index)
        return str(data) if data is not None else None

    def _index_of(self, page_id: str | None) -> int:
        for index in range(self._tabs.count()):
            if self._tabs.tabData(index) == page_id:
                return index
        return -1


def _tool_button(text: str, object_name: str, tooltip: str) -> QToolButton:
    button = QToolButton()
    button.setText(text)
    button.setObjectName(object_name)
    button.setToolTip(tooltip)
    button.setAutoRaise(True)
    return button
