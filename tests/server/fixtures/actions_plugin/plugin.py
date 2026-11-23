"""Test-only plugin: actions that leave files behind so a test can see them run."""

import json
from pathlib import Path

from decker_plugin_sdk import ActionCallContext, action


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
