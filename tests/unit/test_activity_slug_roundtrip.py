"""One activity, one key, whichever route reached it.

`POST /recommendations` stores a row under `schemas.slugify(activity)`. The
agent extracts an activity-shaped noun from a question and looks it up under
`schemas.slugify(phrase)`. The two are the same function and were still not the
same rule, because the extraction additionally skipped leading filler words and
the write path did not:

    form "a picnic"          -> slugify -> a_picnic   (the row that was stored)
    question "...a picnic?"  -> candidate "picnic" -> picnic  (the row looked up)

So a traveller could have an activity scored through the UI's own form and then
be told by the agent, in the next breath, that it was not on record. The score
existed, under a key nothing would ever ask for again.

The fix is one list in one place: `schemas.ACTIVITY_FILLER_WORDS`, trimmed off
both ends by `slugify` and imported by the router's extraction as its own filler
list. This file is the round trip that proves the two agree -- not by reading
the source, but by putting a name in at the write path and asking a question
about it at the read path.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from services.agent import dates, router
from services.common import schemas

TODAY = date(2026, 10, 5)
STORED = [date(2026, 10, 5), date(2026, 10, 6)]

LISBON = {
    "id": "lisbon",
    "name": "Lisbon",
    "country": "Portugal",
    "timezone": "Europe/Lisbon",
    "aliases": [],
    "coastal": True,
}
COVERAGE = {
    "cities": [LISBON],
    "weather_first_date": "2026-09-23",
    "weather_last_date": "2026-10-08",
    "weather_as_of": "2026-09-23",
}

# Names a traveller types into the form, and that no catalogue keyword claims.
# Each is spelled the way the mismatch actually showed up: a leading article, a
# leading verb, and the bare noun that always worked.
ROUND_TRIP_NAMES = [
    "a picnic",
    "A Picnic",
    "picnic",
    "go stargazing",
    "stargazing",
    "the stargazing",
    "rock climbing",
    "hot air ballooning",
]


def _row(day: date, activity: str, label: str) -> dict[str, Any]:
    return {
        "forecast_date": day,
        "activity": activity,
        "activity_label": label,
        "score": 42,
        "band": "poor",
        "text": None,
        "status": "ready",
        "requested": True,
        "reasons": [],
    }


@pytest.fixture
def route(monkeypatch):
    """A Router whose recommendation table holds exactly the slugs a test puts
    in it, so a lookup under the wrong key comes back empty rather than
    accidentally matching something else."""
    monkeypatch.setattr(dates, "today_in", lambda _timezone: TODAY)
    monkeypatch.setattr(router.queries, "cities", lambda _conn: [LISBON])
    monkeypatch.setattr(router.queries, "coverage", lambda _conn: COVERAGE)
    for name in ("events", "facts", "places"):
        monkeypatch.setattr(router.queries, name, lambda *_a, **_k: [])
    monkeypatch.setattr(
        router.queries, "expired_events", lambda *_a, **_k: {"expired": 0, "last_checked": None}
    )
    monkeypatch.setattr(
        router.queries,
        "forecast",
        lambda *_a, **_k: [
            {
                "forecast_date": day,
                "provider": "open-meteo",
                "temp_max_c": 22.0,
                "temp_min_c": 14.0,
                "precip_mm": 0.0,
                "precip_prob": 0,
                "wind_kmh": 10.0,
                "sunshine_hours": 8.0,
                "as_of": "2026-09-23",
            }
            for day in STORED
        ],
    )

    stored: list[dict[str, Any]] = []

    def recommendations(_conn, _city=None, *, start=None, end=None, activity=None):
        return [row for row in stored if activity is None or row["activity"] == activity]

    monkeypatch.setattr(router.queries, "recommendations", recommendations)
    return router.Router(object()), stored


def write_path(name: str) -> str:
    """Exactly what `POST /recommendations` stores the row under.

    `services/api/main.py` calls `schemas.slugify(body.activity)` and publishes
    that as `activity`; the consumer writes it unchanged. Kept as a named
    function so this file says which line it is standing in for.
    """
    return schemas.slugify(name)


@pytest.mark.parametrize("name", ROUND_TRIP_NAMES)
def test_the_question_looks_up_the_key_the_form_stored(route, name):
    """The round trip, end to end and in that order: store under the write
    path's key, then ask the question and require the answer to come from that
    row."""
    routing, stored = route
    slug = write_path(name)
    stored.extend(_row(day, slug, name.strip()) for day in STORED)

    result = routing.retrieve(f"Is tomorrow a good day for {name} in Lisbon?")

    assert result.resolution.activities == [slug], (
        f"{name!r} stored as {slug!r} but the question resolved " f"{result.resolution.activities}"
    )
    assert result.resolution.unknown_activities == []
    assert [row["activity"] for row in result.recommendations] == [slug, slug]


@pytest.mark.parametrize("name", ROUND_TRIP_NAMES)
def test_an_unstored_name_is_a_gap_under_the_same_key(route, name):
    """The other half, and the one that makes the first half mean something. An
    empty table must report the gap under the SAME slug the form would have
    written -- otherwise the two halves could disagree and both still pass."""
    routing, _stored = route
    slug = write_path(name)

    result = routing.retrieve(f"Is tomorrow a good day for {name} in Lisbon?")

    assert result.resolution.unknown_activities == [slug], name
    assert result.resolution.activities == []


@pytest.mark.parametrize(
    ("typed", "slug"),
    [
        ("a picnic", "picnic"),
        ("A PICNIC", "picnic"),
        ("  the picnic  ", "picnic"),
        ("go stargazing", "stargazing"),
        ("Fine Dining!", "fine_dining"),
        # Filler inside a name is part of it: "watch the sunset" is one of the
        # catalogue's own keywords and must not collapse to "watch_sunset".
        ("watch the sunset", "watch_the_sunset"),
        # Only the ends are trimmed, and only whole words: "auto" is not "a".
        ("auto rickshaw tour", "auto_rickshaw_tour"),
    ],
)
def test_slugify_trims_filler_off_both_ends_and_nowhere_else(typed, slug):
    assert schemas.slugify(typed) == slug


@pytest.mark.parametrize("typed", ["a", "the", "a go", "  an  "])
def test_a_name_that_is_nothing_but_filler_is_refused(typed):
    """`POST /recommendations` turns this into a 422 rather than storing a row
    keyed on a stop word. Before the trim it stored `a`, `the` and `a_go` --
    three rows no question could ever reach."""
    with pytest.raises(ValueError):
        schemas.slugify(typed)


def test_the_two_filler_lists_are_one_object():
    """Not an equality check. The router holds a reference to the same frozenset
    the write path trims with, so there is no second list to fall out of step.
    """
    assert router.CANDIDATE_FILLER_WORDS is schemas.ACTIVITY_FILLER_WORDS


def test_no_catalogue_activity_key_is_reshaped_by_the_trim():
    """The bound on the change. `slugify` now drops words, so the guarantee
    worth asserting is that it drops none from the keys the catalogue already
    ships -- a renamed key would orphan every stored row under the old one."""
    catalogue = router.load_activity_keywords(router.config.DATA_DIR / "activities.yml")

    for key in catalogue:
        assert schemas.slugify(key.replace("_", " ")) == key, key


# --------------------------------- rows written before the trim existed ----
#
# The catalogue's own keys are safe, which is what the test above establishes.
# Rows a USER asked for are not: the slug is frozen at write time -- `slugify` is
# called in `services/api/main.py` and never in the consumer or in
# `services/common/queries.py` -- so an install that scored "a picnic" under the
# old rule holds a row keyed `a_picnic` for as long as the volume lives, and
# nothing re-keys it.
#
# This is a stated limitation, not a fix, and the two tests below are what makes
# it demonstrated rather than asserted. The README carries it in the
# known-limitations section with the operator remedy. A migration was considered
# and rejected: the only uniqueness on `recommendations` is
# `PRIMARY KEY (city_id, forecast_date, activity)` (db/migrations/001_init.sql),
# so `UPDATE ... SET activity = <trimmed>` collides wherever the same city and day
# already hold the trimmed key -- an install that scored both "a picnic" and
# "picnic" for Lisbon on one date. Resolving that collision means discarding one
# of two rows a user asked for, and a migration that runs on every boot is the
# worst place to make that choice silently.


def test_a_row_stored_under_the_old_key_is_not_found_by_the_question():
    """The limitation, demonstrated. Not a defect being pinned as correct: this
    is what an upgraded install looks like, written down so nobody has to
    rediscover it from a confused answer."""
    assert schemas.slugify("a picnic") == "picnic"
    # What the OLD write path stored, reproduced without depending on it: it
    # slugified the form field verbatim, with no trim.
    old_key = "_".join("a picnic".split())

    assert old_key == "a_picnic"
    assert old_key != schemas.slugify("a picnic")


def test_the_old_key_is_reported_as_a_gap_rather_than_answered_wrongly(route):
    """The half that decides whether this limitation is acceptable, and it is the
    reason a migration is not owed. The question does not find the row -- but it
    does not borrow another activity's number either: it says the score is not on
    record, under the key it looked for. A stale row makes the agent less
    informed, never wrong.

    The row itself is not lost. It stays visible on `GET /scores` and
    `GET /activities`, it is pickable by its label in the UI's Suitability view,
    and `POST /reenrich` still targets it by key -- so the operator remedy is to
    request the activity once more through the form, which writes it under the
    trimmed key.
    """
    routing, stored = route
    stored.extend(_row(day, "a_picnic", "a picnic") for day in STORED)

    result = routing.retrieve("Is tomorrow a good day for a picnic in Lisbon?")

    # Looked up under the new key, found nothing, and said so.
    assert result.resolution.unknown_activities == ["picnic"]
    assert result.resolution.activities == []
    # And the stale row was not served under a name nobody asked for.
    assert result.recommendations == []
