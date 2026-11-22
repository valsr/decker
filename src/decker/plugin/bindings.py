"""Gesture bindings: which action a card runs for each gesture."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

GESTURES: tuple[str, ...] = ("press", "double_press", "long_press")
NOTHING_ACTION_ID: str = "com.decker.core.nothing"
_ACTION_ID = re.compile(r"[a-z0-9]+(\.[a-z0-9-]+)+")


@dataclass(frozen=True)
class Binding:
    """An action and the arguments it runs with."""

    action: str
    args: Mapping[str, Any] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", MappingProxyType(dict(self.args)))

    @property
    def is_nothing(self) -> bool:
        """Whether this binding is Do Nothing."""
        return self.action == NOTHING_ACTION_ID

    def to_json(self) -> dict[str, Any]:
        """The JSON form: action id and args."""
        return {"action": self.action, "args": dict(self.args)}


def parse_binding(raw: Any, where: str) -> Binding:
    """Parse one binding object; raises ValueError mentioning `where`."""
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where}: a binding must be an object")
    action = raw.get("action")
    if not isinstance(action, str) or _ACTION_ID.fullmatch(action) is None:
        raise ValueError(f"{where}: binding needs an action id (reverse-domain), got {action!r}")
    args = raw.get("args", {})
    if not isinstance(args, Mapping):
        raise ValueError(f"{where}: binding args must be an object")
    return Binding(action, dict(args))


def parse_manifest_bindings(raw: Any, where: str) -> dict[str, Binding]:
    """Parse a manifest `bindings` object; omitted gestures are Do Nothing."""
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where}: bindings must be an object")
    unknown = sorted(str(k) for k in raw if k not in GESTURES)
    if unknown:
        raise ValueError(f"{where}: unknown gesture(s) {', '.join(unknown)} (known: {', '.join(GESTURES)})")
    return {g: parse_binding(raw[g], f"{where}.{g}") if g in raw else Binding(NOTHING_ACTION_ID, {}) for g in GESTURES}


def normalize_stored_bindings(raw: Any) -> dict[str, Binding]:
    """Keep well-formed stored entries for known gestures; anything else means the default. Never raises."""
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, Binding] = {}
    for gesture in GESTURES:
        try:
            result[gesture] = parse_binding(raw.get(gesture), gesture)
        except ValueError:
            continue
    return result


def effective_bindings(defaults: Mapping[str, Binding], stored: Mapping[str, Binding]) -> dict[str, Binding]:
    """Per gesture, the stored binding if present, else the default."""
    return {g: stored.get(g) or defaults[g] for g in GESTURES if g in stored or g in defaults}


def stored_to_json(stored: Mapping[str, Binding]) -> dict[str, dict[str, Any]]:
    """JSON form of stored bindings."""
    return {g: b.to_json() for g, b in stored.items()}
