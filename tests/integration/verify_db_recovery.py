"""A separate reader must see the accepted ID after database recovery."""

import os
import time

from services.common import config
from services.common.db import connect

message_id = os.environ["AOW_TEST_MESSAGE_ID"].strip()
deadline = time.monotonic() + 120
with connect(config.reader_dsn(), autocommit=True) as observer:
    while time.monotonic() < deadline:
        row = observer.execute(
            "SELECT count(*) AS total FROM ingest_log WHERE message_id = %s",
            (message_id,),
        ).fetchone()
        if row["total"] == 1:
            break
        time.sleep(2)
    else:
        raise AssertionError(f"accepted {message_id} did not commit after DB recovery")
print(f"PASS: separate reader sees committed {message_id}")
