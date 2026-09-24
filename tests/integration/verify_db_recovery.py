"""Every accepted ID must be in ingest_log exactly once, seen from another session.

`AOW_TEST_MESSAGE_IDS` is a comma-separated list, so one call covers all three
M11 drills and the reconciliation replay. Two properties are checked, and the
difference between them matters:

  * missing is retried until the deadline -- redelivery and reconnection are
    allowed to take time, and a slow recovery is not a failure
  * duplicated fails immediately -- `ingest_log`'s primary key means it can
    never heal, so waiting would only delay the same answer

The connection is the API container's read-only role, opened here and closed
here. That is deliberate: a write that was acknowledged but never committed
cannot hide inside the consumer's own open transaction if a different session
is the one doing the looking. That is exactly how F1 stayed invisible.
"""

import os
import time

from services.common import config
from services.common.db import connect

ids = [value.strip() for value in os.environ["AOW_TEST_MESSAGE_IDS"].split(",") if value.strip()]
assert ids, "AOW_TEST_MESSAGE_IDS named no message to verify"

deadline = time.monotonic() + 180
with connect(config.reader_dsn(), autocommit=True) as observer:
    while True:
        rows = observer.execute(
            "SELECT message_id, count(*) AS total FROM ingest_log"
            " WHERE message_id = ANY(%s) GROUP BY message_id",
            (ids,),
        ).fetchall()
        stored = {row["message_id"]: row["total"] for row in rows}
        duplicated = sorted(mid for mid, total in stored.items() if total != 1)
        if duplicated:
            raise AssertionError(f"stored more than once: {', '.join(duplicated)}")
        missing = [mid for mid in ids if mid not in stored]
        if not missing:
            break
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"{len(missing)} of {len(ids)} accepted ID(s) absent from ingest_log "
                f"after recovery: {', '.join(missing)}"
            )
        time.sleep(2)
print(f"PASS: a separate reader sees all {len(ids)} accepted ID(s), exactly once each")
