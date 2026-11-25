"""Tests for decker.client.connection_manager."""

import json
import time
from typing import Any
from unittest.mock import MagicMock, patch

from assertpy import assert_that

from decker.client.connection_manager import (
    ConnectionManager,
    ServerState,
    _ClientService,
    _parse_device_rules,
    _parse_window,
)
from decker.client.context import AppContext
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
    ServerConnectedEvent,
    ServerDisconnectedEvent,
    WindowFocusedEvent,
    WindowPickedEvent,
)
from decker.client.events.event_manager import EventManager
from decker.common.model import (
    AppRule,
    DefaultRule,
    DeviceHardwareInfo,
    DevicePages,
    DeviceProfiles,
    DeviceRules,
    MatchCondition,
    PageInfo,
    ProfileInfo,
    WindowInfo,
)
from decker.common.utils import api_response
from decker.server.service import DeckerServiceStatus, DeckerStatusEnum


@api_response
def _server_status(status: DeckerStatusEnum) -> DeckerServiceStatus:
    return DeckerServiceStatus(service_status=status, last_heartbeat=0.0)


def _heartbeat_payload(status: DeckerStatusEnum) -> str:
    """Build a heartbeat reply with the server's own serializer so the wire format can't drift."""
    return str(_server_status(status))


def _healthy_conn() -> MagicMock:
    conn = MagicMock()
    conn.root.heartbeat.return_value = _heartbeat_payload(DeckerStatusEnum.HEALTHY)
    conn.root.devices_list.return_value = json.dumps([])
    conn.root.plugins_load_failures.return_value = json.dumps([])
    conn.root.plugins_cards_list.return_value = json.dumps([])
    return conn


