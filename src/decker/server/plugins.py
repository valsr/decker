"""Owns plugin discovery and runs plugin processes through the supervisor."""

import base64
import binascii
import io
import json
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from PIL import Image

from decker.asset.iconpack import IconPackRegistry
from decker.asset.renderer import AssetRenderer
from decker.asset.tint import tint_svg
from decker.common import config
from decker.common.manager import Manager
from decker.common.options import is_valid_source
from decker.common.platforms import detect_platform, platform_supports
from decker.common.template import expand_template, template_names
from decker.common.text_style import is_color
from decker.common.variables import CORE_VARIABLE_NAMES
from decker.common.version import decker_version
from decker.plugin.bindings import (
    GESTURES,
    Binding,
    effective_bindings,
    normalize_stored_bindings,
    parse_binding,
)
from decker.plugin.environment import stage_sdk
from decker.plugin.events import PluginLoadFailedEvent
from decker.plugin.gestures import Gesture, GestureRecognizer
from decker.plugin.icons import read_icon
from decker.plugin.loader import (
    InstalledPlugin,
    PluginLoader,
    recover_interrupted_installs,
)
from decker.plugin.manifest import (
    ActionSpec,
    CardSpec,
    PluginManifest,
    declared_options_sources,
)
from decker.plugin.protocol import PROTOCOL_VERSION, Message
from decker.plugin.runtime.launcher import launch_command, launch_env
from decker.plugin.runtime.process import PluginProcess
from decker.plugin.runtime.supervisor import PluginSupervisor, SupervisorPolicy
from decker.plugin.settings_schema import (
    apply_schema,
    schema_to_json,
    validate_settings,
)
from decker.plugin.settings_store import PluginSettingsStore
from decker.plugin.target import CardTarget
from decker.server.action_calls import ActionCall
from decker.server.actions import ActionRunner
from decker.server.appearance import (
    PACK_ICON_SCALE,
    Composed,
    compose_key_face,
    inner_size,
    intern_background,
    resolve_appearance,
)
from decker.server.contributions import ContributionRegistry
from decker.server.hardware_sink import StreamDeckHardwareSink, write_face
from decker.server.options_runner import OptionsResult, OptionsRunner
from decker.server.page_vars import (
    DeviceLayout,
    VariableChain,
    deliver_settings,
    has_dynamic_fields,
    keep_raw_templates,
    page_variables,
    resolve_profile_switch_target,
    resolve_switch_target,
    stored_page_ids,
    stored_profile_ids,
    templated_texts,
)
from decker.server.variable_runner import VariableRunner
from decker.server.variables import VariableConflict, VariableStore

if TYPE_CHECKING:
    from decker.server.server_context import ServerContext

_DEFAULT_MEMORY_LIMIT = 256 * 1024 * 1024
_FAILED_LABEL = "Plugin failed"
_MISSING_LABEL = "Plugin missing"
_OPEN_URL_SCHEMES = ("http", "https")
_BLANK_CARD = "com.decker.blank.blank"
_DEFAULT_KEY_SIZE = (72, 72)


_StateChangedCallback = Callable[[str, int], None]


class _CardsHandler:
    """The "cards" contribution handler: delegates to the PluginManager's process and card logic."""

    kind = "cards"

    def __init__(self, manager: "PluginManager") -> None:
        self._manager = manager

    def register(self, plugin: InstalledPlugin) -> None:
        self._manager.register_cards(plugin)

    def unregister(self, plugin_id: str) -> None:
        self._manager.unregister_cards(plugin_id)


class _ActionsHandler:
    """The "actions" contribution handler: hands a plugin's actions to the ActionRunner."""

    kind = "actions"

    def __init__(self, manager: "PluginManager") -> None:
        self._manager = manager

    def register(self, plugin: InstalledPlugin) -> None:
        self._manager.register_actions(plugin)

    def unregister(self, plugin_id: str) -> None:
        self._manager.unregister_actions(plugin_id)


class _VariablesHandler:
    """The "variables" contribution handler: declares a plugin's variables in the VariableStore."""

    kind = "variables"

    def __init__(self, manager: "PluginManager") -> None:
        self._manager = manager

    def register(self, plugin: InstalledPlugin) -> None:
        self._manager.register_variables(plugin)

    def unregister(self, plugin_id: str) -> None:
        self._manager.unregister_variables(plugin_id)


@dataclass
class _Live:  # pylint: disable=too-many-instance-attributes
    """One active card instance: where it is, what it declared and how its gestures are recognised."""

    target: CardTarget
    card: CardSpec
    recognizer: GestureRecognizer
    bindings: dict[str, Binding]
    settings: dict[str, Any]
    appearance: dict[str, Any] = field(default_factory=dict)
    plugin_image: bytes | None = None
    plugin_icon_ref: str | None = None  # resolved at compose time, so an uninstalled pack drops the image
    plugin_image_color: str | None = None
    plugin_title: str = ""
    state: int = 0
    delivered: dict[str, Any] = field(default_factory=dict)
    delivered_bindings: dict[str, Any] = field(default_factory=dict)
    last_paint: datetime | None = None


_DEVICE_NAVIGATION: frozenset[str] = frozenset({"switch_page", "switch_profile"})
_FOREIGN_EVENTS: frozenset[str] = frozenset({"switch_page", "switch_profile", "show_ok", "show_alert", "set_image"})
_VARIABLE_EVENTS: frozenset[str] = frozenset(
    {"register_variable", "unregister_variable", "variable_updated", "variable_value"}
)


def _uses_pack_icon(live: _Live) -> bool:
    if live.plugin_icon_ref is not None:
        return True
    # The resolved look, like _compose: a manifest-default icon or background ref counts too.
    appearance = resolve_appearance(live.card.appearance, live.appearance)
    background = appearance["background"]
    return bool(appearance["icon"]["ref"]) or (background["type"] == "image" and bool(background["image"]["ref"]))


