"""Action registration and the context an action receives."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, TypeVar

from ._proxy import CardProxy


class Input(StrEnum):
    """What triggered an action."""

    PRESS = "press"
    DOUBLE_PRESS = "double_press"
    LONG_PRESS = "long_press"
    PLUGIN = "plugin"


@dataclass(frozen=True)
class ActionCallContext:  # pylint: disable=too-many-instance-attributes
    """Everything an action function is told about its call."""

    action: str
    request: str
    called_at: datetime
    input: Input
    card: CardProxy | None
    args: dict[str, Any]
    caller: str | None
    _chain: str = ""
    _depth: int = 0
    _device: str | None = None


ActionFn = Callable[[ActionCallContext], Any]
_F = TypeVar("_F", bound=ActionFn)
_ACTIONS: dict[str, ActionFn] = {}


def action(action_id: str) -> "Callable[[_F], _F]":
    """Register ``fn(ctx: ActionCallContext)`` under a manifest action id; it runs when the action does."""

    def decorate(fn: _F) -> _F:
        _ACTIONS[action_id] = fn
        return fn

    return decorate


def registered_actions() -> dict[str, ActionFn]:
    """The actions registered so far with @action."""
    return dict(_ACTIONS)