class TestConnectionManager:
    def setup_method(self):
        EventManager.reset()
        self._config_patches = [
            patch("decker.client.config.get_server_host", return_value="localhost"),
            patch("decker.client.config.get_server_port", return_value=18861),
        ]
        for p in self._config_patches:
            p.start()
        self.manager = ConnectionManager()

    def teardown_method(self):
        self.manager.stop()
        for p in self._config_patches:
            p.stop()

    @patch("decker.client.connection_manager.rpyc.utils.factory.connect")
    def test_try_connect_success_marks_connected_and_pushes_events(self, mock_connect: MagicMock):
        mock_conn = MagicMock()
        mock_conn.root.devices_list.return_value = json.dumps([{"id": "abc123", "serial": "X"}])
        mock_conn.root.plugins_load_failures.return_value = json.dumps([])
        mock_conn.root.plugins_cards_list.return_value = json.dumps([])
        mock_connect.return_value = mock_conn

        self.manager._try_connect()  # pylint: disable=protected-access

        assert_that(self.manager.state).is_equal_to(ServerState.HEALTHY)
        events = EventManager.get().drain()
        assert_that(events).is_length(3)
        assert_that(events[0]).is_instance_of(ServerConnectedEvent)
        assert_that(events[1]).is_instance_of(DeviceListUpdatedEvent)
        assert isinstance(events[1], DeviceListUpdatedEvent)
        assert_that(events[1].devices).is_length(1)
        assert_that(events[1].devices[0].id).is_equal_to("abc123")
        assert_that(events[1].devices[0].serial).is_equal_to("X")

    @patch("decker.client.connection_manager.rpyc.utils.factory.connect")
    def test_try_connect_failure_leaves_disconnected_and_pushes_nothing(self, mock_connect: MagicMock):
        mock_connect.side_effect = ConnectionRefusedError("refused")

        self.manager._try_connect()  # pylint: disable=protected-access

        assert_that(self.manager.state).is_equal_to(ServerState.DISCONNECTED)
        assert_that(EventManager.get().drain()).is_empty()

    def test_check_heartbeat_failure_pushes_disconnected_event(self):
        mock_conn = MagicMock()
        mock_conn.root.heartbeat.side_effect = ConnectionResetError("lost")
        self.manager._conn = mock_conn  # pylint: disable=protected-access
        self.manager.state = ServerState.HEALTHY

        self.manager._check_heartbeat()  # pylint: disable=protected-access

        assert_that(self.manager.state).is_equal_to(ServerState.DISCONNECTED)
        events = EventManager.get().drain()
        assert_that(events).is_length(1)
        assert_that(events[0]).is_instance_of(ServerDisconnectedEvent)

    def test_check_heartbeat_success_refreshes_devices(self):
        mock_conn = MagicMock()
        mock_conn.root.heartbeat.return_value = _heartbeat_payload(DeckerStatusEnum.HEALTHY)
        mock_conn.root.devices_list.return_value = json.dumps([{"id": "abc123"}])
        mock_conn.root.plugins_load_failures.return_value = json.dumps([])
        mock_conn.root.plugins_cards_list.return_value = json.dumps([])
        self.manager._conn = mock_conn  # pylint: disable=protected-access
        self.manager.state = ServerState.HEALTHY

        self.manager._check_heartbeat()  # pylint: disable=protected-access

        events = EventManager.get().drain()
        assert_that(events).is_length(2)
        assert_that(events[0]).is_instance_of(DeviceListUpdatedEvent)

    def test_check_heartbeat_non_healthy_status_marks_unhealthy_but_stays_connected(self):
        mock_conn = MagicMock()
        mock_conn.root.heartbeat.return_value = _heartbeat_payload(DeckerStatusEnum.UNHEALTHY)
        mock_conn.root.devices_list.return_value = json.dumps([])
        mock_conn.root.plugins_load_failures.return_value = json.dumps([])
        mock_conn.root.plugins_cards_list.return_value = json.dumps([])
        self.manager._conn = mock_conn  # pylint: disable=protected-access
        self.manager.state = ServerState.HEALTHY

        self.manager._check_heartbeat()  # pylint: disable=protected-access

        assert_that(self.manager.state).is_equal_to(ServerState.UNHEALTHY)
        assert_that(self.manager.is_connected).is_true()
        assert_that(self.manager.is_ready).is_true()

    def test_initializing_state_is_connected_but_not_ready(self):
        self.manager.state = ServerState.INITIALIZING

        assert_that(self.manager.is_connected).is_true()
        assert_that(self.manager.is_ready).is_false()

    def test_disconnected_state_is_neither_connected_nor_ready(self):
        assert_that(self.manager.is_connected).is_false()
        assert_that(self.manager.is_ready).is_false()

    @patch("decker.client.connection_manager.rpyc.utils.factory.connect")
    def test_try_connect_reads_host_and_port_from_config(self, mock_connect: MagicMock):
        mock_connect.side_effect = ConnectionRefusedError("refused")
        with (
            patch("decker.client.config.get_server_host", return_value="otherhost"),
            patch("decker.client.config.get_server_port", return_value=9999),
        ):
            self.manager._try_connect()  # pylint: disable=protected-access

        assert_that(mock_connect.call_args.args[:2]).is_equal_to(("otherhost", 9999))

    def test_check_heartbeat_healthy_server_reply_marks_healthy(self):
        self.manager._conn = _healthy_conn()  # pylint: disable=protected-access
        self.manager.state = ServerState.UNHEALTHY

        self.manager._check_heartbeat()  # pylint: disable=protected-access

        assert_that(self.manager.state).is_equal_to(ServerState.HEALTHY)

    def test_check_heartbeat_non_dict_reply_disconnects_without_raising(self):
        conn = _healthy_conn()
        conn.root.heartbeat.return_value = json.dumps(None)
        self.manager._conn = conn  # pylint: disable=protected-access
        self.manager.state = ServerState.HEALTHY

        self.manager._check_heartbeat()  # pylint: disable=protected-access

        assert_that(self.manager.state).is_equal_to(ServerState.DISCONNECTED)
        assert_that(EventManager.get().drain()[-1]).is_instance_of(ServerDisconnectedEvent)

    def test_refresh_devices_malformed_payload_does_not_raise(self):
        conn = _healthy_conn()
        conn.root.devices_list.return_value = json.dumps([{"vendor_id": "abc"}])
        self.manager._conn = conn  # pylint: disable=protected-access

        self.manager._refresh_devices()  # pylint: disable=protected-access

        assert_that(EventManager.get().drain()).is_empty()

    def test_refresh_does_not_push_unchanged_device_and_card_lists(self):
        conn = _healthy_conn()
        conn.root.devices_list.return_value = json.dumps([{"id": "abc123"}])
        self.manager._conn = conn  # pylint: disable=protected-access

        self.manager._refresh_all()  # pylint: disable=protected-access
        first = EventManager.get().drain()
        self.manager._refresh_all()  # pylint: disable=protected-access
        second = EventManager.get().drain()

        assert_that([type(e) for e in first]).contains(DeviceListUpdatedEvent, CardListUpdatedEvent)
        assert_that(second).is_empty()

    def test_run_once_survives_unexpected_error(self):
        self.manager._run_once = MagicMock(side_effect=[RuntimeError("boom"), 0.0])  # type: ignore[method-assign]
        self.manager._wait = MagicMock(side_effect=lambda _t: self.manager._stop_event.set())  # type: ignore[method-assign]

        self.manager._run()  # pylint: disable=protected-access

        assert_that(self.manager.state).is_equal_to(ServerState.DISCONNECTED)

    def test_request_reconnect_drops_connection_and_pushes_disconnect(self):
        conn = _healthy_conn()
        self.manager._conn = conn  # pylint: disable=protected-access
        self.manager.state = ServerState.HEALTHY
        self.manager.request_reconnect()
        self.manager._try_connect = MagicMock()  # type: ignore[method-assign]
        self.manager._wait = MagicMock()  # type: ignore[method-assign]

        self.manager._run_once(0.0)  # pylint: disable=protected-access

        conn.close.assert_called_once()
        self.manager._try_connect.assert_called_once()  # pylint: disable=protected-access
        assert_that(EventManager.get().drain()[0]).is_instance_of(ServerDisconnectedEvent)

    @patch("decker.client.connection_manager.rpyc.utils.factory.connect")
    def test_try_connect_does_not_overwrite_state_closed_during_initial_pull(self, mock_connect: MagicMock):
        conn = _healthy_conn()

        def _close_mid_pull() -> str:
            self.manager._close_connection()  # pylint: disable=protected-access
            return json.dumps([])

        conn.root.devices_list.side_effect = _close_mid_pull
        mock_connect.return_value = conn

        self.manager._try_connect()  # pylint: disable=protected-access

        assert_that(self.manager.state).is_equal_to(ServerState.DISCONNECTED)

    def test_stop_closes_connection_and_clears_connected(self):
        mock_conn = MagicMock()
        self.manager._conn = mock_conn  # pylint: disable=protected-access
        self.manager.state = ServerState.HEALTHY

        self.manager.stop()

        mock_conn.close.assert_called_once()
        assert_that(self.manager.state).is_equal_to(ServerState.DISCONNECTED)

    @patch("decker.client.connection_manager.rpyc.utils.factory.connect")
    def test_try_connect_pushes_plugin_load_failed_event(self, mock_connect: MagicMock):
        mock_conn = MagicMock()
        mock_conn.root.devices_list.return_value = json.dumps([])
        mock_conn.root.plugins_load_failures.return_value = json.dumps(
            [{"plugin_uuid": "com.decker.broken", "error": "boom"}]
        )
        mock_conn.root.plugins_cards_list.return_value = json.dumps([])
        mock_connect.return_value = mock_conn

        self.manager._try_connect()  # pylint: disable=protected-access

        events = EventManager.get().drain()
        failure_events = [e for e in events if isinstance(e, PluginLoadFailedEvent)]
        assert_that(failure_events).is_length(1)
        assert_that(failure_events[0].plugin_uuid).is_equal_to("com.decker.broken")
        assert_that(failure_events[0].error).is_equal_to("boom")

    def test_plugin_failure_is_only_reported_once(self):
        mock_conn = MagicMock()
        mock_conn.root.plugins_load_failures.return_value = json.dumps(
            [{"plugin_uuid": "com.decker.broken", "error": "boom"}]
        )
        self.manager._conn = mock_conn  # pylint: disable=protected-access

        self.manager._check_plugin_failures()  # pylint: disable=protected-access
        self.manager._check_plugin_failures()  # pylint: disable=protected-access

        events = [e for e in EventManager.get().drain() if isinstance(e, PluginLoadFailedEvent)]
        assert_that(events).is_length(1)

    @patch("decker.client.connection_manager.rpyc.utils.factory.connect")
    def test_try_connect_pushes_card_list_updated_event(self, mock_connect: MagicMock):
        mock_conn = MagicMock()
        mock_conn.root.devices_list.return_value = json.dumps([])
        mock_conn.root.plugins_load_failures.return_value = json.dumps([])
        mock_conn.root.plugins_cards_list.return_value = json.dumps(
            [{"plugin_uuid": "p", "plugin_name": "P", "card_uuid": "p.a", "card_name": "A"}]
        )
        mock_connect.return_value = mock_conn

        self.manager._try_connect()  # pylint: disable=protected-access

        events = [e for e in EventManager.get().drain() if isinstance(e, CardListUpdatedEvent)]
        assert_that(events).is_length(1)
        assert_that(events[0].cards).is_equal_to(
            [{"plugin_uuid": "p", "plugin_name": "P", "card_uuid": "p.a", "card_name": "A"}]
        )

    def test_check_heartbeat_success_refreshes_cards(self):
        mock_conn = MagicMock()
        mock_conn.root.heartbeat.return_value = _heartbeat_payload(DeckerStatusEnum.HEALTHY)
        mock_conn.root.devices_list.return_value = json.dumps([])
        mock_conn.root.plugins_load_failures.return_value = json.dumps([])
        mock_conn.root.plugins_cards_list.return_value = json.dumps(
            [{"plugin_uuid": "p", "plugin_name": "P", "card_uuid": "p.a", "card_name": "A"}]
        )
        self.manager._conn = mock_conn  # pylint: disable=protected-access
        self.manager.state = ServerState.HEALTHY

        self.manager._check_heartbeat()  # pylint: disable=protected-access

        events = [e for e in EventManager.get().drain() if isinstance(e, CardListUpdatedEvent)]
        assert_that(events).is_length(1)

    def test_presets_refresh_pushes_event_once_per_change(self):
        presets = [{"preset_uuid": "p.a", "preset_name": "Do"}]
        mock_conn = MagicMock()
        mock_conn.root.plugins_presets_list.return_value = json.dumps(presets)
        self.manager._conn = mock_conn  # pylint: disable=protected-access

        self.manager._refresh_presets()  # pylint: disable=protected-access
        self.manager._refresh_presets()  # pylint: disable=protected-access
        changed = [*presets, {"preset_uuid": "p.b", "preset_name": "Other"}]
        mock_conn.root.plugins_presets_list.return_value = json.dumps(changed)
        self.manager._refresh_presets()  # pylint: disable=protected-access

        events = [e for e in EventManager.get().drain() if isinstance(e, PresetListUpdatedEvent)]
        assert_that([e.presets for e in events]).is_equal_to([presets, changed])

    def test_refresh_cards_failure_does_not_raise(self):
        mock_conn = MagicMock()
        mock_conn.root.plugins_cards_list.side_effect = RuntimeError("boom")
        self.manager._conn = mock_conn  # pylint: disable=protected-access

        self.manager._refresh_cards()  # pylint: disable=protected-access

        assert_that(EventManager.get().drain()).is_empty()

    def test_call_delegates_to_rpc_root(self):
        mock_conn = MagicMock()
        mock_conn.root.some_method.return_value = "result"
        self.manager._conn = mock_conn  # pylint: disable=protected-access
        result = self.manager.call("some_method", "arg1", 42)
        mock_conn.root.some_method.assert_called_once_with("arg1", 42)
        assert_that(result).is_equal_to("result")

    def test_upload_call_waits_up_to_120_seconds(self, monkeypatch):
        seen: dict = {}

        class FakeResult:
            value = "job-1"

            def set_expiry(self, timeout):
                seen["timeout"] = timeout

        monkeypatch.setattr(
            "decker.client.connection_manager.async_", lambda fn: lambda *a: (seen.update(args=a), FakeResult())[1]
        )
        self.manager._conn = MagicMock()  # pylint: disable=protected-access

        result = self.manager.call("plugin_install", "x.deckerplugin", b"data")

        assert_that(result).is_equal_to("job-1")
        assert_that(seen["timeout"]).is_equal_to(120.0)
        assert_that(seen["args"]).is_equal_to(("x.deckerplugin", b"data"))

    def test_call_returns_none_when_not_connected(self):
        result = self.manager.call("some_method")
        assert_that(result).is_none()

    def test_call_returns_none_and_logs_on_rpc_error(self):
        mock_conn = MagicMock()
        mock_conn.root.some_method.side_effect = RuntimeError("rpc error")
        self.manager._conn = mock_conn  # pylint: disable=protected-access
        result = self.manager.call("some_method")
        assert_that(result).is_none()

    def test_refresh_key_display_states_pushes_event(self):
        mock_conn = MagicMock()
        mock_conn.root.key_get_states.return_value = json.dumps([{"key_index": 0, "label": "5", "settings": {}}])
        self.manager._conn = mock_conn  # pylint: disable=protected-access
        AppContext.reset()
        AppContext.get().active_device = DeviceHardwareInfo(
            id="dev1", type="Stream Deck", vendor_id=0, product_id=0, num_keys=15
        )
        self.manager._refresh_key_display_states()  # pylint: disable=protected-access
        events = [e for e in EventManager.get().drain() if isinstance(e, KeyDisplayStatesUpdatedEvent)]
        assert_that(events).is_length(1)
        assert_that(events[0].device_id).is_equal_to("dev1")
        AppContext.reset()

    def test_refresh_key_display_states_no_op_without_device(self):
        mock_conn = MagicMock()
        self.manager._conn = mock_conn  # pylint: disable=protected-access
        AppContext.reset()
        self.manager._refresh_key_display_states()  # pylint: disable=protected-access
        mock_conn.root.key_get_states.assert_not_called()
        AppContext.reset()

    @patch("decker.client.connection_manager.rpyc.utils.factory.connect")
    def test_start_spawns_a_worker_thread_and_stop_joins_it(self, mock_connect: MagicMock):
        mock_connect.side_effect = ConnectionRefusedError("refused")
        with (
            patch("decker.client.connection_manager._RETRY_INTERVAL_S", 0.01),
            patch("decker.client.connection_manager._HEARTBEAT_INTERVAL_S", 0.01),
        ):
            self.manager.start()
            time.sleep(0.05)
            thread = self.manager._thread  # pylint: disable=protected-access
            assert thread is not None
            assert_that(thread.is_alive()).is_true()

            self.manager.stop()

            assert_that(self.manager._thread).is_none()  # pylint: disable=protected-access


