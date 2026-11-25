"""RPyC service definition for the Decker server."""

import base64
import copy
import inspect
import ipaddress
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath
from typing import TYPE_CHECKING, Any

from rpyc.core.service import Service
from rpyc.utils import exposed

from decker.asset.iconresize import ImageImportError, ImportOptions
from decker.asset.usericons import ImportResult, normalize_id
from decker.common import config
from decker.common.appearance_schema import APPEARANCE_KEYS
from decker.common.icon_ref import DECKER_ICON_REF, parse_icon_ref
from decker.common.model import (
    AppRule,
    DefaultRule,
    DeviceHardwareInfo,
    DevicePages,
    DeviceProfiles,
    KeyState,
    MatchCondition,
    PageInfo,
    ProfileInfo,
)
from decker.common.platforms import (
    HostPlatform,
    PlatformSpec,
    detect_platform,
    platform_supports,
)
from decker.common.text_style import is_color
from decker.common.utils import api_response
from decker.common.version import decker_version, is_compatible
from decker.plugin.icons import read_icon
from decker.plugin.loader import InstalledPlugin
from decker.plugin.repository_index import IndexVersion, latest_compatible
from decker.plugin.settings_schema import schema_to_json
from decker.plugin.target import CardTarget
from decker.server.key_assignments import BLANK_CARD_ID, BLANK_PLUGIN_ID, KeyAssignment
from decker.server.plugin_installer import PluginInstallError
from decker.server.repositories import CatalogEntry, RepositoryError, RepositoryStore
from decker.server.settings import apply_settings_request, read_settings
from decker.server.update_checker import UpdateChecker
from decker.server.window_wiring import (
    WINDOW_PICKED_EVENT,
    status_payload,
    window_payload,
)

if TYPE_CHECKING:
    from decker.server.page_controller import PageController
    from decker.server.server_context import ServerContext

_LONG_PRESS_HOLD_S = 0.6
_INTERNAL_ERROR = "internal error"
_REMOTE_INSTALL_DENIED = "Installing plugins is only allowed from the server's own machine"


def _peer_host(conn: Any) -> str | None:
    try:
        host = conn._channel.stream.sock.getpeername()[0]  # pylint: disable=protected-access
    except Exception:  # pylint: disable=broad-except
        return None
    return host if isinstance(host, str) else None


def _wire_get(raw: Any, key: str, default: Any) -> Any:
    """`raw.get` without attribute access, which a client's netref denies to the server."""
    return raw[key] if key in raw else default


