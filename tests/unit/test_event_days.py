"""F2: the day an event is on, from the query to the answer to the itinerary.

The reproduction, from the review: the Laver Cup is stored as
``starts_at = 2026-09-25T00:00:00+01:00``, which the API serialised as
``2026-09-24T23:00:00Z``. Asked "Are there any sports events tomorrow in
London?" on 2026-09-24, the agent answered that none were on; the itinerary
put the tournament on the 24th, and showed it on none of the other two days it
actually runs.

The SQL half of the fix -- deriving the local dates through the city's IANA
zone -- is proved against a real Postgres in
``tests/integration/event_local_days.py``. What is proved here is everything
downstream of it: the grouping, the rendering, the validator's idea of which
dates are supported, and the two example questions.

Every date is written out and the clock is frozen, so these keep meaning the
same thing after the staged forecast window has expired.
"""

from datetime import UTC, date, datetime

import pytest

from services.agent import grounding, router
from services.agent import main as agent_main
from services.common import queries

# The review's dates. "Today" is the 24th; the tournament runs the 25th to the
# 27th; the 28th is the day after it ends.
TODAY = date(2026, 9, 24)
START = date(2026, 9, 25)
MIDDLE = date(2026, 9, 26)
END = date(2026, 9, 27)
AFTER = date(2026, 9, 28)

LONDON = {
    "id": "london",
    "name": "London",
    "country": "United Kingdom",
    "timezone": "Europe/London",
    "aliases": [],
    "coastal": False,
}

# The stored row as `queries.events` returns it: the UTC instant untouched, the
# local dates derived beside it. 2026-09-24T23:00Z *is* 2026-09-25T00:00+01:00.
LAVER_CUP = {
    "id": "theo2:laver-cup-2026",
    "city_id": "london",
    "title": "Laver Cup 2026",
    "category": "sport",
    "venue": "The O2 arena",
    "starts_at": datetime(2026, 9, 24, 23, 0, tzinfo=UTC),
    "ends_at": datetime(2026, 9, 27, 22, 59, tzinfo=UTC),
    "timezone": "Europe/London",
    "starts_on": START,
    "ends_on": END,
    "source": "The O2 arena official event listing",
    "source_url": "https://www.theo2.co.uk/events/detail/laver-cup-2026",
    "is_sample": False,
    # The day the listing page was last opened, and the expiry derived from it
    # (migration 006). Both are on every row `queries.events` returns, and the
    # itinerary carries `checked_at` through onto each event line, so a fixture
    # without them is not the shape the code under test receives.
    "checked_at": datetime(2026, 9, 24, 18, 0, tzinfo=UTC),
    "valid_until": datetime(2026, 10, 15, 18, 0, tzinfo=UTC),
}


def one_day(event_id="theo2:the-strokes", day=START, title="The Strokes", category="concert"):
    return dict(
        LAVER_CUP,
        id=event_id,
        title=title,
        category=category,
        starts_on=day,
        ends_on=day,
        starts_at=datetime(day.year, day.month, day.day, 19, tzinfo=UTC),
        ends_at=None,
    )


# --------------------------------------------------- which days a row is on ----


def test_a_local_midnight_start_belongs_to_the_local_day_not_the_utc_one():
    """The exact row from the review. Its UTC date is the 24th; it is on the
    25th, because that is what the clock in London says when it starts."""
    assert LAVER_CUP["starts_at"].date() == TODAY  # what the old code read
    assert queries.event_days(LAVER_CUP)[0] == START


def test_a_multi_day_event_is_on_every_day_it_runs():
    """The system's definition of "on that day": a traveller planning the 26th
    of a tournament that runs the 25th to the 27th has to see it."""
    assert queries.event_days(LAVER_CUP) == [START, MIDDLE, END]


def test_a_single_day_event_is_on_exactly_one_day():
    assert queries.event_days(one_day()) == [START]


def test_a_row_whose_end_precedes_its_start_is_clamped_to_one_day():
    """Never an empty list: a row the system holds is on some day, and a bad
    `ends_at` must not make an event disappear from every query."""
    broken = dict(LAVER_CUP, starts_on=END, ends_on=START)
    assert queries.event_days(broken) == [END]


def test_a_row_with_no_end_day_falls_back_to_its_start():
    assert queries.event_days(dict(LAVER_CUP, ends_on=None)) == [START]


# --------------------------------------------------------- the agent's answer ----


