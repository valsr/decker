"""Manages the Decker RPyC server subprocess."""

import json
import logging
import multiprocessing
import multiprocessing.synchronize
import os
import signal
import socket
import sys
import threading
from typing import Any, Protocol

from rpyc.utils.server import ThreadedServer

import decker.common.logging
from decker.common import config
from decker.common import paths as decker_paths
from decker.common.context import ServerAppContext
from decker.common.manager import Manager
from decker.common.version import decker_version
from decker.server.app_rule_controller import AppRuleController
from decker.server.app_rule_engine import AppRuleEngine
from decker.server.app_rule_store import AppRuleStore
from decker.server.client_tracker import ClientTracker
from decker.server.device_state_store import DeviceStateStore
from decker.server.devices.manager import DeviceManager
from decker.server.event_bus import EventBus
from decker.server.key_assignments import KeyAssignmentStore
from decker.server.page_controller import PageController
from decker.server.pages import PageStore
from decker.server.plugin_installer import PluginInstaller
from decker.server.plugins import PluginManager
from decker.server.reconfigure import RESTART_EXIT_CODE, ReconfigurePolicy
from decker.server.repositories import RepositoryStore
from decker.server.screensaver.screensaver_controller import ScreensaverController
from decker.server.screensaver.screensaver_engine import ScreensaverEngine
from decker.server.screensaver.screensaver_store import ScreensaverStore
from decker.server.server_context import ServerContext
from decker.server.service import DeckerService
from decker.server.update_checker import UpdateChecker
from decker.server.window_wiring import build_window_watcher
from decker.server.windows.select import select_backend


class NoDelayThreadedServer(ThreadedServer):
    """One thread per client with Nagle disabled.

    rpyc's ThreadPoolServer notices an idle client's next request only on its 0.1 s poll, so every request, and every
    reply to a call the server makes to a client, took about 200 ms. A thread per client blocks on its socket instead.
    """

    def _authenticate_and_serve_client(self, sock: socket.socket) -> None:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        super()._authenticate_and_serve_client(sock)


class Closable(Protocol):  # pylint: disable=too-few-public-methods
    """Something that can be asked to stop serving."""

    def close(self) -> None:
        """Stop serving."""


