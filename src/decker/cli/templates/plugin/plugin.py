"""A minimal Decker plugin: a card whose press runs the plugin's hello action."""

from decker_plugin_sdk import ActionCallContext, DeckerPlugin, action, card


@card("{{card_id}}")
class Hello(DeckerPlugin):
    """A card bound to the hello action in the manifest."""


@action("{{plugin_id}}.hello")
def hello(ctx: ActionCallContext) -> None:
    if ctx.card is not None:
        ctx.card.show_ok()
