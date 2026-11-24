"""Test-only plugin: actions that leave files behind so a test can see them run."""

import json
from pathlib import Path

from decker_plugin_sdk import ActionCallContext, ActionError, action, call_action


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
