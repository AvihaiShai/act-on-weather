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


def connect(
    dsn: str,
    *,
    autocommit: bool = False,
    max_wait: float = 30.0,
    attempts: int = 0,
) -> psycopg.Connection:
    """Connect, retrying with backoff.

    `attempts=0` retries forever, which is what a long-lived service wants: the
    consumer's whole job is to still be there when Postgres comes back. A
    finite `attempts` gives up and re-raises instead, for the callers that must
    answer now rather than wait -- see `Pool.conn_if_up`.
    """
    delay = 1.0
    tried = 0
    while True:
        try:
            conn = psycopg.connect(dsn, autocommit=autocommit, row_factory=dict_row)
            conn.execute("SET application_name = 'aow'")
            # With autocommit=False, SET starts a transaction. The consumer's
            # later `with conn.transaction()` would then be only a savepoint;
            # it could ACK a message whose write was never committed.
            if not autocommit:
                conn.commit()
            return conn
        except psycopg.OperationalError as exc:
            tried += 1
            if attempts and tried >= attempts:
                raise
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

    @property
    def conn_if_up(self) -> psycopg.Connection | None:
        """A connection if one can be had immediately, otherwise `None`.

        `conn` retries forever, and for a background loop that is correct. It
        is wrong inside a request that has to be answered now -- above all a
        write, which the API accepts into its outbox and by design does not
        need the database for at all. A write path that reached `conn` would
        block its worker for as long as the outage lasted, losing a record the
        outbox was ready to make durable.
        """
        if self._conn is not None and not self._conn.closed:
            return self._conn
        try:
            self._conn = connect(self.dsn, autocommit=self.autocommit, attempts=1)
        except psycopg.OperationalError:
            self._conn = None
            return None
        return self._conn

    def drop(self) -> None:
        if self._conn is not None:
            # The socket is usually already dead when we get here -- that is the
            # whole reason for dropping it -- so closing it is best effort.
            with suppress(Exception):
                self._conn.close()
            self._conn = None
