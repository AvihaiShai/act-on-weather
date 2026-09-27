"""Which local days a stored listing is offered on, checked against its source.

Six of the 55 rows in `data/events.seed.jsonl` carry an `ends_at`; five of
those end on a later local day than they start. A dress-rehearsal review read
that shape as a defect -- "four one-night concert rows that spread one night
over extra days" -- and proposed collapsing four of the five to a single day,
sparing only the Laver Cup as a legitimate three-day tournament.

**That reading is wrong, and this file is here so nobody acts on it.** Every
one of the five is a genuine multi-day run. Checked on 2026-09-27 against each
venue's own listing, which is the same page and the same JSON-LD block that
`services/tools/event_recheck.py` reads:

    theo2:laver-cup-2026       "startDate": "2026-09-25T11:30:00+01:00"
                               "endDate":   "2026-09-27T12:30:00+01:00"
    theo2:niall-horan-2026     "startDate": "2026-10-02T18:30:00+01:00"
                               "endDate":   "2026-10-03T23:00:00+01:00"
    theo2:the-strokes          "startDate": "2026-10-06T18:30:00+01:00"
                               "endDate":   "2026-10-07T23:00:00+01:00"
    theo2:westlife-2026        "startDate": "2026-10-09T18:30:00+01:00"
                               "endDate":   "2026-10-11T22:30:00+01:00"

The three O2 concerts each list a door time per night -- two nights for Niall
Horan and The Strokes, three for Westlife -- so the spans are the venue's, not
an artefact. The Laver Cup values are also the ones already transcribed in
`tests/unit/test_recheck.py:57-58`, which is what confirms these readings came
off the real pages.

The fifth row is Lisbon's, and its span was never in doubt: the seed's own
`source` field records "run of 30 September - 2 October 2026, 21:30 each
night", and the page still reads `30 setembro, 2026 a 2 outubro, 2026` at
21:30 with no cancellation marker. It has no structured data, which is why
`tests/integration/event_recheck.py` uses it as the row that cannot be
auto-renewed.

What *is* a placeholder is the time of day. Seven theo2 rows are stored at
local midnight and the ends at 23:59, because a multi-day run is recorded as
whole days on purpose -- `services/tools/event_recheck.py:324-330` states the
decision, and the recheck tool compares at day granularity so that 00:00
against the page's 11:30 is not reported as a change. So this file asserts the
**days**, which are sourced, and says nothing about the instants, which are
not. Correcting the instants would be a separate change with its own evidence,
and it would move none of the days below.

The day arithmetic mirrors `EVENTS_LOCALISED_SQL`
(`services/common/queries.py:56-70`) rather than trusting the stored offset:
the window is half-open, so a run billed as ending at local midnight ends on
the previous day, and the end can never fall before the start.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "data/events.seed.jsonl"

VERIFIED = [
    json.loads(line) for line in SEED.read_text(encoding="utf-8").splitlines() if line.strip()
]
CITIES = {
    city["slug"]: city
    for city in yaml.safe_load((ROOT / "data/cities.yml").read_text(encoding="utf-8"))["cities"]
}

# The local days each listing is offered on, as its own venue's page gives
# them. A row absent from this table is a one-day listing; a row present is a
# run somebody read off a source. Nothing may be added here without a quoted
# source in the docstring above.
SOURCED_RUNS = {
    "theo2:laver-cup-2026": (date(2026, 9, 25), date(2026, 9, 27)),
    "theo2:niall-horan-2026": (date(2026, 10, 2), date(2026, 10, 3)),
    "theo2:the-strokes": (date(2026, 10, 6), date(2026, 10, 7)),
    "theo2:westlife-2026": (date(2026, 10, 9), date(2026, 10, 11)),
    "coliseulisboa:radio-macau-2026-09-30": (date(2026, 9, 30), date(2026, 10, 2)),
}


def local_days(row: dict) -> tuple[date, date]:
    """`starts_on` and `ends_on`, derived the way the SQL derives them."""
    zone = ZoneInfo(CITIES[row["city_id"]]["timezone"])
    starts_on = datetime.fromisoformat(row["starts_at"]).astimezone(zone).date()
    closes = datetime.fromisoformat(row["ends_at"] or row["starts_at"])
    ends_on = (closes - timedelta(microseconds=1)).astimezone(zone).date()
    return starts_on, max(starts_on, ends_on)


@pytest.mark.parametrize("row", VERIFIED, ids=[row["id"] for row in VERIFIED])
def test_a_row_runs_for_exactly_the_days_its_source_gives(row):
    """The days a listing is offered on are the days somebody read off a page.

    A row that spreads one night across two dates invents a night. A run
    collapsed to its first night loses one -- which is the mistake this file
    exists to prevent, and the more expensive of the two, because a reviewer
    who opens the venue page sees the night we dropped.
    """
    starts_on, ends_on = local_days(row)
    expected = SOURCED_RUNS.get(row["id"], (starts_on, starts_on))
    assert (starts_on, ends_on) == expected


def test_only_sourced_runs_span_more_than_one_day():
    """The guard on the table above: a new multi-day row has to be declared
    here, with its source quoted, rather than appearing quietly in the seed."""
    spanning = {row["id"] for row in VERIFIED if local_days(row)[0] != local_days(row)[1]}
    assert spanning == set(SOURCED_RUNS)


def test_the_laver_cup_is_still_the_three_day_tournament():
    """Named on its own because it is the row every review agrees must not be
    touched, and the one most other tests pin."""
    row = next(r for r in VERIFIED if r["id"] == "theo2:laver-cup-2026")
    starts_on, ends_on = local_days(row)
    assert row["category"] == "sport"
    assert (ends_on - starts_on).days + 1 == 3


def test_a_run_billed_to_local_midnight_would_not_open_an_extra_day():
    """The half-open rule, asserted on data rather than on the SQL: this is
    what stops `ends_at` at 00:00 from adding a day the venue never listed."""
    zone = ZoneInfo("Europe/London")
    midnight = datetime(2026, 10, 8, 0, 0, tzinfo=zone)
    assert (midnight - timedelta(microseconds=1)).astimezone(zone).date() == date(2026, 10, 7)
