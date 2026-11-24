"""Size limit shared by every icon reader."""

from decker.common import config


def max_icon_bytes() -> int:
    """Return the library's maximum image file size in bytes (`icons.max_file_size`)."""
    return config.get_as("icons.max_file_size", int)
