"""SQLite-backed store of each device's ordered pages and its default page."""

import sqlite3
import threading
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from decker.common import paths as decker_paths
from decker.common.manager import Manager
from decker.common.model import PageInfo, ProfileInfo

if TYPE_CHECKING:
    from decker.server.server_context import ServerContext

_SCHEMA_PAGES = """
CREATE TABLE IF NOT EXISTS pages (
    page_id   TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    name      TEXT NOT NULL,
    position  INTEGER NOT NULL
)
"""

_SCHEMA_DEFAULTS = """
CREATE TABLE IF NOT EXISTS page_defaults (
    device_id TEXT PRIMARY KEY,
    page_id   TEXT NOT NULL
)
"""

_SCHEMA_PROFILES = """
CREATE TABLE IF NOT EXISTS profiles (
    device_id  TEXT NOT NULL,
    profile_id INTEGER NOT NULL,
    name       TEXT NOT NULL,
    icon       TEXT NOT NULL DEFAULT '',
    position   INTEGER NOT NULL,
    PRIMARY KEY (device_id, profile_id)
)
"""

_SCHEMA_PROFILE_DEFAULTS = """
CREATE TABLE IF NOT EXISTS profile_defaults (
    device_id  TEXT NOT NULL,
    profile_id INTEGER NOT NULL,
    page_id    TEXT NOT NULL,
    PRIMARY KEY (device_id, profile_id)
)
"""

_SCHEMA_ACTIVE_PROFILES = """
CREATE TABLE IF NOT EXISTS active_profiles (
    device_id  TEXT PRIMARY KEY,
    profile_id INTEGER NOT NULL
)
"""

_PAGE_NAME_PREFIX = "Page "
_PROFILE_NAME_PREFIX = "Profile "
DEFAULT_PROFILE_NAME = "Default"


def initial_page_id(device_id: str) -> str:
    """The id of a device's first page; key data stored before pages existed migrates to it."""
    return f"{device_id}-page-1"


