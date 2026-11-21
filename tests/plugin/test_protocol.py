import json

import pytest
from assertpy import assert_that

from decker.plugin.protocol import (
    MAX_LINE_BYTES,
    PLUGIN_EVENTS,
    PROTOCOL_VERSION,
    SERVER_EVENTS,
    Message,
    ProtocolError,
    decode,
    encode,
)


def test_round_trip() -> None:
    message = Message("press", "ctx1", {"a": [1, 2], "b": "é"})
    assert_that(decode(encode(message))).is_equal_to(message)


def test_round_trip_minimal() -> None:
    data = encode(Message("ping"))
    assert_that(json.loads(data)).is_equal_to({"event": "ping"})
    assert_that(decode(data)).is_equal_to(Message("ping"))


def test_encode_is_single_line_even_with_newlines_in_payload() -> None:
    data = encode(Message("log", payload={"message": "a\nb\r\nc"}))
    assert_that(data.endswith(b"\n")).is_true()
    assert_that(data.count(b"\n")).is_equal_to(1)


def test_decode_rejects_non_json() -> None:
    with pytest.raises(ProtocolError):
        decode(b"not json\n")


def test_decode_rejects_non_utf8() -> None:
    with pytest.raises(ProtocolError):
        decode(b'{"event": "\xff"}\n')


def test_decode_rejects_non_object() -> None:
    with pytest.raises(ProtocolError):
        decode(b"[1]\n")


def test_decode_rejects_missing_event() -> None:
    with pytest.raises(ProtocolError):
        decode(b'{"payload": {}}\n')


def test_decode_rejects_bad_context() -> None:
    with pytest.raises(ProtocolError):
        decode(b'{"event": "x", "context": 3}\n')


def test_decode_rejects_non_object_payload() -> None:
    with pytest.raises(ProtocolError):
        decode(b'{"event": "x", "payload": [1]}\n')


def test_decode_rejects_oversized_line() -> None:
    with pytest.raises(ProtocolError):
        decode(b" " * (MAX_LINE_BYTES + 1))


def test_protocol_version() -> None:
    assert_that(PROTOCOL_VERSION).is_equal_to(3)


def test_event_sets_match_spec() -> None:
    assert_that(SERVER_EVENTS).is_equal_to(
        frozenset(
            {
                "hello",
                "will_appear",
                "will_disappear",
                "key_down",
                "key_up",
                "press",
                "double_press",
                "long_press",
                "settings_changed",
                "global_settings_changed",
                "device_connected",
                "device_disconnected",
                "ping",
                "shutdown",
                "get_variable",
                "variables_changed",
                "get_options",
            }
        )
    )
    assert_that(PLUGIN_EVENTS).is_equal_to(
        frozenset(
            {
                "ready",
                "set_image",
                "set_title",
                "set_state",
                "show_alert",
                "show_ok",
                "set_settings",
                "set_global_settings",
                "set_gestures",
                "log",
                "open_url",
                "pong",
                "register_variable",
                "unregister_variable",
                "variable_updated",
                "variable_value",
                "options_result",
            }
        )
    )


def test_decode_rejects_deeply_nested_input() -> None:
    with pytest.raises(ProtocolError):
        decode(b"[" * 200000)
