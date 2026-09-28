"""An activity the traveller types, and the score it may not be given.

The failure these close was reproduced five times across two people. Asked

    Is tomorrow a good day for paragliding in Rome?

the agent answered

    Tomorrow, 2026-09-27, is a good day for paragliding in Rome. ... The
    suitability score for paragliding is good (100/100).

`paragliding` appears nowhere in data/activities.yml. There is no paragliding
rule, no paragliding row and no score of any kind for it: the 100/100 was the
`beach_day` row's, relabelled. Two headline claims of the submission -- "nothing
invented" and "the rule engine is the source of truth, not the model" -- were
falsifiable in one question a reviewer is likely to ask.

The cause was upstream of every check. An out-of-catalogue noun left
`Resolution.activities` empty, so the retrieval applied no activity filter and
handed the model one row per catalogue activity; check 4 stayed quiet because
verdicts existed, check 5 stayed quiet because `brief.unscored` is derived from
`Resolution.activities`, check 8 cannot fire on a word the traveller typed
(`vocabulary()` includes the question), and 100 is allowed unconditionally.

So the tests here come in two halves, and both are needed. The router half
proves the noun becomes a slug and lands in the right one of three places. The
grounding half proves the sentence is rejected if it is written anyway.

Two things must NOT happen, and each has its own test: an out-of-catalogue noun
must not be refused the way an unknown city is -- a previously requested
activity has real stored rows and a real generic score -- and open wording that
names no activity at all must keep its ordinary answer.

The clock is frozen and every date is written out, so these keep meaning
something after the staged forecast window has expired.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from services.agent import dates, grounding, planning, router
from services.enricher import main as enricher

TODAY = date(2026, 9, 26)
TOMORROW = date(2026, 9, 27)

ROME = {
    "id": "rome",
    "name": "Rome",
    "country": "Italy",
    "timezone": "Europe/Rome",
    "aliases": ["roma"],
    "coastal": True,
}
LISBON = {
    "id": "lisbon",
    "name": "Lisbon",
    "country": "Portugal",
    "timezone": "Europe/Lisbon",
    "aliases": ["lisboa"],
    "coastal": True,
}
LONDON = {
    "id": "london",
    "name": "London",
    "country": "United Kingdom",
    "timezone": "Europe/London",
    "aliases": [],
    "coastal": False,
}
COVERAGE = {
    "cities": [LONDON, LISBON, ROME],
    "weather_first_date": "2026-09-24",
    "weather_last_date": "2026-10-09",
    "weather_as_of": "2026-09-24",
}

# The four the answer could have stolen a number from. `beach_day` is the one it
# actually took, and it is the one that reaches 100 -- the four capped sea
# activities cannot (`score_ceiling: 69`), which is why a fabricated "good
# (100/100)" can only have come from a land row.
CATALOGUE = (
    ("beach_day", "A day at the beach", 100, "good"),
    ("sightseeing", "Sightseeing on foot", 88, "good"),
    ("running", "Running", 61, "fair"),
    ("surfing", "Surfing", 69, "fair"),
)


def forecast_row(day: date) -> dict[str, Any]:
    return {
        "forecast_date": day,
        "provider": "open-meteo",
        "temp_max_c": 27.0,
        "temp_min_c": 14.0,
        "precip_mm": 0.0,
        "precip_prob": 0,
        "wind_kmh": 11.0,
        "sunshine_hours": 11.8,
        "as_of": "2026-09-24",
    }


def verdict_row(
    day: date,
    activity: str,
    label: str,
    score: int,
    band: str,
    *,
    requested: bool = False,
    reasons: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "forecast_date": day,
        "activity": activity,
        "activity_label": label,
        "score": score,
        "band": band,
        "text": None,
        "status": "ready",
        "requested": requested,
        "reasons": list(reasons),
    }


@pytest.fixture
def stub(monkeypatch):
    """A Router over canned rows, and a record of what it asked the database.

    No database, no model, no clock. `rows` is mutable so a test can seed a
    previously requested activity before it asks. The `recommendations` stub
    honours `activity=` because the repair probes with it -- a stub that ignored
    the argument would be testing a query the system does not make.
    """
    monkeypatch.setattr(dates, "today_in", lambda _timezone: TODAY)
    monkeypatch.setattr(router.queries, "cities", lambda _conn: [LONDON, LISBON, ROME])
    monkeypatch.setattr(router.queries, "coverage", lambda _conn: COVERAGE)
    monkeypatch.setattr(router.queries, "forecast", lambda *_a, **_k: [forecast_row(TOMORROW)])
    monkeypatch.setattr(router.queries, "events", lambda *_a, **_k: [])
    monkeypatch.setattr(
        router.queries, "expired_events", lambda *_a, **_k: {"expired": 0, "last_checked": None}
    )
    monkeypatch.setattr(router.queries, "facts", lambda *_a, **_k: [])
    monkeypatch.setattr(router.queries, "places", lambda *_a, **_k: [])

    rows = [verdict_row(TOMORROW, *row) for row in CATALOGUE]
    asked: dict[str, list] = {"recommendations": []}

    def recommendations(_conn, city_id=None, *, start=None, end=None, activity=None):
        asked["recommendations"].append(activity)
        out = rows
        if activity:
            out = [r for r in out if r["activity"] == activity]
        return out

    monkeypatch.setattr(router.queries, "recommendations", recommendations)
    return router.Router(object()), rows, asked


def agent(monkeypatch, result):
    """The handler with its pool and router replaced by this one retrieval."""
    monkeypatch.setenv("POSTGRES_READER_PASSWORD", "unit-test")
    from services.agent import main

    monkeypatch.setattr(main, "pool", type("StubPool", (), {"conn": object()})())
    monkeypatch.setattr(
        main,
        "Router",
        lambda _conn: type("StubRouter", (), {"retrieve": lambda self, _q: result})(),
    )
    return main


# ------------------------------- RC1: paragliding in Rome, never requested ----

PARAGLIDING = "Is tomorrow a good day for paragliding in Rome?"


def test_an_out_of_catalogue_noun_becomes_a_slug_and_is_recorded_unscored(stub):
    route, _rows, asked = stub
    result = route.retrieve(PARAGLIDING)

    assert result.resolution.unknown_activities == ["paragliding"]
    assert "paragliding" in result.unscored_activities
    # It is not pretended to be a catalogue activity, and it did not silently
    # become one of the four rows the answer used to steal a number from.
    assert result.resolution.activities == []
    assert "paragliding" in asked["recommendations"], "the slug was never looked up"


def test_the_paragliding_gap_is_stated_and_names_the_route_that_would_score_it(stub):
    route, _rows, _asked = stub
    brief = grounding.build(route.retrieve(PARAGLIDING))

    gaps = [g for g in brief.gaps if g.subject == "activity:paragliding"]
    assert len(gaps) == 1, [g.subject for g in brief.gaps]
    text = gaps[0].text
    assert "not on record" in text
    # Requirement 3: the supported workflow stays discoverable. A refusal would
    # have stranded `rules.score_requested`, which exists and works.
    assert "general outdoor comfort" in text
    assert "POST /recommendations" in text

    block = grounding.prompt_block(brief)
    assert "ASKED ABOUT BUT NOT ON RECORD" in block
    assert "paragliding" in block
    assert "Do not give a verdict on these" in block


@pytest.mark.parametrize(
    "answer",
    [
        # The answer that was actually returned, five times out of five.
        "The suitability score for paragliding is good (100/100).",
        "Tomorrow is a good day for paragliding in Rome.",
        "The wind is light, so paragliding is good tomorrow.",
        "Paragliding is ideal for tomorrow given the clear skies.",
    ],
)
def test_a_verdict_on_the_typed_activity_is_rejected(stub, answer):
    route, _rows, _asked = stub
    brief = grounding.build(route.retrieve(PARAGLIDING))

    found = grounding.violations(answer, brief)
    assert any("paragliding" in v for v in found), found


def test_the_fabricated_score_never_reaches_the_traveller(monkeypatch, stub):
    """The end-to-end property: whatever the model would write, the answer the
    traveller reads carries no number for paragliding at all.

    This used to be proved through the grounding guard -- the model was called,
    it wrote the fabricated verdict, check 5 threw the wording away and `respond`
    attached a `note`. It is now proved one step earlier and one step harder: a
    question whose only named activity has no score is answered in code, so the
    model is never asked. `llm_called is False` is the assertion that says so,
    and it is strictly stronger than catching the wording afterwards, because
    there is no wording to catch.

    The stub below stays in place deliberately. If this route ever falls back to
    the model again, `chat_json` is there to write the fabrication and the
    assertions below will fail on it rather than quietly passing.
    """
    route, _rows, _asked = stub
    result = route.retrieve(PARAGLIDING)
    main = agent(monkeypatch, result)

    monkeypatch.setattr(
        main.client,
        "chat_json",
        lambda *_a, **_k: {
            "answer": "Tomorrow, 2026-09-27, is a good day for paragliding in Rome. "
            "The suitability score for paragliding is good (100/100)."
        },
    )
    response = main.ask(main.AskIn(question=PARAGLIDING))

    assert response["llm_called"] is False, "the model was asked to phrase a gap"
    # The specific regression: no number of any kind beside the word.
    for line in response["answer"].splitlines():
        if "paragliding" in line.lower():
            assert not any(char.isdigit() for char in line), line
    assert "score for paragliding" not in response["answer"]
    assert "no suitability score on record" in response["answer"]
    # And no other activity's number was substituted for the missing one --
    # `beach_day`'s real 100/100 is where the stolen number came from.
    assert "100/100" not in response["answer"]


def test_the_grounding_guard_still_rejects_the_wording_it_was_written_for(stub):
    """The guard is now a second line rather than the first, and it must stay
    armed. `grounding.violations` is what protects every route that DOES reach
    the model, so the checks are asserted directly here even though `ask()` no
    longer depends on them for this question."""
    route, _rows, _asked = stub
    brief = grounding.build(route.retrieve(PARAGLIDING))

    found = grounding.violations("The suitability score for paragliding is good (100/100).", brief)
    assert any("paragliding" in v for v in found), found


# ------------------------------------- RC2: scuba diving in a coastal city ----

SCUBA = "Is it a good day for scuba diving in Lisbon tomorrow?"


def test_a_sea_activity_the_catalogue_lacks_is_not_resolved_to_one_it_has(stub):
    """Lisbon is coastal, so the tempting fabrication is "coastal, therefore a
    sea activity is fine". It must not become swimming or surfing either."""
    route, _rows, _asked = stub
    result = route.retrieve(SCUBA)

    assert result.resolution.unknown_activities == ["scuba_diving"]
    assert "scuba_diving" in result.unscored_activities
    assert result.resolution.activities == []
    assert "swimming" not in result.unscored_activities
    assert "surfing" not in result.unscored_activities


