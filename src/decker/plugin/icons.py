"""Locate and read action icon images declared in plugin manifests."""

import logging
from pathlib import Path

from decker.asset.builtinicons import read_builtin_ref
from decker.common.icon_limits import max_icon_bytes

# Elgato manifests name icons without an extension; @2x is the high-resolution variant.
_CANDIDATE_SUFFIXES = ("", ".svg", "@2x.png", ".png")


def resolve_icon(plugin_dir: Path, icon: str | None) -> Path | None:
    """Return the icon file for a manifest Icon entry, or None if it is unset, missing or outside plugin_dir."""
    if not icon:
        return None
    root = plugin_dir.resolve()
    for suffix in _CANDIDATE_SUFFIXES:
        candidate = (root / f"{icon}{suffix}").resolve()
        if not candidate.is_relative_to(root):
            logging.warning("Ignoring icon %r: it points outside %s", icon, plugin_dir)
            return None
        if candidate.is_file():
            return candidate
    return None


def read_icon(plugin_dir: Path, icon: str | None) -> bytes | None:
    """The icon's bytes (a built-in ``fas:play`` ref or a plugin file), or None if missing or over the size limit."""
    builtin = read_builtin_ref(icon)
    if builtin is not None:
        return builtin
    path = resolve_icon(plugin_dir, icon)
    if path is None:
        return None
    try:
        if path.stat().st_size > max_icon_bytes():
            logging.warning("Ignoring icon %s: larger than %d bytes", path, max_icon_bytes())
            return None
        return path.read_bytes()
    except OSError as exc:
        logging.warning("Could not read icon %s: %s", path, exc)
        return None
