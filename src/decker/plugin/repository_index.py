"""Plugin repository index (format v1) parsing and validation."""

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from decker.common.icon_ref import parse_builtin_ref
from decker.common.platforms import PlatformSpec, parse_platform_spec
from decker.common.version import is_compatible
from decker.plugin.manifest import is_valid_plugin_id, is_valid_version

MAX_INDEX_BYTES = 4 * 1024 * 1024
SUPPORTED_INDEX_FORMAT = 1
_MAX_NAME = 100
_MAX_DESCRIPTION = 2000
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")


class RepositoryIndexError(Exception):
    """A repository index is unusable as a whole."""


class _EntryError(ValueError):
    """A single index entry is invalid and gets skipped."""


@dataclass(frozen=True)
class IndexVersion:
    """One downloadable version of a plugin."""

    version: str
    decker: str | None
    url: str
    size: int
    sha256: str
    released: str
    contributes: dict[str, list[str]]
    platforms: PlatformSpec | None = None


@dataclass(frozen=True)
class IndexPlugin:
    """A plugin offered by a repository; versions are newest first."""

    id: str
    name: str
    author: str
    description: str
    category: str
    homepage: str
    icon_url: str | None
    versions: tuple[IndexVersion, ...]
    icon_ref: str | None = None


@dataclass(frozen=True)
class RepositoryIndex:
    """A parsed repository index plus warnings about skipped entries."""

    name: str
    generated: str
    plugins: tuple[IndexPlugin, ...]
    warnings: tuple[str, ...]


def parse_index(data: bytes, index_url: str) -> RepositoryIndex:
    """Parse and validate index bytes; invalid entries are skipped with a warning."""
    if len(data) > MAX_INDEX_BYTES:
        raise RepositoryIndexError(f"Index exceeds {MAX_INDEX_BYTES} bytes")
    try:
        doc = json.loads(data)
    except (ValueError, RecursionError) as exc:
        raise RepositoryIndexError(f"Index is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise RepositoryIndexError("Index must be a JSON object")
    if doc.get("format") != SUPPORTED_INDEX_FORMAT or isinstance(doc.get("format"), bool):
        raise RepositoryIndexError(f"Unsupported index format: {doc.get('format')!r}")
    raw_plugins = doc.get("plugins")
    if not isinstance(raw_plugins, list):
        raise RepositoryIndexError("Index 'plugins' must be a list")

    warnings: list[str] = []
    plugins: list[IndexPlugin] = []
    seen: set[str] = set()
    for raw in raw_plugins:
        label = raw.get("id") if isinstance(raw, dict) else None
        try:
            plugin = _parse_plugin(raw, index_url, warnings)
        except _EntryError as exc:
            warnings.append(f"Skipped plugin {label!r}: {exc}")
            continue
        if plugin is None:
            continue
        if plugin.id in seen:
            warnings.append(f"Skipped duplicate plugin id {plugin.id!r}")
            continue
        seen.add(plugin.id)
        plugins.append(plugin)
    return RepositoryIndex(
        name=_text(doc.get("name"), _MAX_NAME) or "Repository",
        generated=_text(doc.get("generated"), _MAX_NAME),
        plugins=tuple(plugins),
        warnings=tuple(warnings),
    )


def _text(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _resolve_url(value: Any, index_url: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _EntryError("missing url")
    try:
        url = urljoin(index_url, value.strip())
        scheme = urlparse(url).scheme
        allowed = {"http", "https"} | ({"file"} if urlparse(index_url).scheme == "file" else set())
    except ValueError as exc:
        raise _EntryError(f"invalid url: {exc}") from exc
    if scheme not in allowed:
        raise _EntryError(f"url scheme {scheme!r} not allowed")
    return url


def _parse_plugin(raw: Any, index_url: str, warnings: list[str]) -> IndexPlugin | None:
    if not isinstance(raw, dict):
        raise _EntryError("not an object")
    plugin_id = raw.get("id")
    if not is_valid_plugin_id(plugin_id):
        raise _EntryError("invalid id")
    raw_versions = raw.get("versions")
    if not isinstance(raw_versions, list):
        raise _EntryError("'versions' must be a list")
    versions: list[IndexVersion] = []
    for raw_version in raw_versions:
        try:
            versions.append(_parse_version(raw_version, index_url))
        except _EntryError as exc:
            warnings.append(f"Skipped version of {plugin_id!r}: {exc}")
    if not versions:
        if not raw_versions:
            raise _EntryError("no versions")
        return None
    versions.sort(key=lambda v: Version(v.version), reverse=True)
    icon_url: str | None = None
    icon_ref: str | None = None
    raw_icon = raw.get("icon")
    if isinstance(raw_icon, str) and parse_builtin_ref(raw_icon.strip()) is not None:
        icon_ref = raw_icon.strip()
    elif raw_icon:
        try:
            icon_url = _resolve_url(raw["icon"], index_url)
        except _EntryError as exc:
            warnings.append(f"Ignored icon of {plugin_id!r}: {exc}")
    return IndexPlugin(
        id=str(plugin_id),
        name=_text(raw.get("name"), _MAX_NAME) or str(plugin_id),
        author=_text(raw.get("author"), _MAX_NAME),
        description=_text(raw.get("description"), _MAX_DESCRIPTION),
        category=_text(raw.get("category"), _MAX_NAME),
        homepage=_text(raw.get("homepage"), 500),
        icon_url=icon_url,
        versions=tuple(versions),
        icon_ref=icon_ref,
    )


def _parse_version(raw: Any, index_url: str) -> IndexVersion:
    if not isinstance(raw, dict):
        raise _EntryError("version is not an object")
    version = raw.get("version")
    if not is_valid_version(version):
        raise _EntryError(f"invalid version {version!r}")
    try:
        Version(str(version))
    except InvalidVersion as exc:
        raise _EntryError(f"unsortable version {version!r}") from exc
    size = raw.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise _EntryError(f"invalid size {size!r}")
    sha256 = raw.get("sha256")
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise _EntryError("invalid sha256")
    return IndexVersion(
        version=str(version),
        decker=_parse_specifier(raw.get("decker")),
        url=_resolve_url(raw.get("url"), index_url),
        size=size,
        sha256=sha256.lower(),
        released=_text(raw.get("released"), _MAX_NAME),
        contributes=_parse_contributes(raw.get("contributes")),
        platforms=_parse_platforms(raw),
    )


def _parse_platforms(raw: dict[str, Any]) -> PlatformSpec | None:
    try:
        return parse_platform_spec(raw.get("platforms"), raw.get("linux"))
    except ValueError as exc:
        raise _EntryError(f"invalid platform data: {exc}") from exc


def _parse_specifier(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _EntryError("invalid decker specifier")
    try:
        SpecifierSet(value)
    except InvalidSpecifier as exc:
        raise _EntryError(f"invalid decker specifier {value!r}") from exc
    return value.strip() or None


def _parse_contributes(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(kind): [_text(n, _MAX_NAME) for n in names if isinstance(n, str)]
        for kind, names in value.items()
        if isinstance(names, list)
    }


def latest_compatible(versions: tuple[IndexVersion, ...]) -> IndexVersion | None:
    """Return the highest version that is compatible with this Decker, if any."""
    best: tuple[Version, IndexVersion] | None = None
    for item in versions:
        if not is_compatible(item.decker):
            continue
        try:
            parsed = Version(item.version)
        except InvalidVersion:
            continue
        if best is None or parsed > best[0]:
            best = (parsed, item)
    return best[1] if best else None
