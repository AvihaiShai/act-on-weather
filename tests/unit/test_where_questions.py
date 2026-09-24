"""The `where` route: a location question gets a location, or an honest gap.

The bug these tests exist for was visible in one screenshot. Asked "where can
I surf in tel aviv", the agent answered with seven days of surfing suitability
scores and no location at all -- the system answering the question it had an
answer for rather than the one that was asked. `where` matched no intent word,
so the location half of the question was never seen.

Three properties are pinned here, and the middle one is the load-bearing one.

  1. A `where` question is recognised and routed to places, not to weather.
  2. A beach is never offered as a surf spot. Tel Aviv has five beaches on
     record; not one of them is recorded as having rideable surf, so the
     answer is "I do not have a verified surf spot", not a beach. This is the
     agent's half of the rule `test_planner_venues.py` pins for the planner.
  3. Scores appear only when the question also asked about timing, and say
     what they are when they do -- a suitability score rates the stored
     forecast, which for surfing is wind and rain, never the sea.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from services.agent import dates, router

TODAY = date(2026, 9, 24)

TEL_AVIV = {
    "id": "tel-aviv",
    "name": "Tel Aviv",
    "country": "Israel",
    "timezone": "Asia/Jerusalem",
    "aliases": ["tel aviv", "tlv"],
    "coastal": True,
    # The coast reference `queries.cities` returns since migration 007: the
    # named point that makes `coastal` checkable, and how far the forecast
    # point sits from it. Tel Aviv is the near case at 1.4 km; the caveat is
    # emitted anyway, because standing on the shore is not measuring the water.
    "coast_name": "Gordon Beach",
    "coast_lat": 32.0836,
    "coast_lon": 34.7669,
    "coast_distance_km": 1.416,
}
LONDON = {
    "id": "london",
    "name": "London",
    "country": "United Kingdom",
    "timezone": "Europe/London",
    "aliases": ["london"],
    "coastal": False,
}

COVERAGE = {
    "cities": [LONDON, TEL_AVIV],
    "entities": [{"entity": "places", "as_of": "2026-09-24T08:47:17+00:00"}],
    "weather_first_date": str(TODAY),
    "weather_last_date": str(TODAY + timedelta(days=6)),
    "weather_as_of": "2026-09-23T18:16:00+00:00",
}


def place(pid: str, name: str, category: str, city: str = "tel-aviv"):
    return {
        "id": pid,
        "city_id": city,
        "name": name,
        "category": category,
        "lat": 32.08,
        "lon": 34.77,
        "source": "Wikidata (CC0)",
        "source_url": f"https://www.wikidata.org/wiki/{pid.split(':')[-1]}",
        "is_sample": False,
        "as_of": "2026-09-24T08:47:17+00:00",
    }


# The rows Wikidata actually returned for Tel Aviv, so the fixture cannot
# drift into fiction.
TEL_AVIV_PLACES = [
    place("wikidata:Q56377198", "Bugrashov Beach, Tel Aviv", "beach"),
    place("wikidata:Q136117836", "Frishman Beach", "beach"),
    place("wikidata:Q56376795", "Hilton Beach, Tel Aviv", "beach"),
    place("wikidata:Q56377589", "Jerusalem Beach, Tel Aviv", "beach"),
    place("wikidata:Q56378021", "Metzitzim Beach, Tel Aviv", "beach"),
    place("wikidata:Q7217322", "Tel Aviv Marina", "marina"),
    place("wikidata:Q1216226", "Tel Aviv Museum of Art", "museum"),
]


def recommendation(day: date, activity: str, label: str, score: int, band: str):
    return {
        "forecast_date": day,
        "activity": activity,
        "activity_label": label,
        "score": score,
        "band": band,
        "text": None,
        "status": "ready",
    }


@pytest.fixture
def stub(monkeypatch):
    """A Router over canned rows: no database, no model, no clock."""
    monkeypatch.setattr(dates, "today_in", lambda _timezone: TODAY)
    monkeypatch.setattr(router.queries, "cities", lambda _conn: [LONDON, TEL_AVIV])
    monkeypatch.setattr(router.queries, "coverage", lambda _conn: COVERAGE)
    monkeypatch.setattr(router.queries, "events", lambda *_a, **_k: [])
    # Asked only when the retrieval above comes back empty, so a gap can
    # say whether the feed went stale or was never there.
    monkeypatch.setattr(
        router.queries, "expired_events", lambda *_a, **_k: {"expired": 0, "last_checked": None}
    )
    monkeypatch.setattr(router.queries, "facts", lambda *_a, **_k: [])
    monkeypatch.setattr(router.queries, "forecast", lambda *_a, **_k: [])

    asked: dict[str, list] = {"places": [], "recommendations": []}

    def places(_conn, city_id=None, *, categories=None, limit=50):
        asked["places"].append(categories)
        rows = TEL_AVIV_PLACES if city_id == "tel-aviv" else []
        if categories:
            rows = [r for r in rows if r["category"] in categories]
        return rows[:limit]

    def recommendations(_conn, city_id=None, *, start=None, end=None, activity=None):
        asked["recommendations"].append(activity)
        if city_id != "tel-aviv":
            return []
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        # 69/fair, not 100/good: surfing carries `score_ceiling` in
        # data/activities.yml, so no stored surfing row can say `good`. A
        # fixture that still did would be testing a row the system cannot
        # produce.
        rows = [recommendation(d, "surfing", "Surfing", 69, "fair") for d in days]
        if activity:
            rows = [r for r in rows if r["activity"] == activity]
        return rows

    monkeypatch.setattr(router.queries, "places", places)
    monkeypatch.setattr(router.queries, "recommendations", recommendations)
    return router.Router(object()), asked


def agent(monkeypatch):
    monkeypatch.setenv("POSTGRES_READER_PASSWORD", "unit-test")
    from services.agent import main

    return main


# ------------------------------------------------------- reading the question ----


@pytest.mark.parametrize(
    "question",
    [
        "where can I surf in tel aviv",
        "Where should I go surfing in Tel Aviv?",
        "what is the nearest surf spot in tel aviv",
        "which beach in tel aviv",
        "which ski resorts are near tel aviv",
    ],
)
def test_a_location_question_is_recognised_as_one(stub, question):
    router_, _asked = stub
    assert stub[0].resolve(question).asks_where, question


@pytest.mark.parametrize(
    "question",
    [
        "is it good for surfing in tel aviv",
        "can I surf tomorrow in tel aviv",
        "what is the weather in tel aviv",
    ],
)
def test_a_verdict_question_is_not_mistaken_for_a_location_one(stub, question):
    assert not stub[0].resolve(question).asks_where, question


def test_somewhere_does_not_count_as_where(stub):
    """Whole-word matching, for the same reason `eat` is not inside `weather`."""
    assert not stub[0].resolve("I want to go somewhere warm in tel aviv").asks_where


def test_a_bare_where_question_is_not_read_as_asking_about_timing(stub):
    resolution = stub[0].resolve("where can I surf in tel aviv")
    assert resolution.asks_where and not resolution.asks_when
    assert resolution.where_only


@pytest.mark.parametrize(
    "question",
    [
        "where and when can I surf in tel aviv",
        "where can I surf in tel aviv tomorrow",
        "where can I surf in tel aviv this weekend",
        "where can I surf in tel aviv, and what are the conditions",
    ],
)
def test_a_where_and_when_question_asks_both(stub, question):
    resolution = stub[0].resolve(question)
    assert resolution.asks_where, question
    assert resolution.asks_when, question
    assert not resolution.where_only, question


# --------------------------------------------------------------- retrieving ----


def test_a_bare_where_question_fetches_no_weather(stub):
    """The screenshot bug, as a property: no scores were asked for, so none are
    looked up and none can lead the answer."""
    router_, asked = stub
    result = router_.retrieve("where can I surf in tel aviv")
    assert result.forecast == []
    assert result.recommendations == []
    assert asked["recommendations"] == []


def test_a_where_and_when_question_does_fetch_the_scores(stub):
    router_, _asked = stub
    result = router_.retrieve("where and when can I surf in tel aviv")
    assert len(result.recommendations) == 7


def test_a_location_question_survives_a_stale_forecast(stub, monkeypatch):
    """A surf spot does not expire when the snapshot does. The coverage gate
    refuses a weather question outside the window; refusing a location question
    for the same reason would be answering a question that was not asked."""
    stale = dict(COVERAGE, weather_first_date="2026-01-01", weather_last_date="2026-01-07")
    monkeypatch.setattr(router.queries, "coverage", lambda _conn: stale)
    result = stub[0].retrieve("where can I surf in tel aviv")
    assert result.refusal is None
    # And the timing half of the same question is still refused.
    mixed = stub[0].retrieve("where and when can I surf in tel aviv this week")
    assert mixed.refusal and "no weather data" in mixed.refusal


def test_an_activity_with_a_venue_category_is_located(stub):
    router_, asked = stub
    result = router_.retrieve("where are the museums in tel aviv")
    assert [r["name"] for r in result.venues["museums"]] == ["Tel Aviv Museum of Art"]
    assert result.unlocated_activities == []
    assert ["museum"] in asked["places"]


def test_an_activity_with_no_venue_category_is_reported_unlocated(stub):
    result = stub[0].retrieve("where can I surf in tel aviv")
    assert result.venues == {}
    assert result.unlocated_activities == ["surfing"]


def test_a_where_question_about_an_activity_does_not_fetch_the_general_place_list(stub):
    """It asked where to surf, not what else is in town. An unfiltered list of
    beaches, parks and museums in the answer's context is exactly the material
    a model turns into a surf spot."""
    router_, asked = stub
    router_.retrieve("where can I surf in tel aviv")
    assert asked["places"] == []


# ---------------------------------------------- what the source did not say ----


@pytest.mark.parametrize("activity", ["surfing", "swimming", "fishing", "boat_ride"])
def test_no_coastal_activity_is_located_from_a_beach_row(stub, activity):
    """The rule the whole route turns on, and the agent's copy of
    `test_planner_venues.test_a_beach_is_not_evidence_of_surf_swimming_fishing_or_boats`.

    Wikidata Q40080 and OSM `natural=beach` say a beach is there. They do not
    say the surf is rideable, the water lifeguarded, the angling permitted or a
    boat available. Five beach rows in the same city change none of that.
    """
    venues, unlocated = stub[0].venues_for("tel-aviv", [activity])
    assert venues == {}
    assert unlocated == [activity]


def test_the_beaches_are_on_record_so_the_gap_is_a_choice_not_an_empty_table(stub):
    """Guards the test above from passing for the wrong reason."""
    beaches = [p for p in TEL_AVIV_PLACES if p["category"] == "beach"]
    assert len(beaches) == 5
    venues, unlocated = stub[0].venues_for("tel-aviv", ["beach_day"])
    assert unlocated == []
    assert venues["beach_day"]


def test_no_beach_name_reaches_a_surf_answer(stub, monkeypatch):
    main = agent(monkeypatch)
    result = stub[0].retrieve("where can I surf in tel aviv")
    answer = main.where_answer(result)
    for beach in (p["name"] for p in TEL_AVIV_PLACES if p["category"] == "beach"):
        assert beach not in answer
    assert "Marina" not in answer


# ------------------------------------------------------------- the answer ----


def test_the_answer_to_where_is_the_gap_and_nothing_else(stub, monkeypatch):
    main = agent(monkeypatch)
    result = stub[0].retrieve("where can I surf in tel aviv")
    answer = main.where_answer(result)
    assert (
        answer
        == "I do not have a verified surf spot for Tel Aviv: no source I hold records where to do it."
    )
    # No scores, because none were asked for.
    assert "/100" not in answer


def test_the_ask_endpoint_uses_the_where_route(stub, monkeypatch):
    main = agent(monkeypatch)
    result = stub[0].retrieve("where can I surf in tel aviv")
    monkeypatch.setattr(main, "pool", type("StubPool", (), {"conn": object()})())
    monkeypatch.setattr(
        main, "Router", lambda _conn: type("R", (), {"retrieve": lambda self, _q: result})()
    )

    def never(*_a, **_k):
        raise AssertionError("the where route must not call the model")

    monkeypatch.setattr(main.client, "chat_json", never)
    response = main.ask(main.AskIn(question="where can I surf in tel aviv"))
    assert response["llm_called"] is False
    assert "verified surf spot" in response["answer"]
    assert response["rows_used"]["recommendations"] == 0
    assert "places as of 2026-09-24" in response["as_of"]
    # The footer must not claim a forecast this answer never read.
    assert "forecast covers" not in response["as_of"]


def test_a_located_answer_names_the_venue_and_its_source(stub, monkeypatch):
    main = agent(monkeypatch)
    result = stub[0].retrieve("where are the museums in tel aviv")
    answer = main.where_answer(result)
    assert "Tel Aviv Museum of Art (museum)" in answer
    assert "Wikidata (CC0)" in router.footer(result)


def test_a_where_and_when_answer_leads_with_the_place_then_labels_the_scores(stub, monkeypatch):
    main = agent(monkeypatch)
    result = stub[0].retrieve("where and when can I surf in tel aviv")
    answer = main.where_answer(result)
    lines = [line for line in answer.splitlines() if line.strip()]
    assert lines[0].startswith("I do not have a verified surf spot")
    assert "Stored suitability" in answer
    assert f"{TODAY}: Surfing is fair (69/100)." in answer
    # The caveat the scores must never appear without. It says two things: a
    # score is not a verdict on the place, and for surfing it is not a verdict
    # on the water either -- naming where the forecast was actually taken.
    assert "rate the stored weather, not the place" in answer
    assert "the Tel Aviv forecast point, 1.4 km from Gordon Beach" in answer
    assert "nothing in the data measures the waves, the swell or the water temperature" in answer


def test_an_unscored_and_unlocated_activity_says_both(stub, monkeypatch):
    """Surfing in London: no coast, so no score, and no surf spot either."""
    main = agent(monkeypatch)
    result = stub[0].retrieve("where and when can I surf in london this week")
    answer = main.where_answer(result)
    assert "I do not have a verified surf spot for London" in answer
    assert "no suitability score on record; London has no coast on record" in answer
    assert "/100" not in answer


# ------------------------------------------------------------ provenance ----


def test_an_answer_with_no_weather_in_it_does_not_claim_a_forecast(stub):
    """The footer stamps what the answer rested on. A location answer read no
    forecast, and a window under it would say otherwise."""
    result = stub[0].retrieve("where are the museums in tel aviv")
    stamp = router.footer(result)
    assert "forecast covers" not in stamp
    assert "weather as of" not in stamp
    assert "places as of 2026-09-24" in stamp
    assert "Wikidata (CC0)" in stamp


def test_a_coverage_refusal_still_stamps_the_window_it_is_about(stub):
    """A refusal for want of coverage is a statement about the forecast window,
    so the window is exactly the provenance it rests on."""
    result = stub[0].retrieve("what is the weather in tel aviv on 2027-07-04")
    assert result.refusal
    assert "forecast covers" in router.footer(result)


# --------------------------------------------------------- wording helpers ----


@pytest.mark.parametrize(
    "activity, expected",
    [
        ("surfing", "surf spot"),
        ("swimming", "supervised swimming spot"),
        ("fishing", "fishing spot"),
        ("boat_ride", "boat hire"),
        ("museums", "museum"),
        ("beach_day", "beach"),
        # No `where_noun` and no venue category: built from the label, with the
        # article stripped so it reads as a noun.
        ("running", "place for running"),
        ("standup_comedy", "place for stand-up comedy show"),
    ],
)
def test_the_noun_an_answer_uses_for_a_place(activity, expected):
    assert router.where_noun(activity) == expected


def test_the_two_gaps_are_worded_differently(stub):
    """The two gaps are different statements, and must not be blurred: "this
    city has none" is not "no source records these anywhere"."""
    no_source = router.where_gap("surfing", "Tel Aviv")
    none_here = router.where_gap("museums", "Tel Aviv")
    assert "no source I hold records where to do it" in no_source
    assert none_here == "I have no museum on record for Tel Aviv."
