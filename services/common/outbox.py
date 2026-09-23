"""The producer-side outbox: where the no-data-loss guarantee begins (M11).

A producer never publishes straight to RabbitMQ. It writes the envelope to a
local SQLite file on a persistent volume, with `synchronous=FULL` so the row is
on disk before the write returns, and only then does a publisher loop drain the
file to the broker under publisher confirms.

That fsynced row is the acceptance point: from it on, the record survives the
broker being down, the consumer being down and the database being down. Before
it, nothing is promised -- a record that was never accepted can be re-fetched
while connected, and the README says so.

SQLite rather than a table in Postgres because the whole point is to survive
Postgres being unreachable, and rather than an in-memory buffer because the
whole point is to survive the process dying.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

from .envelope import Envelope, now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id   TEXT NOT NULL UNIQUE,
  routing_key  TEXT NOT NULL,
  body         BLOB NOT NULL,
  accepted_at  TEXT NOT NULL,
  published_at TEXT,
  attempts     INTEGER NOT NULL DEFAULT 0,
  last_error   TEXT
);
CREATE INDEX IF NOT EXISTS outbox_unpublished ON outbox (seq) WHERE published_at IS NULL;
"""


class Outbox:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because the API accepts on its request
        # threads and publishes on a background one. Every caller holds a lock
        # around this handle; nothing here is concurrent.
        self.conn = sqlite3.connect(
            self.path, isolation_level=None, timeout=30, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        # WAL for a concurrent reader; FULL because a fsync per accepted record
        # is exactly the cost we are choosing to pay for the guarantee.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- producer side ------------------------------------------------------
    def accept(self, envelope: Envelope) -> str:
        """Durably accept one record. Returns its message_id.

        Re-accepting the same message_id is a no-op, so a producer that retries
        after a crash cannot double-publish.
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO outbox (message_id, routing_key, body, accepted_at)"
            " VALUES (?, ?, ?, ?)",
            (envelope.message_id, envelope.routing_key, envelope.to_bytes(), now_utc().isoformat()),
        )
        return envelope.message_id

    def accept_many(self, envelopes: list[Envelope]) -> list[str]:
        with self.conn:
            self.conn.execute("BEGIN")
            ids = [self.accept(e) for e in envelopes]
        return ids

    # -- publisher side -----------------------------------------------------
    def unpublished(self, limit: int = 200) -> Iterator[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT seq, message_id, routing_key, body FROM outbox"
            " WHERE published_at IS NULL ORDER BY seq LIMIT ?",
            (limit,),
        ).fetchall()
        return iter(rows)

    def mark_published(self, seq: int) -> None:
        self.conn.execute(
            "UPDATE outbox SET published_at = ?, last_error = NULL WHERE seq = ?",
            (now_utc().isoformat(), seq),
        )

    def mark_failed(self, seq: int, error: str) -> None:
        self.conn.execute(
            "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE seq = ?",
            (error[:500], seq),
        )

    # -- observability ------------------------------------------------------
    def counts(self) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN published_at IS NULL THEN 1 ELSE 0 END) AS pending"
            " FROM outbox"
        ).fetchone()
        return {"total": row["total"] or 0, "pending": row["pending"] or 0}

    def status_of(self, message_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT message_id, routing_key, accepted_at, published_at, attempts, last_error"
            " FROM outbox WHERE message_id = ?",
            (message_id,),
        ).fetchone()
