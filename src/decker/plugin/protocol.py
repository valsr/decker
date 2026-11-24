"""Newline-delimited JSON protocol spoken between the server and plugin processes."""

import json
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 5
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_VALUE_BYTES = 1024 * 1024

SERVER_EVENTS: frozenset[str] = frozenset(
    {
        "hello",
        "will_appear",
        "will_disappear",
        "key_down",
        "key_up",
        "settings_changed",
        "global_settings_changed",
        "device_connected",
        "device_disconnected",
        "deck_brightness",
        "deck_brightness_changed",
        "ping",
        "shutdown",
        "get_variable",
        "variables_changed",
        "get_options",
        "run_action",
        "call_result",
        "image_result",
    }
)

PLUGIN_EVENTS: frozenset[str] = frozenset(
    {
        "ready",
        "set_image",
        "set_title",
        "set_state",
        "show_alert",
        "show_ok",
        "set_settings",
        "set_global_settings",
        "log",
        "open_url",
        "pong",
        "register_variable",
        "unregister_variable",
        "variable_updated",
        "variable_value",
        "options_result",
        "action_result",
        "call_action",
        "get_image",
        "switch_page",
        "switch_profile",
        "get_deck_brightness",
        "set_deck_brightness",
    }
)


class ProtocolError(ValueError):
    """Raised when a protocol line cannot be decoded."""


@dataclass(frozen=True)
class Message:
    """One protocol message; `context` is the per-key context id, omitted for plugin-wide events."""

    event: str
    context: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


def encode(message: Message) -> bytes:
    """Serialise a message to a single UTF-8 JSON line terminated by a newline."""
    data: dict[str, Any] = {"event": message.event}
    if message.context is not None:
        data["context"] = message.context
    if message.payload:
        data["payload"] = message.payload
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def decode(line: bytes) -> Message:
    """Parse one protocol line, raising ProtocolError if it is malformed."""
    if len(line) > MAX_LINE_BYTES:
        raise ProtocolError(f"line exceeds {MAX_LINE_BYTES} bytes")
    try:
        data = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProtocolError(f"invalid message: {exc}") from exc
    if not isinstance(data, dict):
        raise ProtocolError("message must be a JSON object")
    event = data.get("event")
    if not isinstance(event, str) or not event:
        raise ProtocolError("message is missing a string 'event'")
    context = data.get("context")
    if context is not None and not isinstance(context, str):
        raise ProtocolError("'context' must be a string")
    payload = data.get("payload", {})
    if not isinstance(payload, dict):
        raise ProtocolError("'payload' must be an object")
    return Message(event, context, payload)
