"""A half-expired snapshot must shrink the answer, not the truth about it.

The coverage gate in `router.retrieve` refuses a question whose whole window
is outside the stored forecast, and `test_router.py` pins that. This file
pins the case in between, which is the *normal* state of an air-gapped stack:
the snapshot was staged on Monday, it is now Thursday, and "what can I do this
week in Rome?" asks about days that still have rows and days that never will.

Before this was fixed the retrieval clipped its queries to the covered days
but kept the full window everywhere the reader and the validator look:

  * no gap sentence said the other days were missing, and
  * `Brief.allowed_dates()` still contained them, so a model sentence
    asserting weather on a day with no row passed `violations` clean.

That is hard rule 2 of the assignment -- a question outside the coverage gets
an explicit "no data for that date", never a guess -- failing in the one form
a reviewer is most likely to meet, because it needs no unusual question at
all, only a snapshot that is a few days old.

**Which days count as covered is decided by the rows that came back, not by
the coverage window.** `queries.coverage` reports a global MIN/MAX over
`weather_daily` across every city and `queries.in_coverage` never sees the
city, so the window says "covered" for a day this city has no row for whenever
one city was ingested further ahead than another, or a single day failed. The
missing days are therefore not always a tail: they can lead, straddle both
ends, or sit in the middle of the window. Each shape is tested below, because
each one produces a different true sentence.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from services.agent import dates, grounding, router

TODAY = date(2026, 9, 25)
WEEK = [f"2026-09-{d}" for d in range(25, 31)] + ["2026-10-01"]
QUESTION = "What can I do this week in Rome?"

ROME = {
    "id": "rome",
    "name": "Rome",
    "country": "Italy",
    "timezone": "Europe/Rome",
    "aliases": ["rome", "roma"],
    "coastal": False,
}


def _forecast_row(day: date):
    return {
        "forecast_date": day,
        "temp_min_c": 14.0,
        "temp_max_c": 24.0,
        "precip_mm": 0.0,
        "precip_prob": 5,
        "wind_kmh": 9.0,
        "uv_index": 5.0,
        "sunshine_hours": 9.0,
        "sunrise": "06:58",
        "sunset": "19:07",
        "weather_code": 1,
        "source_url": "https://open-meteo.com/",
        "as_of": "2026-09-22T06:00:00+00:00",
        "provider": "open-meteo",
    }


def _recommendation(day: date):
    return {
        "forecast_date": day,
        "activity": "sightseeing",
        "activity_label": "Sightseeing",
        "score": 82,
        "band": "good",
        "text": None,
        "status": "ready",
    }


@pytest.fixture
def ask(monkeypatch):
    """Ask Rome a question against an exactly specified set of stored days.

    `stored` is the days `weather_daily` holds rows for. `first`/`last` are the
    *global* coverage bounds `queries.coverage` reports, which default to the
    span of the stored days but can be set wider -- that is what reproduces a
    day inside the advertised window with no row behind it.
    """

    def _ask(stored, *, first=None, last=None, question=QUESTION):
        stored = sorted(stored)
        coverage = {
            "cities": [ROME],
            "entities": [{"entity": "places", "as_of": "2026-09-22T06:00:00+00:00"}],
            "weather_first_date": first or stored[0],
            "weather_last_date": last or stored[-1],
            "weather_as_of": "2026-09-22T06:00:00+00:00",
        }
        kept = set(stored)

        monkeypatch.setattr(dates, "today_in", lambda _tz: TODAY)
        monkeypatch.setattr(router.queries, "cities", lambda _conn: [ROME])
        monkeypatch.setattr(router.queries, "coverage", lambda _conn: coverage)
        monkeypatch.setattr(router.queries, "events", lambda *_a, **_k: [])
        monkeypatch.setattr(
            router.queries,
            "expired_events",
            lambda *_a, **_k: {"expired": 0, "last_checked": None},
        )
        monkeypatch.setattr(router.queries, "facts", lambda *_a, **_k: [])
        monkeypatch.setattr(router.queries, "places", lambda *_a, **_k: [])

        def _span(start, end):
            return [start + timedelta(days=i) for i in range((end - start).days + 1)]

        # A database returns the rows it has and no others. A fixture that
        # answered for a day it was told it does not hold would be testing a
        # database that cannot exist.
        def forecast(_conn, _city, *, start, end):
            return [_forecast_row(d) for d in _span(start, end) if d.isoformat() in kept]

        def recommendations(_conn, _city, *, start=None, end=None, activity=None):
            return [_recommendation(d) for d in _span(start, end) if d.isoformat() in kept]

        monkeypatch.setattr(router.queries, "forecast", forecast)
        monkeypatch.setattr(router.queries, "recommendations", recommendations)

        result = router.Router(object()).retrieve(question)
        return result, grounding.build(result)

    return _ask


def _gap(brief):
    texts = [g.text for g in brief.gaps if g.subject == "coverage:partial"]
    assert texts, f"no coverage gap; gaps were {[g.subject for g in brief.gaps]}"
    return texts[0]


# ------------------------------------------------- the ordinary trailing gap --


def test_the_answer_states_which_days_it_has_no_data_for(ask):
    """The gap sentence is the "no data for that date" the brief requires."""
    _result, brief = ask(WEEK[:3])

    sentence = _gap(brief)
    assert "2026-09-28 to 2026-10-01" in sentence
    assert "The stored forecast ends on 2026-09-27" in sentence


def test_an_uncovered_day_is_not_a_date_the_answer_may_assert(ask):
    """The validator's allow-list must not include days with no rows.

    This is the assertion that keeps the fix honest: a gap sentence the model
    is free to contradict is decoration.
    """
    _result, brief = ask(WEEK[:3])

    allowed = brief.allowed_dates()
    assert set(WEEK[:3]) <= allowed
    assert not (set(WEEK[3:]) & allowed), f"uncovered days are still assertable: {allowed}"


def test_weather_claimed_for_an_uncovered_day_is_a_violation(ask):
    """The end-to-end property, through the real validator."""
    _result, brief = ask(WEEK[:3])

    invented = (
        "Rome looks pleasant all week. On 2026-10-01 you can go sightseeing under clear skies."
    )
    assert grounding.violations(invented, brief), "an invented day passed the grounding check"

    grounded = "On 2026-09-26 sightseeing scores 82 out of 100, which is good."
    assert grounding.violations(grounded, brief) == []


def test_the_rendered_answer_carries_the_limit_next_to_the_heading(ask):
    """What the reader sees must account for every day the heading claims.

    The heading deliberately still states the asked window -- that is the
    question, and shrinking it would misreport what was asked. What it may not
    do is state a week and then show three days with nothing saying why.
    """
    _result, brief = ask(WEEK[:3])
    rendered = grounding.render(brief)

    assert brief.window.startswith("2026-09-25")
    day_lines = [line for line in rendered.splitlines() if line.startswith("- 2026-")]
    assert len(day_lines) == 6, "one forecast and one verdict line per covered day"
    assert "No weather is stored for 2026-09-28 to 2026-10-01" in rendered


def test_the_model_is_told_which_days_it_may_not_describe(ask):
    """The prompt has to carry the gap, not only the validator.

    Catching an invented day after the fact costs the whole model answer: the
    agent falls back to `render`. Telling the model up front is what keeps the
    good answer the common case.
    """
    _result, brief = ask(WEEK[:3])
    prompt = grounding.prompt_block(brief)

    assert "DATES WITH NO STORED WEATHER" in prompt
    assert "2026-10-01" in prompt.split("DATES WITH NO STORED WEATHER")[1]


def test_a_fully_covered_week_gains_no_gap_and_loses_no_day(ask):
    """The control. A snapshot that reaches the end of the week is unchanged.

    Without this, every assertion above would pass by simply always
    truncating -- including when there is nothing to truncate.
    """
    _result, brief = ask(WEEK)

    assert [g for g in brief.gaps if g.subject == "coverage:partial"] == []
    assert set(WEEK) <= brief.allowed_dates()


# ------------------------------------------- gaps that are not at the end --


def test_a_leading_gap_does_not_claim_the_forecast_ended(ask):
    """Days missing at the *start* have the opposite explanation.

    "The stored forecast ends on 2026-10-01" would be both false and useless
    here: the forecast reaches the end of the week perfectly well, it simply
    does not go back far enough.
    """
    _result, brief = ask(WEEK[2:], first="2026-09-27", last="2026-10-01")

    sentence = _gap(brief)
    assert "2026-09-25 to 2026-09-26" in sentence
    assert "The stored forecast begins on 2026-09-27" in sentence
    assert "ends on" not in sentence
    assert not (set(WEEK[:2]) & brief.allowed_dates())


def test_a_gap_at_both_ends_names_both_and_claims_neither_cause(ask):
    """Two runs, and an explanation that fits both.

    A single first-to-last span here would read "2026-09-25 to 2026-10-01" --
    every day of the week, including the four that are stored.
    """
    _result, brief = ask(WEEK[2:5], first="2026-09-27", last="2026-09-29")

    sentence = _gap(brief)
    assert "2026-09-25 to 2026-09-26, 2026-09-30 to 2026-10-01" in sentence
    assert "covers 2026-09-27 to 2026-09-29" in sentence
    assert "begins on" not in sentence and "ends on" not in sentence
    assert set(WEEK[2:5]) == brief.allowed_dates() & set(WEEK)


def test_a_missing_day_inside_the_window_is_found_and_named(ask):
    """The case the coverage window cannot see at all.

    `weather_first_date`/`weather_last_date` are a global MIN/MAX over every
    city, so a day inside them proves nothing about this one. Here the bounds
    span the whole week and 2026-09-27 simply has no Rome row -- one failed
    day in an otherwise healthy ingest.
    """
    stored = [d for d in WEEK if d != "2026-09-27"]
    _result, brief = ask(stored, first="2026-09-25", last="2026-10-01")

    sentence = _gap(brief)
    assert "No weather is stored for 2026-09-27 in Rome" in sentence
    assert "has no row for Rome on them" in sentence
    assert "2026-09-27" not in brief.allowed_dates()
    assert set(stored) <= brief.allowed_dates()

    invented = "On 2026-09-27 Rome is sunny, so plan the Forum then."
    assert grounding.violations(invented, brief), "an internal missing day passed the check"


def test_two_separate_internal_gaps_are_listed_separately(ask):
    """Non-consecutive missing days must not be collapsed into one range."""
    stored = [d for d in WEEK if d not in {"2026-09-26", "2026-09-29"}]
    _result, brief = ask(stored, first="2026-09-25", last="2026-10-01")

    assert "2026-09-26, 2026-09-29" in _gap(brief)


def test_the_city_is_not_covered_merely_because_another_city_is(ask):
    """No row for this city at all, inside an advertised window.

    The global coverage bounds span the whole week -- because some other city
    was ingested -- so the refusal branch does not fire. The answer must still
    not be free to describe a single day.
    """
    _result, brief = ask([], first="2026-09-25", last="2026-10-01")

    assert brief.allowed_dates() & set(WEEK) == set()
    assert "No weather is stored for 2026-09-25 to 2026-10-01" in _gap(brief)

    invented = "On 2026-09-26 Rome is warm and dry, so plan outdoor time."
    assert grounding.violations(invented, brief), "a week with no rows was described anyway"
    # The same claim with no date in it -- "Rome is warm and dry this week" --
    # used to pass here, because every check above needs a date to test. That
    # is check 4b's job now, and tests/unit/test_undated_weather.py owns it.


# --------------------------------------------------------- date grouping --


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (["2026-09-27"], "2026-09-27"),
        (["2026-09-27", "2026-09-28"], "2026-09-27 to 2026-09-28"),
        (["2026-09-27", "2026-09-29"], "2026-09-27, 2026-09-29"),
        (["2026-09-30", "2026-10-01"], "2026-09-30 to 2026-10-01"),
        (["2026-10-01", "2026-09-25"], "2026-09-25, 2026-10-01"),
    ],
)
def test_consecutive_dates_collapse_and_others_do_not(days, expected):
    """Including across a month boundary, which string comparison gets wrong."""
    assert grounding._date_runs(days) == expected
