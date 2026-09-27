"""F9, phase 2 of 3: the withdrawn record arrives, through the front door.

Run via ``docker compose exec -T ingestor python - < tests/integration/retraction_inject_event.py``,
against a DISPOSABLE project, after ``retraction_before_record.py``.
``scripts/retraction-drill.sh`` drives all three phases.

Accepted into the *ingestor's* outbox rather than published straight to the
broker, and that matters twice over. It is the same front door every collected
record uses, so the message_id is genuinely in the accepted set and the delivery
guarantee under test is the real one. And the rebuild in phase 3 replays from
this volume -- ``compose.yml`` mounts it into the consumer as
``/source-outbox:ro`` -- so a listing accepted anywhere else would simply vanish
at the wipe and every absence assertion afterwards would be vacuous.

The synthetic listing is deliberately not a row from the committed snapshot. The
drill needs a record this install does not hold when the withdrawal arrives, and
using a real one would mean either editing the snapshot or withdrawing something
a reader might legitimately want back. The ``drill:`` prefix says what the row is
to anybody who finds it later, and ``source`` says where it came from.

Leaves a durable row in the outbox for the life of the volume. Disposable project
only.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, "/app")

from services.common import config  # noqa: E402
from services.common.envelope import Envelope  # noqa: E402
from services.common.outbox import Outbox  # noqa: E402

CITY = os.environ["AOW_DRILL_CITY"]
EVENT_ID = os.environ["AOW_DRILL_EVENT_ID"]

now = datetime.now(UTC)
# Far enough ahead that the freshness filter cannot be what hides the row. If it
# were expired, its absence from the default read would prove nothing -- which is
# also why phase 3 checks `include_expired` rather than trusting the default.
starts_at = now + timedelta(days=3)

envelope = Envelope.create(
    config.RK_EVENT,
    {
        "id": EVENT_ID,
        "city_id": CITY,
        "title": "Arrival-order drill listing",
        "category": "music",
        "venue": "drill venue",
        "starts_at": starts_at.isoformat(),
        "ends_at": None,
        "source": "integration drill",
        "source_url": "https://example.invalid/arrival-order-drill",
        "is_sample": False,
        "as_of": now.isoformat(),
        "checked_at": now.isoformat(),
        "valid_until": None,
    },
    source="drill",
    observed_at=now,
    city=CITY,
)
box = Outbox(config.OUTBOX_PATH)
box.accept(envelope)
counts = box.counts()
# Printed alone on stdout so the drill script can capture it directly, the same
# shape `services/tools/inject.py` uses.
print(envelope.message_id)
print(f"accepted; outbox total={counts['total']} pending={counts['pending']}", file=sys.stderr)
