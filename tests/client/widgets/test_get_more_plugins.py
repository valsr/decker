"""Tests for decker.client.widgets.get_more_plugins."""

import base64
import json
from pathlib import Path
import threading
from unittest.mock import MagicMock, patch

import pytest
from assertpy import assert_that
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMessageBox

from decker.client.card_icons import builtin_pixmap, decode_icon, placeholder_pixmap
from decker.client.context import AppContext
from decker.client.events import (
    PluginInstallFinishedEvent,
    PluginInstallProgressEvent,
    PluginsChangedEvent,
    ServerDisconnectedEvent,
)
from decker.client.events.event_manager import EventManager
from decker.client.ui_state_store import UiStateStore
from decker.client.widgets import plugins_tab
from decker.client.widgets.get_more_plugins import (
    GetMorePluginsDialog,
    button_state,
    human_size,
    platform_badge,
)

# pylint: disable=protected-access,redefined-outer-name,unused-argument


def _plugin(plugin_id, name, **overrides):
    version = {"version": "2.0.0", "decker": ">=0.1", "size": 2048, "released": "2026-01-01", "compatible": True}
    plugin = {
        "id": plugin_id,
        "name": name,
        "author": "Acme",
        "description": f"{name} description",
        "category": "Tools",
        "homepage": "https://acme.example",
        "repository": "https://repo.example/index.json",
        "shadowed_by": [],
        "installed_version": None,
        "latest_compatible": "2.0.0",
        "versions": [
            {**version, "contributes": {"actions": ["Do it"], "icon_packs": [], "variables": ["media_volume", "m_{x}"]}}
        ],
    }
    plugin.update(overrides)
    return plugin


_CATALOG = [
    _plugin("a.fresh", "Fresh"),
    _plugin("a.update", "Updatable", installed_version="1.0.0", category="Media", author="Zed"),
    _plugin("a.done", "Done", installed_version="2.0.0"),
    _plugin(
        "a.old",
        "Incompatible",
        latest_compatible=None,
        versions=[
            {"version": "9.0.0", "decker": ">=9", "size": 1, "released": "", "compatible": False, "contributes": {}}
        ],
    ),
]


@pytest.fixture
def manager(qapp):
    AppContext.reset()
    EventManager.reset()
    cm = MagicMock()
    answers = {"repository_catalog": _CATALOG, "repository_icon": "", "plugin_install_from_repository": "job-1"}
    cm.call.side_effect = lambda method, *args: answers.get(method)
    cm.answers = answers
    AppContext.get().connection_manager = cm
    plugins_tab._install_allowed_cache = True
    yield cm
    AppContext.reset()
    EventManager.reset()
    plugins_tab._install_allowed_cache = False


def _settle(dialog, qapp):
    for _ in range(3):
        for worker in (dialog._worker, dialog._icon_worker):
            while worker is not None and worker.is_alive():
                worker.join(1)
        qapp.processEvents()


@pytest.fixture
def dialog(manager, qapp):
    widget = GetMorePluginsDialog()
    _settle(widget, qapp)
    yield widget
    widget.close()
    widget.deleteLater()


def test_filters_by_search_and_category(dialog):
    assert_that(dialog._rows).is_length(4)
    assert_that(dialog._category.itemText(0)).is_equal_to("All categories")
    assert_that([dialog._category.itemText(i) for i in range(dialog._category.count())]).contains("Media", "Tools")

    dialog._search.setText("ZED")
    assert_that(list(dialog._rows)).is_equal_to(["a.update"])
    dialog._search.setText("a.fresh")
    assert_that(list(dialog._rows)).is_equal_to(["a.fresh"])
    dialog._search.setText("")
    dialog._category.setCurrentText("Media")
    assert_that(list(dialog._rows)).is_equal_to(["a.update"])


def test_button_states(dialog):
    states = {pid: (row.button.text(), row.button.isEnabled()) for pid, row in dialog._rows.items()}
    assert_that(states["a.fresh"]).is_equal_to(("Install", True))
    assert_that(states["a.update"]).is_equal_to(("Update", True))
    assert_that(states["a.done"]).is_equal_to(("Installed", False))
    assert_that(states["a.old"]).is_equal_to(("Requires Decker >=9", False))


