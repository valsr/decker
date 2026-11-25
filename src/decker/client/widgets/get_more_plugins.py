"""Get More Plugins dialog: browse the plugin repositories' catalog and install or update plugins."""

import base64
import logging
import threading
from collections import deque
from collections.abc import Callable
from html import escape
from typing import Any

from packaging.version import InvalidVersion, Version
from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from decker.asset.builtinicons import read_builtin_ref
from decker.client.card_icons import decode_icon, placeholder_pixmap
from decker.client.connection_manager import to_plain
from decker.client.context import AppContext
from decker.client.events import (
    PluginInstallFinishedEvent,
    PluginInstallProgressEvent,
    PluginsChangedEvent,
    ServerDisconnectedEvent,
)
from decker.client.events.event_manager import EventManager
from decker.client.ui_state_store import UiStateStore
from decker.client.widgets.plugins_tab import (
    FALLBACK_HOST_LABEL,
    NOT_ALLOWED_TOOLTIP,
    install_allowed,
    parse_host_label,
    platform_badge,
)

ALL_CATEGORIES = "All categories"
_THUMB_SIZE = 40
_SEP = ", "
_ID_ROLE = Qt.ItemDataRole.UserRole
_FILTER_KEY = "plugins.platform_filter"


def human_size(size: int) -> str:
    """Format a byte count as B / KiB / MiB / GiB."""
    value = float(size)
    for unit in ("B", "KiB", "MiB"):
        if value < 1024:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def button_state(plugin: dict[str, Any]) -> tuple[str, bool]:
    """The (text, enabled) of a catalog row's action button."""
    latest = plugin.get("latest_compatible")
    installed = plugin.get("installed_version")
    if not latest:
        versions = plugin.get("versions") or []
        required = versions[0].get("decker", "") if versions else ""
        return (f"Requires Decker {required}".strip(), False)
    if not installed:
        return ("Install", True)
    if installed == latest or _is_newer(installed, latest):
        return ("Installed", False)
    return ("Update", True)


def _is_newer(installed: str, latest: str) -> bool:
    try:
        return Version(installed) > Version(latest)
    except InvalidVersion:
        return False


def version_text(plugin: dict[str, Any]) -> str:
    """`installed → available` when an update exists, else the available (or installed) version."""
    latest = plugin.get("latest_compatible") or ""
    installed = plugin.get("installed_version") or ""
    if installed and latest and installed != latest and not _is_newer(installed, latest):
        return f"{installed} → {latest}"
    return latest or installed


def _text(plugin: dict[str, Any], key: str) -> str:
    return str(plugin.get(key) or "")


def _matches(plugin: dict[str, Any], needle: str) -> bool:
    fields = (plugin.get("name"), plugin.get("id"), plugin.get("description"), plugin.get("author"))
    return any(needle in str(field or "").lower() for field in fields)


def _builtin_pixmap(ref: Any) -> QPixmap | None:
    raw = read_builtin_ref(ref)
    if raw is None:
        return None
    tint = QApplication.palette().text().color().name()
    return decode_icon(base64.b64encode(raw).decode("ascii"), tint)


