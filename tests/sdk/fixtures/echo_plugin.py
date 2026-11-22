"""Fixture plugin used by the SDK tests."""

import os
import threading
import time

from decker_plugin_sdk import CardContext, DeckerPlugin, action, card

BLOCK = threading.Event()
SEEN: list[str] = []
BINDINGS: list[dict] = []
SIZES: list[tuple[int, int]] = []


@card("test.echo.echo")
class Echo(DeckerPlugin):
    def on_key_down(self, ctx: CardContext) -> None:
        mode = ctx.settings.get("mode")
        if mode == "junk":
            os.system("echo junk-from-child")
            os.write(1, b"junk-from-fd1\n")
            print("junk-from-print")
        if mode == "block":
            BLOCK.wait(5)
        elif mode == "boom":
            raise RuntimeError("boom")
        elif mode == "slow":
            time.sleep(0.2)
        BINDINGS.append(ctx.bindings)
        SIZES.append(ctx.size)
        SEEN.append(str(ctx.settings.get("tag", "")))
        ctx.set_title("pressed")


ACTION_CALLS: list[dict] = []


@action("test.echo.record")
def record(settings: dict) -> None:
    if settings.get("fail"):
        raise ValueError("nope")
    ACTION_CALLS.append(settings)
