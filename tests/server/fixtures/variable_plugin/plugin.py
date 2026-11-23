"""Test-only plugin: a self-incrementing counter, a per-item value and a getter that can be made slow."""

import threading
import time
from pathlib import Path

from decker_plugin_sdk import (
    ActionCallContext,
    PluginContext,
    action,
    init,
    main,
    signal_variable_updated,
    variable,
)

_LOCK = threading.Lock()
_COUNTER = [0]
_SLOW_FLAG = Path(__file__).with_name("slow")  # the getter sleeps while this file exists
_TICKS = Path(__file__).with_name("ticks")  # one byte per main tick: lets a test see main stop


def _add(amount: int) -> None:
    with _LOCK:
        _COUNTER[0] += amount


@init
def start(ctx: PluginContext) -> None:
    ctx.register_variable("test_alpha_value")


@main(interval=0.1)
def tick(_ctx: PluginContext) -> None:
    _add(1)
    with _TICKS.open("ab") as ticks:
        ticks.write(b".")
    signal_variable_updated("test_counter")


@variable("test_counter")
def counter() -> int:
    with _LOCK:
        return _COUNTER[0]


@variable("test_slow")
def slow() -> str:
    if _SLOW_FLAG.exists():
        time.sleep(60.0)
    return "done"


@variable("test_{item}_value")
def item_value(item: str) -> str:
    return f"value-of-{item}"


@action("com.decker.vartest.bump")
def bump(_ctx: ActionCallContext) -> None:
    _add(1000)
    signal_variable_updated("test_counter")
