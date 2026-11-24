"""The system's freedesktop icon theme, indexed so it can be served as virtual icon packs."""

import configparser
import hashlib
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from decker.common import config
from decker.common.icon_limits import max_icon_bytes

SYSTEM_PLUGIN_ID = "decker.system"
SYSTEM_PLUGIN_NAME = "System Icons"
PACK_PREFIX = "decker.system."
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("actions", "Actions"),
    ("applications", "Applications"),
    ("categories", "Categories"),
    ("devices", "Devices"),
    ("emblems", "Emblems"),
    ("emotes", "Emotes"),
    ("mimetypes", "Mimetypes"),
    ("places", "Places"),
    ("status", "Status"),
    ("other", "Other"),
)
IMAGE_SUFFIXES = (".svg", ".png", ".jpg", ".jpeg")
_KNOWN_CATEGORIES = {key for key, _label in CATEGORIES} - {"other"}
_FALLBACK_THEMES = ("Adwaita", "hicolor")


@dataclass(frozen=True)
class ThemeIndex:
    """The icons of one theme chain: category key -> icon name -> the file to serve."""

    theme: str
    version: str
    icons: Mapping[str, Mapping[str, Path]]


def category_of(context: str | None) -> str:
    """The pack key for a theme directory's ``Context`` (anything unknown, or none, is "other")."""
    key = (context or "").strip().lower()
    return key if key in _KNOWN_CATEGORIES else "other"