class PluginManager(Manager):  # pylint: disable=too-many-instance-attributes,too-many-public-methods
    """Owned by the server: discovers plugins, runs them out of process and routes events both ways."""

    def __init__(
        self,
        plugins_dir: Path | None = None,
        policy: SupervisorPolicy | None = None,
        settings_store: PluginSettingsStore | None = None,
        variable_timeout: float | None = None,
    ) -> None:
        self._settings_store = settings_store if settings_store is not None else PluginSettingsStore()
        self._user_loader = PluginLoader(plugins_dir) if plugins_dir is not None else PluginLoader()
        self._failures: list[PluginLoadFailedEvent] = []
        self._placeholders: set[tuple[str, int]] = set()
        self._plugins: dict[str, InstalledPlugin] = {}
        self._builtin_ids: set[str] = set()
        self._live: dict[str, _Live] = {}
        self._stored_bindings: dict[str, Mapping[str, Any]] = {}
        self._warned_foreign: set[tuple[str, str]] = set()
        self._lock = threading.RLock()
        self._sdk_path: Path | None = None
        self._device_manager: Any = None
        self._event_bus: Any = None
        self._page_controller: Any = None
        self._page_store: Any = None
        self._icon_cache: dict[tuple[Path, str | None], str] = {}
        self._hellos: dict[str, dict[str, Any]] = {}
        self._sink: StreamDeckHardwareSink = StreamDeckHardwareSink(None, self._set_key_label)
        self._state_changed_cb: _StateChangedCallback | None = None
        self.settings_persister: Callable[[CardTarget, dict[str, Any]], None] | None = None
        if policy is None:
            limit = int(config.get_or_default("plugin.memory_limit", _DEFAULT_MEMORY_LIMIT))
            policy = SupervisorPolicy(memory_limit=limit)
        self._supervisor = PluginSupervisor(policy, PluginProcess, self._on_plugin_message, self._on_plugin_failed)
        self._supervisor.on_exit = self._on_plugin_exit
        self._action_runner = ActionRunner(self._supervisor, self._on_action_failed, self._resolve_args)
        self._foreign_warned: set[str] = set()
        self._drive_warned: set[str] = set()
        self._render_warned: set[str] = set()
        self.icon_packs = IconPackRegistry(on_failure=self._record_failure)
        self.variables = VariableStore(on_change=self._on_variables_changed)
        self._variable_runner = VariableRunner(
            self._supervisor, self.variables, **({} if variable_timeout is None else {"timeout": variable_timeout})
        )
        self._options_runner = OptionsRunner(self._supervisor)
        self._contributions = ContributionRegistry(
            [_CardsHandler(self), _ActionsHandler(self), self.icon_packs, _VariablesHandler(self)]
        )

    def create(self, context: "ServerContext") -> None:
        """Wire up the device manager, then scan plugins and start the supervisor."""
        self._device_manager = context.device_manager
        if self._device_manager is not None:
            self._device_manager.add_open_listener(self._on_device_opened)
            self._device_manager.add_close_listener(self._on_device_closed)
        self._event_bus = context.event_bus
        self._page_controller = getattr(context, "page_controller", None)
        self._page_store = getattr(context, "page_store", None)
        self._sink = StreamDeckHardwareSink(self._device_manager, self._set_key_label)
        self._state_changed_cb = self._make_state_changed_cb(context)
        self.settings_persister = self._make_settings_persister(context)
        try:
            self._sdk_path = stage_sdk()
        except OSError:
            logging.exception("Could not stage the plugin SDK; installed plugins will not be able to start")
        self.icon_packs.warm()
        recover_interrupted_installs(self._user_loader.plugins_dir)
        builtin = self._user_loader.scan_builtin()
        user = self._user_loader.scan()
        with self._lock:
            self._failures = builtin.failures + user.failures
        for plugin in builtin.plugins:
            self._builtin_ids.add(plugin.manifest.id)
            self.add_plugin(plugin)
        for plugin in user.plugins:
            with self._lock:
                duplicate = plugin.manifest.id in self._plugins
                if duplicate:
                    self._failures.append(PluginLoadFailedEvent(plugin.manifest.id, "duplicate plugin id"))
            if not duplicate:
                self.add_plugin(plugin)
        self._supervisor.start()
        logging.info(
            "PluginManager started: %d loaded, %d failed to load", len(self.plugins()), len(self.load_failures())
        )

    def destroy(self) -> None:
        """Stop every plugin process and release resources."""
        if self._device_manager is not None:
            self._device_manager.remove_open_listener(self._on_device_opened)
            self._device_manager.remove_close_listener(self._on_device_closed)
        with self._lock:
            live = list(self._live.values())
            self._live.clear()
        for item in live:
            item.recognizer.cancel()
        self._variable_runner.stop_all()
        self._options_runner.stop_all()
        self._supervisor.stop_all()
        self._settings_store.close()

    def add_plugin(self, plugin: InstalledPlugin) -> None:
        """Register a plugin's contributions; an cards plugin's process starts when its first card appears."""
        manifest = plugin.manifest
        host = detect_platform()
        if not platform_supports(manifest.platforms, host):
            logging.warning("[%s] plugin does not declare support for this platform (%s)", manifest.id, host.label())
        with self._lock:
            self._plugins[manifest.id] = plugin
        try:
            self._contributions.add(plugin)
        except Exception as exc:  # pylint: disable=broad-except
            logging.exception("Registering plugin %s failed", manifest.id)
            self._record_failure(manifest.id, str(exc))
        else:
            if manifest.variables:  # always on: a permanent reference, dropped when unregister forgets the plugin
                self._supervisor.acquire(manifest.id)
        self._recompose_icon_users()

    def register_cards(self, plugin: InstalledPlugin) -> None:
        """Register a plugin's cards with the supervisor (the "cards" contribution handler)."""
        self._register_runtime(plugin)

    def register_actions(self, plugin: InstalledPlugin) -> None:
        """Make a plugin's actions runnable (the "actions" contribution handler)."""
        self._register_runtime(plugin)
        self._action_runner.register(plugin)

    def unregister_actions(self, plugin_id: str) -> None:
        """Forget a plugin's actions and stop its process (the "actions" contribution handler)."""
        with self._lock:
            self._foreign_warned.discard(plugin_id)
            self._drive_warned.discard(plugin_id)
            self._render_warned.discard(plugin_id)
        self._action_runner.unregister(plugin_id)
        self._supervisor.unregister(plugin_id)
        with self._lock:
            self._hellos.pop(plugin_id, None)

    def register_variables(self, plugin: InstalledPlugin) -> None:
        """Declare a plugin's variables; a collision raises VariableConflict (the "variables" handler)."""
        self._register_runtime(plugin)
        try:
            self.variables.declare(plugin.manifest.id, plugin.manifest.variables)
        except VariableConflict:
            self._supervisor.unregister(plugin.manifest.id)  # the registry only rolls back handlers that succeeded
            with self._lock:
                self._hellos.pop(plugin.manifest.id, None)
            raise
        self.refresh_variable_users(self._names_owned_by(plugin.manifest.id))  # "{name}" now expands to ""

    def unregister_variables(self, plugin_id: str) -> None:
        """Forget a plugin's variables and stop its process (the "variables" handler)."""
        owned = self._names_owned_by(plugin_id)
        self.variables.undeclare(plugin_id)
        self._supervisor.unregister(plugin_id)
        self._variable_runner.plugin_stopped(plugin_id)
        self._options_runner.plugin_stopped(plugin_id)
        with self._lock:
            self._hellos.pop(plugin_id, None)
        self.refresh_variable_users(owned)  # "{name}" now stays as typed

    def _on_variables_changed(self, names: frozenset[str]) -> None:
        self._variable_runner.changed(names)
        self.refresh_variable_users(names)

    def _on_plugin_exit(self, plugin_id: str) -> None:
        self._variable_runner.plugin_stopped(plugin_id)
        # Check-then-act: a restart landing between has_process() and plugin_stopped() would fail a fresh request
        # (a microsecond window; a generation-tagged on_exit would close it).
        if not self._supervisor.has_process(plugin_id):  # else a fresh process already serves a newer request
            self._options_runner.plugin_stopped(plugin_id)
            self._action_runner.plugin_stopped(plugin_id)
        self.variables.drop_values(plugin_id)

    def _names_owned_by(self, plugin_id: str) -> frozenset[str]:
        with self._lock:
            lives = list(self._live.values())
        used = frozenset().union(*(title | texts for title, texts in map(self._template_uses, lives)))
        return frozenset(name for name in used if self.variables.owner_of(name) == plugin_id)

    def list_actions(self) -> list[dict[str, Any]]:
        """Every action declared across all loaded plugin manifests, with its settings schema."""
        listed = self._action_runner.listing(
            lambda plugin, spec: self._icon_b64(plugin.plugin_dir, spec.icon or plugin.manifest.icon)
        )
        return [{**item, "plugin_builtin": self.is_builtin(item["plugin_uuid"])} for item in listed]

    def plugin_options(self, plugin_id: str, source: str) -> OptionsResult:
        """Ask a plugin for the options of a dynamic choice field it declares; blocks up to the runner's timeout."""
        if type(plugin_id) is not str:  # pylint: disable=unidiomatic-typecheck
            return OptionsResult(False, error="unknown plugin")  # exact types: a netref can claim to be a str
        if type(source) is not str or not is_valid_source(source):  # pylint: disable=unidiomatic-typecheck
            return OptionsResult(False, error="unknown options source")
        with self._lock:
            plugin = self._plugins.get(plugin_id)
        if plugin is None:
            return OptionsResult(False, error="unknown plugin")
        manifest = plugin.manifest
        if source not in declared_options_sources(manifest.global_settings, manifest.cards, manifest.actions):
            return OptionsResult(False, error="unknown options source")
        return self._options_runner.fetch(plugin_id, source)

    def run_action(self, call: ActionCall) -> None:
        """Run an action call in the background; a failure is announced as an ``action_failed`` event."""
        self._action_runner.run(call)

    def _resolve_args(self, spec: ActionSpec, args: Mapping[str, Any], device: str | None) -> dict[str, Any]:
        if device is not None:
            layout = self._device_layout(device)
            return deliver_settings(spec.settings, args, layout, self._expansion(page_variables(layout)))
        names = {name: "" for name in CORE_VARIABLE_NAMES}
        return deliver_settings(spec.settings, args, DeviceLayout(), VariableChain(names, self.variables.view()))

    def _on_action_failed(self, action_id: str, error: str) -> None:
        bus = self._event_bus
        if bus is not None:
            bus.push("action_failed", json.dumps({"action_id": action_id, "error": error}))

    def _register_runtime(self, plugin: InstalledPlugin) -> None:
        manifest = plugin.manifest
        with self._lock:
            known = manifest.id in self._hellos
        if manifest.runtime is None or known:
            return
        try:
            command = launch_command(plugin.plugin_dir, manifest.runtime, plugin.builtin)
        except ValueError as exc:
            self._record_failure(manifest.id, str(exc))
            return
        hello = {
            "protocol": PROTOCOL_VERSION,
            "decker_version": decker_version(),
            "plugin_id": manifest.id,
            "global_settings": self.global_settings(manifest.id),
        }
        with self._lock:
            self._hellos[manifest.id] = hello
        if self._sdk_path is None:
            self._record_failure(manifest.id, "plugin SDK is not staged")
            return
        env = launch_env(self._sdk_path)
        self._supervisor.register(manifest.id, command, plugin.plugin_dir, env, hello)

    def remove_plugin(self, plugin_id: str) -> None:
        """Stop a plugin, drop its live instances and forget it."""
        self._contributions.remove(plugin_id)
        with self._lock:
            self._plugins.pop(plugin_id, None)
            self._icon_cache.clear()
            self._drop_failure(plugin_id)
        self._recompose_icon_users()

    def _recompose_icon_users(self) -> None:
        """Redraw the keys showing a pack icon or a bound action's icon, since what is installed just changed."""
        with self._lock:
            users = [live for live in self._live.values() if _uses_pack_icon(live) or self._shows_action(live)]
        for live in users:
            self._recompose(live)

    def unregister_cards(self, plugin_id: str) -> None:
        """Stop a plugin's process and drop its live instances (the "cards" contribution handler)."""
        with self._lock:
            dropped = [c for c, live in self._live.items() if live.target.plugin_uuid == plugin_id]
            recognizers = [self._live.pop(c).recognizer for c in dropped]
        for recognizer in recognizers:
            recognizer.cancel()
        self._supervisor.unregister(plugin_id)
        with self._lock:
            self._hellos.pop(plugin_id, None)

    def _record_failure(self, plugin_id: str, error: str) -> None:
        with self._lock:
            self._failures.append(PluginLoadFailedEvent(plugin_id, error))

    def is_builtin(self, plugin_id: str) -> bool:
        """Whether the plugin ships inside Decker (it cannot be uninstalled or replaced)."""
        return plugin_id in self._builtin_ids

    def plugins(self) -> list[InstalledPlugin]:
        """Return every known plugin."""
        with self._lock:
            return list(self._plugins.values())

    def global_settings(self, plugin_id: str) -> dict[str, Any]:
        """Return the plugin-wide settings validated against the manifest schema, defaults filled in."""
        with self._lock:
            plugin = self._plugins.get(plugin_id)
        stored = self._settings_store.get_global_settings(plugin_id)
        return apply_schema(plugin.manifest.global_settings, stored) if plugin is not None else stored

    def update_global_settings(self, plugin_id: str, raw: dict[str, Any], notify: bool = True) -> dict[str, Any] | None:
        """Validate and persist plugin-wide settings, telling the running plugin; None for an unknown plugin."""
        with self._lock:
            plugin = self._plugins.get(plugin_id)
        if plugin is None:
            return None
        settings = apply_schema(plugin.manifest.global_settings, raw)
        self._settings_store.set_global_settings(plugin_id, settings)
        with self._lock:
            hello = self._hellos.get(plugin_id)
            if hello is not None:
                hello["global_settings"] = settings
        if notify:
            self._supervisor.send(plugin_id, Message("global_settings_changed", None, {"global_settings": settings}))
        return settings

    def delete_plugin_settings(self, plugin_id: str) -> None:
        """Forget every persisted setting of a plugin (used on uninstall)."""
        self._settings_store.delete_plugin(plugin_id)

    def live_targets(self, plugin_id: str) -> list[CardTarget]:
        """Return the card instances currently active for a plugin."""
        with self._lock:
            return [live.target for live in self._live.values() if live.target.plugin_uuid == plugin_id]

    def load_failures(self) -> list[PluginLoadFailedEvent]:
        """Return the plugins that failed to load or run."""
        with self._lock:
            return list(self._failures)

    def list_cards(self) -> list[dict[str, Any]]:
        """Return every card declared across all plugin manifests.

        Each entry carries icon_b64 (the card's icon, base64-encoded, or "" if none), its tooltip and
        its settings schema in the manifest JSON shape.
        """
        cards: list[dict[str, Any]] = []
        for plugin in self.plugins():
            manifest = plugin.manifest
            cards.extend(
                {
                    "plugin_uuid": manifest.id,
                    "plugin_name": manifest.name,
                    "plugin_builtin": self.is_builtin(manifest.id),
                    "card_uuid": spec.id,
                    "card_name": spec.name,
                    "icon_b64": self._icon_b64(plugin.plugin_dir, spec.icon or manifest.icon),
                    "tooltip": spec.tooltip,
                    "settings_schema": schema_to_json(spec.settings),
                    "appearance": dict(spec.appearance),
                    "bindings": {g: b.to_json() for g, b in spec.bindings.items()},
                }
                for spec in manifest.cards
            )
        return cards

    def get_key_display_states(self, device_id: str) -> list[dict[str, Any]]:
        """Return the display label and animation id (None when static) of every labelled or animated key."""
        state = self._device_manager.get_device_state(device_id) if self._device_manager is not None else None
        if state is None:
            return []
        labels = dict(state.get_all_labels())
        animation_ids = {k: spec.animation_id for k, spec in state.get_all_animations().items()}
        keys = list(labels) + [k for k in animation_ids if k not in labels]
        return [{"key_index": k, "label": labels.get(k, ""), "animation_id": animation_ids.get(k)} for k in keys]

    def store_settings(self, target: CardTarget, settings: dict[str, Any]) -> None:
        """Persist settings for a card instance so its plugin receives them on activation."""
        self._settings_store.set_settings(target.plugin_uuid, target.card_uuid, target.context_id, settings)

    def complete_settings(self, target: CardTarget, settings: dict[str, Any]) -> dict[str, Any]:
        """Return settings validated against the card's schema with defaults filled in."""
        spec = self._card_spec(target.plugin_uuid, target.card_uuid)
        return apply_schema(spec.settings, settings) if spec is not None else dict(settings)

    def appearance_for(self, target: CardTarget, stored: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return (resolved appearance, appearance defaults) for a card's stored appearance."""
        spec = self._card_spec(target.plugin_uuid, target.card_uuid)
        defaults = spec.appearance if spec is not None else {}
        return resolve_appearance(defaults, stored), resolve_appearance(defaults, {})

    def set_appearance(self, target: CardTarget, stored: dict[str, Any]) -> None:
        """Apply a new stored appearance to a live instance and redraw its key without asking the plugin."""
        with self._lock:
            live = self._live.get(target.context_id)
            if live is None:
                return
            live.appearance = dict(stored)
        self._recompose(live)

    def preview_image(self, target: CardTarget, stored: dict[str, Any]) -> bytes | None:
        """Compose a live key with a draft appearance, changing nothing; None if the instance is not live."""
        with self._lock:
            live = self._live.get(target.context_id)
            if live is None:
                return None
        draft = replace(live, appearance=dict(stored))
        try:
            return self._compose(draft).png
        except Exception:  # pylint: disable=broad-except
            logging.warning("Could not compose preview for context %s", target.context_id, exc_info=True)
            return None

    def activate_card(self, target: CardTarget, appearance: dict[str, Any] | None = None, draw: bool = True) -> None:
        """Tell the card's plugin that an instance appeared, starting the plugin if needed.

        ``draw=False`` skips the first, plugin-less draw of the key: the caller has just put the key's last image
        on the hardware, and drawing the bare background and title over it only makes the key flicker until the
        plugin's own image arrives. An animated face is still started (over that cached poster), because animations
        live in memory only and a reopened device or restarted server has none.
        """
        spec = self._card_spec(target.plugin_uuid, target.card_uuid)
        with self._lock:
            plugin = self._plugins.get(target.plugin_uuid)
        if plugin is None:
            self._mark_missing(target)
            return
        if spec is None or (plugin.manifest.runtime is None and not spec.headless):
            logging.warning("Cannot activate unknown card %s/%s", target.plugin_uuid, target.card_uuid)
            return
        with self._lock:
            previous = self._live.get(target.context_id)
        if previous is not None:
            self.deactivate_card(previous.target)
        self._clear_placeholder(target.device_id, target.key_index)
        stored = self._settings_store.get_settings(target.plugin_uuid, target.card_uuid, target.context_id)
        live = _Live(
            target=target,
            card=spec,
            recognizer=GestureRecognizer(self._gesture_emitter(target.context_id)),
            bindings={},
            settings=apply_schema(spec.settings, stored),
            appearance=dict(appearance or {}),
        )
        live.bindings = self._effective(live)
        with self._lock:
            self._live[target.context_id] = live
        self._start_live(live, draw)

    def _start_live(self, live: _Live, draw: bool) -> None:
        self._recompose(live, poster=draw)
        plugin_id = live.target.plugin_uuid
        if live.card.headless:
            return
        self._supervisor.acquire(plugin_id)
        if self._supervisor.is_failed(plugin_id):
            self._mark_failed(live)
        elif self._supervisor.is_ready(plugin_id):
            self._send_will_appear(live)

    def deactivate_card(self, target: CardTarget) -> None:
        """Tell the plugin the instance is gone and release its process."""
        with self._lock:
            live = self._live.pop(target.context_id, None)
        if live is None:
            return
        live.recognizer.cancel()
        if live.card.headless:
            return
        plugin_id = target.plugin_uuid
        if self._supervisor.is_ready(plugin_id):
            self._supervisor.send(plugin_id, Message("will_disappear", target.context_id))
        self._supervisor.release(plugin_id)

    def dispatch_key_down(self, target: CardTarget) -> None:
        """Forward the raw key down, then feed the press to the instance's gesture recognizer."""
        live = self._live.get(target.context_id)
        if live is None:
            return
        self._send_context(live, "key_down")
        live.recognizer.key_down(self._registered(live))

    def dispatch_key_up(self, target: CardTarget) -> None:
        """Forward the raw key up, then feed the release to the instance's gesture recognizer."""
        live = self._live.get(target.context_id)
        if live is None:
            return
        self._send_context(live, "key_up")
        live.recognizer.key_up()

    def dispatch_settings_changed(self, target: CardTarget) -> None:
        """Send the instance its new settings; re-saving settings retries a failed plugin."""
        plugin_id = target.plugin_uuid
        existing = self._live.get(target.context_id)
        if existing is not None and existing.card.headless:
            return
        if self._supervisor.is_failed(plugin_id):
            with self._lock:
                self._drop_failure(plugin_id)
            self._supervisor.reset_failure(plugin_id)
        live = self._live.get(target.context_id)
        if live is None:
            return
        stored = self._settings_store.get_settings(target.plugin_uuid, target.card_uuid, target.context_id)
        live.settings = apply_schema(live.card.settings, stored)
        self._send_context(live, "settings_changed", self._settings_payload(live, self._delivery(live)))

    def set_bindings(self, target: CardTarget, stored: Mapping[str, Any]) -> None:
        """Remember a card instance's stored gesture bindings; a live instance re-registers and is told."""
        with self._lock:
            self._stored_bindings[target.context_id] = dict(stored)
            live = self._live.get(target.context_id)
        if live is None:
            return
        effective = self._effective(live)
        if effective == live.bindings:
            return
        live.bindings = effective
        self._send_context(live, "settings_changed", self._settings_payload(live, self._delivery(live)))
        self._recompose(live)

    def validate_card_settings(self, target: CardTarget, settings: Mapping[str, Any]) -> dict[str, str]:
        """Field key -> message for every setting that breaks the card's schema."""
        spec = self._card_spec(target.plugin_uuid, target.card_uuid)
        return validate_settings(spec.settings, settings) if spec is not None else {}

    def validate_bindings(self, target: CardTarget, stored: Mapping[str, Any]) -> dict[str, str]:
        """Errors in stored bindings, keyed ``<gesture>.<arg>``, or ``<gesture>`` for a foreign plugin-scoped action."""
        errors: dict[str, str] = {}
        for gesture in GESTURES:
            if gesture not in stored:
                continue
            try:
                binding = parse_binding(stored[gesture], gesture)
            except ValueError as exc:
                errors[gesture] = str(exc)
                continue
            errors.update(self._binding_errors(target, gesture, binding))
        return errors

    def _binding_errors(self, target: CardTarget, gesture: str, binding: Binding) -> dict[str, str]:
        found = self._action_runner.scope_of(binding.action)
        spec = self._action_runner.spec(binding.action)
        if binding.is_nothing or found is None or spec is None:
            return {}
        owner, scope = found
        if scope != "global" and owner != target.plugin_uuid:
            return {gesture: "is another plugin's action and cannot be used on this card"}
        return {f"{gesture}.{key}": msg for key, msg in validate_settings(spec.settings, binding.args).items()}

    def _effective(self, live: _Live) -> dict[str, Binding]:
        with self._lock:
            stored = self._stored_bindings.get(live.target.context_id, {})
        return effective_bindings(live.card.bindings, normalize_stored_bindings(stored))

    def _allowed(self, live: _Live, binding: Binding) -> bool:
        if binding.is_nothing:
            return False
        found = self._action_runner.scope_of(binding.action)
        if found is None:
            return False
        owner, scope = found
        if scope == "global" or owner == live.target.plugin_uuid:
            return True
        key = (live.target.context_id, binding.action)
        if key not in self._warned_foreign:
            self._warned_foreign.add(key)
            logging.warning(
                "[%s] binding to %s, another plugin's action, is ignored", live.target.card_uuid, binding.action
            )
        return False

    def _registered(self, live: _Live) -> frozenset[Gesture]:
        return frozenset(Gesture(g) for g, binding in live.bindings.items() if self._allowed(live, binding))

    def _on_gesture(self, live: _Live, gesture: Gesture) -> None:
        binding = live.bindings.get(gesture.value)
        if binding is None or not self._allowed(live, binding):
            return
        call = ActionCall(
            binding.action,
            dict(binding.args),
            input=gesture.value,
            card=self._card_payload(live),
            device=live.target.device_id,
        )
        self._action_runner.run(call)

    def _card_payload(self, live: _Live) -> dict[str, Any]:
        target = live.target
        column, row = self._key_position(target)
        return {
            "context": target.context_id,
            "card": target.card_uuid,
            "plugin": target.plugin_uuid,
            "device": target.device_id,
            "key": {"index": target.key_index, "column": column, "row": row},
            "size": list(self._key_size(target)),
            "settings": self.delivered_settings(live),
            "last_paint": live.last_paint.isoformat() if live.last_paint is not None else None,
        }

    def _settings_payload(self, live: _Live, delivered: dict[str, Any]) -> dict[str, Any]:
        return {"settings": delivered, "bindings": self._delivered_bindings(live)}

    # ------------------------------------------------------------------
    # Server -> plugin helpers
    # ------------------------------------------------------------------

    def _recompose(self, live: _Live, poster: bool = True) -> None:
        """Compose the key face outside the lock, then write it if the instance is still active.

        Only the final sink write holds self._lock, so no hardware write can follow deactivate_card.
        With poster=False only an animation is started and nothing is written for a still image.
        """
        target = live.target
        try:
            composed = self._compose(live, poster)
            with self._lock:
                if self._live.get(target.context_id) is not live:
                    return  # gone
                written = write_face(self._sink, target, composed, poster)
        except Exception:  # pylint: disable=broad-except
            logging.warning("Could not compose key image for context %s", target.context_id, exc_info=True)
            return
        if poster or written:
            self._notify_state(target.device_id, target.key_index)

    def _compose(self, live: _Live, poster: bool = True) -> Composed:
        with self._lock:
            plugin = self._plugins.get(live.target.plugin_uuid)
            spec = live.card
            state = spec.states[live.state] if 0 <= live.state < len(spec.states) else None
            image, plugin_title, stored = live.plugin_image, live.plugin_title, dict(live.appearance)
            icon_ref, image_color = live.plugin_icon_ref, live.plugin_image_color
        if icon_ref is not None:
            image = self.icon_packs.read_ref(icon_ref)
        appearance = resolve_appearance(spec.appearance, stored)
        override = appearance["icon"]
        override_image = self.icon_packs.read_ref(override["ref"])
        if override_image is not None:  # the user's icon replaces whatever the plugin or its manifest shows
            image, icon_ref, image_color = override_image, override["ref"], override["color"]
        if image is None and state is not None and state.image:
            image = read_icon(plugin.plugin_dir, state.image) if plugin is not None else None
        title = plugin_title or (state.title if state is not None and state.title else "")
        face = self._face_action(live)
        if face is not None:
            action, icon = face
            image = image if image is not None else icon
            title = title or action.name
        return compose_key_face(
            self._expanded_appearance(live.target.device_id, appearance),
            image,
            title,
            image_color,
            PACK_ICON_SCALE if icon_ref is not None else 1.0,
            poster=poster,
            background_image=self._background_bytes(appearance),
        )

    def _shows_action(self, live: _Live) -> bool:
        press = live.bindings.get("press")
        return live.card.id == _BLANK_CARD and press is not None and not press.is_nothing

    def _face_action(self, live: _Live) -> tuple[ActionSpec, bytes | None] | None:
        press = live.bindings.get("press")
        if not self._shows_action(live) or press is None or not self._allowed(live, press):
            return None
        found = self._action_runner.scope_of(press.action)
        action = self._action_runner.spec(press.action)
        with self._lock:
            plugin = self._plugins.get(found[0]) if found is not None else None
        if action is None or plugin is None:
            return None
        return action, read_icon(plugin.plugin_dir, action.icon or plugin.manifest.icon)

    def _background_bytes(self, appearance: dict[str, Any]) -> bytes | None:
        background = appearance["background"]
        if background["type"] != "image":
            return None
        try:
            data = self.icon_packs.read_ref(background["image"]["ref"])
            color = background["image"].get("color")
            if data is not None and color:
                data = tint_svg(data, color)  # a no-op for anything but an SVG
            return intern_background(data)
        except Exception:  # pylint: disable=broad-except
            logging.debug("Could not read background image %r", background["image"]["ref"], exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Page variables and settings delivery
    # ------------------------------------------------------------------

    def _device_layout(self, device_id: str) -> DeviceLayout:
        if self._page_store is None or self._page_controller is None:
            return DeviceLayout()
        try:
            store = self._page_store
            profiles = tuple(store.list_profiles(device_id))
            by_profile = {p.profile_id: tuple(store.list_pages(device_id, p.profile_id)) for p in profiles}
            active_profile = int(store.active_profile_id(device_id))
            return DeviceLayout(
                pages=by_profile.get(active_profile, ()),
                active_page_id=str(self._page_controller.active_page_id(device_id)),
                profiles=profiles,
                active_profile_id=active_profile,
                pages_by_profile=MappingProxyType(by_profile),
            )
        except Exception:  # pylint: disable=broad-except
            logging.debug("Pages unavailable for %s", device_id, exc_info=True)
            return DeviceLayout()

    def page_variables(self, device_id: str) -> dict[str, str]:
        """Template variables for what the device shows (empty when pages are unavailable)."""
        return page_variables(self._device_layout(device_id))

    def delivered_settings(self, live: _Live) -> dict[str, Any]:
        """The settings a plugin receives: opted-in text expanded and page ids resolved; stored values stay raw."""
        layout = self._device_layout(live.target.device_id)
        return deliver_settings(live.card.settings, live.settings, layout, self._expansion(page_variables(layout)))

    def _expansion(self, page: dict[str, str]) -> Mapping[str, str] | None:
        # None (leave templated text raw) only when neither pages nor any declared variable could expand.
        if not page and not self.variables.has_declarations():
            return None
        return VariableChain(page, self.variables.view())

    def delivered_bindings(self, live: _Live) -> dict[str, Any]:
        """The bindings a plugin receives: each action's arguments resolved like settings; Do Nothing and
        uninstalled actions keep theirs raw."""
        layout = self._device_layout(live.target.device_id)
        variables = self._expansion(page_variables(layout))
        result: dict[str, Any] = {}
        for gesture, binding in live.bindings.items():
            sent = binding.to_json()
            spec = None if binding.is_nothing else self._action_runner.spec(binding.action)
            if spec is not None:
                sent["args"] = deliver_settings(spec.settings, binding.args, layout, variables)
            result[gesture] = sent
        return result

    def _delivered_bindings(self, live: _Live) -> dict[str, Any]:
        delivered = self.delivered_bindings(live)
        live.delivered_bindings = delivered
        return delivered

    def _has_dynamic_bindings(self, live: _Live) -> bool:
        specs = (self._action_runner.spec(b.action) for b in live.bindings.values() if not b.is_nothing)
        return any(spec is not None and has_dynamic_fields(spec.settings) for spec in specs)

    def _delivery(self, live: _Live) -> dict[str, Any]:
        delivered = self.delivered_settings(live)
        live.delivered = delivered
        return delivered

    def _expanded_appearance(self, device_id: str, appearance: dict[str, Any]) -> dict[str, Any]:
        variables = self._expansion(self.page_variables(device_id))
        title = appearance.get("title")
        if variables is not None and isinstance(title, dict) and isinstance(title.get("text"), str):
            return {**appearance, "title": {**title, "text": expand_template(title["text"], variables)}}
        return appearance

    def refresh_page_dependent(self, device_id: str) -> None:
        """Redraw and re-notify the device's keys whose title or settings depend on its pages."""
        with self._lock:
            lives = [live for live in self._live.values() if live.target.device_id == device_id]
        for live in lives:
            try:
                self._refresh_live(live)
            except Exception:  # pylint: disable=broad-except
                logging.warning("Refreshing key %s after a page change failed", live.target.context_id, exc_info=True)

    def _refresh_live(self, live: _Live) -> None:
        with self._lock:
            title = resolve_appearance(live.card.appearance, live.appearance).get("title", {})
        if "{" in str(title.get("text", "")):
            self._recompose(live)
        if has_dynamic_fields(live.card.settings) or self._has_dynamic_bindings(live):
            self._redeliver(live)

    def refresh_variable_users(self, names: frozenset[str]) -> None:
        """Redraw and re-notify every live key whose title or template settings use one of the names."""
        if not names:
            return
        with self._lock:
            lives = list(self._live.values())
        for live in lives:
            try:
                title, texts = self._template_uses(live)
                if title & names:
                    self._recompose(live)
                if texts & names:
                    self._redeliver(live)
            except Exception:  # pylint: disable=broad-except
                logging.warning(
                    "Refreshing key %s after a variable change failed", live.target.context_id, exc_info=True
                )

    def _template_uses(self, live: _Live) -> tuple[frozenset[str], frozenset[str]]:
        with self._lock:
            title = resolve_appearance(live.card.appearance, live.appearance).get("title", {})
            settings = dict(live.settings)
        text = title.get("text") if isinstance(title, dict) else None
        title_names = template_names(text) if isinstance(text, str) else frozenset()
        setting_names = frozenset().union(*map(template_names, templated_texts(live.card.settings, settings)))
        return title_names, setting_names

    def _redeliver(self, live: _Live) -> None:
        if live.card.headless or not self._supervisor.is_ready(live.target.plugin_uuid):
            return
        delivered = self.delivered_settings(live)
        if delivered != live.delivered or self.delivered_bindings(live) != live.delivered_bindings:
            live.delivered = delivered
            self._send_context(live, "settings_changed", self._settings_payload(live, delivered))

    def _notify_state(self, device_id: str, key_index: int) -> None:
        callback = self._state_changed_cb
        if callback is None:
            return
        try:
            callback(device_id, key_index)
        except Exception:  # pylint: disable=broad-except
            logging.warning("Key state notification failed", exc_info=True)

    def _send_context(self, live: _Live, event: str, payload: dict[str, Any] | None = None) -> None:
        if live.card.headless:  # no process knows this instance
            return
        self._supervisor.send(live.target.plugin_uuid, Message(event, live.target.context_id, payload or {}))

    def _send_will_appear(self, live: _Live) -> None:
        target = live.target
        column, row = self._key_position(target)
        payload = {
            "card": target.card_uuid,
            "device": target.device_id,
            "key": {"index": target.key_index, "column": column, "row": row},
            "size": list(self._key_size(target)),
            "settings": self._delivery(live),
            "bindings": self._delivered_bindings(live),
            "state": live.state,
        }
        self._supervisor.send(target.plugin_uuid, Message("will_appear", target.context_id, payload))

    def _key_position(self, target: CardTarget) -> tuple[int, int]:
        try:
            device = self._device_manager.open_device(target.device_id)
            position = device.key_position(target.key_index)
            if position is not None:
                return position
        except Exception:  # pylint: disable=broad-except
            logging.debug("No key layout for %s key %d", target.device_id, target.key_index)
        return target.key_index, 0

    def _key_size(self, target: CardTarget) -> tuple[int, int]:
        try:
            width, height = self._device_manager.open_device(target.device_id).key_size
            return int(width), int(height)
        except Exception:  # pylint: disable=broad-except
            return _DEFAULT_KEY_SIZE

    def _gesture_emitter(self, context_id: str) -> Callable[[Gesture], None]:
        def _emit(gesture: Gesture) -> None:
            live = self._live.get(context_id)
            if live is None:
                return
            self._on_gesture(live, gesture)

        return _emit

    def _device_ids(self) -> list[str]:
        if self._device_manager is None:
            return []
        try:
            return [info.id for info in self._device_manager.list_devices() if isinstance(info.id, str)]
        except Exception:  # pylint: disable=broad-except
            logging.debug("Could not list devices for plugin announcement", exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Plugin -> server handling
    # ------------------------------------------------------------------

    def _on_plugin_message(self, plugin_id: str, message: Message) -> None:
        try:
            self._route_message(plugin_id, message)
        except Exception:  # pylint: disable=broad-except
            logging.exception("[%s] failed to handle %s", plugin_id, message.event)

    def _route_message(self, plugin_id: str, message: Message) -> None:
        event = message.event
        if event == "ready":
            self._on_ready(plugin_id)
        elif event == "log":
            self._on_log(plugin_id, message.payload)
        elif event == "open_url":
            self._on_open_url(plugin_id, message.payload)
        elif event == "set_global_settings":
            self._on_set_global_settings(plugin_id, message.payload)
        elif event == "action_result":
            self._action_runner.on_result(plugin_id, message.payload)
        elif event == "call_action":
            self._action_runner.on_plugin_call(plugin_id, message.payload)
        elif event == "get_image":
            self._on_get_image(plugin_id, message.payload)
        elif event == "options_result":
            self._options_runner.on_result(plugin_id, message.payload)
        elif event in _VARIABLE_EVENTS:
            self._route_variable_message(plugin_id, message)
        elif event in _DEVICE_NAVIGATION and message.context is None:
            self._on_device_navigation(plugin_id, message)
        else:
            self._route_context_message(plugin_id, message)

    def _on_device_navigation(self, plugin_id: str, message: Message) -> None:
        device_id = message.payload.get("device")
        request = message.payload.get("request")
        if not isinstance(device_id, str) or not self._action_runner.may_drive(plugin_id, request, device_id):
            self._warn_refused(plugin_id, f"dropped {message.event[:80]} without a running call for its device")
            return
        if message.event == "switch_page":
            self._switch_page(device_id, message.payload, plugin_id)
        else:
            self._switch_profile(device_id, message.payload, plugin_id)

    def _warn_refused(self, plugin_id: str, text: str) -> None:
        if plugin_id in self._drive_warned:
            logging.debug("[%s] %s", plugin_id, text)
        else:
            self._drive_warned.add(plugin_id)
            logging.warning("[%s] %s", plugin_id, text)

    def _route_variable_message(self, plugin_id: str, message: Message) -> None:
        if message.event == "variable_value":
            self._variable_runner.on_value(plugin_id, message.payload)
            return
        name = message.payload.get("name")
        if not isinstance(name, str):
            logging.debug("[%s] dropped %s without a name", plugin_id, message.event)
        elif message.event == "unregister_variable":
            self.variables.unregister(plugin_id, name)
        elif message.event == "register_variable" and not self.variables.register(plugin_id, name):
            logging.warning(
                "[%s] register_variable(%r) rejected: not inside one of its patterns, taken, or over the limit",
                plugin_id,
                name[:80],
            )
        elif message.event == "variable_updated" and name not in self.variables.registered_names(plugin_id):
            logging.debug("[%s] ignored variable_updated for %r, which it does not own", plugin_id, name[:80])
        else:
            self._variable_runner.ask(plugin_id, name)

    def _route_context_message(self, plugin_id: str, message: Message) -> None:
        with self._lock:
            live = self._live.get(message.context or "")
            if live is None:
                logging.debug("[%s] dropped %s for inactive context %s", plugin_id, message.event, message.context)
                return
            if live.target.plugin_uuid != plugin_id and not self._foreign_allowed(plugin_id, message):
                return
            handled, recompose = self._apply_sink_event(live, message)
        if recompose:
            self._recompose(live)
        if handled:
            return
        if message.event == "set_settings":
            self._on_set_settings(live, message.payload)
        elif message.event == "set_gestures":
            logging.debug("[%s] set_gestures is ignored: gestures come from bindings", plugin_id)
        elif message.event == "switch_page":
            self._on_switch_page(live, message.payload)
        elif message.event == "switch_profile":
            self._on_switch_profile(live, message.payload)

    def _foreign_allowed(self, plugin_id: str, message: Message) -> bool:
        context = message.context or ""
        if message.event in _FOREIGN_EVENTS and self._action_runner.may_touch(
            plugin_id, message.payload.get("request"), context
        ):
            return True
        event = message.event[:80]
        if plugin_id in self._foreign_warned:
            logging.debug("[%s] dropped %s for a card it does not own: %s", plugin_id, event, context[:80])
        else:
            self._foreign_warned.add(plugin_id)
            logging.warning("[%s] dropped %s for a card it does not own (%s)", plugin_id, event, context[:80])
        return False

    def _apply_sink_event(self, live: _Live, message: Message) -> tuple[bool, bool]:
        """Update the instance (caller holds the lock); returns (handled, needs a recompose)."""
        target, payload = live.target, message.payload
        if message.event == "set_image":
            recompose = self._on_set_image(live, payload)
            if recompose:
                live.last_paint = datetime.now(timezone.utc)
            return True, recompose
        if message.event == "set_title":
            live.plugin_title = str(payload.get("title", ""))
            self._sink.set_title(target.device_id, target.key_index, live.plugin_title)
            return True, True
        if message.event == "set_state":
            state = payload.get("state")
            live.state = state if isinstance(state, int) else 0
            self._sink.set_state(target.device_id, target.key_index, live.state)
            return True, True
        if message.event == "show_alert":
            self._sink.show_alert(target.device_id, target.key_index)
            return True, False
        if message.event == "show_ok":
            self._sink.show_ok(target.device_id, target.key_index)
            return True, False
        return False, False

    def _on_device_opened(self, device_id: str) -> None:
        self._broadcast_device("device_connected", device_id)

    def _on_device_closed(self, device_id: str) -> None:
        self._broadcast_device("device_disconnected", device_id)

    def _broadcast_device(self, event: str, device_id: str) -> None:
        # A plugin that is not ready yet learns the live devices from the burst in _on_ready.
        for plugin_id in self._supervisor.running_ids():
            self._supervisor.send_if_ready(plugin_id, Message(event, payload={"device": device_id}))

    def _on_ready(self, plugin_id: str) -> None:
        self._variable_runner.send_snapshot(plugin_id)
        for name in self.variables.registered_names(plugin_id):
            self._variable_runner.ask(plugin_id, name)
        with self._lock:
            lives = [l for l in self._live.values() if l.target.plugin_uuid == plugin_id and not l.card.headless]
        for live in lives:
            self._clear_placeholder(live.target.device_id, live.target.key_index)
            self._send_will_appear(live)
        for device_id in self._device_ids():
            self._supervisor.send(plugin_id, Message("device_connected", payload={"device": device_id}))

    def _on_set_image(self, live: _Live, payload: dict[str, Any]) -> bool:
        who = live.target.plugin_uuid
        if "icon" in payload:
            if payload.get("image") is not None:
                logging.warning("[%s] set_image with both image and icon dropped", who)
                return False
            return self._set_icon(live, payload)
        image = payload.get("image")
        live.plugin_icon_ref = live.plugin_image_color = None
        if image is None:
            live.plugin_image = None
            return True
        try:
            data = base64.b64decode(str(image), validate=True)
        except (binascii.Error, ValueError):
            logging.warning("[%s] set_image with invalid base64 dropped", who)
            return False
        live.plugin_image = data
        return True

    def _on_get_image(self, plugin_id: str, payload: dict[str, Any]) -> None:
        request, context, call = payload.get("request"), payload.get("context"), payload.get("call")
        if not isinstance(request, str):
            logging.debug("[%s] dropped get_image without a string request", plugin_id)
            return
        image = None
        if isinstance(context, str) and (call is None or isinstance(call, str)):
            image = self._plugin_layer_png(plugin_id, context, call)
        reply = {"request": request, "image": image, "format": "png"}
        self._supervisor.send(plugin_id, Message("image_result", None, reply))

    def _plugin_layer_png(self, plugin_id: str, context: str, call: str | None) -> str | None:
        """The key's plugin image layer as base64 PNG at key size; None when absent, refused or unrenderable."""
        with self._lock:
            live = self._live.get(context)
            if live is None:
                return None
            if live.target.plugin_uuid != plugin_id and not (
                call is not None and self._action_runner.may_touch(plugin_id, call, context)
            ):
                return None
            data, icon_ref, color = live.plugin_image, live.plugin_icon_ref, live.plugin_image_color
            size = self._key_size(live.target)
        try:
            if icon_ref is not None:
                data = self.icon_packs.read_ref(icon_ref)
            rendered = self._render_layer(data, size, color, PACK_ICON_SCALE if icon_ref is not None else 1.0)
            if rendered is None:
                return None
            buffer = io.BytesIO()
            rendered.save(buffer, format="PNG")
            return base64.b64encode(buffer.getvalue()).decode("ascii")
        except Exception:  # pylint: disable=broad-except
            with self._lock:
                first = plugin_id not in self._render_warned
                self._render_warned.add(plugin_id)
            if first:
                logging.warning("[%s] get_image could not render %s", plugin_id, context[:80], exc_info=True)
            else:
                logging.debug("[%s] get_image could not render %s", plugin_id, context[:80], exc_info=True)
            return None

    @staticmethod
    def _render_layer(data: bytes | None, size: tuple[int, int], color: str | None, scale: float) -> Image.Image | None:
        if data is None:
            return None
        inner = (inner_size(size[0], scale), inner_size(size[1], scale))
        layer = AssetRenderer().render_bytes(data, inner[0], inner[1], color)
        if layer is None:
            return None
        canvas = Image.new("RGBA", size, (0, 0, 0, 0))
        canvas.alpha_composite(layer, dest=((size[0] - inner[0]) // 2, (size[1] - inner[1]) // 2))
        return canvas

    def _set_icon(self, live: _Live, payload: dict[str, Any]) -> bool:
        who = live.target.plugin_uuid
        ref, color = payload.get("icon"), payload.get("color")
        live.plugin_image = None
        live.plugin_icon_ref = live.plugin_image_color = None
        if self.icon_packs.read_ref(ref) is None:
            logging.warning("[%s] set_image icon %r is not a readable pack icon; image cleared", who, ref)
            return True
        live.plugin_icon_ref = ref
        if color is not None and not is_color(color):
            logging.warning("[%s] set_image color %r is not #rrggbb[aa]; icon left untinted", who, color)
        elif color is not None:
            live.plugin_image_color = color
        return True

    def _on_set_settings(self, live: _Live, payload: dict[str, Any]) -> None:
        raw = payload.get("settings")
        if not isinstance(raw, dict):
            logging.warning("[%s] set_settings without a settings object dropped", live.target.plugin_uuid)
            return
        incoming = keep_raw_templates(live.card.settings, raw, live.delivered, live.settings)
        incoming = stored_profile_ids(live.card.settings, incoming, live.delivered, live.settings)
        merged = {**live.settings, **stored_page_ids(live.card.settings, incoming)}
        settings = apply_schema(live.card.settings, merged)
        live.settings = settings
        live.delivered = self.delivered_settings(live)
        self.store_settings(live.target, settings)
        if self.settings_persister is not None:
            self.settings_persister(live.target, settings)

    def _on_switch_page(self, live: _Live, payload: dict[str, Any]) -> None:
        """Switch the key's device; runs without the manager lock because a switch deactivates cards."""
        self._switch_page(live.target.device_id, payload, live.target.plugin_uuid)

    def _on_switch_profile(self, live: _Live, payload: dict[str, Any]) -> None:
        """Switch the key's device to another profile; runs without the manager lock like a page switch."""
        self._switch_profile(live.target.device_id, payload, live.target.plugin_uuid)

    def _switch_page(self, device_id: str, payload: dict[str, Any], who: str) -> None:
        if self._page_controller is None:
            return
        target = resolve_switch_target(self._device_layout(device_id), payload)
        if target is None:
            logging.info("[%s] switch_page %r has no target", who, payload)
            return
        self._page_controller.activate(device_id, target)

    def _switch_profile(self, device_id: str, payload: dict[str, Any], who: str) -> None:
        if self._page_controller is None:
            return
        target = resolve_profile_switch_target(self._device_layout(device_id), payload)
        if target is None:
            logging.info("[%s] switch_profile %r has no target", who, payload)
            return
        self._page_controller.activate_profile(device_id, target)

    def _on_set_global_settings(self, plugin_id: str, payload: dict[str, Any]) -> None:
        raw = payload.get("settings")
        if not isinstance(raw, dict):
            logging.warning("[%s] set_global_settings without a settings object dropped", plugin_id)
            return
        self.update_global_settings(plugin_id, raw, notify=False)

    @staticmethod
    def _on_log(plugin_id: str, payload: dict[str, Any]) -> None:
        levels = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}
        level = levels.get(str(payload.get("level", "info")).lower(), logging.INFO)
        logging.log(level, "[%s] %s", plugin_id, payload.get("message", ""))

    def _on_open_url(self, plugin_id: str, payload: dict[str, Any]) -> None:
        url = payload.get("url")
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme.lower() not in _OPEN_URL_SCHEMES or not parsed.netloc:
            logging.warning("[%s] open_url with unsupported url dropped: %r", plugin_id, url)
            return
        self._push("open_url", {"plugin_id": plugin_id, "url": url})

    def _on_plugin_failed(self, plugin_id: str, reason: str) -> None:
        logging.error("Plugin %s failed: %s", plugin_id, reason)
        self._variable_runner.plugin_stopped(plugin_id)
        self._options_runner.plugin_stopped(plugin_id)
        event = PluginLoadFailedEvent(plugin_uuid=plugin_id, error=reason)
        with self._lock:
            self._drop_failure(plugin_id)
            self._failures.append(event)
            lives = [l for l in self._live.values() if l.target.plugin_uuid == plugin_id and not l.card.headless]
        for live in lives:
            self._mark_failed(live)
        self._push("plugin_failed", {"plugin_id": plugin_id, "error": reason})

    def _mark_failed(self, live: _Live) -> None:
        target = live.target
        with self._lock:
            if self._live.get(target.context_id) is not live:
                return
            self._sink.clear_image(target.device_id, target.key_index)
            self._sink.set_title(target.device_id, target.key_index, _FAILED_LABEL)
            self._placeholders.add((target.device_id, target.key_index))
        self._notify_state(target.device_id, target.key_index)

    def _mark_missing(self, target: CardTarget) -> None:
        with self._lock:
            failed = any(f.plugin_uuid == target.plugin_uuid for f in self._failures)
        label = _FAILED_LABEL if failed else _MISSING_LABEL
        logging.warning(
            "Plugin %s is not %s; key %d keeps its assignment",
            target.plugin_uuid,
            "loaded" if failed else "installed",
            target.key_index,
        )
        with self._lock:
            self._sink.clear_image(target.device_id, target.key_index)
            self._sink.set_title(target.device_id, target.key_index, label)
            self._placeholders.add((target.device_id, target.key_index))
        self._notify_state(target.device_id, target.key_index)

    def _clear_placeholder(self, device_id: str, key_index: int) -> None:
        with self._lock:
            if (device_id, key_index) not in self._placeholders:
                return
            self._placeholders.discard((device_id, key_index))
            self._sink.set_title(device_id, key_index, "")

    def _drop_failure(self, plugin_id: str) -> None:
        self._failures = [f for f in self._failures if f.plugin_uuid != plugin_id]

    def _push(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._event_bus is not None:
            self._event_bus.push(event_type, json.dumps(payload))

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _card_spec(self, plugin_id: str, card_id: str) -> CardSpec | None:
        with self._lock:
            plugin = self._plugins.get(plugin_id)
        if plugin is None:
            return None
        return next((a for a in plugin.manifest.cards if a.id == card_id), None)

    @staticmethod
    def _make_settings_persister(context: "ServerContext") -> Callable[[CardTarget, dict[str, Any]], None]:
        def _persist(target: CardTarget, settings: dict[str, Any]) -> None:
            page_id = context.page_controller.active_page_id(target.device_id)
            context.key_assignment_store.update_settings(target.device_id, page_id, target.key_index, settings)

        return _persist

    def _make_state_changed_cb(self, context: "ServerContext") -> _StateChangedCallback:
        def _on_state_changed(device_id: str, key_index: int) -> None:
            device_state = context.device_manager.get_device_state(device_id)
            if device_state is None:
                return
            per_key = device_state.get_key_state(key_index)
            image_b64 = base64.b64encode(per_key.image).decode("ascii") if per_key.image else None
            animation = device_state.get_key_animation(key_index)
            appearance, defaults = self._key_appearance(device_id, key_index)
            context.event_bus.push(
                "key_state_changed",
                json.dumps(
                    {
                        "device_id": device_id,
                        "key_index": key_index,
                        "image_b64": image_b64,
                        "animation_id": animation.animation_id if animation is not None else None,
                        "label": per_key.label,
                        "appearance": appearance,
                        "appearance_defaults": defaults,
                    }
                ),
            )

        return _on_state_changed

    def _key_appearance(self, device_id: str, key_index: int) -> tuple[dict[str, Any], dict[str, Any]]:
        with self._lock:
            live = next(
                (
                    item
                    for item in self._live.values()
                    if item.target.device_id == device_id and item.target.key_index == key_index
                ),
                None,
            )
            if live is None:
                return {}, {}
            return resolve_appearance(live.card.appearance, live.appearance), resolve_appearance(
                live.card.appearance, {}
            )

    def _set_key_label(self, device_id: str, key_index: int, label: str) -> None:
        if self._device_manager is None:
            return
        state = self._device_manager.get_device_state(device_id)
        if state is not None:
            state.update_key_label(key_index, label)

    def _icon_b64(self, plugin_dir: Path, icon: str | None) -> str:
        key = (plugin_dir, icon)
        if key not in self._icon_cache:
            data = read_icon(plugin_dir, icon)
            self._icon_cache[key] = base64.b64encode(data).decode("ascii") if data else ""
        return self._icon_cache[key]