class TestClientService:
    def setup_method(self):
        EventManager.reset()

    def test_exposed_on_server_event_invokes_callback(self):
        received = []
        service = _ClientService(on_event=lambda t, p: received.append((t, p)))
        service.exposed_on_server_event("key_event", '{"key": 1}')
        assert_that(received).is_length(1)
        assert_that(received[0][0]).is_equal_to("key_event")

    def test_exposed_on_server_event_swallows_callback_errors(self):
        def bad_callback(t: str, p: str) -> None:
            raise RuntimeError("boom")

        service = _ClientService(on_event=bad_callback)
        service.exposed_on_server_event("key_event", "{}")  # should not raise


class TestOnServerEvent:
    def setup_method(self):
        EventManager.reset()
        self.manager = ConnectionManager()

    def test_key_event_dispatches_key_event(self):
        payload = json.dumps({"device_id": "abc", "key_index": 3, "pressed": True})
        self.manager._on_server_event("key_event", payload)  # pylint: disable=protected-access
        events = [e for e in EventManager.get().drain() if isinstance(e, KeyEvent)]
        assert_that(events).is_length(1)
        assert_that(events[0].device_id).is_equal_to("abc")
        assert_that(events[0].key_index).is_equal_to(3)
        assert_that(events[0].pressed).is_true()

    def test_key_state_changed_dispatches_display_states_event(self):
        payload = json.dumps({"device_id": "abc", "key_index": 2, "image_b64": None, "label": "Hi"})
        self.manager._on_server_event("key_state_changed", payload)  # pylint: disable=protected-access
        events = [e for e in EventManager.get().drain() if isinstance(e, KeyDisplayStatesUpdatedEvent)]
        assert_that(events).is_length(1)
        assert_that(events[0].device_id).is_equal_to("abc")
        assert_that(events[0].states[0]["key_index"]).is_equal_to(2)
        assert_that(events[0].states[0]["label"]).is_equal_to("Hi")
        assert_that(events[0].states[0]["animation_id"]).is_none()

    def test_key_state_changed_passes_the_animation_id_through(self):
        payload = json.dumps({"device_id": "abc", "key_index": 2, "image_b64": "x", "label": "", "animation_id": "a1"})
        self.manager._on_server_event("key_state_changed", payload)  # pylint: disable=protected-access
        events = [e for e in EventManager.get().drain() if isinstance(e, KeyDisplayStatesUpdatedEvent)]
        assert_that(events[0].states[0]["animation_id"]).is_equal_to("a1")

    def test_plugin_failed_dispatches_plugin_load_failed_event(self):
        payload = json.dumps({"plugin_id": "com.decker.x", "error": "crashed"})
        self.manager._on_server_event("plugin_failed", payload)  # pylint: disable=protected-access
        events = [e for e in EventManager.get().drain() if isinstance(e, PluginLoadFailedEvent)]
        assert_that(events).is_length(1)
        assert_that(events[0].plugin_uuid).is_equal_to("com.decker.x")
        assert_that(events[0].error).is_equal_to("crashed")

    def test_action_failed_is_forwarded(self):
        payload = json.dumps({"action_id": "com.t.http", "error": "HTTP 500"})
        self.manager._on_server_event("action_failed", payload)  # pylint: disable=protected-access
        events = [e for e in EventManager.get().drain() if isinstance(e, ActionFailedEvent)]

        assert_that([(e.action_id, e.error) for e in events]).is_equal_to([("com.t.http", "HTTP 500")])

    def test_open_url_event_parsed(self):
        payload = json.dumps({"plugin_id": "com.decker.x", "url": "https://example.com"})
        self.manager._on_server_event("open_url", payload)  # pylint: disable=protected-access
        events = [e for e in EventManager.get().drain() if isinstance(e, OpenUrlRequestedEvent)]
        assert_that(events).is_length(1)
        assert_that(events[0].plugin_id).is_equal_to("com.decker.x")
        assert_that(events[0].url).is_equal_to("https://example.com")

    def test_install_events_become_client_events(self):
        self.manager._on_server_event(  # pylint: disable=protected-access
            "plugin_install_progress", json.dumps({"job_id": "j", "message": "working"})
        )
        self.manager._on_server_event(  # pylint: disable=protected-access
            "plugin_install_finished",
            json.dumps(
                {"job_id": "j", "ok": True, "plugin_id": "a.b", "version": "2", "previous_version": None, "message": ""}
            ),
        )
        events = EventManager.get().drain()
        assert_that([type(e) for e in events]).is_equal_to([PluginInstallProgressEvent, PluginInstallFinishedEvent])
        progress, finished = events
        assert isinstance(progress, PluginInstallProgressEvent) and isinstance(finished, PluginInstallFinishedEvent)
        assert_that(progress.message).is_equal_to("working")
        assert_that(finished.ok).is_true()
        assert_that(finished.previous_version).is_equal_to("")

    def test_plugins_changed_refreshes_card_list(self):
        self.manager._on_server_event("plugins_changed", "{}")  # pylint: disable=protected-access
        assert_that([type(e) for e in EventManager.get().drain()]).is_equal_to([PluginsChangedEvent])
        assert_that(self.manager._cards_refresh_requested.is_set()).is_true()  # pylint: disable=protected-access

        self.manager._conn = MagicMock()  # pylint: disable=protected-access
        self.manager._conn.root.plugins_cards_list.return_value = json.dumps(
            [{"id": "x"}]
        )  # pylint: disable=protected-access
        self.manager._run_once(time.monotonic())  # pylint: disable=protected-access
        events = [e for e in EventManager.get().drain() if isinstance(e, CardListUpdatedEvent)]
        assert_that(events).is_length(1)

    def test_unknown_event_type_is_silently_ignored(self):
        self.manager._on_server_event("some_future_event", "{}")  # pylint: disable=protected-access
        assert_that(EventManager.get().drain()).is_empty()

    def test_malformed_payload_does_not_raise(self):
        self.manager._on_server_event("key_event", "NOT JSON")  # pylint: disable=protected-access


