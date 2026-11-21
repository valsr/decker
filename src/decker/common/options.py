"""Limits and cleaning for the options a plugin supplies to a dynamic choice field."""

import re
from typing import Any

MAX_OPTIONS = 256
MAX_TEXT = 256
MAX_SOURCE_LENGTH = 64

_SOURCE = re.compile(r"[a-z][a-z0-9_]*")


def is_valid_source(name: str) -> bool:
    """Whether `name` is a well-formed options source name."""
    return len(name) <= MAX_SOURCE_LENGTH and _SOURCE.fullmatch(name) is not None


def clean_options(raw: Any) -> list[dict[str, str]]:
    """Keep well-formed, unique-valued `{label, value}` string entries, cut and capped to the limits."""
    if not isinstance(raw, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("label"), str):
            continue
        if not isinstance(entry.get("value"), str):
            continue
        value = entry["value"][:MAX_TEXT]
        if value in seen:
            continue
        seen.add(value)
        result.append({"label": entry["label"][:MAX_TEXT], "value": value})
        if len(result) >= MAX_OPTIONS:
            break
    return result
