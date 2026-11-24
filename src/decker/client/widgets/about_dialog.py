"""About dialog: client and server versions, authors, repository and licence notes."""

import html
import json
from importlib import metadata
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from decker.client.assets import ICON_PATH
from decker.common.version import decker_version

REPOSITORY_URL = "https://github.com/valsr/decker"
_LOGO_SIZE = 96


def server_version(connection_manager: Any) -> str | None:
    """The server's version from its status RPC; None when not connected or the call fails, 'unknown' for an old server."""
    if connection_manager is None or not connection_manager.is_connected:
        return None
    try:
        version = json.loads(connection_manager.call("status")).get("version")
    except (TypeError, ValueError, AttributeError):
        return None
    return str(version) if version else "unknown"


def _authors() -> list[str]:
    try:
        info = metadata.metadata("decker")
    except metadata.PackageNotFoundError:
        return ["valsr"]
    return [name for name in info.get_all("Author") or [] if name] or ["valsr"]


class AboutDialog(QDialog):
    """Modal dialog describing this Decker install."""

    def __init__(
        self, server_version: str | None, parent: QWidget | None = None
    ) -> None:  # pylint: disable=redefined-outer-name
        super().__init__(parent)
        self.setWindowTitle("About Decker")

        logo = QLabel()
        logo.setPixmap(QIcon(str(ICON_PATH)).pixmap(_LOGO_SIZE, _LOGO_SIZE))
        logo.setAlignment(Qt.AlignmentFlag.AlignTop)

        title = QLabel("<h2>Decker</h2>")
        body = QLabel(self._body_html(server_version))
        body.setObjectName("about_body")
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setOpenExternalLinks(True)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)

        text = QVBoxLayout()
        text.addWidget(title)
        text.addWidget(body)
        header = QHBoxLayout()
        header.addWidget(logo)
        header.addLayout(text)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(buttons)

    @staticmethod
    def _body_html(server: str | None) -> str:
        lines = [
            f"Client version: {decker_version()}",
            f"Server version: {html.escape(server) if server else 'not connected'}",
            f"Authors: {', '.join(_authors())}",
            f'Repository: <a href="{REPOSITORY_URL}">{REPOSITORY_URL}</a>',
            "MIT License",
            "Includes Font Awesome Free 7.3.1 and DejaVu fonts",
        ]
        return "<br>".join(lines)
