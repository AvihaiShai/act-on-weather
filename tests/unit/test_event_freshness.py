"""The freshness policy for event listings (F9).

A row in `events` is not an observation, it is somebody's note of a web page,
taken on a particular day. Nothing in an air-gapped run can find out that the
venue cancelled the show the following week. The policy that follows from that
is the subject of this file:

  * every row records `checked_at`, the day its listing page was last opened;
  * `valid_until` is derived from it by `config.event_valid_until`, once, by
    the producer -- never written by hand and never recomputed per reader;
  * a read returns only rows still inside that window, so nothing past its
    recheck date is presented as a currently scheduled event;
  * an operator can still see what fell out of the window, because "the feed
    went stale" and "there was never anything here" are different problems and
    only the first one is fixed by a connected refresh;
  * and when the filter is the reason an answer is empty, the answer says so.

These tests use a recording stand-in for the connection rather than a database,
because what is being asserted is the shape of the statement the query builds --
which filter is applied by default, and which is dropped on request. The SQL
itself runs against real Postgres in `tests/integration/`.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

import pytest

from services.agent import grounding
from services.common import config, queries, schemas
from services.common.rabbit import Poison
from services.consumer import main as consumer
from services.ingestor import main as ingestor

# ------------------------------------------------------------ stand-ins ----


class RecordingConn:
    """Captures the statement and parameters a query builds.

    It returns whatever `rows` it was given, so a caller that goes on to shape
    the result still gets something to shape; the point of the object is the
    statement it kept.
    """

    def __init__(self, rows: list[dict] | None = None, one: dict | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.one = one
        self.sql = ""
        self.params: dict = {}

    def execute(self, sql, params=None):  # noqa: ANN001 - a stand-in for psycopg
        self.sql = " ".join(sql.split())
        self.params = dict(params or {})
        return self

    def fetchall(self) -> list[dict]:
        return self.rows

    def fetchone(self) -> dict | None:
        return self.one


# ----------------------------------------------------- deriving the expiry --


def test_the_expiry_is_the_configured_window_after_the_check():
    """One function owns the arithmetic, so the ingestor and the consumer
    cannot drift apart on what "still current" means."""
    checked = datetime(2026, 9, 24, 18, 0, tzinfo=UTC)
    assert config.event_valid_until(checked) == checked + timedelta(days=config.EVENT_RECHECK_DAYS)


def test_the_window_is_read_from_the_environment(monkeypatch):
    """The window is an operational decision, not a constant.

    Asserted by re-importing the module with the variable set, rather than by
    setting the constant directly: setting the constant would pass even if
    `AOW_EVENT_RECHECK_DAYS` were mis-spelled or never read, which is the only
    way this setting can actually be got wrong.
    """
    monkeypatch.setenv("AOW_EVENT_RECHECK_DAYS", "3")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.EVENT_RECHECK_DAYS == 3
        checked = datetime(2026, 9, 24, 18, 0, tzinfo=UTC)
        assert reloaded.event_valid_until(checked) == datetime(2026, 9, 27, 18, 0, tzinfo=UTC)
    finally:
        # Every other module holds a reference to this one, so leaving it
        # reloaded under a test-only value would leak into the rest of the run.
        monkeypatch.delenv("AOW_EVENT_RECHECK_DAYS")
        importlib.reload(config)
    assert config.EVENT_RECHECK_DAYS == 21


def test_the_running_window_overrides_the_one_baked_into_the_snapshot(monkeypatch):
    """A snapshot is committed once and replayed on every boot, possibly months
    later on a machine configured differently from the one that built it.

    `checked_at` is a fact and travels unchanged. `valid_until` is not: it is
    that fact plus an operational choice, so the value in the file is only the
    default the snapshot was built with, and the running stack's own window has
    to win. Without this the variable would take effect only at `make snapshot`
    time, which is not where an operator would look for it.
    """
    monkeypatch.setattr(config, "EVENT_RECHECK_DAYS", 7)
    row = {
        "id": "theo2:laver-cup-2026",
        "as_of": "2026-09-24T18:00:00+00:00",
        "checked_at": "2026-09-24T18:00:00+00:00",
        "valid_until": "2026-10-15T18:00:00+00:00",
    }
    applied = ingestor.apply_freshness_policy(config.RK_EVENT, row)
    assert applied["valid_until"] == "2026-10-01T18:00:00+00:00"
    assert applied["checked_at"] == row["checked_at"], "the recorded fact must not move"
    assert row["valid_until"] == "2026-10-15T18:00:00+00:00", "the caller's row was mutated"


def test_a_changed_window_is_a_new_delivery_and_an_unchanged_one_is_not():
    """The outbox deduplicates on `message_id`, so a derived value that is not
    part of the id can never reach the database.

    That is the trap this guards: lowering the recheck window would re-accept
    every row, mint the id it minted last time, and be silently discarded as a
    replay -- leaving the configured window and the stored window disagreeing
    with nothing to say so. The other half matters just as much: replaying the
    same snapshot under the same window must still deduplicate, because that is
    the property the delivery guarantee rests on.
    """
    row = {
        "id": "theo2:laver-cup-2026",
        "city_id": "london",
        "as_of": "2026-09-24T18:00:00+00:00",
        "checked_at": "2026-09-24T18:00:00+00:00",
        "valid_until": "2026-10-15T18:00:00+00:00",
    }

    def envelope_id(payload):
        [envelope] = ingestor.envelopes_from(config.RK_EVENT, [payload], "snapshot")
        return envelope.message_id

    as_shipped = envelope_id(row)
    assert as_shipped == envelope_id(dict(row)), "the same row minted two ids"
    shortened = envelope_id({**row, "valid_until": "2026-10-01T18:00:00+00:00"})
    assert shortened != as_shipped, "a changed expiry reused the id; the outbox would drop it"


def test_a_non_event_payload_is_passed_through_untouched():
    """Weather, places and facts have no recheck window, and a helper that
    quietly added a field to them would be writing a column that does not
    exist."""
    row = {"city_id": "rome", "forecast_date": "2026-09-25", "as_of": "2026-09-23T18:00:00+00:00"}
    assert ingestor.apply_freshness_policy(config.RK_WEATHER, row) == row


# ------------------------------------------------------------ the filter ----


def test_a_read_excludes_expired_listings_by_default():
    """The important default in the whole feature.

    Everything downstream of this call -- the agent's event answers, the
    itinerary, the API -- renders what it gets back as a schedule. So the
    filter is on unless a caller deliberately turns it off, rather than off
    unless a caller remembers to turn it on."""
    conn = RecordingConn()
    queries.events(conn, "london")
    assert "AND is_current" in conn.sql


def test_an_operator_can_ask_for_the_expired_rows():
    """The stale readings are still there, and an operator deciding whether to
    run a connected refresh needs to see them. `is_current` travels on every
    row, so the two are never confused once they are in the same list."""
    conn = RecordingConn()
    queries.events(conn, "london", include_expired=True)
    assert "AND is_current" not in conn.sql
    assert "is_current" in conn.sql, "the derived column is still selected"


def test_the_expired_count_is_scoped_to_the_same_question():
    """A gap sentence quoting a count from a different city or a different week
    would be worse than no sentence at all, so the follow-up query carries the
    same city, window and category filters as the read that came back empty."""
    conn = RecordingConn(
        rows=[
            {"category": "concert", "expired": 4, "last_checked": "2026-09-24"},
            {"category": "dance", "expired": 1, "last_checked": "2026-09-20"},
        ]
    )
    start, end = datetime(2026, 10, 20).date(), datetime(2026, 10, 26).date()
    result = queries.expired_events(
        conn, "tel-aviv", start=start, end=end, categories=["concert", "dance"]
    )
    assert "NOT is_current" in conn.sql
    assert conn.params["city"] == "tel-aviv"
    assert conn.params["categories"] == ["concert", "dance"]
    # The window matters most of the three: a count from a different week would
    # look exactly as plausible as the right one in the sentence it ends up in.
    assert conn.params["start"] == start
    assert conn.params["end"] == end
    assert "ends_on >= %(start)s::date" in conn.sql
    assert "starts_on <= %(end)s::date" in conn.sql
    assert result["expired"] == 5
    assert result["by_category"]["concert"]["expired"] == 4
    assert result["by_category"]["dance"]["expired"] == 1
    # The overall last-checked is the newest of them, because it answers "how
    # out of date is what I am holding" rather than "when was this one read".
    assert result["last_checked"] == "2026-09-24"


def test_the_coverage_window_is_measured_over_current_rows_only():
    """The advertised window has to be a window the system can answer within.

    An expired listing three weeks out would otherwise stretch the reported
    event coverage past the last date anything is actually returned for, which
    is the same kind of quiet overclaim the weather coverage window exists to
    prevent."""
    assert "WHERE e.valid_until > now()" in queries.COVERAGE_SQL


def test_coverage_counts_current_and_expired_rows_separately_per_city():
    """F9 was a finding about shape rather than about totals. A per-city split
    of current against expired is what lets the UI and the closure record say
    where the feed is thin without anybody re-counting the file."""
    for column in ("verified_events_current", "verified_events_expired"):
        assert column in queries.BY_CITY_SQL
    assert "valid_until > now()" in queries.EVENT_FRESHNESS_SQL
    assert "valid_until <= now()" in queries.EVENT_FRESHNESS_SQL


# ------------------------------------------------------- what it says ----


_TEL_AVIV = {
    "id": "tel-aviv",
    "name": "Tel Aviv",
    "country": "Israel",
    "timezone": "Asia/Jerusalem",
    "aliases": ["tel aviv", "tlv"],
    "coastal": True,
    "coast_name": "Gordon Beach",
    "coast_lat": 32.0836,
    "coast_lon": 34.7669,
    "coast_distance_km": 1.416,
}

# Wide enough that "this week" resolves inside it whatever day the suite runs.
_COVERAGE = {
    "cities": [_TEL_AVIV],
    "weather_first_date": "2020-01-01",
    "weather_last_date": "2099-12-31",
    "weather_as_of": "2026-09-24T18:00:00+00:00",
    "entities": [],
    "by_city": [],
}


class _Resolution:
    def __init__(self) -> None:
        self.intents = {"events"}
        self.city = {"id": "tel-aviv", "name": "Tel Aviv", "coastal": True}


class _Retrieval:
    def __init__(self, expired: dict) -> None:
        self.expired_events = expired
        self.resolution = _Resolution()


def test_no_stale_clause_when_nothing_was_filtered_out():
    """A sentence about expiry with no number behind it would be noise, and
    worse, it would imply a feed problem where the honest answer is simply that
    nothing was ever recorded."""
    assert grounding._stale_feed_sentence(_Retrieval({"expired": 0, "last_checked": None})) == ""
    assert grounding._stale_feed_sentence(_Retrieval({})) == ""


@pytest.mark.parametrize(
    ("expired", "expected"),
    [
        (1, "One stored listing"),
        (4, "4 stored listings"),
    ],
)
def test_the_stale_clause_names_the_count_and_the_check_date(expired, expected):
    """ "Nothing on record" invites a reader to conclude the city is quiet.
    "The listings we hold were last checked on the 24th and are past their
    recheck date" tells them the system is the limit, and what would fix it."""
    sentence = grounding._stale_feed_sentence(
        _Retrieval({"expired": expired, "last_checked": "2026-09-24T18:00:00+00:00"})
    )
    assert expected in sentence
    assert "2026-09-24" in sentence
    assert "recheck date" in sentence


