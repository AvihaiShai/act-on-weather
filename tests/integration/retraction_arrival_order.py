"""F9, phase 3 of 3: the record that arrived after its withdrawal is never published.

Run via ``docker compose exec -T api python - < tests/integration/retraction_arrival_order.py``,
against a DISPOSABLE project, and only after ``retraction_before_record.py`` and
``retraction_inject_event.py``. ``scripts/retraction-drill.sh`` drives all three
phases and passes the environment they share.

The defect. Before migration 010 a retraction that marked no row was spent the
moment it committed -- its ``message_id`` was in ``ingest_log``, so no redelivery
could ever reach the handler again -- and the record it named was published in
full when it finally arrived. The full argument, and the three fault-free routes
into that arrival order, are in ``retraction_before_record.py``.

Only a real database can prove the fix, for two reasons. "The record never
becomes visible" is a predicate repeated across a dozen reads, so the property
worth testing is that it is gone from every surface, not that one WHERE clause
works. And the second half is about a rebuild: ``user_data.wipe`` deletes every
collected row and replays it from the ingestor's outbox, which is exactly the
operation that used to bring withdrawn rows back.

What this phase proves:

  1. the record is stored -- it has a revision history, so the row exists;
  2. the recorded decision is unchanged, and in particular its date did not move
     to the moment the record arrived;
  3. it is absent from ``/events`` and from ``include_expired``, the read an
     operator uses to see what the freshness filter hid, which must not become a
     back door into withdrawn content;
  4. a rebuild replays it back into the table and it is STILL absent.

Step 1 is the one that fails on the old code: the row appears, published, and
every read serves it.

Destructive: ``POST /user-data/wipe`` deletes every collected row and rebuilds
them from the outbox. It takes no backup.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

sys.path.insert(0, "/app")

from services.common import config  # noqa: E402
from services.common.db import connect  # noqa: E402

BASE = os.environ.get("AOW_API_BASE", "http://127.0.0.1:8000")
CITY = os.environ["AOW_DRILL_CITY"]
EVENT_ID = os.environ["AOW_DRILL_EVENT_ID"]
REASON = os.environ["AOW_DRILL_REASON"]
# Phase 1 printed the decision date on its last line and the script passes it
# here. Required, not defaulted: without it this file cannot tell "the date never
# moved" from "there was never a date", and the second is the old defect.
DECIDED_AT = datetime.fromisoformat(os.environ["AOW_DRILL_RETRACTED_AT"])


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


def history_of(entity: str, entity_id: str) -> list:
    return get(f"/records/{entity}/{entity_id}/history")


def wait_until(predicate, what: str, timeout: int = 180):
    """Poll a read until the write behind it has landed."""
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
    """`record_retractions` has no read route and does not need one: it is the
    consumer's own durable state, not published output."""
    with connect(config.reader_dsn(), autocommit=True) as observer:
        return observer.execute(
            "SELECT entity, entity_id, retracted_at, retraction_reason, retracted_by"
            " FROM record_retractions WHERE entity = %s AND entity_id = %s",
            (entity, entity_id),
        ).fetchone()


# ----------------------------- 0. the drill really is in the state claimed ----

# Absence is what every assertion below looks for, and absence is also what an
# install that was never withdrawn from looks like. So the precondition is
# checked positively first, or this file passes without testing anything.
recorded = ledger_row("events", EVENT_ID)
assert recorded, (
    f"no withdrawal is recorded for {EVENT_ID}. Run "
    "tests/integration/retraction_before_record.py first."
)
assert recorded["retraction_reason"] == REASON, recorded

# ------------------- 1. the record arrived, and it really is stored ----

# The positive half. `record_history` carries no retraction filter -- a
# withdrawal is a revision of the row, not a delete -- so history is how a
# withdrawn row proves it is there without being served anywhere.
wait_until(
    lambda: history_of("events", EVENT_ID),
    f"{EVENT_ID} to be stored. Run tests/integration/retraction_inject_event.py "
    "in the ingestor first",
)
print(f"{EVENT_ID} is stored: it has a revision history")

# ----------------------- 2. the decision did not move when it arrived ----

stored = ledger_row("events", EVENT_ID)
assert stored["retracted_at"] == DECIDED_AT, (
    "the decision date moved when the record arrived. A withdrawal keeps the date "
    f"it was decided on: {DECIDED_AT.isoformat()} -> {stored['retracted_at'].isoformat()}"
)
print(f"the recorded decision is unchanged, still {DECIDED_AT.isoformat()}")

# --------------------- 3. and it is published nowhere at all ----

assert EVENT_ID not in ids(events()), (
    "the record was published even though it had already been withdrawn. This is "
    "the arrival-order defect: the retraction committed against an absent row, its "
    "message_id went into ingest_log, and nothing was left to withdraw the record "
    "when it finally arrived."
)
assert EVENT_ID not in ids(events(include_expired=True)), (
    "a withdrawn event came back under include_expired -- the operator read is a "
    "back door into retracted content"
)
by_city = {row["city_id"]: row for row in get("/coverage")["by_city"]}
served = len(events(include_expired=True))
assert by_city[CITY]["events"] <= served, (
    f"the per-city count ({by_city[CITY]['events']}) exceeds what the read serves "
    f"({served}), so one of the two is counting the withdrawn row"
)
print(f"absent from /events, from include_expired, and from the {CITY} counts")

# ------------------------- 4. a rebuild does not republish it either ----

# The operation that used to bring withdrawn rows back: every collected row is
# deleted and replayed from the ingestor's outbox -- which now holds the injected
# listing, so the rebuild genuinely re-creates the row rather than leaving it out.
#
# The wipe is asynchronous, and "the record is absent" is also true while the
# rebuild has simply not run yet, so waiting for absence would pass without
# testing anything. `wipe_user_data` clears `record_history` as its last
# statement, and step 1 proved this row had history, so an emptied history is a
# positive signal that the rebuild actually happened.
post("/user-data/wipe", {"confirm": "WIPE"})
wait_until(lambda: not history_of("events", EVENT_ID), "the rebuild to clear record_history")
wait_until(lambda: len(events(include_expired=True)) > 1, "the rebuild to restore the event feed")
wait_until(
    lambda: history_of("events", EVENT_ID),
    "the rebuild to replay the withdrawn listing back into the table",
)

assert EVENT_ID not in ids(events()), (
    "the rebuild republished the withdrawn record. A wipe replays collected "
    "envelopes, so a withdrawal that is not carried across undoes itself."
)
assert EVENT_ID not in ids(
    events(include_expired=True)
), "the rebuild republished the withdrawn record under include_expired"

after = ledger_row("events", EVENT_ID)
assert after, "the rebuild emptied the ledger"
assert after["retracted_at"] == DECIDED_AT, (
    "the rebuild restamped the withdrawal with the clock of the rebuild: "
    f"{DECIDED_AT.isoformat()} -> {after['retracted_at'].isoformat()}"
)

print("OK: a record withdrawn before this install held it stayed withdrawn when it")
print("    arrived, and stayed withdrawn across a full rebuild from the outbox.")
