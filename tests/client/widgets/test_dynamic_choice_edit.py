import json
import threading
import time
from typing import Any

import pytest
from assertpy import assert_that
from PyQt6 import sip
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QComboBox, QToolButton

from decker.client.widgets import dynamic_choice_edit
from decker.client.widgets.dynamic_choice_edit import DynamicChoiceEdit, fetch_options
from decker.client.widgets.schema_form import SchemaForm

_ENTRY: dict[str, Any] = {
    "key": "voice",
    "type": "choice",
    "options_source": "voices",
    "options": [{"label": "Default", "value": ""}, {"label": "Alice", "value": "alice"}],
}
_DYNAMIC = [{"label": "Alice again", "value": "alice"}, {"label": "Bob", "value": "bob"}]
_TIMEOUT = 3.0


def _ok(options: list[dict[str, str]]) -> Any:
    return lambda _plugin, _source: (True, options, "")


def _items(edit: DynamicChoiceEdit) -> list[tuple[str, Any]]:
    combo = edit.findChild(QComboBox)
    assert combo is not None
    return [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]


def _until(condition: Any) -> None:
    deadline = time.monotonic() + _TIMEOUT
    while not condition():
        assert time.monotonic() < deadline, "timed out waiting for the condition"
        QApplication.processEvents()
        time.sleep(0.005)


def _wait_loaded(edit: DynamicChoiceEdit) -> None:
    _until(lambda: ("Bob", "bob") in _items(edit) or edit._error != "")


def _join(edit: DynamicChoiceEdit) -> None:
    assert edit._thread is not None
    edit._thread.join(5)
    assert_that(edit._thread.is_alive()).is_false()