def test_a_two_category_question_gets_a_count_per_category(monkeypatch):
    """The failure this is really guarding against, through the real `_gaps`.

    Asked about two kinds at once, the answer carries a gap sentence for each.
    A single total attached to both would quote the concert count in the dance
    sentence -- and for a category the city has never had a listing for, it
    would announce a stale listing that never existed, which is the "the feed
    is out of date" / "the city is quiet" confusion inverted. So the count is
    looked up per category, and this asserts it on the gaps the agent actually
    emits rather than on the sentence helper in isolation.
    """
    from services.agent import router

    monkeypatch.setenv("POSTGRES_READER_PASSWORD", "unit-test")
    monkeypatch.setattr(router.queries, "cities", lambda _conn: [_TEL_AVIV])
    monkeypatch.setattr(router.queries, "coverage", lambda _conn: _COVERAGE)
    monkeypatch.setattr(router.queries, "forecast", lambda *_a, **_k: [])
    monkeypatch.setattr(router.queries, "recommendations", lambda *_a, **_k: [])
    monkeypatch.setattr(router.queries, "places", lambda *_a, **_k: [])
    monkeypatch.setattr(router.queries, "facts", lambda *_a, **_k: [])
    monkeypatch.setattr(router.queries, "events", lambda *_a, **_k: [])
    monkeypatch.setattr(
        router.queries,
        "expired_events",
        lambda *_a, **_k: {
            "expired": 4,
            "last_checked": "2026-09-24T18:00:00+00:00",
            # Three concert listings have aged out. No theatre listing has ever
            # existed here, so `by_category` simply has no entry for it.
            "by_category": {
                "concert": {"expired": 3, "last_checked": "2026-09-24T18:00:00+00:00"},
                "comedy": {"expired": 1, "last_checked": "2026-09-20T18:00:00+00:00"},
            },
        },
    )

    result = router.Router(object()).retrieve(
        "are there any concerts or theatre in tel aviv this week?"
    )
    gaps = {gap.subject: gap.text for gap in grounding.build(result).gaps}

    assert "3 stored listings" in gaps["events:concert"]
    assert "2026-09-24" in gaps["events:concert"]
    # The one that matters: a category with nothing on record gets the plain
    # sentence, with no borrowed number and no invented stale listing.
    assert "recheck date" not in gaps["events:theatre"]
    assert "stored listing" not in gaps["events:theatre"]
    assert "No theatre performance is on record" in gaps["events:theatre"]


