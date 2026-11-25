"""Bottom panel for configuring the selected key: preview, appearance rows, then Card and Actions tabs."""

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from decker.client.card_icons import tab_icon
from decker.client.widgets.bindings_edit import GESTURES, BindingsEdit, BindingsSpec
from decker.client.widgets.key_preview import KeyPreview
from decker.client.widgets.schema_form import AppearanceForm, SchemaForm
from decker.common.appearance_schema import APPEARANCE_SCHEMA
from decker.common.background import normalize_background
from decker.common.text_style import fold_legacy_title

_PAGE_PLACEHOLDER = 0
_PAGE_FORM = 1
_PREVIEW_DEBOUNCE_MS = 150
CARD_TAB = "Card"
_CARD_ICON = "fas:image"
_GESTURE_ICONS = {"press": "fas:caret-right", "double_press": "fas:caret-square-right", "long_press": "fas:hourglass"}
_NO_CARD_SETTINGS = "This card has no settings"

_TOP = Qt.AlignmentFlag.AlignTop


def tab_text(name: str, modified: bool) -> str:
    """A tab's title, with a trailing ``*`` while its contents differ from the defaults."""
    return f"{name} *" if modified else name


PreviewProvider = Callable[[dict[str, Any]], str | None]


@dataclass(frozen=True)
class SaveOutcome:
    """Result of a save: ``errors`` maps ``settings.<key>`` / ``bindings.<gesture>.<key>`` to a message."""

    ok: bool
    errors: Mapping[str, str] = field(default_factory=dict)


SaveCallback = Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], SaveOutcome]  # settings, appearance, bindings

_SETTINGS_PREFIX = "settings."
_BINDINGS_PREFIX = "bindings."


def _run_job(job: Callable[[], str | None]) -> str | None:
    try:
        return job()
    except Exception:  # pylint: disable=broad-except
        logging.debug("Key preview failed", exc_info=True)
        return None


def _run_in_thread(job: Callable[[], str | None], done: Callable[[str | None], None]) -> None:
    """Run ``job`` on a worker thread and hand its result (None on failure) to ``done`` from that thread."""
    threading.Thread(target=lambda: done(_run_job(job)), name="key-preview", daemon=True).start()


