"""Icon references (``pack:<pack id>/<path>``) and the value of an ``icon`` setting."""

import re
from collections.abc import Mapping
from typing import Any

from decker.common.text_style import is_color

ICON_REF_PREFIX = "pack:"
DECKER_ICON_REF = "decker:logo"
DEFAULT_ICON_COLOR = "#ffffff"
BUILTIN_PACK_PREFIX = "decker.builtin."
BUILTIN_PACK_IDS: Mapping[str, str] = {
    "fab": "decker.builtin.fontawesome.brands",
    "far": "decker.builtin.fontawesome.regular",
    "fas": "decker.builtin.fontawesome.solid",
}
_SHORT_PREFIX_OF = {pack_id: prefix for prefix, pack_id in BUILTIN_PACK_IDS.items()}
_ICON_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
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
    """The reference for icon ``path`` of pack ``pack_id`` (the short ``fas:name`` form for built-in packs)."""
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


def parse_icon_ref(ref: Any) -> tuple[str, str] | None:
    """``(pack_id, path)`` for a well-formed reference, else None; absolute and ``..`` paths are refused."""
    if not isinstance(ref, str):
        return None
    short = parse_builtin_ref(ref)
    if short is not None:
        return short
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
