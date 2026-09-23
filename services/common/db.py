"""Postgres connections.

One helper, used by every service that touches the database. `connect` retries
with backoff rather than exiting, because a database outage must look like a
pause to the consumer, not like a crash loop -- that is drill 2 of M11.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager, suppress

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)


def connect(dsn: str, *, autocommit: bool = False, max_wait: float = 30.0) -> psycopg.Connection:
    delay = 1.0
    while True:
        try:
            conn = psycopg.connect(dsn, autocommit=autocommit, row_factory=dict_row)
            conn.execute("SET application_name = 'aow'")
            return conn
        except psycopg.OperationalError as exc:
            log.warning("postgres unreachable (%s); retrying in %.0fs", exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, max_wait)


@contextmanager
def cursor(conn: psycopg.Connection):
    with conn.cursor() as cur:
        yield cur


class Pool:
    """A single lazily-reconnecting connection.

    Not a real pool: these are single-threaded processes, and one connection
    that knows how to come back is what they actually need.
    """

    def __init__(self, dsn: str, *, autocommit: bool = False):
        self.dsn = dsn
        self.autocommit = autocommit
        self._conn: psycopg.Connection | None = None

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = connect(self.dsn, autocommit=self.autocommit)
        return self._conn

    def drop(self) -> None:
        if self._conn is not None:
            # The socket is usually already dead when we get here -- that is the
            # whole reason for dropping it -- so closing it is best effort.
            with suppress(Exception):
                self._conn.close()
            self._conn = None