def test_install_calls_rpc_and_disables_buttons(dialog, manager, qapp):
    dialog._rows["a.fresh"].button.click()
    _settle(dialog, qapp)

    installs = [c.args for c in manager.call.call_args_list if c.args[0] == "plugin_install_from_repository"]
    assert_that(installs).is_equal_to([("plugin_install_from_repository", "a.fresh")])
    assert_that(dialog._rows["a.fresh"].button.isEnabled()).is_false()
    assert_that(dialog._rows["a.update"].button.isEnabled()).is_false()
    dialog._on_progress(PluginInstallProgressEvent(job_id="other", message="Uploading x"))
    assert_that(dialog._rows["a.fresh"].button.text()).is_equal_to("Installing…")
    dialog._on_progress(PluginInstallProgressEvent(job_id="job-1", message="Downloading…"))
    assert_that(dialog._rows["a.fresh"].button.text()).is_equal_to("Downloading…")
    dialog._on_finished(
        PluginInstallFinishedEvent(job_id="other", ok=True, plugin_id="x", version="1", previous_version="", message="")
    )
    assert_that(dialog._installing).is_equal_to("a.fresh")


def test_install_error_is_reported_and_buttons_return(dialog, manager, qapp):
    manager.answers["plugin_install_from_repository"] = "error: nope"
    dialog._rows["a.fresh"].button.click()
    _settle(dialog, qapp)

    assert_that(dialog._status.text()).contains("nope")
    assert_that(dialog._rows["a.fresh"].button.isEnabled()).is_true()


def test_finished_event_refreshes(dialog, manager, qapp):
    dialog._rows["a.fresh"].button.click()
    _settle(dialog, qapp)
    manager.answers["repository_catalog"] = [_plugin("a.fresh", "Fresh", installed_version="2.0.0")]

    dialog._on_finished(
        PluginInstallFinishedEvent(
            job_id="job-1", ok=True, plugin_id="a.fresh", version="2.0.0", previous_version="", message=""
        )
    )
    _settle(dialog, qapp)

    assert_that(list(dialog._rows)).is_equal_to(["a.fresh"])
    assert_that(dialog._rows["a.fresh"].button.text()).is_equal_to("Installed")
    assert_that(dialog._status.text()).contains("Installed a.fresh")


def test_details_pane_shows_size_and_repository(dialog):
    dialog._list.setCurrentRow(0)
    text = dialog._details.text()
    assert_that(text).contains("2.0 KiB", "https://repo.example/index.json", "Acme", "https://acme.example", "&gt;=0.1")
    assert_that(text).contains("Do it")
    assert_that(text).contains("Variables: media_volume", "m_{x}")


def test_icons_load_one_at_a_time_off_the_ui_thread(dialog, manager, qapp):
    _settle(dialog, qapp)
    ids = [c.args[1] for c in manager.call.call_args_list if c.args[0] == "repository_icon"]
    assert_that(ids).contains("a.fresh", "a.old")


def _dialog_with(manager, qapp, plugin):
    manager.answers["repository_catalog"] = [plugin]
    widget = GetMorePluginsDialog()
    _settle(widget, qapp)
    return widget


def test_ref_icon_row_draws_without_rpc(manager, qapp):
    widget = _dialog_with(manager, qapp, _plugin("a.ref", "Ref", icon_ref="fas:toolbox"))
    row = widget._rows["a.ref"]
    placeholder = placeholder_pixmap("Ref", 40).toImage()
    assert_that(row.icon_label.pixmap().toImage() == placeholder).is_false()
    assert_that(widget._pending).does_not_contain("a.ref")
    ids = [c.args[1] for c in manager.call.call_args_list if c.args[0] == "repository_icon"]
    assert_that(ids).does_not_contain("a.ref")
    widget.close()


def test_unknown_ref_falls_back_to_placeholder(manager, qapp):
    widget = _dialog_with(manager, qapp, _plugin("a.bad", "Bad", icon_ref="fas:no-such-icon"))
    row = widget._rows["a.bad"]
    assert_that(row.has_local_icon).is_false()
    placeholder = placeholder_pixmap("Bad", 40).toImage()
    assert_that(row.icon_label.pixmap().toImage() == placeholder).is_true()
    widget.close()