_DEVICE_PAGES = DevicePages(
    device_id="dev1",
    pages=[PageInfo("p1", "Page 1"), PageInfo("p2", "Media")],
    default_page_id="p1",
    active_page_id="p2",
)


@api_response
def _pages_payload(pages: DevicePages) -> DevicePages:
    """Serialise with the server's own encoder so the wire format can't drift."""
    return pages


class TestPageRefresh:  # pylint: disable=protected-access
    def setup_method(self):
        EventManager.reset()
        AppContext.reset()
        AppContext.get().active_device = DeviceHardwareInfo(
            id="dev1", type="Stream Deck", vendor_id=0, product_id=0, num_keys=15
        )
        self.manager = ConnectionManager()
        self.conn = MagicMock()
        self.conn.root.pages_list.return_value = str(_pages_payload(_DEVICE_PAGES))
        self.conn.root.key_get_states.return_value = json.dumps([])
        self.manager._conn = self.conn

    def teardown_method(self):
        self.manager._conn = None
        AppContext.reset()

    def test_refresh_pages_pushes_parsed_pages(self):
        self.manager._refresh_pages()

        events = [e for e in EventManager.get().drain() if isinstance(e, PagesUpdatedEvent)]
        assert_that(events).is_length(1)
        assert_that(events[0].pages).is_equal_to(_DEVICE_PAGES)
        self.conn.root.pages_list.assert_called_once_with("dev1")

    def test_refresh_pages_without_device_is_a_no_op(self):
        AppContext.reset()

        self.manager._refresh_pages()

        self.conn.root.pages_list.assert_not_called()

    def test_malformed_or_failed_pages_response_is_logged_not_raised(self):
        for reply in (json.dumps([]), json.dumps({"device_id": "dev1", "pages": []}), RuntimeError("down")):
            if isinstance(reply, Exception):
                self.conn.root.pages_list.side_effect = reply
            else:
                self.conn.root.pages_list.return_value = reply

            self.manager._refresh_pages()

        assert_that([e for e in EventManager.get().drain() if isinstance(e, PagesUpdatedEvent)]).is_empty()

    def test_pages_changed_for_active_device_requests_refresh(self):
        self.manager._on_server_event("pages_changed", json.dumps({"device_id": "dev1"}))

        assert_that(self.manager._layout_refresh_requested.is_set()).is_true()
        assert_that(self.manager._device_refresh_requested.is_set()).is_false()
        assert_that(self.manager._wake_event.is_set()).is_true()
        self.conn.root.pages_list.assert_not_called()  # fetched later on the worker thread

    def test_worker_layout_refresh_skips_rules_and_screensaver(self):
        self.manager.request_layout_refresh()
        with patch("decker.client.connection_manager._HEARTBEAT_INTERVAL_S", 3600):
            self.manager._run_once(last_heartbeat=time.monotonic())

        self.conn.root.pages_list.assert_called_once_with("dev1")
        self.conn.root.key_get_states.assert_called_once_with("dev1")
        self.conn.root.app_rules_list.assert_not_called()
        self.conn.root.screensaver_get.assert_not_called()

    def test_pages_changed_for_other_device_is_ignored(self):
        self.manager._on_server_event("pages_changed", json.dumps({"device_id": "other"}))

        assert_that(self.manager._device_refresh_requested.is_set()).is_false()
        assert_that(self.manager._layout_refresh_requested.is_set()).is_false()

    def test_worker_handles_refresh_request(self):
        self.manager.request_device_refresh()
        with patch("decker.client.connection_manager._HEARTBEAT_INTERVAL_S", 3600):
            self.manager._run_once(last_heartbeat=time.monotonic())

        self.conn.root.pages_list.assert_called_once_with("dev1")
        self.conn.root.key_get_states.assert_called_once_with("dev1")
        assert_that(self.manager._device_refresh_requested.is_set()).is_false()

    @patch("decker.client.connection_manager.rpyc.utils.factory.connect")
    def test_connect_fetches_pages_for_active_device(self, mock_connect: MagicMock):
        conn = _healthy_conn()
        conn.root.pages_list.return_value = str(_pages_payload(_DEVICE_PAGES))
        conn.root.key_get_states.return_value = json.dumps([])
        mock_connect.return_value = conn
        self.manager._conn = None
        with (
            patch("decker.client.config.get_server_host", return_value="localhost"),
            patch("decker.client.config.get_server_port", return_value=18861),
        ):
            self.manager._try_connect()

        assert_that([e for e in EventManager.get().drain() if isinstance(e, PagesUpdatedEvent)]).is_length(1)
        self.manager._close_connection()

    def test_updates_event_routed(self):
        payload = {"updates": [{"id": "a.b", "name": "B", "installed": "1", "available": "2"}], "missing": []}
        self.manager._on_server_event(
            "plugin_updates_available", json.dumps(payload)
        )  # pylint: disable=protected-access
        (event,) = EventManager.get().drain()
        assert isinstance(event, PluginUpdatesAvailableEvent)
        assert_that(event.updates).is_equal_to(payload["updates"])
        assert_that(event.missing).is_equal_to([])

    def test_check_finished_event_routed(self):
        payload = {"repositories": [{"url": "u", "name": "N", "plugin_count": 1, "last_checked": 1.0, "error": None}]}
        self.manager._on_server_event(
            "repositories_check_finished", json.dumps(payload)
        )  # pylint: disable=protected-access
        (event,) = EventManager.get().drain()
        assert isinstance(event, RepositoriesCheckFinishedEvent)
        assert_that(event.repositories).is_equal_to(payload["repositories"])

    def test_updates_fetched_on_connect(self):
        conn = _healthy_conn()
        conn.root.plugin_updates.return_value = {"updates": [], "missing": [{"id": "m"}]}
        with patch("rpyc.utils.factory.connect", return_value=conn), patch(
            "decker.client.connection_manager.BgServingThread"
        ):
            self.manager._try_connect()  # pylint: disable=protected-access
        events = [e for e in EventManager.get().drain() if isinstance(e, PluginUpdatesAvailableEvent)]
        assert_that(events).is_length(1)
        assert_that(events[0].missing).is_equal_to([{"id": "m"}])


