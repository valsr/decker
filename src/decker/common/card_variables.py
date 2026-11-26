"""Rules for per-card action variables: names, slots and value size."""

import re

NAME_PATTERN: re.Pattern[str] = re.compile(r"[a-z][a-z0-9_-]*")
RESERVED_NAME = "status"
MAX_SLOT_LENGTH = 32
MAX_VALUE_BYTES = 64 * 1024


def is_valid_name(name: str) -> bool:
    """Whether ``name`` is a well-formed variable name."""
    return NAME_PATTERN.fullmatch(name) is not None


def is_valid_slot(slot: str) -> bool:
    """Whether ``slot`` is a valid name, short enough, and not the reserved one."""
    return is_valid_name(slot) and len(slot) <= MAX_SLOT_LENGTH and slot != RESERVED_NAME
