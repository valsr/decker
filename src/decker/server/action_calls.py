"""The call every key gesture and plugin request becomes before it reaches the ActionRunner."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

INPUTS: frozenset[str] = frozenset({"press", "double_press", "long_press", "plugin"})
MAX_DEPTH = 8


def _new_chain() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ActionCall:
    """One request to run an action, with where it came from."""

    action: str
    args: Mapping[str, Any]
    input: str
    card: Mapping[str, Any] | None = None
    caller: str | None = None
    chain: str = field(default_factory=_new_chain)
    depth: int = 0
    device: str | None = None
    called_at: datetime = field(default_factory=_now)
    timeout: float | None = None
    parent: str | None = None

    def payload(self, request: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """The ``run_action`` payload for a plugin."""
        return {
            "action": self.action,
            "request": request,
            "args": dict(args),
            "input": self.input,
            "called_at": self.called_at.isoformat(timespec="milliseconds"),
            "card": dict(self.card) if self.card is not None else None,
            "caller": self.caller,
            "chain": self.chain,
            "depth": self.depth,
        }
