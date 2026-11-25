"""Owns the RPyC connection and its background worker thread. No Qt imports."""

import base64
import binascii
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, fields
from enum import StrEnum
from typing import Any

import rpyc.utils.factory
from rpyc.core.protocol import Connection
from rpyc.core.service import Service
from rpyc.utils.helpers import BgServingThread, async_

from decker.client import config as client_config
from decker.client.events import (
    ActionFailedEvent,
    AppRulesUpdatedEvent,
    CardListUpdatedEvent,
    DetectionStatusEvent,
    DeviceListUpdatedEvent,
    KeyDisplayStatesUpdatedEvent,
    KeyEvent,
    OpenUrlRequestedEvent,
    PagesUpdatedEvent,
    PluginInstallFinishedEvent,
    PluginInstallProgressEvent,
    PluginLoadFailedEvent,
    PluginsChangedEvent,
    PluginUpdatesAvailableEvent,
    PresetListUpdatedEvent,
    ProfilesUpdatedEvent,
    RepositoriesCheckFinishedEvent,
    ScreensaverSettingsEvent,
    ScreensaverStateEvent,
    ServerConnectedEvent,
    ServerDisconnectedEvent,
    WindowFocusedEvent,
    WindowPickedEvent,
)
from decker.client.events.event_manager import EventManager
from decker.client.server_settings import ServerSettingsInfo, parse_server_settings
from decker.common.model import (
    AppRule,
    DefaultRule,
    DeviceHardwareInfo,
    DevicePages,
    DeviceProfiles,
    DeviceRules,
    KeyInfo,
    MatchCondition,
    PageInfo,
    ProfileInfo,
    ScreensaverSettings,
    WindowInfo,
)

_SERVER_UNAVAILABLE = "server unavailable"
_RETRY_INTERVAL_S = 3.0
_WINDOW_EVENT_TYPES = ("app_rules_changed", "window_detection_changed", "window_focused", "window_picked")
_SCREENSAVER_EVENT_TYPES = ("screensaver_changed", "screensaver_settings_changed")
_HEARTBEAT_INTERVAL_S = 5.0
_LONG_CALL_TIMEOUTS: dict[str, float] = {"plugin_install": 120.0, "repositories_check": 120.0, "repository_add": 60.0}


class ServerState(StrEnum):
    """Connection state of the client towards the server, as seen by ConnectionManager."""

    DISCONNECTED = "disconnected"  # no connection
    INITIALIZING = "initializing"  # connected, initial data not yet pulled
    UNHEALTHY = "unhealthy"  # connected, but the last heartbeat reported a non-healthy status
    HEALTHY = "healthy"  # connected and the last heartbeat reported healthy


class _ClientService(Service):
    """Exposed to the server so it can push events to this client."""

    def __init__(self, on_event: Callable[[str, str], None]) -> None:
        super().__init__()
        self._on_event = on_event

    def exposed_on_server_event(self, event_type: str, payload: str) -> None:
        """Called by the server to deliver a push event."""
        try:
            self._on_event(event_type, payload)
        except Exception as exc:  # pylint: disable=broad-except
            logging.warning("Error handling server event %s: %s", event_type, exc)


