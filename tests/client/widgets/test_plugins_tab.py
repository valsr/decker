"""Tests for decker.client.widgets.plugins_tab."""

import base64
import json
import threading
from unittest.mock import MagicMock, patch

import pytest
from assertpy import assert_that
from PyQt6.QtWidgets import QMessageBox

from decker.client.context import AppContext
from decker.client.events import (
    PluginInstallFinishedEvent,
    PluginInstallProgressEvent,
    PluginsChangedEvent,
    PluginUpdatesAvailableEvent,
)
from decker.client.events.event_manager import EventManager
from decker.client.widgets import dynamic_choice_edit, plugins_tab
from decker.client.widgets.dynamic_choice_edit import DynamicChoiceEdit
from decker.client.widgets.plugins_tab import PluginsTab, install_package_file

# pylint: disable=protected-access,redefined-outer-name,unused-argument

_PLUGINS = [
    {
        "id": "com.decker.core",
        "name": "Core",
        "version": "1.0.0",
        "author": "Decker",
        "description": "",
        "actions": 2,
        "icon_packs": 1,
        "icon_b64": base64.b64encode(b"").decode(),
        "global_settings_schema": [],
        "global_settings": {},
    },
    {
        "id": "com.acme.extra",
        "name": "Extra",
        "version": "2.1.0",
        "author": "Acme",
        "description": "Extra things",
        "actions": 1,
        "icon_packs": 0,
        "variables": 2,
        "icon_b64": "",
        "global_settings_schema": [
            {"key": "token", "type": "text", "label": "Token", "default": ""},
            {"key": "voice", "type": "choice", "label": "Voice", "options_source": "voices", "options": []},
        ],
        "global_settings": {"token": "abc", "voice": ""},
    },
]


@pytest.fixture
def manager(qapp):
    AppContext.reset()
    EventManager.reset()
    cm = MagicMock()
    answers = {
        "plugins_list": json.dumps(_PLUGINS),
        "plugin_install_allowed": True,
        "plugin_usage": 3,
        "plugin_uninstall": True,
        "plugin_update_global_settings": True,
        "plugin_install": "job-1",
    }
    cm.call.side_effect = lambda method, *args: answers.get(method)
    cm.answers = answers
    AppContext.get().connection_manager = cm
    yield cm
    AppContext.reset()


@pytest.fixture
def tab(manager):
    widget = PluginsTab()
    widget.refresh()
    return widget


def _select(tab, plugin_id):
    for row in range(tab._plugin_list.count()):
        item = tab._plugin_list.item(row)
        if item.data(plugins_tab._PLUGIN_ID_ROLE) == plugin_id:
            tab._plugin_list.setCurrentItem(item)


def test_lists_plugins_with_contribution_summary(tab):
    texts = [tab._plugin_list.item(i).text() for i in range(tab._plugin_list.count())]

    assert_that(texts).is_length(2)
    assert_that(texts[0]).contains("Core", "v1.0.0", "Decker", "2 actions · 1 icon pack")
    assert_that(texts[1]).contains("Extra", "Acme", "1 action · 2 variables")


def test_uninstall_confirms_with_usage_count_and_calls_rpc(tab, manager):
    _select(tab, "com.acme.extra")
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as question:
        tab._uninstall_btn.click()

    assert_that(question.call_args.args[2]).is_equal_to("Extra is used on 3 keys. Uninstall it and clear those keys?")
    manager.call.assert_any_call("plugin_uninstall", "com.acme.extra")


def test_uninstall_declined_does_not_call_rpc(tab, manager):
    _select(tab, "com.acme.extra")
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
        tab._uninstall_btn.click()

    assert_that([c.args[0] for c in manager.call.call_args_list]).does_not_contain("plugin_uninstall")


def test_uninstall_enabled_for_selected_plugin(tab):
    assert_that(tab._uninstall_btn.isEnabled()).is_false()
    _select(tab, "com.acme.extra")
    assert_that(tab._uninstall_btn.isEnabled()).is_true()


def test_uninstall_button_is_disabled_for_builtin_plugins(manager):
    manager.answers["plugins_list"] = json.dumps([{**_PLUGINS[0], "builtin": True}, _PLUGINS[1]])
    tab = PluginsTab()
    tab.refresh()

    _select(tab, "com.decker.core")

    assert_that(tab._uninstall_btn.isEnabled()).is_false()
    assert_that(tab._uninstall_btn.toolTip()).contains("built into Decker")
    _select(tab, "com.acme.extra")
    assert_that(tab._uninstall_btn.isEnabled()).is_true()


def test_install_button_disabled_when_not_allowed(manager):
    manager.answers["plugin_install_allowed"] = False
    tab = PluginsTab()
    tab.refresh()

    assert_that(tab._install_btn.isEnabled()).is_false()
    assert_that(tab._install_btn.toolTip()).is_equal_to(
        "Installing plugins is only allowed from the server's own machine"
    )


