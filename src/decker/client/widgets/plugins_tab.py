"""Settings → Plugins tab: installed plugins (failed ones grayed), install/uninstall/update and global settings."""

import json
import logging
import threading
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon, QPalette, QPixmap
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from decker.client.card_icons import decode_icon, icon_tint
from decker.client.context import AppContext
from decker.client.events import (
    PluginInstallFinishedEvent,
    PluginInstallProgressEvent,
    PluginLoadFailedEvent,
    PluginsChangedEvent,
    PluginUpdatesAvailableEvent,
)
from decker.client.events.event_manager import EventManager
from decker.client.widgets.schema_form import SchemaForm

PACKAGE_SUFFIX = ".deckerplugin"
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
TRUST_NOTICE = "Plugins run with your user's permissions. Only install plugins from authors you trust."
NOT_ALLOWED_TOOLTIP = "Installing plugins is only allowed from the server's own machine"
_FAILURE_HINT = (
    "Installing and uninstalling is only allowed from the server's own machine, and only one at a time; "
    "otherwise check the connection."
)
FALLBACK_HOST_LABEL = "this system"
_PLUGIN_ID_ROLE = Qt.ItemDataRole.UserRole


_install_allowed_cache: bool = False


def install_allowed() -> bool:
    """The cached answer to whether this client may install and uninstall plugins (no RPC)."""
    return _install_allowed_cache


def refresh_install_allowed() -> bool:
    """Ask the server whether installs are allowed and cache the answer."""
    global _install_allowed_cache  # pylint: disable=global-statement
    manager = AppContext.get().connection_manager
    _install_allowed_cache = manager is not None and bool(manager.call("plugin_install_allowed"))
    return _install_allowed_cache


def install_package_file(path: Path, parent: QWidget | None = None) -> threading.Thread | None:
    """Confirm (with the trust notice), then upload a .deckerplugin file on a worker thread.

    Returns the worker thread (None when nothing was started). Failures are reported as a finished event.
    """
    manager = AppContext.get().connection_manager
    if manager is None:
        QMessageBox.warning(parent, "Install plugin", "Not connected to the server.")
        return None
    try:
        too_large = path.stat().st_size > MAX_PACKAGE_BYTES
    except OSError as exc:
        QMessageBox.warning(parent, "Install plugin", f"Cannot read {path.name}: {exc}")
        return None
    if too_large:
        QMessageBox.warning(parent, "Install plugin", f"{path.name} is larger than 64 MiB and cannot be installed.")
        return None
    answer = QMessageBox.question(parent, "Install plugin", f"Install {path.name}?\n\n{TRUST_NOTICE}")
    if answer != QMessageBox.StandardButton.Yes:
        return None
    EventManager.get().emit(PluginInstallProgressEvent(job_id="", message=f"Uploading {path.name}…"))
    worker = threading.Thread(target=_upload_package, args=(manager, path), daemon=True)
    worker.start()
    return worker


def _upload_package(manager: Any, path: Path) -> None:
    try:
        job_id = manager.call("plugin_install", path.name, path.read_bytes())
        error = "" if job_id else f"Could not install {path.name}. {_FAILURE_HINT}"
    except OSError as exc:
        error = f"Cannot read {path.name}: {exc}"
    if error:
        EventManager.get().push(
            PluginInstallFinishedEvent(
                job_id="", ok=False, plugin_id="", version="", previous_version="", message=error
            )
        )


def platform_badge(plugin: dict[str, Any], host_label: str) -> str:
    """The "Not supported on <host>" badge text for a plugin, or an empty string when it is supported."""
    return "" if plugin.get("supported", True) is not False else f"Not supported on {host_label}"


def parse_host_label(raw: Any) -> str:
    """The label from a `platform_info` RPC result, or a generic fallback when unavailable."""
    try:
        label = json.loads(raw)["label"] if raw else ""
    except (ValueError, TypeError, KeyError):
        label = ""
    return str(label) or FALLBACK_HOST_LABEL


def _contribution_summary(plugin: dict[str, Any]) -> str:
    parts = []
    for key, singular in (
        ("cards", "card"),
        ("actions", "action"),
        ("icon_packs", "icon pack"),
        ("variables", "variable"),
    ):
        count = int(plugin.get(key, 0))
        if count:
            parts.append(f"{count} {singular}{'' if count == 1 else 's'}")
    return " · ".join(parts) or "no contributions"


