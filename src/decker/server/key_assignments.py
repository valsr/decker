"""SQLite-backed store mapping (device_id, page_id, key_index) to an active card assignment."""

import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from decker.common import paths as decker_paths
from decker.common.dbmigrate import rename_column
from decker.common.manager import Manager
from decker.plugin.target import CardTarget
from decker.server.pages import initial_page_id

if TYPE_CHECKING:
    from decker.server.server_context import ServerContext

BLANK_PLUGIN_ID = "com.decker.blank"
BLANK_CARD_ID = "com.decker.blank.blank"
RETIRED_CARDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("com.decker.core", "com.decker.core.runaction"),
        ("com.decker.core", "com.decker.core.switch"),
        (BLANK_PLUGIN_ID, "com.decker.blank.pagename"),
        (BLANK_PLUGIN_ID, "com.decker.blank.pagecount"),
    }
)
_TABLE = "key_assignments"
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    device_id   TEXT NOT NULL,
    page_id     TEXT NOT NULL,
    key_index   INTEGER NOT NULL,
    plugin_uuid TEXT NOT NULL,
    card_uuid TEXT NOT NULL,
    context_id  TEXT NOT NULL,
    settings    TEXT NOT NULL DEFAULT '{{}}',
    appearance  TEXT NOT NULL DEFAULT '{{}}',
    bindings    TEXT NOT NULL DEFAULT '{{}}',
    PRIMARY KEY (device_id, page_id, key_index)
)
"""
_META_SCHEMA = "CREATE TABLE IF NOT EXISTS key_assignment_meta (name TEXT PRIMARY KEY)"
_LEGACY_COLUMNS = "key_index, plugin_uuid, card_uuid, context_id, settings"
_COLUMNS = f"{_LEGACY_COLUMNS}, appearance, bindings"
_COUNTER_PLUGIN = "com.decker.counter"
_COUNTER_APPEARANCE_MIGRATION = "counter_appearance"
_COUNTER_APPEARANCE_KEYS = (
    "background",
    "title",
    "title_position",
    "title_font",
    "title_size",
    "title_color",
    "title_outline",
    "title_outline_width",
)


@dataclass(frozen=True)
class KeyAssignment:
    """One active key slot: which card occupies it and its current settings."""

    target: CardTarget
    settings: dict[str, Any]
    appearance: dict[str, Any] = field(default_factory=dict)
    bindings: dict[str, Any] = field(default_factory=dict)


class KeyAssignmentStore(Manager):
    """Persists and retrieves key→card mappings per page from decker.db."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._lock = threading.Lock()
        self._db_path = Path(db_path) if db_path is not None else decker_paths.db()
        self._conn: sqlite3.Connection | None = None

    def create(self, context: "ServerContext") -> None:
        """Open the SQLite connection, migrate pre-page data, and initialise the schema."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        rename_column(self._db, _TABLE, "action_uuid", "card_uuid")  # before the page migration reads the columns
        self._migrate_to_pages()
        self._db.execute(_SCHEMA)
        self._db.execute(_META_SCHEMA)
        self._add_appearance_column()
        self._add_bindings_column()
        self._migrate_counter_appearance()
        self._db.commit()

    def destroy(self) -> None:
        """Close the database connection once any statement running on another thread has finished."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def _db(self) -> sqlite3.Connection:
        assert self._conn is not None, "KeyAssignmentStore.create() must be called before use"
        return self._conn

    def assign(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        device_id: str,
        page_id: str,
        key_index: int,
        target: CardTarget,
        settings: dict[str, Any],
        bindings: dict[str, Any] | None = None,
        appearance: dict[str, Any] | None = None,
    ) -> None:
        """Persist an assignment, replacing any previous one for this slot."""
        with self._lock:
            self._db.execute(
                f"""
                INSERT INTO {_TABLE}
                    (device_id, page_id, key_index, plugin_uuid, card_uuid, context_id, settings, appearance, bindings)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id, page_id, key_index) DO UPDATE SET
                    plugin_uuid = excluded.plugin_uuid,
                    card_uuid = excluded.card_uuid,
                    context_id  = excluded.context_id,
                    settings    = excluded.settings,
                    appearance  = excluded.appearance,
                    bindings    = excluded.bindings
                """,
                (
                    device_id,
                    page_id,
                    key_index,
                    target.plugin_uuid,
                    target.card_uuid,
                    target.context_id,
                    json.dumps(settings),
                    json.dumps(appearance or {}),
                    json.dumps(bindings or {}),
                ),
            )
            self._db.commit()

    def unassign(self, device_id: str, page_id: str, key_index: int) -> None:
        """Remove the assignment for this slot. No-op if unassigned."""
        with self._lock:
            self._db.execute(
                f"DELETE FROM {_TABLE} WHERE device_id = ? AND page_id = ? AND key_index = ?",
                (device_id, page_id, key_index),
            )
            self._db.commit()

    def get(self, device_id: str, page_id: str, key_index: int) -> KeyAssignment | None:
        """Return the assignment for this slot, or None if unassigned."""
        with self._lock:
            row = self._db.execute(
                f"SELECT {_COLUMNS} FROM {_TABLE} WHERE device_id = ? AND page_id = ? AND key_index = ?",
                (device_id, page_id, key_index),
            ).fetchone()
        if row is None or _is_retired(device_id, page_id, row):
            return None
        return _to_assignment(device_id, row)[1]

    def update_settings(self, device_id: str, page_id: str, key_index: int, settings: dict[str, Any]) -> bool:
        """Overwrite settings for an existing assignment. Returns False if slot is unassigned."""
        with self._lock:
            cursor = self._db.execute(
                f"UPDATE {_TABLE} SET settings = ? WHERE device_id = ? AND page_id = ? AND key_index = ?",
                (json.dumps(settings), device_id, page_id, key_index),
            )
            self._db.commit()
        return cursor.rowcount > 0

    def update_appearance(self, device_id: str, page_id: str, key_index: int, appearance: dict[str, Any]) -> bool:
        """Overwrite the key's appearance. Returns False if slot is unassigned."""
        with self._lock:
            cursor = self._db.execute(
                f"UPDATE {_TABLE} SET appearance = ? WHERE device_id = ? AND page_id = ? AND key_index = ?",
                (json.dumps(appearance), device_id, page_id, key_index),
            )
            self._db.commit()
        return cursor.rowcount > 0

    def update_bindings(self, device_id: str, page_id: str, key_index: int, bindings: dict[str, Any]) -> bool:
        """Overwrite the key's gesture bindings. Returns False if slot is unassigned."""
        with self._lock:
            cursor = self._db.execute(
                f"UPDATE {_TABLE} SET bindings = ? WHERE device_id = ? AND page_id = ? AND key_index = ?",
                (json.dumps(bindings), device_id, page_id, key_index),
            )
            self._db.commit()
        return cursor.rowcount > 0

    def all_for_page(self, device_id: str, page_id: str) -> list[tuple[int, KeyAssignment]]:
        """Return all assignments on one page as (key_index, assignment) pairs, in key order."""
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLUMNS} FROM {_TABLE} WHERE device_id = ? AND page_id = ? ORDER BY key_index",
                (device_id, page_id),
            ).fetchall()
        return [_to_assignment(device_id, row) for row in rows if not _is_retired(device_id, page_id, row)]

    def assignments_for_plugin(self, plugin_id: str) -> list[tuple[str, str, int, KeyAssignment]]:
        """Return every assignment of a plugin's cards as (device_id, page_id, key_index, assignment)."""
        with self._lock:
            rows = self._db.execute(
                f"SELECT device_id, page_id, {_COLUMNS} FROM {_TABLE} WHERE plugin_uuid = ? "
                "ORDER BY device_id, page_id, key_index",
                (plugin_id,),
            ).fetchall()
        return [
            (row[0], row[1], *_to_assignment(row[0], row[2:]))
            for row in rows
            if not _is_retired(row[0], row[1], row[2:])
        ]

    def plugin_ids(self) -> set[str]:
        """Return the ids of every plugin referenced by at least one assignment."""
        with self._lock:
            rows = self._db.execute(f"SELECT DISTINCT plugin_uuid FROM {_TABLE}").fetchall()
        return {row[0] for row in rows}

    def delete_page(self, device_id: str, page_id: str) -> None:
        """Remove every assignment on a page."""
        with self._lock:
            self._db.execute(f"DELETE FROM {_TABLE} WHERE device_id = ? AND page_id = ?", (device_id, page_id))
            self._db.commit()

    def move(self, device_id: str, page_id: str, from_index: int, to_index: int) -> bool:
        """Re-key an assignment to another slot of its page, replacing what was there (False if nothing moved)."""
        if from_index == to_index:
            return False
        with self._lock, self._db:
            exists = self._db.execute(
                f"SELECT 1 FROM {_TABLE} WHERE device_id = ? AND page_id = ? AND key_index = ?",
                (device_id, page_id, from_index),
            ).fetchone()
            if exists is None:
                return False
            self._db.execute(
                f"DELETE FROM {_TABLE} WHERE device_id = ? AND page_id = ? AND key_index = ?",
                (device_id, page_id, to_index),
            )
            self._db.execute(
                f"UPDATE {_TABLE} SET key_index = ? WHERE device_id = ? AND page_id = ? AND key_index = ?",
                (to_index, device_id, page_id, from_index),
            )
        return True

    def copy_key(
        self, device_id: str, page_id: str, from_index: int, to_index: int, context_id: str
    ) -> KeyAssignment | None:
        """Copy an assignment to another slot of its page under a new context id, replacing what was there."""
        if from_index == to_index:
            return None
        with self._lock, self._db:
            row = self._db.execute(
                f"SELECT {_COLUMNS} FROM {_TABLE} WHERE device_id = ? AND page_id = ? AND key_index = ?",
                (device_id, page_id, from_index),
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                f"DELETE FROM {_TABLE} WHERE device_id = ? AND page_id = ? AND key_index = ?",
                (device_id, page_id, to_index),
            )
            self._db.execute(
                f"INSERT INTO {_TABLE} (device_id, page_id, {_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (device_id, page_id, to_index, row[1], row[2], context_id, row[4], row[5], row[6]),
            )
        return self.get(device_id, page_id, to_index)

    def copy_page(
        self, device_id: str, source_page_id: str, target_page_id: str, page_map: Mapping[str, str] | None = None
    ) -> list[tuple[int, KeyAssignment]]:
        """Copy every assignment of a page onto another page, giving each copy a new context id.

        Settings values and binding args that are exactly an id in ``page_map`` are rewritten to the page's copy.
        """
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLUMNS} FROM {_TABLE} WHERE device_id = ? AND page_id = ? ORDER BY key_index",
                (device_id, source_page_id),
            ).fetchall()
            copies = [_page_copy(row, page_map or {}) for row in rows]
            self._db.executemany(
                f"INSERT INTO {_TABLE} (device_id, page_id, {_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(device_id, target_page_id, *copy) for copy in copies],
            )
            self._db.commit()
        return [_to_assignment(device_id, copy) for copy in copies]

    def _migrate_to_pages(self) -> None:
        columns = [row[1] for row in self._db.execute(f"PRAGMA table_info({_TABLE})").fetchall()]
        if not columns or "page_id" in columns:
            return
        rows = self._db.execute(f"SELECT device_id, {_LEGACY_COLUMNS} FROM {_TABLE}").fetchall()
        with self._db:
            self._db.execute(f"ALTER TABLE {_TABLE} RENAME TO {_TABLE}_legacy")
            self._db.execute(_SCHEMA)
            self._db.executemany(
                f"INSERT INTO {_TABLE} (device_id, page_id, {_LEGACY_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(row[0], initial_page_id(row[0]), *row[1:]) for row in rows],
            )
            self._db.execute(f"DROP TABLE {_TABLE}_legacy")

    def _add_appearance_column(self) -> None:
        columns = [row[1] for row in self._db.execute(f"PRAGMA table_info({_TABLE})").fetchall()]
        if "appearance" not in columns:
            self._db.execute(f"ALTER TABLE {_TABLE} ADD COLUMN appearance TEXT NOT NULL DEFAULT '{{}}'")

    def _add_bindings_column(self) -> None:
        columns = [row[1] for row in self._db.execute(f"PRAGMA table_info({_TABLE})").fetchall()]
        if "bindings" not in columns:
            self._db.execute(f"ALTER TABLE {_TABLE} ADD COLUMN bindings TEXT NOT NULL DEFAULT '{{}}'")

    def _migrate_counter_appearance(self) -> None:
        done = self._db.execute(
            "SELECT 1 FROM key_assignment_meta WHERE name = ?", (_COUNTER_APPEARANCE_MIGRATION,)
        ).fetchone()
        if done is not None:
            return
        rows = self._db.execute(
            f"SELECT device_id, page_id, key_index, settings, appearance FROM {_TABLE} WHERE plugin_uuid = ?",
            (_COUNTER_PLUGIN,),
        ).fetchall()
        for device_id, page_id, key_index, settings_json, appearance_json in rows:
            settings = json.loads(settings_json)
            moved = {k: settings.pop(k) for k in _COUNTER_APPEARANCE_KEYS if k in settings}
            if not moved.get("title_outline"):
                moved.pop("title_outline", None)
            self._db.execute(
                f"UPDATE {_TABLE} SET settings = ?, appearance = ? "
                "WHERE device_id = ? AND page_id = ? AND key_index = ?",
                (
                    json.dumps(settings),
                    json.dumps({**json.loads(appearance_json), **moved}),
                    device_id,
                    page_id,
                    key_index,
                ),
            )
        self._db.execute("INSERT INTO key_assignment_meta (name) VALUES (?)", (_COUNTER_APPEARANCE_MIGRATION,))