_DEVICE_PROFILES = DeviceProfiles(
    device_id="dev1",
    profiles=[ProfileInfo(0, "Default", ""), ProfileInfo(2, "Work", "pack:a/b.svg")],
    active_profile_id=2,
)


@api_response
def _profiles_payload(profiles: DeviceProfiles) -> DeviceProfiles:
    return profiles


class TestProfileRefresh:  # pylint: disable=protected-access
    def setup_method(self):
        EventManager.reset()
        AppContext.reset()
        AppContext.get().active_device = DeviceHardwareInfo(
            id="dev1", type="Stream Deck", vendor_id=0, product_id=0, num_keys=15
        )
        self.manager = ConnectionManager()
        self.conn = MagicMock()
        self.conn.root.pages_list.return_value = str(_pages_payload(_DEVICE_PAGES))
        self.conn.root.profiles_list.return_value = str(_profiles_payload(_DEVICE_PROFILES))
        self.conn.root.key_get_states.return_value = json.dumps([])
        self.manager._conn = self.conn

    def teardown_method(self):
        self.manager._conn = None
        AppContext.reset()

    def test_refresh_profiles_pushes_parsed_profiles(self):
        self.manager._refresh_profiles()

        events = [e for e in EventManager.get().drain() if isinstance(e, ProfilesUpdatedEvent)]
        assert_that(events).is_length(1)
        assert_that(events[0].profiles).is_equal_to(_DEVICE_PROFILES)
        self.conn.root.profiles_list.assert_called_once_with("dev1")

    def test_malformed_or_failed_profiles_response_is_logged_not_raised(self):
        replies = [json.dumps([]), json.dumps({"device_id": "dev1", "profiles": []}), RuntimeError("down")]
        replies.append(
            json.dumps({"device_id": "d", "profiles": [{"profile_id": "x", "name": "n"}], "active_profile_id": 0})
        )
        for reply in replies:
            if isinstance(reply, Exception):
                self.conn.root.profiles_list.side_effect = reply
            else:
                self.conn.root.profiles_list.return_value = reply

            self.manager._refresh_profiles()

        assert_that([e for e in EventManager.get().drain() if isinstance(e, ProfilesUpdatedEvent)]).is_empty()

    def test_refresh_without_device_is_a_no_op(self):
        AppContext.reset()

        self.manager._refresh_profiles()

        self.conn.root.profiles_list.assert_not_called()

    def test_profiles_changed_for_active_device_requests_refresh(self):
        self.manager._on_server_event("profiles_changed", json.dumps({"device_id": "dev1"}))

        assert_that(self.manager._layout_refresh_requested.is_set()).is_true()

    def test_profiles_changed_for_other_device_is_ignored(self):
        self.manager._on_server_event("profiles_changed", json.dumps({"device_id": "other"}))

        assert_that(self.manager._device_refresh_requested.is_set()).is_false()

    def test_device_refresh_fetches_pages_and_profiles(self):
        self.manager._refresh_active_device()

        self.conn.root.pages_list.assert_called_once_with("dev1")
        self.conn.root.profiles_list.assert_called_once_with("dev1")

    def test_pages_carry_their_profile_id(self):
        payload = json.loads(str(_pages_payload(DevicePages("dev1", [PageInfo("p1", "A")], "p1", "p1", profile_id=3))))
        self.conn.root.pages_list.return_value = json.dumps(payload)

        self.manager._refresh_pages()

        pages = [e for e in EventManager.get().drain() if isinstance(e, PagesUpdatedEvent)][0].pages
        assert_that(pages.profile_id).is_equal_to(3)