def _plugin_icon(plugin: dict[str, Any], failed: bool = False) -> QIcon:
    encoded = plugin.get("icon_b64")
    pixmap = (decode_icon(str(encoded), icon_tint()) if encoded else None) or QPixmap()
    icon = QIcon(pixmap)
    return QIcon(icon.pixmap(pixmap.size(), QIcon.Mode.Disabled)) if failed and not pixmap.isNull() else icon


def _failure_tooltip(plugin: dict[str, Any], error: str) -> str:
    description = str(plugin.get("description", ""))
    return f"Failed: {error}" + (f"\n\n{description}" if description else "")


class PluginsTab(QWidget):  # pylint: disable=too-many-instance-attributes
    """Lists installed plugins (failed ones grayed, error on hover) and manages install, uninstall and settings."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._plugins: dict[str, dict[str, Any]] = {}
        self._failed_plugins: dict[str, str] = {}
        self._updates: dict[str, str] = {}

        self._plugin_list: QListWidget = QListWidget()
        self._install_btn: QPushButton = QPushButton("Install…")
        self._get_more_btn: QPushButton = QPushButton("Get More Plugins…")
        self._repositories: Any = None
        self._get_more_dialog: Any = None
        self._uninstall_btn: QPushButton = QPushButton("Uninstall")
        self._update_btn: QPushButton = QPushButton("Update")
        self._status_label: QLabel = QLabel()
        self._settings_form: SchemaForm = SchemaForm()
        self._apply_settings_btn: QPushButton = QPushButton("Apply plugin settings")
        self._settings_title: QLabel = QLabel("Plugin settings")
        self._selected_id: str | None = None
        self._host_label: str = FALLBACK_HOST_LABEL

        self._build()
        em = EventManager.get()
        em.subscribe(PluginLoadFailedEvent, self._on_plugin_load_failed)
        em.subscribe(PluginInstallProgressEvent, self._on_install_progress)
        em.subscribe(PluginInstallFinishedEvent, self._on_install_finished)
        em.subscribe(PluginsChangedEvent, self._on_plugins_changed)
        em.subscribe(PluginUpdatesAvailableEvent, self._on_updates_available)

    def refresh(self) -> None:
        """Re-fetch the installed plugins and the install permission from the server."""
        manager = AppContext.get().connection_manager
        raw = manager.call("plugins_list") if manager is not None else None
        try:
            plugins: list[dict[str, Any]] = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            plugins = []
        self._plugins = {str(p["id"]): p for p in plugins}
        self._failed_plugins = self._fetch_failures(manager, self._failed_plugins)
        self._host_label = parse_host_label(manager.call("platform_info") if manager is not None else None)
        self._render_plugins()
        allowed = refresh_install_allowed()
        self._install_btn.setEnabled(allowed)
        self._install_btn.setToolTip("" if allowed else NOT_ALLOWED_TOOLTIP)
        self._show_selection()
        self._repositories.refresh()

    def set_failed_plugins(self, failures: dict[str, str]) -> None:
        """Replace the failed plugins (plugin UUID → error message); they show grayed in the list."""
        self._failed_plugins = dict(failures)
        self._render_plugins()

    @staticmethod
    def _fetch_failures(manager: Any, previous: dict[str, str]) -> dict[str, str]:
        raw = manager.call("plugins_load_failures") if manager is not None else None
        try:
            failures = json.loads(raw) if raw else None
        except (ValueError, TypeError):
            failures = None
        if not isinstance(failures, list):
            return previous  # unknown: keep what the failure events reported
        return {
            str(f["plugin_uuid"]): str(f.get("error", ""))
            for f in failures
            if isinstance(f, dict) and "plugin_uuid" in f
        }

    def _rows(self) -> dict[str, dict[str, Any]]:
        """Installed plugins plus folders that failed to load (no manifest, so only their folder name is known)."""
        broken = {uuid: {"id": uuid, "name": uuid} for uuid in self._failed_plugins if uuid not in self._plugins}
        return {**broken, **self._plugins}

    def _build(self) -> None:
        from decker.client.widgets.repositories_section import (  # pylint: disable=import-outside-toplevel,cyclic-import
            RepositoriesSection,
        )

        self._repositories = RepositoriesSection()
        self._plugin_list.setIconSize(self._plugin_list.iconSize() * 2)
        self._plugin_list.currentItemChanged.connect(self._on_selection_changed)
        self._install_btn.clicked.connect(self._on_install_clicked)
        self._get_more_btn.clicked.connect(self._on_get_more_clicked)
        self._uninstall_btn.clicked.connect(self._on_uninstall_clicked)
        self._uninstall_btn.setEnabled(False)
        self._update_btn.clicked.connect(self._on_update_clicked)
        self._update_btn.setEnabled(False)
        self._apply_settings_btn.clicked.connect(self._on_apply_settings)
        self._status_label.setWordWrap(True)
        self._status_label.setProperty("role", "muted")
        self._settings_title.setProperty("role", "title")
        self._set_settings_visible(False)

        buttons = QHBoxLayout()
        buttons.addWidget(self._install_btn)
        buttons.addWidget(self._update_btn)
        buttons.addWidget(self._uninstall_btn)
        buttons.addStretch()
        buttons.addWidget(self._get_more_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self._plugin_list, 2)
        layout.addLayout(buttons)
        layout.addWidget(self._status_label)
        layout.addWidget(self._repositories)
        layout.addWidget(self._settings_title)
        layout.addWidget(self._settings_form)
        layout.addWidget(self._apply_settings_btn)

    def _render_plugins(self) -> None:
        self._plugin_list.blockSignals(True)
        self._plugin_list.clear()
        rows = self._rows()
        for plugin_id, plugin in sorted(rows.items(), key=lambda kv: str(kv[1].get("name", kv[0])).lower()):
            item = self._plugin_item(plugin_id, plugin)
            self._plugin_list.addItem(item)
            if plugin_id == self._selected_id:
                self._plugin_list.setCurrentItem(item)
        self._plugin_list.blockSignals(False)
        if self._selected_id not in rows:
            self._selected_id = None
        self._show_selection()

    def _plugin_item(self, plugin_id: str, plugin: dict[str, Any]) -> QListWidgetItem:
        error = self._failed_plugins.get(plugin_id)
        if plugin_id in self._plugins:
            text = (
                f"{plugin.get('name', plugin_id)}  v{plugin.get('version', '')} · {plugin.get('author', '')}\n"
                f"{_contribution_summary(plugin)}"
            )
        else:
            text = f"{plugin_id}\nCould not be loaded"
        if error is not None:
            text += " · Failed"
        if plugin_id in self._updates:
            text += f" · Update available: {self._updates[plugin_id]}"
        badge = platform_badge(plugin, self._host_label)
        if badge:
            text += f" · {badge}"
        item = QListWidgetItem(_plugin_icon(plugin, error is not None), text)
        item.setData(_PLUGIN_ID_ROLE, plugin_id)
        if error is None:
            item.setToolTip(str(plugin.get("description", "")))
        else:
            item.setToolTip(_failure_tooltip(plugin, error))
            item.setForeground(self.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text))
        return item

    def _on_selection_changed(self, current: QListWidgetItem | None, _previous: Any = None) -> None:
        self._selected_id = str(current.data(_PLUGIN_ID_ROLE)) if current is not None else None
        self._show_selection()

    def _show_selection(self) -> None:
        plugin = self._rows().get(self._selected_id or "")
        builtin = bool(plugin and plugin.get("builtin"))
        self._uninstall_btn.setEnabled(plugin is not None and not builtin and install_allowed())
        self._uninstall_btn.setToolTip(
            "This plugin is built into Decker" if builtin else "" if install_allowed() else NOT_ALLOWED_TOOLTIP
        )
        self._show_update_button(plugin)
        schema = plugin.get("global_settings_schema") if plugin else None  # a folder that failed to load has none
        if plugin is None or not schema:
            self._set_settings_visible(False)
            return
        self._settings_form.set_schema(schema, plugin.get("global_settings") or {}, str(plugin["id"]))
        self._set_settings_visible(True)

    def _show_update_button(self, plugin: dict[str, Any] | None) -> None:
        available = self._updates.get(str(plugin["id"])) if plugin else None
        self._update_btn.setEnabled(available is not None and install_allowed())
        if available is None:
            self._update_btn.setToolTip("No update available for this plugin")
        else:
            self._update_btn.setToolTip(f"Update to {available}" if install_allowed() else NOT_ALLOWED_TOOLTIP)

    def _on_update_clicked(self) -> None:
        plugin = self._rows().get(self._selected_id or "")
        manager = AppContext.get().connection_manager
        if plugin is None or manager is None or str(plugin["id"]) not in self._updates:
            return
        reply = manager.call("plugin_install_from_repository", plugin["id"])
        if not reply or str(reply).startswith("error:"):
            self._status_label.setText(f"Could not update {plugin['name']}. {reply or _FAILURE_HINT}")
        else:
            self._status_label.setText(f"Updating {plugin['name']}…")

    def _set_settings_visible(self, visible: bool) -> None:
        for widget in (self._settings_title, self._settings_form, self._apply_settings_btn):
            widget.setVisible(visible)

    def _on_install_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Install plugin", "", f"Decker plugins (*{PACKAGE_SUFFIX})")
        if path:
            install_package_file(Path(path), self)

    def _on_get_more_clicked(self) -> None:
        from decker.client.widgets.get_more_plugins import (  # pylint: disable=import-outside-toplevel,cyclic-import
            GetMorePluginsDialog,
        )

        if self._get_more_dialog is None:
            self._get_more_dialog = GetMorePluginsDialog(self)
            self._get_more_dialog.finished.connect(lambda *_: setattr(self, "_get_more_dialog", None))
        self._get_more_dialog.show()
        self._get_more_dialog.raise_()
        self._get_more_dialog.activateWindow()

    def _on_uninstall_clicked(self) -> None:
        plugin = self._rows().get(self._selected_id or "")
        manager = AppContext.get().connection_manager
        if plugin is None or manager is None:
            return
        usage = manager.call("plugin_usage", plugin["id"])
        if usage is None:
            self._status_label.setText(f"Couldn't check where {plugin['name']} is used; try again.")
            return
        count = int(usage)
        answer = QMessageBox.question(
            self, "Uninstall plugin", f"{plugin['name']} is used on {count} keys. Uninstall it and clear those keys?"
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if manager.call("plugin_uninstall", plugin["id"]):
            self._failed_plugins.pop(str(plugin["id"]), None)
            self._status_label.setText(f"Uninstalled {plugin['name']}.")
        else:
            self._status_label.setText(f"Could not uninstall {plugin['name']}. {_FAILURE_HINT}")
        self.refresh()

    def _on_apply_settings(self) -> None:
        manager = AppContext.get().connection_manager
        if manager is None or self._selected_id is None:
            return
        values = json.dumps(self._settings_form.values())
        ok = manager.call("plugin_update_global_settings", self._selected_id, values)
        self._status_label.setText("Plugin settings applied." if ok else "Could not apply plugin settings.")
        if ok:
            self._plugins[self._selected_id]["global_settings"] = self._settings_form.values()

    def _on_install_progress(self, event: PluginInstallProgressEvent) -> None:
        self._status_label.setText(event.message)

    def _on_install_finished(self, event: PluginInstallFinishedEvent) -> None:
        if event.ok:
            if event.previous_version:
                text = f"Updated {event.plugin_id} from {event.previous_version} to {event.version}."
            else:
                text = f"Installed {event.plugin_id} {event.version}."
        else:
            text = f"Install failed: {event.message}"
        self._status_label.setText(text)
        logging.info("Plugin install %s finished: %s", event.job_id, text)

    def _on_updates_available(self, event: PluginUpdatesAvailableEvent) -> None:
        self._updates = {str(u["id"]): str(u["available"]) for u in event.updates}
        self._render_plugins()

    def _on_plugins_changed(self, _event: PluginsChangedEvent) -> None:
        self.refresh()

    def _on_plugin_load_failed(self, event: PluginLoadFailedEvent) -> None:
        self._failed_plugins[event.plugin_uuid] = event.error
        self._render_plugins()
