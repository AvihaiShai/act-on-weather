"""What a coastal score is allowed to claim.

The review found two halves of one defect. `coastal: true` in data/cities.yml
was an unchecked assertion -- Rome carried it, and Rome's forecast point is
about 25 km from the sea -- and the scores it unlocked were computed from wind,
rain and temperature and then reported as a verdict on surfing, swimming,
fishing and boat rides, none of which are decided by the air.

No marine data was added to close it, because a wave feed is a second provider
to stage into the offline bundle. What was added is evidence and a limit:

  * every coastal city names a real point on its coast, and the distance from
    its forecast point to that point is derived, never committed, so it cannot
    go stale against the coordinates it comes from;
  * the four sea-dependent activities are capped at 69 -- one point below the
    `good` floor in `rules.BANDS` -- so however perfect the land forecast is,
    the system reports them as at best `fair` and says why;
  * and every answer that shows one of those scores carries a sentence naming
    the forecast point, its distance from the coast, and the fact that nothing
    in the stored data measures the water.

These tests hold all three. The wording ones go through the agent's real
render path rather than re-deriving a sentence, because a caveat that only a
test knows how to produce is a caveat the traveller never sees.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from services.agent import dates, router
from services.common import coast, rules
from services.consumer import main as consumer

ROOT = Path(__file__).resolve().parents[2]
CITIES_YML = ROOT / "data" / "cities.yml"
ACTIVITIES_YML = ROOT / "data" / "activities.yml"

# The four activities whose quality is a property of the water. Written out
# here rather than read from the file, so that dropping the flag from one of
# them fails a test instead of silently narrowing the rule it protects.
SEA_STATE = ("surfing", "swimming", "fishing", "boat_ride")

# Warm, dry, sunny, with enough breeze to satisfy surfing's `min_wind_kmh` and
# little enough to stay inside a boat ride's limit: every land rule these four
# have is satisfied, so the only thing that can hold their score down is the
# ceiling.
PERFECT_COASTAL_DAY = {
    "temp_max_c": 27.0,
    "temp_min_c": 21.0,
    "precip_mm": 0.0,
    "precip_prob": 5,
    "wind_kmh": 14.0,
    "uv_index": 5.0,
    "sunshine_hours": 11.0,
}

CAPPED = "capped at 69"


@pytest.fixture(scope="module")
def activities():
    _version, acts = rules.load_activities(ACTIVITIES_YML)
    return acts


@pytest.fixture(scope="module")
def cities():
    return yaml.safe_load(CITIES_YML.read_text(encoding="utf-8"))["cities"]


# ---------------------------------------------------------- the ceiling ----


@pytest.mark.parametrize("activity", SEA_STATE)
def test_a_perfect_forecast_cannot_make_a_sea_activity_good(activities, activity):
    """The rule the whole finding turns on.

    Every land rule these four carry is satisfied by `PERFECT_COASTAL_DAY`, so
    without the ceiling each of them would score 100 and be reported as `good`
    -- a confident verdict on a sea nothing in this system has looked at.
    """
    result = rules.score_activity(activity, activities[activity], PERFECT_COASTAL_DAY)
    assert result.score == 69, f"{activity} scored {result.score}"
    assert result.band == "fair"


@pytest.mark.parametrize("activity", ["beach_day", "sightseeing"])
def test_an_activity_the_weather_really_does_decide_is_untouched(activities, activity):
    """A day on the sand is a judgement about sun, heat, rain and wind, and
    those are measured. Capping it would be the opposite error: refusing to
    answer a question the data does answer."""
    result = rules.score_activity(activity, activities[activity], PERFECT_COASTAL_DAY)
    assert result.band == "good"
    assert result.score > 69
    assert not any(CAPPED in reason for reason in result.reasons)


def test_the_beach_still_reaches_a_hundred(activities):
    """The sharpest form of the exception: on the same forecast that caps
    surfing at 69, a day at the beach is a flawless 100. The two sit side by
    side in the heatmap, and the difference between them is evidence."""
    assert (
        rules.score_activity("beach_day", activities["beach_day"], PERFECT_COASTAL_DAY).score == 100
    )


@pytest.mark.parametrize("activity", SEA_STATE)
def test_the_cap_says_so_when_it_binds(activities, activity):
    """A score quietly lowered is a score the reader cannot argue with. The
    reason is what the model is handed to write from, so it has to name the
    cap and what is missing."""
    reasons = rules.score_activity(activity, activities[activity], PERFECT_COASTAL_DAY).reasons
    capped = [reason for reason in reasons if CAPPED in reason]
    assert len(capped) == 1, reasons
    assert "waves" in capped[0] and "swell" in capped[0] and "water temperature" in capped[0]


@pytest.mark.parametrize("activity", SEA_STATE)
def test_the_cap_is_silent_when_the_weather_already_decided(activities, activity):
    """A cold, wet, blowing day scores these four well under 69 on the weather
    alone. Saying "capped at 69" there would be false: nothing was capped."""
    storm = {
        "temp_max_c": 4.0,
        "temp_min_c": 1.0,
        "precip_mm": 20.0,
        "precip_prob": 95,
        "wind_kmh": 60.0,
        "uv_index": 1.0,
        "sunshine_hours": 0.0,
    }
    result = rules.score_activity(activity, activities[activity], storm)
    assert result.score < 69
    assert not any(CAPPED in reason for reason in result.reasons)


def test_the_ceiling_sits_one_point_below_the_good_band(activities):
    """69 is not a round number chosen for its looks. `BANDS` puts `good` at
    70, so the ceiling is the highest score that cannot be read as a
    recommendation -- and if the band floor ever moves, this fails."""
    good_floor = dict(rules.BANDS)["good"]
    for activity in SEA_STATE:
        ceiling = activities[activity]["score_ceiling"]
        assert ceiling == good_floor - 1
        assert rules.band_for(ceiling) == "fair"


def test_only_the_sea_dependent_activities_are_capped(activities):
    """The flag and the ceiling are one decision and must travel together: a
    capped activity with no stated reason, or a flagged one with no cap, is
    half the fix."""
    flagged = {k for k, cfg in activities.items() if cfg.get("sea_state_unmeasured")}
    capped = {k for k, cfg in activities.items() if cfg.get("score_ceiling") is not None}
    assert flagged == set(SEA_STATE)
    assert capped == set(SEA_STATE)
    # The beach is the deliberate exception, and it is the one most likely to
    # be swept in by somebody tidying up "the coastal activities".
    assert activities["beach_day"].get("requires_coast")
    assert not activities["beach_day"].get("sea_state_unmeasured")


# ------------------------------------------------------ the coast claim ----


def test_every_coastal_city_names_its_coast(cities):
    """`coastal: true` decides whether a city gets surfing rows at all. Without
    a reference point it is an assertion nobody can check, which is how Rome --
    25 km inland of Lido di Ostia -- came to carry a surf verdict."""
    for city in cities:
        block = city.get("coast")
        if city["coastal"]:
            assert block, f"{city['slug']} is coastal with no coast reference"
            assert block["name"]
            assert isinstance(block["lat"], int | float)
            assert isinstance(block["lon"], int | float)
        else:
            assert not block, f"{city['slug']} is inland but names a coast"


def test_london_is_the_inland_city(cities):
    """Guards the test above from passing because every city is coastal."""
    inland = [c["slug"] for c in cities if not c["coastal"]]
    assert inland == ["london"]


@pytest.mark.parametrize(
    "slug, expected_km",
    [
        ("rome", 24.7),
        ("lisbon", 17.8),
        ("tel-aviv", 1.4),
        ("reykjavik", 2.6),
    ],
)
def test_the_stored_distance_is_derived_from_the_committed_coordinates(cities, slug, expected_km):
    """What the consumer would write to `cities.coast_distance_km`, checked
    against an independently computed great-circle distance.

    The expected values are the ones the review was given, to a tenth of a
    kilometre. Two of them are the finding itself: Rome's forecast point is
    about 25 km from the sea and Lisbon's about 18 km, so neither city's
    forecast is a coastal measurement however the flag reads.
    """
    city = next(c for c in cities if c["slug"] == slug)
    columns = consumer.coast_columns(city)
    assert columns["coast_name"] == city["coast"]["name"]
    assert columns["coast_lat"] == city["coast"]["lat"]
    assert columns["coast_lon"] == city["coast"]["lon"]
    assert columns["coast_distance_km"] == pytest.approx(expected_km, abs=0.1)


def test_an_inland_city_stores_no_coast_at_all(cities):
    """Nulls, not zeroes: a distance of 0 km would read as "on the beach"."""
    london = next(c for c in cities if c["slug"] == "london")
    assert consumer.coast_columns(london) == {
        "coast_name": None,
        "coast_lat": None,
        "coast_lon": None,
        "coast_distance_km": None,
    }


def test_the_distance_is_not_written_down_anywhere(cities):
    """A coast block may name a point and describe it, and may not state a
    distance. A committed number would outlive the coordinate it was measured
    from, and the first anyone would know of it is an answer quoting a distance
    that stopped being true."""
    for city in cities:
        assert set(city.get("coast") or {}) <= {"name", "lat", "lon", "note"}


# ----------------------------------------------------------- the wording ----

TODAY = date(2026, 9, 24)

# The city as `queries.cities` returns it once migration 007 has run, so the
# caveat below is built from the same four columns the agent reads in
# production.
TEL_AVIV = {
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
ROME = {
    "id": "rome",
    "name": "Rome",
    "country": "Italy",
    "timezone": "Europe/Rome",
    "aliases": ["rome", "roma"],
    "coastal": True,
    "coast_name": "Lido di Ostia",
    "coast_lat": 41.7325,
    "coast_lon": 12.2777,
    "coast_distance_km": 24.681,
}

COVERAGE = {
    "cities": [ROME, TEL_AVIV],
    "entities": [{"entity": "places", "as_of": "2026-09-24T08:47:17+00:00"}],
    "weather_first_date": str(TODAY),
    "weather_last_date": str(TODAY + timedelta(days=6)),
    "weather_as_of": "2026-09-23T18:16:00+00:00",
}


@pytest.fixture
def ask(monkeypatch, activities):
    """The agent over canned rows: no database, no model, no clock.

    The recommendation rows are scored by the real rule engine rather than
    typed out, so the band and the score in the answer are the ones the shipped
    catalogue produces -- and a question about surfing on a perfect day comes
    back as the capped 69 rather than an invented 100.
    """
    monkeypatch.setenv("POSTGRES_READER_PASSWORD", "unit-test")
    from services.agent import main

    monkeypatch.setattr(dates, "today_in", lambda _timezone: TODAY)
    monkeypatch.setattr(router.queries, "cities", lambda _conn: [ROME, TEL_AVIV])
    monkeypatch.setattr(router.queries, "coverage", lambda _conn: COVERAGE)
    monkeypatch.setattr(router.queries, "events", lambda *_a, **_k: [])
    monkeypatch.setattr(router.queries, "facts", lambda *_a, **_k: [])
    monkeypatch.setattr(router.queries, "forecast", lambda *_a, **_k: [])
    monkeypatch.setattr(router.queries, "places", lambda *_a, **_k: [])

    def recommendations(_conn, city_id=None, *, start=None, end=None, activity=None):
        if activity not in activities:
            return []
        scored = rules.score_activity(activity, activities[activity], PERFECT_COASTAL_DAY)
        days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
        return [
            {
                "forecast_date": day,
                "activity": activity,
                "activity_label": activities[activity]["label"],
                "score": scored.score,
                "band": scored.band,
                "reasons": scored.reasons,
                "text": None,
                "status": "ready",
            }
            for day in days
        ]

    monkeypatch.setattr(router.queries, "recommendations", recommendations)

    def answer(question: str) -> str:
        result = router.Router(object()).retrieve(question)
        assert result.refusal is None, result.refusal
        assert result.recommendations, f"no scores retrieved for {question!r}"
        return main.named_activity_answer(result)

    return answer


@pytest.mark.parametrize(
    "question, activity",
    [
        ("is it good for surfing in tel aviv this week", "surfing"),
        ("can I swim in tel aviv this week", "swimming"),
        ("is the fishing any good in tel aviv this week", "fishing"),
        ("is it a good week for a boat ride in tel aviv", "boat_ride"),
    ],
)
def test_a_named_sea_activity_answer_always_carries_the_caveat(ask, question, activity):
    """The concrete defect this closes.

    `where_answer` already said what a coastal score does not cover; this route
    -- the one that answers "is it good for surfing in Tel Aviv tomorrow?" --
    emitted the scores with no caveat at all, which is the most direct way for
    the system to imply it had looked at the sea.
    """
    answer = ask(question)
    assert "Gordon Beach" in answer
    assert "1.4 km" in answer
    assert "nothing in the data measures the waves, the swell or the water temperature" in answer


def test_the_caveat_names_the_distance_the_review_actually_caught(ask):
    """Rome is the city the finding names: the forecast that scores its surf is
    taken about 25 km from the water."""
    answer = ask("is it good for surfing in rome this week")
    assert "the Rome forecast point, 25 km from Lido di Ostia" in answer


def test_the_capped_score_is_what_the_answer_reports(ask):
    """The ceiling and the caveat are one story: the answer shows `fair (69/100)`
    on a flawless day and says in the next breath why it cannot say more."""
    answer = ask("is it good for surfing in tel aviv this week")
    assert f"{TODAY}: Surfing is fair (69/100)." in answer


@pytest.mark.parametrize(
    "question",
    [
        "is it good for running in tel aviv this week",
        "is it a good week for the beach in tel aviv",
        "is it good for sightseeing in rome this week",
    ],
)
def test_an_answer_about_a_land_activity_does_not_carry_the_caveat(ask, question):
    """The caveat has to stay rare enough to mean something. Running, a day on
    the sand and sightseeing are decided by weather this system measures, and
    attaching a disclaimer about swell to them would be noise."""
    answer = ask(question)
    assert "swell" not in answer
    assert "Gordon Beach" not in answer and "Lido di Ostia" not in answer


def test_the_where_route_keeps_its_own_caveat(monkeypatch, ask):
    """`where_answer` combines both sentences: a score is not a verdict on the
    place, and for a sea activity it is not a verdict on the water either."""
    monkeypatch.setenv("POSTGRES_READER_PASSWORD", "unit-test")
    from services.agent import main

    result = router.Router(object()).retrieve("where and when can I surf in tel aviv")
    answer = main.where_answer(result)
    assert "rate the stored weather, not the place" in answer
    assert "They rate the stored forecast for the Tel Aviv forecast point, 1.4 km" in answer


def test_a_city_with_no_stored_coast_still_refuses_to_claim_the_sea():
    """A null coast column -- an inland city, or a row written before migration
    007 -- must lose the distance and keep the claim. Dropping the whole caveat
    because one column is null would be the wrong way to fail."""
    sentence = coast.sea_state_caveat({"id": "london", "name": "London"})
    assert "the London forecast point" in sentence
    assert "nothing in the data measures the waves" in sentence
    assert " km" not in sentence


# -------------------------------------------------------- the itinerary ----


def plan(monkeypatch, rows: list[dict]):
    """One day of Tel Aviv itinerary, built from the rows handed in.

    The planner is where a coastal score stops being a number in a table and
    becomes advice for a Tuesday, so it is the last place the caveat could go
    missing.
    """
    monkeypatch.setenv("POSTGRES_READER_PASSWORD", "unit-test")
    from services.agent import main

    coverage = {
        "weather_first_date": str(TODAY),
        "weather_last_date": str(TODAY),
        "weather_as_of": str(TODAY),
    }
    monkeypatch.setattr(main, "pool", type("StubPool", (), {"conn": object()})())
    monkeypatch.setattr(main.queries, "cities", lambda _conn: [TEL_AVIV])
    monkeypatch.setattr(main.queries, "coverage", lambda _conn: coverage)
    monkeypatch.setattr(
        main.queries,
        "recommendations",
        lambda *_a, **_k: [{**row, "forecast_date": TODAY} for row in rows],
    )
    monkeypatch.setattr(main.queries, "places", lambda *_a, **_k: [])
    monkeypatch.setattr(main.queries, "events", lambda *_a, **_k: [])
    built = main.build_itinerary(
        main.ItineraryIn(city="tel-aviv", start_date=TODAY, end_date=TODAY)
    )
    return built["days"][0]


def row(activity: str, label: str, score: int, band: str) -> dict:
    return {
        "activity": activity,
        "activity_label": label,
        "score": score,
        "band": band,
        "reasons": [],
        "text": None,
        "status": "ready",
    }


def test_a_planned_sea_day_says_what_it_did_not_measure(monkeypatch):
    day = plan(monkeypatch, [row("surfing", "Surfing", 69, "fair")])
    assert day["activity"] == "Surfing"
    assert "the Tel Aviv forecast point, 1.4 km from Gordon Beach" in day["activity_caveat"]


def test_a_sea_activity_offered_as_a_runner_up_still_carries_it(monkeypatch):
    """The alternatives are rendered with their scores, so a day that suggests
    a boat ride second makes the same claim a day that suggests it first
    does."""
    day = plan(
        monkeypatch,
        [
            row("sightseeing", "Sightseeing on foot", 94, "good"),
            row("boat_ride", "A boat ride", 69, "fair"),
        ],
    )
    assert day["activity"] == "Sightseeing on foot"
    assert [alt["label"] for alt in day["alternatives"]] == ["A boat ride"]
    assert "Gordon Beach" in day["activity_caveat"]


def test_a_land_only_day_carries_no_caveat(monkeypatch):
    day = plan(
        monkeypatch,
        [
            row("sightseeing", "Sightseeing on foot", 94, "good"),
            row("museums", "Visiting a museum", 55, "fair"),
        ],
    )
    assert day["activity_caveat"] is None


# ------------------------------------------------------------- the UI ----


def test_the_ui_caption_is_word_for_word_the_agent_caveat(monkeypatch):
    """The UI ships without `services/common/`, so the sentence exists twice --
    once in `common.coast` and once in `services/ui/app.py`. This is what stops
    the two drifting: the caption the heatmap actually renders is compared with
    the agent's, for the same city row, through a real app render.
    """
    from tests.unit.test_ui import FIXTURES, _run

    lisbon = {
        **next(c for c in FIXTURES["coverage"]["cities"] if c["id"] == "lisbon"),
        "coast_name": "Praia de Carcavelos",
        "coast_lat": 38.6797,
        "coast_lon": -9.3372,
        "coast_distance_km": 17.81,
    }
    coverage = {
        **FIXTURES["coverage"],
        "cities": [lisbon if c["id"] == "lisbon" else c for c in FIXTURES["coverage"]["cities"]],
    }

    app = _run(monkeypatch, overrides={"coverage": coverage})
    assert not app.exception, [element.value for element in app.exception]
    captions = [element.value for element in app.caption]
    assert coast.sea_state_caveat(lisbon) in captions
    # And it says the thing the fixture city needs it to say: Lisbon's forecast
    # point is in the city, and the ocean beaches are 18 km away in Cascais.
    assert "18 km from Praia de Carcavelos" in coast.sea_state_caveat(lisbon)
