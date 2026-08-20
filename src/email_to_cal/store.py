"""Durable state: which flagged messages failed, and what has been written to the calendar.

Success needs no memory. The flag comes off the message, so a processed email is simply
one the mailbox no longer offers - and re-flagging it deliberately runs it again.
Failures are remembered so a message that cannot be processed is retried a few times and
then left alone rather than reprocessed on every pass, forever.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

_SCHEMA = """
CREATE TABLE IF NOT EXISTS failures (
    message_id   TEXT PRIMARY KEY,
    subject      TEXT NOT NULL,
    attempts     INTEGER NOT NULL,
    detail       TEXT NOT NULL,
    retry_at     REAL,
    updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    uid          TEXT PRIMARY KEY,
    message_id   TEXT NOT NULL,
    summary      TEXT NOT NULL,
    starts_at    TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS heartbeat (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    beat_at      REAL NOT NULL
);
"""


@dataclass(frozen=True)
class Failure:
    """One message that could not be turned into events."""

    message_id: str
    subject: str
    attempts: int
    detail: str
    # When the next attempt is due, or None once the service has given up on it.
    retry_at: float | None

    @property
    def given_up(self) -> bool:
        return self.retry_at is None


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

    # -- failures -------------------------------------------------------------------

    def failure(self, message_id: str) -> Failure | None:
        row = self._conn.execute(
            "SELECT message_id, subject, attempts, detail, retry_at FROM failures "
            "WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        return Failure(row[0], row[1], row[2], row[3], row[4]) if row else None

    def record_failure(
        self, message_id: str, subject: str, attempts: int, detail: str, retry_at: float | None
    ) -> None:
        self._conn.execute(
            "INSERT INTO failures (message_id, subject, attempts, detail, retry_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(message_id) DO UPDATE SET subject = excluded.subject, "
            "attempts = excluded.attempts, detail = excluded.detail, "
            "retry_at = excluded.retry_at, updated_at = excluded.updated_at",
            (message_id, subject[:500], attempts, detail[:2000], retry_at, time.time()),
        )

    def clear_failure(self, message_id: str) -> None:
        self._conn.execute("DELETE FROM failures WHERE message_id = ?", (message_id,))

    def list_failures(self) -> list[Failure]:
        rows = self._conn.execute(
            "SELECT message_id, subject, attempts, detail, retry_at FROM failures "
            "ORDER BY updated_at DESC"
        ).fetchall()
        return [Failure(r[0], r[1], r[2], r[3], r[4]) for r in rows]

    # -- written events -------------------------------------------------------------

    def record_event(self, uid: str, message_id: str, summary: str, starts_at: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO events (uid, message_id, summary, starts_at, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (uid, message_id, summary, starts_at, time.time()),
        )

    def recent_events(self, limit: int = 20) -> list[tuple[str, str, float]]:
        rows = self._conn.execute(
            "SELECT summary, starts_at, created_at FROM events ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

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
