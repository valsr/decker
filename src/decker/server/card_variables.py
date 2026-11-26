"""In-memory per-card action variables and their template view."""

import copy
import threading
from collections.abc import Iterator, Mapping
from typing import Any

from decker.common.card_variables import (
    RESERVED_NAME,
    is_valid_name,
    is_valid_slot,
    render_value,
)

STATUS_SUCCESS = "SUCCESS"
STATUS_FAILURE = "FAILURE"


class _Binding:
    """Variables of one binding: its own and one set per slot."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.slots: dict[str, dict[str, Any]] = {}

    def target(self, slot: str | None) -> dict[str, Any]:
        """Values of the binding itself, or of one slot."""
        if slot is None:
            return self.values
        return self.slots.setdefault(slot, {})

    def snapshot(self) -> dict[str, Any]:
        """Deep copy with slots nested by name."""
        result: dict[str, Any] = copy.deepcopy(self.values)
        for slot, values in self.slots.items():
            result[slot] = copy.deepcopy(values)
        return result


class CardVariableStore:
    """Thread-safe store of variables keyed by card context, binding and optional slot."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, _Binding]] = {}

    def set(self, context: str, binding: str, name: str, value: Any, slot: str | None = None) -> None:
        """Store ``value`` as ``name`` of the binding, or of its slot."""
        with self._lock:
            self._binding(context, binding).target(slot)[name] = value

    def set_status(self, context: str, binding: str, ok: bool, slot: str | None = None) -> None:
        """Store ``SUCCESS`` or ``FAILURE`` as the reserved ``status``."""
        self.set(context, binding, RESERVED_NAME, STATUS_SUCCESS if ok else STATUS_FAILURE, slot)

    def reset_binding(self, context: str, binding: str) -> None:
        """Forget the binding's variables, status and slots."""
        with self._lock:
            self._data.get(context, {}).pop(binding, None)

    def drop(self, context: str) -> None:
        """Forget everything of a card."""
        with self._lock:
            self._data.pop(context, None)

    def snapshot(self, context: str) -> dict[str, dict[str, Any]]:
        """Deep copy as ``{binding: {name: value, slot: {name: value}}}``."""
        with self._lock:
            return {name: binding.snapshot() for name, binding in self._data.get(context, {}).items()}

    def view(self, context: str, declared: Mapping[str, frozenset[str]]) -> Mapping[str, str]:
        """Template lookup of a card; ``declared`` maps a binding to the names its action declared."""
        return _CardView(self, context, declared)

    def lookup(self, context: str, binding: str, slot: str | None, name: str) -> Any:
        """Raw stored value, or ``None`` when unset."""
        with self._lock:
            found = self._data.get(context, {}).get(binding)
            if found is None:
                return None
            values = found.values if slot is None else found.slots.get(slot, {})
            return values.get(name)

    def _binding(self, context: str, binding: str) -> _Binding:
        return self._data.setdefault(context, {}).setdefault(binding, _Binding())


class _CardView(Mapping[str, str]):
    """Mapping whose ``get`` renders ``binding.name`` and ``binding.slot.name``, ``None`` if not a variable."""

    def __init__(self, store: CardVariableStore, context: str, declared: Mapping[str, frozenset[str]]) -> None:
        self._store = store
        self._context = context
        self._declared = declared

    def get(self, key: str, default: Any = None) -> Any:
        parts = key.split(".")
        if len(parts) not in (2, 3) or parts[0] not in self._declared:
            return default
        binding, name = parts[0], parts[-1]
        slot = parts[1] if len(parts) == 3 else None
        if slot is not None and not is_valid_slot(slot):
            return default
        if slot is None and name not in self._declared[binding] | {RESERVED_NAME}:
            return default
        if name != RESERVED_NAME and not is_valid_name(name):
            return default
        return render_value(self._store.lookup(self._context, binding, slot, name))

    def __getitem__(self, key: str) -> str:
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return str(value)

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0
