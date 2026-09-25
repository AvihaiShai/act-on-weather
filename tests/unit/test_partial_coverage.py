"""A half-expired snapshot must shrink the answer, not the truth about it.

The coverage gate in `router.retrieve` refuses a question whose whole window
is outside the stored forecast, and `test_router.py` pins that. This file
pins the case in between, which is the *normal* state of an air-gapped stack:
the snapshot was staged on Monday, it is now Thursday, and "what can I do this
week in Rome?" asks about four days that still have rows and three that never
will.

Before this was fixed the retrieval clipped its queries to the covered days
but kept the full window everywhere the reader and the validator look:

  * the answer's heading read "Rome, 2026-09-25 to 2026-10-01" while carrying
    three days of forecast,
  * no gap sentence said the other four days were missing, and
  * `Brief.allowed_dates()` still contained them, so a model sentence
    asserting weather on 2026-10-01 passed `violations` clean.

That is hard rule 2 of the assignment -- a question outside the coverage gets
an explicit "no data for that date", never a guess -- failing in the one form
a reviewer is most likely to meet, because it needs no unusual question at
all, only a snapshot that is a few days old.

The three assertions below are deliberately separate. Narrowing the window
without adding the gap would silently drop the days; adding the gap without
narrowing `allowed_dates` would leave the validator permissive.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from services.agent import dates, grounding, router

TODAY = date(2026, 9, 25)
# Staged three days ago and never refreshed: it still reaches tomorrow, and
# stops four days short of the week the question asks about.
LAST_COVERED = date(2026, 9, 27)

ROME = {
    "id": "rome",
    "name": "Rome",
    "country": "Italy",
    "timezone": "Europe/Rome",
    "aliases": ["rome", "roma"],
    "coastal": False,
}

COVERAGE = {
    "cities": [ROME],
    "entities": [{"entity": "places", "as_of": "2026-09-22T06:00:00+00:00"}],
    "weather_first_date": "2026-09-22",
    "weather_last_date": str(LAST_COVERED),
    "weather_as_of": "2026-09-22T06:00:00+00:00",
}

COVERED = ["2026-09-25", "2026-09-26", "2026-09-27"]
UNCOVERED = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"]


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
def rome(monkeypatch):
    """A Router whose stored forecast stops four days before the week ends."""
    monkeypatch.setattr(dates, "today_in", lambda _tz: TODAY)
    monkeypatch.setattr(router.queries, "cities", lambda _conn: [ROME])
    monkeypatch.setattr(router.queries, "coverage", lambda _conn: COVERAGE)
    monkeypatch.setattr(router.queries, "events", lambda *_a, **_k: [])
    monkeypatch.setattr(
        router.queries, "expired_events", lambda *_a, **_k: {"expired": 0, "last_checked": None}
    )
    monkeypatch.setattr(router.queries, "facts", lambda *_a, **_k: [])
    monkeypatch.setattr(router.queries, "places", lambda *_a, **_k: [])

    def _days(start, end):
        return [start + timedelta(days=i) for i in range((end - start).days + 1)]

    # The database only ever returns rows it has: the query is already clipped
    # to the covered days, and a fixture that answered beyond `weather_last_date`
    # would be testing a database that cannot exist. Read off COVERAGE rather
    # than the constant, so a test that widens the window widens the rows too.
    def _last():
        return date.fromisoformat(COVERAGE["weather_last_date"])

    def forecast(_conn, _city, *, start, end):
        return [_forecast_row(d) for d in _days(start, end) if d <= _last()]

    def recommendations(_conn, _city, *, start=None, end=None, activity=None):
        return [_recommendation(d) for d in _days(start, end) if d <= _last()]

    monkeypatch.setattr(router.queries, "forecast", forecast)
    monkeypatch.setattr(router.queries, "recommendations", recommendations)
    return router.Router(object())


def _brief(rome):
    result = rome.retrieve("What can I do this week in Rome?")
    assert result.refusal is None, "a week with three covered days is not a refusal"
    return result, grounding.build(result)


def test_the_answer_states_which_days_it_has_no_data_for(rome):
    """The gap sentence is the "no data for that date" the brief requires."""
    result, brief = _brief(rome)

    gaps = [g.text for g in brief.gaps if g.subject == "coverage:partial"]
    assert gaps, f"no coverage gap for {UNCOVERED}; gaps were {[g.subject for g in brief.gaps]}"
    sentence = gaps[0]
    assert "2026-09-28" in sentence and "2026-10-01" in sentence
    assert str(LAST_COVERED) in sentence, "say where the stored forecast actually ends"


def test_an_uncovered_day_is_not_a_date_the_answer_may_assert(rome):
    """The validator's allow-list must not include days with no rows.

    This is the assertion that keeps the fix honest: a gap sentence the model
    is free to contradict is decoration.
    """
    _result, brief = _brief(rome)

    allowed = brief.allowed_dates()
    assert set(COVERED) <= allowed
    assert not (set(UNCOVERED) & allowed), f"uncovered days are still assertable: {allowed}"


def test_weather_claimed_for_an_uncovered_day_is_a_violation(rome):
    """The end-to-end property, through the real validator."""
    _result, brief = _brief(rome)

    invented = (
        "Rome looks pleasant all week. On 2026-10-01 you can go sightseeing under clear skies."
    )
    assert grounding.violations(invented, brief), "an invented day passed the grounding check"

    grounded = "On 2026-09-26 sightseeing scores 82 out of 100, which is good."
    assert grounding.violations(grounded, brief) == []


def test_the_rendered_answer_carries_the_limit_next_to_the_heading(rome):
    """What the reader sees must account for every day the heading claims.

    The heading deliberately still states the asked window -- that is the
    question, and shrinking it would misreport what was asked. What it may not
    do is state a week and then show three days with nothing saying why. The
    gap sentence is code-written and appended after the model, so it survives
    any wording the model chooses.
    """
    _result, brief = _brief(rome)
    rendered = grounding.render(brief)

    assert brief.window.startswith("2026-09-25")
    day_lines = [line for line in rendered.splitlines() if line.startswith("- 2026-")]
    assert len(day_lines) == len(COVERED) * 2, "one forecast and one verdict line per covered day"
    assert "No weather is stored for 2026-09-28 to 2026-10-01" in rendered
    assert f"forecast ends on {LAST_COVERED}" in rendered


def test_the_model_is_told_which_days_it_may_not_describe(rome):
    """The prompt has to carry the gap, not only the validator.

    Catching an invented day after the fact costs the whole model answer: the
    agent falls back to `render`. Telling the model up front is what keeps the
    good answer the common case.
    """
    _result, brief = _brief(rome)
    prompt = grounding.prompt_block(brief)

    assert "DATES WITH NO STORED WEATHER" in prompt
    assert "2026-10-01" in prompt.split("DATES WITH NO STORED WEATHER")[1]


def test_a_fully_covered_week_gains_no_gap_and_loses_no_day(monkeypatch, rome):
    """The control. A snapshot that reaches the end of the week is unchanged.

    Without this, narrowing the window to the covered days would pass every
    assertion above by simply always truncating -- including when there is
    nothing to truncate.
    """
    monkeypatch.setitem(COVERAGE, "weather_last_date", "2026-10-05")
    try:
        result = rome.retrieve("What can I do this week in Rome?")
        brief = grounding.build(result)

        assert [g.subject for g in brief.gaps if g.subject == "coverage:partial"] == []
        assert set(COVERED) | set(UNCOVERED) <= brief.allowed_dates()
    finally:
        COVERAGE["weather_last_date"] = str(LAST_COVERED)