@pytest.mark.parametrize(
    "answer",
    [
        "Scuba diving is suitable in Lisbon tomorrow.",
        "The water is calm, so scuba diving is good for tomorrow.",
        "Visibility is fine for scuba diving.",
    ],
)
def test_no_sea_claim_is_allowed_for_the_unscored_activity(stub, answer):
    route, _rows, _asked = stub
    brief = grounding.build(route.retrieve(SCUBA))
    assert grounding.violations(answer, brief), answer


SCUBA_INLAND = "Is it a good day for scuba diving in London tomorrow?"


def test_a_typed_sea_activity_inland_is_told_why_there_is_no_score(monkeypatch, stub):
    """ "No suitability score on record" is true but incomplete, and the missing
    half is the useful one. "surfing in London" has always been told that London
    has no coast on record, because `requires_coast` is on surfing's catalogue
    row; "scuba diving in London" was told only that no score existed, because
    no catalogue row carries it. Both are decided by the same fact about the
    city."""
    route, _rows, _asked = stub
    result = route.retrieve(SCUBA_INLAND)
    main = agent(monkeypatch, result)
    response = main.ask(main.AskIn(question=SCUBA_INLAND))

    answer = response["answer"]
    assert "scuba diving: no suitability score on record; London has no coast on record." in answer
    assert response["llm_called"] is False