class TestDevicesChanged:  # pylint: disable=protected-access
    def setup_method(self):
        EventManager.reset()
        AppContext.reset()
        AppContext.get().active_device = DeviceHardwareInfo(
            id="dev1", type="Stream Deck", vendor_id=0, product_id=0, num_keys=15
        )
        self.manager = ConnectionManager()
        self.conn = MagicMock()
        self.conn.root.devices_list.return_value = json.dumps([])
        self.conn.root.pages_list.return_value = str(_pages_payload(_DEVICE_PAGES))
        self.conn.root.profiles_list.return_value = str(_profiles_payload(_DEVICE_PROFILES))
        self.conn.root.key_get_states.return_value = json.dumps([])
        self.manager._conn = self.conn

    def teardown_method(self):
        self.manager._conn = None
        AppContext.reset()

    def test_the_event_asks_for_a_device_list_and_active_device_refresh(self):
        self.manager._on_server_event("devices_changed", json.dumps({"device_id": "dev1"}))

        assert_that(self.manager._devices_refresh_requested.is_set()).is_true()
        assert_that(self.manager._device_refresh_requested.is_set()).is_true()
        self.conn.root.devices_list.assert_not_called()  # fetched later on the worker thread

    def test_the_worker_lists_devices_and_refreshes_the_active_one(self):
        self.manager._on_server_event("devices_changed", json.dumps({"device_id": "dev1"}))
        with patch("decker.client.connection_manager._HEARTBEAT_INTERVAL_S", 3600):
            self.manager._run_once(last_heartbeat=time.monotonic())

        self.conn.root.devices_list.assert_called_once_with()
        self.conn.root.pages_list.assert_called_once_with("dev1")
        assert_that(self.manager._devices_refresh_requested.is_set()).is_false()


