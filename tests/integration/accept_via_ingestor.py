"""Accept one record into the *ingestor's* outbox, the way the ingestor does.

The API drills prove the API's outbox. They do not prove the ingestor's: it is
a different volume, drained by a different loop (`ingestor.main.drain`) in a
different process. This fixture closes that gap without inventing a production
surface for it -- there is no "accept one record" endpoint on the ingestor, and
adding one for a test would be worse than this.

So it runs inside the ingestor container and calls the ingestor's own
`envelopes_from` and `Outbox.accept_many` against `config.OUTBOX_PATH`, which
is the real outbox on the real volume. Nothing about publishing is simulated:
the ingestor's own running `drain()` picks the row up, under publisher
confirms, exactly as it does for a snapshot record.

`AOW_TEST_LABEL` goes into the natural key, so each drill mints its own
deterministic `message_id` and the drills cannot alias each other.
"""

import os

from services.common import config
from services.common.envelope import now_utc
from services.common.outbox import Outbox
from services.ingestor.main import envelopes_from

label = os.environ["AOW_TEST_LABEL"].strip()
payload = {
    "id": f"ci-drill:{label.replace(' ', '-')}",
    "city_id": "rome",
    "name": f"CI outage drill ({label})",
    "category": "test",
    "source": "ci-drill",
    "as_of": now_utc().isoformat(),
}

box = Outbox(config.OUTBOX_PATH)
try:
    envelopes = list(envelopes_from(config.RK_PLACE, [payload], "ci-drill"))
    message_id = box.accept_many(envelopes)[0]
    # Acceptance is the claim being made here, so assert the fsynced row exists
    # before anyone downstream is asked about it.
    assert box.status_of(message_id) is not None
finally:
    box.close()
print(message_id)
