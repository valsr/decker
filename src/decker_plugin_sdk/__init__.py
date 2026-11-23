"""Dependency-free SDK for writing Decker plugins."""

from ._actions import ActionCallContext, Input, action
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
    "CardContext",
    "CardProxy",
    "DeckerPlugin",
    "Input",
    "OwnCardProxy",
    "PluginContext",
    "action",
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
