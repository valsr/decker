"""Font Awesome Free icons bundled with Decker, served as the virtual ``decker.builtin.*`` packs."""

import logging
from pathlib import Path
from typing import Any

from decker.common.icon_limits import max_icon_bytes
from decker.common.icon_ref import BUILTIN_PACK_IDS, parse_builtin_ref

BUILTIN_PLUGIN_ID = "decker.builtin"
BUILTIN_PLUGIN_NAME = "Built-In"
FONTAWESOME_VERSION = "7.3.1"
FONTAWESOME_DIR = Path(__file__).parent.parent / "assets" / "fontawesome"

# (pack id, folder, name, description suffix), in picker order
_PACKS: tuple[tuple[str, str, str, str], ...] = (
    (BUILTIN_PACK_IDS["fab"], "brands", "FontAwesome Brands", "Brands"),
    (BUILTIN_PACK_IDS["far"], "regular", "FontAwesome Regular", "Regular"),
    (BUILTIN_PACK_IDS["fas"], "solid", "FontAwesome Solids", "Solid"),
)
_FOLDERS = {pack_id: folder for pack_id, folder, _name, _desc in _PACKS}


class BuiltinIcons:
    """The bundled Font Awesome packs (read-only)."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = (root if root is not None else FONTAWESOME_DIR).resolve()
        self._listed: dict[str, list[str]] = {}

    def owns(self, pack_id: str) -> bool:
        """Whether ``pack_id`` is one of the three built-in packs."""
        return pack_id in _FOLDERS

    def list_packs(self) -> list[dict[str, Any]]:
        """The three packs, shaped like plugin packs."""
        return [
            {
                "id": pack_id,
                "name": name,
                "description": f"Font Awesome Free {FONTAWESOME_VERSION}: {suffix}",
                "plugin_id": BUILTIN_PLUGIN_ID,
                "plugin_name": BUILTIN_PLUGIN_NAME,
                "plugin_version": FONTAWESOME_VERSION,
                "icon_count": len(self.list_icons(pack_id)),
            }
            for pack_id, _folder, name, suffix in _PACKS
        ]

    def list_icons(self, pack_id: str) -> list[str]:
        """The pack's ``<name>.svg`` paths, sorted."""
        folder = _FOLDERS.get(pack_id)
        if folder is None:
            return []
        if pack_id not in self._listed:
            self._listed[pack_id] = sorted(p.name for p in (self._root / folder).glob("*.svg"))
        return self._listed[pack_id]

    def read_icon(self, pack_id: str, name: Any) -> bytes | None:
        """The icon's bytes, or None for an unknown pack, a path outside the pack, a non-SVG or an oversized file."""
        folder = _FOLDERS.get(pack_id)
        if folder is None or not isinstance(name, str) or not name.endswith(".svg"):
            return None
        pack_root = self._root / folder
        try:
            path = (pack_root / name).resolve()
            if not path.is_relative_to(pack_root) or not path.is_file() or path.stat().st_size > max_icon_bytes():
                return None
            return path.read_bytes()
        except (OSError, ValueError) as exc:
            logging.debug("Could not read built-in icon %s/%s: %s", pack_id, name, exc)
            return None


def read_builtin_ref(ref: Any) -> bytes | None:
    """The bytes of the icon a ``fab:``/``far:``/``fas:`` short reference names, or None."""
    parsed = parse_builtin_ref(ref)
    return BuiltinIcons().read_icon(*parsed) if parsed is not None else None
