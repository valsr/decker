"""Plugin manifest (format 3) parsing and validation."""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from packaging.requirements import InvalidRequirement, Requirement

from decker.common import card_variables
from decker.common.appearance_schema import APPEARANCE_KEYS
from decker.common.icon_ref import parse_icon_ref
from decker.common.platforms import PlatformSpec, parse_platform_spec
from decker.common.variables import (
    CORE_VARIABLE_NAMES,
    MAX_DECLARATIONS,
    is_valid_name,
    is_valid_pattern,
    match_pattern,
)
from decker.plugin.bindings import Binding, parse_manifest_bindings
from decker.plugin.settings_schema import (
    SettingEntry,
    SettingField,
    parse_settings_schema,
)

_SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?")
SUPPORTED_FORMATS = (3,)
KNOWN_CONTRIBUTION_KINDS = frozenset({"cards", "actions", "icon_packs", "variables", "presets"})
_RUNTIME_TYPES = ("python", "executable")
MAX_ACTION_TIMEOUT_S = 300
_UUID_PATTERN = re.compile(r"[a-z0-9]+(\.[a-z0-9-]+)+")


def is_valid_plugin_id(value: object) -> bool:
    """Whether value is a valid v1 plugin id (reverse-DNS style)."""
    return isinstance(value, str) and _UUID_PATTERN.fullmatch(value) is not None


def is_valid_version(value: object) -> bool:
    """Whether value is a valid semver plugin version."""
    return isinstance(value, str) and _SEMVER.fullmatch(value) is not None


class ManifestValidationError(ValueError):
    """A manifest.json failed schema or naming-convention validation."""


@dataclass(frozen=True)
class RuntimeSpec:
    """How a plugin's code is run."""

    type: Literal["python", "executable"]
    entry: str
    python: str | None = None
    requirements: tuple[str, ...] = ()


@dataclass(frozen=True)
class StateSpec:
    """A visual state of a card."""

    image: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class CardSpec:
    """A key card contributed by a plugin."""

    id: str
    name: str
    tooltip: str = ""
    icon: str | None = None
    states: tuple[StateSpec, ...] = ()
    settings: tuple[SettingEntry, ...] = ()
    appearance: dict[str, Any] = field(default_factory=dict, hash=False)
    static: bool = False
    bindings: Mapping[str, Binding] = field(default_factory=dict, hash=False)
    actions: tuple[str, ...] = ()

    @property
    def headless(self) -> bool:
        """Whether the card has no process of its own: a static preset."""
        return self.static


@dataclass(frozen=True)
class PresetSpec:
    """A ready-made Blank card a plugin offers: a name, icon, appearance and bindings, nothing else."""

    id: str
    name: str
    tooltip: str = ""
    icon: str | None = None
    appearance: dict[str, Any] = field(default_factory=dict, hash=False)
    bindings: Mapping[str, Binding] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class ActionVariable:
    """A per-card variable an action declares it may set."""

    name: str
    description: str = ""


@dataclass(frozen=True)
class ActionSpec:
    """A headless action a plugin contributes: code to run with settings, drawing nothing."""

    id: str
    name: str
    tooltip: str = ""
    icon: str | None = None
    settings: tuple[SettingEntry, ...] = ()
    scope: Literal["global", "plugin"] = "plugin"
    returns: str = ""
    timeout: int | None = None
    variables: tuple[ActionVariable, ...] = ()


@dataclass(frozen=True)
class IconPackSpec:
    """An icon pack contributed by a plugin."""

    id: str
    name: str
    path: str
    description: str = ""


@dataclass(frozen=True)
class VariableSpec:
    """A global template variable a plugin declares, by exact name or by name pattern."""

    name: str | None
    pattern: str | None
    description: str = ""

    def __post_init__(self) -> None:
        if (self.name is None) == (self.pattern is None):
            raise ValueError("a variable declares exactly one of name and pattern")


