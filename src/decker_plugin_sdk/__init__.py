"""Dependency-free SDK for writing Decker plugins."""

from ._actions import (
    ActionCallContext,
    ActionError,
    Input,
    action,
    call_action,
    call_action_async,
)
from ._canvas import Canvas
from ._context import CardContext
from ._options import options
from ._proxy import CardProxy, OwnCardProxy
from ._runtime import DeckerPlugin, card, run
from ._variables import (
    PluginContext,
    get_variable,
    has_variable,
    init,
    main,
    register_variable,
    signal_variable_updated,
    unregister_variable,
    variable,
)

__all__ = [
    "ActionCallContext",
    "ActionError",
    "Canvas",
    "CardContext",
    "CardProxy",
    "DeckerPlugin",
    "Input",
    "OwnCardProxy",
    "PluginContext",
    "action",
    "call_action",
    "call_action_async",
    "card",
    "get_variable",
    "has_variable",
    "init",
    "main",
    "options",
    "register_variable",
    "run",
    "signal_variable_updated",
    "unregister_variable",
    "variable",
]
