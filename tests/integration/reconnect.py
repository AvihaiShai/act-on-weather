"""A reconnected consumer connection must commit a write visible to a second session.

Run inside the consumer container after the queue/database smoke check. The
isolated CI Compose project and its volumes are removed when the job exits.
"""

from uuid import uuid4

from psycopg.pq import TransactionStatus

from services.common import config
from services.common.db import Pool, connect

pool = Pool(config.writer_dsn())
try:
    for generation in ("initial", "reconnected"):
        conn = pool.conn
        assert conn.pgconn.transaction_status == TransactionStatus.IDLE, generation
        message_id = str(uuid4())
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ingest_log (message_id, routing_key, source) VALUES (%s, %s, %s)",
                (message_id, config.RK_PATCH, "ci-reconnect"),
            )
        with connect(config.writer_dsn(), autocommit=True) as observer:
            found = observer.execute(
                "SELECT 1 FROM ingest_log WHERE message_id = %s", (message_id,)
            ).fetchone()
        assert found, f"{generation} transaction was not committed"
        pool.drop()
finally:
    pool.drop()

print("PASS: initial and reconnected writes committed and visible to a separate session")