class ConnectionManager:  # pylint: disable=too-many-instance-attributes
    """Maintains a connection to the Decker server on a background thread.

    Pushes events to EventManager as connection state changes. Owns the worker thread's full lifecycle.
    Server-initiated push events are received via _ClientService / BgServingThread.
    """

    def __init__(self) -> None:
        self._conn: Connection | None = None
        self._bg_server: BgServingThread | None = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._reconnect_requested = threading.Event()
        self._device_refresh_requested = threading.Event()
        self._layout_refresh_requested = threading.Event()
        self._cards_refresh_requested = threading.Event()
        self._devices_refresh_requested = threading.Event()
        self._rules_refresh_requested = threading.Event()
        self._screensaver_refresh_lock = threading.Lock()
        self._screensaver_refresh_devices: set[str] = set()
        self._detection_refresh_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self.state: ServerState = ServerState.DISCONNECTED
        self._reported_plugin_failures: set[str] = set()
        self._last_devices: list[DeviceHardwareInfo] | None = None
        self._last_cards: list[dict[str, Any]] | None = None
        self._last_presets: list[dict[str, Any]] | None = None
        self.server_settings: ServerSettingsInfo | None = None

    @property
    def is_connected(self) -> bool:
        """True while a connection to the server is open, regardless of its health."""
        return self.state != ServerState.DISCONNECTED

    @property
    def is_ready(self) -> bool:
        """True once the initial data pull has completed on the current connection."""
        return self.state in (ServerState.HEALTHY, ServerState.UNHEALTHY)

    def start(self) -> None:
        """Start the background connection thread. No-op if already running."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the worker thread to stop and close the connection."""
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None:
            self._thread.join(timeout=_HEARTBEAT_INTERVAL_S)
            self._thread = None
        self._close_connection()

    def request_reconnect(self) -> None:
        """Drop the current connection (if any) and reconnect using the current config."""
        self._reconnect_requested.set()
        self._wake_event.set()

    def request_device_refresh(self) -> None:
        """Re-fetch the active device's pages and key states on the worker thread."""
        self._device_refresh_requested.set()
        self._wake_event.set()

    @property
    def devices(self) -> list[DeviceHardwareInfo]:
        """The devices the server last listed (empty before the first list arrives)."""
        return list(self._last_devices or [])

    def request_layout_refresh(self) -> None:
        """Re-fetch only the active device's profiles, pages and key faces (what a page or profile switch changes)."""
        self._layout_refresh_requested.set()
        self._wake_event.set()

    def request_devices_refresh(self) -> None:
        """Re-fetch the device list (and so notice a device that went away or came back) on the worker thread."""
        self._devices_refresh_requested.set()
        self._wake_event.set()

    def request_cards_refresh(self) -> None:
        """Re-fetch the plugin card list on the worker thread."""
        self._cards_refresh_requested.set()
        self._wake_event.set()

    def _wait(self, timeout: float) -> None:
        self._wake_event.wait(timeout)
        self._wake_event.clear()

    def _run(self) -> None:
        last_heartbeat = 0.0
        while not self._stop_event.is_set():
            try:
                last_heartbeat = self._run_once(last_heartbeat)
            except Exception as e:  # pylint: disable=broad-except
                # The worker must never die, or the client silently stops reconnecting.
                logging.exception("Unexpected error in connection worker")
                self._drop_connection(f"internal error: {e}")
                self._wait(_RETRY_INTERVAL_S)

    def _run_once(self, last_heartbeat: float) -> float:
        if self._reconnect_requested.is_set():
            self._reconnect_requested.clear()
            if self._conn is not None:
                self._drop_connection("reconnect requested")
        if self._conn is None:
            self._try_connect()
            if self._conn is None:
                self._wait(_RETRY_INTERVAL_S)
            return last_heartbeat
        self._wait(_HEARTBEAT_INTERVAL_S)
        if self._stop_event.is_set() or self._reconnect_requested.is_set():
            return last_heartbeat
        if self._device_refresh_requested.is_set():
            self._device_refresh_requested.clear()
            self._layout_refresh_requested.clear()
            self._refresh_active_device()
        elif self._layout_refresh_requested.is_set():
            self._layout_refresh_requested.clear()
            self._refresh_layout()
        if self._devices_refresh_requested.is_set():
            self._devices_refresh_requested.clear()
            self._refresh_devices()
        if self._cards_refresh_requested.is_set():
            self._cards_refresh_requested.clear()
            self._refresh_cards()
            self._refresh_presets()
        if self._rules_refresh_requested.is_set():
            self._rules_refresh_requested.clear()
            self.refresh_app_rules()
        self._refresh_requested_screensavers()
        if self._detection_refresh_requested.is_set():
            self._detection_refresh_requested.clear()
            self.refresh_detection_status()
        now = time.monotonic()
        if now - last_heartbeat >= _HEARTBEAT_INTERVAL_S:
            self._check_heartbeat()
            return now
        return last_heartbeat

    def _drop_connection(self, reason: str) -> None:
        self._close_connection()
        EventManager.get().push(ServerDisconnectedEvent(reason=reason))

    def _try_connect(self) -> None:
        host: str = ""
        port: int = -1
        try:
            host = client_config.get_server_host()
            port = client_config.get_server_port()
            self._conn = rpyc.utils.factory.connect(
                host,
                port,
                service=_ClientService(self._on_server_event),
            )
        except Exception as e:  # pylint: disable=broad-except
            logging.warning("Could not connect to server at %s:%d — %s", host, port, e)
            return
        logging.info("Connected to server at %s:%d", host, port)
        self.state = ServerState.INITIALIZING
        self._last_devices = None
        self._last_cards = None
        self._last_presets = None
        self._bg_server = BgServingThread(self._conn)
        self.call("client_attached")
        EventManager.get().push(ServerConnectedEvent(host=host, port=port))
        self._refresh_all()
        self.refresh_server_settings()
        self._refresh_updates()
        self._refresh_active_device()
        self.refresh_detection_status()
        # A concurrent stop() may have closed the connection during the initial pull.
        if self.state == ServerState.INITIALIZING:
            self.state = ServerState.HEALTHY

    def refresh_server_settings(self) -> ServerSettingsInfo | None:
        """Fetch the server's settings (and whether this client may change them); None if unavailable."""
        reply = self.call("server_settings")
        try:
            info = parse_server_settings(json.loads(reply)) if isinstance(reply, str) else None
        except ValueError:
            info = None
        self.server_settings = info
        return info

    def set_server_settings(self, values: dict[str, Any]) -> tuple[bool, str | None]:
        """Ask the server to change settings. Returns (accepted, error text)."""
        reply = self.call("server_settings_set", json.dumps(values))
        try:
            result = json.loads(reply) if isinstance(reply, str) else None
        except ValueError:
            result = None
        if not isinstance(result, dict):
            return False, "No answer from the server."
        return bool(result.get("ok")), result.get("error")

    def _refresh_updates(self) -> None:
        report = to_plain(self.call("plugin_updates"))
        if isinstance(report, dict):
            EventManager.get().push(_parse_updates(report))

    def _refresh_all(self) -> None:
        self._refresh_devices()
        self._refresh_cards()
        self._refresh_presets()
        self._check_plugin_failures()

    def _call_json(self, method: str, *args: Any) -> Any:
        """Call an RPC method that returns a JSON string and decode it."""
        if self._conn is None:
            return None
        result = getattr(self._conn.root, method)(*args)
        return json.loads(result)

    def _check_heartbeat(self) -> None:
        try:
            response = self._call_json("heartbeat")
            if not isinstance(response, dict):
                raise ValueError(f"unexpected heartbeat response: {response!r}")
        except Exception as e:  # pylint: disable=broad-except
            logging.warning("Server heartbeat failed: %s", e)
            self._drop_connection(str(e))
            return
        if response.get("service_status") == "healthy":
            self.state = ServerState.HEALTHY
        else:
            logging.warning("Server reported unhealthy status: %r", response)
            self.state = ServerState.UNHEALTHY
        self._refresh_all()

    def _check_plugin_failures(self) -> None:
        if self._conn is None:
            return
        try:
            failures: list[dict[str, Any]] = self._call_json("plugins_load_failures") or []
        except Exception as e:  # pylint: disable=broad-except
            logging.warning("Could not fetch plugin load failures: %s", e)
            return
        for failure in failures:
            plugin_uuid = str(failure.get("plugin_uuid", ""))
            if plugin_uuid in self._reported_plugin_failures:
                continue
            self._reported_plugin_failures.add(plugin_uuid)
            EventManager.get().push(PluginLoadFailedEvent(plugin_uuid=plugin_uuid, error=str(failure.get("error", ""))))

    def _refresh_devices(self) -> None:
        if self._conn is None:
            return
        try:
            raw: list[dict[str, Any]] = self._call_json("devices_list")
            devices = [_parse_hw_info(d) for d in raw]
        except Exception as e:  # pylint: disable=broad-except
            logging.warning("Could not list devices: %s", e)
            return
        if devices == self._last_devices:
            return
        self._last_devices = devices
        EventManager.get().push(DeviceListUpdatedEvent(devices=devices))

    def _refresh_presets(self) -> None:
        if self._conn is None:
            return
        try:
            presets: list[dict[str, Any]] = self._call_json("plugins_presets_list")
        except Exception as e:  # pylint: disable=broad-except
            logging.warning("Could not list plugin presets: %s", e)
            return
        if not isinstance(presets, list) or presets == self._last_presets:
            return
        self._last_presets = presets
        EventManager.get().push(PresetListUpdatedEvent(presets=presets))

    def _refresh_cards(self) -> None:
        if self._conn is None:
            return
        try:
            cards: list[dict[str, Any]] = self._call_json("plugins_cards_list")
        except Exception as e:  # pylint: disable=broad-except
            logging.warning("Could not list plugin cards: %s", e)
            return
        if cards == self._last_cards:
            return
        self._last_cards = cards
        EventManager.get().push(CardListUpdatedEvent(cards=cards))

    def call(self, method: str, *args: Any) -> Any:
        """Call an RPC method on the server and return the result. Returns None on error."""
        if self._conn is None:
            return None
        try:
            function = getattr(self._conn.root, method)
            timeout = _LONG_CALL_TIMEOUTS.get(method)
            if timeout is None:
                return function(*args)
            result = async_(function)(*args)
            result.set_expiry(timeout)
            return result.value
        except Exception as exc:  # pylint: disable=broad-except
            logging.warning("RPC %s failed: %s", method, exc)
            return None

    def _refresh_active_device(self) -> None:
        self._refresh_profiles()
        self._refresh_pages()
        self.refresh_app_rules()
        device_id = _active_device_id()
        if device_id is not None:
            self.refresh_screensaver(device_id)
        self._refresh_key_display_states()

    def _refresh_layout(self) -> None:
        self._refresh_profiles()
        self._refresh_pages()
        self._refresh_key_display_states()

    def _refresh_pages(self) -> None:
        device_id = _active_device_id()
        if self._conn is None or device_id is None:
            return
        try:
            pages = _parse_device_pages(self._call_json("pages_list", device_id))
        except Exception as exc:  # pylint: disable=broad-except
            logging.warning("Could not fetch pages for %s: %s", device_id, exc)
            return
        EventManager.get().push(PagesUpdatedEvent(pages=pages))

    def _refresh_profiles(self) -> None:
        device_id = _active_device_id()
        if self._conn is None or device_id is None:
            return
        try:
            profiles = _parse_device_profiles(self._call_json("profiles_list", device_id))
        except Exception as exc:  # pylint: disable=broad-except
            logging.warning("Could not fetch profiles for %s: %s", device_id, exc)
            return
        EventManager.get().push(ProfilesUpdatedEvent(profiles=profiles))

    def refresh_app_rules(self) -> None:
        """Fetch the active device's app rules and push them as an AppRulesUpdatedEvent."""
        device_id = _active_device_id()
        if self._conn is None or device_id is None:
            return
        try:
            rules = _parse_device_rules(self._call_json("app_rules_list", device_id))
        except Exception as exc:  # pylint: disable=broad-except
            logging.warning("Could not fetch app rules for %s: %s", device_id, exc)
            return
        EventManager.get().push(AppRulesUpdatedEvent(rules=rules))

    def refresh_screensaver(self, device_id: str) -> None:
        """Fetch a device's screensaver settings and push them as a ScreensaverSettingsEvent."""
        status = self.screensaver_get(device_id)
        if status is not None:
            EventManager.get().push(status)

    def screensaver_get(self, device_id: str) -> ScreensaverSettingsEvent | None:
        """Fetch a device's screensaver settings and state; None when unknown or the server is unavailable."""
        try:
            return _parse_screensaver(device_id, self._call_json("screensaver_get", device_id))
        except Exception as exc:  # pylint: disable=broad-except
            logging.warning("Could not fetch screensaver settings for %s: %s", device_id, exc)
            return None

    def screensaver_set(self, device_id: str, settings: ScreensaverSettings) -> tuple[bool, str]:
        """Save a device's screensaver settings; returns (ok, error)."""
        try:
            data = self._call_json("screensaver_set", device_id, json.dumps(asdict(settings)))
            if not isinstance(data, dict):
                return False, _SERVER_UNAVAILABLE
            return bool(data.get("ok")), str(data.get("error", ""))
        except Exception as exc:  # pylint: disable=broad-except
            logging.warning("Could not save screensaver settings for %s: %s", device_id, exc)
            return False, _SERVER_UNAVAILABLE

    def screensaver_activate(self, device_id: str) -> bool:
        """Activate a device's screensaver now; False when refused or the server is unavailable."""
        return self._screensaver_toggle("screensaver_activate", device_id)

    def screensaver_deactivate(self, device_id: str) -> bool:
        """Deactivate a device's screensaver; False when refused or the server is unavailable."""
        return self._screensaver_toggle("screensaver_deactivate", device_id)

    def screensaver_preview(self, device_id: str, settings: ScreensaverSettings) -> tuple[bytes | None, str]:
        """Render a draft screensaver; returns (PNG bytes, error), exactly one of which is set."""
        try:
            data = self._call_json("screensaver_preview", device_id, json.dumps(asdict(settings)))
            if not isinstance(data, dict):
                return None, _SERVER_UNAVAILABLE
            if data.get("png"):
                return base64.b64decode(str(data["png"]), validate=True), ""
            return None, str(data.get("error") or "no preview")
        except (binascii.Error, ValueError) as exc:
            return None, f"bad preview data: {exc}"
        except Exception as exc:  # pylint: disable=broad-except
            logging.warning("Could not preview screensaver for %s: %s", device_id, exc)
            return None, _SERVER_UNAVAILABLE

    def _screensaver_toggle(self, method: str, device_id: str) -> bool:
        try:
            return self._call_json(method, device_id) is True
        except Exception as exc:  # pylint: disable=broad-except
            logging.warning("RPC %s failed for %s: %s", method, device_id, exc)
            return False

    def _refresh_requested_screensavers(self) -> None:
        with self._screensaver_refresh_lock:
            devices, self._screensaver_refresh_devices = self._screensaver_refresh_devices, set()
        for device_id in sorted(devices):
            self.refresh_screensaver(device_id)

    def refresh_detection_status(self) -> None:
        """Fetch the window detection status and push it as a DetectionStatusEvent."""
        if self._conn is None:
            return
        try:
            data = self._call_json("window_detection_status")
            if not isinstance(data, dict):
                raise ValueError(f"unexpected detection status: {data!r}")
            event = DetectionStatusEvent(
                state=str(data["state"]),
                reason=str(data.get("reason") or ""),
                backend=str(data.get("backend") or ""),
                supports_pick=bool(data.get("supports_pick", False)),
                window=_parse_window(data.get("window")),
            )
        except Exception as exc:  # pylint: disable=broad-except
            logging.warning("Could not fetch window detection status: %s", exc)
            return
        EventManager.get().push(event)

    def app_rule_add(self, device_id: str, rule: AppRule) -> AppRule | None:
        """Add a rule; returns the stored rule, or None when the server rejected it or the call failed."""
        try:
            data = self._call_json("app_rule_add", device_id, asdict(rule))
            return _parse_rule(data) if data is not None else None
        except Exception as exc:  # pylint: disable=broad-except
            logging.warning("Could not add app rule: %s", exc)
            return None

    def app_rule_update(self, device_id: str, rule: AppRule) -> bool:
        """Replace a rule; False when the server rejected it or the call failed."""
        return self._call_bool("app_rule_update", device_id, asdict(rule))

    def app_rule_delete(self, device_id: str, rule_id: str) -> bool:
        """Delete a rule; False when it is unknown or the call failed."""
        return self._call_bool("app_rule_delete", device_id, rule_id)

    def app_rule_move(self, device_id: str, rule_id: str, position: int) -> bool:
        """Move a rule to a priority position; False when it is unknown or the call failed."""
        return self._call_bool("app_rule_move", device_id, rule_id, position)

    def app_default_rule_set(self, device_id: str, default_rule: DefaultRule) -> bool:
        """Set the default rule; False when the server rejected it or the call failed."""
        return self._call_bool(
            "app_default_rule_set", device_id, default_rule.enabled, default_rule.profile_id, default_rule.page_id
        )

    def window_pick(self) -> bool:
        """Start a window pick; the result arrives as a WindowPickedEvent. False when it could not start."""
        return self._call_bool("window_pick")

    def _call_bool(self, method: str, *args: Any) -> bool:
        try:
            return self._call_json(method, *args) is True
        except Exception as exc:  # pylint: disable=broad-except
            logging.warning("RPC %s failed: %s", method, exc)
            return False

    def _refresh_key_display_states(self) -> None:
        device_id = _active_device_id()
        if self._conn is None or device_id is None:
            return
        try:
            states: list[dict[str, Any]] = self._call_json("key_get_states", device_id)
        except Exception as exc:  # pylint: disable=broad-except
            logging.warning("Could not fetch key display states: %s", exc)
            return
        EventManager.get().push(KeyDisplayStatesUpdatedEvent(device_id=device_id, states=states))

    def _on_server_event(self, event_type: str, payload: str) -> None:
        """Dispatch a push event received from the server to the local EventManager."""
        try:
            data: dict[str, Any] = json.loads(payload)
        except (ValueError, TypeError) as exc:
            logging.warning("Malformed server event payload for %s: %s", event_type, exc)
            return

        if event_type == "key_event":
            EventManager.get().push(
                KeyEvent(
                    device_id=str(data["device_id"]),
                    key_index=int(data["key_index"]),
                    pressed=bool(data["pressed"]),
                )
            )
        elif event_type == "key_state_changed":
            device_id = str(data["device_id"])
            states: list[dict[str, Any]] = [
                {
                    "key_index": data["key_index"],
                    "image_b64": data.get("image_b64"),
                    "label": data.get("label", ""),
                    "animation_id": data.get("animation_id"),
                }
            ]
            EventManager.get().push(KeyDisplayStatesUpdatedEvent(device_id=device_id, states=states))
        elif event_type == "plugin_failed":
            EventManager.get().push(
                PluginLoadFailedEvent(plugin_uuid=str(data.get("plugin_id", "")), error=str(data.get("error", "")))
            )
        elif event_type == "action_failed":
            EventManager.get().push(
                ActionFailedEvent(action_id=str(data.get("action_id", "")), error=str(data.get("error", "")))
            )
        elif event_type == "open_url":
            EventManager.get().push(
                OpenUrlRequestedEvent(plugin_id=str(data.get("plugin_id", "")), url=str(data.get("url", "")))
            )
        elif event_type in ("pages_changed", "profiles_changed", "devices_changed"):
            self._on_layout_changed(event_type, data)
        elif event_type == "plugin_install_progress":
            EventManager.get().push(
                PluginInstallProgressEvent(job_id=str(data.get("job_id", "")), message=str(data.get("message", "")))
            )
        elif event_type == "plugin_install_finished":
            EventManager.get().push(_parse_install_finished(data))
        elif event_type == "plugin_updates_available":
            EventManager.get().push(_parse_updates(data))
        elif event_type == "repositories_check_finished":
            EventManager.get().push(RepositoriesCheckFinishedEvent(repositories=list(data.get("repositories") or [])))
        elif event_type in _WINDOW_EVENT_TYPES:
            self._on_window_event(event_type, data)
        elif event_type in _SCREENSAVER_EVENT_TYPES:
            self._on_screensaver_event(event_type, data)
        elif event_type == "plugins_changed":
            self.request_cards_refresh()
            EventManager.get().push(PluginsChangedEvent())
        else:
            logging.debug("Unknown server event type: %s", event_type)

    def _on_screensaver_event(self, event_type: str, data: dict[str, Any]) -> None:
        device_id = str(data.get("device_id", ""))
        if event_type == "screensaver_changed":
            EventManager.get().push(ScreensaverStateEvent(device_id=device_id, active=bool(data.get("active"))))
        # screensaver_changed carries no error text, so a state change also refetches last_error on the worker.
        with self._screensaver_refresh_lock:
            self._screensaver_refresh_devices.add(device_id)
        self._wake_event.set()

    def _on_window_event(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type == "app_rules_changed":
            if data.get("device_id") == _active_device_id():
                self._rules_refresh_requested.set()
                self._wake_event.set()
        elif event_type == "window_detection_changed":
            self._detection_refresh_requested.set()
            self._wake_event.set()
        elif event_type == "window_focused":
            window = _parse_window(data.get("window"))
            if window is not None:
                EventManager.get().push(WindowFocusedEvent(window=window))
        elif event_type == "window_picked":
            EventManager.get().push(WindowPickedEvent(window=_parse_window(data.get("window"))))

    def _on_layout_changed(self, event_type: str, data: dict[str, Any]) -> None:
        active = data.get("device_id") == _active_device_id()
        if event_type == "devices_changed":
            self.request_devices_refresh()  # a device went away or came back, whichever device it was
            if active:
                self.request_device_refresh()
        elif active:
            self.request_layout_refresh()

    def _close_connection(self) -> None:
        self.state = ServerState.DISCONNECTED
        self.server_settings = None
        if self._bg_server is not None:
            try:
                self._bg_server.stop()
            except Exception:  # pylint: disable=broad-except
                pass
            self._bg_server = None
        if self._conn is None:
            return
        try:
            self._conn.close()
        except Exception:  # pylint: disable=broad-except
            pass
        self._conn = None


def _active_device_id() -> str | None:
    from decker.client.context import (  # pylint: disable=import-outside-toplevel  # avoids an import cycle
        AppContext,
    )

    return AppContext.get().active_device_id


def _parse_device_pages(data: Any) -> DevicePages:
    """Deserialise a pages_list response; raises ValueError if it is malformed."""
    if not isinstance(data, dict):
        raise ValueError(f"unexpected pages response: {data!r}")
    pages = [PageInfo(page_id=str(p["page_id"]), name=str(p["name"])) for p in data.get("pages") or []]
    if not pages:
        raise ValueError("device has no pages")
    return DevicePages(
        device_id=str(data["device_id"]),
        pages=pages,
        default_page_id=str(data["default_page_id"]),
        active_page_id=str(data["active_page_id"]),
        profile_id=int(data.get("profile_id", 0)),
    )


def _parse_device_profiles(data: Any) -> DeviceProfiles:
    """Deserialise a profiles_list response; raises ValueError if it is malformed."""
    if not isinstance(data, dict):
        raise ValueError(f"unexpected profiles response: {data!r}")
    profiles = [
        ProfileInfo(
            profile_id=_int_id(p["profile_id"]),
            name=str(p["name"]),
            icon=str(p.get("icon", "")),
            icon_color=str(p.get("icon_color", "")),
        )
        for p in data.get("profiles") or []
    ]
    if not profiles:
        raise ValueError("device has no profiles")
    return DeviceProfiles(
        device_id=str(data["device_id"]),
        profiles=profiles,
        active_profile_id=_int_id(data["active_profile_id"]),
    )


def _parse_device_rules(data: Any) -> DeviceRules:
    """Deserialise an app_rules_list response; raises ValueError if it is malformed."""
    if not isinstance(data, dict):
        raise ValueError(f"unexpected app rules response: {data!r}")
    try:
        default = data["default_rule"]
        return DeviceRules(
            device_id=str(data["device_id"]),
            rules=[_parse_rule(r) for r in data["rules"]],
            default_rule=DefaultRule(
                enabled=bool(default["enabled"]),
                profile_id=_int_id(default["profile_id"]),
                page_id=str(default.get("page_id", "")),
            ),
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"malformed app rules response: {exc!r}") from exc


def _parse_rule(data: Any) -> AppRule:
    return AppRule(
        rule_id=str(data["rule_id"]),
        name=str(data["name"]),
        enabled=bool(data["enabled"]),
        conditions=[MatchCondition(str(c["field"]), str(c["op"]), str(c["value"])) for c in data["conditions"]],
        profile_id=_int_id(data["profile_id"]),
        page_id=str(data.get("page_id", "")),
    )


def _parse_window(data: Any) -> WindowInfo | None:
    """Deserialise a window dict; None for null or a non-dict; missing fields become empty."""
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    return WindowInfo(
        window_class=str(data.get("window_class") or ""),
        window_type=str(data.get("window_type") or ""),
        title=str(data.get("title") or ""),
        executable=str(data.get("executable") or ""),
        desktop_file=str(data.get("desktop_file") or ""),
        pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else 0,
    )


def _int_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"profile id {value!r} is not an integer")
    return value