class TestDynamicChoiceEdit:  # pylint: disable=protected-access
    def test_static_options_show_first_then_dynamic_without_repeats(self, qapp):  # pylint: disable=unused-argument
        edit = DynamicChoiceEdit(_ENTRY, "p", _ok(_DYNAMIC))
        _wait_loaded(edit)
        assert_that(_items(edit)).is_equal_to([("Default", ""), ("Alice", "alice"), ("Bob", "bob")])

    def test_stored_value_not_in_the_list_stays_selected(self, qapp):  # pylint: disable=unused-argument
        edit = DynamicChoiceEdit(_ENTRY, "p", _ok(_DYNAMIC))
        edit.set_value("gone")
        _wait_loaded(edit)
        assert_that(edit.value()).is_equal_to("gone")
        assert_that(edit.findChild(QComboBox).toolTip()).is_equal_to("unavailable")
        edit.set_value("bob")
        assert_that(edit.value()).is_equal_to("bob")
        assert_that(edit.findChild(QComboBox).toolTip()).is_equal_to("")

    def test_typing_a_value_is_returned_by_value(self, qapp):  # pylint: disable=unused-argument
        edit = DynamicChoiceEdit(_ENTRY, "p", _ok(_DYNAMIC))
        _wait_loaded(edit)
        combo = edit.findChild(QComboBox)
        seen: list[int] = []
        edit.value_changed.connect(lambda: seen.append(1))
        combo.setEditText("custom-slug")
        assert_that(seen).is_length(1)
        assert_that(edit.value()).is_equal_to("custom-slug")
        combo.setEditText("Bob")
        assert_that(edit.value()).is_equal_to("bob")

    def test_failure_keeps_static_options_and_current_value_and_sets_tooltip(
        self, qapp, caplog
    ):  # pylint: disable=unused-argument
        edit = DynamicChoiceEdit(_ENTRY, "p", lambda _p, _s: (False, [], "plugin is down"))
        edit.set_value("kept")
        _until(lambda: edit._error != "")
        assert_that(_items(edit)).is_equal_to([("Default", ""), ("Alice", "alice")])
        assert_that(edit.value()).is_equal_to("kept")
        assert_that(edit.findChild(QComboBox).toolTip()).is_equal_to("plugin is down")
        assert_that(edit.findChild(QToolButton).toolTip()).is_equal_to("plugin is down")
        assert_that([r for r in caplog.records if r.levelname == "WARNING"]).is_length(1)

    def test_a_raising_fetch_is_a_failure(self, qapp):  # pylint: disable=unused-argument
        def boom(_p: str, _s: str) -> Any:
            raise OSError("boom")

        edit = DynamicChoiceEdit(_ENTRY, "p", boom)
        _until(lambda: edit._error == "boom")

    def test_loading_emits_no_value_changed(self, qapp):  # pylint: disable=unused-argument
        edit = DynamicChoiceEdit(_ENTRY, "p", _ok(_DYNAMIC))
        seen: list[int] = []
        edit.value_changed.connect(lambda: seen.append(1))
        edit.set_value("alice")
        _wait_loaded(edit)
        assert_that(seen).is_empty()
        assert_that(edit.value()).is_equal_to("alice")

    def test_refresh_button_fetches_again(self, qapp):  # pylint: disable=unused-argument
        calls: list[tuple[str, str]] = []

        def fetch(plugin: str, source: str) -> Any:
            calls.append((plugin, source))
            return True, [], ""

        edit = DynamicChoiceEdit(_ENTRY, "p", fetch)
        _until(lambda: len(calls) == 1)
        edit.findChild(QToolButton).click()
        _until(lambda: len(calls) == 2)
        assert_that(calls).is_equal_to([("p", "voices"), ("p", "voices")])
        _join(edit)

    def test_stale_result_after_a_newer_reload_is_dropped(self, qapp):  # pylint: disable=unused-argument
        release = threading.Event()
        answers = [[{"label": "Old", "value": "old"}], _DYNAMIC]

        def fetch(_p: str, _s: str) -> Any:
            mine = answers.pop(0)
            if mine is not _DYNAMIC:
                release.wait(5)  # the first request finishes last
            return True, mine, ""

        edit = DynamicChoiceEdit(_ENTRY, "p", fetch)
        first = edit._thread
        edit.reload()
        _wait_loaded(edit)
        release.set()
        assert first is not None
        first.join(5)
        QApplication.processEvents()
        assert_that(_items(edit)).does_not_contain(("Old", "old"))
        _join(edit)

    def test_result_after_the_widget_is_deleted_does_not_raise(self, qapp, recwarn):  # pylint: disable=unused-argument
        release = threading.Event()

        def fetch(_p: str, _s: str) -> Any:
            release.wait(5)
            return True, _DYNAMIC, ""

        edit = DynamicChoiceEdit(_ENTRY, "p", fetch)
        thread = edit._thread
        assert thread is not None
        sip.delete(edit)
        release.set()
        thread.join(5)
        assert_that(thread.is_alive()).is_false()
        QApplication.processEvents()
        assert_that(list(recwarn)).is_empty()

    def test_form_without_plugin_id_falls_back_to_static_options_and_typing(
        self, qapp, monkeypatch
    ):  # pylint: disable=unused-argument
        monkeypatch.setattr(dynamic_choice_edit, "_default_fetch", lambda: pytest.fail("must not fetch"))
        form = SchemaForm()
        form.set_schema([_ENTRY], {"voice": "alice"})
        edit = form._fields["voice"].widget
        assert isinstance(edit, DynamicChoiceEdit)
        assert_that(form.values()).is_equal_to({"voice": "alice"})
        edit.findChild(QComboBox).setEditText("typed")
        assert_that(form.values()).is_equal_to({"voice": "typed"})


_TWINS = [{"label": "Alice", "value": "a1"}, {"label": "Alice", "value": "a2"}]


def _twins() -> DynamicChoiceEdit:
    edit = DynamicChoiceEdit({"key": "k", "type": "choice", "options_source": "s"}, "p", _ok(_TWINS))
    _until(lambda: edit.findChild(QComboBox).count() == 2)
    return edit


def _pick(edit: DynamicChoiceEdit, index: int) -> None:
    combo = edit.findChild(QComboBox)
    combo.setCurrentIndex(index)
    combo.activated.emit(index)