class _PluginRow(QWidget):
    install_clicked = pyqtSignal(str)

    def __init__(self, plugin: dict[str, Any], host_label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.plugin_id = str(plugin["id"])
        self.icon_label: QLabel = QLabel()
        self.button: QPushButton = QPushButton()
        text, enabled = button_state(plugin)
        self.button.setText(text)
        self.button.setEnabled(enabled)
        self.actionable: bool = enabled
        self.button.clicked.connect(lambda: self.install_clicked.emit(self.plugin_id))
        self.icon_label.setFixedSize(_THUMB_SIZE, _THUMB_SIZE)
        self.has_local_icon: bool = False
        local = _builtin_pixmap(plugin.get("icon_ref"))
        if local is not None:
            self.has_local_icon = True
            self.set_pixmap(local)
        else:
            self.set_pixmap(placeholder_pixmap(str(plugin.get("name") or self.plugin_id), _THUMB_SIZE))

        name = escape(_text(plugin, "name") or self.plugin_id)
        title = QLabel(f"<b>{name}</b> {escape(version_text(plugin))}")
        short = QLabel(str(plugin.get("description") or ""))
        short.setProperty("role", "muted")
        short.setWordWrap(True)
        texts = QVBoxLayout()
        texts.addWidget(title)
        texts.addWidget(short)
        self.badge: QLabel = QLabel(platform_badge(plugin, host_label))
        self.badge.setProperty("role", "muted")
        self.badge.setVisible(bool(self.badge.text()))
        texts.addWidget(self.badge)
        layout = QHBoxLayout(self)
        layout.addWidget(self.icon_label)
        layout.addLayout(texts, 1)
        layout.addWidget(self.button)

    def set_pixmap(self, pixmap: Any) -> None:
        """Show pixmap as the row icon."""
        self.icon_label.setPixmap(pixmap.scaled(_THUMB_SIZE, _THUMB_SIZE, Qt.AspectRatioMode.KeepAspectRatio))


class GetMorePluginsDialog(QDialog):  # pylint: disable=too-many-instance-attributes
    """Non-modal catalog browser; every RPC (icon fetches included) runs on a worker thread."""

    _finished = pyqtSignal(object, object)

    def __init__(self, parent: QWidget | None = None, ui_state_store: UiStateStore | None = None) -> None:
        super().__init__(parent)
        self._ui_state_store: UiStateStore = ui_state_store or UiStateStore()
        self._host_label: str = FALLBACK_HOST_LABEL
        self.setWindowTitle("Get More Plugins")
        self.resize(760, 560)
        self._catalog: list[dict[str, Any]] = []
        self._rows: dict[str, _PluginRow] = {}
        self._icons: dict[str, Any] = {}
        self._pending: deque[str] = deque()
        self._installing: str | None = None
        self._job_id: str | None = None
        self._early_finished: PluginInstallFinishedEvent | None = None
        self._progress: str = ""
        self._worker: threading.Thread | None = None
        self._icon_worker: threading.Thread | None = None
        self._icon_busy: bool = False

        self._search: QLineEdit = QLineEdit()
        self._category: QComboBox = QComboBox()
        self._platform_only: QCheckBox = QCheckBox()
        self._refresh_btn: QPushButton = QPushButton("Refresh")
        self._list: QListWidget = QListWidget()
        self._details: QLabel = QLabel()
        self._status: QLabel = QLabel()
        self._build()
        em = EventManager.get()
        em.subscribe(PluginInstallProgressEvent, self._on_progress)
        em.subscribe(PluginInstallFinishedEvent, self._on_finished)
        em.subscribe(PluginsChangedEvent, self._on_plugins_changed)
        em.subscribe(ServerDisconnectedEvent, self._on_disconnected)
        self.refresh()

    def refresh(self) -> None:
        """Re-read the catalog from the server."""
        self._status.setText("Loading…")
        self._run(_load_catalog, self._show_catalog)

    def done(self, result: int) -> None:
        """Close the dialog, dropping queued icon loads and event subscriptions."""
        self._pending.clear()
        em = EventManager.get()
        em.unsubscribe(PluginInstallProgressEvent, self._on_progress)
        em.unsubscribe(PluginInstallFinishedEvent, self._on_finished)
        em.unsubscribe(PluginsChangedEvent, self._on_plugins_changed)
        em.unsubscribe(ServerDisconnectedEvent, self._on_disconnected)
        super().done(result)

    def _build(self) -> None:
        self._search.setPlaceholderText("Search plugins…")
        self._search.setClearButtonEnabled(True)
        self._category.addItem(ALL_CATEGORIES)
        self._details.setWordWrap(True)
        self._details.setTextFormat(Qt.TextFormat.RichText)
        self._details.setOpenExternalLinks(True)
        self._details.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._status.setWordWrap(True)
        self._status.setProperty("role", "muted")
        self._list.setIconSize(QSize(_THUMB_SIZE, _THUMB_SIZE))
        self._finished.connect(lambda done, result: done(result))
        self._platform_only.setText(_filter_text(self._host_label))
        self._platform_only.setChecked(self._ui_state_store.get(_FILTER_KEY) is not False)
        self._platform_only.toggled.connect(self._on_filter_toggled)
        self._search.textChanged.connect(lambda *_: self._apply_filter())
        self._category.currentIndexChanged.connect(lambda *_: self._apply_filter())
        self._refresh_btn.clicked.connect(self.refresh)
        self._list.currentItemChanged.connect(lambda *_: self._show_details())

        top = QHBoxLayout()
        top.addWidget(self._search, 1)
        top.addWidget(self._category)
        top.addWidget(self._refresh_btn)
        panes = QHBoxLayout()
        panes.addWidget(self._list, 3)
        panes.addWidget(self._details, 2)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self._platform_only)
        layout.addLayout(panes, 1)
        layout.addWidget(self._status)

    def _run(self, work: Callable[[Any], Any], done: Callable[[Any], None]) -> None:
        manager = AppContext.get().connection_manager
        if manager is None:
            done(None)
            self._status.setText("Not connected to the server.")
            return

        def target() -> None:
            try:
                result = work(manager)
            except Exception:  # pylint: disable=broad-exception-caught
                logging.exception("Plugin repository call failed")
                result = None
            self._finished.emit(done, result)

        self._worker = threading.Thread(target=target, daemon=True)
        self._worker.start()

    def _on_filter_toggled(self, checked: bool) -> None:
        self._ui_state_store.set(_FILTER_KEY, checked)
        self._apply_filter()

    def _show_catalog(self, loaded: Any) -> None:
        catalog, label = loaded if loaded else (None, FALLBACK_HOST_LABEL)
        self._host_label = label
        self._platform_only.setText(_filter_text(label))
        if not isinstance(catalog, list):
            self._status.setText("Could not load the plugin catalog from the server.")
            return
        self._catalog = _visible_catalog(catalog)
        self._status.setText("" if self._catalog else "No plugins are available from the configured repositories.")
        chosen = self._category.currentText()
        self._category.blockSignals(True)
        self._category.clear()
        self._category.addItem(ALL_CATEGORIES)
        self._category.addItems(sorted({str(p["category"]) for p in self._catalog if p.get("category")}))
        index = self._category.findText(chosen)
        self._category.setCurrentIndex(max(index, 0))
        self._category.blockSignals(False)
        self._apply_filter()

    def _apply_filter(self) -> None:
        needle = self._search.text().strip().lower()
        category = self._category.currentText()
        selected = self._selected_id()
        self._pending.clear()
        self._list.clear()
        self._rows.clear()
        platform_only = self._platform_only.isChecked()
        for plugin in self._catalog:
            if platform_only and plugin.get("supported", True) is False:
                continue
            if category != ALL_CATEGORIES and plugin.get("category") != category:
                continue
            if needle and not _matches(plugin, needle):
                continue
            self._add_row(plugin)
            if str(plugin["id"]) == selected:
                self._list.setCurrentRow(self._list.count() - 1)
        self._apply_busy()
        self._show_details()
        self._schedule_next_load()

    def _add_row(self, plugin: dict[str, Any]) -> None:
        plugin_id = str(plugin["id"])
        row = _PluginRow(plugin, self._host_label)
        row.install_clicked.connect(self._on_install)
        item = QListWidgetItem()
        item.setData(_ID_ROLE, plugin_id)
        item.setSizeHint(row.sizeHint())
        self._list.addItem(item)
        self._list.setItemWidget(item, row)
        self._rows[plugin_id] = row
        if row.has_local_icon:
            self._icons[plugin_id] = row.icon_label.pixmap()
        elif plugin_id in self._icons:
            if self._icons[plugin_id] is not None:
                row.set_pixmap(self._icons[plugin_id])
        else:
            self._pending.append(plugin_id)

    def _schedule_next_load(self) -> None:
        # One icon at a time on a worker: the server may have to fetch it over the network.
        manager = AppContext.get().connection_manager
        if self._icon_busy or not self._pending or manager is None:
            return
        self._icon_busy = True
        plugin_id = self._pending.popleft()

        def target() -> None:
            try:
                raw = manager.call("repository_icon", plugin_id)
            except Exception:  # pylint: disable=broad-exception-caught
                logging.exception("Could not load icon for %s", plugin_id)
                raw = None
            self._finished.emit(lambda result: self._icon_loaded(plugin_id, result), raw)

        self._icon_worker = threading.Thread(target=target, daemon=True)
        self._icon_worker.start()

    def _icon_loaded(self, plugin_id: str, raw: Any) -> None:
        self._icon_busy = False
        pixmap = decode_icon(raw) if isinstance(raw, str) else None
        self._icons[plugin_id] = pixmap
        row = self._rows.get(plugin_id)
        if row is not None and pixmap is not None:
            row.set_pixmap(pixmap)
        self._schedule_next_load()

    def _selected_id(self) -> str | None:
        item = self._list.currentItem()
        return str(item.data(_ID_ROLE)) if item is not None else None

    def _show_details(self) -> None:
        plugin = next((p for p in self._catalog if str(p["id"]) == self._selected_id()), None)
        self._details.setText(_details_html(plugin) if plugin is not None else "Select a plugin to see its details.")

    def _apply_busy(self) -> None:
        allowed = install_allowed()
        for plugin_id, row in self._rows.items():
            row.button.setEnabled(row.actionable and allowed and self._installing is None)
            row.button.setToolTip("" if allowed or not row.actionable else NOT_ALLOWED_TOOLTIP)
            if self._installing is not None and plugin_id == self._installing:
                row.button.setText(self._progress or "Installing…")

    def _on_install(self, plugin_id: str) -> None:
        if self._installing is not None or not install_allowed():
            return
        if not self._confirm_unsupported(plugin_id):
            return
        self._installing = plugin_id
        self._job_id = None
        self._early_finished = None
        self._progress = ""
        self._status.setText(f"Installing {plugin_id}…")
        self._apply_busy()
        self._run(lambda m: m.call("plugin_install_from_repository", plugin_id), self._after_start)

    def _confirm_unsupported(self, plugin_id: str) -> bool:
        plugin = next((p for p in self._catalog if str(p["id"]) == plugin_id), None)
        if plugin is None or plugin.get("supported", True) is not False:
            return True
        name = _text(plugin, "name") or plugin_id
        answer = QMessageBox.question(
            self, "Install plugin", f"{name} does not declare support for {self._host_label}. Install anyway?"
        )
        return bool(answer == QMessageBox.StandardButton.Yes)

    def _after_start(self, job_id: Any) -> None:
        if isinstance(job_id, str) and job_id and not job_id.startswith("error:"):
            self._job_id = job_id
            early, self._early_finished = self._early_finished, None
            if early is not None:
                self._on_finished(early)
            return
        message = str(job_id).removeprefix("error:").strip() if job_id else "Could not reach the server."
        self._status.setText(f"Install failed: {message}")
        self._installing = None
        self._apply_filter()

    def _on_progress(self, event: PluginInstallProgressEvent) -> None:
        if self._installing is None or event.job_id != self._job_id:
            return
        self._progress = event.message
        row = self._rows.get(self._installing)
        if row is not None:
            row.button.setText(event.message)
        self._status.setText(event.message)

    def _on_finished(self, event: PluginInstallFinishedEvent) -> None:
        if self._installing is None:
            return
        if self._job_id is None:
            self._early_finished = event
            return
        if event.job_id != self._job_id:
            return
        self._installing = None
        self._job_id = None
        self._status.setText(
            f"Installed {event.plugin_id} {event.version}." if event.ok else f"Install failed: {event.message}"
        )
        if event.ok:
            self._mark_installed(event.plugin_id, event.version)
        self._apply_filter()
        self._run(_load_catalog, self._show_catalog_keep_status)

    def _mark_installed(self, plugin_id: str | None, version: str | None) -> None:
        # Until the server's fresh catalog arrives the rows would still offer "Install" for what was just installed.
        for plugin in self._catalog:
            if str(plugin["id"]) == plugin_id:
                plugin["installed_version"] = version

    def _on_disconnected(self, _event: ServerDisconnectedEvent) -> None:
        if self._installing is None:
            return
        interrupted = self._installing
        self._installing = None
        self._job_id = None
        self._early_finished = None
        self._apply_filter()
        self._status.setText(f"Install of {interrupted} was interrupted: the connection to the server was lost.")

    def _on_plugins_changed(self, _event: PluginsChangedEvent) -> None:
        # Plugins changed elsewhere (e.g. "Update all"); an own job reloads the catalog when it finishes.
        if self._installing is None:
            self.refresh()

    def _show_catalog_keep_status(self, catalog: Any) -> None:
        status = self._status.text()
        self._show_catalog(catalog)
        if self._catalog:
            self._status.setText(status)