class ShutdownGate:
    """Remembers a shutdown request so one that arrives before the server exists still takes effect."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._requested = False
        self._server: Closable | None = None

    @property
    def requested(self) -> bool:
        """True once a shutdown has been requested."""
        with self._lock:
            return self._requested

    def request(self) -> None:
        """Record a shutdown request and close the attached server, if any."""
        with self._lock:
            self._requested = True
            server, self._server = self._server, None
        if server is not None:
            server.close()

    def attach(self, server: Closable) -> bool:
        """Attach the server; returns True (after closing it) if a shutdown was already requested."""
        with self._lock:
            self._server = server
            requested = self._requested
            if requested:
                self._server = None
        if requested:
            server.close()
        return requested


def stop_process(
    process: multiprocessing.Process,
    stop_event: multiprocessing.synchronize.Event,
    grace: float,
    terminate_wait: float = 5.0,
) -> str:
    """Set the stop event, wait `grace`, then terminate, then kill; returns "graceful", "terminated" or "killed"."""
    stop_event.set()
    process.join(timeout=grace)
    if not process.is_alive():
        return "graceful"
    logging.warning("Server process did not stop on request, terminating")
    process.terminate()
    process.join(timeout=terminate_wait)
    if not process.is_alive():
        return "terminated"
    logging.warning("Server process did not terminate gracefully, killing")
    process.kill()
    process.join()
    return "killed"


class ServerProcess:
    """Spawns and owns the RPyC server subprocess."""

    def __init__(self, port: int, startup_wait: float, app_context: ServerAppContext, host: str = "localhost") -> None:
        """Initialize the server process."""
        self.host = host
        self.port = port
        self._startup_wait = startup_wait
        self._process: multiprocessing.Process | None = None
        self._context: ServerContext | None = None
        self._app_context = app_context
        self._stop_event: multiprocessing.synchronize.Event = multiprocessing.Event()
        self._gate: ShutdownGate | None = None

    def start(self) -> bool:
        """Spawn the RPyC server subprocess.

        Returns:
            bool: True if the process started and is alive, False otherwise.
        """
        if self._process is not None and self._process.is_alive():
            logging.warning("Server process is already running")
            return False

        try:
            logging.info("Starting RPyC server on port %d", self.port)
            self._stop_event.clear()
            self._process = multiprocessing.Process(target=self._run_server, daemon=False)
            self._process.start()
            self._process.join(timeout=self._startup_wait)

            if self._process.is_alive():
                logging.info("RPyC server process started (pid %d)", self._process.pid)
                return True

            logging.error("Server process exited immediately after spawn")
            return False
        except Exception as e:
            logging.error("Failed to start server process: %s", e)
            return False

    def stop(self) -> bool:
        """Terminate the RPyC server subprocess.

        Returns:
            bool: True if stopped successfully, False an issue occurred and the process couldn't be stopped.
        """
        if self._process is None:
            logging.warning("No server process to stop")
            return True

        if not self._process.is_alive():
            self._process = None
            return True

        try:
            logging.info("Stopping RPyC server (pid %d)", self._process.pid or -1)
            timeout = float(config.get_as_or_default("server.stop_timeout", float, 10.0))
            stop_process(self._process, self._stop_event, timeout)
            logging.info("RPyC server stopped")
            self._process = None
            return True
        except Exception as e:
            logging.error("Failed to stop server process: %s", e)
            return False

    def _try_destroy_manager(self, manager: Manager) -> None:
        """Call destroy() on a manager, catching and logging any exception."""
        try:
            manager.destroy()
        except Exception as e:  # pylint: disable=broad-except
            logging.error("Error destroying %s: %s", type(manager).__name__, e)

    @property
    def is_running(self) -> bool:
        """True if the server subprocess is alive."""
        return self._process is not None and self._process.is_alive()

    @property
    def pid(self) -> int | None:
        """PID of the server subprocess, or None if not running."""
        if self._process and self._process.is_alive():
            return self._process.pid
        return None

    @property
    def exit_code(self) -> int | None:
        """Exit code of the subprocess, or None if still running or not started."""
        if self._process is not None and not self._process.is_alive():
            return self._process.exitcode
        return None

    @property
    def exited_cleanly(self) -> bool:
        """True when the subprocess stopped with exit code 0 (intentional stop)."""
        return self.exit_code == 0

    # ------------------------------------------------------------------
    # Child-process internals
    # ------------------------------------------------------------------

    def _on_hardware_key_event(self, device_id: str, key_index: int, pressed: bool) -> None:
        """Route a hardware key event to the plugin and push it to all connected clients."""
        if self._context is None:
            return
        page_id = self._context.page_controller.active_page_id(device_id)
        assignment = self._context.key_assignment_store.get(device_id, page_id, key_index)
        if assignment is not None:
            if pressed:
                self._context.plugin_manager.dispatch_key_down(assignment.target)
            else:
                self._context.plugin_manager.dispatch_key_up(assignment.target)
        self._context.event_bus.push(
            "key_event",
            json.dumps({"device_id": device_id, "key_index": key_index, "pressed": pressed}),
        )

    def _restore_devices(self, device_id_filter: str | None = None) -> None:
        """Show each connected device's active page and re-activate that page's persisted assignments.

        If device_id_filter is given, only that device is restored. Images are re-pushed to the hardware
        because the device is blank after a server restart.
        """
        if self._context is None:
            return
        all_ids = {info.id for info in self._context.device_manager.list_devices() if info.id is not None}
        device_ids = {device_id_filter} & all_ids if device_id_filter else all_ids
        for device_id in sorted(device_ids):
            self._context.page_controller.restore(device_id)

    def _start_window_watcher(self, ctx: ServerContext) -> None:
        try:
            ctx.window_watcher = build_window_watcher(ctx, select_backend(os.environ, sys.platform))
            ctx.window_watcher.start()
            engine = ctx.app_rule_engine
            for device_id in ctx.device_manager.opened_device_ids():
                if engine is not None:
                    engine.evaluate_device(device_id)
        except Exception as exc:  # pylint: disable=broad-except
            logging.warning("Window detection could not start: %s", exc)

    def _resolve_target_device(self, target: str) -> str | None:
        """Find a connected device matching the given ID or USB path.

        Returns the device ID if found, or None if no match.
        """
        if self._context is None:
            return None
        for info in self._context.device_manager.list_devices():
            if info.id == target or info.path == target:
                return info.id
        return None

    def _signal_handler(self, signum: int, _: Any) -> None:
        """Graceful shutdown: close the RPyC server."""
        logging.info("Received signal %d, shutting down", signum)
        self._request_shutdown()

    def _cleanup(self, ctx: ServerContext, managers: list[Manager]) -> None:
        if ctx.client_tracker is not None:
            ctx.client_tracker.cancel()
        if ctx.update_checker is not None:
            ctx.update_checker.stop()
        if ctx.window_watcher is not None:
            ctx.window_watcher.stop()
        ctx.device_manager.set_open_hook(None)
        for manager in reversed(managers):
            self._try_destroy_manager(manager)

    def _on_last_client_gone(self) -> None:
        ctx = self._context
        if ctx is None or ctx.restart_requested:
            return
        if config.get_as("server.stop_when_last_client_leaves", bool):
            logging.info("The last client has left, shutting down")
            self._request_shutdown()

    def _request_shutdown(self) -> None:
        if self._gate is not None:
            self._gate.request()

    def _watch_stop_event(self) -> None:
        self._stop_event.wait()
        logging.info("Stop requested, shutting down")
        self._request_shutdown()

    def _serve(self, server: NoDelayThreadedServer, gate: ShutdownGate) -> None:
        if gate.requested:
            return
        try:
            server.start()
        except OSError:
            if not gate.requested:
                raise

    def _build_context(self) -> tuple[ServerContext, list[Manager]]:
        state_store = DeviceStateStore()
        page_store = PageStore()
        key_store = KeyAssignmentStore()
        page_controller = PageController()
        device_manager = DeviceManager(on_key_event=self._on_hardware_key_event)
        plugin_manager = PluginManager()
        rule_store, rule_controller, rule_engine = AppRuleStore(), AppRuleController(), AppRuleEngine()
        saver_store, saver_controller = ScreensaverStore(), ScreensaverController()
        saver_engine = ScreensaverEngine(
            saver_store, device_manager, page_controller.active_page_id, saver_controller.notify_changed
        )

        context = ServerContext(
            device_manager=device_manager,
            plugin_manager=plugin_manager,
            key_assignment_store=key_store,
            device_state_store=state_store,
            page_store=page_store,
            page_controller=page_controller,
            event_bus=EventBus(),
            server=None,
            reconfigure_policy=ReconfigurePolicy(self._app_context.allow_client_reconfiguration),
            app_rule_store=rule_store,
            app_rule_controller=rule_controller,
            app_rule_engine=rule_engine,
            screensaver_store=saver_store,
            screensaver_controller=saver_controller,
            screensaver_engine=saver_engine,
            client_tracker=ClientTracker(config.get_as("server.stop_grace", float), self._on_last_client_gone),
        )

        managers: list[Manager] = [
            state_store,
            page_store,
            key_store,
            rule_store,
            saver_store,
            page_controller,
            device_manager,
            plugin_manager,
            rule_controller,
            rule_engine,
            saver_controller,  # before the engine: the engine's shutdown wake still announces through it
            saver_engine,  # destroyed before the device manager, so it can wake active devices
        ]
        self._context = context
        for manager in managers:
            manager.create(context)
        device_manager.set_open_hook(saver_engine.device_opened)
        saver_controller.add_brightness_listener(plugin_manager.on_deck_brightness_changed)
        return context, managers

    def _run_server(self) -> None:
        """Entry point for the server subprocess. Runs RPyC in the main thread."""
        gate = ShutdownGate()
        self._gate = gate
        threading.Thread(target=self._watch_stop_event, name="stop-watcher", daemon=True).start()
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        # Re-apply prefix configuration in the subprocess (safe with fork; required with spawn).
        decker_paths.configure(self._app_context.prefix)

        self.server_id = f"{self.pid or 'unknown'}"
        decker.common.logging.configure(
            self._app_context.verbosity, server_id=self.server_id, log_others=self._app_context.log_others
        )

        ctx, managers = self._build_context()

        # Resolve the target device (if --device was passed).
        matched_device_id: str | None = None
        if self._app_context.target_device is not None:
            matched_device_id = self._resolve_target_device(self._app_context.target_device)
            if matched_device_id is None:
                logging.error("Device not found: %s", self._app_context.target_device)
                for manager in reversed(managers):
                    self._try_destroy_manager(manager)
                sys.exit(1)

        # Reset device state if requested.
        if self._app_context.reset_device_data:
            if matched_device_id is not None:
                ctx.device_state_store.delete_all_key_state_for_device(matched_device_id)
            else:
                ctx.device_state_store.delete_all_key_state()

        ctx.plugin_installer = PluginInstaller(decker_paths.plugins(), ctx, decker_version())
        ctx.repository_store = RepositoryStore(decker_paths.repositories())
        ctx.update_checker = UpdateChecker(
            ctx.repository_store,
            ctx.plugin_manager,
            ctx.key_assignment_store,
            lambda event, payload: ctx.event_bus.push(event, json.dumps(payload)),
        )
        self._restore_devices(device_id_filter=matched_device_id)
        self._start_window_watcher(ctx)
        ctx.device_manager.start_watching(float(config.get_or_default("server.device_watch_interval", 2.0)))

        server = NoDelayThreadedServer(
            DeckerService,
            hostname=self.host,
            port=self.port,
            protocol_config={"allow_public_attrs": True},
        )
        ctx.server = server
        stop_requested = gate.attach(server)
        DeckerService.configure_context(ctx)
        logging.info("RPyC server listening on %s:%d", self.host, self.port)
        try:
            if not stop_requested:
                ctx.update_checker.start()
                self._serve(server, gate)
        finally:
            self._cleanup(ctx, managers)
        if ctx.restart_requested:
            sys.exit(RESTART_EXIT_CODE)
        # Normal return -> subprocess exits with code 0 (intentional stop)