def test_ref_icon_is_smooth_scaled(manager, qapp):
    widget = _dialog_with(manager, qapp, _plugin("a.ref", "Ref", icon_ref="fas:toolbox"))
    source = builtin_pixmap("fas:toolbox")
    assert source is not None
    expected = source.scaled(40, 40, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    assert_that(widget._rows["a.ref"].icon_label.pixmap().toImage() == expected.toImage()).is_true()
    widget.close()


def test_human_size():
    assert_that(human_size(512)).is_equal_to("512 B")
    assert_that(human_size(3 * 1024 * 1024)).is_equal_to("3.0 MiB")


def test_not_connected_reports_and_keeps_buttons_usable(dialog, qapp):
    AppContext.get().connection_manager = None
    dialog.refresh()
    assert_that(dialog._status.text()).contains("Not connected")

    dialog._rows["a.fresh"].button.click()
    assert_that(dialog._installing).is_none()
    assert_that(dialog._status.text()).contains("Not connected")
    assert_that(dialog._rows["a.fresh"].button.isEnabled()).is_true()


def test_rpc_exception_restores_buttons(dialog, manager, qapp):
    manager.call.side_effect = RuntimeError("boom")
    dialog._rows["a.fresh"].button.click()
    _settle(dialog, qapp)

    assert_that(dialog._installing).is_none()
    assert_that(dialog._status.text()).contains("Install failed")
    assert_that(dialog._rows["a.fresh"].button.isEnabled()).is_true()

    dialog.refresh()
    _settle(dialog, qapp)
    assert_that(dialog._status.text()).contains("Could not load")


def test_finished_before_job_id_is_replayed(dialog, manager, qapp):
    dialog._installing = "a.fresh"
    dialog._on_finished(
        PluginInstallFinishedEvent(
            job_id="job-1", ok=True, plugin_id="a.fresh", version="2.0.0", previous_version="", message=""
        )
    )
    assert_that(dialog._installing).is_equal_to("a.fresh")
    dialog._after_start("job-1")
    _settle(dialog, qapp)
    assert_that(dialog._installing).is_none()


def test_plugins_changed_reloads_catalog_when_idle(dialog, manager, qapp):
    manager.answers["repository_catalog"] = [_plugin("a.fresh", "Fresh", installed_version="2.0.0")]

    EventManager.get().emit(PluginsChangedEvent())
    _settle(dialog, qapp)

    assert_that(list(dialog._rows)).is_equal_to(["a.fresh"])
    assert_that(dialog._rows["a.fresh"].button.text()).is_equal_to("Installed")


def test_plugins_changed_ignored_while_installing(dialog, manager, qapp):
    dialog._rows["a.fresh"].button.click()
    _settle(dialog, qapp)
    manager.answers["repository_catalog"] = []

    EventManager.get().emit(PluginsChangedEvent())
    _settle(dialog, qapp)

    assert_that(dialog._rows).is_length(4)


def test_icon_rpc_runs_on_a_worker_thread(manager, qapp):
    threads: list[int] = []

    def fake(method, *args):
        if method == "repository_icon":
            threads.append(threading.get_ident())
        return {"repository_catalog": [_plugin("a.fresh", "Fresh")]}.get(method, "")

    manager.call.side_effect = fake
    widget = GetMorePluginsDialog()
    _settle(widget, qapp)

    assert_that(threads).is_length(1)
    assert_that(threads[0]).is_not_equal_to(threading.get_ident())
    widget.close()


def test_shadowed_duplicates_are_hidden_and_noted_on_the_winner(manager, qapp):
    manager.answers["repository_catalog"] = [
        _plugin("a.dup", "Dup", repository="https://one.example/index.json"),
        _plugin(
            "a.dup",
            "Dup",
            repository="https://two.example/index.json",
            shadowed_by="https://one.example/index.json",
            installed_version="1.0.0",
        ),
    ]
    widget = GetMorePluginsDialog()
    _settle(widget, qapp)

    assert_that(widget._list.count()).is_equal_to(1)
    assert_that(widget._rows["a.dup"].button.text()).is_equal_to("Install")
    widget._list.setCurrentRow(0)
    text = widget._details.text()
    assert_that(text).contains("Also offered by https://two.example/index.json (not used)")
    assert_that(text).contains("Repository: https://one.example/index.json")
    widget.close()


def test_fallback_uses_the_newest_version_when_none_is_compatible():
    plugin = _plugin(
        "a.old",
        "Old",
        latest_compatible=None,
        versions=[
            {"version": "9.0.0", "decker": ">=9", "size": 1, "contributes": {}},
            {"version": "1.0.0", "decker": ">=1", "size": 1, "contributes": {}},
        ],
    )
    assert_that(button_state(plugin)).is_equal_to(("Requires Decker >=9", False))


def test_install_controls_disabled_when_not_allowed(manager, qapp):
    plugins_tab._install_allowed_cache = False
    widget = GetMorePluginsDialog()
    _settle(widget, qapp)

    assert_that(widget._rows["a.fresh"].button.isEnabled()).is_false()
    assert_that(widget._rows["a.update"].button.isEnabled()).is_false()
    assert_that(widget._rows["a.fresh"].button.toolTip()).is_not_empty()
    widget._on_install("a.fresh")
    assert_that(widget._installing).is_none()
    widget.close()


def test_disconnect_resets_the_in_flight_install(dialog, manager, qapp):
    dialog._rows["a.fresh"].button.click()
    _settle(dialog, qapp)
    assert_that(dialog._installing).is_equal_to("a.fresh")

    EventManager.get().emit(ServerDisconnectedEvent(reason="gone"))

    assert_that(dialog._installing).is_none()
    assert_that(dialog._status.text()).contains("a.fresh", "interrupted")
    assert_that(dialog._rows["a.fresh"].button.isEnabled()).is_true()


def _finish_install(dialog, plugin_id, version="2.0.0", ok=True, message=""):
    dialog._installing = plugin_id
    dialog._job_id = "job-1"
    dialog._on_finished(
        PluginInstallFinishedEvent(
            job_id="job-1", ok=ok, plugin_id=plugin_id, version=version, previous_version="", message=message
        )
    )


def test_installed_plugin_shows_installed_at_once_not_after_the_catalog_reloads(dialog, manager, qapp):
    # The server's fresh catalog has not arrived yet (no _settle): the row must already say Installed.
    _finish_install(dialog, "a.fresh")

    button = dialog._rows["a.fresh"].button
    assert_that((button.text(), button.isEnabled())).is_equal_to(("Installed", False))
    _settle(dialog, qapp)


def test_updated_plugin_shows_installed_at_once(dialog, manager, qapp):
    _finish_install(dialog, "a.update", version="2.0.0")

    button = dialog._rows["a.update"].button
    assert_that((button.text(), button.isEnabled())).is_equal_to(("Installed", False))
    _settle(dialog, qapp)


def test_failed_install_does_not_mark_the_plugin_installed(dialog, manager, qapp):
    _finish_install(dialog, "a.fresh", ok=False, message="boom")

    button = dialog._rows["a.fresh"].button
    assert_that((button.text(), button.isEnabled())).is_equal_to(("Install", True))
    _settle(dialog, qapp)


def test_servers_fresh_catalog_still_wins_after_an_install(dialog, manager, qapp):
    manager.answers["repository_catalog"] = [_plugin("a.fresh", "Fresh", installed_version="2.0.0")]

    _finish_install(dialog, "a.fresh")
    _settle(dialog, qapp)

    assert_that(list(dialog._rows)).is_equal_to(["a.fresh"])
    assert_that(dialog._rows["a.fresh"].button.text()).is_equal_to("Installed")


def test_installing_a_plugin_leaves_the_other_rows_alone(dialog, manager, qapp):
    _finish_install(dialog, "a.fresh")

    states = {pid: row.button.text() for pid, row in dialog._rows.items()}
    assert_that(states["a.update"]).is_equal_to("Update")
    assert_that(states["a.done"]).is_equal_to("Installed")
    _settle(dialog, qapp)


_HOST = {"os": "linux", "desktop": "kde", "session": "wayland", "label": "Linux (KDE, Wayland)"}


@pytest.fixture
def store(tmp_path):
    ui_store = UiStateStore(tmp_path / "config.db")
    yield ui_store
    ui_store.close()


@pytest.fixture
def platform_manager(manager):
    manager.answers["platform_info"] = json.dumps(_HOST)
    manager.answers["repository_catalog"] = [
        _plugin("a.fresh", "Fresh", supported=True),
        _plugin("a.mac", "MacOnly", supported=False),
    ]
    return manager


def test_platform_badge_is_empty_for_supported_plugins_and_names_the_host_otherwise():
    assert_that(platform_badge({"supported": True}, "Linux (KDE)")).is_equal_to("")
    assert_that(platform_badge({}, "Linux (KDE)")).is_equal_to("")
    assert_that(platform_badge({"supported": False}, "Linux (KDE)")).is_equal_to("Not supported on Linux (KDE)")


def test_filter_hides_unsupported_plugins_by_default(platform_manager, store, qapp):
    widget = GetMorePluginsDialog(ui_state_store=store)
    _settle(widget, qapp)

    assert_that(widget._platform_only.isChecked()).is_true()
    assert_that(widget._platform_only.text()).is_equal_to(
        "Only show plugins that support this system (Linux (KDE, Wayland))"
    )
    assert_that(list(widget._rows)).is_equal_to(["a.fresh"])
    widget.close()


def test_unchecking_the_filter_shows_unsupported_plugins_with_a_badge(platform_manager, store, qapp):
    widget = GetMorePluginsDialog(ui_state_store=store)
    _settle(widget, qapp)

    widget._platform_only.setChecked(False)

    assert_that(list(widget._rows)).is_equal_to(["a.fresh", "a.mac"])
    assert_that(widget._rows["a.mac"].badge.text()).is_equal_to("Not supported on Linux (KDE, Wayland)")
    assert_that(widget._rows["a.fresh"].badge.text()).is_equal_to("")
    widget.close()


def _unfiltered(platform_manager, store, qapp):
    store.set("plugins.platform_filter", False)
    widget = GetMorePluginsDialog(ui_state_store=store)
    _settle(widget, qapp)
    return widget


def _installs(manager):
    return [c.args for c in manager.call.call_args_list if c.args[0] == "plugin_install_from_repository"]


def test_install_of_an_unsupported_plugin_asks_and_cancel_does_not_install(platform_manager, store, qapp):
    widget = _unfiltered(platform_manager, store, qapp)

    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as question:
        widget._on_install("a.mac")

    assert_that(question.call_args.args[2]).is_equal_to(
        "MacOnly does not declare support for Linux (KDE, Wayland). Install anyway?"
    )
    assert_that(widget._installing).is_none()
    assert_that(_installs(platform_manager)).is_empty()
    widget.close()


def test_install_of_an_unsupported_plugin_proceeds_on_yes(platform_manager, store, qapp):
    widget = _unfiltered(platform_manager, store, qapp)

    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
        widget._on_install("a.mac")
    _settle(widget, qapp)

    assert_that(_installs(platform_manager)).is_equal_to([("plugin_install_from_repository", "a.mac")])
    widget.close()


def test_install_of_a_supported_plugin_does_not_ask(platform_manager, store, qapp):
    widget = GetMorePluginsDialog(ui_state_store=store)
    _settle(widget, qapp)

    with patch.object(QMessageBox, "question") as question:
        widget._on_install("a.fresh")
    _settle(widget, qapp)

    question.assert_not_called()
    assert_that(widget._installing).is_equal_to("a.fresh")
    widget.close()


def test_filter_choice_is_remembered(platform_manager, store, qapp):
    first = GetMorePluginsDialog(ui_state_store=store)
    _settle(first, qapp)
    first._platform_only.setChecked(False)
    first.close()

    second = GetMorePluginsDialog(ui_state_store=store)
    _settle(second, qapp)

    assert_that(store.get("plugins.platform_filter")).is_false()
    assert_that(second._platform_only.isChecked()).is_false()
    assert_that(list(second._rows)).is_equal_to(["a.fresh", "a.mac"])
    second.close()


def test_platform_info_is_fetched_off_the_ui_thread(platform_manager, store, qapp):
    threads: list[int] = []
    original = platform_manager.call.side_effect

    def fake(method, *args):
        if method == "platform_info":
            threads.append(threading.get_ident())
        return original(method, *args)

    platform_manager.call.side_effect = fake
    widget = GetMorePluginsDialog(ui_state_store=store)
    _settle(widget, qapp)

    assert_that(threads).is_not_empty()
    assert_that(threads).does_not_contain(threading.get_ident())
    widget.close()


_CURRENT_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
    b'<path fill="currentColor" d="M0 0h10v10H0z"/></svg>'
)