def test_the_two_wording_defects_the_model_produced_here_are_unreachable(monkeypatch, stub):
    """The live `aow-demo` answer to this question inferred a sea state from a
    land forecast -- "a 7.8mm rainfall and a 9km/h wind, which may affect diving
    conditions", the inference `services/common/coast.py` forbids -- and framed
    an activity as an event, "scuba diving is not scheduled for this date".
    Neither is a borrowed score, and both were visible to a reviewer.

    They came from the model, and the model is no longer asked: a question whose
    only named activity has no score is answered in code. Rather than add two
    more grounding checks for two more phrasings, the route that produced them
    is gone.
    """
    route, _rows, _asked = stub
    result = route.retrieve(SCUBA_INLAND)
    main = agent(monkeypatch, result)
    response = main.ask(main.AskIn(question=SCUBA_INLAND))

    answer = response["answer"].lower()
    assert "diving conditions" not in answer
    assert "not scheduled" not in answer
    assert "rainfall" not in answer
    assert "km/h" not in answer


# ---------------------------------------------- RC3: an ordinary activity ----


def test_a_catalogue_activity_is_unchanged(stub):
    """The no-regression case, and the one a stricter check 4 would have
    endangered."""
    route, _rows, asked = stub
    result = route.retrieve("Is it a good day for running in Rome tomorrow?")

    assert result.resolution.activities == ["running"]
    assert result.resolution.unknown_activities == []
    assert result.unscored_activities == []
    assert asked["recommendations"] == ["running"]
    assert [row["activity"] for row in result.recommendations] == ["running"]
    assert result.recommendations[0]["score"] == 61
    assert result.recommendations[0]["band"] == "fair"