def _filter_text(host_label: str) -> str:
    return f"Only show plugins that support this system ({host_label})"


def _load_catalog(manager: Any) -> tuple[Any, str]:
    catalog = to_plain(manager.call("repository_catalog"))
    try:
        label = parse_host_label(manager.call("platform_info"))
    except Exception:  # pylint: disable=broad-exception-caught
        logging.exception("Could not read the server platform")
        label = FALLBACK_HOST_LABEL
    return catalog, label


def _visible_catalog(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop shadowed duplicates, noting on the winning entry which repositories also offer it."""
    also: dict[str, list[str]] = {}
    for plugin in catalog:
        if plugin.get("shadowed_by"):
            also.setdefault(str(plugin["id"]), []).append(str(plugin.get("repository") or ""))
    winners = [{**p, "also_offered_by": also.get(str(p["id"]), [])} for p in catalog if not p.get("shadowed_by")]
    return sorted(winners, key=lambda p: str(p.get("name") or p.get("id")).lower())


def _details_html(plugin: dict[str, Any]) -> str:
    versions = plugin.get("versions") or []
    latest = plugin.get("latest_compatible")
    chosen = next((v for v in versions if v.get("version") == latest), versions[0] if versions else {})
    contributes = chosen.get("contributes") or {}
    name, description = escape(_text(plugin, "name") or str(plugin["id"])), escape(_text(plugin, "description"))
    lines = [
        f"<h3>{name}</h3>",
        f"<p>{description}</p>",
        "<p>Author: " + escape(_text(plugin, "author") or "unknown") + "</p>",
    ]
    homepage = _text(plugin, "homepage")
    if homepage.startswith(("http://", "https://")):
        lines.append(f'<p>Homepage: <a href="{escape(homepage, quote=True)}">{escape(homepage)}</a></p>')
    for kind, label in (
        ("cards", "Cards"),
        ("actions", "Actions"),
        ("icon_packs", "Icon packs"),
        ("variables", "Variables"),
    ):
        names = [str(item) for item in contributes.get(kind) or []]
        if names:
            lines.append(f"<p>{label}: {escape(_SEP.join(names))}</p>")
    if chosen:
        size, decker = human_size(int(chosen.get("size") or 0)), escape(_text(chosen, "decker") or "any")
        lines.append(f"<p>Size: {size}</p><p>Requires Decker: {decker}</p>")
    lines.append("<p>Repository: " + escape(_text(plugin, "repository")) + "</p>")
    for other in plugin.get("also_offered_by") or []:
        lines.append(f"<p>Also offered by {escape(str(other))} (not used)</p>")
    return "".join(lines)
