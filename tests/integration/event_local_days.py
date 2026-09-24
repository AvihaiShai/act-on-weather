"""F2: an event's calendar day is the day it falls on in the city.

Run via ``docker compose exec -T api python - < tests/integration/event_local_days.py``.

This is the half of F2 that only a real database can answer. The local dates
are derived in SQL (``queries.EVENTS_LOCALISED_SQL``) by ``AT TIME ZONE`` over
the city's IANA zone, so a Python fixture cannot prove the rule holds -- only
Postgres, with the real tzdata, can.

Everything asserted here is fixed. The Laver Cup row is in the committed
snapshot with hard-coded dates, and the literal timestamps below are written
out, so nothing depends on today's date or on the staged forecast window: these
assertions keep meaning the same thing long after the snapshot's weather has
expired.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, "/app")

from services.common import config, queries  # noqa: E402
from services.common.db import Pool  # noqa: E402

BASE = os.environ.get("AOW_API_BASE", "http://127.0.0.1:8000")

# The seeded London row, verbatim from data/snapshot/events.jsonl:
#   starts_at 2026-09-25T00:00:00+01:00  ->  2026-09-24T23:00:00Z
#   ends_at   2026-09-27T23:59:00+01:00
# Local midnight on the 25th. Read as UTC it is the 24th, which is the day the
# API filter, the agent and the itinerary all used to report.
EVENT_ID = "theo2:laver-cup-2026"
LOCAL_START = "2026-09-25"
LOCAL_END = "2026-09-27"


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return json.load(response)


def ids(rows) -> set[str]:
    return {row["id"] for row in rows}


def day(value: str) -> list[dict]:
    return get(f"/events?city=london&start={value}&end={value}&limit=50")


def wait_for_event() -> None:
    """The snapshot reaches the database through the queue, so give it time."""
    deadline = time.monotonic() + 180
    last: object = None
    while time.monotonic() < deadline:
        try:
            rows = get("/events?city=london&limit=50")
            last = sorted(ids(rows))
            if EVENT_ID in ids(rows):
                return
        except OSError as exc:
            last = exc
        time.sleep(2)
    raise AssertionError(f"{EVENT_ID} never reached the database: {last}")


wait_for_event()

# -------------------------------------------- 1. the local start date wins ----

[row] = [r for r in day(LOCAL_START) if r["id"] == EVENT_ID]
assert row["starts_on"] == LOCAL_START, row["starts_on"]
assert row["ends_on"] == LOCAL_END, row["ends_on"]
assert row["timezone"] == "Europe/London", row["timezone"]
# The stored instant is untouched. Only its interpretation moved: this is a
# reading fix, not a data migration.
assert row["starts_at"].startswith("2026-09-24T23:00:00"), row["starts_at"]

# The day before is the UTC day, and the day the old filter answered with.
assert EVENT_ID not in ids(day("2026-09-24")), "the UTC day still matches"

# ----------------------------- 2. a multi-day event is on every day it runs ----

for active in (LOCAL_START, "2026-09-26", LOCAL_END):
    assert EVENT_ID in ids(day(active)), f"missing on {active}"
assert EVENT_ID not in ids(day("2026-09-28")), "matched the day after it ends"

# A range holding none of its days must not match; one overlapping only its
# middle must.
assert EVENT_ID not in ids(get("/events?city=london&start=2026-09-20&end=2026-09-24"))
assert EVENT_ID in ids(get("/events?city=london&start=2026-09-26&end=2026-09-26"))

# The category filter still applies on top of the date filter (F3).
window = f"/events?city=london&start={LOCAL_START}&end={LOCAL_END}"
assert EVENT_ID not in ids(get(window + "&category=concert"))
assert EVENT_ID in ids(get(window + "&category=sport"))

# The verified London concert on the same local day, from the widened feed
# (F9). It is what E2 now answers from, so the day it lands on is load-bearing:
#   starts_at 2026-09-25T12:30:00+01:00, ends_at 13:15 the same day
CONCERT_ID = "lso:free-friday-lunchtime-2026-09-25"
assert CONCERT_ID in ids(get(window + "&category=concert")), "the seeded concert is not on the 25th"
assert CONCERT_ID not in ids(get(window + "&category=sport"))
assert CONCERT_ID not in ids(day("2026-09-26")), "a lunchtime recital ran into the next day"

# ------------- 2b. the same rules against the other rows the feed now holds ----

# Another local-midnight start, in the same zone but three weeks later, so the
# expression is exercised on a row the F2 work did not have in front of it:
#   Niall Horan, 2026-10-02T00:00:00+01:00 -> 2026-10-01T23:00:00Z
NIALL = "theo2:niall-horan-2026"
assert NIALL in ids(day("2026-10-02")), "the local-midnight concert lost its own day"
assert NIALL not in ids(day("2026-10-01")), "the UTC day matched again"
assert NIALL in ids(day("2026-10-03")), "the second day of a two-day run is missing"

# A multi-day row in a different city, so the join really is per-city and not a
# single server zone: Lisbon, 2026-09-30T21:30+01:00 to 2026-10-02T23:59+01:00.
RADIO_MACAU = "coliseulisboa:radio-macau-2026-09-30"
for active in ("2026-09-30", "2026-10-01", "2026-10-02"):
    assert RADIO_MACAU in ids(
        get(f"/events?city=lisbon&start={active}&end={active}&limit=50")
    ), f"the Lisbon run is missing on {active}"
assert RADIO_MACAU not in ids(get("/events?city=lisbon&start=2026-10-03&end=2026-10-03&limit=50"))

# ---------------------------------- 3. the half-open rule, against tzdata ----

# An event billed as ending at local midnight ends on the previous day rather
# than opening the next one. No row in the default snapshot does that, so the
# expression is evaluated against literal timestamps instead -- still the real
# Postgres, the real zones and the real DST offsets.
LDN, LIS = "Europe/London", "Europe/Lisbon"

# (starts_at, ends_at, timezone) -> (expected starts_on, expected ends_on)
CASES = [
    # Local midnight start, three days long: the Laver Cup's own timestamps.
    (("2026-09-25T00:00+01:00", "2026-09-27T23:59+01:00", LDN), ("2026-09-25", "2026-09-27")),
    # Ends at local midnight, so it is still a one-day event.
    (("2026-09-23T20:00+00:00", "2026-09-23T23:00+00:00", LIS), ("2026-09-23", "2026-09-23")),
    # No end at all: one day, and not the day before it.
    (("2026-09-25T00:00+01:00", None, LDN), ("2026-09-25", "2026-09-25")),
    # Zero length, clamped to its own start.
    (("2026-09-25T10:00+01:00", "2026-09-25T10:00+01:00", LDN), ("2026-09-25", "2026-09-25")),
    # BST ends 2026-10-25: 23:30Z on the 24th is 00:30 local on the 25th.
    (("2026-10-24T23:30+00:00", "2026-10-25T02:00+00:00", LDN), ("2026-10-25", "2026-10-25")),
]

SQL = """
SELECT (%(starts_at)s::timestamptz AT TIME ZONE %(tz)s)::date AS starts_on,
       GREATEST(
         (%(starts_at)s::timestamptz AT TIME ZONE %(tz)s)::date,
         ((COALESCE(%(ends_at)s::timestamptz, %(starts_at)s::timestamptz)
             - INTERVAL '1 microsecond') AT TIME ZONE %(tz)s)::date
       ) AS ends_on
"""

conn = Pool(config.reader_dsn(), autocommit=True).conn
for (starts_at, ends_at, tz), (starts_on, ends_on) in CASES:
    got = conn.execute(SQL, {"starts_at": starts_at, "ends_at": ends_at, "tz": tz}).fetchone()
    what = f"{starts_at} to {ends_at} in {tz}"
    assert str(got["starts_on"]) == starts_on, f"{what}: start {got['starts_on']}"
    assert str(got["ends_on"]) == ends_on, f"{what}: end {got['ends_on']}"
    # The days the rest of the system then files this event under.
    days = queries.event_days(got)
    assert str(days[0]) == starts_on and str(days[-1]) == ends_on, f"{what}: {days}"

print(
    f"PASS: event days are local ({EVENT_ID} runs {LOCAL_START} to {LOCAL_END}), "
    f"multi-day rows match every active day, {len(CASES)} boundary cases hold"
)