def test_repository_svg_icon_is_drawn_in_the_icon_colour(manager, qapp):
    manager.answers["repository_icon"] = base64.b64encode(_CURRENT_SVG).decode("ascii")
    qapp.setProperty("iconColor", "#ff0000")
    try:
        widget = _dialog_with(manager, qapp, _plugin("a.svg", "Svg"))
        image = widget._rows["a.svg"].icon_label.pixmap().toImage()
    finally:
        qapp.setProperty("iconColor", None)

    colour = image.pixelColor(image.width() // 2, image.height() // 2)
    assert_that((colour.red(), colour.green(), colour.blue())).is_equal_to((255, 0, 0))


def test_repository_png_icon_is_unchanged(manager, qapp):
    png = Path(__file__).parents[2] / "fixtures" / "plugins" / "com.decker.testfixture" / "icons" / "counter.png"
    encoded = base64.b64encode(png.read_bytes()).decode("ascii")
    manager.answers["repository_icon"] = encoded
    expected = decode_icon(encoded)
    qapp.setProperty("iconColor", "#ff0000")
    try:
        widget = _dialog_with(manager, qapp, _plugin("a.png", "Png"))
        shown = widget._rows["a.png"].icon_label.pixmap()
    finally:
        qapp.setProperty("iconColor", None)

    assert expected is not None
    scaled = expected.scaled(
        shown.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    assert_that(shown.toImage() == scaled.toImage()).is_true()
