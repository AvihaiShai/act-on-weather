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
  * every row records the day somebody last opened its listing page, and the
    snapshot carries the expiry derived from that day;
  * and the snapshot the ingestor replays is that file plus that one derived
    column, and nothing else.

A place is not an event: the seed is allowed to name a venue, but a venue page
is never on its own the evidence that something is scheduled there, so every
row must point at a listing URL of its own.

The freshness half of this file exists because of F9. A stored event is a
reading of a web page taken on a particular day; an air-gapped run cannot
discover that the venue cancelled the show a week later, so the only honest
thing it can do is record when the reading was taken and stop presenting it as
a schedule once it is too old. `checked_at` is that day, `valid_until` is when
it stops counting, and the derivation between them belongs to
`config.event_valid_until` so that no two producers implement it differently.

The committed `valid_until` is the default window applied at `make snapshot`
time, and the running stack does not depend on it: the ingestor re-derives it
on accept from the window actually configured, so a deployment that re-checks
weekly does not inherit the expiry of the machine that built the file.
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
SHIPPED = read(SNAPSHOT)
CITIES = {
    city["slug"]: city
    for city in yaml.safe_load((ROOT / "data/cities.yml").read_text(encoding="utf-8"))["cities"]
}
EVENT_TYPES = set(
    yaml.safe_load((ROOT / "data/event_types.yml").read_text(encoding="utf-8"))["event_types"]
)

# The window `make snapshot` builds the committed file with, and therefore the
# only one the committed file can be checked against. Deliberately a literal
# here and not `config.EVENT_RECHECK_DAYS`: see the test that uses it.
DEFAULT_WINDOW = 21


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


def test_the_snapshot_is_the_seed_plus_its_derived_expiry():
    """`fetch_content.load_events` copies the seed through and adds exactly one
    column, so a snapshot that has drifted from the seed in any other way means
    one of the two was edited alone -- and the snapshot is the one the
    air-gapped run actually replays.

    `valid_until` is the one column it may add, and it is derived rather than
    written into the seed on purpose: the expiry is a property of the freshness
    policy, not of the listing, so no committed row can opt out of the recheck
    window by carrying an expiry of its own."""
    shipped = {row["id"]: dict(row) for row in SHIPPED}
    assert set(shipped) == {row["id"] for row in VERIFIED}
    for row in VERIFIED:
        stripped = dict(shipped[row["id"]])
        assert stripped.pop("valid_until")
        assert stripped == row


@pytest.mark.parametrize("row", read(SNAPSHOT), ids=ids(read(SNAPSHOT)))
def test_the_shipped_expiry_is_the_default_window_after_the_check(row):
    """The derivation, checked against the policy rather than against a literal.

    This is the assertion that would catch a hand-edited snapshot: a row whose
    expiry was extended without re-opening its listing page would pass every
    other test in this file, and would be exactly the thing the freshness
    policy exists to prevent.

    Asserted against the *default* window rather than `config.EVENT_RECHECK_DAYS`
    on purpose. The committed file is an artifact, built once; the constant is
    whatever the machine running the suite happens to be configured with, and
    coupling the two would fail 39 tests over data that is not wrong. The
    running stack does not depend on the shipped value either -- the ingestor
    re-derives it on accept from the window actually in force."""
    checked = datetime.fromisoformat(row["checked_at"])
    assert datetime.fromisoformat(row["valid_until"]) == checked + timedelta(days=DEFAULT_WINDOW)


def test_no_verified_id_is_also_a_sample_id():
    """The two files are never merged, but they are both keyed on `id`. A
    collision would let a generated row supersede a checked one."""
    assert not set(ids(VERIFIED)) & set(ids(read(SAMPLES)))


# ------------------------------------------------------------- per row ----


@pytest.mark.parametrize("row", read(SNAPSHOT), ids=ids(read(SNAPSHOT)))
def test_row_validates_against_the_payload_model(row):
    """The consumer validates against this model before it writes. A row that
    fails it is a poison message shipped in the repo.

    Validated against the snapshot rather than the seed, because the snapshot
    is what the ingestor actually publishes: the seed is one derived column
    short of a payload, and validating it would be testing a shape no message
    ever carries."""
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
def test_the_listing_url_is_not_a_venue_record(row):
    """The distinction the whole feed rests on: a venue exists, an event is
    scheduled.

    A generated sample's URL is deliberately the venue's own Wikidata or
    OpenStreetMap record, and the UI renders it as a *venue reference* rather
    than a source, because there is no listing to point at. A verified row is
    the opposite claim -- something is on, here, on this date -- and it is only
    entitled to make it if the URL is a page that says so. A Wikidata entry
    behind a verified row would be a venue promoted into a schedule, which is
    exactly the defect F3 closed in the agent and this keeps out of the data."""
    url = row["source_url"]
    for venue_register in ("wikidata.org", "openstreetmap.org", "wikipedia.org"):
        assert venue_register not in url, f"{row['id']} cites a venue record, not a listing"