class PageStore(Manager):
    """Persists profiles and their pages per device in decker.db. Every device has at least one profile.

    A device's first profile (id 0, "Default") with its first page ("Page 1", id initial_page_id) is created on first
    access. Page ids are unique per device, so a page identifies its profile.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self._db_path = Path(db_path) if db_path is not None else decker_paths.db()
        self._conn: sqlite3.Connection | None = None

    def create(self, context: "ServerContext") -> None:
        """Open the SQLite connection, initialise the schema and migrate pre-profile data."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        for schema in (
            _SCHEMA_PAGES,
            _SCHEMA_DEFAULTS,
            _SCHEMA_PROFILES,
            _SCHEMA_PROFILE_DEFAULTS,
            _SCHEMA_ACTIVE_PROFILES,
        ):
            self._db.execute(schema)
        profile_columns = [row[1] for row in self._db.execute("PRAGMA table_info(profiles)").fetchall()]
        if "icon_color" not in profile_columns:
            self._db.execute("ALTER TABLE profiles ADD COLUMN icon_color TEXT NOT NULL DEFAULT ''")
        columns = [row[1] for row in self._db.execute("PRAGMA table_info(pages)").fetchall()]
        if "profile_id" not in columns:
            self._db.execute("ALTER TABLE pages ADD COLUMN profile_id INTEGER NOT NULL DEFAULT 0")
        self._migrate_to_profiles()
        self._db.commit()

    def destroy(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def _db(self) -> sqlite3.Connection:
        assert self._conn is not None, "PageStore.create() must be called before use"
        return self._conn

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    def list_profiles(self, device_id: str) -> list[ProfileInfo]:
        """Return the device's profiles in dropdown order, creating "Default" with "Page 1" if it has none."""
        with self._lock:
            return self._profiles(device_id)

    def get_profile(self, device_id: str, profile_id: int) -> ProfileInfo | None:
        """Return the profile if the device has it, else None."""
        return next((p for p in self.list_profiles(device_id) if p.profile_id == profile_id), None)

    def add_profile(self, device_id: str, name: str | None = None) -> ProfileInfo:
        """Append a profile with the lowest unused id and one page; blank names become "Profile <id>"."""
        with self._lock:
            profiles = self._profiles(device_id)
            profile_id = _lowest_unused(p.profile_id for p in profiles)
            profile = ProfileInfo(profile_id, (name or "").strip() or f"{_PROFILE_NAME_PREFIX}{profile_id}", "")
            self._insert_profile(device_id, profile, position=self._next_profile_position(device_id))
            self._insert(device_id, PageInfo(uuid.uuid4().hex, f"{_PAGE_NAME_PREFIX}1"), 0, profile_id)
            return profile

    def rename_profile(self, device_id: str, profile_id: int, name: str) -> bool:
        """Rename a profile. Returns False for an unknown profile or a blank name."""
        name = name.strip()
        if not name:
            return False
        with self._lock:
            return self._update_profile(device_id, profile_id, "name", name)

    def set_profile_icon(self, device_id: str, profile_id: int, icon: str, color: str = "") -> bool:
        """Set a profile's icon ref ("" = the Decker icon) and SVG fill colour. False for an unknown profile."""
        with self._lock:
            if not self._update_profile(device_id, profile_id, "icon", icon):
                return False
            return self._update_profile(device_id, profile_id, "icon_color", color)

    def delete_profile(self, device_id: str, profile_id: int) -> list[str]:
        """Delete a profile with its pages; returns the deleted page ids ([] when unknown or the last profile)."""
        with self._lock:
            profiles = self._profiles(device_id)
            if len(profiles) <= 1 or all(p.profile_id != profile_id for p in profiles):
                return []
            page_ids = [p.page_id for p in self._pages(device_id, profile_id)]
            self._db.execute("DELETE FROM pages WHERE device_id = ? AND profile_id = ?", (device_id, profile_id))
            self._db.execute(
                "DELETE FROM profile_defaults WHERE device_id = ? AND profile_id = ?", (device_id, profile_id)
            )
            self._db.execute("DELETE FROM profiles WHERE device_id = ? AND profile_id = ?", (device_id, profile_id))
            self._db.execute(
                "DELETE FROM active_profiles WHERE device_id = ? AND profile_id = ?", (device_id, profile_id)
            )
            self._db.commit()
            return page_ids

    def clone_profile(self, device_id: str, profile_id: int) -> tuple[ProfileInfo, dict[str, str]] | None:
        """Copy a profile and its pages; returns the new profile and {old page id: new page id}, or None."""
        with self._lock:
            profiles = self._profiles(device_id)
            source = next((p for p in profiles if p.profile_id == profile_id), None)
            if source is None:
                return None
            clone = ProfileInfo(
                _lowest_unused(p.profile_id for p in profiles),
                _unique_name(f"{source.name} copy", {p.name for p in profiles}),
                source.icon,
                source.icon_color,
            )
            self._insert_profile(device_id, clone, position=self._next_profile_position(device_id))
            mapping: dict[str, str] = {}
            for position, page in enumerate(self._pages(device_id, profile_id)):
                mapping[page.page_id] = uuid.uuid4().hex
                self._insert(device_id, PageInfo(mapping[page.page_id], page.name), position, clone.profile_id)
            default = self._default_row(device_id, profile_id)
            if default in mapping:
                self._set_default(device_id, clone.profile_id, mapping[default])
            return clone, mapping

    def active_profile_id(self, device_id: str) -> int:
        """Return the profile shown on the device: the stored choice, else the first profile."""
        with self._lock:
            profiles = self._profiles(device_id)
            row = self._db.execute(
                "SELECT profile_id FROM active_profiles WHERE device_id = ?", (device_id,)
            ).fetchone()
            ids = {p.profile_id for p in profiles}
            return row[0] if row is not None and row[0] in ids else profiles[0].profile_id

    def set_active_profile(self, device_id: str, profile_id: int) -> bool:
        """Remember profile_id as the device's active profile. Returns False for an unknown profile."""
        with self._lock:
            if self.get_profile(device_id, profile_id) is None:
                return False
            self._db.execute(
                """
                INSERT INTO active_profiles (device_id, profile_id) VALUES (?, ?)
                ON CONFLICT(device_id) DO UPDATE SET profile_id = excluded.profile_id
                """,
                (device_id, profile_id),
            )
            self._db.commit()
            return True

    def profile_of_page(self, device_id: str, page_id: str) -> int | None:
        """Return the id of the profile that owns the page, or None for an unknown page."""
        with self._lock:
            self._profiles(device_id)
            row = self._db.execute(
                "SELECT profile_id FROM pages WHERE device_id = ? AND page_id = ?", (device_id, page_id)
            ).fetchone()
        return row[0] if row is not None else None

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    def list_pages(self, device_id: str, profile_id: int | None = None) -> list[PageInfo]:
        """Return a profile's pages in display order (the active profile's when profile_id is None)."""
        with self._lock:
            profiles = self._profiles(device_id)
            wanted = self.active_profile_id(device_id) if profile_id is None else profile_id
            if all(p.profile_id != wanted for p in profiles):
                return []
            return self._pages(device_id, wanted)

    def get(self, device_id: str, page_id: str) -> PageInfo | None:
        """Return the page if it belongs to device_id (in any of its profiles), else None."""
        with self._lock:
            self._profiles(device_id)
            row = self._db.execute(
                "SELECT page_id, name FROM pages WHERE device_id = ? AND page_id = ?", (device_id, page_id)
            ).fetchone()
        return PageInfo(page_id=row[0], name=row[1]) if row is not None else None

    def add_page(self, device_id: str, name: str | None = None, profile_id: int | None = None) -> PageInfo:
        """Append a page, named "Page N" (first unused N) unless a non-blank name is given."""
        with self._lock:
            target = self.active_profile_id(device_id) if profile_id is None else profile_id
            if self.get_profile(device_id, target) is None:
                raise ValueError(f"device {device_id!r} has no profile {target}")
            pages = self.list_pages(device_id, target)
            page = PageInfo(page_id=uuid.uuid4().hex, name=(name or "").strip() or _next_page_name(pages))
            self._insert(device_id, page, self._next_position(device_id), target)
            return page

    def rename_page(self, device_id: str, page_id: str, name: str) -> bool:
        """Rename a page. Returns False for an unknown page or a blank name."""
        name = name.strip()
        if not name:
            return False
        with self._lock:
            cursor = self._db.execute(
                "UPDATE pages SET name = ? WHERE device_id = ? AND page_id = ?", (name, device_id, page_id)
            )
            self._db.commit()
        return cursor.rowcount > 0

    def move_page(self, device_id: str, page_id: str, new_index: int) -> bool:
        """Move a page to new_index (clamped) within its profile. Returns False for an unknown page."""
        with self._lock:
            profile_id = self.profile_of_page(device_id, page_id)
            if profile_id is None:
                return False
            rows = self._db.execute(
                "SELECT page_id, position FROM pages WHERE device_id = ? AND profile_id = ? ORDER BY position",
                (device_id, profile_id),
            ).fetchall()
            ids = [row[0] for row in rows]
            ids.remove(page_id)
            ids.insert(max(0, min(new_index, len(ids))), page_id)
            # Reuse the profile's own position slots so other profiles' rows keep their relative order.
            slots = [row[1] for row in rows]
            self._db.executemany(
                "UPDATE pages SET position = ? WHERE device_id = ? AND page_id = ?",
                [(slot, device_id, pid) for slot, pid in zip(slots, ids)],
            )
            self._db.commit()
            return True

    def delete_page(self, device_id: str, page_id: str) -> bool:
        """Delete a page. Refuses a profile's last page; a deleted default page passes to the profile's first page."""
        with self._lock:
            profile_id = self.profile_of_page(device_id, page_id)
            if profile_id is None or len(self._pages(device_id, profile_id)) <= 1:
                return False
            was_default = self._default_row(device_id, profile_id) == page_id
            self._db.execute("DELETE FROM pages WHERE device_id = ? AND page_id = ?", (device_id, page_id))
            if was_default:
                self._db.execute(
                    "DELETE FROM profile_defaults WHERE device_id = ? AND profile_id = ?", (device_id, profile_id)
                )
            self._db.commit()
            return True

    def default_page_id(self, device_id: str, profile_id: int | None = None) -> str:
        """Return the page a profile starts on: the chosen default, else its first page."""
        with self._lock:
            wanted = self.active_profile_id(device_id) if profile_id is None else profile_id
            pages = self.list_pages(device_id, wanted)
            row = self._default_row(device_id, wanted)
            return row if row in {page.page_id for page in pages} else pages[0].page_id

    def set_default_page(self, device_id: str, page_id: str) -> bool:
        """Make page_id the default page of its profile. Returns False for an unknown page."""
        with self._lock:
            profile_id = self.profile_of_page(device_id, page_id)
            if profile_id is None:
                return False
            self._set_default(device_id, profile_id, page_id)
            return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _profiles(self, device_id: str) -> list[ProfileInfo]:
        rows = self._db.execute(
            "SELECT profile_id, name, icon, icon_color FROM profiles WHERE device_id = ? ORDER BY position",
            (device_id,),
        ).fetchall()
        if rows:
            return [ProfileInfo(row[0], row[1], row[2], row[3]) for row in rows]
        profile = ProfileInfo(0, DEFAULT_PROFILE_NAME, "")
        self._insert_profile(device_id, profile, position=0)
        if self._db.execute("SELECT 1 FROM pages WHERE device_id = ? LIMIT 1", (device_id,)).fetchone() is None:
            self._insert(device_id, PageInfo(initial_page_id(device_id), f"{_PAGE_NAME_PREFIX}1"), 0, 0)
        return [profile]

    def _pages(self, device_id: str, profile_id: int) -> list[PageInfo]:
        rows = self._db.execute(
            "SELECT page_id, name FROM pages WHERE device_id = ? AND profile_id = ? ORDER BY position",
            (device_id, profile_id),
        ).fetchall()
        return [PageInfo(page_id=row[0], name=row[1]) for row in rows]

    def _default_row(self, device_id: str, profile_id: int) -> str | None:
        row = self._db.execute(
            "SELECT page_id FROM profile_defaults WHERE device_id = ? AND profile_id = ?", (device_id, profile_id)
        ).fetchone()
        return row[0] if row is not None else None

    def _set_default(self, device_id: str, profile_id: int, page_id: str) -> None:
        self._db.execute(
            """
            INSERT INTO profile_defaults (device_id, profile_id, page_id) VALUES (?, ?, ?)
            ON CONFLICT(device_id, profile_id) DO UPDATE SET page_id = excluded.page_id
            """,
            (device_id, profile_id, page_id),
        )
        self._db.commit()

    def _update_profile(self, device_id: str, profile_id: int, column: str, value: str) -> bool:
        self._profiles(device_id)
        cursor = self._db.execute(
            f"UPDATE profiles SET {column} = ? WHERE device_id = ? AND profile_id = ?", (value, device_id, profile_id)
        )
        self._db.commit()
        return cursor.rowcount > 0

    def _insert_profile(self, device_id: str, profile: ProfileInfo, position: int) -> None:
        self._db.execute(
            "INSERT INTO profiles (device_id, profile_id, name, icon, icon_color, position) VALUES (?, ?, ?, ?, ?, ?)",
            (device_id, profile.profile_id, profile.name, profile.icon, profile.icon_color, position),
        )
        self._db.commit()

    def _insert(self, device_id: str, page: PageInfo, position: int, profile_id: int) -> None:
        self._db.execute(
            "INSERT INTO pages (page_id, device_id, name, position, profile_id) VALUES (?, ?, ?, ?, ?)",
            (page.page_id, device_id, page.name, position, profile_id),
        )
        self._db.commit()

    def _next_position(self, device_id: str) -> int:
        row = self._db.execute("SELECT MAX(position) FROM pages WHERE device_id = ?", (device_id,)).fetchone()
        return (row[0] if row and row[0] is not None else -1) + 1

    def _next_profile_position(self, device_id: str) -> int:
        row = self._db.execute("SELECT MAX(position) FROM profiles WHERE device_id = ?", (device_id,)).fetchone()
        return (row[0] if row and row[0] is not None else -1) + 1

    def _migrate_to_profiles(self) -> None:
        """Give every device that has pages but no profile the profile 0 "Default" holding them all."""
        devices = self._db.execute(
            "SELECT DISTINCT device_id FROM pages WHERE device_id NOT IN (SELECT device_id FROM profiles)"
        ).fetchall()
        for (device_id,) in devices:
            self._db.execute(
                "INSERT INTO profiles (device_id, profile_id, name, icon, position) VALUES (?, 0, ?, '', 0)",
                (device_id, DEFAULT_PROFILE_NAME),
            )
            self._db.execute(
                "INSERT OR IGNORE INTO profile_defaults (device_id, profile_id, page_id) "
                "SELECT device_id, 0, page_id FROM page_defaults WHERE device_id = ?",
                (device_id,),
            )


def _lowest_unused(used: Iterable[int]) -> int:
    taken = set(used)
    number = 0
    while number in taken:
        number += 1
    return number


def _unique_name(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    number = 2
    while f"{base} {number}" in taken:
        number += 1
    return f"{base} {number}"


def _next_page_name(pages: list[PageInfo]) -> str:
    names = {page.name for page in pages}
    number = 1
    while f"{_PAGE_NAME_PREFIX}{number}" in names:
        number += 1
    return f"{_PAGE_NAME_PREFIX}{number}"
