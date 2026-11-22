"""Declarative settings schema used by plugin manifests to describe an action's settings form."""

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType
from typing import Any

from decker.common.background import BACKGROUND_DEFAULT, normalize_background
from decker.common.icon_ref import normalize_icon_value
from decker.common.options import MAX_TEXT, is_valid_source
from decker.common.slots import SLOT_NAMES, normalize_slots
from decker.common.text_style import TEXT_STYLE_DEFAULTS, normalize_text_style

FIELD_TYPES = frozenset(
    {
        "int",
        "float",
        "text",
        "bool",
        "choice",
        "color",
        "font",
        "icon",
        "text_style",
        "background",
        "page",
        "profile",
        "action",
        "action_params",
        "slots",
    }
)

_PROFILE_ID_PATTERN = re.compile(r"[0-9]*\Z")
_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
_RGBA_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")
_EXTRA_KEYS: dict[str, tuple[str, ...]] = {
    "int": ("min", "max", "step", "special_value"),
    "float": ("min", "max", "step", "decimals"),
    "text": ("multiline", "template", "pattern", "min_length", "max_length"),
    "text_style": ("template",),
    "color": ("alpha",),
    "page": ("profile_field",),
    "action_params": ("action_field",),
    "choice": ("options", "options_source"),
}
_ZERO_VALUES: dict[str, Any] = {
    "int": 0,
    "float": 0.0,
    "text": "",
    "bool": False,
    "color": "#000000",
    "font": "",
    "page": "",
    "profile": "",
    "action": "",
    "action_params": {},
    "icon": {"ref": "", "color": "#ffffff"},
    "text_style": dict(TEXT_STYLE_DEFAULTS),
    "background": {**BACKGROUND_DEFAULT, "image": dict(BACKGROUND_DEFAULT["image"])},
    "slots": dict.fromkeys(SLOT_NAMES),
}
# Object-valued types: normalised field by field against the field default (never rejected as a whole).
_OBJECT_NORMALIZERS: dict[str, Callable[..., dict[str, Any]]] = {
    "text_style": normalize_text_style,
    "icon": normalize_icon_value,
    "background": normalize_background,
    "slots": normalize_slots,
}
_MISSING = object()


@dataclass(frozen=True)
class SettingSection:
    """A heading in the settings form."""

    title: str


@dataclass(frozen=True)
class SettingField:
    """A single typed setting; `extra` holds the type-specific keys."""

    key: str
    type: str
    label: str
    default: Any = None
    optional: bool = False
    required: bool = False
    placeholder: str = ""
    help: str = ""
    extra: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}), hash=False)


SettingEntry = SettingSection | SettingField