def retrieve(question, events, today=TODAY, monkeypatch=None):
    """The real router, with only the SQL replaced.

    The fake `events` filters the way the real query does -- overlap on local
    dates -- so a test cannot pass against a filter the database would not
    apply.
    """
    coverage = {
        "cities": [LONDON],
        "weather_first_date": "2026-09-23",
        "weather_last_date": "2026-10-08",
        "weather_as_of": "2026-09-23",
    }

    def fake_events(_conn, _city, *, start=None, end=None, categories=None, **_kwargs):
        rows = [r for r in events if r["ends_on"] >= start and r["starts_on"] <= end]
        if categories:
            rows = [r for r in rows if r["category"] in categories]
        return rows

    monkeypatch.setattr(router.queries, "cities", lambda _conn: [LONDON])
    monkeypatch.setattr(router.queries, "coverage", lambda _conn: coverage)
    monkeypatch.setattr(router.queries, "events", fake_events)
    # Asked only when the retrieval above comes back empty, so a gap can
    # say whether the feed went stale or was never there.
    monkeypatch.setattr(
        router.queries, "expired_events", lambda *_a, **_k: {"expired": 0, "last_checked": None}
    )
    monkeypatch.setattr(router.queries, "places", lambda *_a, **_k: [])
    monkeypatch.setattr(router.queries, "forecast", lambda *_a, **_k: [])
    monkeypatch.setattr(router.queries, "recommendations", lambda *_a, **_k: [])
    # The frozen clock. "Tomorrow" is the 25th and stays the 25th.
    monkeypatch.setattr(router.dates, "today_in", lambda _tz: today)
    return router.Router(object()).retrieve(question)


QUESTION = "Are there any sports events tomorrow in London?"


def test_the_sports_question_from_the_review_finds_the_event_on_the_25th(monkeypatch):
    """The reproduced failure, asked word for word with the clock on the 24th.

    Before F2 this returned no rows, and the agent said none were on."""
    result = retrieve(QUESTION, [LAVER_CUP], monkeypatch=monkeypatch)

    assert str(result.resolution.window) == "2026-09-25"
    assert [r["id"] for r in result.events] == ["theo2:laver-cup-2026"]

    brief = grounding.build(result)
    assert [e.day for e in brief.events] == ["2026-09-25"]
    answer = grounding.render(brief)
    assert "Laver Cup 2026" in answer
    assert "2026-09-25" in answer
    # And no gap sentence claiming nothing is on.
    assert not any(g.subject.startswith("events") for g in brief.gaps), brief.gaps


def test_the_same_question_on_the_28th_still_reports_no_event(monkeypatch):
    """The fix widens the window on purpose; it must not widen it past the end.
    An event that has finished is not on "tomorrow"."""
    result = retrieve(QUESTION, [LAVER_CUP], today=AFTER, monkeypatch=monkeypatch)

    assert result.events == []
    brief = grounding.build(result)
    assert any(g.subject == "events:sport" for g in brief.gaps)
    assert "Laver Cup" not in grounding.render(brief)


def test_asked_about_the_middle_day_the_event_is_still_on(monkeypatch):
    result = retrieve(
        "Are there any sports events in London on 2026-09-26?",
        [LAVER_CUP],
        monkeypatch=monkeypatch,
    )
    assert [r["id"] for r in result.events] == ["theo2:laver-cup-2026"]
    assert "2026-09-26" in grounding.render(grounding.build(result))


def test_a_run_of_days_is_rendered_as_a_range_not_as_one_date(monkeypatch):
    """Three identical dated lines would read as three fixtures. One range
    reads as one tournament, which is what the row says."""
    result = retrieve("What is on in London this week?", [LAVER_CUP], monkeypatch=monkeypatch)
    fact = grounding.build(result).events[0]

    assert fact.when() == "2026-09-25 to 2026-09-27"
    assert grounding.build(result).events[0].days() == ["2026-09-25", "2026-09-26", "2026-09-27"]
    assert "2026-09-25 to 2026-09-27" in grounding.render(grounding.build(result))
    assert "2026-09-25 to 2026-09-27" in grounding.prompt_block(grounding.build(result))


def test_a_one_day_event_is_still_rendered_as_a_bare_date(monkeypatch):
    result = retrieve("What is on in London this week?", [one_day()], monkeypatch=monkeypatch)
    fact = grounding.build(result).events[0]

    assert fact.when() == "2026-09-25"
    assert "2026-09-25 to" not in grounding.render(grounding.build(result))


def test_every_day_of_a_run_is_a_date_the_answer_may_name(monkeypatch):
    """The validator rejects a date no row carries. The 26th of a tournament
    that runs the 25th to the 27th *is* carried by the row, and saying so must
    not be thrown away as an invention.

    Asked about the 25th alone, so the question's own window is one day and the
    26th and 27th can only be allowed by the event's run.
    """
    brief = grounding.build(
        retrieve(
            "Are there any sports events in London on 2026-09-25?",
            [LAVER_CUP],
            monkeypatch=monkeypatch,
        )
    )

    assert brief.window_days == ["2026-09-25"]
    assert {"2026-09-26", "2026-09-27"} <= brief.allowed_dates()
    assert "2026-09-28" not in brief.allowed_dates()
    assert grounding.violations("The Laver Cup 2026 runs on 2026-09-26.", brief) == []
    assert grounding.violations("The Laver Cup 2026 is on 2026-09-28.", brief)


