"""Tests for decker.client.tray.TrayIcon."""

from unittest.mock import MagicMock

from assertpy import assert_that
from PyQt6.QtWidgets import QSystemTrayIcon

from decker.client.tray import TrayIcon


def _make_window(visible: bool = True) -> MagicMock:
    window = MagicMock()
    window.isVisible.return_value = visible
    return window


class TestMenu:
    def test_menu_has_show_hide_settings_about_and_quit(self, qapp):  # pylint: disable=unused-argument
        tray = TrayIcon(_make_window())
        actions = tray._menu.actions()  # pylint: disable=protected-access
        labels = [action.text() for action in actions if not action.isSeparator()]
        assert_that(labels).is_equal_to(["Hide", "Settings", "About Decker…", "Quit"])

    def test_settings_opens_settings_dialog(self, qapp):  # pylint: disable=unused-argument
        window = _make_window()
        tray = TrayIcon(window)

        _action(tray, "Settings").trigger()

        window.open_settings.assert_called_once()

    def test_about_opens_about_dialog(self, qapp):  # pylint: disable=unused-argument
        window = _make_window()
        tray = TrayIcon(window)

        _action(tray, "About Decker…").trigger()

        window.open_about.assert_called_once()

    def test_toggle_label_reflects_visible_window(self, qapp):  # pylint: disable=unused-argument
        tray = TrayIcon(_make_window(visible=True))
        tray._update_toggle_label()  # pylint: disable=protected-access
        assert_that(tray._toggle_action.text()).is_equal_to("Hide")  # pylint: disable=protected-access

    def test_toggle_label_reflects_hidden_window(self, qapp):  # pylint: disable=unused-argument
        tray = TrayIcon(_make_window(visible=False))
        tray._update_toggle_label()  # pylint: disable=protected-access
        assert_that(tray._toggle_action.text()).is_equal_to("Show")  # pylint: disable=protected-access


class TestToggleWindowVisibility:
    def test_hides_visible_window(self, qapp):  # pylint: disable=unused-argument
        window = _make_window(visible=True)
        tray = TrayIcon(window)
        tray._toggle_window_visibility()  # pylint: disable=protected-access
        window.hide.assert_called_once()
        window.show.assert_not_called()

    def test_shows_and_raises_hidden_window(self, qapp):  # pylint: disable=unused-argument
        window = _make_window(visible=False)
        tray = TrayIcon(window)
        tray._toggle_window_visibility()  # pylint: disable=protected-access
        window.show.assert_called_once()
        window.raise_.assert_called_once()
        window.activateWindow.assert_called_once()
        window.hide.assert_not_called()


class TestActivated:
    def test_trigger_toggles_visibility(self, qapp):  # pylint: disable=unused-argument
        window = _make_window(visible=True)
        tray = TrayIcon(window)
        tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)  # pylint: disable=protected-access
        window.hide.assert_called_once()

    def test_other_reasons_do_nothing(self, qapp):  # pylint: disable=unused-argument
        window = _make_window(visible=True)
        tray = TrayIcon(window)
        tray._on_activated(QSystemTrayIcon.ActivationReason.Context)  # pylint: disable=protected-access
        window.hide.assert_not_called()
        window.show.assert_not_called()


class TestQuit:
    def test_quit_action_shuts_the_app_down_through_the_window(self, qapp):  # pylint: disable=unused-argument
        window = _make_window()
        tray = TrayIcon(window)

        _action(tray, "Quit").trigger()

        window.quit_app.assert_called_once()


def _action(tray: TrayIcon, text: str):
    return next(a for a in tray._menu.actions() if a.text() == text)  # pylint: disable=protected-access