def _parse_hw_info(data: dict[str, Any]) -> DeviceHardwareInfo:
    """Deserialise a raw device dict (from the JSON transport) into a DeviceHardwareInfo."""
    keys = [
        KeyInfo(
            row=k.get("row", 0),
            col=k.get("col", 0),
            pixel_width=k.get("pixel_width", 0),
            pixel_height=k.get("pixel_height", 0),
            type=k.get("type", "key"),
        )
        for k in data.get("keys") or []
    ]
    return DeviceHardwareInfo(
        id=data.get("id") or None,
        type=str(data.get("type", "")),
        vendor_id=int(data.get("vendor_id") or 0),
        product_id=int(data.get("product_id") or 0),
        num_keys=int(data.get("num_keys") or 0),
        keys=keys,
        serial=data.get("serial") or None,
        path=str(data.get("path") or ""),
        firmware=data.get("firmware") or None,
    )


def to_plain(value: Any) -> Any:
    """Copy an RPC result (possibly remote list/dict references) into plain local lists and dicts."""
    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(item) for item in value]
    return value


def _parse_updates(data: dict[str, Any]) -> PluginUpdatesAvailableEvent:
    return PluginUpdatesAvailableEvent(updates=list(data.get("updates") or []), missing=list(data.get("missing") or []))


def _parse_install_finished(data: dict[str, Any]) -> PluginInstallFinishedEvent:
    return PluginInstallFinishedEvent(
        job_id=str(data.get("job_id", "")),
        ok=bool(data.get("ok", False)),
        plugin_id=str(data.get("plugin_id") or ""),
        version=str(data.get("version") or ""),
        previous_version=str(data.get("previous_version") or ""),
        message=str(data.get("message") or ""),
    )


def _parse_screensaver(device_id: str, data: Any) -> ScreensaverSettingsEvent | None:
    """Parse a screensaver_get response; unknown keys are ignored so newer servers stay compatible."""
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError(f"unexpected screensaver response: {data!r}")
    known = {f.name for f in fields(ScreensaverSettings)}
    settings = ScreensaverSettings(**{k: v for k, v in data.items() if k in known})
    return ScreensaverSettingsEvent(
        device_id=device_id,
        settings=settings,
        active=bool(data.get("active", False)),
        last_error=str(data.get("last_error", "")),
    )
