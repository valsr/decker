"""Owns plugin discovery and runs plugin processes through the supervisor."""

import base64
import binascii
import json
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from decker.asset.iconpack import IconPackRegistry
from decker.asset.tint import tint_svg
from decker.common import config
from decker.common.manager import Manager
from decker.common.platforms import detect_platform, platform_supports
from decker.common.slots import SLOT_NAMES, normalize_slots
from decker.common.template import expand_template, template_names
from decker.common.text_style import is_color
from decker.common.version import decker_version
from decker.plugin.environment import stage_sdk
from decker.plugin.events import PluginLoadFailedEvent
from decker.plugin.gestures import Gesture, GestureRecognizer
from decker.plugin.icons import read_icon
from decker.plugin.loader import (
    InstalledPlugin,
    PluginLoader,
    recover_interrupted_installs,
)
from decker.plugin.manifest import CardSpec, PluginManifest
from decker.plugin.protocol import PROTOCOL_VERSION, Message
from decker.plugin.runtime.launcher import launch_command, launch_env
from decker.plugin.runtime.process import PluginProcess
from decker.plugin.runtime.supervisor import PluginSupervisor, SupervisorPolicy
from decker.plugin.settings_schema import (
    SettingField,
    apply_schema,
    schema_to_json,
)
from decker.plugin.settings_store import PluginSettingsStore
from decker.plugin.target import CardTarget
from decker.server.actions import ActionRunner, bound_action
from decker.server.appearance import (
    PACK_ICON_SCALE,
    Composed,
    compose_key_face,
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
from decker.server.switch import SwitchController
from decker.server.variable_runner import VariableRunner
from decker.server.variables import VariableConflict, VariableStore

if TYPE_CHECKING:
    from decker.server.server_context import ServerContext

_DEFAULT_MEMORY_LIMIT = 256 * 1024 * 1024
_FAILED_LABEL = "Plugin failed"
_MISSING_LABEL = "Plugin missing"
_OPEN_URL_SCHEMES = ("http", "https")


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
    gestures: frozenset[Gesture]
    settings: dict[str, Any]
    appearance: dict[str, Any] = field(default_factory=dict)
    plugin_image: bytes | None = None
    plugin_icon_ref: str | None = None  # resolved at compose time, so an uninstalled pack drops the image
    plugin_image_color: str | None = None
    plugin_title: str = ""
    state: int = 0
    delivered: dict[str, Any] = field(default_factory=dict)
    parent: str | None = None  # the Switch's context id, for the card a Switch shows


def _where(target: CardTarget) -> tuple[str, int]:
    return target.device_id, target.key_index


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


def _gestures_from(names: Any) -> frozenset[Gesture]:
    if not isinstance(names, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(Gesture(n) for n in names if n in {g.value for g in Gesture})


def _declares_source(manifest: PluginManifest, source: str) -> bool:
    entries = [*manifest.global_settings]
    for card in manifest.cards:
        entries.extend(card.settings)
    for action in manifest.actions:
        entries.extend(action.settings)
    return any(isinstance(e, SettingField) and e.extra.get("options_source") == source for e in entries)


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
        self._lock = threading.RLock()
        # serialises child swaps; taken before self._lock, never inside it. Composing under it relies on
        # PageController.active_page_id staying lock-free (page switches hold the controller lock while activating cards).
        self._switch_lock = threading.RLock()
        self._switches = SwitchController()
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
        self._action_runner = ActionRunner(self._supervisor, self._on_action_failed)
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
        self._reshow_switches()

    def register_cards(self, plugin: InstalledPlugin) -> None:
        """Register a plugin's cards with the supervisor (the "cards" contribution handler)."""
        self._register_runtime(plugin)

    def register_actions(self, plugin: InstalledPlugin) -> None:
        """Make a plugin's actions runnable (the "actions" contribution handler)."""
        self._register_runtime(plugin)
        self._action_runner.register(plugin)

    def unregister_actions(self, plugin_id: str) -> None:
        """Forget a plugin's actions and stop its process (the "actions" contribution handler)."""
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
        self._options_runner.plugin_stopped(plugin_id)
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
        with self._lock:
            plugin = self._plugins.get(plugin_id)
        if plugin is None:
            return OptionsResult(False, error="unknown plugin")
        if not _declares_source(plugin.manifest, source):
            return OptionsResult(False, error="unknown options source")
        return self._options_runner.fetch(plugin_id, source)

    def run_action(self, action_id: str, settings: dict[str, Any]) -> None:
        """Run an action in the background; a failure is announced as an ``action_failed`` event."""
        self._action_runner.run(action_id, settings)

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
            command = launch_command(plugin.plugin_dir, manifest.runtime)
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
        self._reshow_switches()

    def _recompose_icon_users(self) -> None:
        """Redraw the keys showing a pack icon, since the set of installed packs just changed."""
        with self._lock:
            users = [live for live in self._live.values() if _uses_pack_icon(live)]
        for live in users:
            self._recompose(live)

    def unregister_cards(self, plugin_id: str) -> None:
        """Stop a plugin's process and drop its live instances (the "cards" contribution handler)."""
        with self._lock:
            dropped = [c for c, live in self._live.items() if live.target.plugin_uuid == plugin_id]
            recognizers = [self._live.pop(c).recognizer for c in dropped]
            orphans = [live.target for live in self._live.values() if live.parent in dropped]
        for recognizer in recognizers:
            recognizer.cancel()
        for target in orphans:
            self.deactivate_card(target)
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
                    "action": spec.action or "",  # the action a preset card runs, "" for any other card
                    "icon_b64": self._icon_b64(plugin.plugin_dir, spec.icon or manifest.icon),
                    "tooltip": spec.tooltip,
                    "settings_schema": schema_to_json(spec.settings),
                    "appearance": dict(spec.appearance),
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
        draft = self._preview_subject(live, stored)
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
            gestures=_gestures_from(sorted(spec.gestures)),
            settings=apply_schema(spec.settings, stored),
            appearance=dict(appearance or {}),
        )
        with self._lock:
            self._live[target.context_id] = live
        if spec.switch:
            self._show_child(live, draw)
        else:
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
        if live.card.switch:
            self._hide_child(live)
        if live.card.headless:
            return
        plugin_id = target.plugin_uuid
        if self._supervisor.is_ready(plugin_id):
            self._supervisor.send(plugin_id, Message("will_disappear", target.context_id))
        self._supervisor.release(plugin_id)

    def dispatch_key_down(self, target: CardTarget) -> None:
        """Forward the raw key down, then feed the press to the instance's gesture recognizer."""
        live = self._live.get(target.context_id)
        if live is None or live.card.static or live.parent is not None:
            return
        self._send_context(live, "key_down")
        live.recognizer.key_down(live.gestures)

    def dispatch_key_up(self, target: CardTarget) -> None:
        """Forward the raw key up, then feed the release to the instance's gesture recognizer."""
        live = self._live.get(target.context_id)
        if live is None or live.card.static or live.parent is not None:
            return
        self._send_context(live, "key_up")
        live.recognizer.key_up()

    def dispatch_settings_changed(self, target: CardTarget) -> None:
        """Send the instance its new settings; re-saving settings retries a failed plugin."""
        plugin_id = target.plugin_uuid
        existing = self._live.get(target.context_id)
        if existing is not None and existing.card.switch:
            stored = self._settings_store.get_settings(target.plugin_uuid, target.card_uuid, target.context_id)
            existing.settings = apply_schema(existing.card.settings, stored)
            self._show_child(existing)
            return
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
        self._send_context(live, "settings_changed", {"settings": self._delivery(live)})

    # ------------------------------------------------------------------
    # Switch cards: the shown child card and slot gestures
    # ------------------------------------------------------------------

    def _child_of(self, live: _Live) -> _Live | None:
        if not live.card.switch:
            return None
        context_id = live.target.context_id
        with self._lock:
            return next((self._live[c] for c in (f"{context_id}:{s}" for s in SLOT_NAMES) if c in self._live), None)

    def _preview_subject(self, live: _Live, stored: dict[str, Any]) -> _Live:
        """The throwaway instance a preview composes: a Switch's fresh-context card, else the draft-styled card."""
        draft = replace(live, appearance=dict(stored))
        if not live.card.switch:
            return draft
        # Draft slots win; otherwise the persisted ones. Never the live child or the controller's remembered slot.
        raw = stored.get("slots")
        if raw is None:
            persisted = self._settings_store.get_settings(
                live.target.plugin_uuid, live.target.card_uuid, live.target.context_id
            )
            raw = persisted.get("slots")
        slots = self._effective_slots({"slots": normalize_slots(raw)})
        name = SwitchController().shown_slot(live.target.context_id, slots)
        spec = None if name is None else self._child_spec(slots[name])
        if name is None or spec is None:
            return draft
        slot = slots[name]
        target = CardTarget(slot["plugin"], slot["card"], f"{live.target.context_id}:{name}", *_where(live.target))
        return _Live(
            target=target,
            card=spec,
            recognizer=live.recognizer,
            gestures=frozenset(),
            settings=apply_schema(spec.settings, dict(slot["settings"])),
            appearance=dict(slot["appearance"]),
            parent=live.target.context_id,
        )

    def _child_spec(self, slot: Mapping[str, Any]) -> CardSpec | None:
        with self._lock:
            plugin = self._plugins.get(str(slot.get("plugin")))
        spec = self._card_spec(str(slot.get("plugin")), str(slot.get("card")))
        if plugin is None or spec is None or spec.switch or (plugin.manifest.runtime is None and not spec.headless):
            return None
        return spec

    def _slot_installed(self, slot: Any) -> bool:
        if not isinstance(slot, Mapping):
            return False
        if slot.get("kind") == "action":
            return self._action_runner.spec(str(slot.get("action"))) is not None
        return self._child_spec(slot) is not None

    def _effective_slots(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        # Uninstalled cards and actions read as empty here, but the stored value keeps them for a reinstall.
        slots = settings.get("slots")
        if not isinstance(slots, Mapping):
            return {}
        return {name: slot if self._slot_installed(slot) else None for name, slot in slots.items()}

    def _show_child(self, live: _Live, draw: bool = True) -> None:
        context_id = live.target.context_id
        with self._switch_lock:
            with self._lock:
                if self._live.get(context_id) is not live:
                    return
                current = self._child_of(live)
            slots = self._effective_slots(live.settings)
            shown = self._switches.shown_slot(context_id, slots)
            if current is not None and shown is not None and self._update_child_in_place(live, current, shown, slots):
                live.gestures = self._switches.gestures(slots)
                return
            if current is not None:
                self.deactivate_card(current.target)
            self._clear_placeholder(live.target.device_id, live.target.key_index)
            live.gestures = self._switches.gestures(slots)
            if shown is None or not self._activate_child(live, shown, slots[shown], draw):
                self._recompose(live, poster=draw)

    def _update_child_in_place(self, parent: _Live, current: _Live, shown: str, slots: Mapping[str, Any]) -> bool:
        slot = slots[shown]
        target = current.target
        if (
            target.context_id != f"{parent.target.context_id}:{shown}"
            or (target.plugin_uuid, target.card_uuid) != (slot["plugin"], slot["card"])
            or self._supervisor.is_failed(target.plugin_uuid)
        ):
            return False
        current.settings = apply_schema(current.card.settings, dict(slot["settings"]))
        current.appearance = dict(slot["appearance"])
        self._send_context(current, "settings_changed", {"settings": self._delivery(current)})
        self._recompose(current)
        return True

    def _activate_child(self, parent: _Live, name: str, slot: Mapping[str, Any], draw: bool) -> bool:
        spec = self._child_spec(slot)
        if spec is None:
            return False
        where = parent.target
        target = CardTarget(
            slot["plugin"], slot["card"], f"{where.context_id}:{name}", where.device_id, where.key_index
        )
        child = _Live(
            target=target,
            card=spec,
            recognizer=GestureRecognizer(self._gesture_emitter(target.context_id)),
            gestures=frozenset(),
            settings=apply_schema(spec.settings, dict(slot["settings"])),
            appearance=dict(slot["appearance"]),
            parent=where.context_id,
        )
        with self._lock:
            if self._live.get(where.context_id) is not parent:
                return True  # the Switch went away meanwhile: nothing to show
            self._live[target.context_id] = child
        self._start_live(child, draw)
        return True

    def _hide_child(self, parent: _Live) -> None:
        with self._switch_lock:
            child = self._child_of(parent)
            if child is not None:
                self.deactivate_card(child.target)

    def _on_switch_gesture(self, live: _Live, gesture: Gesture) -> None:
        outcome = self._switches.handle(live.target.context_id, self._effective_slots(live.settings), gesture)
        if outcome.action is not None:
            self.run_action(*outcome.action)
        if outcome.show is not None:
            self._show_child(live)

    def _reshow_switches(self) -> None:
        with self._lock:
            switches = [live for live in self._live.values() if live.card.switch]
        for live in switches:
            try:
                slots = self._effective_slots(live.settings)
                live.gestures = self._switches.gestures(slots)
                child = self._child_of(live)
                wanted = self._switches.shown_slot(live.target.context_id, slots)
                if child is None or child.target.context_id != f"{live.target.context_id}:{wanted}":
                    self._show_child(live)
            except Exception:  # pylint: disable=broad-except
                logging.warning("Refreshing Switch %s failed", live.target.context_id, exc_info=True)

    def _write_back_slot(self, child: _Live, settings: dict[str, Any]) -> None:
        name = child.target.context_id.rsplit(":", 1)[1]
        with self._lock:
            parent = self._live.get(child.parent or "")
            slots = dict(parent.settings.get("slots") or {}) if parent is not None else {}
            slot = slots.get(name)
            if parent is None or not isinstance(slot, Mapping):
                return
            if (slot.get("plugin"), slot.get("card")) != (child.target.plugin_uuid, child.target.card_uuid):
                return
            slots[name] = {**slot, "settings": settings}
            parent.settings = stored = {**parent.settings, "slots": slots}
        self.store_settings(parent.target, stored)
        if self.settings_persister is not None:
            self.settings_persister(parent.target, stored)

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
                if self._live.get(target.context_id) is not live or self._child_of(live) is not None:
                    return  # gone, or a Switch whose shown card draws the key
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
        return compose_key_face(
            self._expanded_appearance(live.target.device_id, appearance),
            image,
            title,
            image_color,
            PACK_ICON_SCALE if icon_ref is not None else 1.0,
            poster=poster,
            background_image=self._background_bytes(appearance),
        )

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
        if has_dynamic_fields(live.card.settings):
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
        if delivered != live.delivered:
            live.delivered = delivered
            self._send_context(live, "settings_changed", {"settings": delivered})

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
            "settings": self._delivery(live),
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

    def _gesture_emitter(self, context_id: str) -> Callable[[Gesture], None]:
        def _emit(gesture: Gesture) -> None:
            live = self._live.get(context_id)
            if live is None:
                return
            if live.card.switch:
                self._on_switch_gesture(live, gesture)
                return
            if live.card.bound:  # a press runs the card's action; the card has no process to tell
                if gesture == Gesture.PRESS:
                    self._run_bound(live)
                return
            self._send_context(live, gesture.value)

        return _emit

    def _run_bound(self, live: _Live) -> None:
        binding = bound_action(live.card, live.settings)
        if binding is None:
            self._on_action_failed("", "no action is chosen on this key")
            return
        self.run_action(*binding)

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
        elif event == "options_result":
            self._options_runner.on_result(plugin_id, message.payload)
        elif event in _VARIABLE_EVENTS:
            self._route_variable_message(plugin_id, message)
        else:
            self._route_context_message(plugin_id, message)

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
            if live is None or live.target.plugin_uuid != plugin_id:
                logging.debug("[%s] dropped %s for inactive context %s", plugin_id, message.event, message.context)
                return
            handled, recompose = self._apply_sink_event(live, message)
        if recompose:
            self._recompose(live)
        if handled:
            return
        if message.event == "set_settings":
            self._on_set_settings(live, message.payload)
        elif message.event == "set_gestures":
            self._on_set_gestures(live, message.payload)
        elif message.event == "switch_page":
            self._on_switch_page(live, message.payload)
        elif message.event == "switch_profile":
            self._on_switch_profile(live, message.payload)

    def _apply_sink_event(self, live: _Live, message: Message) -> tuple[bool, bool]:
        """Update the instance (caller holds the lock); returns (handled, needs a recompose)."""
        target, payload = live.target, message.payload
        if message.event == "set_image":
            return True, self._on_set_image(live, payload)
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
        if live.parent is not None:
            self._write_back_slot(live, settings)
            return
        self.store_settings(live.target, settings)
        if self.settings_persister is not None:
            self.settings_persister(live.target, settings)

    def _on_switch_page(self, live: _Live, payload: dict[str, Any]) -> None:
        """Switch the key's device; runs without the manager lock because a switch deactivates cards."""
        if self._page_controller is None:
            return
        device_id = live.target.device_id
        target = resolve_switch_target(self._device_layout(device_id), payload)
        if target is None:
            logging.info("[%s] switch_page %r has no target", live.target.plugin_uuid, payload)
            return
        self._page_controller.activate(device_id, target)

    def _on_switch_profile(self, live: _Live, payload: dict[str, Any]) -> None:
        """Switch the key's device to another profile; runs without the manager lock like a page switch."""
        if self._page_controller is None:
            return
        device_id = live.target.device_id
        target = resolve_profile_switch_target(self._device_layout(device_id), payload)
        if target is None:
            logging.info("[%s] switch_profile %r has no target", live.target.plugin_uuid, payload)
            return
        self._page_controller.activate_profile(device_id, target)

    def _on_set_global_settings(self, plugin_id: str, payload: dict[str, Any]) -> None:
        raw = payload.get("settings")
        if not isinstance(raw, dict):
            logging.warning("[%s] set_global_settings without a settings object dropped", plugin_id)
            return
        self.update_global_settings(plugin_id, raw, notify=False)

    def _on_set_gestures(self, live: _Live, payload: dict[str, Any]) -> None:
        gestures = _gestures_from(payload.get("gestures"))
        if not gestures:
            logging.warning("[%s] set_gestures without valid gestures ignored", live.target.plugin_uuid)
            return
        live.gestures = gestures

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
                    if item.target.device_id == device_id and item.target.key_index == key_index and item.parent is None
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
