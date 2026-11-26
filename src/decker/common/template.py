"""Opt-in ``{variable}`` expansion for key text."""

import re
from collections.abc import Mapping

TEMPLATE_VARIABLES: tuple[str, ...] = ("page_name", "page_index", "page_count", "current_profile", "profile_id")

_TOKEN = re.compile(r"\{\{|\}\}|\{([a-z_][a-z0-9_]*(?:\.[a-z][a-z0-9_-]*){0,2})\}")


def expand_template(text: str, variables: Mapping[str, str]) -> str:
    """Replace ``{name}`` with its value; ``{{``/``}}`` are literal braces, unknown names stay as typed."""

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        return variables.get(match.group(1), token)

    return _TOKEN.sub(_replace, text)


def template_names(text: str) -> frozenset[str]:
    """Return the ``{name}`` tokens in text, ignoring literal ``{{``/``}}``."""
    return frozenset(match.group(1) for match in _TOKEN.finditer(text) if match.group(1))