@pytest.mark.parametrize("row", VERIFIED, ids=ids(VERIFIED))
def test_row_records_when_it_was_checked(row):
    """A listing can be cancelled or moved after it was read, so a row without
    a checked-at date is not verifiable, only asserted.

    `checked_at` is a column of its own rather than a second job for `as_of`,
    because the two answer different questions: `as_of` is the provenance
    stamp printed beside an answer, and `checked_at` is what the freshness
    policy is computed from. They happen to be equal in this file today, and
    they stop being equal the moment an operator patches `checked_at` after
    re-opening a listing page."""
    checked = datetime.fromisoformat(row["checked_at"])
    assert checked.tzinfo is not None
    assert datetime.fromisoformat(row["as_of"]).tzinfo is not None


@pytest.mark.parametrize("row", VERIFIED, ids=ids(VERIFIED))
def test_no_row_carries_a_hand_written_expiry(row):
    """The seed states when it was checked and nothing about how long that
    lasts. Letting a row ship its own `valid_until` would let one listing opt
    out of the recheck window, which is the single thing this policy exists to
    stop."""
    assert "valid_until" not in row


@pytest.mark.parametrize("row", VERIFIED, ids=ids(VERIFIED))
def test_row_uses_a_category_the_vocabulary_can_reach(row):
    """A category nobody can ask for is a row nobody can retrieve.

    `data/event_types.yml` is the words-to-category map the router matches on,
    and a row filed under a category missing from it is invisible to every
    question: that is how the one comedy listing in Reykjavik was unreachable
    until `comedy` was added, and it is why the Suzanne Dellal dance listings
    got a `dance` category rather than being filed as theatre."""
    assert row["category"] in EVENT_TYPES


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


def test_every_city_has_a_verified_listing_inside_the_forecast_window():
    """Coverage that lands outside the forecast window demonstrates nothing.

    The trip planner joins events to scored days, so a city whose only checked
    listing falls after the last day of the weather snapshot is, for every
    question a reviewer can actually ask, a city with no events. This is the
    assertion F9 was really about: not the row count, but whether the rows are
    reachable from the dates the system can answer for."""
    window = sorted(
        json.loads(line)["forecast_date"]
        for line in (ROOT / "data/snapshot/weather.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    first, last = window[0], window[-1]
    in_window: dict[str, int] = {}
    for row in VERIFIED:
        if first <= row["starts_at"][:10] <= last:
            in_window[row["city_id"]] = in_window.get(row["city_id"], 0) + 1
    missing = sorted(set(CITIES) - set(in_window))
    assert not missing, f"no verified event between {first} and {last} for {missing}"


def test_the_coverage_shape_is_reported_rather_than_averaged():
    """The manifest states the spread, so thin coverage cannot hide in a total.

    26 events across five cities read as coverage right up until you saw that
    eleven of them were in London and one was in Tel Aviv. The per-city counts
    are derived from the shipped file by `scripts/snapshot_manifest.py`, so
    this asserts that the derived numbers and the file still agree -- which is
    what lets the README and the UI quote a distribution instead of a
    reassuring single number."""
    manifest = json.loads((ROOT / "data/snapshot/MANIFEST.json").read_text(encoding="utf-8"))
    by_city = manifest["entities"]["events"]["by_city"]
    counted: dict[str, int] = {}
    for row in VERIFIED:
        counted[row["city_id"]] = counted.get(row["city_id"], 0) + 1
    assert by_city == counted
    assert set(by_city) == set(CITIES)


def test_the_weakest_city_is_named_rather_than_smoothed_over():
    """A guard against quietly claiming coverage this feed does not have.

    Hand-verification reaches as far as somebody's afternoon reaches, and the
    honest form of that is a number a reader can see. If the thinnest city ever
    creeps back to a single listing, the README's claim that all five cities
    are usable stops being true and this test is what says so."""
    counted: dict[str, int] = {}
    for row in VERIFIED:
        counted[row["city_id"]] = counted.get(row["city_id"], 0) + 1
    weakest = min(counted.values())
    assert weakest >= 2, f"only {weakest} verified listing(s) in {min(counted, key=counted.get)}"
