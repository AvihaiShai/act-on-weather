"""F9, phase 2 of 3: the withdrawn record arrives, and a curated copy with it.

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

TWO envelopes are accepted, and the second is not decoration. Phase 3 step 5
proves that a withdrawal corrected through ``POST /records/.../retract`` is not
reverted by a rebuild, and the only thing that could revert it is a
``record.retract`` the rebuild replays. ``wipe_user_data`` replays what the
INGESTOR accepted and excludes the API's own (``source <> 'api'``), so while
phase 1's API withdrawal was the only one in the install the replay set was
empty: ``apply_retraction(..., replay=True)`` was never reached, and step 5
passed whether the replay branch existed or not. ``data/retractions.jsonl`` is 0
bytes, so the shipped curated list supplies nothing either. The second envelope
below is what the rebuild replays, and it is what makes that step mean
something -- with it, removing ``replay=True`` reverts the corrected wording and
step 5 fails.

Leaves two durable rows in the outbox for the life of the volume. Disposable
project only.
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
# Deliberately the SAME wording phase 1 already withdrew the record with. The
# curated envelope below is delivered live as well as replayed, and a delivery
# does correct the wording (`ON CONFLICT ... DO UPDATE SET retraction_reason`) --
# so a different reason here would change the ledger out from under phase 3's
# step 0 and turn a real assertion into a race. Identical wording makes the live
# delivery a no-op on the ledger's contents and leaves the replay pass as the
# only thing step 5 is measuring.
REASON = os.environ["AOW_DRILL_REASON"]

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
        # Required, and it was `None`. `schemas.Event.valid_until` is a plain
        # `datetime` under `extra="forbid"`, so the envelope was accepted by the
        # outbox -- which validates nothing -- published, and then rejected by
        # the consumer as `Poison` and dead-lettered. Phase 2 still printed a
        # message_id and exited 0, and phase 3 then waited 180 s for a record
        # that was never going to be stored. Derived from the same helper both
        # real producers call, so the injected listing expires by the shipped
        # policy rather than by a literal invented here.
        "valid_until": config.event_valid_until(now).isoformat(),
    },
    source="drill",
    observed_at=now,
    city=CITY,
)
# The curated half. `source="retractions"` is the label the real curated list
# carries -- `accept_retractions()` in services/ingestor/main.py passes exactly
# that to `envelopes_from` -- so this envelope sits in `wipe_user_data`'s replay
# set for the same reason a shipped withdrawal would, rather than because the
# drill asked for an exception.
#
# `retracted_at` is deliberately EARLIER than the decision phase 1 recorded. A
# central list carries the date the decision was taken, and neither a delivery
# nor a replay may move a date already on record: the ledger's conflict clause
# omits `retracted_at` and the row's UPDATE wraps it in COALESCE. Phase 3 then
# asserts the recorded date is still phase 1's, which with an earlier date in
# flight is a real assertion rather than a tautology.
retraction = Envelope.create(
    config.RK_RETRACT,
    {
        "entity": "events",
        "entity_id": EVENT_ID,
        "reason": REASON,
        "retracted_at": (now - timedelta(days=30)).isoformat(),
        "retracted_by": "drill",
    },
    source="retractions",
    observed_at=now,
    city=CITY,
)

box = Outbox(config.OUTBOX_PATH)
# The record first, then the withdrawal, which is the order a real boot uses:
# `accept_snapshot()` runs the snapshot files and then `accept_retractions()`.
# The rebuild does not depend on it -- `wipe_user_data` splits records and
# retractions into two passes precisely so outbox order cannot matter -- but the
# live delivery reads more like the real thing this way.
box.accept(envelope)
box.accept(retraction)
counts = box.counts()
# Two ids on stdout, the listing first and the curated withdrawal LAST, because
# the last line is the one the drill script captures -- the same convention phase 1
# uses for the decision date. Phase 3 asserts that THIS id reached `ingest_log`,
# rather than counting non-API retractions: a count says "something the rebuild
# will replay exists", and what step 5 needs is that the envelope naming this
# record is the one being replayed.
print(envelope.message_id)
print(retraction.message_id)
print(
    f"accepted; listing={envelope.message_id} curated_retraction={retraction.message_id}"
    f" outbox total={counts['total']} pending={counts['pending']}",
    file=sys.stderr,
)
