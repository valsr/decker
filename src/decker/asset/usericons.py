"""The user's own imported icons, stored as plain files in the user icons folder."""

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from decker.asset.iconresize import (
    ImageImportError,
    ImportOptions,
    convert_raster,
    preview_png,
)
from decker.asset.svgcheck import validate_svg
from decker.common.icon_limits import max_icon_bytes
from decker.common.icon_ref import USER_PACK_ID
from decker.common.paths import user_icons

USER_PLUGIN_ID = "decker.user"
USER_PLUGIN_NAME = "User"

_EXTENSIONS = (".svg", ".png", ".gif")
_MAX_ID_LENGTH = 64
_INVALID = re.compile(r"[^a-z0-9_-]+")


def normalize_id(name: str) -> str:
    """Return the icon id for a name (lowercase ``[a-z0-9_-]``), or ``""`` when nothing usable remains."""
    text = _INVALID.sub("-", name.lower()).strip("-_")
    return text[:_MAX_ID_LENGTH].strip("-_")


def _is_svg(data: bytes) -> bool:
    return data.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<")


@dataclass(frozen=True)
class ImportResult:
    """Outcome of an icon import."""

    ok: bool
    icon_id: str = ""
    file: str = ""
    error: str = ""
    suggestion: str = ""

    def to_json(self) -> str:
        """Serialise for the RPC layer."""
        return json.dumps(
            {"ok": self.ok, "id": self.icon_id, "file": self.file, "error": self.error, "suggestion": self.suggestion}
        )


class UserIcons:
    """Listing, reading and importing of the icons in the user icons folder."""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory: Path | None = directory
        self._lock = threading.Lock()

    @property
    def directory(self) -> Path:
        """The icons folder (the default is resolved on every use)."""
        return self._directory if self._directory is not None else user_icons()

    def list_icons(self) -> list[str]:
        """Sorted file names of the icons present."""
        try:
            entries = list(self.directory.iterdir())
        except OSError:
            return []
        return sorted(p.name for p in entries if p.suffix.lower() in _EXTENSIONS and p.is_file())

    def resolve(self, name: str) -> str | None:
        """The listed file a file name or bare id (``logo`` for ``logo.svg``) names, or None."""
        listed = self.list_icons()
        if name in listed:
            return name
        return next((file for file in listed if Path(file).stem.lower() == name), None)

    def read_icon(self, name: str) -> bytes | None:
        """Return the icon's bytes (by file name or bare id), or None when it is not listed or too large."""
        file = self.resolve(name)
        if file is None:
            return None
        path = self.directory / file
        try:
            if path.stat().st_size > max_icon_bytes():
                return None
            return path.read_bytes()
        except OSError:
            return None

    def delete_icon(self, name: str) -> bool:
        """Delete a listed icon file; False when no such icon is listed or it cannot be removed."""
        with self._lock:
            if name not in self.list_icons():
                return False
            try:
                (self.directory / name).unlink()
            except OSError:
                return False
        return True

    def pack_info(self) -> dict[str, Any]:
        """The pack entry for the icon pack listing."""
        return {
            "id": USER_PACK_ID,
            "name": USER_PLUGIN_NAME,
            "description": "Icons you imported",
            "plugin_id": USER_PLUGIN_ID,
            "plugin_name": USER_PLUGIN_NAME,
            "plugin_version": "1",
            "icon_count": len(self.list_icons()),
        }

    def exists(self, icon_id: str) -> bool:
        """Whether an icon with this id exists under any supported extension."""
        return any(Path(name).stem.lower() == icon_id for name in self.list_icons())

    def suggest_id(self, name: str) -> str:
        """The first free id for a name: ``id``, ``id-2``, ``id-3``, ..."""
        base = normalize_id(name) or "icon"
        candidate, number = base, 2
        while self.exists(candidate):
            candidate = f"{base}-{number}"
            number += 1
        return candidate

    def preview(self, data: bytes, options: ImportOptions) -> bytes:
        """Return what the icon would look like (SVG unchanged, rasters as a 144x144 PNG); writes nothing."""
        if len(data) > max_icon_bytes():
            raise ImageImportError(f"image is larger than {max_icon_bytes()} bytes")
        if _is_svg(data):
            validate_svg(data)
            return data
        return preview_png(data, options)

    def import_icon(self, name: str, data: bytes, options: ImportOptions) -> ImportResult:
        """Validate and store an icon under the id derived from name; never overwrites an existing icon."""
        icon_id = normalize_id(name)
        if not icon_id:
            return ImportResult(ok=False, error="invalid name")
        try:
            payload, extension = self._prepare(data, options)
        except ValueError as exc:
            return ImportResult(ok=False, icon_id=icon_id, error=str(exc))
        with self._lock:
            if self.exists(icon_id):
                return ImportResult(
                    ok=False, icon_id=icon_id, error="id already exists", suggestion=self.suggest_id(name)
                )
            file_name = f"{icon_id}.{extension}"
            try:
                self._write_atomic(file_name, payload)
            except OSError as exc:
                return ImportResult(ok=False, icon_id=icon_id, error=f"cannot write icon: {exc}")
        return ImportResult(ok=True, icon_id=icon_id, file=file_name)

    @staticmethod
    def _prepare(data: bytes, options: ImportOptions) -> tuple[bytes, str]:
        if len(data) > max_icon_bytes():
            raise ImageImportError(f"image is larger than {max_icon_bytes()} bytes")
        if _is_svg(data):
            validate_svg(data)
            return data, "svg"
        return convert_raster(data, options)

    def _write_atomic(self, file_name: str, payload: bytes) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=self.directory, prefix=".import-", suffix=".tmp")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
            os.replace(temp_name, self.directory / file_name)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