def test_install_progress_and_result_shown(tab):
    EventManager.get().emit(PluginInstallProgressEvent(job_id="j", message="Installing requirements"))
    assert_that(tab._status_label.text()).is_equal_to("Installing requirements")

    EventManager.get().emit(
        PluginInstallFinishedEvent(
            job_id="j", ok=True, plugin_id="com.acme.extra", version="2.1.0", previous_version="2.0.0", message=""
        )
    )
    assert_that(tab._status_label.text()).contains("2.0.0", "2.1.0")

    EventManager.get().emit(
        PluginInstallFinishedEvent(
            job_id="k", ok=False, plugin_id="", version="", previous_version="", message="bad manifest"
        )
    )
    assert_that(tab._status_label.text()).contains("bad manifest")


def test_plugins_changed_refreshes_list(tab, manager):
    manager.answers["plugins_list"] = json.dumps(_PLUGINS[:1])

    EventManager.get().emit(PluginsChangedEvent())

    assert_that(tab._plugin_list.count()).is_equal_to(1)


def test_global_settings_form_applies(tab, manager):
    _select(tab, "com.decker.core")
    assert_that(tab._apply_settings_btn.isHidden()).is_true()
    _select(tab, "com.acme.extra")
    assert_that(tab._apply_settings_btn.isHidden()).is_false()
    assert_that(tab._settings_form.values()).is_equal_to({"token": "abc", "voice": ""})

    tab._apply_settings_btn.click()

    manager.call.assert_any_call(
        "plugin_update_global_settings", "com.acme.extra", json.dumps({"token": "abc", "voice": ""})
    )


def test_install_button_uses_file_dialog_and_confirms(tab, manager, tmp_path):
    package = tmp_path / "x.deckerplugin"
    package.write_bytes(b"zip")
    with (
        patch.object(plugins_tab.QFileDialog, "getOpenFileName", return_value=(str(package), "")),
        patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as question,
    ):
        tab._install_btn.click()

    assert_that(question.call_args.args[2]).contains(plugins_tab.TRUST_NOTICE)
    assert_that(tab._status_label.text()).is_equal_to("Uploading x.deckerplugin…")
    _join_uploads()
    manager.call.assert_any_call("plugin_install", "x.deckerplugin", b"zip")


def _join_uploads():
    for thread in threading.enumerate():
        if thread is not threading.current_thread() and thread.daemon:
            thread.join(2)
    EventManager.get().drain_and_dispatch()


def test_uninstall_disabled_when_not_allowed(manager):
    manager.answers["plugin_install_allowed"] = False
    tab = PluginsTab()
    tab.refresh()
    _select(tab, "com.acme.extra")

    assert_that(tab._uninstall_btn.isEnabled()).is_false()
    assert_that(tab._uninstall_btn.toolTip()).is_equal_to(plugins_tab.NOT_ALLOWED_TOOLTIP)


def test_uninstall_failure_mentions_same_machine_rule(tab, manager):
    manager.answers["plugin_uninstall"] = None
    _select(tab, "com.acme.extra")
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
        tab._uninstall_btn.click()

    assert_that(tab._status_label.text()).contains("server's own machine")


def test_unknown_usage_does_not_claim_zero_keys(tab, manager):
    manager.answers["plugin_usage"] = None
    _select(tab, "com.acme.extra")
    with patch.object(QMessageBox, "question") as question:
        tab._uninstall_btn.click()

    question.assert_not_called()
    assert_that(tab._status_label.text()).is_equal_to("Couldn't check where Extra is used; try again.")
    assert_that([c.args[0] for c in manager.call.call_args_list]).does_not_contain("plugin_uninstall")


class TestInstallPackageFile:
    def test_declined_sends_nothing(self, manager, tmp_path):
        package = tmp_path / "x.deckerplugin"
        package.write_bytes(b"zip")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            install_package_file(package)

        assert_that([c.args[0] for c in manager.call.call_args_list]).does_not_contain("plugin_install")

    def test_oversize_refused(self, manager, tmp_path):
        package = tmp_path / "x.deckerplugin"
        package.write_bytes(b"zip")
        with (
            patch.object(plugins_tab, "MAX_PACKAGE_BYTES", 1),
            patch.object(QMessageBox, "warning") as warning,
            patch.object(QMessageBox, "question") as question,
        ):
            install_package_file(package)

        warning.assert_called_once()
        question.assert_not_called()

    def test_upload_runs_off_ui_thread_and_reports_status_first(self, manager, tmp_path):
        seen: dict[str, threading.Thread] = {}
        statuses = []
        EventManager.get().subscribe(PluginInstallProgressEvent, lambda e: statuses.append(e.message))
        manager.call.side_effect = lambda method, *a: seen.setdefault(method, threading.current_thread()) and "job"
        package = tmp_path / "x.deckerplugin"
        package.write_bytes(b"zip")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            worker = install_package_file(package)

        assert_that(statuses).is_equal_to(["Uploading x.deckerplugin…"])
        assert worker is not None
        worker.join(2)
        assert_that(seen["plugin_install"]).is_not_same_as(threading.current_thread())

    def test_server_refusal_reported_with_same_machine_hint(self, manager, tmp_path):
        manager.answers["plugin_install"] = None
        finished = []
        EventManager.get().subscribe(PluginInstallFinishedEvent, lambda e: finished.append(e))
        package = tmp_path / "x.deckerplugin"
        package.write_bytes(b"zip")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            worker = install_package_file(package)
        assert worker is not None
        worker.join(2)
        EventManager.get().drain_and_dispatch()

        assert_that(finished).is_length(1)
        assert_that(finished[0].ok).is_false()
        assert_that(finished[0].message).contains("server's own machine")

    def test_no_connection_warns(self, qapp, tmp_path):
        AppContext.reset()
        with patch.object(QMessageBox, "warning") as warning:
            install_package_file(tmp_path / "x.deckerplugin")

        warning.assert_called_once()

    def test_unreadable_file_warns(self, manager, tmp_path):
        with patch.object(QMessageBox, "warning") as warning:
            install_package_file(tmp_path / "missing.deckerplugin")

        warning.assert_called_once()


