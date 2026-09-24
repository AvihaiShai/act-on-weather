"""Accept one durable API write while Postgres is stopped in CI."""

import json
import os
import urllib.request

from services.common import config
from services.common.outbox import Outbox

body = json.dumps(
    {
        "city": "rome",
        "forecast_date": os.environ["AOW_TEST_DATE"].strip(),
        "activity": "ci database outage",
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