class SettingsValidationError(ValueError):
    """Raised by a strict `apply_schema`; `errors` maps each failing field key to a message."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{key}: {message}" for key, message in errors.items()))
        self.errors: dict[str, str] = errors


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_options(raw: Any, where: str, has_source: bool = False) -> tuple[Mapping[str, Any], ...]:
    if has_source and raw is None:
        return ()
    if not isinstance(raw, list) or not (raw or has_source):
        raise ValueError(f"{where}: choice requires a non-empty 'options' list")
    options: list[Mapping[str, Any]] = []
    for option in raw:
        if not isinstance(option, dict) or "value" not in option or not isinstance(option.get("label"), str):
            raise ValueError(f"{where}: each option needs a 'label' string and a 'value'")
        value = option["value"]
        if has_source and not (isinstance(value, str) and len(value) <= MAX_TEXT):
            raise ValueError(
                f"{where}: options of a choice with 'options_source' need string values of at most {MAX_TEXT} characters"
            )
        options.append(MappingProxyType({"label": option["label"], "value": value}))
    return tuple(options)


def _parse_extra(type_: str, data: dict[str, Any], where: str) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    for name in _EXTRA_KEYS.get(type_, ()):
        if name in data:
            extra[name] = data[name]
    if "template" in data and type_ not in ("text", "text_style"):
        raise ValueError(f"{where}: 'template' is only valid on text and text_style fields")
    if "options_source" in data and type_ != "choice":
        raise ValueError(f"{where}: 'options_source' is only valid on choice fields")
    if "options_source" in extra and not (
        isinstance(extra["options_source"], str) and is_valid_source(extra["options_source"])
    ):
        raise ValueError(f"{where}: 'options_source' must be a lowercase source name like 'output_devices'")
    if type_ == "choice":
        extra["options"] = _parse_options(data.get("options"), where, "options_source" in extra)
    for name in ("min", "max", "step", "decimals"):
        if name in extra and not _is_number(extra[name]):
            raise ValueError(f"{where}: '{name}' must be a number")
    if type_ == "int":
        for name in ("min", "max", "step"):
            if name in extra and not isinstance(extra[name], int):
                raise ValueError(f"{where}: '{name}' must be an integer for an int field")
    if "special_value" in extra and not isinstance(extra["special_value"], str):
        raise ValueError(f"{where}: 'special_value' must be a string")
    _check_profile_field(type_, data, extra, where)
    if "action_field" in data and type_ != "action_params":
        raise ValueError(f"{where}: 'action_field' is only valid on action_params fields")
    if "action_field" in extra and not isinstance(extra["action_field"], str):
        raise ValueError(f"{where}: 'action_field' must be a string")
    for name in ("multiline", "alpha", "template"):
        if name in extra and not isinstance(extra[name], bool):
            raise ValueError(f"{where}: '{name}' must be a boolean")
    _check_text_rules(type_, data, extra, where)
    return extra


def _check_text_rules(type_: str, data: dict[str, Any], extra: Mapping[str, Any], where: str) -> None:
    for name in ("pattern", "min_length", "max_length"):
        if name in data and type_ != "text":
            raise ValueError(f"{where}: '{name}' is only valid on text fields")
    if "pattern" in extra:
        try:
            if not isinstance(extra["pattern"], str):
                raise TypeError
            _compiled(extra["pattern"])
        except (re.error, TypeError) as exc:
            raise ValueError(f"{where}: 'pattern' must be a valid regular expression string") from exc
    for name in ("min_length", "max_length"):
        value = extra.get(name, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{where}: '{name}' must be a non-negative integer")
    if extra.get("min_length", 0) > extra.get("max_length", extra.get("min_length", 0)):
        raise ValueError(f"{where}: 'min_length' must not exceed 'max_length'")


def _check_profile_field(type_: str, data: dict[str, Any], extra: Mapping[str, Any], where: str) -> None:
    if "profile_field" in data and type_ != "page":
        raise ValueError(f"{where}: 'profile_field' is only valid on page fields")
    if "profile_field" in extra and not isinstance(extra["profile_field"], str):
        raise ValueError(f"{where}: 'profile_field' must be a string")


def _in_range(value: float, extra: Mapping[str, Any]) -> bool:
    return not (("min" in extra and value < extra["min"]) or ("max" in extra and value > extra["max"]))


def _coerce(type_: str, value: Any, extra: Mapping[str, Any]) -> Any:
    """Return the typed value, or `_MISSING` when it is not valid for the type."""
    result: Any = _MISSING
    if type_ == "int":
        result = _coerce_int(value)
    elif type_ == "float":
        result = _coerce_float(value)
    elif type_ == "bool":
        result = value if isinstance(value, bool) else _MISSING
    elif type_ == "color":
        pattern = _RGBA_PATTERN if extra.get("alpha") else _COLOR_PATTERN
        result = value if isinstance(value, str) and pattern.match(value) else _MISSING
    elif type_ == "choice" and "options_source" in extra:
        result = value if isinstance(value, str) and len(value) <= MAX_TEXT else _MISSING
    elif type_ == "choice":
        allowed = [o["value"] for o in extra["options"]]
        result = next((o for o in allowed if type(o) is type(value) and o == value), _MISSING)
    elif type_ in _OBJECT_NORMALIZERS:
        result = _OBJECT_NORMALIZERS[type_](value)
    elif type_ == "profile":
        result = value if isinstance(value, str) and _PROFILE_ID_PATTERN.match(value) else _MISSING
    elif type_ == "action_params":
        result = dict(value) if isinstance(value, dict) else _MISSING
    elif isinstance(value, str):
        result = value
    if type_ in ("int", "float") and result is not _MISSING and not _in_range(result, extra):
        return _MISSING
    return result


def _coerce_int(value: Any) -> Any:
    if isinstance(value, bool):
        return _MISSING
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return _MISSING
    return _MISSING


def _coerce_float(value: Any) -> Any:
    if isinstance(value, bool):
        return _MISSING
    try:
        result = float(value.strip() if isinstance(value, str) else value)
    except (TypeError, ValueError, OverflowError):
        return _MISSING
    return result if math.isfinite(result) else _MISSING


def _copy(value: Any) -> Any:
    return dict(value) if isinstance(value, dict) else value


def _parse_default(type_: str, data: dict[str, Any], optional: bool, extra: Mapping[str, Any], where: str) -> Any:
    if data.get("default") is None:
        if optional:
            return None
        if type_ == "choice":
            return extra["options"][0]["value"] if extra["options"] else ""
        return _copy(_ZERO_VALUES[type_])
    raw = data["default"]
    if type_ in _OBJECT_NORMALIZERS:
        if not isinstance(raw, dict) and not (type_ == "background" and isinstance(raw, str)):
            raise ValueError(f"{where}: default {raw!r} is not valid for type '{type_}'")
        return _OBJECT_NORMALIZERS[type_](raw)
    # A JSON float with an int-like value (e.g. 1.0) is not a valid int default.
    value = _MISSING if type_ == "int" and not isinstance(raw, (int, str)) else _coerce(type_, raw, extra)
    if value is _MISSING or (type_ in ("int", "float") and isinstance(raw, str)):
        raise ValueError(f"{where}: default {raw!r} is not valid for type '{type_}'")
    return value


def _parse_field(data: dict[str, Any], where: str) -> SettingField:
    key, type_ = data.get("key"), data.get("type")
    if not isinstance(key, str) or not key:
        raise ValueError(f"{where}: field requires a non-empty string 'key'")
    if type_ not in FIELD_TYPES:
        raise ValueError(f"{where}: unknown field type {type_!r}")
    for name in ("label", "placeholder", "help"):
        if name in data and not isinstance(data[name], str):
            raise ValueError(f"{where}: '{name}' must be a string")
    if not isinstance(data.get("optional", False), bool):
        raise ValueError(f"{where}: 'optional' must be a boolean")
    optional = data.get("optional", False)
    if not isinstance(data.get("required", False), bool):
        raise ValueError(f"{where}: 'required' must be a boolean")
    extra = _parse_extra(type_, data, where)
    return SettingField(
        key=key,
        type=type_,
        label=data.get("label", key),
        default=_parse_default(type_, data, optional, extra, where),
        optional=optional,
        required=data.get("required", False),
        placeholder=data.get("placeholder", ""),
        help=data.get("help", ""),
        extra=MappingProxyType(extra),
    )


def parse_settings_schema(data: Any, where: str) -> tuple[SettingEntry, ...]:
    """Parse a manifest settings list; raises ValueError mentioning `where` on any problem."""
    if not isinstance(data, list):
        raise ValueError(f"{where}: settings must be a list")
    entries: list[SettingEntry] = []
    keys: set[str] = set()
    for index, item in enumerate(data):
        here = f"{where}[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{here}: settings entry must be an object")
        if "section" in item:
            if not isinstance(item["section"], str) or not item["section"]:
                raise ValueError(f"{here}: section title must be a non-empty string")
            entries.append(SettingSection(item["section"]))
            continue
        parsed = _parse_field(item, here)
        if parsed.key in keys:
            raise ValueError(f"{here}: duplicate key {parsed.key!r}")
        keys.add(parsed.key)
        entries.append(parsed)
    _check_profile_fields(entries, where)
    return tuple(entries)


def _check_profile_fields(entries: Sequence[SettingEntry], where: str) -> None:
    profile_keys = {e.key for e in entries if isinstance(e, SettingField) and e.type == "profile"}
    for entry in entries:
        if isinstance(entry, SettingField) and entry.extra.get("profile_field", "") not in profile_keys | {""}:
            raise ValueError(f"{where}: profile_field {entry.extra['profile_field']!r} is not a profile field")


def _is_empty(entry: SettingField, raw: Any) -> bool:
    if entry.type == "icon" and isinstance(raw, dict):
        return not raw.get("ref")
    return raw is None or raw == ""


def _field_error(entry: SettingField, raw: Any) -> str | None:
    if _is_empty(entry, raw):
        if entry.required:
            return "is required"
        if entry.optional or entry.type == "text" or entry.type in _OBJECT_NORMALIZERS:
            return None
    if entry.type in _OBJECT_NORMALIZERS:
        return None
    value = _coerce(entry.type, raw, entry.extra)
    if value is _MISSING:
        return "is not a valid value"
    return _text_error(entry.extra, value) if entry.type == "text" else None


def _text_error(extra: Mapping[str, Any], value: str) -> str | None:
    if "pattern" in extra and not _compiled(extra["pattern"]).fullmatch(value):
        return f"does not match the pattern {extra['pattern']!r}"
    if len(value) < extra.get("min_length", 0):
        return f"must be at least {extra['min_length']} characters"
    if len(value) > extra.get("max_length", len(value)):
        return f"must be at most {extra['max_length']} characters"
    return None


def validate_settings(schema: Sequence[SettingEntry], values: Mapping[str, Any]) -> dict[str, str]:
    """Return field key -> message for every value that breaks the schema; empty when all is valid."""
    errors: dict[str, str] = {}
    for entry in schema:
        if not isinstance(entry, SettingField):
            continue
        raw = values.get(entry.key, _MISSING)
        message = _field_error(entry, entry.default if raw is _MISSING else raw)
        if message:
            errors[entry.key] = message
    return errors


def apply_schema(schema: Sequence[SettingEntry], values: Mapping[str, Any], strict: bool = False) -> dict[str, Any]:
    """Return a complete, typed settings dict; invalid values default, or raise when `strict`."""
    if strict and (errors := validate_settings(schema, values)):
        raise SettingsValidationError(errors)
    result: dict[str, Any] = {}
    for entry in schema:
        if not isinstance(entry, SettingField):
            continue
        raw = values.get(entry.key, _MISSING)
        if raw is _MISSING:
            result[entry.key] = _copy(entry.default)
        elif entry.type in _OBJECT_NORMALIZERS:
            result[entry.key] = _OBJECT_NORMALIZERS[entry.type](raw, entry.default or _ZERO_VALUES[entry.type])
        elif entry.optional and (raw is None or raw == ""):
            result[entry.key] = None
        else:
            value = _coerce(entry.type, raw, entry.extra)
            result[entry.key] = entry.default if value is _MISSING else value
    return result


def _field_to_json(entry: SettingField) -> dict[str, Any]:
    out: dict[str, Any] = {"key": entry.key, "type": entry.type, "label": entry.label}
    if entry.default is not None:
        out["default"] = entry.default
    if entry.optional:
        out["optional"] = True
    if entry.required:
        out["required"] = True
    if entry.placeholder:
        out["placeholder"] = entry.placeholder
    if entry.help:
        out["help"] = entry.help
    for name, value in entry.extra.items():
        out[name] = [dict(o) for o in value] if name == "options" else value
    return out


def schema_to_json(schema: Sequence[SettingEntry]) -> list[dict[str, Any]]:
    """Serialise a schema back to the manifest shape (sent to clients)."""
    return [{"section": e.title} if isinstance(e, SettingSection) else _field_to_json(e) for e in schema]
