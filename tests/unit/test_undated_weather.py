"""The weather claim that carries no date, and our own sentence coming back.

Two holes that every check in `grounding.violations` was shaped to walk past,
because every one of them needs a date to test:

  * An undated weather claim. "Rome is warm and dry this week" against a brief
    that holds no `DayFact` at all is the whole forecast invented, in a
    sentence with nothing in it for a date check to look at. Check 4b closes
    it, shaped like check 4 -- the trigger is the absence of the rows.
  * Our own coverage-gap sentence, repeated. The prompt shows the model that
    sentence and asks it not to repeat it; when it repeats it anyway, the
    dates and figures in it are precisely the days no row carries, so checks 7
    and 9 threw the answer away and told the operator the rows did not support
    a sentence we wrote ourselves.

The second one only became reachable when `allowed_dates` narrowed to the days
a forecast row came back for, which is also what this file pins: the narrowing
belongs to that allow-list and must not reach `vocabulary`, or the figures in
our own prose stop being quotable.

The clock is never read here. Every date is written out, so these tests keep
meaning something after the staged snapshot has expired.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from services.agent import grounding, router

DAY1 = date(2026, 9, 25)
DAY2 = date(2026, 9, 26)
DAY3 = date(2026, 9, 27)
WEEK = [f"2026-09-{d}" for d in range(25, 31)] + ["2026-10-01"]

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
    "weather_first_date": "2026-09-25",
    "weather_last_date": "2026-10-01",
    "weather_as_of": "2026-09-22T06:00:00+00:00",
}


def _forecast_row(day):
    return {
        "forecast_date": day,
        "provider": "open-meteo",
        "temp_max_c": 24.0,
        "temp_min_c": 14.0,
        "precip_mm": 0.0,
        "precip_prob": 5,
        "wind_kmh": 9.0,
        "sunshine_hours": 9.0,
    }


def _event(event_id, title, category, day, last_day=None):
    return {
        "id": event_id,
        "title": title,
        "category": category,
        "venue": "Auditorium Parco della Musica",
        "starts_at": datetime(day.year, day.month, day.day, 20, tzinfo=UTC),
        "timezone": "Europe/Rome",
        "starts_on": day,
        "ends_on": last_day or day,
        "is_sample": False,
        "source": "venue listing",
    }


def _brief(question, *, forecast=(), events=(), facts=(), covered=None, uncovered=()):
    """A Brief built through the real `grounding.build`.

    The retrieval is assembled by hand so the stored rows are stated exactly,
    but the resolution is the actual router code -- only the SQL is replaced --
    so intent and event-category detection are under test here too. The window
    is pinned rather than parsed: `dates.parse` reads the clock, and a test
    whose asked window moves every day cannot pin which days were covered.
    """
    resolution = router.Resolution(
        question=question,
        city=ROME,
        window=router.dates.DateRange(date(2026, 9, 25), date(2026, 10, 1), "this week"),
    )
    text = question.lower()
    for intent, words in router.INTENT_WORDS.items():
        if any(router._mentions(text, word) for word in words):
            resolution.intents.append(intent)
    if not resolution.intents:
        resolution.intents = ["weather", "activities"]
    resolution.event_categories = grounding.requested_event_categories(text)

    result = router.Retrieval(
        resolution,
        COVERAGE,
        True,
        forecast=list(forecast),
        events=list(events),
        facts=list(facts),
    )
    result.weather_scoped = True
    result.covered_days = (
        list(covered) if covered is not None else [str(r["forecast_date"]) for r in forecast]
    )
    result.uncovered_days = list(uncovered)
    return grounding.build(result)


# ------------------------------------------ 4b: a weather claim with no row --


def test_an_undated_weather_claim_with_no_forecast_row_is_rejected():
    """The hole. No date, so checks 7 and 9 have nothing to test, and before
    this check the sentence was the model's forecast presented as ours."""
    brief = _brief("What can I do this week in Rome?", uncovered=WEEK)
    assert brief.days == []

    found = grounding.violations("Rome is warm and dry this week.", brief)
    assert any("no stored forecast row" in v for v in found), found


def test_an_undated_weather_claim_is_rejected_whatever_the_question_was():
    """A question about history does not license a forecast either. The check
    is about which rows came back, not about which intent was resolved."""
    brief = _brief("Tell me about the history of Rome.")
    found = grounding.violations("Rome is mild at this time of year.", brief)
    assert any("no stored forecast row" in v for v in found), found


def test_a_weather_claim_with_stored_rows_behind_it_is_allowed():
    """The control. With rows this is exactly the sentence we want written --
    without it every assertion above would pass by rejecting all weather."""
    brief = _brief(
        "What is the weather this week in Rome?",
        forecast=[_forecast_row(DAY1), _forecast_row(DAY2)],
    )
    assert grounding.violations("Rome is warm and dry this week.", brief) == []


