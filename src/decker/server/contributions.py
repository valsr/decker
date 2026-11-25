"""Dispatches the kinds of content a plugin contributes to the handler for each kind."""

import logging
from collections.abc import Sequence
from typing import Protocol

from decker.plugin.loader import InstalledPlugin
from decker.plugin.manifest import KNOWN_CONTRIBUTION_KINDS


class ContributionHandler(Protocol):
    """Registers and unregisters one kind of plugin contribution."""

    kind: str

    def register(self, plugin: InstalledPlugin) -> None:
        """Make the plugin's contributions of this kind available."""

    def unregister(self, plugin_id: str) -> None:
        """Forget everything of this kind that the plugin contributed."""


def contributed_kinds(plugin: InstalledPlugin) -> frozenset[str]:
    """Return the contribution kinds the plugin's manifest actually declares."""
    kinds = {
        "cards": plugin.manifest.cards,
        "actions": plugin.manifest.actions,
        "icon_packs": plugin.manifest.icon_packs,
        "variables": plugin.manifest.variables,
        "presets": plugin.manifest.presets,
    }
    return frozenset(kind for kind, entries in kinds.items() if entries)


class ContributionRegistry:
    """Routes plugin registration to one handler per contribution kind."""

    def __init__(self, handlers: Sequence[ContributionHandler]) -> None:
        kinds = [handler.kind for handler in handlers]
        assert set(kinds) == KNOWN_CONTRIBUTION_KINDS and len(kinds) == len(set(kinds)), kinds
        self._handlers = list(handlers)

    def add(self, plugin: InstalledPlugin) -> None:
        """Register the plugin with each handler it has content for; undo them all if one fails."""
        kinds = contributed_kinds(plugin)
        done: list[ContributionHandler] = []
        for handler in self._handlers:
            if handler.kind not in kinds:
                continue
            try:
                handler.register(plugin)
            except Exception:
                for registered in reversed(done):
                    self._unregister_quietly(registered, plugin.manifest.id)
                raise
            done.append(handler)

    def remove(self, plugin_id: str) -> None:
        """Unregister the plugin from every handler."""
        for handler in self._handlers:
            handler.unregister(plugin_id)

    @staticmethod
    def _unregister_quietly(handler: ContributionHandler, plugin_id: str) -> None:
        try:
            handler.unregister(plugin_id)
        except Exception:  # pylint: disable=broad-except
            logging.exception("Rolling back %s contribution of %s failed", handler.kind, plugin_id)