def test_the_named_activity_answer_still_renders_from_its_own_row(monkeypatch, stub):
    route, _rows, _asked = stub
    result = route.retrieve("Is it a good day for running in Rome tomorrow?")
    main = agent(monkeypatch, result)

    def fail(*_a, **_k):
        raise AssertionError("a named activity is answered from its rows, not by the model")

    monkeypatch.setattr(main.client, "chat_json", fail)
    response = main.ask(main.AskIn(question=result.resolution.question))

    assert response["llm_called"] is False
    assert f"{TOMORROW}: Running is fair (61/100)." in response["answer"]
    # No generic caveat: this activity has a rule of its own.
    assert "asked for this activity by name" not in response["answer"]


def test_the_new_name_keyed_check_does_not_fire_on_a_retrieved_activity(stub):
    route, _rows, _asked = stub
    brief = grounding.build(route.retrieve("Is it a good day for running in Rome tomorrow?"))
    assert grounding.violations(f"Running is fair on {TOMORROW}, at 61 out of 100.", brief) == []


def test_a_verdict_on_a_catalogue_activity_with_no_retrieved_row_is_rejected(stub):
    """Check 10's own case. `hiking` is in the catalogue, so it is not an
    unknown noun and check 5 never sees it -- but no row for it came back, so
    the model scoring it is scoring from its weights."""
    route, _rows, _asked = stub
    brief = grounding.build(route.retrieve("What should I do in Rome tomorrow?"))

    assert brief.verdicts, "the check is deliberately silent when nothing was scored"
    found = grounding.violations("Hiking is ideal for tomorrow in Rome.", brief)
    assert any("hiking" in v for v in found), found


# -------------------------------- RC4: an activity a request already scored ----

KITE = "Is tomorrow a good day for kite flying in Rome?"
GENERIC_REASON = "scored against general outdoor comfort, not a rule tuned for this activity"