def test_saying_no_forecast_is_on_record_is_allowed():
    """The honest sentence for the same absence has to survive, or the check
    leaves the answer with nothing at all it is allowed to say."""
    brief = _brief("What can I do this week in Rome?", uncovered=WEEK)
    for wording in (
        "No forecast is on record for Rome.",
        "There is no stored weather for those dates.",
        "I do not have a forecast for this week.",
        "I have no weather on record for Rome.",
    ):
        assert grounding.violations(wording, brief) == [], wording


def test_background_prose_about_the_climate_may_still_be_paraphrased():
    """A weather word the retrieved summary carries is a paraphrase of a
    stored fact, not a forecast."""
    brief = _brief(
        "Tell me about Rome.",
        facts=[{"title": "Rome", "summary": "Rome has a Mediterranean climate with mild winters."}],
    )
    assert grounding.violations("Rome has mild winters.", brief) == []


# ------------------------------------------------- our own words, repeated --


def test_our_own_coverage_sentence_repeated_is_not_a_violation():
    """The prompt shows the model this sentence and asks it not to repeat it.

    When it repeats it anyway that is code's own wording coming back. It names
    precisely the days no row carries, so it failed the date and figure checks
    and cost the answer its wording -- over a sentence we handed the model
    ourselves, twice, in `prompt_block`.
    """
    brief = _brief("What can I do this week in Rome?", uncovered=WEEK)
    gap = [g.text for g in brief.gaps if g.subject == "coverage:partial"]
    assert gap, [g.subject for g in brief.gaps]

    assert grounding.violations(gap[0], brief) == []


def test_a_paraphrase_of_the_gap_sentence_is_still_checked():
    """Only the wording we guarantee is exempt.

    A reworded version names the same uncovered days without the guarantee, so
    the date check still has to look at it. What it must not do is also
    complain about the *figures* in it: "1" and "10" came out of the window the
    traveller asked about, which is vocabulary whatever the forecast reaches.
    """
    brief = _brief(
        "What can I do this week in Rome?",
        forecast=[_forecast_row(DAY1)],
        uncovered=WEEK[1:],
    )
    found = grounding.violations("I have no stored weather for 2026-10-01.", brief)
    assert any("2026-10-01" in v and "no retrieved row carries" in v for v in found), found
    assert not any("states the figure" in v for v in found), found


def test_the_asked_window_stays_quotable_after_the_allow_list_narrows():
    """`window_days` feeds two different things and only one of them narrows.

    `allowed_dates` is the allow-list for check 7 and must shrink to the days a
    row came back for. `vocabulary` is what checks 8 and 9 read, and it must
    not: the uncovered days are numbers our own gap sentence prints.
    """
    brief = _brief(
        "What can I do this week in Rome?",
        forecast=[_forecast_row(DAY1)],
        uncovered=WEEK[1:],
    )
    assert brief.window_days == WEEK
    assert brief.allowed_dates() & set(WEEK) == {"2026-09-25"}
    assert {"30", "10", "1"} <= brief.allowed_numbers()


# --------------------------------------------- the three checks, together --


def test_a_partial_week_with_events_answers_without_a_spurious_violation():
    """The integration case: partial weather, a listing outside it, one answer.

    A concert row sits on a day the forecast does not reach. Naming it is
    supported by the event row, so check 3b must not place it wrongly and
    check 7 must not call the date invented -- while an invented day is still
    caught and the coverage gap is still stated.
    """
    brief = _brief(
        "What can I do this week in Rome? We like concerts.",
        forecast=[_forecast_row(DAY1), _forecast_row(DAY2)],
        events=[_event("v:1", "Chamber Recital", "concert", date(2026, 9, 30))],
        uncovered=["2026-09-27", "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"],
    )
    assert [g.subject for g in brief.gaps if g.subject == "coverage:partial"]

    grounded = "Chamber Recital is a concert at Auditorium Parco della Musica on 2026-09-30."
    assert grounding.violations(grounded, brief) == [], grounded

    invented = "Concerts are scheduled on 2026-09-30 and 2026-10-01."
    found = grounding.violations(invented, brief)
    assert any("2026-10-01" in v and "no stored event row covers" in v for v in found), found


def test_a_partial_week_does_not_make_4b_fire_on_the_days_it_does_hold():
    """Check 4b is all-or-nothing on the rows. A partially covered week has
    `DayFact`s, so describing them is what the answer is for."""
    brief = _brief(
        "What is the weather this week in Rome?",
        forecast=[_forecast_row(DAY1), _forecast_row(DAY2)],
        uncovered=WEEK[2:],
    )
    answer = "On 2026-09-25 Rome is warm and dry, and 2026-09-26 stays clear."
    assert grounding.violations(answer, brief) == [], answer