def _remap_pages(settings_json: str, page_map: Mapping[str, str]) -> str:
    settings = json.loads(settings_json)
    if not page_map or not isinstance(settings, dict):
        return settings_json
    return json.dumps({k: page_map.get(v, v) if isinstance(v, str) else v for k, v in settings.items()})


def _remap_binding_args(bindings_json: str, page_map: Mapping[str, str]) -> str:
    bindings = json.loads(bindings_json)
    if not page_map or not isinstance(bindings, dict):
        return bindings_json
    for binding in bindings.values():
        if isinstance(binding, dict) and isinstance(binding.get("args"), dict):
            binding["args"] = {k: page_map.get(v, v) if isinstance(v, str) else v for k, v in binding["args"].items()}
    return json.dumps(bindings)


def _page_copy(row: tuple[Any, ...], page_map: Mapping[str, str]) -> tuple[Any, ...]:
    return (
        *row[:3],
        uuid.uuid4().hex,
        _remap_pages(row[4], page_map),
        row[5],
        _remap_binding_args(row[6], page_map),
    )


def _is_retired(device_id: str, page_id: str, row: tuple[Any, ...]) -> bool:
    key_index, plugin_uuid, card_uuid = row[0], row[1], row[2]
    if (plugin_uuid, card_uuid) not in RETIRED_CARDS:
        return False
    logging.info(
        "Key %d of page %s on device %s names the retired card %s; it loads as an empty key",
        key_index,
        page_id,
        device_id,
        card_uuid,
    )
    return True


def _to_assignment(device_id: str, row: tuple[Any, ...]) -> tuple[int, KeyAssignment]:
    key_index, plugin_uuid, card_uuid, context_id, settings_json, appearance_json, bindings_json = row
    target = CardTarget(
        device_id=device_id,
        key_index=key_index,
        plugin_uuid=plugin_uuid,
        card_uuid=card_uuid,
        context_id=context_id,
    )
    return key_index, KeyAssignment(
        target=target,
        settings=json.loads(settings_json),
        appearance=json.loads(appearance_json),
        bindings=json.loads(bindings_json),
    )
