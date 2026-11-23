"""Test-only plugin: actions that leave files behind so a test can see them run."""

import json
from pathlib import Path
from typing import Any

from decker_plugin_sdk import action


@action("com.decker.acttest.mark")
def mark(settings: dict[str, Any]) -> None:
    with Path(__file__).with_name("marks.log").open("a", encoding="utf-8") as log:
        log.write(str(settings["tag"]))


@action("com.decker.acttest.public")
def public(settings: dict[str, Any]) -> None:
    Path(__file__).with_name("public.log").write_text(json.dumps(settings), encoding="utf-8")
