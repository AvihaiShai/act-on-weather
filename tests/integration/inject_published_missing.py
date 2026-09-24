"""Create the old confirmed-but-absent state atomically in disposable CI data."""

import os

from services.common import config
from services.common.envelope import Envelope
from services.common.outbox import Outbox

envelope = Envelope.create(
    config.RK_RECOMMENDATION_REQUEST,
    {
        "city_id": "rome",
        "forecast_date": os.environ["AOW_TEST_DATE"].strip(),
        "activity": "ci_published_missing",
        "activity_label": "ci published missing",
    },
    source="api",
    observed_at="",
    city="rome",
)
box = Outbox(config.OUTBOX_PATH)
# The API publisher sees only committed rows. One transaction prevents it from
# racing between acceptance and the simulated historical published marker.
with box.conn:
    box.conn.execute("BEGIN")
    box.accept(envelope)
    seq = box.conn.execute(
        "SELECT seq FROM outbox WHERE message_id = ?", (envelope.message_id,)
    ).fetchone()["seq"]
    box.mark_published(seq)
print(envelope.message_id)
