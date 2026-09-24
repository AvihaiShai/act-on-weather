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

from datetime import UTC, datetime, timedelta

import pytest

from services.agent import grounding
from services.common import config, queries, schemas
from services.common.rabbit import Poison
from services.consumer import main as consumer

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


def test_the_window_is_configurable_and_read_from_the_environment(monkeypatch):
    """The window is an operational decision, not a constant. A deployment that
    re-checks its listings weekly should be able to say so without editing the
    data, and the derived column then moves for every row at once."""
    monkeypatch.setattr(config, "EVENT_RECHECK_DAYS", 3)
    checked = datetime(2026, 9, 24, 18, 0, tzinfo=UTC)
    assert config.event_valid_until(checked) == datetime(2026, 9, 27, 18, 0, tzinfo=UTC)


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
    conn = RecordingConn(one={"expired": 4, "last_checked": "2026-09-24"})
    result = queries.expired_events(
        conn,
        "tel-aviv",
        start=datetime(2026, 10, 20).date(),
        end=datetime(2026, 10, 26).date(),
        categories=["concert"],
    )
    assert "NOT is_current" in conn.sql
    assert conn.params["city"] == "tel-aviv"
    assert conn.params["categories"] == ["concert"]
    assert result["expired"] == 4


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
