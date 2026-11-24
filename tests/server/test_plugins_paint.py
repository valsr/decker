import base64
import io
from datetime import datetime, timezone
from typing import Any

import pytest
from assertpy import assert_that
from PIL import Image

from decker.plugin.protocol import Message
from tests.server.test_plugins import _COUNTER, REC_TARGET, Env
from tests.server.test_plugins_bindings import _in_flight

_REC = "com.decker.rec"


@pytest.fixture(name="env")
def env_fixture(tmp_path):
    environment = Env(tmp_path)
    yield environment
    environment.manager.destroy()


def _b64(color: str | None = "#00ff00", size: int = 8) -> str:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if color is not None:
        image.paste(Image.new("RGBA", (size // 2, size // 2), color), (0, 0))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _sent(env: Env) -> list[Message]:
    sent: list[Message] = []
    env.manager._supervisor.send = lambda _plugin, message: sent.append(message)  # type: ignore[assignment,method-assign]
    return sent


def _own(env: Env, event: str, payload: dict[str, Any]) -> None:
    env.manager._route_message(_REC, Message(event, REC_TARGET.context_id, payload))  # pylint: disable=protected-access


def _foreign(env: Env, event: str, payload: dict[str, Any]) -> None:
    env.manager._route_message(
        _COUNTER, Message(event, REC_TARGET.context_id, payload)
    )  # pylint: disable=protected-access


def _decode(message: Message) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(message.payload["image"]))).convert("RGBA")


def _live(env: Env) -> Any:
    return env.manager._live[REC_TARGET.context_id]  # pylint: disable=protected-access


class TestLastPaint:
    def test_last_paint_recorded_on_set_image(self, env) -> None:
        env.manager.activate_card(REC_TARGET)
        assert_that(_live(env).last_paint).is_none()
        before = datetime.now(timezone.utc)

        _own(env, "set_image", {"image": _b64()})

        assert_that(_live(env).last_paint).is_between(before, datetime.now(timezone.utc))

    def test_rejected_set_image_does_not_count_as_a_paint(self, env) -> None:
        env.manager.activate_card(REC_TARGET)

        _own(env, "set_image", {"image": "!!not base64!!"})

        assert_that(_live(env).last_paint).is_none()

    def test_run_action_card_payload_carries_last_paint(self, env) -> None:
        env.manager.activate_card(REC_TARGET)
        payload = env.manager._card_payload(_live(env))  # pylint: disable=protected-access
        assert_that(payload["last_paint"]).is_none()

        _own(env, "set_image", {"image": _b64()})
        payload = env.manager._card_payload(_live(env))  # pylint: disable=protected-access

        assert_that(datetime.fromisoformat(payload["last_paint"]).tzinfo).is_not_none()


class TestGetImage:
    def test_get_image_returns_own_plugin_layer_as_png_at_key_size(self, env) -> None:
        env.manager.activate_card(REC_TARGET)
        _own(env, "set_image", {"image": _b64("#00ff00")})
        sent = _sent(env)

        _own(env, "get_image", {"request": "r1", "context": REC_TARGET.context_id})

        reply = sent[0]
        assert_that(reply.event).is_equal_to("image_result")
        assert_that(reply.payload["request"]).is_equal_to("r1")
        assert_that(reply.payload["format"]).is_equal_to("png")
        image = _decode(reply)
        assert_that(image.size).is_equal_to(env.manager._key_size(REC_TARGET))  # pylint: disable=protected-access
        assert_that(image.getpixel((2, 2))).is_equal_to((0, 255, 0, 255))
        assert_that(image.getchannel("A").getpixel((image.width - 2, image.height - 2))).is_equal_to(0)

    def test_get_image_excludes_background_and_title(self, env) -> None:
        env.manager.activate_card(REC_TARGET, {"background": {"type": "color", "color": "#ff0000"}})
        _own(env, "set_title", {"title": "Hello"})
        _own(env, "set_image", {"image": _b64(None)})
        sent = _sent(env)

        _own(env, "get_image", {"request": "r1", "context": REC_TARGET.context_id})

        assert_that(_decode(sent[0]).getchannel("A").getextrema()).is_equal_to((0, 0))

    def test_get_image_without_a_layer_answers_null(self, env) -> None:
        env.manager.activate_card(REC_TARGET)
        sent = _sent(env)

        _own(env, "get_image", {"request": "r1", "context": REC_TARGET.context_id})

        assert_that(sent[0].payload).is_equal_to({"request": "r1", "image": None, "format": "png"})

    def test_get_image_for_foreign_context_needs_an_in_flight_call(self, env) -> None:
        env.manager.activate_card(REC_TARGET)
        _own(env, "set_image", {"image": _b64()})
        sent = _sent(env)
        context = REC_TARGET.context_id

        _foreign(env, "get_image", {"request": "r1", "context": context})
        _foreign(env, "get_image", {"request": "r2", "context": context, "call": "req-1"})
        _in_flight(env)
        _foreign(env, "get_image", {"request": "r3", "context": context, "call": "req-1"})
        _foreign(env, "get_image", {"request": "r4", "context": context, "call": "other"})

        images = {m.payload["request"]: m.payload["image"] for m in sent}
        assert_that(images["r1"]).is_none()
        assert_that(images["r2"]).is_none()
        assert_that(images["r3"]).is_not_none()
        assert_that(images["r4"]).is_none()

    @pytest.mark.parametrize(
        "payload", [{"request": "r1", "context": 5}, {"request": "r1", "context": "x", "call": 7}, {"request": "r1"}]
    )
    def test_malformed_context_or_call_answers_null(self, env, payload) -> None:
        sent = _sent(env)

        _own(env, "get_image", payload)

        assert_that(sent).is_length(1)
        assert_that(sent[0].payload["image"]).is_none()

    def test_get_image_without_a_string_request_is_dropped(self, env) -> None:
        sent = _sent(env)

        _own(env, "get_image", {"request": 5, "context": REC_TARGET.context_id})
        _own(env, "get_image", {"context": REC_TARGET.context_id})

        assert_that(sent).is_empty()

    def test_rendering_failure_answers_null(self, env, monkeypatch) -> None:
        env.manager.activate_card(REC_TARGET)
        _own(env, "set_image", {"image": _b64()})
        sent = _sent(env)

        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr("decker.server.plugins.AssetRenderer.render_bytes", boom)
        _own(env, "get_image", {"request": "r1", "context": REC_TARGET.context_id})

        assert_that(sent[0].payload["image"]).is_none()


class TestForeignPaint:
    def test_foreign_set_image_accepted_during_call(self, env) -> None:
        env.manager.activate_card(REC_TARGET)
        _in_flight(env)

        _foreign(env, "set_image", {"image": _b64(), "request": "req-1"})

        assert_that(_live(env).plugin_image).is_not_none()
        assert_that(_live(env).last_paint).is_not_none()

    def test_late_foreign_set_image_after_result_is_dropped(self, env) -> None:
        env.manager.activate_card(REC_TARGET)
        _in_flight(env)
        env.manager._action_runner.on_result(
            _COUNTER, {"request": "req-1", "ok": True}
        )  # pylint: disable=protected-access

        _foreign(env, "set_image", {"image": _b64(), "request": "req-1"})

        assert_that(_live(env).plugin_image).is_none()
        assert_that(_live(env).last_paint).is_none()


class TestForeignFeedback:
    def test_foreign_show_ok_and_alert_accepted_during_call_and_dropped_without(self, env) -> None:
        env.manager.activate_card(REC_TARGET)
        shown: list[str] = []
        sink = env.manager._sink  # pylint: disable=protected-access
        sink.show_ok = lambda *_a: shown.append("ok")
        sink.show_alert = lambda *_a: shown.append("alert")

        _foreign(env, "show_ok", {"request": "req-1"})
        _foreign(env, "show_alert", {"request": "req-1"})
        assert_that(shown).is_empty()

        _in_flight(env)
        _foreign(env, "show_ok", {"request": "req-1"})
        _foreign(env, "show_alert", {"request": "req-1"})

        assert_that(shown).is_equal_to(["ok", "alert"])