def test_a_previously_requested_activity_resolves_to_its_own_stored_rows(stub):
    """The case a blanket refusal would have got wrong. `POST /recommendations`
    stores a real row for a typed activity and `queries.recommendations` never
    filters on `requested`, so the row was always there -- the router simply
    could not see it."""
    route, rows, asked = stub
    rows.append(
        verdict_row(
            TOMORROW,
            "kite_flying",
            "kite flying",
            64,
            "fair",
            requested=True,
            reasons=(GENERIC_REASON,),
        )
    )
    result = route.retrieve(KITE)

    assert result.resolution.activities == ["kite_flying"]
    assert result.resolution.unknown_activities == []
    assert "kite_flying" not in result.unscored_activities
    # Twice, and both are the narrowed query: once to find out whether the slug
    # has a row at all, once by the retrieval. Never the unfiltered read that
    # used to hand the model one row per catalogue activity.
    assert asked["recommendations"] == ["kite_flying", "kite_flying"]
    assert [row["activity"] for row in result.recommendations] == ["kite_flying"]
    # Its own score, not the 100 sitting on the beach_day row beside it.
    assert result.recommendations[0]["score"] == 64
    assert result.recommendations[0]["band"] == "fair"


def test_a_requested_activity_is_answered_with_its_generic_label(monkeypatch, stub):
    route, rows, _asked = stub
    rows.append(
        verdict_row(
            TOMORROW,
            "kite_flying",
            "kite flying",
            64,
            "fair",
            requested=True,
            reasons=(GENERIC_REASON,),
        )
    )
    result = route.retrieve(KITE)
    main = agent(monkeypatch, result)

    def fail(*_a, **_k):
        raise AssertionError("a resolved activity is answered from its rows")

    monkeypatch.setattr(main.client, "chat_json", fail)
    response = main.ask(main.AskIn(question=KITE))

    assert response["llm_called"] is False
    assert f"{TOMORROW}: kite flying is fair (64/100)." in response["answer"]
    # Requirement 4: the generic measure is labelled where its score is shown.
    # It was on the row and reached no screen at all before this.
    assert GENERIC_REASON in response["answer"]
    assert "not on record" not in response["answer"]
    assert "100/100" not in response["answer"]


# ------------------------------------------- RC5: open wording, no activity ----


def test_open_wording_keeps_its_ordinary_answer(stub):
    """The false-positive guard, and the direct test of why check 4 is left as
    an emptiness test. `activities` is the router's fallback intent, so a
    verdict-word-keyed tightening would reject this."""
    route, _rows, _asked = stub
    result = route.retrieve("Is tomorrow a good day to be outside in Rome?")

    assert result.resolution.unknown_activities == []
    assert result.unscored_activities == []
    assert len(result.recommendations) == len(CATALOGUE)

    brief = grounding.build(result)
    assert not [g for g in brief.gaps if g.subject.startswith("activity:")]
    answer = (
        f"Tomorrow, {TOMORROW}, Rome has a high of 27C and 11.8 hours of sunshine. "
        f"A day at the beach is good, at 100 out of 100."
    )
    assert grounding.violations(answer, brief) == []


@pytest.mark.parametrize(
    "question",
    [
        "What is the weather tomorrow in Rome?",
        "Where should I go for dinner in Rome?",
        "Tell me about the history of Lisbon",
        "Which concerts are scheduled in London this week?",
        "What markets are on in London this week?",
    ],
)
def test_a_question_that_names_no_activity_produces_no_unknown_activity(stub, question):
    route, _rows, _asked = stub
    assert route.retrieve(question).resolution.unknown_activities == []


# ---------------------------------- R2: an activity is not a scheduled event ----


def test_the_verdicts_prompt_header_says_these_are_not_events(stub):
    """The asymmetry that made the assignment's own Rome question fall back in
    half its runs: the PLACES block carried a negative from the start and the
    verdicts block carried none, while one of the eighteen catalogue labels is
    literally "An open-air music festival"."""
    route, _rows, _asked = stub
    block = grounding.prompt_block(
        grounding.build(route.retrieve("What can I do in Rome tomorrow?"))
    )

    header = next(line for line in block.splitlines() if line.startswith("Suitability scores"))
    assert "not a thing that is scheduled" in header.lower() or "None of them is a thing" in header
    assert "attend" in header


