"""Tests for decker.client.widgets.about_dialog."""

import json
from unittest.mock import MagicMock

from assertpy import assert_that
from PyQt6.QtWidgets import QLabel

from decker.client.widgets.about_dialog import AboutDialog, server_version


def _body(dialog: AboutDialog) -> str:
    label = dialog.findChild(QLabel, "about_body")
    assert label is not None
    return label.text()


def test_about_shows_versions_authors_and_repository(qapp):  # pylint: disable=unused-argument
    dialog = AboutDialog(server_version="0.7.2")
    text = _body(dialog)
    assert_that(text).contains("0.7.2").contains("valsr").contains("https://github.com/valsr/decker")
    assert_that(text).contains("Font Awesome").contains("DejaVu")


def test_about_without_server_says_not_connected(qapp):  # pylint: disable=unused-argument
    dialog = AboutDialog(server_version=None)
    assert_that(_body(dialog)).contains("not connected")


def test_server_version_reads_the_status_rpc():
    manager = MagicMock()
    manager.is_connected = True
    manager.call.return_value = json.dumps({"version": "0.7.2"})
    assert_that(server_version(manager)).is_equal_to("0.7.2")


def test_server_version_is_none_when_disconnected_or_the_call_fails():
    manager = MagicMock()
    manager.is_connected = False
    assert_that(server_version(manager)).is_none()
    manager.is_connected = True
    manager.call.return_value = None
    assert_that(server_version(manager)).is_none()
