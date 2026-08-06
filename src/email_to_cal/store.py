"""Durable state: where we are in the mailbox, what we have already seen and written."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import TracebackType
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS imap_cursor (
    folder       TEXT PRIMARY KEY,
    uidvalidity  INTEGER NOT NULL,
    last_uid     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_messages (
    message_id   TEXT PRIMARY KEY,
    processed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS emitted_events (
    event_id     TEXT PRIMARY KEY,
    message_id   TEXT NOT NULL,
    calendar_id  TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS calendar_ids (
    name         TEXT PRIMARY KEY,
    calendar_id  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_cache (
    key          TEXT PRIMARY KEY,
    payload      TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS heartbeat (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    beat_at      REAL NOT NULL
);
"""


class Store:
    """Thin SQLite wrapper. Single-threaded by construction; the app has one worker."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # -- IMAP cursor ----------------------------------------------------------------

    def get_cursor(self, folder: str) -> tuple[int, int] | None:
        row = self._conn.execute(
            "SELECT uidvalidity, last_uid FROM imap_cursor WHERE folder = ?", (folder,)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def set_cursor(self, folder: str, uidvalidity: int, last_uid: int) -> None:
        self._conn.execute(
            "INSERT INTO imap_cursor (folder, uidvalidity, last_uid) VALUES (?, ?, ?) "
            "ON CONFLICT(folder) DO UPDATE SET uidvalidity = excluded.uidvalidity, "
            "last_uid = excluded.last_uid",
            (folder, uidvalidity, last_uid),
        )

    # -- message dedup --------------------------------------------------------------

    def has_seen(self, message_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM seen_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def mark_seen(self, message_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_messages (message_id, processed_at) VALUES (?, ?)",
            (message_id, time.time()),
        )

    # -- emitted events -------------------------------------------------------------

    def record_event(self, event_id: str, message_id: str, calendar_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO emitted_events (event_id, message_id, calendar_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (event_id, message_id, calendar_id, time.time()),
        )

    def has_event(self, event_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM emitted_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return row is not None

    # -- calendar id cache ----------------------------------------------------------

    def get_calendar_id(self, name: str) -> str | None:
        row = self._conn.execute(
            "SELECT calendar_id FROM calendar_ids WHERE name = ?", (name.lower(),)
        ).fetchone()
        return row[0] if row else None

    def set_calendar_id(self, name: str, calendar_id: str) -> None:
        self._conn.execute(
            "INSERT INTO calendar_ids (name, calendar_id) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET calendar_id = excluded.calendar_id",
            (name.lower(), calendar_id),
        )

    # -- LLM response cache ---------------------------------------------------------

    def get_cached(self, key: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT payload FROM llm_cache WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        parsed: dict[str, Any] = json.loads(row[0])
        return parsed

    def put_cached(self, key: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO llm_cache (key, payload, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(payload), time.time()),
        )

    # -- liveness -------------------------------------------------------------------

    def beat(self) -> None:
        self._conn.execute(
            "INSERT INTO heartbeat (id, beat_at) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET beat_at = excluded.beat_at",
            (time.time(),),
        )

    def last_beat(self) -> float | None:
        row = self._conn.execute("SELECT beat_at FROM heartbeat WHERE id = 1").fetchone()
        return row[0] if row else None