def icon_search_dirs(env: Mapping[str, str], home: Path) -> list[Path]:
    """The freedesktop icon base directories in lookup order (they may not all exist)."""
    data_home = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    data_dirs = [d for d in (env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if d]
    return [data_home / "icons", home / ".icons", *(Path(d) / "icons" for d in data_dirs)]


def detect_theme(configured: str, home: Path, search_dirs: Sequence[Path]) -> str:
    """The icon theme: configured, else the GTK or KDE setting, else Adwaita, else hicolor (installed ones only)."""
    candidates = [configured.strip(), _gtk_theme(home), _kde_theme(home), *_FALLBACK_THEMES]
    for name in candidates:
        if name and any((base / name / "index.theme").is_file() for base in search_dirs):
            return name
    return "hicolor"


def build_index(theme: str, search_dirs: Sequence[Path]) -> ThemeIndex:
    """Index ``theme`` and the themes it inherits from (ending at hicolor); unreadable parts are skipped."""
    roots = _theme_roots(theme, search_dirs)
    allowed = _allowed_prefixes(search_dirs)
    best: dict[tuple[str, str], tuple[int, tuple[int, int, int], Path]] = {}
    for position, root, parser in roots:
        _scan_theme(root, parser, position, allowed, best)
    icons: dict[str, dict[str, Path]] = {}
    for (category, icon_name), (_position, _quality, path) in best.items():
        icons.setdefault(category, {})[icon_name] = path
    return ThemeIndex(theme, _version(theme, roots), icons)


def theme_version(theme: str, search_dirs: Sequence[Path]) -> str:
    """The version string ``build_index`` would give, without scanning any icons."""
    return _version(theme, _theme_roots(theme, search_dirs))


def _version(theme: str, roots: Sequence[tuple[int, Path, configparser.ConfigParser]]) -> str:
    """Hash of what changes when a theme does: its index, its icon cache and its icon directories' mtimes."""
    digest = hashlib.md5(usedforsecurity=False)
    for _position, root, parser in roots:
        stamps = [root / "index.theme", root / "icon-theme.cache", *(root / d for d in _directories(parser))]
        for path in stamps:
            digest.update(f"{path}:{_mtime(path)};".encode())
    return f"{theme}-{digest.hexdigest()[:8]}"


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except (OSError, ValueError):
        return 0


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return path


def _directories(parser: configparser.ConfigParser) -> list[str]:
    """The theme's icon directories that are safe to join onto its root (relative, no ``..``, no NUL)."""
    listed = (d.strip() for d in parser.get("Icon Theme", "Directories", fallback="").split(","))
    return [d for d in listed if d and "\x00" not in d and not Path(d).is_absolute() and ".." not in Path(d).parts]


def _theme_roots(theme: str, search_dirs: Sequence[Path]) -> list[tuple[int, Path, configparser.ConfigParser]]:
    """``(chain position, theme directory, theme definition)`` for every base dir that holds a theme of the chain.

    A theme is defined by the first ``index.theme`` found; its icons may live in any base dir, including ones
    (such as ``~/.local/share/icons/hicolor``) that have no index of their own.
    """
    roots: list[tuple[int, Path, configparser.ConfigParser]] = []
    for position, name in enumerate(_theme_chain(theme, search_dirs)):
        parser = _definition(name, search_dirs)
        if parser is None:
            continue
        roots += [(position, base / name, parser) for base in search_dirs if (base / name).is_dir()]
    return roots


def _definition(name: str, search_dirs: Sequence[Path]) -> configparser.ConfigParser | None:
    for base in search_dirs:
        parser = _read_ini(base / name / "index.theme")
        if parser is not None:
            return parser
    return None


def _theme_chain(theme: str, search_dirs: Sequence[Path]) -> list[str]:
    """The theme, then what it inherits from depth-first, then hicolor as the final fallback."""
    chain: list[str] = []

    def visit(name: str) -> None:
        if name in chain:
            return
        chain.append(name)
        parser = _definition(name, search_dirs)
        if parser is None:
            return
        for parent in (p.strip() for p in parser.get("Icon Theme", "Inherits", fallback="").split(",")):
            if parent and parent != "hicolor":
                visit(parent)

    visit(theme)
    if "hicolor" not in chain:
        chain.append("hicolor")
    return chain


def _scan_theme(
    root: Path,
    parser: configparser.ConfigParser,
    position: int,
    allowed: Sequence[str],
    best: dict[tuple[str, str], tuple[int, tuple[int, int, int], Path]],
) -> None:
    for directory in _directories(parser):
        if not parser.has_section(directory):
            continue
        section = parser[directory]
        category = category_of(section.get("Context"))
        scalable = section.get("Type", "").strip().lower() == "scalable" or "scalable" in directory.lower()
        try:
            entries = list(os.scandir(root / directory))
        except (OSError, ValueError):
            continue
        for entry in entries:
            icon_name = _icon_name(entry.name)
            if icon_name is None or not _servable(entry, allowed):
                continue
            quality = (1 if scalable else 0, _size(section), 1 if entry.name.lower().endswith(".svg") else 0)
            key = (category, icon_name)
            current = best.get(key)
            if current is None or (position, tuple(-q for q in quality)) < (current[0], tuple(-q for q in current[1])):
                best[key] = (position, quality, Path(entry.path))


def _servable(entry: os.DirEntry[str], allowed: Sequence[str]) -> bool:
    """A regular image file, or a symlink to an existing image file inside the icon directories.

    Symlinks are judged from their link text (resolving tens of thousands of them is slow); ``read_icon`` checks
    the fully resolved target again before serving anything.
    """
    try:
        if not entry.is_symlink():
            return entry.is_file(follow_symlinks=False)
        target = os.path.normpath(os.path.join(os.path.dirname(entry.path), os.readlink(entry.path)))
        if not target.lower().endswith(IMAGE_SUFFIXES) or not any(target.startswith(root + os.sep) for root in allowed):
            return False
        return entry.is_file()  # follows the link: false for broken links, loops and directories
    except (OSError, ValueError):
        return False


def _inside(path: Path, roots: Sequence[Path]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


def _allowed_prefixes(search_dirs: Sequence[Path]) -> tuple[str, ...]:
    """The search directories as plain strings, both as given and fully resolved."""
    return tuple({os.path.normpath(str(d)) for d in search_dirs} | {str(_resolved(d)) for d in search_dirs})


def _size(section: configparser.SectionProxy) -> int:
    try:
        return int(section.get("Size", "0")) * max(int(section.get("Scale", "1")), 1)
    except ValueError:
        return 0


def _icon_name(filename: str) -> str | None:
    lowered = filename.lower()
    for suffix in IMAGE_SUFFIXES:
        if lowered.endswith(suffix) and len(filename) > len(suffix):
            return filename[: -len(suffix)]
    return None


_INI_CACHE: dict[Path, tuple[int, configparser.ConfigParser | None]] = {}


def _parse_ini(path: Path) -> configparser.ConfigParser | None:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # keys are case-sensitive in desktop files
    try:
        with path.open(encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, UnicodeDecodeError, configparser.Error):
        return None
    return parser


def _read_ini(path: Path) -> configparser.ConfigParser | None:
    """Parse an ini/desktop file, reusing the last parse while its mtime is unchanged (theme files are large)."""
    try:
        mtime = path.stat().st_mtime_ns
    except (OSError, ValueError):
        return None
    cached = _INI_CACHE.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    parser = _parse_ini(path)
    _INI_CACHE[path] = (mtime, parser)
    return parser


def _gtk_theme(home: Path) -> str:
    parser = _read_ini(home / ".config" / "gtk-3.0" / "settings.ini")
    return _clean(parser.get("Settings", "gtk-icon-theme-name", fallback="")) if parser is not None else ""


def _kde_theme(home: Path) -> str:
    parser = _read_ini(home / ".config" / "kdeglobals")
    return _clean(parser.get("Icons", "Theme", fallback="")) if parser is not None else ""


def _clean(value: str) -> str:
    return value.strip().strip("'\"").strip()


def _configured_theme() -> str:
    return str(config.get_or_default("icons.system_theme", "") or "")


class SystemIcons:
    """The active icon theme as virtual packs ``decker.system.<category>`` (read-only, built lazily).

    ``enabled`` defaults to Linux only; a disabled instance exposes nothing."""

    def __init__(
        self,
        search_dirs: Sequence[Path] | None = None,
        home: Path | None = None,
        configured_theme: Callable[[], str] | None = None,
        recheck_s: float = 2.0,
        enabled: bool | None = None,
    ) -> None:
        self._enabled = sys.platform == "linux" if enabled is None else enabled
        self._home = home if home is not None else Path.home()
        self._search_dirs = list(search_dirs) if search_dirs is not None else icon_search_dirs(os.environ, self._home)
        self._configured = configured_theme or _configured_theme
        self._recheck_s = recheck_s
        self._index: ThemeIndex | None = None
        self._checked = 0.0
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._warm_thread: threading.Thread | None = None

    def warm(self) -> None:
        """Build the index in the background so the first icon request does not wait for it."""
        if not self._enabled:
            return
        self._warm_thread = threading.Thread(target=self._warm, name="system-icons-warm", daemon=True)
        self._warm_thread.start()

    def _warm(self) -> None:
        try:
            self._snapshot()
        except Exception:  # pylint: disable=broad-except
            logging.warning("Could not index the system icon theme", exc_info=True)

    def theme_name(self) -> str:
        """The theme in use (see ``detect_theme``)."""
        return detect_theme(self._configured(), self._home, self._search_dirs)

    def version(self) -> str:
        """``<theme>-<hash>``; changes when the theme or its index files change."""
        return self._snapshot().version

    def owns(self, pack_id: str) -> bool:
        """Whether ``pack_id`` is one of the ten system packs."""
        return self._enabled and _category_of_pack(pack_id) is not None

    def list_packs(self) -> list[dict[str, Any]]:
        """The non-empty category packs, shaped like plugin packs, in category order."""
        if not self._enabled:
            return []
        index = self._snapshot()
        return [
            {
                "id": f"{PACK_PREFIX}{key}",
                "name": label,
                "description": f"System icons: {label}",
                "plugin_id": SYSTEM_PLUGIN_ID,
                "plugin_name": SYSTEM_PLUGIN_NAME,
                "plugin_version": index.version,
                "icon_count": len(index.icons[key]),
            }
            for key, label in CATEGORIES
            if index.icons.get(key)
        ]

    def list_icons(self, pack_id: str) -> list[str]:
        """The category's icon names, sorted (empty for anything that is not a system pack)."""
        category = _category_of_pack(pack_id) if self._enabled else None
        return sorted(self._snapshot().icons.get(category, {})) if category is not None else []

    def read_icon(self, pack_id: str, name: Any) -> bytes | None:
        """The bytes of an icon, or None for an unknown name, a non-image or oversized file, or a read error."""
        category = _category_of_pack(pack_id) if self._enabled else None
        if category is None or not isinstance(name, str) or not name:
            return None
        path = self._snapshot().icons.get(category, {}).get(name)  # names are looked up, never joined into a path
        if path is None:
            return None
        try:
            return self._read_servable(path)
        except (OSError, ValueError) as exc:
            logging.debug("Could not read system icon %s: %s", path, exc)
            return None

    def _read_servable(self, path: Path) -> bytes | None:
        """The file's bytes if it resolves to an image inside the icon directories and is small enough."""
        resolved = path.resolve()
        within = _inside(resolved, [_resolved(d) for d in self._search_dirs])
        if resolved.suffix.lower() not in IMAGE_SUFFIXES or not resolved.is_file() or not within:
            return None
        return resolved.read_bytes() if resolved.stat().st_size <= max_icon_bytes() else None

    def _snapshot(self) -> ThemeIndex:
        """The current index. The theme is re-checked at most every ``recheck_s`` seconds, by one thread at a time;
        meanwhile readers keep getting the previous index. Only the very first build makes callers wait."""
        now = time.monotonic()
        with self._lock:
            index, checked = self._index, self._checked
        if index is not None and now - checked < self._recheck_s:
            return index
        if index is not None:
            if not self._build_lock.acquire(blocking=False):  # pylint: disable=consider-using-with
                return index  # another thread is refreshing; its result is picked up by the next call
        else:
            self._build_lock.acquire()  # pylint: disable=consider-using-with
        try:
            return self._refresh(now)
        finally:
            self._build_lock.release()

    def _refresh(self, now: float) -> ThemeIndex:
        """Rebuild the index if the theme changed (caller holds the build lock; no other lock is held during I/O)."""
        with self._lock:
            index = self._index
        theme = self.theme_name()
        version = theme_version(theme, self._search_dirs)
        if index is not None and index.version == version:
            with self._lock:
                self._checked = now
            return index
        built = build_index(theme, self._search_dirs)
        with self._lock:
            self._index, self._checked = built, now
        return built


def _category_of_pack(pack_id: str) -> str | None:
    if not isinstance(pack_id, str) or not pack_id.startswith(PACK_PREFIX):
        return None
    key = pack_id[len(PACK_PREFIX) :]
    return key if any(key == known for known, _label in CATEGORIES) else None