# ------------------------------------------------------- re-checking a row --


class _PatchCursor:
    """Records the UPDATE the consumer builds, and reports one row changed."""

    def __init__(self) -> None:
        self.sql = ""
        self.params: dict = {}

    def execute(self, sql, params=None):  # noqa: ANN001 - a stand-in for psycopg
        self.sql = " ".join(sql.split())
        self.params = dict(params or {})
        return self

    def fetchone(self) -> dict:
        return {"revision": 2}


def test_re_checking_a_listing_moves_its_expiry_with_it():
    """The only way to extend a row's life without re-ingesting the seed.

    Patching `checked_at` alone would be a no-op that looks like a fix: the row
    would carry a fresh check date and still be filtered out as stale. So the
    derived column moves with it, by the same policy the ingestor uses."""
    cursor = _PatchCursor()
    consumer.apply_patch(
        cursor,
        schemas.RecordPatch(
            entity="events",
            entity_id="theo2:laver-cup-2026",
            fields={"checked_at": "2026-10-01T09:00:00+00:00"},
        ),
    )
    assert "valid_until = %(valid_until)s" in cursor.sql
    assert cursor.params["valid_until"] == config.event_valid_until(
        datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    )


def test_the_expiry_cannot_be_set_by_hand():
    """A hand-set expiry would let one row opt out of the recheck window, which
    is the single thing the policy exists to stop. It is rejected as poison
    rather than ignored, because a correction the system silently discards is
    worse than one it refuses."""
    with pytest.raises(Poison):
        consumer.apply_patch(
            _PatchCursor(),
            schemas.RecordPatch(
                entity="events",
                entity_id="theo2:laver-cup-2026",
                fields={"valid_until": "2027-01-01T00:00:00+00:00"},
            ),
        )


