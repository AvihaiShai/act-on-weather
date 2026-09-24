"""The verified event seed, checked as data.

`data/events.seed.jsonl` is the one file in this system that is neither fetched
nor generated: somebody opened a venue's or an organiser's own listing page,
read the title, the date, the time and the hall off it, and wrote a row. That
makes it the file most able to go quietly wrong, and the one thing the brief is
absolute about -- no invented events, concerts or fixtures -- is exactly what a
wrong row here would break.

So these tests do not test code. They test the shipped file, on the properties
a reader of an agent answer is entitled to assume:

  * every row is a real listing, not a sample, and says where it came from;
  * every row names a city this system actually knows;
  * every start time is a real instant carrying the city's own UTC offset, so
    "Friday lunchtime in London" is Friday lunchtime in London and not in UTC;
  * a run that ends, ends after it starts;
  * and the snapshot the ingestor replays is that file, byte for byte.

A place is not an event: the seed is allowed to name a venue, but a venue page
is never on its own the evidence that something is scheduled there, so every
row must point at a listing URL of its own.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from services.common import schemas

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "data/events.seed.jsonl"
SNAPSHOT = ROOT / "data/snapshot/events.jsonl"
SAMPLES = ROOT / "data/events.samples.jsonl"


def read(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


VERIFIED = read(SEED)
CITIES = {
    city["slug"]: city
    for city in yaml.safe_load((ROOT / "data/cities.yml").read_text(encoding="utf-8"))["cities"]
}


def ids(rows: list[dict]) -> list[str]:
    return [row["id"] for row in rows]


# ------------------------------------------------------------ the file ----


def test_the_seed_is_not_empty():
    """Guards every parametrised test below: an empty file would make them
    all pass without asserting anything."""
    assert len(VERIFIED) >= 7


def test_ids_are_unique():
    """The id is the idempotency key. A duplicate would silently collapse two
    listings into one row, and the second would be lost on replay."""
    assert len(set(ids(VERIFIED))) == len(VERIFIED)


def test_the_snapshot_is_the_seed():
    """`fetch_content.load_events` copies the seed through unchanged, so a
    snapshot that has drifted from it means one of the two was edited alone --
    and the snapshot is the one the air-gapped run actually replays."""
    assert read(SNAPSHOT) == VERIFIED


def test_no_verified_id_is_also_a_sample_id():
    """The two files are never merged, but they are both keyed on `id`. A
    collision would let a generated row supersede a checked one."""
    assert not set(ids(VERIFIED)) & set(ids(read(SAMPLES)))


# ------------------------------------------------------------- per row ----


@pytest.mark.parametrize("row", VERIFIED, ids=ids(VERIFIED))
def test_row_validates_against_the_payload_model(row):
    """The consumer validates against this model before it writes. A row that
    fails it is a poison message shipped in the repo."""
    schemas.Event(**row)


@pytest.mark.parametrize("row", VERIFIED, ids=ids(VERIFIED))
def test_row_is_a_real_listing_and_says_so(row):
    """`is_sample` is what the UI and the agent read to decide whether to warn
    the reader. In this file it is false, and the title never borrows the
    sample prefix that marks a generated row."""
    assert row["is_sample"] is False
    assert not row["title"].startswith("Sample: ")


@pytest.mark.parametrize("row", VERIFIED, ids=ids(VERIFIED))
def test_row_carries_a_source_and_a_listing_url(row):
    """Provenance is the whole basis for claiming the event is real. The URL
    must be a listing that can be re-opened and re-checked -- which is also why
    a row is not allowed to fall back to a venue's own page with no listing."""
    assert row["source"].strip()
    assert row["source_url"].startswith("https://")


@pytest.mark.parametrize("row", VERIFIED, ids=ids(VERIFIED))
def test_row_records_when_it_was_checked(row):
    """`as_of` is the last-checked time, and it is what the UI prints next to
    the answer. A listing can be cancelled or moved after it was read, so a row
    without a checked-at date is not verifiable, only asserted."""
    checked = datetime.fromisoformat(row["as_of"])
    assert checked.tzinfo is not None


@pytest.mark.parametrize("row", VERIFIED, ids=ids(VERIFIED))
def test_row_names_a_known_city(row):
    """`city_id` is a foreign key to `cities`. A typo would be rejected by the
    database at the write boundary, which is far too late to notice."""
    assert row["city_id"] in CITIES


@pytest.mark.parametrize("row", VERIFIED, ids=ids(VERIFIED))
def test_start_time_carries_the_city_s_own_utc_offset(row):
    """The single mistake this file is most likely to make. A listing prints a
    local time; storing it with the wrong offset moves a lunchtime concert into
    the morning and can move an evening one onto the previous day, which then
    lands under the wrong forecast date."""
    starts = datetime.fromisoformat(row["starts_at"])
    assert starts.tzinfo is not None, "a naive start time is not an instant"
    tz = ZoneInfo(CITIES[row["city_id"]]["timezone"])
    assert starts.utcoffset() == starts.astimezone(tz).utcoffset()


@pytest.mark.parametrize(
    "row", [r for r in VERIFIED if r["ends_at"]], ids=ids([r for r in VERIFIED if r["ends_at"]])
)
def test_a_run_that_ends_ends_after_it_starts(row):
    """Multi-night runs are one row with an `ends_at`, the same shape the
    existing O2 rows use. An inverted pair would make the row match no date at
    all, which reads as "no data" rather than as a broken row."""
    starts = datetime.fromisoformat(row["starts_at"])
    ends = datetime.fromisoformat(row["ends_at"])
    assert ends > starts
    assert ends - starts < timedelta(days=30), "a month-long 'event' is probably a season"


# ------------------------------------------------------------ coverage ----


def test_london_has_a_verified_concert_inside_the_stored_forecast_window():
    """The brief's own example asks for a concert in London this week. It is
    answerable only if a checked concert listing falls inside the window the
    weather snapshot covers -- otherwise the honest answer is "no data", which
    is correct but demonstrates nothing."""
    window = sorted(
        json.loads(line)["forecast_date"]
        for line in (ROOT / "data/snapshot/weather.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    first, last = window[0], window[-1]
    concerts = [
        row
        for row in VERIFIED
        if row["city_id"] == "london"
        and row["category"] == "concert"
        and first <= row["starts_at"][:10] <= last
    ]
    assert concerts, f"no verified London concert between {first} and {last}"


def test_every_city_the_system_knows_has_at_least_one_verified_event():
    """Not a property of the world -- a property we went and checked. Four of
    the five cities had no verified listing at all, which left the planner
    demonstrable only in London or only on generated samples."""
    covered = {row["city_id"] for row in VERIFIED}
    assert covered == set(CITIES), f"no verified event for {sorted(set(CITIES) - covered)}"
