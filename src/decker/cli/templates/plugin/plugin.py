"""A minimal Decker plugin: a card whose press runs the plugin's hello action."""

import sys
from typing import Any

from decker_plugin_sdk import DeckerPlugin, action, card


@card("{{card_id}}")
class Hello(DeckerPlugin):
    """A card bound to the hello action in the manifest."""


@action("{{plugin_id}}.hello")
def hello(settings: dict[str, Any]) -> None:
    print("Hello from {{plugin_id}}!", file=sys.stderr)