def test_the_coverage_tab_reports_the_freshness_split_through_a_real_render(monkeypatch):
    """The numbers reach a reader, not just an API response.

    F9's point was that a single total hides the shape of the feed, so the
    counts are only worth computing if the page actually says them. This runs
    the real Streamlit app against a coverage payload carrying the new keys --
    the fixture is an older capture that predates them -- and asserts the
    caption names the current count, the expired count and the city spread.
    """
    from tests.unit.test_ui import FIXTURES, _open_page, _run

    coverage = {
        **FIXTURES["coverage"],
        "event_freshness": {
            "current": 31,
            "expired": 8,
            "samples": 45,
            "oldest_check": "2026-09-24T18:00:00Z",
            "next_expiry": "2026-10-15T18:00:00Z",
            "cities_covered": 4,
        },
        "by_city": [
            {**row, "verified_events_current": 6, "verified_events_expired": 2}
            for row in FIXTURES["coverage"]["by_city"]
        ],
    }

    app = _open_page(_run(monkeypatch, overrides={"coverage": coverage}), "data-coverage")
    assert not app.exception, [element.value for element in app.exception]
    captions = " ".join(element.value for element in app.caption)
    assert "31 checked listing(s) are still inside their recheck window" in captions
    assert "8 have fallen out of it" in captions
    # The spread, not just the total: four of the five cities are covered, and
    # the caption has to be the thing that says so.
    assert f"across 4 of {len(coverage['cities'])} cities" in captions


def test_a_patch_that_does_not_touch_the_check_date_leaves_the_expiry_alone():
    """Correcting a title is not a re-check. Only re-opening the listing page
    justifies extending the row's life, so every other patchable field leaves
    the expiry exactly where it was."""
    cursor = _PatchCursor()
    consumer.apply_patch(
        cursor,
        schemas.RecordPatch(
            entity="events",
            entity_id="theo2:laver-cup-2026",
            fields={"venue": "The O2 arena, North Greenwich"},
        ),
    )
    assert "valid_until" not in cursor.sql