# ------------------------------------------------------------ the itinerary ----


@pytest.fixture
def itinerary(monkeypatch):
    """`POST /itinerary` with the database replaced and the clock frozen.

    The weather rows are written out rather than fetched, so this keeps passing
    after the staged forecast has expired -- the review's itinerary bug was
    only ever about which day a row lands on.
    """
    days = [TODAY, START, MIDDLE, END, AFTER]
    coverage = {
        "cities": [LONDON],
        "weather_first_date": days[0].isoformat(),
        "weather_last_date": days[-1].isoformat(),
        "weather_as_of": "2026-09-23T06:00:00+00:00",
        "entities": [],
        "by_city": [],
    }
    scores = [
        {
            "city_id": "london",
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
            "weather_as_of": None,
            "updated_at": None,
        }
        for day in days
    ]

    def fake_events(_conn, _city, *, start=None, end=None, **_kwargs):
        return [r for r in stored["events"] if r["ends_on"] >= start and r["starts_on"] <= end]

    stored = {"events": [LAVER_CUP]}
    monkeypatch.setattr(agent_main.queries, "cities", lambda _conn: [LONDON])
    monkeypatch.setattr(agent_main.queries, "coverage", lambda _conn: coverage)
    monkeypatch.setattr(agent_main.queries, "recommendations", lambda *_a, **_k: scores)
    monkeypatch.setattr(agent_main.queries, "places", lambda *_a, **_k: [])
    monkeypatch.setattr(agent_main.queries, "events", fake_events)
    # Asked only when the retrieval above comes back empty, so a gap can
    # say whether the feed went stale or was never there.
    monkeypatch.setattr(
        agent_main.queries, "expired_events", lambda *_a, **_k: {"expired": 0, "last_checked": None}
    )
    monkeypatch.setattr(agent_main.dates, "today_in", lambda _tz: TODAY)
    monkeypatch.setattr(type(agent_main.pool), "conn", property(lambda _self: object()))

    def build(events=None, start=days[0], end=days[-1]):
        if events is not None:
            stored["events"] = events
        plan = agent_main.build_itinerary(
            agent_main.ItineraryIn(city="london", start_date=start, end_date=end)
        )
        return {d["date"]: [e["id"] for e in d["events"]] for d in plan["days"]}, plan

    return build


def test_the_itinerary_puts_the_event_on_its_local_days(itinerary):
    """The review's itinerary bug: the Laver Cup appeared on the 24th, its UTC
    date, and on none of the three days it runs."""
    by_day, _ = itinerary()

    assert by_day["2026-09-24"] == []
    assert by_day["2026-09-25"] == ["theo2:laver-cup-2026"]
    assert by_day["2026-09-26"] == ["theo2:laver-cup-2026"]
    assert by_day["2026-09-27"] == ["theo2:laver-cup-2026"]
    assert by_day["2026-09-28"] == []


def test_the_itinerary_numbers_the_days_of_a_run(itinerary):
    """So three repeated lines read as one tournament. The UI prints this."""
    _, plan = itinerary()
    runs = {d["date"]: d["events"][0] for d in plan["days"] if d["events"]}

    assert [(e["day_index"], e["day_count"]) for e in runs.values()] == [(1, 3), (2, 3), (3, 3)]
    assert {e["starts_on"] for e in runs.values()} == {"2026-09-25"}
    assert {e["ends_on"] for e in runs.values()} == {"2026-09-27"}


def test_the_itinerary_marks_a_one_day_event_as_a_single_day(itinerary):
    _, plan = itinerary([one_day()])
    [event] = [e for d in plan["days"] for e in d["events"]]

    assert (event["day_index"], event["day_count"]) == (1, 1)
    assert event["starts_on"] == event["ends_on"] == "2026-09-25"


def test_a_run_that_starts_before_the_itinerary_shows_on_its_covered_days(itinerary):
    """Asked for the 26th alone, a tournament that opened on the 25th is still
    on -- and it is labelled day 2, not day 1."""
    by_day, plan = itinerary(start=MIDDLE, end=MIDDLE)

    assert by_day == {"2026-09-26": ["theo2:laver-cup-2026"]}
    assert plan["days"][0]["events"][0]["day_index"] == 2
