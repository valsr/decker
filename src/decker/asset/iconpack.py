"""Registry of icon packs contributed by installed plugins."""

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from decker.asset.builtinicons import BuiltinIcons
from decker.asset.systemicons import PACK_PREFIX, SystemIcons
from decker.asset.usericons import USER_PACK_ID, UserIcons
from decker.common.icon_ref import BUILTIN_PACK_PREFIX, DECKER_ICON_REF, parse_icon_ref
from decker.plugin.icons import max_icon_bytes
from decker.plugin.loader import InstalledPlugin

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".svg"})
_FailureCallback = Callable[[str, str], None]
_DECKER_ICON = Path(__file__).parent.parent / "assets" / "decker.svg"


@dataclass(frozen=True)
class _Pack:
    plugin_id: str
    plugin_name: str
    plugin_version: str
    name: str
    description: str
    root: Path


class IconPackRegistry:
    """Handler for the "icon_packs" contribution: knows each pack's folder and serves its icons."""

    kind = "icon_packs"

    def __init__(
        self,
        on_failure: _FailureCallback | None = None,
        system_icons: SystemIcons | None = None,
        user_icons: UserIcons | None = None,
    ) -> None:
        self._packs: dict[str, _Pack] = {}
        self.user_icons: UserIcons = user_icons if user_icons is not None else UserIcons()
        self._builtin = BuiltinIcons()
        self._system = system_icons if system_icons is not None else SystemIcons()
        self._on_failure = on_failure
        self._lock = threading.RLock()

    def register(self, plugin: InstalledPlugin) -> None:
        """Register the plugin's packs; a pack whose folder is missing or escapes the plugin is refused."""
        plugin_id = plugin.manifest.id
        plugin_root = plugin.plugin_dir.resolve()
        for spec in plugin.manifest.icon_packs:
            if spec.id.startswith((PACK_PREFIX, BUILTIN_PACK_PREFIX)) or spec.id == USER_PACK_ID:
                logging.error("[%s] icon pack %s refused: reserved icon pack id", plugin_id, spec.id)
                if self._on_failure is not None:
                    self._on_failure(plugin_id, f"icon pack {spec.id}: reserved icon pack id")
                continue
            root = (plugin_root / spec.path).resolve()
            error = self._refusal(spec.id, root, plugin_root)
            if error is not None:
                logging.error("[%s] icon pack %s refused: %s", plugin_id, spec.id, error)
                if self._on_failure is not None:
                    self._on_failure(plugin_id, f"icon pack {spec.id}: {error}")
                continue
            with self._lock:
                self._packs[spec.id] = _Pack(
                    plugin_id, plugin.manifest.name, plugin.manifest.version, spec.name, spec.description, root
                )

    def warm(self) -> None:
        """Index the system icon theme in the background (it takes a second or two on a large theme)."""
        self._system.warm()

    def unregister(self, plugin_id: str) -> None:
        """Drop every pack the plugin contributed."""
        with self._lock:
            self._packs = {k: v for k, v in self._packs.items() if v.plugin_id != plugin_id}

    def list_packs(self) -> list[dict[str, Any]]:
        """Every registered pack with its owner and icon count, then the User, Built-In and System Icons packs."""
        with self._lock:
            packs = list(self._packs.items())
        plugin_packs = [
            {
                "id": pack_id,
                "name": pack.name,
                "description": pack.description,
                "plugin_id": pack.plugin_id,
                "plugin_name": pack.plugin_name,
                "plugin_version": pack.plugin_version,
                "icon_count": len(self._icons_of(pack)),
            }
            for pack_id, pack in sorted(packs, key=lambda item: item[1].name)
        ]
        return plugin_packs + [self.user_icons.pack_info()] + self._builtin.list_packs() + self._system.list_packs()

    def list_icons(self, pack_id: str) -> list[str]:
        """Return the pack's image paths relative to its folder; icons.json decides the order when present."""
        if pack_id == USER_PACK_ID:
            return self.user_icons.list_icons()
        if self._builtin.owns(pack_id):
            return self._builtin.list_icons(pack_id)
        if self._system.owns(pack_id):
            return self._system.list_icons(pack_id)
        pack = self._pack(pack_id)
        return self._icons_of(pack) if pack is not None else []

    def read_icon(self, pack_id: str, name: str) -> bytes | None:
        """Return the icon's bytes, or None if unknown, outside the pack folder, not an image or too large."""
        if pack_id == USER_PACK_ID:
            return self.user_icons.read_icon(name)
        if self._builtin.owns(pack_id):
            return self._builtin.read_icon(pack_id, name)
        if self._system.owns(pack_id):
            return self._system.read_icon(pack_id, name)
        pack = self._pack(pack_id)
        return self._read_pack_icon(pack_id, pack, name) if pack is not None else None

    @staticmethod
    def _read_pack_icon(pack_id: str, pack: "_Pack", name: str) -> bytes | None:
        try:
            path = (pack.root / name).resolve()
        except (OSError, ValueError):
            logging.warning("Refusing icon %r of pack %s: invalid name", name, pack_id)
            return None
        if not path.is_relative_to(pack.root) or path.suffix.lower() not in _IMAGE_SUFFIXES:
            logging.warning("Refusing icon %r of pack %s: not an image inside the pack", name, pack_id)
            return None
        try:
            if not path.is_file() or path.stat().st_size > max_icon_bytes():
                return None
            return path.read_bytes()
        except OSError as exc:
            logging.warning("Could not read icon %s: %s", path, exc)
            return None

    def read_ref(self, ref: Any) -> bytes | None:
        """The bytes of the icon a ``pack:<id>/<path>``, ``fas:<name>`` or ``decker:logo`` reference names, or None."""
        if ref == DECKER_ICON_REF:
            return _DECKER_ICON.read_bytes()
        parsed = parse_icon_ref(ref)
        return self.read_icon(*parsed) if parsed is not None else None

    def _pack(self, pack_id: str) -> _Pack | None:
        with self._lock:
            return self._packs.get(pack_id)

    def _refusal(self, pack_id: str, root: Path, plugin_root: Path) -> str | None:
        if not root.is_relative_to(plugin_root):
            return "path points outside the plugin folder"
        if not root.is_dir():
            return "path is not a folder"
        with self._lock:
            if pack_id in self._packs:
                return "duplicate icon pack id"
        return None

    @staticmethod
    def _icons_of(pack: _Pack) -> list[str]:
        found: set[str] = set()
        for path in pack.root.rglob("*"):
            if path.suffix.lower() in _IMAGE_SUFFIXES and path.is_file():
                if path.resolve().is_relative_to(pack.root):
                    found.add(path.relative_to(pack.root).as_posix())
        ordered = [name for name in _declared_order(pack.root) if name in found]
        return ordered + sorted(found - set(ordered))


def _declared_order(root: Path) -> list[str]:
    try:
        data = json.loads((root / "icons.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    names = (entry.get("path") if isinstance(entry, dict) else entry for entry in data)
    return [name for name in names if isinstance(name, str)]