# One field per manifest key: a frozen dataclass of plain values, so the attribute count is not a design smell.
@dataclass(frozen=True)
class PluginManifest:  # pylint: disable=too-many-instance-attributes
    """Validated contents of a plugin's manifest.json."""

    format: int
    id: str
    name: str
    version: str
    author: str
    description: str
    homepage: str | None
    icon: str | None
    category: str | None
    decker: str | None
    runtime: RuntimeSpec | None
    permissions: tuple[str, ...]
    global_settings: tuple[SettingEntry, ...]
    cards: tuple[CardSpec, ...]
    actions: tuple[ActionSpec, ...]
    icon_packs: tuple[IconPackSpec, ...]
    platforms: PlatformSpec | None = None
    variables: tuple[VariableSpec, ...] = ()
    presets: tuple[PresetSpec, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PluginManifest":
        """Validate and construct a PluginManifest from parsed JSON. Raises ManifestValidationError."""
        fmt = data.get("format")
        if fmt is None:
            raise ManifestValidationError("manifest missing required field: format")
        if type(fmt) is int and fmt in (1, 2):  # pylint: disable=unidiomatic-typecheck
            raise ManifestValidationError(
                f"manifest format {fmt} is no longer supported: Decker 0.7 needs format {SUPPORTED_FORMATS[0]}"
            )
        if type(fmt) is not int or fmt not in SUPPORTED_FORMATS:  # pylint: disable=unidiomatic-typecheck
            raise ManifestValidationError(f"unsupported manifest format {fmt!r} (supported: {list(SUPPORTED_FORMATS)})")
        plugin_id = _parse_id(data.get("id"), "manifest id")
        missing = [key for key in ("name", "version", "author") if not _is_nonempty_str(data.get(key))]
        if missing:
            raise ManifestValidationError(f"manifest missing required field(s): {', '.join(missing)}")
        if not is_valid_version(str(data["version"])):
            raise ManifestValidationError(f"manifest version {data['version']!r} is not a semantic version (x.y.z)")

        contributes = data.get("contributes")
        if not isinstance(contributes, dict) or not contributes:
            raise ManifestValidationError("manifest contributes must be a non-empty object")
        unknown = sorted(set(contributes) - KNOWN_CONTRIBUTION_KINDS)
        if unknown:
            raise ManifestValidationError(
                f"manifest contributes unknown kind(s) {', '.join(unknown)}: requires a newer version of Decker"
            )

        runtime = _parse_runtime(data.get("runtime")) if data.get("runtime") is not None else None
        cards = tuple(_parse_card(e, plugin_id) for e in _as_list(contributes.get("cards", []), "cards"))
        packs = tuple(_parse_icon_pack(e, plugin_id) for e in _as_list(contributes.get("icon_packs", []), "icon_packs"))
        actions = tuple(_parse_action(e, plugin_id) for e in _as_list(contributes.get("actions", []), "actions"))
        needs_runtime = next((a for a in cards if not a.headless), None)
        if needs_runtime and runtime is None:
            raise ManifestValidationError(
                f"manifest contributes cards but declares no runtime (card {needs_runtime.id} is not static)"
            )
        if actions and runtime is None:
            raise ManifestValidationError("manifest contributes actions but declares no runtime")
        presets = _parse_presets(contributes.get("presets", []), plugin_id)
        _check_preset_ids_unique(presets, cards, actions)
        _check_card_actions(cards, actions)
        _check_own_bindings(cards, actions, plugin_id, "card")
        _check_own_bindings(presets, actions, plugin_id, "preset")
        _check_preset_action_scopes(presets, actions)
        variables = _parse_variables(contributes.get("variables", []))
        if variables:
            _require_variable_prerequisites(runtime, data.get("decker"))
        if presets:
            _require_decker_constraint(data.get("decker"), "contributes presets")
        if not cards and not actions and not packs and not variables and not presets:
            raise ManifestValidationError("manifest contributes no cards, actions, icon packs, variables or presets")
        global_settings = _parse_global_settings(data.get("global_settings", []))
        if declared_options_sources(global_settings, cards, actions):
            _require_options_prerequisites(runtime, data.get("decker"))

        return cls(
            format=fmt,
            id=plugin_id,
            name=str(data["name"]),
            version=str(data["version"]),
            author=str(data["author"]),
            description=str(data.get("description") or ""),
            homepage=_optional_str(data.get("homepage")),
            icon=_optional_str(data.get("icon")),
            category=_optional_str(data.get("category")),
            decker=_optional_str(data.get("decker")),
            runtime=runtime,
            permissions=tuple(str(p) for p in _as_list(data.get("permissions", []), "permissions")),
            global_settings=global_settings,
            cards=cards,
            actions=actions,
            icon_packs=packs,
            platforms=_parse_platforms(data),
            variables=variables,
            presets=presets,
        )

    @classmethod
    def load(cls, manifest_path: str | Path) -> "PluginManifest":
        """Read and validate manifest.json at the given path. Raises ManifestValidationError."""
        path = Path(manifest_path)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ManifestValidationError(f"could not read {path}: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ManifestValidationError(f"malformed JSON in {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ManifestValidationError(f"{path} must contain a JSON object")
        return cls.from_dict(data)


def _parse_id(value: Any, what: str) -> str:
    if not is_valid_plugin_id(value):
        raise ManifestValidationError(f"{what} {value!r} is not a valid reverse-domain identifier")
    return value


def _parse_contribution_id(value: Any, plugin_id: str, what: str) -> str:
    cid = _parse_id(value, what)
    if not cid.startswith(plugin_id + "."):
        raise ManifestValidationError(f"{what} {cid!r} must start with the plugin id {plugin_id + '.'!r}")
    return cid


def _parse_platforms(data: dict[str, Any]) -> PlatformSpec | None:
    try:
        return parse_platform_spec(data.get("platforms"), data.get("linux"))
    except ValueError as exc:
        raise ManifestValidationError(f"manifest {exc}") from exc


def _as_list(value: Any, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise ManifestValidationError(f"manifest {what} must be a list")
    return value


def _parse_settings(value: Any, where: str) -> tuple[SettingEntry, ...]:
    try:
        return parse_settings_schema(value, where)
    except ValueError as exc:
        raise ManifestValidationError(str(exc)) from exc


def _parse_global_settings(value: Any) -> tuple[SettingEntry, ...]:
    entries = _parse_settings(value, "global_settings")
    for entry in entries:
        if isinstance(entry, SettingField) and entry.type in ("page", "profile"):
            raise ManifestValidationError(
                f"global_settings: field {entry.key!r} of type {entry.type!r} is not allowed here"
            )
    return entries


def _parse_runtime(data: Any) -> RuntimeSpec:
    if not isinstance(data, dict):
        raise ManifestValidationError("manifest runtime must be an object")
    rtype = data.get("type")
    if rtype not in _RUNTIME_TYPES:
        raise ManifestValidationError(f"runtime type {rtype!r} must be one of {', '.join(_RUNTIME_TYPES)}")
    entry = data.get("entry")
    if not isinstance(entry, str) or not entry.strip():
        raise ManifestValidationError("runtime entry must be a non-empty string")
    requirements = tuple(
        _validate_requirement(r) for r in _as_list(data.get("requirements", []), "runtime requirements")
    )
    return RuntimeSpec(type=rtype, entry=entry, python=_optional_str(data.get("python")), requirements=requirements)


def _validate_requirement(req: Any) -> str:
    if not isinstance(req, str):
        raise ManifestValidationError(f"requirement {req!r} must be a string")
    try:
        parsed = Requirement(req)
    except InvalidRequirement as exc:
        raise ManifestValidationError(f"invalid requirement {req!r}: {exc}") from exc
    if parsed.url:
        raise ManifestValidationError(f"requirement {req!r} must be a package-index requirement, not a URL")
    return req


def _parse_card(entry: Any, plugin_id: str) -> CardSpec:
    if not isinstance(entry, dict) or "id" not in entry or "name" not in entry:
        raise ManifestValidationError(f"invalid card entry: {entry!r}")
    card_id = _parse_contribution_id(entry["id"], plugin_id, "card id")
    _reject_retired_card_keys(entry, card_id)
    static = _parse_static(entry, card_id)
    appearance = _parse_appearance(entry, f"card {card_id}")
    states = tuple(_parse_state(s, card_id) for s in _as_list(entry.get("states", []), "states"))
    return CardSpec(
        id=card_id,
        name=str(entry["name"]),
        tooltip=str(entry.get("tooltip") or ""),
        icon=_optional_str(entry.get("icon")),
        states=states,
        settings=_parse_settings(entry.get("settings", []), f"card {card_id} settings"),
        appearance=appearance,
        static=static,
        bindings=_parse_card_bindings(entry, card_id),
        actions=_parse_card_actions(entry.get("actions", []), card_id),
    )


def _parse_card_actions(value: Any, card_id: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ManifestValidationError(f"card {card_id} actions must be a list")
    ids: list[str] = []
    for action_id in value:
        if not isinstance(action_id, str) or not is_valid_plugin_id(action_id):
            raise ManifestValidationError(f"card {card_id} action id {action_id!r} is not a valid identifier")
        if action_id in ids:
            raise ManifestValidationError(f"card {card_id} lists action {action_id!r} more than once")
        ids.append(action_id)
    return tuple(ids)


def _check_card_actions(cards: tuple[CardSpec, ...], actions: tuple[ActionSpec, ...]) -> None:
    scopes = {action.id: action.scope for action in actions}
    for card in cards:
        for action_id in card.actions:
            if action_id not in scopes:
                raise ManifestValidationError(f"card {card.id} lists unknown action {action_id!r}")
            if scopes[action_id] != "plugin":
                raise ManifestValidationError(
                    f"card {card.id} lists global action {action_id!r}; a card may list only plugin-scoped actions"
                )


def _parse_appearance(entry: dict[str, Any], owner: str) -> dict[str, Any]:
    appearance = entry.get("appearance", {})
    if not isinstance(appearance, dict):
        raise ManifestValidationError(f"{owner} appearance must be an object")
    for key in appearance:
        if key not in APPEARANCE_KEYS:
            raise ManifestValidationError(f"{owner} has unknown appearance key {key!r}")
    return dict(appearance)


_PRESET_KEYS = frozenset({"id", "name", "tooltip", "icon", "appearance", "bindings"})
_PRESET_ICON_PREFIXES = ("pack:", "fas:", "far:", "fab:")


def _parse_presets(value: Any, plugin_id: str) -> tuple[PresetSpec, ...]:
    presets = tuple(_parse_preset(e, plugin_id) for e in _as_list(value, "presets"))
    ids = [preset.id for preset in presets]
    for preset_id in ids:
        if ids.count(preset_id) > 1:
            raise ManifestValidationError(f"duplicate preset id {preset_id!r}")
    return presets


def _check_preset_ids_unique(
    presets: tuple[PresetSpec, ...], cards: tuple[CardSpec, ...], actions: tuple[ActionSpec, ...]
) -> None:
    for kind, others in (("card", cards), ("action", actions)):
        taken = {other.id for other in others}
        for preset in presets:
            if preset.id in taken:
                raise ManifestValidationError(f"preset id {preset.id!r} is also {_article(kind)} {kind} id")


def _article(kind: str) -> str:
    return "an" if kind[0] in "aeiou" else "a"


def _parse_preset(entry: Any, plugin_id: str) -> PresetSpec:
    if not isinstance(entry, dict) or "id" not in entry or "name" not in entry:
        raise ManifestValidationError(f"invalid preset entry: {entry!r}")
    preset_id = _parse_contribution_id(entry["id"], plugin_id, "preset id")
    unknown = sorted(str(key) for key in set(entry) - _PRESET_KEYS)
    if unknown:
        raise ManifestValidationError(f"preset {preset_id} has unknown key(s) {', '.join(unknown)}")
    appearance = _parse_appearance(entry, f"preset {preset_id}")
    _check_preset_icon_ref(appearance, preset_id)
    try:
        bindings = parse_manifest_bindings(entry.get("bindings", {}), f"preset {preset_id} bindings")
    except ValueError as exc:
        raise ManifestValidationError(str(exc)) from exc
    return PresetSpec(
        id=preset_id,
        name=str(entry["name"]),
        tooltip=str(entry.get("tooltip") or ""),
        icon=_optional_str(entry.get("icon")),
        appearance=appearance,
        bindings=bindings,
    )


def _check_preset_icon_ref(appearance: dict[str, Any], preset_id: str) -> None:
    icon = appearance.get("icon")
    if icon is None:
        return
    ref = icon.get("ref") if isinstance(icon, dict) else None
    if not (isinstance(ref, str) and ref.startswith(_PRESET_ICON_PREFIXES) and parse_icon_ref(ref) is not None):
        raise ManifestValidationError(
            f"preset {preset_id} appearance icon ref {ref!r} must be a pack:, fas:, far: or fab: reference"
        )


def _parse_action(entry: Any, plugin_id: str) -> ActionSpec:
    if not isinstance(entry, dict) or "id" not in entry or "name" not in entry:
        raise ManifestValidationError(f"invalid action entry: {entry!r}")
    action_id = _parse_contribution_id(entry["id"], plugin_id, "action id")
    return ActionSpec(
        id=action_id,
        name=str(entry["name"]),
        tooltip=str(entry.get("tooltip") or ""),
        icon=_optional_str(entry.get("icon")),
        settings=_parse_settings(entry.get("settings", []), f"action {action_id} settings"),
        scope=_parse_scope(entry.get("scope", "plugin"), action_id),
        returns=_parse_returns(entry.get("returns", ""), action_id),
        timeout=_parse_timeout(entry.get("timeout"), action_id),
        variables=_parse_action_variables(entry.get("variables", []), action_id),
    )


def _parse_action_variables(value: Any, action_id: str) -> tuple[ActionVariable, ...]:
    if not isinstance(value, list):
        raise ManifestValidationError(f"action {action_id} variables must be a list")
    if len(value) > card_variables.MAX_ACTION_VARIABLES:
        raise ManifestValidationError(
            f"action {action_id} may declare at most {card_variables.MAX_ACTION_VARIABLES} variables"
        )
    result: list[ActionVariable] = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ManifestValidationError(f"action {action_id} has an invalid variable entry: {item!r}")
        name = item["name"]
        if not card_variables.is_valid_name(name) or name == card_variables.RESERVED_NAME:
            raise ManifestValidationError(f"action {action_id} variable name {name!r} is invalid or reserved")
        if any(v.name == name for v in result):
            raise ManifestValidationError(f"action {action_id} declares variable {name!r} twice")
        result.append(ActionVariable(name, str(item.get("description") or "")))
    return tuple(result)


def _parse_timeout(value: Any, action_id: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_ACTION_TIMEOUT_S:
        raise ManifestValidationError(
            f"action {action_id} timeout must be a whole number of seconds, 1-{MAX_ACTION_TIMEOUT_S}"
        )
    return value


def _parse_scope(value: Any, action_id: str) -> Literal["global", "plugin"]:
    if value not in ("global", "plugin"):
        raise ManifestValidationError(f"action {action_id} scope must be global or plugin, got {value!r}")
    return cast(Literal["global", "plugin"], value)


def _parse_returns(value: Any, action_id: str) -> str:
    if not isinstance(value, str):
        raise ManifestValidationError(f"action {action_id} returns must be a string")
    return value


_RETIRED_CARD_KEYS = ("gestures", "card", "action", "action_settings", "behavior")


def _reject_retired_card_keys(entry: dict[str, Any], card_id: str) -> None:
    for key in _RETIRED_CARD_KEYS:
        if key in entry:
            raise ManifestValidationError(
                f"card {card_id} has unknown key {key!r}: format 3 cards declare bindings instead of gestures"
            )


def _parse_card_bindings(entry: dict[str, Any], card_id: str) -> dict[str, Binding]:
    try:
        return parse_manifest_bindings(entry.get("bindings", {}), f"card {card_id} bindings")
    except ValueError as exc:
        raise ManifestValidationError(str(exc)) from exc


def _check_own_bindings(
    owners: Sequence[CardSpec | PresetSpec], actions: tuple[ActionSpec, ...], plugin_id: str, noun: str
) -> None:
    own = {a.id for a in actions}
    for owner in owners:
        for gesture, binding in owner.bindings.items():
            if binding.action.startswith(plugin_id + ".") and binding.action not in own:
                raise ManifestValidationError(
                    f"{noun} {owner.id} binds {gesture} to {binding.action!r}, which this plugin does not declare"
                )


def _check_preset_action_scopes(presets: tuple[PresetSpec, ...], actions: tuple[ActionSpec, ...]) -> None:
    plugin_scoped = {a.id for a in actions if a.scope != "global"}
    for preset in presets:
        for gesture, binding in preset.bindings.items():
            if binding.action in plugin_scoped:
                raise ManifestValidationError(
                    f"preset {preset.id} binds {gesture} to {binding.action!r}, a plugin-scoped action; "
                    "a preset may bind only global actions"
                )


def _parse_static(entry: dict[str, Any], card_id: str) -> bool:
    static = entry.get("static", False)
    if not isinstance(static, bool):
        raise ManifestValidationError(f"card {card_id} static must be a boolean")
    if static:
        for name in ("settings",):
            if name in entry:
                raise ManifestValidationError(f"static card {card_id} cannot declare {name}")
    return static


def _parse_variables(value: Any) -> tuple[VariableSpec, ...]:
    entries = _as_list(value, "variables")
    if len(entries) > MAX_DECLARATIONS:
        raise ManifestValidationError(f"manifest declares {len(entries)} variables, at most {MAX_DECLARATIONS} allowed")
    specs = tuple(_parse_variable(entry) for entry in entries)
    declared = [spec.name or spec.pattern for spec in specs]
    for key in declared:
        if declared.count(key) > 1:
            raise ManifestValidationError(f"duplicate variable declaration {key!r}")
    return specs


def _parse_variable(entry: Any) -> VariableSpec:
    if not isinstance(entry, dict) or (entry.get("name") is None) == (entry.get("pattern") is None):
        raise ManifestValidationError(f"variable entry needs exactly one of name and pattern: {entry!r}")
    name, pattern, description = entry.get("name"), entry.get("pattern"), entry.get("description")
    if description is not None and not isinstance(description, str):
        raise ManifestValidationError(f"variable description must be a string: {description!r}")
    if name is not None and not (isinstance(name, str) and is_valid_name(name)):
        raise ManifestValidationError(f"invalid variable name {name!r}")
    if pattern is not None and not (isinstance(pattern, str) and is_valid_pattern(pattern)):
        raise ManifestValidationError(f"invalid variable pattern {pattern!r}")
    clash = next((c for c in sorted(CORE_VARIABLE_NAMES) if c == name or (pattern and match_pattern(pattern, c))), None)
    if clash is not None:
        raise ManifestValidationError(f"variable {name or pattern!r} collides with the core variable {clash!r}")
    return VariableSpec(name=name, pattern=pattern, description=description or "")


def _require_variable_prerequisites(runtime: RuntimeSpec | None, decker: Any) -> None:
    if runtime is None:
        raise ManifestValidationError("manifest contributes variables but declares no runtime")
    _require_decker_constraint(decker, "contributes variables")


def _require_decker_constraint(decker: Any, what: str) -> None:
    if not _is_nonempty_str(decker):
        raise ManifestValidationError(f'manifest {what}, so it must declare a "decker" version constraint')


def declared_options_sources(
    global_settings: tuple[SettingEntry, ...], cards: tuple[CardSpec, ...], actions: tuple[ActionSpec, ...]
) -> frozenset[str]:
    """Every `options_source` name used by a settings field of the manifest."""
    entries: list[SettingEntry] = [*global_settings]
    for card in cards:
        entries.extend(card.settings)
    for action in actions:
        entries.extend(action.settings)
    return frozenset(
        str(e.extra["options_source"]) for e in entries if isinstance(e, SettingField) and e.extra.get("options_source")
    )


def _require_options_prerequisites(runtime: RuntimeSpec | None, decker: Any) -> None:
    if runtime is None:
        raise ManifestValidationError("manifest uses options_source but declares no runtime")
    if not _is_nonempty_str(decker):
        raise ManifestValidationError('manifest uses options_source, so it must declare a "decker" version constraint')


def _parse_state(entry: Any, card_id: str) -> StateSpec:
    if not isinstance(entry, dict):
        raise ManifestValidationError(f"card {card_id} state must be an object")
    return StateSpec(image=_optional_str(entry.get("image")), title=_optional_str(entry.get("title")))


def _parse_icon_pack(entry: Any, plugin_id: str) -> IconPackSpec:
    if not isinstance(entry, dict) or not all(_is_nonempty_str(entry.get(k)) for k in ("id", "name", "path")):
        raise ManifestValidationError(f"invalid icon pack entry: {entry!r}")
    return IconPackSpec(
        id=_parse_contribution_id(entry["id"], plugin_id, "icon pack id"),
        name=entry["name"],
        path=entry["path"],
        description=str(entry.get("description") or ""),
    )


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _optional_str(value: Any) -> str | None:
    return value if _is_nonempty_str(value) else None
