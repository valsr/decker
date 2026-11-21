"""Test-only plugin: dynamic choice option sources of every shape."""

import asyncio
import os
import time
from pathlib import Path

from decker_plugin_sdk import options

# Written when the process starts: lets a test prove that a plugin was never launched.
Path(__file__).with_name("started").write_text(str(os.getpid()))


@options("devices")
def devices() -> list[object]:
    return [
        {"label": "Speakers", "value": "speakers"},
        ("Headset", "headset"),
        "hdmi",
        {"label": "Speakers again", "value": "speakers"},
        {"label": "L" * 300, "value": "long"},
    ]


@options("actions_list")
async def actions_list() -> list[object]:
    await asyncio.sleep(0.01)
    return [("First", "first"), ("Second", "second")]


@options("globals")
def globals_() -> list[str]:
    return ["eu", "us"]


@options("slow")
def slow() -> list[str]:
    time.sleep(60.0)
    return ["never"]


@options("boom")
def boom() -> list[str]:  # registered but undeclared in the manifest: the server must refuse it first
    return ["boom"]


@options("raises")
def raises() -> list[str]:
    raise RuntimeError("the source broke")
