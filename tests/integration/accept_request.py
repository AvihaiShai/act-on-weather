"""Accept one durable API write while a dependency is stopped behind it.

Run once per M11 failure mode -- consumer down, broker down, database down --
so each drill carries its own `message_id`. `AOW_TEST_LABEL` keeps the three
activities distinct, which makes the recommendation rows as traceable as the
ingest_log rows.

The assertion here is only about acceptance: the record reached the fsynced
outbox, which is where the no-data-loss guarantee starts. Whether it was
committed is `verify_db_recovery.py`'s question, asked from another connection.
"""

import json
import os
import urllib.request

from services.common import config
from services.common.outbox import Outbox

body = json.dumps(
    {
        "city": "rome",
        "forecast_date": os.environ["AOW_TEST_DATE"].strip(),
        "activity": os.environ["AOW_TEST_LABEL"].strip(),
    }
).encode()
request = urllib.request.Request(
    "http://127.0.0.1:8000/recommendations",
    data=body,
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=10) as response:
    assert response.status == 202
    message_id = json.load(response)["message_id"]
assert Outbox(config.OUTBOX_PATH).status_of(message_id) is not None
print(message_id)