def test_a_suitability_row_narrated_as_a_scheduled_event_is_rejected(stub, monkeypatch):
    route, rows, _asked = stub
    rows.append(verdict_row(TOMORROW, "music_festival", "An open-air music festival", 85, "good"))
    brief = grounding.build(route.retrieve("What can I do in Rome tomorrow?"))

    found = grounding.violations(
        f"On {TOMORROW} it is a good day to attend the open-air music festival.", brief
    )
    assert any("scheduled" in v for v in found), found


def test_the_same_row_offered_as_an_activity_is_allowed(stub):
    """The check must not cost a correct answer its wording."""
    route, rows, _asked = stub
    rows.append(verdict_row(TOMORROW, "music_festival", "An open-air music festival", 85, "good"))
    brief = grounding.build(route.retrieve("What can I do in Rome tomorrow?"))

    answer = f"On {TOMORROW} an open-air music festival rates good, at 85 out of 100."
    assert grounding.violations(answer, brief) == []


@pytest.mark.parametrize(
    "text",
    [
        # The wording that reached the trip planner, unchecked.
        "A good day to attend the open-air music festival with mild temperatures.",
        "The market is taking place under clear skies.",
        "Tickets are worth having for this one, with light wind all day.",
    ],
)
def test_the_enricher_refuses_to_say_a_suitability_row_is_on(text):
    with pytest.raises(enricher.LlmInvalidOutput, match="scheduled event"):
        enricher.validate_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "A pleasant day for a boat ride, with clear skies and mild temperatures.",
        "There is a 10% chance of rain, so it is still a reasonable day for a walk.",
        "The temperature is on the mild side and the wind is light.",
        "A fine open-air music festival day: clear skies and 11 hours of sunshine.",
    ],
)
def test_the_enricher_still_accepts_ordinary_wording(text):
    assert enricher.validate_text(text) == text


def test_the_planner_falls_back_to_the_rule_engine_reasons(stub):
    """The planner repeats the enricher's stored sentence verbatim, so a row
    worded before this guard existed still reaches a traveller. It gets the
    reasons the score was actually computed from instead."""
    scheduled = {
        "activity": "music_festival",
        "activity_label": "An open-air music festival",
        "score": 85,
        "band": "good",
        "status": "ready",
        "text": "A good day to attend the open-air music festival.",
        "reasons": ["clear skies", "mild temperatures"],
    }
    assert planning.grounded_why(scheduled) == "clear skies; mild temperatures"

    ordinary = {**scheduled, "text": "A fine day for the open-air music festival."}
    assert planning.grounded_why(ordinary) == "A fine day for the open-air music festival."

    suggestions = planning.plan_day([scheduled], {}, set(), {}, varied=True)
    assert suggestions[0]["why"] == "clear skies; mild temperatures"


# ------------------------------------- R3: the traveller's own descriptor ----

E2 = (
    "What activities can I do with my wife this week in London? "
    "We like concerts, shopping and fine dining."
)
RESTAURANTS = (
    "68-86 Bar and Restaurant",
    "Akoko",
    "Alain Ducasse at The Dorchester",
    "Bar Italia",
)


@pytest.fixture
def e2(monkeypatch):
    """The assignment's own London question, over four `category=restaurant`
    rows and nothing else. None of them carries a tier, a price band or a
    rating -- Bar Italia is a 24-hour Soho cafe."""
    monkeypatch.setattr(dates, "today_in", lambda _timezone: TODAY)
    monkeypatch.setattr(router.queries, "cities", lambda _conn: [LONDON])
    monkeypatch.setattr(router.queries, "coverage", lambda _conn: COVERAGE)
    monkeypatch.setattr(router.queries, "forecast", lambda *_a, **_k: [forecast_row(TOMORROW)])
    monkeypatch.setattr(router.queries, "events", lambda *_a, **_k: [])
    monkeypatch.setattr(
        router.queries, "expired_events", lambda *_a, **_k: {"expired": 0, "last_checked": None}
    )
    monkeypatch.setattr(router.queries, "facts", lambda *_a, **_k: [])
    monkeypatch.setattr(
        router.queries,
        "recommendations",
        lambda *_a, **_k: [verdict_row(TOMORROW, *row) for row in CATALOGUE[:3]],
    )
    monkeypatch.setattr(
        router.queries,
        "places",
        lambda *_a, **_k: [
            {
                "id": f"wd:{name.lower().replace(' ', '-')}",
                "name": name,
                "category": "restaurant",
                "is_sample": False,
                "source": "Wikidata (CC0)",
            }
            for name in RESTAURANTS
        ],
    )
    return grounding.build(router.Router(object()).retrieve(E2))


