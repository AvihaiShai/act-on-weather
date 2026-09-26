"""The planner plans the days the selected city actually has rows for.

`queries.coverage` reports MIN/MAX over `weather_daily` with no city predicate
(see `COVERAGE_SQL`), and `queries.in_coverage` never sees a city. Building the
day list from that window offered days the chosen city has no forecast for:

  * the day was rendered by the UI as "no scored activity" under a poor-band
    score pill -- a data gap shown as a verdict on the weather;
  * `requested_days_outside_coverage` came back empty, so the UI's "left out
    rather than guessed" warning never fired;
  * the title and `end_date` counted the day, so a saved itinerary recorded a
    range nothing had been scored across.

It is reachable whenever one city was ingested further ahead than another, when
a single day failed to ingest, and after a partial refresh -- the ordinary
state, not the odd one. Every date is written out and nothing is derived from
the clock, so these keep meaning the same thing after the staged window has
expired.
"""

from datetime import date

import pytest
from fastapi import HTTPException

from services.agent import main as agent_main

LISBON = {
    "id": "lisbon",
    "name": "Lisbon",
    "country": "Portugal",
    "timezone": "Europe/Lisbon",
    "aliases": [],
    "coastal": True,
    "coast_name": "Carcavelos",
    "coast_lat": 38.68,
    "coast_lon": -9.33,
    "coast_distance_km": 18.0,
}

WINDOW = [date(2026, 9, 24) for _ in range(0)] + [
    date(2026, 9, 24),
    date(2026, 9, 25),
    date(2026, 9, 26),
    date(2026, 9, 27),
    date(2026, 9, 28),
]
AS_OF = "2026-09-23T06:00:00+00:00"

# The global window runs the whole five days because *another* city reaches the
# 28th. Lisbon does not, and that is the whole point of the fixture.
COVERAGE = {
    "cities": [LISBON],
    "weather_first_date": WINDOW[0].isoformat(),
    "weather_last_date": WINDOW[-1].isoformat(),
    "weather_as_of": AS_OF,
    "entities": [],
    "by_city": [],
}


def _score(day):
    return {
        "city_id": "lisbon",
        "forecast_date": day,
        "activity": "museum_day",
        "activity_label": "Museum day",
        "requested": False,
        "score": 70,
        "band": "good",
        "reasons": [],
        "rule_version": "1",
        "status": "ready",
        "text": None,
        "model": None,
        "last_error": None,
        "weather_as_of": AS_OF,
        "updated_at": None,
    }


@pytest.fixture
def build(monkeypatch):
    """`POST /itinerary` with the database replaced and the clock frozen.

    `city_days` is what Lisbon has a `weather_daily` row for; the recommendation
    rows follow it, because the consumer scores a day only when it stored one.
    """

    def _build(city_days, start=WINDOW[0], end=WINDOW[-1], stamps=None):
        monkeypatch.setattr(agent_main.queries, "cities", lambda _conn: [LISBON])
        monkeypatch.setattr(agent_main.queries, "coverage", lambda _conn: COVERAGE)
        monkeypatch.setattr(
            agent_main.queries,
            "forecast",
            lambda *_a, **_k: [
                {"forecast_date": d, "as_of": (stamps or {}).get(d, AS_OF)} for d in city_days
            ],
        )
        monkeypatch.setattr(
            agent_main.queries, "recommendations", lambda *_a, **_k: [_score(d) for d in city_days]
        )
        monkeypatch.setattr(agent_main.queries, "places", lambda *_a, **_k: [])
        monkeypatch.setattr(agent_main.queries, "events", lambda *_a, **_k: [])
        monkeypatch.setattr(agent_main.dates, "today_in", lambda _tz: WINDOW[0])
        monkeypatch.setattr(type(agent_main.pool), "conn", property(lambda _s: object()))
        return agent_main.build_itinerary(
            agent_main.ItineraryIn(city="lisbon", start_date=start, end_date=end)
        )

    return _build


def test_a_day_this_city_has_no_row_for_is_not_offered(build):
    """The defect, stated as the rule it broke: a plan is built from rows.

    Before the fix the 27th and the 28th were in `days` with a null activity,
    which the UI renders as "no scored activity" beside a poor-band pill.
    """
    plan = build(WINDOW[:3])

    assert [day["date"] for day in plan["days"]] == ["2026-09-24", "2026-09-25", "2026-09-26"]
    assert all(day["activity"] is not None for day in plan["days"])


def test_the_missing_days_are_stated_as_a_gap(build):
    """Omitted is not enough; the traveller asked about those days.

    `requested_days_outside_coverage` is the field the UI already renders as
    "No stored weather for: ... Those days are left out rather than guessed."
    It was empty in exactly the case it exists for.
    """
    plan = build(WINDOW[:3])

    assert plan["requested_days_outside_coverage"] == ["2026-09-27", "2026-09-28"]


def test_a_day_missing_from_the_middle_of_the_window_is_a_gap_too(build):
    """A day that failed to ingest sits *inside* the window.

    The global MIN/MAX cannot see it at all, so this is the case no
    first-to-last comparison could ever have caught.
    """
    plan = build([WINDOW[0], WINDOW[1], WINDOW[3], WINDOW[4]])

    assert "2026-09-26" not in [day["date"] for day in plan["days"]]
    assert plan["requested_days_outside_coverage"] == ["2026-09-26"]


def test_the_title_and_the_range_count_only_the_days_that_were_planned(build):
    """A saved itinerary records the range it was scored across, not the one
    that was asked for: `end_date` travels to `POST /itineraries`."""
    plan = build(WINDOW[:3])

    assert plan["title"] == "3 days in Lisbon"
    assert plan["start_date"] == "2026-09-24"
    assert plan["end_date"] == "2026-09-26"


def test_a_city_with_no_rows_in_the_window_is_refused_and_named(build):
    """Not "5 days in Lisbon" with five empty days.

    The global window says these dates are covered, so the old refusal branch
    was unreachable for a city that holds nothing in it.
    """
    with pytest.raises(HTTPException) as raised:
        build([])

    assert raised.value.status_code == 422
    assert "Lisbon" in raised.value.detail


def test_the_global_coverage_window_does_not_decide_which_days_are_planned(build):
    """Pins the mechanism, not only the outcome.

    `queries.in_coverage` compares against a MIN/MAX taken across every city.
    Re-introducing it here would restore the defect while every assertion above
    that happened to use an edge gap still passed.
    """

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the planner must not decide days from the global window")

    original = agent_main.queries.in_coverage
    agent_main.queries.in_coverage = forbidden
    try:
        plan = build(WINDOW[:3])
    finally:
        agent_main.queries.in_coverage = original

    assert len(plan["days"]) == 3


def test_a_fully_covered_request_is_unchanged(build):
    """The control: nothing about the ordinary case moves."""
    plan = build(WINDOW)

    assert [day["date"] for day in plan["days"]] == [d.isoformat() for d in WINDOW]
    assert plan["requested_days_outside_coverage"] == []
    assert plan["title"] == "5 days in Lisbon"


def test_plan_provenance_comes_from_its_own_days(build):
    newer = "2026-09-24T06:00:00+00:00"
    plan = build(WINDOW[:2], stamps={WINDOW[1]: newer})

    assert plan["as_of"] == newer
    assert [day["weather_as_of"] for day in plan["days"]] == [AS_OF, newer]