class CardConfigPanel(QStackedWidget):  # pylint: disable=too-many-instance-attributes
    """Stacked config panel: a placeholder page, then preview, appearance, Save and the Card and gesture tabs."""

    _preview_ready = pyqtSignal(int, object)

    def __init__(self) -> None:
        super().__init__()
        self._preview_seq = 0
        self._preview_ready.connect(self._on_preview_ready)
        self._save_cb: SaveCallback | None = None
        self._loaded: tuple[Any, ...] | None = None
        self._appearance_defaults: dict[str, Any] = {}
        self._stored_background: Any = None
        self._stored_norm: Any = None
        self._preview_provider: PreviewProvider | None = None
        self._dirty = False
        self._baseline: tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None = None

        placeholder = QLabel("Hover a key and click ✎ (or focus it and press F2) to configure it")
        placeholder.setProperty("role", "muted")
        self.addWidget(placeholder)  # page 0

        self._header = QLabel()
        self._header.setProperty("role", "title")
        self._preview = KeyPreview()
        self._appearance_form = AppearanceForm()
        self._settings_form = SchemaForm()
        self._bindings_edit = BindingsEdit()
        self._no_settings = QLabel(_NO_CARD_SETTINGS)
        self._no_settings.setProperty("role", "muted")
        self._tabs = QTabWidget()
        self._card_index = 0
        self._gesture_index: dict[str, int] = {}
        self._save_btn = QPushButton("Save")
        self._save_btn.setProperty("dirty", False)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(_PREVIEW_DEBOUNCE_MS)
        self._preview_timer.timeout.connect(self._request_preview)
        self.addWidget(self._build_form_page())  # page 1
        self._appearance_form.changed.connect(self._on_edited)
        self._settings_form.changed.connect(self._on_edited)
        self._bindings_edit.changed.connect(self._on_edited)

    @property
    def is_dirty(self) -> bool:
        """Whether the forms differ from the last loaded or saved values."""
        return self._dirty

    def set_preview_provider(self, provider: PreviewProvider | None) -> None:
        """Set how the draft appearance is turned into a base64 key image (None or "" keeps the last picture)."""
        self._preview_provider = provider

    def refresh_preview_soon(self) -> None:
        """Recompose the preview after the debounce (e.g. when the plugin's own image changed)."""
        self._preview_timer.start()

    def set_preview_image(self, image_b64: str | None) -> None:
        """Show an image in the preview."""
        self._preview.set_image(image_b64)

    def show_placeholder(self) -> None:
        """Switch to the placeholder page."""
        self._reset_editing()
        self.setCurrentIndex(_PAGE_PLACEHOLDER)

    def forget_loaded(self) -> None:
        """Treat the next values as a different key's, so edits made for the previous key are replaced."""
        self._reset_editing()

    def _reset_editing(self) -> None:
        self._loaded = None
        self._baseline = None
        self._preview_timer.stop()
        self._preview_seq += 1  # drops a reply still in flight for the previous key
        self._preview.set_image(None)
        self._refresh_state()

    def show_card_config(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        settings_schema: list[dict[str, Any]],
        settings: dict[str, Any],
        appearance: dict[str, Any],
        appearance_defaults: dict[str, Any],
        save_cb: SaveCallback,
        title: str = "",
        plugin_id: str = "",
        bindings: BindingsSpec | None = None,
    ) -> None:
        """Populate and show the forms; identical values re-sent while shown leave edits in progress alone."""
        self._save_cb = save_cb
        self._header.setText(title)
        appearance_defaults = _fold_background(fold_legacy_title(appearance_defaults), None)
        self._appearance_defaults = appearance_defaults
        self._stored_background = appearance.get("background")
        appearance = _merged(appearance, appearance_defaults)
        self._stored_norm = appearance.get("background")
        if self._stored_norm == appearance_defaults.get("background"):
            self._stored_background = None
        spec = bindings or BindingsSpec({}, {})
        loaded = (
            settings_schema,
            dict(settings),
            _resolved(appearance, appearance_defaults),
            dict(appearance_defaults),
            plugin_id,
            spec,
        )
        if self.currentIndex() == _PAGE_FORM and loaded == self._loaded:
            return
        self._loaded = loaded
        self._appearance_form.set_schema(APPEARANCE_SCHEMA, appearance)
        self._settings_form.set_schema(settings_schema, settings, plugin_id)
        self._bindings_edit.set_bindings(spec, plugin_id)
        self._show_tabs(bool(settings_schema), bindings is not None)
        self.setCurrentIndex(_PAGE_FORM)
        self._baseline = self._current_values()
        self._refresh_state()
        self._request_preview()

    def _show_tabs(self, has_settings: bool, has_bindings: bool) -> None:
        """Show the tabs that apply; an empty card keeps its Card tab so it can say there is nothing to set."""
        self._no_settings.setVisible(not has_settings)
        self._tabs.setTabVisible(self._card_index, has_settings or not has_bindings)
        for index in self._gesture_index.values():
            self._tabs.setTabVisible(index, has_bindings)
        visible = [i for i in range(self._tabs.count()) if self._tabs.isTabVisible(i)]
        self._tabs.setCurrentIndex(visible[0] if visible else 0)

    def _refresh_tab_marks(self) -> None:
        card_modified = self._settings_form.values() != self._settings_form.defaults()
        self._tabs.setTabText(self._card_index, tab_text(CARD_TAB, card_modified))
        for gesture, title in GESTURES:
            modified = self._bindings_edit.is_modified(gesture)
            self._tabs.setTabText(self._gesture_index[gesture], tab_text(title, modified))

    def _build_form_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(self._header)

        self._save_btn.clicked.connect(self._on_save)
        left = QVBoxLayout()  # the preview and Save form one column
        left.addWidget(self._preview, 0, _TOP)
        self._save_btn.setFixedWidth(self._preview.width())
        left.addWidget(self._save_btn)
        left.addStretch(1)
        top = QHBoxLayout()
        top.addLayout(left)
        top.addWidget(self._appearance_form, 1, _TOP)
        layout.addLayout(top)  # everything above the tabs stays put while the tab's contents scroll

        self._preview.set_icon_requested.connect(self._browse_icon)
        self._preview.clear_icon_requested.connect(self._clear_icon)

        self._tabs.setDocumentMode(True)  # tabs on a rule across the panel, no frame round the pages
        self._card_index = self._tabs.addTab(
            _scrolling(self._no_settings, self._settings_form), tab_icon(_CARD_ICON), CARD_TAB
        )
        for gesture, title in GESTURES:
            scroll = _scrolling(self._bindings_edit.row_widget(gesture))
            self._gesture_index[gesture] = self._tabs.addTab(scroll, tab_icon(_GESTURE_ICONS[gesture]), title)
        layout.addWidget(self._tabs, 1)
        return page

    def _icon_edit(self) -> Any:
        icon = self._appearance_form._fields.get("icon")  # pylint: disable=protected-access
        return icon.widget if icon is not None else None

    def _browse_icon(self) -> None:
        if (edit := self._icon_edit()) is not None:
            edit.browse()

    def _clear_icon(self) -> None:
        if (edit := self._icon_edit()) is not None:
            edit.clear_icon()

    def _on_edited(self) -> None:
        self._refresh_state()
        self._preview_timer.start()

    def _refresh_state(self) -> None:
        values = self._current_values()
        dirty = self._baseline is not None and values != self._baseline
        if dirty != self._dirty or self._save_btn.property("dirty") != dirty:
            self._dirty = dirty
            self._save_btn.setProperty("dirty", dirty)
            if (style := self._save_btn.style()) is not None:
                style.unpolish(self._save_btn)
                style.polish(self._save_btn)
        self._refresh_tab_marks()
        self._save_btn.setEnabled(not (self._settings_form.has_errors() or self._bindings_edit.has_errors()))
        icon = self._appearance_form.values().get("icon")
        self._preview.set_has_icon(isinstance(icon, dict) and bool(icon.get("ref")))

    def _current_values(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        appearance = _resolved(self._appearance_form.values(), self._appearance_defaults)
        return self._settings_form.values(), appearance, self._bindings_edit.value()

    def _draft_appearance(self) -> dict[str, Any]:
        draft = {
            key: value
            for key, value in self._appearance_form.values().items()
            if value != self._appearance_defaults.get(key)
        }
        if self._stored_background is not None and "background" in self._appearance_form.values():
            # An untouched stored background keeps the shape it was stored in (a legacy colour string stays one).
            if self._appearance_form.values()["background"] == self._stored_norm:
                draft["background"] = self._stored_background
        return draft

    def _request_preview(self) -> None:
        provider = self._preview_provider
        if provider is None:
            return
        self._preview_seq += 1
        seq, draft = self._preview_seq, self._draft_appearance()
        _run_in_thread(lambda: provider(draft), lambda image: self._preview_ready.emit(seq, image))

    def _on_preview_ready(self, seq: int, image: Any) -> None:
        if seq == self._preview_seq and isinstance(image, str) and image:
            self._preview.set_image(image)

    def _route_errors(self, errors: Mapping[str, str]) -> None:
        self._settings_form.set_errors(
            {key.removeprefix(_SETTINGS_PREFIX): msg for key, msg in errors.items() if key.startswith(_SETTINGS_PREFIX)}
        )
        self._bindings_edit.set_errors(
            {key.removeprefix(_BINDINGS_PREFIX): msg for key, msg in errors.items() if key.startswith(_BINDINGS_PREFIX)}
        )

    def _on_save(self) -> None:
        if self._save_cb is None:
            return
        settings = self._settings_form.values()
        bindings = self._bindings_edit.value()
        form_appearance = self._appearance_form.values()
        previous = (self._loaded, self._baseline)
        # Record the saved state first so a synchronous echo of it is recognised as identical and keeps later edits.
        if self._loaded is not None:
            self._loaded = (
                self._loaded[0],
                settings,
                _resolved(form_appearance, self._appearance_defaults),
                self._loaded[3],
                self._loaded[4],
                BindingsSpec(self._loaded[5].defaults, bindings),
            )
        self._baseline = self._current_values()
        draft = self._draft_appearance()
        outcome = self._save_cb(settings, draft, bindings)
        if not outcome.ok:
            self._loaded, self._baseline = previous
            self._route_errors(outcome.errors)
        else:
            self._stored_background = draft.get("background")
            self._stored_norm = form_appearance.get("background")
            self.refresh_preview_soon()
        self._refresh_state()


def _scrolling(*widgets: QWidget) -> QScrollArea:
    """A frameless scroll area stacking ``widgets`` at the top."""
    body = QWidget()
    body_layout = QVBoxLayout(body)
    body_layout.setContentsMargins(0, 8, 0, 0)
    for widget in widgets:
        body_layout.addWidget(widget)
    body_layout.addStretch(1)
    scroll = QScrollArea()
    scroll.setWidget(body)
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    return scroll


def _merged(appearance: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Stored appearance with legacy title keys folded in and the title merged field-wise over the defaults."""
    folded = _fold_background(fold_legacy_title(appearance), defaults.get("background"))
    merged = {**defaults, **folded}
    if "title" in merged:
        merged["title"] = {**defaults.get("title", {}), **folded.get("title", {})}
    return merged


def _resolved(appearance: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Appearance with defaults applied; None means "unset" and is dropped so shapes compare equal."""
    return {k: v for k, v in {**defaults, **appearance}.items() if v is not None}


def _fold_background(values: dict[str, Any], defaults: Any) -> dict[str, Any]:
    """Copy of ``values`` whose background, if any, is a complete dict (a legacy colour string becomes one)."""
    if "background" not in values:
        return values
    base = normalize_background(defaults) if defaults is not None else None
    return {**values, "background": normalize_background(values["background"], base)}