class TestLiteralAndDuplicates:  # pylint: disable=protected-access
    def test_enter_on_typed_text_keeps_it_and_emits_once(self, qapp):  # pylint: disable=unused-argument
        edit = DynamicChoiceEdit(_ENTRY, "p", _ok([]))
        _join(edit)
        combo = edit.findChild(QComboBox)
        seen: list[str] = []
        edit.value_changed.connect(lambda: seen.append(edit.value()))
        line = combo.lineEdit()
        assert line is not None
        line.setText("custom")
        QTest.keyClick(line, Qt.Key.Key_Return)  # type: ignore[call-overload]  # PyQt6 stub declares self
        assert_that(edit.value()).is_equal_to("custom")
        assert_that(combo.count()).is_equal_to(2)
        assert_that(seen).is_equal_to(["custom"])

    def test_same_label_options_return_their_own_values(self, qapp):  # pylint: disable=unused-argument
        edit = _twins()
        seen: list[str] = []
        edit.value_changed.connect(lambda: seen.append(edit.value()))
        _pick(edit, 1)
        assert_that(edit.value()).is_equal_to("a2")
        _pick(edit, 0)
        assert_that(edit.value()).is_equal_to("a1")
        assert_that(seen).is_equal_to(["a2", "a1"])

    def test_stored_unlisted_value_equal_to_a_label_stays_as_stored(self, qapp):  # pylint: disable=unused-argument
        entry = {"key": "k", "type": "choice", "options_source": "s", "options": [{"label": "Alice", "value": "a1"}]}
        edit = DynamicChoiceEdit(entry, "p", _ok([{"label": "Zed", "value": "z"}]))
        edit.set_value("Alice")
        assert_that(edit.value()).is_equal_to("Alice")
        _until(lambda: ("Zed", "z") in _items(edit))
        assert_that(edit.value()).is_equal_to("Alice")
        _pick(edit, 0)
        assert_that(edit.value()).is_equal_to("a1")

    def test_stored_value_stays_literal_when_a_fetch_lists_its_label(self, qapp):  # pylint: disable=unused-argument
        edit = DynamicChoiceEdit(_ENTRY, "p", _ok([{"label": "Carol", "value": "c1"}]))
        edit.set_value("Carol")
        _until(lambda: ("Carol", "c1") in _items(edit))
        assert_that(edit.value()).is_equal_to("Carol")

    def test_refresh_button_is_hidden_without_a_source(self, qapp):  # pylint: disable=unused-argument
        edit = DynamicChoiceEdit({"key": "k", "type": "choice", "options": []}, "")
        assert_that(edit.findChild(QToolButton).isHidden()).is_true()
        live = DynamicChoiceEdit(_ENTRY, "p", _ok([]))
        assert_that(live.findChild(QToolButton).isHidden()).is_false()
        _join(live)


class TestFetchOptions:
    def test_fetch_options_helper_parses_the_rpc_and_never_raises(self) -> None:
        class Rpc:
            def __init__(self, reply: Any) -> None:
                self.reply = reply
                self.args: tuple[Any, ...] = ()

            def call(self, method: str, *args: Any) -> Any:
                self.args = (method, *args)
                if isinstance(self.reply, Exception):
                    raise self.reply
                return self.reply

        good = Rpc(
            json.dumps(
                {"ok": True, "options": [{"label": "A", "value": "a"}, {"label": 1, "value": "x"}, "junk"], "error": ""}
            )
        )
        assert_that(fetch_options(good, "p", "s")).is_equal_to((True, [{"label": "A", "value": "a"}], ""))
        assert_that(good.args).is_equal_to(("plugin_options", "p", "s"))
        assert_that(fetch_options(None, "p", "s")).is_equal_to((False, [], "not connected"))
        assert_that(fetch_options(Rpc("not json"), "p", "s")[0]).is_false()
        assert_that(fetch_options(Rpc(None), "p", "s")[0]).is_false()
        assert_that(fetch_options(Rpc(OSError("down")), "p", "s")[2]).contains("down")
        refused = Rpc(json.dumps({"ok": False, "options": [], "error": "nope"}))
        assert_that(fetch_options(refused, "p", "s")).is_equal_to((False, [], "nope"))