def test_get_more_button_opens_single_dialog(manager, qapp):
    tab = PluginsTab()
    tab._get_more_btn.click()
    first = tab._get_more_dialog
    tab._get_more_btn.click()

    assert_that(first).is_not_none()
    assert_that(tab._get_more_dialog).is_same_as(first)
    assert_that(first.isModal()).is_false()
    first.close()


def test_plugins_tab_badges_updates(tab):
    update = {"id": "com.acme.extra", "name": "Extra", "installed": "2.1.0", "available": "2.2.0"}

    EventManager.get().emit(PluginUpdatesAvailableEvent(updates=[update], missing=[]))

    texts = {
        tab._plugin_list.item(i).data(plugins_tab._PLUGIN_ID_ROLE): tab._plugin_list.item(i).text() for i in range(2)
    }
    assert_that(texts["com.acme.extra"]).contains("Update available: 2.2.0")
    assert_that(texts["com.decker.core"]).does_not_contain("Update available")

    EventManager.get().emit(PluginUpdatesAvailableEvent(updates=[], missing=[]))
    assert_that(tab._plugin_list.item(0).text()).does_not_contain("Update available")
    assert_that(tab._plugin_list.item(1).text()).does_not_contain("Update available")


def test_installed_plugin_rows_show_the_badge(manager):
    host = {"os": "linux", "desktop": "kde", "session": "wayland", "label": "Linux (KDE, Wayland)"}
    manager.answers["platform_info"] = json.dumps(host)
    manager.answers["plugins_list"] = json.dumps(
        [{**_PLUGINS[0], "supported": True}, {**_PLUGINS[1], "supported": False}]
    )
    widget = PluginsTab()
    widget.refresh()

    items = [widget._plugin_list.item(i) for i in range(2)]
    texts = {str(it.data(plugins_tab._PLUGIN_ID_ROLE)): it.text() for it in items if it is not None}
    assert_that(texts["com.acme.extra"]).contains("Not supported on Linux (KDE, Wayland)")
    assert_that(texts["com.decker.core"]).does_not_contain("Not supported")


def test_update_button_follows_the_selected_plugins_update(tab, manager):
    update = {"id": "com.acme.extra", "name": "Extra", "installed": "2.1.0", "available": "2.2.0"}
    EventManager.get().emit(PluginUpdatesAvailableEvent(updates=[update], missing=[]))

    _select(tab, "com.decker.core")
    assert_that(tab._update_btn.isEnabled()).is_false()
    _select(tab, "com.acme.extra")
    assert_that(tab._update_btn.isEnabled()).is_true()

    manager.answers["plugin_install_from_repository"] = "job-9"
    tab._update_btn.click()

    manager.call.assert_any_call("plugin_install_from_repository", "com.acme.extra")
    assert_that(tab._status_label.text()).is_equal_to("Updating Extra…")


def test_update_failure_reports_the_server_error(tab, manager):
    update = {"id": "com.acme.extra", "name": "Extra", "installed": "2.1.0", "available": "2.2.0"}
    EventManager.get().emit(PluginUpdatesAvailableEvent(updates=[update], missing=[]))
    _select(tab, "com.acme.extra")
    manager.answers["plugin_install_from_repository"] = "error: nope"

    tab._update_btn.click()

    assert_that(tab._status_label.text()).contains("Could not update Extra", "error: nope")


def test_plugins_tab_global_settings_use_the_plugin_id(tab, monkeypatch):
    monkeypatch.setattr(dynamic_choice_edit, "_default_fetch", lambda: lambda _p, _s: (True, [], ""))
    _select(tab, "com.acme.extra")

    field = tab._settings_form._fields["voice"].widget

    assert isinstance(field, DynamicChoiceEdit)
    assert_that(field._plugin_id).is_equal_to("com.acme.extra")
    if field._thread is not None:
        field._thread.join(5)
