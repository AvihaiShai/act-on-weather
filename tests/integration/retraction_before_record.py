"""F9, phase 1 of 3: withdraw a record this install does not hold.

Run via ``docker compose exec -T api python - < tests/integration/retraction_before_record.py``,
against a DISPOSABLE project. ``scripts/retraction-drill.sh`` drives all three
phases and passes the environment they share; this file on its own leaves a
withdrawal behind that cannot be undone.

The defect the three phases close. Migration 009 withdraws a record by marking
its row, so the withdrawal only worked if the row was already there when the
message arrived. When it was not, the UPDATE matched nothing, the handler logged
a warning, and the transaction committed anyway -- carrying the envelope's
``message_id`` into ``ingest_log``. From that point the withdrawal was spent: the
id is deterministic, the outbox row is marked published and never re-drained, and
``ingest_log`` short-circuits every redelivery as a duplicate before the handler
is reached. The record then arrived later and was published in full, with nothing
left to withdraw it.

That ordering is ordinary, not exotic. Three routes reach it with no fault at
all:

  * the curated ``data/retractions.jsonl`` list is re-accepted on every boot and
    applied against whatever this install happens to have ingested, so it
    legitimately names records that are not here yet -- the handler's own
    docstring treats that as normal;
  * ``POST /records/{entity}/{id}/retract`` publishes from the API's own outbox,
    which has no ordering relationship to the ingestor's, so retracting while the
    first snapshot is still draining wins the race by construction;
  * a record that dead-letters is stored only when an operator redrives it, which
    is long after the retraction that named it committed.

What this phase proves: the decision reaches ``record_retractions`` even though
it marked no row. That is the half that used to be lost, and it is what phase 3
relies on.

Every constant comes from the environment rather than a literal, because three
files in three containers have to agree on the record under test and a typo in a
duplicated literal would make phase 3 pass by looking for something nobody had
withdrawn. Integration tests here are streamed in on stdin, so they cannot import
a shared module -- the script is the one place the values are written down.

Destructive: it retracts a record and there is deliberately no un-retract route.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/app")

from services.common import config  # noqa: E402
from services.common.db import connect  # noqa: E402

BASE = os.environ.get("AOW_API_BASE", "http://127.0.0.1:8000")
CITY = os.environ["AOW_DRILL_CITY"]
EVENT_ID = os.environ["AOW_DRILL_EVENT_ID"]
REASON = os.environ["AOW_DRILL_REASON"]


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return json.load(response)


def post(path: str, body: dict):
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def ids(rows) -> set[str]:
    return {row["id"] for row in rows}


def events(include_expired: bool = False) -> list[dict]:
    extra = "&include_expired=true" if include_expired else ""
    return get(f"/events?city={CITY}&limit=500{extra}")


def wait_until(predicate, what: str, timeout: int = 180):
    """Poll a read until the write behind it has landed.

    Every write here is accepted by one service and applied by another, so the
    two are deliberately not synchronous. Every assertion waits for the effect
    rather than for the acknowledgement.
    """
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except (OSError, urllib.error.URLError) as exc:
            last = exc
        time.sleep(2)
    raise AssertionError(f"timed out waiting for {what}: {last!r}")


def ledger_row(entity: str, entity_id: str):
    """The one read that needs the database rather than the API.

    `record_retractions` has no read route and does not need one: it is the
    consumer's own durable state, not published output. Read through the reader
    role, which is the same SELECT-only grant every other service gets.
    """
    with connect(config.reader_dsn(), autocommit=True) as observer:
        return observer.execute(
            "SELECT entity, entity_id, retracted_at, retraction_reason, retracted_by"
            " FROM record_retractions WHERE entity = %s AND entity_id = %s",
            (entity, entity_id),
        ).fetchone()


# ------------------------------------- 0. the record really is absent first ----

# Otherwise this is an ordinary retraction and the drill is testing the arrival
# order that already worked.
wait_until(lambda: events(include_expired=True), "the event feed to reach the database")
assert EVENT_ID not in ids(events(include_expired=True)), (
    f"{EVENT_ID} is already stored, so this drill cannot prove anything about a "
    "withdrawal that arrives first. Run it against a disposable project."
)
assert not ledger_row("events", EVENT_ID), "the ledger already holds this decision"
print(f"{EVENT_ID} is not stored, and no decision is recorded for it")

# ---------------------- 1. withdraw a record this install does not hold ----

accepted = post(
    f"/records/events/{EVENT_ID}/retract",
    {"reason": REASON, "retracted_by": "drill"},
)
assert accepted["accepted"] is True, accepted
print(f"accepted the retraction of an absent {CITY} events row as {accepted['message_id']}")

# ------------------------------- 2. the decision is recorded all the same ----

# Without this row the withdrawal exists nowhere once the transaction commits,
# because its message_id is in `ingest_log` and no redelivery will ever reach the
# handler again.
recorded = wait_until(
    lambda: ledger_row("events", EVENT_ID),
    "the withdrawal to be recorded in record_retractions",
)
assert recorded["retraction_reason"] == REASON, recorded
assert recorded["retracted_by"] == "drill", recorded

print("OK: the decision is recorded although it marked no row.")
# Alone on the last line, so the drill script can capture it and hand it to
# phase 3, which asserts the date did not move when the record arrived.
print(recorded["retracted_at"].isoformat())