class TestServerSettingsCalls:
    _PAYLOAD = {
        "port": 18861,
        "memory_limit": 1073741824,
        "icon_max_file_size": 4194304,
        "stop_when_last_client_leaves": False,
        "reconfigurable": True,
        "last_apply_error": None,
    }

    def setup_method(self) -> None:
        EventManager.reset()
        self.manager = ConnectionManager()

    def test_refresh_parses_the_server_reply_and_stores_it(self) -> None:
        conn = MagicMock()
        conn.root.server_settings.return_value = json.dumps(self._PAYLOAD)
        self.manager._conn = conn  # pylint: disable=protected-access

        info = self.manager.refresh_server_settings()

        assert_that(info).is_not_none()
        assert_that(info.reconfigurable).is_true()  # type: ignore[union-attr]
        assert_that(self.manager.server_settings).is_equal_to(info)

    def test_refresh_without_a_connection_gives_none(self) -> None:
        assert_that(self.manager.refresh_server_settings()).is_none()

    def test_refresh_ignores_a_malformed_reply(self) -> None:
        conn = MagicMock()
        conn.root.server_settings.return_value = "{not json"
        self.manager._conn = conn  # pylint: disable=protected-access

        assert_that(self.manager.refresh_server_settings()).is_none()

    def test_set_returns_the_server_verdict(self) -> None:
        conn = MagicMock()
        conn.root.server_settings_set.return_value = json.dumps({"ok": False, "restarting": False, "error": "nope"})
        self.manager._conn = conn  # pylint: disable=protected-access

        assert_that(self.manager.set_server_settings({"port": 1})).is_equal_to((False, "nope"))
        conn.root.server_settings_set.assert_called_once_with('{"port": 1}')

    def test_set_without_a_connection_reports_no_answer(self) -> None:
        ok, error = self.manager.set_server_settings({"port": 1})

        assert_that(ok).is_false()
        assert_that(error).is_not_empty()

    def test_closing_the_connection_forgets_the_settings(self) -> None:
        conn = MagicMock()
        conn.root.server_settings.return_value = json.dumps(self._PAYLOAD)
        self.manager._conn = conn  # pylint: disable=protected-access
        self.manager.refresh_server_settings()

        self.manager._close_connection()  # pylint: disable=protected-access

        assert_that(self.manager.server_settings).is_none()


def test_try_connect_attaches_as_a_client() -> None:
    EventManager.reset()
    manager = ConnectionManager()
    conn = MagicMock()
    try:
        with patch("rpyc.utils.factory.connect", return_value=conn):
            manager._try_connect()  # pylint: disable=protected-access
    finally:
        manager._close_connection()  # pylint: disable=protected-access

    conn.root.client_attached.assert_called_once_with()


_RULE = AppRule("r1", "Browser", True, [MatchCondition("window_class", "equals", "firefox")], 2, "pg1")
_RULES = DeviceRules("dev1", [_RULE], DefaultRule(True, 1, ""))
_STATUS = {
    "state": "ok",
    "reason": "",
    "backend": "kwin",
    "supports_pick": True,
    "window": {"window_class": "firefox", "title": "T", "pid": 7},
}


@api_response
def _wire(value: object) -> object:
    """Serialise with the server's own encoder so the wire format can't drift."""
    return value


def test_parse_device_rules_round_trip() -> None:
    assert_that(_parse_device_rules(json.loads(str(_wire(_RULES))))).is_equal_to(_RULES)


def test_parse_rules_rejects_malformed() -> None:
    good = json.loads(str(_wire(_RULES)))
    bad_cond = json.loads(str(_wire(_RULES)))
    bad_cond["rules"][0]["conditions"] = [{"field": "title"}]
    no_default = {k: v for k, v in good.items() if k != "default_rule"}
    bad_inputs: list[Any] = [None, [], "x", bad_cond, no_default, {**good, "rules": "oops"}]
    for data in bad_inputs:
        assert_that(_parse_device_rules).raises(ValueError).when_called_with(data)


def test_parse_window_tolerates_missing_fields_and_null() -> None:
    assert_that(_parse_window(None)).is_none()
    assert_that(_parse_window({})).is_equal_to(WindowInfo())
    assert_that(_parse_window({"window_class": "a", "pid": 3})).is_equal_to(WindowInfo(window_class="a", pid=3))


