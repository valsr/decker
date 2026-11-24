"""Icon references (``pack:<pack id>/<path>``, ``fas:<name>``, ``user:<id>``) and the value of an ``icon`` setting."""

import re
from collections.abc import Mapping
from typing import Any

from decker.common.text_style import is_color

ICON_REF_PREFIX = "pack:"
DECKER_ICON_REF = "decker:logo"
DEFAULT_ICON_COLOR = "#ffffff"
BUILTIN_PACK_PREFIX = "decker.builtin."
USER_PACK_ID = "decker.user"
USER_REF_PREFIX = "user:"
BUILTIN_PACK_IDS: Mapping[str, str] = {
    "fab": "decker.builtin.fontawesome.brands",
    "far": "decker.builtin.fontawesome.regular",
    "fas": "decker.builtin.fontawesome.solid",
}
_SHORT_PREFIX_OF = {pack_id: prefix for prefix, pack_id in BUILTIN_PACK_IDS.items()}
_ICON_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_USER_ICON_ID = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?")  # what usericons.normalize_id produces
_USER_SUFFIXES = (".svg", ".png", ".gif")
_DEFAULTS: Mapping[str, Any] = {"ref": "", "color": DEFAULT_ICON_COLOR}


_DISPLAY_SUFFIXES = (".svg", ".png", ".jpg", ".jpeg", ".gif")


def icon_display_name(path: str) -> str:
    """The name to show for an icon path: its last segment without one trailing image suffix.

    System icon names keep their dots (``org.gnome.Foo``), which ``Path.stem`` would cut at the last one.
    """
    name = path.rsplit("/", 1)[-1]
    lowered = name.lower()
    for suffix in _DISPLAY_SUFFIXES:
        if lowered.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def make_icon_ref(pack_id: str, path: str) -> str:
    """The reference for icon ``path`` of pack ``pack_id`` (the short ``fas:name`` or ``user:id`` form if one fits)."""
    if pack_id == USER_PACK_ID:
        icon_id = user_icon_id(path)
        if icon_id is not None:
            return f"{USER_REF_PREFIX}{icon_id}"
    short = _SHORT_PREFIX_OF.get(pack_id)
    name = path.removesuffix(".svg")
    if short is not None and name != path and _ICON_NAME.fullmatch(name):
        return f"{short}:{name}"
    return f"{ICON_REF_PREFIX}{pack_id}/{path}"


def parse_builtin_ref(ref: Any) -> tuple[str, str] | None:
    """``(pack_id, "<name>.svg")`` for a ``fab:``/``far:``/``fas:`` short reference, else None."""
    if not isinstance(ref, str):
        return None
    prefix, colon, name = ref.partition(":")
    pack_id = BUILTIN_PACK_IDS.get(prefix)
    if not colon or pack_id is None or _ICON_NAME.fullmatch(name) is None:
        return None
    return pack_id, f"{name}.svg"


def user_icon_id(path: str) -> str | None:
    """The id of a User icon file name or bare id (``logo.svg`` and ``logo`` give ``logo``), else None."""
    lowered = path.lower()
    stem = next((path[: -len(s)] for s in _USER_SUFFIXES if lowered.endswith(s)), path)
    return stem if _USER_ICON_ID.fullmatch(stem) else None


def parse_icon_ref(ref: Any) -> tuple[str, str] | None:
    """``(pack_id, path)`` for a well-formed reference, else None; absolute and ``..`` paths are refused.

    A ``user:<id>`` reference gives the bare id as its path; the User pack resolves it to the file.
    """
    if not isinstance(ref, str):
        return None
    short = parse_builtin_ref(ref)
    if short is not None:
        return short
    if ref.startswith(USER_REF_PREFIX):
        icon_id = ref[len(USER_REF_PREFIX) :]
        return (USER_PACK_ID, icon_id) if _USER_ICON_ID.fullmatch(icon_id) else None
    if not ref.startswith(ICON_REF_PREFIX):
        return None
    pack_id, slash, path = ref[len(ICON_REF_PREFIX) :].partition("/")
    if not slash or not pack_id or not path or path.startswith("/") or ".." in path.split("/"):
        return None
    return pack_id, path


def normalize_icon_value(value: Any, defaults: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Complete ``{"ref", "color"}`` dict: each invalid or missing field falls back to ``defaults``."""
    base = defaults if defaults is not None else _DEFAULTS
    source: Mapping[str, Any] = value if isinstance(value, Mapping) else {}
    ref = source.get("ref", base["ref"])
    color = source.get("color", base["color"])
    return {
        "ref": ref if ref == "" or parse_icon_ref(ref) is not None else base["ref"],
        "color": color if is_color(color) else base["color"],
    }
