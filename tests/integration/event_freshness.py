"""F9: a stale listing stops being presented as a scheduled event.

Run via ``docker compose exec -T api python - < tests/integration/event_freshness.py``.

An event row is not an observation. It is somebody's note of a web page, taken
on a particular day, and an air-gapped run has no way to discover that the
venue cancelled the show the following week. The policy that follows is that a
row carries the day it was checked, an expiry derived from it, and stops being
returned as a current schedule once that expiry passes.

Only a real database can prove that, because the freshness filter is a SQL
predicate over ``now()`` -- and the whole round trip is worth exercising rather
than the predicate alone, because the interesting property is not "the WHERE
clause works" but "a re-check travels through the queue and moves the row".

So the drill is end to end:

  1. a verified row is current, and visible;
  2. its ``checked_at`` is patched back into the past *through the API*, which
     means through the outbox, the broker and the consumer like any other
     correction (M4, M12);
  3. the row disappears from the default read, which is what the agent, the
     itinerary and the UI all use;
  4. it is still there, still counted, and still carries its provenance under
     ``include_expired`` -- because "the feed went stale" and "there was never
     anything here" are different problems and only the first is fixed by a
     connected refresh;
  5. re-checking it restores it, and the expiry moves with the check rather
     than being settable on its own.

Step 5 is the one that would catch the tempting wrong fix: making ``checked_at``
patchable without recomputing ``valid_until`` gives an operator a button that
looks like it re-checked a listing and does nothing at all.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/app")

BASE = os.environ.get("AOW_API_BASE", "http://127.0.0.1:8000")

# A row from the committed snapshot with fixed dates, so this keeps meaning the
# same thing after the staged forecast has expired.
EVENT_ID = "theo2:laver-cup-2026"
CITY = "london"

# Far enough back that the row is expired under any plausible recheck window,
# and a real instant rather than a relative one so the assertion is readable in
# a failure log.
LONG_AGO = "2020-01-01T00:00:00+00:00"


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return json.load(response)


def patch(entity_id: str, fields: dict) -> str:
    request = urllib.request.Request(
        f"{BASE}/records/events/{entity_id}",
        data=json.dumps(fields).encode(),
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)["message_id"]


def ids(rows) -> set[str]:
    return {row["id"] for row in rows}


def current(city: str = CITY) -> list[dict]:
    return get(f"/events?city={city}&limit=100")


def everything(city: str = CITY) -> list[dict]:
    return get(f"/events?city={city}&include_expired=true&limit=100")


def wait_until(predicate, what: str, timeout: int = 180):
    """Poll a read until the write behind it has landed.

    A patch is accepted by the API and applied by the consumer, so the two are
    deliberately not synchronous. Every assertion below therefore waits for the
    effect rather than for the acknowledgement.
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


# ------------------------------------------- 1. the shipped row is current ----

wait_until(lambda: EVENT_ID in ids(current()), f"{EVENT_ID} to reach the database")

[row] = [r for r in current() if r["id"] == EVENT_ID]
assert row["is_current"] is True, row
assert row["checked_at"], "a verified row with no checked-at date is asserted, not verified"
assert row["valid_until"] > row["checked_at"], (row["checked_at"], row["valid_until"])

# Every other verified row in the default read is current too: the filter is on
# by default, not something each caller has to remember.
assert all(r["is_current"] for r in current()), "an expired row reached the default read"

before = len(current())

# ------------------------------- 2. a re-check travels through the queue ----

patch(EVENT_ID, {"checked_at": LONG_AGO})
wait_until(lambda: EVENT_ID not in ids(current()), "the stale row to leave the default read")

# ------------------------------ 3. it is gone from what a traveller sees ----

assert EVENT_ID not in ids(current()), "a listing nobody has re-checked since 2020 is current"
assert len(current()) == before - 1, "the stale row took something else with it"

# The category and date filters do not smuggle it back in: freshness is applied
# before them, not instead of them.
window = f"/events?city={CITY}&start=2026-09-25&end=2026-09-27"
assert EVENT_ID not in ids(get(window + "&category=sport"))
assert EVENT_ID not in ids(get(window))

# ------------------- 4. it is still stored, counted, and still has a source ----

[stale] = [r for r in everything() if r["id"] == EVENT_ID]
assert stale["is_current"] is False, stale
assert stale["source_url"].startswith("https://"), "provenance was dropped with the row"
assert stale["checked_at"].startswith("2020-01-01"), stale["checked_at"]

coverage = get("/coverage")
freshness = coverage["event_freshness"]
assert freshness["expired"] >= 1, freshness
assert freshness["current"] == before - 1, freshness
by_city = {c["city_id"]: c for c in coverage["by_city"]}
assert by_city[CITY]["verified_events_expired"] >= 1, by_city[CITY]
# The advertised event window is measured over current rows only, so it cannot
# promise dates nothing will be returned for.
events_entity = next(e for e in coverage["entities"] if e["entity"] == "events")
assert events_entity["rows"] == freshness["current"] + freshness["samples"], events_entity

# ------------------------------------ 5. re-checking it brings it back ----

# Deliberately not a hand-set expiry: `valid_until` is not patchable, and the
# only thing that extends a row's life is re-opening its listing page.
with contextlib.suppress(urllib.error.HTTPError):
    # Rejected at the API if it validates there; otherwise the consumer
    # dead-letters it as an unpatchable column. Either way the row stays stale,
    # which is the property being asserted.
    patch(EVENT_ID, {"valid_until": "2099-01-01T00:00:00+00:00"})
assert EVENT_ID not in ids(current()), "a hand-set expiry revived a stale row"

recheck = row["checked_at"]
patch(EVENT_ID, {"checked_at": recheck})
wait_until(lambda: EVENT_ID in ids(current()), "the re-checked row to come back")

[restored] = [r for r in current() if r["id"] == EVENT_ID]
assert restored["is_current"] is True, restored
assert restored["valid_until"] == row["valid_until"], (
    "the expiry did not move with the check date",
    restored["valid_until"],
    row["valid_until"],
)
# The correction is a correction like any other: it is in the history, with a
# revision, because it went through the queue and the consumer's trigger.
history = get(f"/records/events/{EVENT_ID}/history")
assert history, "a re-check left no history"

print(
    f"PASS: {EVENT_ID} left the default read when its check date aged out, stayed "
    f"visible and counted under include_expired, and came back with its derived "
    f"expiry when it was re-checked ({len(history)} history rows)"
)