class TestAppRuleClient:  # pylint: disable=protected-access
    def setup_method(self) -> None:
        EventManager.reset()
        AppContext.reset()
        AppContext.get().active_device = DeviceHardwareInfo(
            id="dev1", type="Stream Deck", vendor_id=0, product_id=0, num_keys=15
        )
        self.manager = ConnectionManager()
        self.conn = MagicMock()
        self.conn.root.app_rules_list.return_value = str(_wire(_RULES))
        self.conn.root.window_detection_status.return_value = json.dumps(_STATUS)
        self.manager._conn = self.conn

    def teardown_method(self) -> None:
        self.manager._conn = None
        AppContext.reset()

    def test_app_rules_changed_event_refreshes_rules(self) -> None:
        self.manager._on_server_event("app_rules_changed", json.dumps({"device_id": "dev1"}))
        self.conn.root.app_rules_list.assert_not_called()  # fetched later on the worker thread
        with patch("decker.client.connection_manager._HEARTBEAT_INTERVAL_S", 3600):
            self.manager._run_once(last_heartbeat=time.monotonic())

        events = [e for e in EventManager.get().drain() if isinstance(e, AppRulesUpdatedEvent)]
        assert_that(events).is_length(1)
        assert_that(events[0].rules).is_equal_to(_RULES)
        self.conn.root.app_rules_list.assert_called_once_with("dev1")

    def test_app_rules_changed_for_other_device_is_ignored(self) -> None:
        self.manager._on_server_event("app_rules_changed", json.dumps({"device_id": "other"}))

        assert_that(self.manager._rules_refresh_requested.is_set()).is_false()

    def test_detection_changed_event_refreshes_status(self) -> None:
        self.manager._on_server_event("window_detection_changed", json.dumps(_STATUS))
        with patch("decker.client.connection_manager._HEARTBEAT_INTERVAL_S", 3600):
            self.manager._run_once(last_heartbeat=time.monotonic())

        events = [e for e in EventManager.get().drain() if isinstance(e, DetectionStatusEvent)]
        assert_that(events).is_length(1)

    def test_malformed_or_failed_rules_response_is_logged_not_raised(self) -> None:
        self.conn.root.app_rules_list.return_value = json.dumps([])
        self.manager.refresh_app_rules()
        self.conn.root.app_rules_list.side_effect = RuntimeError("down")
        self.manager.refresh_app_rules()

        assert_that(EventManager.get().drain()).is_empty()

    @patch("decker.client.connection_manager.rpyc.utils.factory.connect")
    def test_detection_status_pushed_on_connect(self, mock_connect: MagicMock) -> None:
        conn = _healthy_conn()
        conn.root.window_detection_status.return_value = json.dumps(_STATUS)
        conn.root.app_rules_list.return_value = str(_wire(_RULES))
        mock_connect.return_value = conn
        self.manager._conn = None
        with (
            patch("decker.client.config.get_server_host", return_value="localhost"),
            patch("decker.client.config.get_server_port", return_value=18861),
        ):
            self.manager._try_connect()

        drained = EventManager.get().drain()
        (status,) = [e for e in drained if isinstance(e, DetectionStatusEvent)]
        assert_that(status).is_equal_to(
            DetectionStatusEvent("ok", "", "kwin", True, WindowInfo(window_class="firefox", title="T", pid=7))
        )
        assert_that([e for e in drained if isinstance(e, AppRulesUpdatedEvent)]).is_length(1)
        self.manager._close_connection()

    def test_window_focused_maps_to_event(self) -> None:
        self.manager._on_server_event("window_focused", json.dumps({"window": {"window_class": "x", "pid": 1}}))

        assert_that(EventManager.get().drain()).is_equal_to([WindowFocusedEvent(WindowInfo(window_class="x", pid=1))])

    def test_window_picked_event_pushes_event_with_none_on_cancel(self) -> None:
        self.manager._on_server_event("window_picked", json.dumps({"window": None}))
        self.manager._on_server_event("window_picked", json.dumps({"window": {"window_class": "y"}}))

        assert_that(EventManager.get().drain()).is_equal_to(
            [WindowPickedEvent(None), WindowPickedEvent(WindowInfo(window_class="y"))]
        )

    def test_bool_rpc_results_are_parsed_not_truthy(self) -> None:
        for name in ("app_rule_update", "app_rule_delete", "app_rule_move", "app_default_rule_set", "window_pick"):
            getattr(self.conn.root, name).return_value = "false"
        assert_that(self.manager.app_rule_update("dev1", _RULE)).is_false()
        assert_that(self.manager.app_rule_delete("dev1", "r1")).is_false()
        assert_that(self.manager.app_rule_move("dev1", "r1", 0)).is_false()
        assert_that(self.manager.app_default_rule_set("dev1", DefaultRule())).is_false()
        assert_that(self.manager.window_pick()).is_false()

    def test_edit_calls_send_plain_dicts_and_parse_true(self) -> None:
        for name in ("app_rule_update", "app_rule_delete", "app_rule_move", "app_default_rule_set", "window_pick"):
            getattr(self.conn.root, name).return_value = "true"
        assert_that(self.manager.app_rule_update("dev1", _RULE)).is_true()
        sent = self.conn.root.app_rule_update.call_args.args[1]
        assert_that(sent["conditions"]).is_equal_to([{"field": "window_class", "op": "equals", "value": "firefox"}])
        assert_that(self.manager.app_default_rule_set("dev1", DefaultRule(True, 3, "p"))).is_true()
        self.conn.root.app_default_rule_set.assert_called_once_with("dev1", True, 3, "p")
        assert_that(self.manager.app_rule_move("dev1", "r1", 2)).is_true()
        self.conn.root.app_rule_move.assert_called_once_with("dev1", "r1", 2)
        assert_that(self.manager.window_pick()).is_true()

    def test_app_rule_add_returns_stored_rule_or_none(self) -> None:
        self.conn.root.app_rule_add.return_value = str(_wire(_RULE))
        assert_that(self.manager.app_rule_add("dev1", _RULE)).is_equal_to(_RULE)
        self.conn.root.app_rule_add.return_value = "null"
        assert_that(self.manager.app_rule_add("dev1", _RULE)).is_none()

    def test_edit_failure_returns_false(self) -> None:
        self.conn.root.app_rule_delete.side_effect = RuntimeError("down")
        assert_that(self.manager.app_rule_delete("dev1", "r1")).is_false()
