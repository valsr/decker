"""Test-only plugin: actions that leave files behind so a test can see them run."""

import importlib.util
import json
import time
from pathlib import Path
from typing import Any

from decker_plugin_sdk import (
    ActionCallContext,
    ActionError,
    CardContext,
    DeckerPlugin,
    action,
    call_action,
    card,
)


@card("com.decker.acttest.tile")
class Tile(DeckerPlugin):
    def on_will_appear(self, ctx: CardContext) -> None:
        ctx.queue_paint()

    def on_paint(self, ctx: CardContext, img: Any) -> None:  # pylint: disable=unused-argument
        img.paste((255, 0, 0, 255), (0, 0, *img.size))


@card("com.decker.acttest.iconcard")
class IconCard(DeckerPlugin):
    def on_will_appear(self, ctx: CardContext) -> None:
        ctx.set_icon("fas:play", "#00ff00")


@action("com.decker.acttest.mark")
def mark(ctx: ActionCallContext) -> None:
    with Path(__file__).with_name("marks.log").open("a", encoding="utf-8") as log:
        log.write(str(ctx.args["tag"]))


@action("com.decker.acttest.public")
def public(ctx: ActionCallContext) -> None:
    Path(__file__).with_name("public.log").write_text(json.dumps(ctx.args), encoding="utf-8")


@action("com.decker.acttest.nav")
def nav(ctx: ActionCallContext) -> None:
    card = ctx.card
    assert card is not None
    card.go_to_page(ctx.args["page"]["page_id"])
    card.show_ok()


@action("com.decker.acttest.double")
def double(ctx: ActionCallContext) -> int:
    return int(ctx.args["n"]) * 2


@action("com.decker.acttest.chain")
def chain(ctx: ActionCallContext) -> None:  # pylint: disable=unused-argument
    value = call_action("com.decker.acttest.double", {"n": 21})
    Path(__file__).with_name("chain.log").write_text(str(value), encoding="utf-8")


@action("com.decker.acttest.recurse")
def recurse(ctx: ActionCallContext) -> None:
    n = int(ctx.args["n"])
    Path(__file__).with_name("recurse.log").write_text(str(n), encoding="utf-8")
    try:
        call_action("com.decker.acttest.recurse", {"n": n + 1})
    except ActionError as exc:
        Path(__file__).with_name("recurse.err").write_text(str(exc), encoding="utf-8")


@action("com.decker.acttest.relay")
def relay(ctx: ActionCallContext) -> None:  # pylint: disable=unused-argument
    call_action("com.decker.acttest.public", {"page": "p1"})


@action("com.decker.acttest.paint")
def paint(ctx: ActionCallContext) -> None:
    card_proxy = ctx.card
    assert card_proxy is not None
    with card_proxy.canvas.paint() as img:
        width, height = img.size
        img.paste((0, 0, 255, 255), (width // 3, height // 3, width * 2 // 3, height * 2 // 3))


@action("com.decker.acttest.probe")
def probe(ctx: ActionCallContext) -> None:  # pylint: disable=unused-argument
    found = {name: importlib.util.find_spec(name) is not None for name in ("PIL", "psutil", "yaml", "rpyc", "decker")}
    Path(__file__).with_name("probe.log").write_text(json.dumps(found), encoding="utf-8")


@action("com.decker.acttest.step")
def step(ctx: ActionCallContext) -> None:
    with Path(__file__).with_name("steps.log").open("a", encoding="utf-8") as log:
        log.write(str(ctx.args["n"]))
    if ctx.args["fail"]:
        raise ActionError("boom")


@action("com.decker.acttest.next")
def next_page(ctx: ActionCallContext) -> None:
    ctx.switch_page("next")


@action("com.decker.acttest.snooze")
def snooze(ctx: ActionCallContext) -> None:  # pylint: disable=unused-argument
    time.sleep(5)


@action("com.decker.acttest.capped")
def capped(ctx: ActionCallContext) -> None:  # pylint: disable=unused-argument
    started = time.monotonic()
    try:
        call_action("com.decker.acttest.snooze", timeout=60)
        outcome = "finished"
    except ActionError as exc:
        outcome = str(exc)
    elapsed = time.monotonic() - started
    Path(__file__).with_name("capped.log").write_text(f"{elapsed:.1f}|{outcome}", encoding="utf-8")
