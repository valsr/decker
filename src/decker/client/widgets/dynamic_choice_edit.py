"""A dropdown whose options the plugin supplies at runtime, editable so a stored or typed value always fits."""

import json
import logging
import threading
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QComboBox, QHBoxLayout, QToolButton, QWidget

from decker.client.context import AppContext
from decker.client.icon_picker import RpcCaller

FetchOptions = Callable[[str, str], tuple[bool, list[dict[str, str]], str]]

_UNAVAILABLE = "unavailable"
_REFRESH_TIP = "Reload the options"


def fetch_options(rpc: RpcCaller | None, plugin_id: str, source: str) -> tuple[bool, list[dict[str, str]], str]:
    """Ask the server for a plugin's options of ``source``: ``(ok, options, error)``; never raises. Blocks."""
    if rpc is None:
        return False, [], "not connected"
    try:
        data = json.loads(rpc.call("plugin_options", plugin_id, source) or "null")
    except Exception as exc:  # pylint: disable=broad-except
        return False, [], f"could not load the options: {exc}"
    if not isinstance(data, dict):
        return False, [], "could not load the options"
    if not data.get("ok"):
        return False, [], str(data.get("error") or "could not load the options")
    options = [
        {"label": o["label"], "value": o["value"]}
        for o in data.get("options") or []
        if isinstance(o, dict) and isinstance(o.get("label"), str) and isinstance(o.get("value"), str)
    ]
    return True, options, ""


class _Mailbox(QObject):  # pylint: disable=too-few-public-methods
    """Carries a worker's result to the GUI thread (queued, because the receiver lives there)."""

    delivered = pyqtSignal(int, bool, object, str)


class DynamicChoiceEdit(QWidget):  # pylint: disable=too-many-instance-attributes
    """An editable combo of static plus plugin-supplied options and a refresh button; ``value()`` is the choice."""

    value_changed = pyqtSignal()

    def __init__(
        self,
        entry: dict[str, Any],
        plugin_id: str,
        fetch: FetchOptions | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._plugin_id = plugin_id
        self._source = str(entry.get("options_source") or "")
        self._fetch = fetch
        self._static: list[tuple[str, str]] = [
            (str(o["label"]), str(o["value"])) for o in entry.get("options") or [] if isinstance(o, dict)
        ]
        self._token = 0
        self._filling = False
        self._error = ""
        self._literal: str | None = None
        self._last = ""
        self._thread: threading.Thread | None = None
        self._combo = QComboBox()
        self._combo.setEditable(True)
        self._combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._refresh = QToolButton()
        self._refresh.setText("↻")
        self._refresh.setToolTip(_REFRESH_TIP)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._combo, 1)
        layout.addWidget(self._refresh)
        self._refresh.setVisible(bool(self._source))
        self._mailbox = _Mailbox(self)
        self._mailbox.delivered.connect(self._on_delivered)
        self._fill([])
        self._combo.editTextChanged.connect(self._on_text_changed)
        self._combo.activated.connect(self._on_activated)
        self._refresh.clicked.connect(self.reload)
        self.reload()

    def value(self) -> str:
        """The chosen value: an option's value when the text is its label, otherwise the text as typed."""
        if self._literal is not None:
            return self._literal
        text = self._combo.currentText()
        index = self._combo.currentIndex()
        if index < 0 or self._combo.itemText(index) != text:
            index = self._combo.findText(text)
        data = self._combo.itemData(index) if index >= 0 else None
        return text if data is None else str(data)

    def set_value(self, value: Any) -> None:
        """Show ``value`` (kept even when not listed) without emitting ``value_changed``."""
        self._filling = True
        try:
            self._show(str(value) if value is not None else "")
        finally:
            self._filling = False

    def reload(self) -> None:
        """Fetch the options again on a worker thread; the result is applied on the GUI thread."""
        if not self._source:
            return
        self._token += 1
        token = self._token
        fetch = self._fetch or _default_fetch()
        mailbox, plugin_id, source = self._mailbox, self._plugin_id, self._source

        def work() -> None:
            try:
                ok, options, error = fetch(plugin_id, source)
            except Exception as exc:  # pylint: disable=broad-except
                ok, options, error = False, [], str(exc) or "could not load the options"
            try:
                mailbox.delivered.emit(token, ok, options, error)
            except RuntimeError:  # the widget was deleted while the fetch ran
                pass

        self._thread = threading.Thread(target=work, name="dynamic-options", daemon=True)
        self._thread.start()

    def _on_delivered(self, token: int, ok: bool, options: object, error: str) -> None:
        if token != self._token:
            return
        self._error = "" if ok else (error or "could not load the options")
        if not ok:
            logging.warning("Could not load the options of %s/%s: %s", self._plugin_id, self._source, self._error)
        current = self.value()
        self._fill([(o["label"], o["value"]) for o in options] if isinstance(options, list) and ok else [])
        self._filling = True
        try:
            self._show(current)
        finally:
            self._filling = False

    def _fill(self, dynamic: list[tuple[str, str]]) -> None:
        self._filling = True
        try:
            self._combo.clear()
            listed: set[str] = set()
            for label, value in [*self._static, *dynamic]:
                if value not in listed:
                    listed.add(value)
                    self._combo.addItem(label, value)
        finally:
            self._filling = False

    def _show(self, value: str) -> None:
        index = self._combo.findData(value)
        if index >= 0:
            self._literal = None
            self._combo.setCurrentIndex(index)
        else:
            self._literal = value
            self._combo.setCurrentIndex(-1)
            self._combo.setEditText(value)
        self._last = self.value()
        self._update_tips()

    def _update_tips(self) -> None:
        unlisted = bool(self.value()) and self._combo.findData(self.value()) < 0
        self._combo.setToolTip(self._error or (_UNAVAILABLE if unlisted else ""))
        self._refresh.setToolTip(self._error or _REFRESH_TIP)

    def _on_text_changed(self, _text: str) -> None:
        if self._filling:
            return
        self._literal = None
        self._changed()

    def _on_activated(self, _index: int) -> None:
        self._literal = None
        self._changed()

    def _changed(self) -> None:
        self._update_tips()
        if self.value() != self._last:
            self._last = self.value()
            self.value_changed.emit()


def _default_fetch() -> FetchOptions:
    rpc = AppContext.get().connection_manager
    return lambda plugin_id, source: fetch_options(rpc, plugin_id, source)