def test_a_category_only_row_may_not_be_called_fine_dining(e2):
    """The answer the live agent gave, verbatim. Its own prompt forbade it and
    no check enforced it: "is available at" is not a description word, and the
    phrase is in the traveller's question, so it is in `allowed_names()`."""
    found = grounding.violations(
        "Fine dining is available at 68-86 Bar and Restaurant, Akoko, "
        "Alain Ducasse at The Dorchester, and Bar Italia.",
        e2,
    )
    assert any("Bar Italia" in v for v in found), found
    assert any("restaurant" in v for v in found), found


def test_offering_the_same_rows_as_restaurants_on_record_is_allowed(e2):
    """The phrasing the prompt now asks for, and the false-positive guard: a
    clause scoped to the record passes."""
    answer = (
        "For dining, the restaurants on record are 68-86 Bar and Restaurant, Akoko, "
        "Alain Ducasse at The Dorchester and Bar Italia."
    )
    assert grounding.violations(answer, e2) == []


@pytest.mark.parametrize("descriptor", ["upscale", "gourmet", "michelin", "romantic"])
def test_no_quality_tier_may_be_attached_to_a_place(e2, descriptor):
    found = grounding.violations(f"Akoko is an {descriptor} choice for the evening.", e2)
    assert any("Akoko" in v for v in found), found


def test_the_places_prompt_forbids_echoing_the_descriptor_back(e2):
    block = grounding.prompt_block(e2)
    assert "do not repeat the traveller's own description back onto one" in block
    assert "restaurants on record" in block


# ------------------------------------------------- the extraction, on its own ----


@pytest.mark.parametrize(
    "question, expected",
    [
        ("Is tomorrow a good day for paragliding in Rome?", ["paragliding"]),
        ("Is it a good day for scuba diving in Lisbon?", ["scuba diving"]),
        ("is tomorrow a good day for kite flying in Rome?", ["kite flying"]),
        ("where can I paraglide in Rome?", ["paraglide"]),
        # Open wording: "be" ends the phrase, so nothing is extracted at all.
        ("Is tomorrow a good day to be outside in Rome?", []),
        ("Where should I go for dinner in Rome?", []),
        ("What is the weather tomorrow in Rome?", []),
        ("Tell me about the history of Lisbon", []),
        (E2.lower(), []),
    ],
)
def test_the_candidate_phrases_a_question_yields(question, expected):
    """The one judgement call in the repair, tested as a list so it can be
    reviewed as one. A false positive here costs a single extra truthful "not on
    record" line and can never cost a score or an answer; that asymmetry is why
    this is preferred to refusing an unknown noun."""
    assert router._candidate_phrases(question.lower()) == expected


def test_a_typed_noun_normalises_the_same_way_the_write_path_does(stub):
    """One normalisation rule for both routes. `POST /recommendations` slugifies
    with `schemas.slugify`, and a read that used a different rule could not find
    the row the write created."""
    from services.common.schemas import slugify

    route, rows, _asked = stub
    rows.append(verdict_row(TOMORROW, slugify("Kite Flying!"), "Kite Flying!", 64, "fair"))
    assert route.retrieve(
        "is tomorrow a good day for kite flying in Rome?"
    ).resolution.activities == ["kite_flying"]


def test_a_failed_probe_reports_a_gap_rather_than_a_score(monkeypatch, stub):
    """Fail-soft in the safe direction. If the existence probe cannot be
    answered the noun is reported as not on record, which is a worse answer and
    never a wrong one."""
    route, _rows, _asked = stub

    def broken(*_a, **_k):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(router.queries, "recommendations", broken)
    resolution = route.resolve(PARAGLIDING)
    assert resolution.unknown_activities == ["paragliding"]
    assert resolution.activities == []