def _rule_from_wire(raw: Any) -> AppRule | None:
    """Build an AppRule from an RPyC-passed dict (a netref there, so read it key by key); None when malformed."""
    try:
        conditions = [MatchCondition(str(c["field"]), str(c["op"]), str(c["value"])) for c in raw["conditions"]]
        profile_id, page_id = raw["profile_id"], _wire_get(raw, "page_id", "")
        if isinstance(profile_id, bool) or not isinstance(profile_id, int) or not isinstance(page_id, str):
            return None
        rule_id, name = _wire_get(raw, "rule_id", ""), raw["name"]
        return AppRule(str(rule_id), str(name), bool(raw["enabled"]), conditions, profile_id, page_id)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _is_loopback(host: str | None) -> bool:
    if host is None:
        return False
    try:
        address = ipaddress.ip_address(host.split("%", maxsplit=1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


class DeckerStatusEnum(StrEnum):
    """Status values for the Decker service."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


def _platform_fields(spec: PlatformSpec | None) -> dict[str, Any]:
    payload = spec.to_payload() if spec is not None else {}
    return {"platforms": payload.get("platforms"), "linux": payload.get("linux")}


def _catalog_item(entry: CatalogEntry, installed_version: str | None, host: HostPlatform) -> dict[str, Any]:
    plugin = entry.plugin
    latest = _pick_version(plugin.versions, "")
    shown = latest if latest is not None else (plugin.versions[0] if plugin.versions else None)
    shown_spec = shown.platforms if shown is not None else None
    return {
        "id": plugin.id,
        "name": plugin.name,
        "author": plugin.author,
        "description": plugin.description,
        "category": plugin.category,
        "homepage": plugin.homepage,
        "icon_ref": plugin.icon_ref,
        "repository": entry.repository_url,
        "shadowed_by": entry.shadowed_by,
        "installed_version": installed_version,
        "latest_compatible": latest.version if latest is not None else None,
        **_platform_fields(shown_spec),
        "supported": platform_supports(shown_spec, host),
        "versions": [
            {
                "version": item.version,
                "decker": item.decker,
                "size": item.size,
                "released": item.released,
                "compatible": is_compatible(item.decker),
                "contributes": item.contributes,
                **_platform_fields(item.platforms),
            }
            for item in plugin.versions
        ],
    }


def _pick_version(versions: tuple[IndexVersion, ...], wanted: str) -> IndexVersion | None:
    if wanted:
        return next((item for item in versions if item.version == wanted), None)
    return latest_compatible(versions)


@dataclass
class DeckerServiceStatus:
    """Structured status information for the DeckerService."""

    service_status: DeckerStatusEnum
    last_heartbeat: float
    version: str = ""


class DeckerService(Service):  # pylint: disable=too-many-public-methods  # one method per RPC
    """RPyC service exposed to clients for controlling the Decker application."""

    _context: "ServerContext | None" = None

    @classmethod
    def configure_context(cls, context: "ServerContext") -> None:
        """Bind the ServerContext singleton before clients connect."""
        cls._context = context

    def __init__(self) -> None:
        """Initialize the Decker service."""
        super().__init__()
        self._last_heartbeat: float = time.time()
        self._peer_host: str | None = None
        self._attached: bool = False
        self._conn: Any = None

    def on_connect(self, conn: Any) -> None:
        """Handle client connection — register for server-push events."""
        logging.info("Client connected to Decker service")
        self._peer_host = _peer_host(conn)
        self._conn = conn
        if self._context is not None:
            self._context.event_bus.register(conn, loopback=_is_loopback(self._peer_host))

    def on_disconnect(self, conn: Any) -> None:
        """Handle client disconnection — stop pushing events to this client."""
        logging.info("Client disconnected from Decker service")
        if self._context is not None:
            self._context.event_bus.unregister(conn)
            if self._attached and self._context.client_tracker is not None:
                self._context.client_tracker.disconnected()

    @exposed
    def stop(self) -> bool:
        """Gracefully shut down the server. The monitor exits on detecting a clean shutdown."""
        if self._context is None:
            logging.warning("No context configured, cannot stop")
            return False

        if self._context.server is None:
            logging.error("Server reference not configured")
            return False

        self._schedule_shutdown()
        return True

    @exposed
    def client_attached(self) -> bool:
        """Mark this connection as a user-facing client (the GUI); the monitor and the CLI do not call this."""
        if not self._attached and self._context is not None and self._context.client_tracker is not None:
            self._attached = True
            self._context.client_tracker.connected()
        return True

    @exposed
    def server_settings(self) -> str:
        """Return the editable server settings plus whether this client may change them, as JSON."""
        values = read_settings().to_dict()
        values["reconfigurable"] = self._reconfigure_allowed()
        values["last_apply_error"] = config.get_or_default("server.last_apply_error", "") or None
        return json.dumps(values)

    @exposed
    def server_settings_set(self, values_json: str) -> str:
        """Change server settings (JSON object of changed values); restart-needing changes restart the server."""
        error = self._settings_change_error()
        restart = False
        if error is None:
            restart, error = apply_settings_request(values_json)
        if error is not None:
            return json.dumps({"ok": False, "restarting": False, "error": error})
        config.unset("server.last_apply_error")
        if restart:
            self._context.restart_requested = True  # type: ignore[union-attr]
            self._schedule_shutdown()
        return json.dumps({"ok": True, "restarting": restart, "error": None})

    @exposed
    def heartbeat(self) -> str:
        """Respond to a health-check ping from the monitor."""
        self._last_heartbeat = time.time()
        return self.status()  # pyright: ignore[reportReturnType] - @api_response converts the str

    @exposed
    def platform_info(self) -> str:
        """Return the host platform (os, desktop, session, label) as a JSON string."""
        host = detect_platform()
        return json.dumps({**host.to_payload(), "label": host.label()})

    @exposed
    @api_response
    def status(self) -> DeckerServiceStatus:
        """Return service status as a JSON string."""
        return DeckerServiceStatus(
            service_status=DeckerStatusEnum.HEALTHY, last_heartbeat=self._last_heartbeat, version=decker_version()
        )

    @exposed
    @api_response
    def devices_list(self) -> list[DeviceHardwareInfo]:
        """List all connected Stream Deck devices as a JSON string."""
        if self._context is None or self._context.device_manager is None:
            logging.warning("Context or DeviceManager not initialized")
            return []

        return self._context.device_manager.list_devices()

    @exposed
    @api_response
    def plugins_load_failures(self) -> list:
        """List plugins that failed to load at startup as a JSON string."""
        if self._context is None:
            return []
        return [{"plugin_uuid": f.plugin_uuid, "error": f.error} for f in self._context.plugin_manager.load_failures()]

    @exposed
    @api_response
    def plugins_cards_list(self) -> list:
        """List every card declared across all loaded plugin manifests as a JSON string."""
        if self._context is None:
            return []
        return self._context.plugin_manager.list_cards()

    @exposed
    @api_response
    def plugins_presets_list(self) -> list:
        """List every usable preset declared across all loaded plugin manifests as a JSON string."""
        if self._context is None:
            return []
        return self._context.plugin_manager.list_presets()

    @exposed
    @api_response
    def plugins_actions_list(self) -> list:
        """List every action declared across all loaded plugin manifests, with its settings schema."""
        if self._context is None:
            return []
        return self._context.plugin_manager.list_actions()

    @exposed
    @api_response
    def plugin_options(self, plugin_id: str, source: str) -> dict:
        """Return {"ok": bool, "options": [{"label","value"}...], "error": str} for a plugin's dynamic choice source."""
        if self._context is None:
            return {"ok": False, "options": [], "error": "no server"}
        result = self._context.plugin_manager.plugin_options(plugin_id, source)
        return {"ok": result.ok, "options": [dict(o) for o in result.options], "error": result.error}

    @exposed
    @api_response
    def variables_list(self) -> list:
        """List every plugin variable (declared names and registered concrete names) with its owner and value."""
        if self._context is None:
            return []
        return self._context.plugin_manager.variables.listing()

    @exposed
    @api_response
    def plugins_list(self) -> list:
        """List installed plugins with their metadata, card and icon pack counts, and global settings."""
        if self._context is None:
            return []
        manager = self._context.plugin_manager
        return [
            self._plugin_info(
                plugin, manager.global_settings(plugin.manifest.id), manager.is_builtin(plugin.manifest.id)
            )
            for plugin in manager.plugins()
        ]

    @exposed
    @api_response
    def iconpacks_list(self) -> list:
        """List the icon packs: those contributed by installed plugins, then the System Icons categories."""
        if self._context is None:
            return []
        return self._context.plugin_manager.icon_packs.list_packs()

    @exposed
    @api_response
    def iconpack_icons(self, pack_id: str) -> list:
        """List an icon pack's image paths relative to its folder (System Icons: bare icon names)."""
        if self._context is None:
            return []
        return self._context.plugin_manager.icon_packs.list_icons(pack_id)

    @exposed
    def iconpack_icon(self, pack_id: str, name: str) -> str:
        """Return one pack icon as base64, or an empty string if it is missing."""
        if self._context is None:
            return ""
        data = self._context.plugin_manager.icon_packs.read_icon(pack_id, name)
        return base64.b64encode(data).decode("ascii") if data else ""

    @exposed
    def icon_import(self, name: str, filename: str, data: bytes, options: str) -> str:
        """Import an image into the User icon pack (loopback only); returns the result as JSON."""
        if not self._install_allowed():
            raise PermissionError(_REMOTE_INSTALL_DENIED)
        if self._context is None:
            return ImportResult(ok=False, error="server not ready").to_json()
        library = self._context.plugin_manager.icon_packs.user_icons
        label = name if name.strip() else PurePath(filename).stem
        try:
            parsed = ImportOptions.from_json(options)
        except ValueError as exc:
            return ImportResult(ok=False, icon_id=normalize_id(label), error=f"invalid options: {exc}").to_json()
        return library.import_icon(label, bytes(data), parsed).to_json()

    @exposed
    def icon_delete(self, name: str) -> bool:
        """Delete an image from the User icon pack (loopback only); False when it does not exist."""
        if not self._install_allowed():
            raise PermissionError(_REMOTE_INSTALL_DENIED)
        if self._context is None:
            return False
        return self._context.plugin_manager.icon_packs.user_icons.delete_icon(name)

    @exposed
    def icon_import_suggest(self, name: str) -> str:
        """Return the first free User icon id for a name."""
        if self._context is None:
            return normalize_id(name) or "icon"
        return self._context.plugin_manager.icon_packs.user_icons.suggest_id(name)

    @exposed
    def icon_import_preview(self, filename: str, data: bytes, options: str) -> str:
        """Return a base64 PNG (or the SVG) of how an import would look, or an empty string if it is refused."""
        del filename
        if self._context is None:
            return ""
        library = self._context.plugin_manager.icon_packs.user_icons
        try:
            preview = library.preview(bytes(data), ImportOptions.from_json(options))
        except (ValueError, ImageImportError) as exc:
            logging.info("Icon import preview refused: %s", exc)
            return ""
        return base64.b64encode(preview).decode("ascii")

    @exposed
    def plugin_install_allowed(self) -> bool:
        """Report whether this client may install and uninstall plugins."""
        return self._install_allowed()

    @exposed
    def plugin_install(self, filename: str, data: bytes) -> str:
        """Install or upgrade a plugin from .deckerplugin bytes in the background; returns the job id."""
        installer = self._installer()
        return installer.install(filename, bytes(data))

    @exposed
    def plugin_install_status(self, job_id: str) -> str:
        """Return an install job's state (running/succeeded/failed), message and plugin id/version as JSON."""
        return json.dumps(self._installer().status(job_id))

    @exposed
    def plugin_uninstall(self, plugin_id: str) -> bool:
        """Uninstall a plugin, removing its keys from every device and page."""
        installer = self._installer()
        installer.uninstall(plugin_id)
        return True

    @exposed
    def repositories_list(self) -> list[dict[str, Any]]:
        """Return the configured plugin repositories with their last-refresh status."""
        store = self._repository_store()
        if store is None:
            return []
        return [info.to_payload() for info in store.list_repositories()]

    @exposed
    def repository_add(self, url: str) -> str:
        """Add a repository and fetch it. Returns an empty string on success, else an error message."""
        store = self._repository_store()
        if not self._install_allowed():
            return _REMOTE_INSTALL_DENIED
        if store is None:
            return "repositories are not available"
        try:
            store.add(url)
        except RepositoryError as exc:
            return str(exc)
        return ""

    @exposed
    def repository_remove(self, url: str) -> bool:
        """Remove a repository. Returns False when refused or unknown."""
        store = self._repository_store()
        if store is None or not self._install_allowed():
            return False
        return store.remove(url)

    @exposed
    def repositories_check(self) -> dict[str, Any]:
        """Start a background refresh of every repository; `repositories_check_finished` reports the outcome."""
        checker = self._update_checker()
        if checker is None:
            return {"started": False, "reason": "repositories are not available"}
        if not checker.check_async():
            return {"started": False, "reason": "a check is already running"}
        return {"started": True}

    @exposed
    def repository_catalog(self) -> list[dict[str, Any]]:
        """Return every repository plugin with its versions and installed/latest-compatible state."""
        store = self._repository_store()
        if store is None or self._context is None:
            return []
        installed = {item.manifest.id: item.manifest.version for item in self._context.plugin_manager.plugins()}
        manager = self._context.plugin_manager
        host = detect_platform()
        return [
            _catalog_item(entry, installed.get(entry.plugin.id), host)
            for entry in store.catalog()
            if not manager.is_builtin(entry.plugin.id)
        ]

    @exposed
    def repository_icon(self, plugin_id: str) -> str:
        """Return a repository plugin's icon as base64, or an empty string."""
        store = self._repository_store()
        data = store.icon(plugin_id) if store is not None else None
        return base64.b64encode(data).decode("ascii") if data else ""

    @exposed
    def plugin_install_from_repository(self, plugin_id: str, version: str = "") -> str:
        """Install a repository plugin version (default: latest compatible); returns a job id or an `error:` message."""
        if not self._install_allowed():
            return f"error: {_REMOTE_INSTALL_DENIED}"
        store = self._repository_store()
        installer = self._context.plugin_installer if self._context is not None else None
        if store is None or installer is None:
            return "error: plugin installer is not available"
        entry = store.find(plugin_id)
        if entry is None:
            return f"error: {plugin_id} is not in any repository"
        chosen = _pick_version(entry.plugin.versions, version)
        if chosen is None:
            return (
                f"error: no compatible version of {plugin_id} found" if not version else f"error: {version} unavailable"
            )
        if not is_compatible(chosen.decker):
            return f"error: {plugin_id} {chosen.version} requires Decker {chosen.decker}"
        try:
            return installer.install_from_repository(plugin_id, chosen)
        except PluginInstallError as exc:
            return f"error: {exc}"

    @exposed
    def plugin_updates(self) -> dict[str, Any]:
        """Return the current update report: installed plugins with updates and plugins missing from this machine."""
        checker = self._update_checker()
        return checker.report().to_payload() if checker is not None else {"updates": [], "missing": []}

    @exposed
    def update_check_schedule(self) -> str:
        """Return the update check schedule name."""
        checker = self._update_checker()
        return checker.schedule() if checker is not None else "weekly"

    @exposed
    def update_check_set_schedule(self, name: str) -> bool:
        """Set the update check schedule. Returns False when refused or the name is unknown."""
        checker = self._update_checker()
        if checker is None or not self._install_allowed():
            return False
        try:
            checker.set_schedule(name)
        except ValueError:
            return False
        return True

    @exposed
    def plugin_usage(self, plugin_id: str) -> int:
        """Return how many keys across all devices and pages use the plugin's cards."""
        if self._context is None or self._context.plugin_installer is None:
            return 0
        return self._context.plugin_installer.usage(plugin_id)

    @exposed
    def plugin_update_global_settings(self, plugin_id: str, settings_json: str) -> bool:
        """Validate, store and send plugin-wide settings. Returns False for an unknown plugin."""
        if self._context is None:
            return False
        raw = json.loads(settings_json)
        if not isinstance(raw, dict):
            return False
        return self._context.plugin_manager.update_global_settings(plugin_id, raw) is not None

    @exposed
    def key_assign(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        device_id: str,
        page_id: str,
        key_index: int,
        plugin_uuid: str,
        card_uuid: str,
        context_id: str,
        settings_json: str = "{}",
        bindings_json: str = "{}",
    ) -> bool:
        """
        Assign a plugin card to a key slot on a page, activating it if the page is on the device.

        Returns:
            bool: False if the page does not exist on the device.
        """
        if self._context is None or self._context.page_store.get(device_id, page_id) is None:
            return False
        settings: dict[str, Any] = json.loads(settings_json)
        bindings: dict[str, Any] = json.loads(bindings_json)
        target = CardTarget(
            plugin_uuid=plugin_uuid,
            card_uuid=card_uuid,
            context_id=context_id,
            device_id=device_id,
            key_index=key_index,
        )
        self._place_card(target, page_id, settings, bindings, {})
        return True

    @exposed
    @api_response
    def key_assign_preset(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, device_id: str, page_id: str, key_index: int, plugin_uuid: str, preset_id: str, context_id: str
    ) -> dict:
        """
        Drop a plugin preset on a key as a Blank card carrying its appearance and bindings, in one step.

        Returns:
            dict: ``{"ok": bool, "errors": {"<gesture>[.<arg>]": message}}``; ok is False for an unknown page or
            preset (nothing written); the errors name bindings still to be completed, which are stored anyway.
        """
        if self._context is None or self._context.page_store.get(device_id, page_id) is None:
            return {"ok": False, "errors": {}}
        pm = self._context.plugin_manager
        preset = pm.preset(plugin_uuid, preset_id)
        if preset is None or not pm.preset_usable(preset):
            return {"ok": False, "errors": {}}
        appearance = copy.deepcopy(preset.appearance)
        title = appearance.get("title")
        title = dict(title) if isinstance(title, dict) else {}
        title.setdefault("text", preset.name)
        appearance["title"] = title
        bindings = {
            gesture: binding.to_json() for gesture, binding in preset.bindings.items() if not binding.is_nothing
        }
        target = CardTarget(
            plugin_uuid=BLANK_PLUGIN_ID,
            card_uuid=BLANK_CARD_ID,
            context_id=context_id,
            device_id=device_id,
            key_index=key_index,
        )
        self._place_card(target, page_id, {}, bindings, appearance)
        return {"ok": True, "errors": pm.validate_bindings(target, bindings)}

    def _place_card(
        self,
        target: CardTarget,
        page_id: str,
        settings: dict[str, Any],
        bindings: dict[str, Any],
        appearance: dict[str, Any],
    ) -> None:
        """Replace the key's card: deactivate the old one, store the new, and activate it if the page is live."""
        assert self._context is not None
        key_store = self._context.key_assignment_store
        pm = self._context.plugin_manager
        is_active = self._is_active_page(target.device_id, page_id)
        existing = key_store.get(target.device_id, page_id, target.key_index)
        if existing is not None and is_active:
            pm.deactivate_card(existing.target)
        key_store.assign(target.device_id, page_id, target.key_index, target, settings, bindings, appearance)
        pm.store_settings(target, settings)
        pm.set_bindings(target, bindings)
        if appearance:
            pm.set_appearance(target, appearance)
        if is_active:
            self._context.device_manager.open_device(target.device_id)
            pm.activate_card(target, appearance)

    @exposed
    def key_unassign(self, device_id: str, page_id: str, key_index: int) -> bool:
        """
        Remove a plugin card from a key slot on a page, deactivating it if the page is on the device.

        Returns:
            bool: True if an assignment existed and was removed.
        """
        if self._context is None:
            return False
        key_store = self._context.key_assignment_store
        assignment = key_store.get(device_id, page_id, key_index)
        if assignment is None:
            return False
        key_store.unassign(device_id, page_id, key_index)
        if self._is_active_page(device_id, page_id):
            self._context.plugin_manager.deactivate_card(assignment.target)
            self._context.device_manager.clear_key_state(device_id, key_index)
        else:
            self._context.device_manager.delete_stored_key_state(device_id, page_id, key_index)
        return True

    @exposed
    def key_move(self, device_id: str, page_id: str, from_index: int, to_index: int) -> bool:
        """
        Move a key's card to another slot of the page, replacing whatever was there.

        The card instance keeps its identity, settings and appearance.

        Returns:
            bool: False (nothing changed) for an unknown page, an empty source, the same slot or a bad index.
        """
        source = self._movable_source(device_id, page_id, from_index, to_index)
        if source is None or self._context is None:
            return False
        ctx = self._context
        active = self._is_active_page(device_id, page_id)
        replaced = ctx.key_assignment_store.get(device_id, page_id, to_index)
        # the store goes first: a refused or failed write must leave the live instances as they were
        if not ctx.key_assignment_store.move(device_id, page_id, from_index, to_index):
            return False
        if active:
            ctx.plugin_manager.deactivate_card(source.target)
            if replaced is not None:
                ctx.plugin_manager.deactivate_card(replaced.target)
            ctx.device_manager.blank_key(device_id, from_index)
            ctx.device_manager.clear_key_state(device_id, to_index)
            self._activate_slot(device_id, page_id, to_index)
        else:
            ctx.device_manager.delete_stored_key_state(device_id, page_id, from_index)
            ctx.device_manager.delete_stored_key_state(device_id, page_id, to_index)
        return True

    @exposed
    def key_copy(self, device_id: str, page_id: str, from_index: int, to_index: int) -> bool:
        """
        Copy a key's card (settings and appearance) to another slot of the page as a new instance.

        Returns:
            bool: False (nothing changed) for an unknown page, an empty source, the same slot or a bad index.
        """
        source = self._movable_source(device_id, page_id, from_index, to_index)
        if source is None or self._context is None:
            return False
        ctx = self._context
        active = self._is_active_page(device_id, page_id)
        replaced = ctx.key_assignment_store.get(device_id, page_id, to_index)
        if ctx.key_assignment_store.copy_key(device_id, page_id, from_index, to_index, uuid.uuid4().hex) is None:
            return False
        if active:
            if replaced is not None:
                ctx.plugin_manager.deactivate_card(replaced.target)
            ctx.device_manager.clear_key_state(device_id, to_index)
            self._activate_slot(device_id, page_id, to_index)
        else:
            ctx.device_manager.delete_stored_key_state(device_id, page_id, to_index)
        return True

    def _movable_source(self, device_id: str, page_id: str, from_index: int, to_index: int) -> KeyAssignment | None:
        """The assignment to move or copy, or None when the request is not valid (see key_move)."""
        if self._context is None or self._context.page_store.get(device_id, page_id) is None:
            return None
        indexes = (from_index, to_index)
        if any(isinstance(i, bool) or not isinstance(i, int) for i in indexes) or from_index == to_index:
            return None
        device = self._context.device_manager.open_device(device_id)
        if device is None or not all(0 <= i < device.key_count for i in indexes):
            return None
        return self._context.key_assignment_store.get(device_id, page_id, from_index)

    def _activate_slot(self, device_id: str, page_id: str, key_index: int) -> None:
        """Bring the (stored) assignment of a slot live on the device."""
        assert self._context is not None
        assignment = self._context.key_assignment_store.get(device_id, page_id, key_index)
        if assignment is None:
            return
        pm = self._context.plugin_manager
        pm.store_settings(assignment.target, assignment.settings)
        pm.set_bindings(assignment.target, assignment.bindings)
        pm.activate_card(assignment.target, assignment.appearance)

    @exposed
    @api_response
    def key_update_settings(self, device_id: str, page_id: str, key_index: int, settings_json: str) -> dict:
        """
        Validate and save per-key card settings; a live card is notified via on_settings_changed.

        Returns:
            dict: ``{"ok": bool, "errors": {field: message}}``; on errors nothing is persisted.
        """
        if self._context is None:
            return {"ok": False, "errors": {}}
        settings: dict[str, Any] = json.loads(settings_json)
        key_store = self._context.key_assignment_store
        assignment = key_store.get(device_id, page_id, key_index)
        if assignment is None:
            return {"ok": False, "errors": {}}
        errors = self._context.plugin_manager.validate_card_settings(assignment.target, settings)
        if errors:
            return {"ok": False, "errors": errors}
        if not key_store.update_settings(device_id, page_id, key_index, settings):
            return {"ok": False, "errors": {}}
        self._context.plugin_manager.store_settings(assignment.target, settings)
        if self._is_active_page(device_id, page_id):
            self._context.plugin_manager.dispatch_settings_changed(assignment.target)
        return {"ok": True, "errors": {}}

    @exposed
    @api_response
    def key_update_bindings(self, device_id: str, page_id: str, key_index: int, bindings_json: str) -> dict:
        """
        Validate and save a key's gesture bindings; a live card re-registers its gestures.

        Returns:
            dict: ``{"ok": bool, "errors": {"<gesture>[.<arg>]": message}}``; on errors nothing is persisted.
        """
        if self._context is None:
            return {"ok": False, "errors": {}}
        stored = json.loads(bindings_json)
        key_store = self._context.key_assignment_store
        assignment = key_store.get(device_id, page_id, key_index)
        if assignment is None or not isinstance(stored, dict):
            return {"ok": False, "errors": {}}
        errors = self._context.plugin_manager.validate_bindings(assignment.target, stored)
        if errors:
            return {"ok": False, "errors": errors}
        if not key_store.update_bindings(device_id, page_id, key_index, stored):
            return {"ok": False, "errors": {}}
        if self._is_active_page(device_id, page_id):
            self._context.plugin_manager.set_bindings(assignment.target, stored)
        return {"ok": True, "errors": {}}

    @exposed
    def key_update_appearance(self, device_id: str, page_id: str, key_index: int, appearance_json: str) -> bool:
        """
        Update the Decker-drawn appearance (background, title) of a key; a live key is redrawn at once.

        Returns:
            bool: True if the slot was assigned and the appearance was persisted.
        """
        if self._context is None:
            return False
        raw = json.loads(appearance_json)
        if not isinstance(raw, dict):
            return False
        appearance = {k: v for k, v in raw.items() if k in APPEARANCE_KEYS}
        key_store = self._context.key_assignment_store
        if not key_store.update_appearance(device_id, page_id, key_index, appearance):
            return False
        assignment = key_store.get(device_id, page_id, key_index)
        if assignment is not None and self._is_active_page(device_id, page_id):
            self._context.plugin_manager.set_appearance(assignment.target, appearance)
        return True

    @exposed
    def key_preview(self, device_id: str, page_id: str, key_index: int, appearance_json: str) -> str:
        """
        Compose a live key with a draft appearance without saving or showing it.

        Returns:
            str: base64 PNG, or "" if the key is unassigned, not live or the draft is invalid.
        """
        if self._context is None or not isinstance(appearance_json, str):
            return ""
        try:
            raw = json.loads(appearance_json)
            assignment = self._context.key_assignment_store.get(device_id, page_id, key_index)
            if not isinstance(raw, dict) or assignment is None:
                return ""
            draft = {k: v for k, v in raw.items() if k in APPEARANCE_KEYS}
            image = self._context.plugin_manager.preview_image(assignment.target, draft)
            return base64.b64encode(image).decode("ascii") if image else ""
        except Exception:  # pylint: disable=broad-except
            logging.debug("key_preview failed", exc_info=True)
            return ""

    @exposed
    def key_animation(self, animation_id: str) -> str:
        """
        Return the composed animated image of an animated key, by the animation_id of its state event.

        Returns:
            str: base64 animated WebP (GIF without WebP support), or "" if the id is unknown or cannot be built.
        """
        if self._context is None or not isinstance(animation_id, str):
            return ""
        try:
            factory = self._context.device_manager.animation_factory
            spec = factory.spec_for(animation_id) if factory is not None else None
            image = factory.preview_webp(spec) if factory is not None and spec is not None else None
            return base64.b64encode(image).decode("ascii") if image else ""
        except Exception:  # pylint: disable=broad-except
            logging.debug("key_animation failed", exc_info=True)
            return ""

    @exposed
    @api_response
    def pages_list(self, device_id: str, profile_id: int = -1) -> DevicePages | None:
        """Return a profile's pages in order, its default page and the page shown on the device.

        A negative profile_id means the device's active profile; an unknown profile returns nothing.
        """
        if self._context is None or isinstance(profile_id, bool) or not isinstance(profile_id, int):
            return None
        if profile_id < 0:
            return self._context.page_controller.device_pages(device_id)
        return self._context.page_controller.device_pages(device_id, profile_id)

    @exposed
    @api_response
    def profiles_list(self, device_id: str) -> DeviceProfiles | None:
        """Return the device's profiles in dropdown order and its active profile."""
        if self._context is None:
            return None
        return self._context.page_controller.device_profiles(device_id)

    @exposed
    @api_response
    def app_rules_list(self, device_id: str) -> Any:
        """A device's app rules in priority order plus its default rule."""
        controller = self._rule_controller()
        return controller.rules(device_id) if controller is not None else None

    @exposed
    @api_response
    def app_rule_add(self, device_id: str, rule: dict) -> AppRule | None:
        """Append an app rule; returns the stored rule, or None when it is invalid."""
        controller, parsed = self._rule_controller(), _rule_from_wire(rule)
        return controller.add_rule(device_id, parsed) if controller is not None and parsed is not None else None

    @exposed
    @api_response
    def app_rule_update(self, device_id: str, rule: dict) -> bool:
        """Replace an app rule. Returns False for an invalid or unknown rule."""
        controller, parsed = self._rule_controller(), _rule_from_wire(rule)
        return controller is not None and parsed is not None and controller.update_rule(device_id, parsed)

    @exposed
    @api_response
    def app_rule_delete(self, device_id: str, rule_id: str) -> bool:
        """Delete an app rule. Returns False for an unknown rule."""
        controller = self._rule_controller()
        return controller is not None and controller.delete_rule(device_id, rule_id)

    @exposed
    @api_response
    def app_rule_move(self, device_id: str, rule_id: str, position: int) -> bool:
        """Move an app rule to a position in the priority order. Returns False for an unknown rule."""
        controller = self._rule_controller()
        valid = not isinstance(position, bool) and isinstance(position, int)
        return controller is not None and valid and controller.move_rule(device_id, rule_id, position)

    @exposed
    @api_response
    def app_default_rule_set(self, device_id: str, enabled: bool, profile_id: int, page_id: str) -> bool:
        """Set the rule used when no app rule matches. Returns False for an invalid target."""
        controller = self._rule_controller()
        valid = not isinstance(profile_id, bool) and isinstance(profile_id, int) and isinstance(page_id, str)
        return (
            controller is not None
            and valid
            and controller.set_default_rule(device_id, DefaultRule(bool(enabled), profile_id, page_id))
        )

    @exposed
    @api_response
    def window_detection_status(self) -> dict[str, Any] | None:
        """Whether focused-window detection works here and the last window (its title only for local callers)."""
        watcher = self._context.window_watcher if self._context is not None else None
        return status_payload(watcher.status(), _is_loopback(self._peer_host)) if watcher is not None else None

    @exposed
    @api_response
    def window_pick(self) -> bool:
        """Start a window pick; the result arrives later as a window_picked event for this client only."""
        context, conn = self._context, self._conn
        watcher = context.window_watcher if context is not None else None
        if context is None or watcher is None or conn is None:
            return False
        loopback = _is_loopback(self._peer_host)

        def deliver(window: Any) -> None:
            payload = json.dumps({"window": window_payload(window, loopback)})
            context.event_bus.push_to(conn, WINDOW_PICKED_EVENT, payload)

        return watcher.pick(deliver)

    def _rule_controller(self) -> Any:
        return self._context.app_rule_controller if self._context is not None else None

    @exposed
    @api_response
    def screensaver_get(self, device_id: str) -> dict[str, Any] | None:
        """A device's screensaver settings plus `active` and `last_error`, or None for an unknown device."""
        try:
            return self._saver().get(device_id)
        except ValueError:  # an unknown device
            logging.debug("screensaver_get failed", exc_info=True)
            return None
        except Exception:  # pylint: disable=broad-except
            logging.exception("screensaver_get failed")
            return None

    @exposed
    @api_response
    def screensaver_set(self, device_id: str, settings_json: str) -> dict[str, Any]:
        """Validate and store settings (JSON); never activates. Returns {"ok", "error"}; paths need a local caller."""
        try:
            self._saver().set(device_id, settings_json, loopback=_is_loopback(self._peer_host))
        except (ValueError, PermissionError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:  # pylint: disable=broad-except
            logging.exception("screensaver_set failed")
            return {"ok": False, "error": _INTERNAL_ERROR}
        return {"ok": True, "error": ""}

    @exposed
    @api_response
    def screensaver_activate(self, device_id: str) -> bool:
        """Show the screensaver now. Returns False for an unknown device or one that is not open."""
        try:
            return bool(self._saver().activate(device_id))
        except ValueError:  # an unknown device
            logging.debug("screensaver_activate failed", exc_info=True)
            return False
        except Exception:  # pylint: disable=broad-except
            logging.exception("screensaver_activate failed")
            return False

    @exposed
    @api_response
    def screensaver_deactivate(self, device_id: str) -> bool:
        """Wake the device. Returns False for an unknown device or one that was not showing the screensaver."""
        try:
            return bool(self._saver().deactivate(device_id))
        except ValueError:  # an unknown device
            logging.debug("screensaver_deactivate failed", exc_info=True)
            return False
        except Exception:  # pylint: disable=broad-except
            logging.exception("screensaver_deactivate failed")
            return False

    @exposed
    @api_response
    def screensaver_preview(self, device_id: str, settings_json: str) -> dict[str, str]:
        """Compose draft settings at the device's layout: {"png": base64} or {"error": message}."""
        try:
            png = self._saver().preview(device_id, settings_json, loopback=_is_loopback(self._peer_host))
        except (ValueError, PermissionError) as exc:
            return {"error": str(exc)}
        except Exception:  # pylint: disable=broad-except
            logging.exception("screensaver_preview failed")
            return {"error": _INTERNAL_ERROR}
        return {"png": base64.b64encode(png).decode("ascii")}

    def _saver(self) -> Any:
        controller = self._context.screensaver_controller if self._context is not None else None
        if controller is None:
            raise RuntimeError("screensaver is not available")
        return controller

    @exposed
    @api_response
    def profile_add(self, device_id: str, name: str = "") -> ProfileInfo | None:
        """Append a new profile with one page (named "Profile <id>" unless a name is given)."""
        if self._context is None or not isinstance(name, str):
            return None
        return self._context.page_controller.add_profile(device_id, name or None)

    @exposed
    def profile_rename(self, device_id: str, profile_id: int, name: str) -> bool:
        """Rename a profile. Returns False for an unknown profile or a blank name."""
        controller = self._controller_for(profile_id)
        return (
            controller is not None and isinstance(name, str) and controller.rename_profile(device_id, profile_id, name)
        )

    @exposed
    def profile_set_icon(self, device_id: str, profile_id: int, icon: str, color: str = "") -> bool:
        """Set a profile's icon ref ("" = the Decker icon) and SVG fill colour; False for an unknown profile or bad input."""
        controller = self._controller_for(profile_id)
        valid = isinstance(icon, str) and (icon in ("", DECKER_ICON_REF) or parse_icon_ref(icon) is not None)
        valid = valid and isinstance(color, str) and (color == "" or is_color(color))
        return valid and controller is not None and controller.set_profile_icon(device_id, profile_id, icon, color)

    @exposed
    def profile_delete(self, device_id: str, profile_id: int) -> bool:
        """Delete a profile with its pages and keys. The last profile cannot be deleted."""
        controller = self._controller_for(profile_id)
        return controller is not None and controller.delete_profile(device_id, profile_id)

    @exposed
    @api_response
    def profile_clone(self, device_id: str, profile_id: int) -> ProfileInfo | None:
        """Copy a profile with its pages and keys; the copy does not become active."""
        controller = self._controller_for(profile_id)
        return controller.clone_profile(device_id, profile_id) if controller is not None else None

    @exposed
    def profile_activate(self, device_id: str, profile_id: int) -> bool:
        """Show a profile's default page on the device. Returns False for an unknown profile."""
        controller = self._controller_for(profile_id)
        return controller is not None and controller.activate_profile(device_id, profile_id)

    def _controller_for(self, profile_id: object) -> "PageController | None":
        """The page controller when a context exists and profile_id is a real int (not a bool)."""
        if self._context is None or isinstance(profile_id, bool) or not isinstance(profile_id, int):
            return None
        return self._context.page_controller

    @exposed
    @api_response
    def page_add(self, device_id: str, name: str = "") -> PageInfo | None:
        """Append a new empty page to the device ("Page N" unless a name is given)."""
        if self._context is None:
            return None
        return self._context.page_controller.add_page(device_id, name or None)

    @exposed
    def page_rename(self, device_id: str, page_id: str, name: str) -> bool:
        """Rename a page. Returns False for an unknown page or a blank name."""
        return self._context is not None and self._context.page_controller.rename_page(device_id, page_id, name)

    @exposed
    def page_move(self, device_id: str, page_id: str, new_index: int) -> bool:
        """Move a page to new_index within its profile. Returns False for an unknown page."""
        return self._context is not None and self._context.page_controller.move_page(device_id, page_id, new_index)

    @exposed
    def page_delete(self, device_id: str, page_id: str) -> bool:
        """Delete a page and its key assignments. The last page cannot be deleted."""
        return self._context is not None and self._context.page_controller.delete_page(device_id, page_id)

    @exposed
    def page_set_default(self, device_id: str, page_id: str) -> bool:
        """Make a page the one shown when the server starts. Returns False for an unknown page."""
        return self._context is not None and self._context.page_controller.set_default_page(device_id, page_id)

    @exposed
    def page_activate(self, device_id: str, page_id: str) -> bool:
        """Show a page on the device, switching which cards are live. Returns False for an unknown page."""
        return self._context is not None and self._context.page_controller.activate(device_id, page_id)

    @exposed
    @api_response
    def key_get_states(self, device_id: str) -> list[KeyState]:
        """Return full runtime state for every key on the page currently shown on the device.

        Covers all keys (not just assigned ones). Unassigned keys have None for plugin fields
        and None for image_b64 if no image has been pushed yet.
        """
        if self._context is None:
            return []
        dm = self._context.device_manager
        device = dm.open_device(device_id)
        if device is None:
            return []
        hw_info = device.get_hw_info()
        key_store = self._context.key_assignment_store
        page_id = self._context.page_controller.active_page_id(device_id)
        device_state = dm.get_device_state(device_id)
        result: list[KeyState] = []
        for key_index, key_info in enumerate(hw_info.keys):
            assignment = key_store.get(device_id, page_id, key_index)
            per_key = device_state.get_key_state(key_index) if device_state is not None else None
            raw_image = per_key.image if per_key is not None else None
            image_b64 = base64.b64encode(raw_image).decode("ascii") if raw_image else None
            settings, appearance, defaults = self._resolved_key(assignment) if assignment else ({}, {}, {})
            result.append(
                KeyState(
                    key_index=key_index,
                    row=key_info.row,
                    col=key_info.col,
                    state=per_key.press_state if per_key is not None else device.get_key_state(key_index),
                    image_b64=image_b64,
                    label=per_key.label if per_key is not None else "",
                    plugin_uuid=assignment.target.plugin_uuid if assignment else None,
                    card_uuid=assignment.target.card_uuid if assignment else None,
                    context_id=assignment.target.context_id if assignment else None,
                    settings=settings,
                    appearance=appearance,
                    appearance_defaults=defaults,
                    bindings=assignment.bindings if assignment else {},
                )
            )
        return result

    def _resolved_key(self, assignment: KeyAssignment) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Return (settings, appearance, appearance defaults); stored values stand in when the card is broken."""
        assert self._context is not None
        manager = self._context.plugin_manager
        try:
            settings = manager.complete_settings(assignment.target, assignment.settings)
            appearance, defaults = manager.appearance_for(assignment.target, assignment.appearance)
        except Exception:  # pylint: disable=broad-exception-caught
            logging.warning("Cannot resolve key state for %s", assignment.target, exc_info=True)
            stored_settings, stored_appearance = assignment.settings, assignment.appearance
            return (
                dict(stored_settings) if isinstance(stored_settings, dict) else {},
                dict(stored_appearance) if isinstance(stored_appearance, dict) else {},
                {},
            )
        return settings, appearance, defaults

    @exposed
    def key_simulate_press(self, device_id: str, key_index: int) -> bool:
        """
        Simulate a short key press (key_down immediately followed by key_up).

        Returns:
            bool: True if a card was assigned to this key.
        """
        return self._simulate_press(device_id, key_index, long_press=False)

    @exposed
    def key_simulate_long_press(self, device_id: str, key_index: int) -> bool:
        """
        Simulate a long key press (key_down, hold for 600 ms, then key_up).

        Returns:
            bool: True if a card was assigned to this key.
        """
        return self._simulate_press(device_id, key_index, long_press=True)

    @exposed
    @api_response
    def commands(self) -> dict:
        """Return metadata for all user-defined exposed commands as a JSON string."""
        result: dict[str, dict[str, str]] = {}
        for attr, method in self.__class__.__dict__.items():
            if not callable(method) or not getattr(method, "__exposed__", False) or attr == "commands":
                continue
            cmd_path = ".".join(attr.split("_"))
            annotations = getattr(method, "__annotations__", {})
            ret = annotations.get("return", type(None))
            params = [p for p in inspect.signature(method).parameters if p != "self"]
            result[cmd_path] = {
                "description": (inspect.getdoc(method) or "").split("\n", maxsplit=1)[0],
                "return_type": getattr(ret, "__name__", str(ret)),
                "params": ", ".join(params),
            }
        return result

    @exposed
    def set_image(self, device_id: str, key_index: int, image_bytes: bytes) -> bool:
        """
        Set the image on a key from raw bytes of a PNG file.

        Returns:
            bool: True if the image was sent to an open device, False if the device is not open.
        """
        logging.info(
            "Received set_image command for device %s key %d with %d bytes", device_id, key_index, len(image_bytes)
        )
        if self._context is None or self._context.device_manager is None:
            logging.warning("Context not initialized")
            return False
        device = self._context.device_manager.open_device(device_id)
        if device is None:
            logging.warning("DeckerService.set_image: device %s not open", device_id)
            return False

        device.set_image_bytes(key_index, image_bytes)
        return True

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _schedule_shutdown(self) -> None:
        def _shutdown() -> None:
            time.sleep(0.1)
            self._context.server.close()  # type: ignore[union-attr]

        threading.Thread(target=_shutdown, daemon=True).start()
        logging.info("Server shutdown scheduled")

    def _reconfigure_allowed(self) -> bool:
        return self._context is not None and self._context.reconfigure_policy.allows(self._peer_host)

    def _settings_change_error(self) -> str | None:
        if self._context is None or self._context.server is None:
            return "server is not ready"
        if not self._reconfigure_allowed():
            return "reconfiguration is not allowed (start the server with --allow-client-reconfiguration)"
        if self._context.restart_requested:
            return "a restart is already pending"
        return None

    def _install_allowed(self) -> bool:
        return _is_loopback(self._peer_host) or config.get_as_or_default(
            "server.allow_remote_plugin_install", bool, False
        )

    def _repository_store(self) -> RepositoryStore | None:
        return self._context.repository_store if self._context is not None else None

    def _update_checker(self) -> UpdateChecker | None:
        return self._context.update_checker if self._context is not None else None

    def _installer(self) -> Any:
        if not self._install_allowed():
            raise PermissionError(_REMOTE_INSTALL_DENIED)
        if self._context is None or self._context.plugin_installer is None:
            raise RuntimeError("plugin installer is not available")
        return self._context.plugin_installer

    @staticmethod
    def _plugin_info(plugin: InstalledPlugin, global_settings: dict[str, Any], builtin: bool = False) -> dict[str, Any]:
        manifest = plugin.manifest
        icon = read_icon(plugin.plugin_dir, manifest.icon)
        return {
            "id": manifest.id,
            "name": manifest.name,
            "version": manifest.version,
            "author": manifest.author,
            "description": manifest.description,
            "cards": len(manifest.cards),
            "icon_packs": len(manifest.icon_packs),
            "variables": len(manifest.variables),
            "icon_b64": base64.b64encode(icon).decode("ascii") if icon else "",
            "global_settings_schema": schema_to_json(manifest.global_settings),
            "global_settings": global_settings,
            "builtin": builtin,
            **_platform_fields(manifest.platforms),
            "supported": platform_supports(manifest.platforms, detect_platform()),
        }

    def _is_active_page(self, device_id: str, page_id: str) -> bool:
        return self._context is not None and self._context.page_controller.active_page_id(device_id) == page_id

    def _simulate_press(self, device_id: str, key_index: int, long_press: bool) -> bool:
        if self._context is None:
            return False
        page_id = self._context.page_controller.active_page_id(device_id)
        assignment = self._context.key_assignment_store.get(device_id, page_id, key_index)
        if assignment is None:
            return False
        target = assignment.target
        self._context.plugin_manager.dispatch_key_down(target)
        if long_press:
            time.sleep(_LONG_PRESS_HOLD_S)
        self._context.plugin_manager.dispatch_key_up(target)
        return True
