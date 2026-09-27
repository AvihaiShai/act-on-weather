"""Three ways a named-activity answer said less than it knew.

All three were found by reading `docs/DEMO-SCRIPT-2026-09-28.md` §5, which lists
them as live hazards to be steered around during a demo (H2, H6, H7) rather than
as behaviour to rely on. Each is reproduced here first and then pinned.

H2 -- **a named-activity answer dropped the days it had no data for.** `ask()`
appends `grounding.gap_block` after the model has spoken, but the named-activity
route returns in code *before* that line. So "is 2026-10-05 to 2026-10-12 good
for surfing in Tel Aviv?" listed 10-05 to 10-08, stopped, and said nothing about
the four days past the stored forecast -- while `respond` still reported the full
requested window in `dates`. The route that exists to be more truthful than the
model was the one that quietly truncated.

H6 -- **a three-letter noun was discarded rather than reported.** The typed
extraction dropped any slug shorter than four characters, an undocumented and
untested `4`. "Is tomorrow a good day to ski in Reykjavik?" therefore resolved no
activity at all, which removes the activity filter from the retrieval and hands
the model one row per catalogue activity. That is precisely the paragliding
failure `test_typed_activities.py` was written to close, re-opened by a spelling:
`ski` is three characters, `skiing` is six, and only the longer one got a gap.

H7 -- **a stored custom activity lost to a keyword inside its own name.** The UI
ships "kite surfing" as its own placeholder. The write path slugifies it to
`kite_surfing` and scores it; the read path saw the whole word "surfing", matched
the catalogue, and answered with surfing's score under surfing's label. Two
different activities, one of them the wrong one.

The clock is frozen, the coverage window ends on 2026-10-08 -- the real staged
horizon -- and every date is written out, so these keep meaning something after
the snapshot expires.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from services.agent import dates, grounding, router

# The staged snapshot's real horizon (data/snapshot/MANIFEST.json), so the
# window below straddles it the way a demo question would.
HORIZON = date(2026, 10, 8)
TODAY = date(2026, 10, 5)
TOMORROW = date(2026, 10, 6)
STORED = [date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7), HORIZON]
PAST_HORIZON = ["2026-10-09", "2026-10-10", "2026-10-11", "2026-10-12"]

TEL_AVIV = {
    "id": "tel_aviv",
    "name": "Tel Aviv",
    "country": "Israel",
    "timezone": "Asia/Jerusalem",
    "aliases": ["tel-aviv", "telaviv"],
    "coastal": True,
}
REYKJAVIK = {
    "id": "reykjavik",
    "name": "Reykjavik",
    "country": "Iceland",
    "timezone": "Atlantic/Reykjavik",
    "aliases": [],
    "coastal": True,
}
CITIES = [REYKJAVIK, TEL_AVIV]
COVERAGE = {
    "cities": CITIES,
    "weather_first_date": "2026-09-23",
    "weather_last_date": HORIZON.isoformat(),
    "weather_as_of": "2026-09-23",
}

# One land row that reaches 100 and one capped sea row, so a borrowed score is
# identifiable by its number alone.
CATALOGUE = (
    ("beach_day", "A day at the beach", 100, "good"),
    ("surfing", "Surfing", 69, "fair"),
    ("hiking", "Hiking", 74, "good"),
)


def forecast_row(day: date) -> dict[str, Any]:
    return {
        "forecast_date": day,
        "provider": "open-meteo",
        "temp_max_c": 24.0,
        "temp_min_c": 15.0,
        "precip_mm": 0.0,
        "precip_prob": 0,
        "wind_kmh": 11.0,
        "sunshine_hours": 9.0,
        "as_of": "2026-09-23",
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
    """A Router over canned rows, and the activity filters it asked for.

    No database, no model, no clock. `rows` is mutable so a test can seed a
    previously requested activity before asking, and both the forecast and the
    recommendation stubs honour `start`/`end` -- a stub that ignored them could
    not show a window being clipped at the horizon, which is the whole of H2.
    """
    monkeypatch.setattr(dates, "today_in", lambda _timezone: TODAY)
    monkeypatch.setattr(router.queries, "cities", lambda _conn: CITIES)
    monkeypatch.setattr(router.queries, "coverage", lambda _conn: COVERAGE)
    monkeypatch.setattr(router.queries, "events", lambda *_a, **_k: [])
    monkeypatch.setattr(
        router.queries, "expired_events", lambda *_a, **_k: {"expired": 0, "last_checked": None}
    )
    monkeypatch.setattr(router.queries, "facts", lambda *_a, **_k: [])
    monkeypatch.setattr(router.queries, "places", lambda *_a, **_k: [])

    def forecast(_conn, _city=None, *, start=None, end=None):
        return [
            forecast_row(day)
            for day in STORED
            if (start is None or day >= start) and (end is None or day <= end)
        ]

    monkeypatch.setattr(router.queries, "forecast", forecast)

    rows = [verdict_row(day, *row) for day in STORED for row in CATALOGUE]
    asked: list[str | None] = []

    def recommendations(_conn, _city=None, *, start=None, end=None, activity=None):
        asked.append(activity)
        out = [
            row
            for row in rows
            if (start is None or row["forecast_date"] >= start)
            and (end is None or row["forecast_date"] <= end)
        ]
        if activity:
            out = [row for row in out if row["activity"] == activity]
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


def ask(monkeypatch, route, question):
    result = route.retrieve(question)
    main = agent(monkeypatch, result)
    return result, main.ask(main.AskIn(question=question))


# --------------- H2: the days a named-activity answer has no data for ----


# Deliberately a named activity AND a window running four days past the stored
# horizon. The demo script phrases its equivalent as a weather question
# specifically to avoid this route.
SURFING_PAST_HORIZON = "Is 2026-10-05 to 2026-10-12 good for surfing in Tel Aviv?"


def test_the_window_really_does_run_past_the_stored_forecast(stub):
    """The premise, asserted rather than assumed: the question is not refused,
    and the days past the horizon are recorded as uncovered."""
    route, _rows, _asked = stub
    result = route.retrieve(SURFING_PAST_HORIZON)

    assert result.refusal is None, "a partly covered window must not be refused"
    assert result.resolution.activities == ["surfing"]
    assert result.uncovered_days == PAST_HORIZON
    # The rows really are clipped -- this is what the answer used to report as
    # though it were the whole question.
    assert {str(row["forecast_date"]) for row in result.recommendations} == {
        "2026-10-05",
        "2026-10-06",
        "2026-10-07",
        "2026-10-08",
    }


def test_a_named_activity_answer_states_the_dates_it_has_no_data_for(monkeypatch, stub):
    route, _rows, _asked = stub
    _result, response = ask(monkeypatch, route, SURFING_PAST_HORIZON)

    answer = response["answer"]
    # Consecutive days are collapsed into one span by `grounding.date_runs`, so
    # the guarantee is that the whole uncovered stretch is bounded and named --
    # first day, last day, and the reason.
    assert f"{PAST_HORIZON[0]} to {PAST_HORIZON[-1]}" in answer, answer
    assert "No weather is stored" in answer
    assert "The stored forecast ends on 2026-10-08" in answer
    # And it is still the code-rendered route: the model is never asked to
    # phrase a verdict it could round off into a "good week".
    assert response["llm_called"] is False
    for day in ("2026-10-05", "2026-10-08"):
        assert f"- {day}: Surfing is fair (69/100)." in answer


def test_the_reported_window_and_the_answer_no_longer_disagree(monkeypatch, stub):
    """`respond` puts the requested window in `dates`. While the answer listed
    only the covered days, that field claimed four days the answer did not
    cover and nothing on screen said so."""
    route, _rows, _asked = stub
    _result, response = ask(monkeypatch, route, SURFING_PAST_HORIZON)

    assert response["dates"] == "2026-10-05 to 2026-10-12"
    assert "2026-10-12" in response["answer"]


def test_a_fully_covered_named_activity_answer_gains_nothing(monkeypatch, stub):
    """The guard on the repair. An ordinary in-coverage question must read
    exactly as it did before -- no gap sentence, no extra date line."""
    route, _rows, _asked = stub
    result, response = ask(monkeypatch, route, "Is tomorrow good for surfing in Tel Aviv?")
    main = agent(monkeypatch, result)

    assert result.uncovered_days == []
    assert response["answer"] == main.named_activity_answer(result)
    assert "Not on record" not in response["answer"]


def test_a_day_with_a_score_is_never_denied_weather(monkeypatch, stub):
    """A day can hold a stored score while its forecast row is gone. The
    appended gap must not then deny weather for a date listed with a score
    directly above it -- that reads as a contradiction, not as a limit."""
    route, rows, _asked = stub
    result = route.retrieve(SURFING_PAST_HORIZON)
    # Surfing scored on every requested day, forecast still only to the horizon.
    result.recommendations = result.recommendations + [
        verdict_row(date.fromisoformat(day), "surfing", "Surfing", 69, "fair")
        for day in PAST_HORIZON
    ]
    main = agent(monkeypatch, result)
    answer = main.ask(main.AskIn(question=SURFING_PAST_HORIZON))["answer"]

    assert "No weather is stored" not in answer
    assert "- 2026-10-12: Surfing is fair (69/100)." in answer
    assert rows  # the fixture's own rows are untouched


def test_a_stored_day_the_activity_has_no_score_on_is_named(monkeypatch, stub):
    """The gap between the two existing channels: the city has weather for the
    day, and this activity has no row on it. `unscored_activities` only covers
    an activity with no row anywhere."""
    route, rows, _asked = stub
    # Kite surfing scored on two of the four stored days.
    rows.extend(
        verdict_row(day, "kite_surfing", "Kite surfing", 42, "poor", requested=True)
        for day in STORED[:2]
    )
    _result, response = ask(
        monkeypatch, route, "Is 2026-10-05 to 2026-10-08 good for kite surfing in Tel Aviv?"
    )

    answer = response["answer"]
    assert "- 2026-10-05: Kite surfing is poor (42/100)." in answer
    assert (
        "- Kite surfing: no suitability score is stored for 2026-10-07 to 2026-10-08." in answer
    ), answer


# ------------------------- H6: a short noun may not borrow a score -------


SKI = "Is tomorrow a good day to ski in Reykjavik?"


def test_a_three_letter_noun_is_reported_rather_than_discarded(stub):
    route, _rows, _asked = stub
    result = route.retrieve(SKI)

    assert result.resolution.unknown_activities == ["ski"]
    assert "ski" in result.unscored_activities
    # It did not become a catalogue activity on the way.
    assert result.resolution.activities == []


def test_the_short_noun_is_actually_looked_up(stub):
    """The slug reaches the database as a filter, which is what tells a
    previously requested short activity apart from an unknown one."""
    route, _rows, asked = stub
    route.retrieve(SKI)

    assert "ski" in asked, asked


def test_the_short_noun_gap_reaches_the_answer_and_the_prompt(stub):
    route, _rows, _asked = stub
    brief = grounding.build(route.retrieve(SKI))

    gaps = [g for g in brief.gaps if g.subject == "activity:ski"]
    assert len(gaps) == 1, [g.subject for g in brief.gaps]
    assert "not on record" in gaps[0].text
    block = grounding.prompt_block(brief)
    assert "ASKED ABOUT BUT NOT ON RECORD" in block
    assert "ski" in block


@pytest.mark.parametrize(
    "answer",
    [
        "Tomorrow is a good day to ski in Reykjavik.",
        "The suitability score for ski is good (100/100).",
        "Conditions are ideal for ski in Reykjavik tomorrow.",
    ],
)
def test_a_verdict_on_the_short_noun_is_rejected(stub, answer):
    """The grounding check is armed only because the noun reached
    `unscored_activities`. While the noun was discarded by the length floor,
    nothing was watching at all."""
    route, _rows, _asked = stub
    brief = grounding.build(route.retrieve(SKI))

    found = grounding.violations(answer, brief)
    assert found, f"no check fired on {answer!r}"


def test_a_morphological_variant_of_the_noun_is_not_caught(stub):
    """A recorded limit, not a fix. The name-keyed check matches the token the
    traveller typed -- `ski` -- so a model that writes ``Skiing is fair'' has
    said something no check fires on. This is strictly better than before, when
    the noun was discarded and no check fired on any wording of it, and it is
    narrower than it looks: the gap sentence naming `ski` is appended in code
    either way, so the answer contradicts itself rather than passing cleanly.
    Closing it needs stemming, which this router deliberately does not do.
    """
    route, _rows, _asked = stub
    brief = grounding.build(route.retrieve(SKI))

    assert grounding.violations("Skiing is fair tomorrow given the light wind.", brief) == []
    assert any(g.subject == "activity:ski" for g in brief.gaps)


def test_the_floor_matches_the_one_the_write_path_enforces():
    """Two characters, because `RecommendationRequestIn.activity` already allows
    two. A read path stricter than the write path can only lose a noun the
    system would have scored."""
    assert router.MIN_ACTIVITY_SLUG_CHARS == 2


def test_a_previously_requested_short_activity_answers_from_its_own_row(monkeypatch, stub):
    route, rows, _asked = stub
    rows.extend(
        verdict_row(day, "ski", "ski", 31, "poor", requested=True, reasons=("wind 11 km/h",))
        for day in STORED
    )
    _result, response = ask(monkeypatch, route, SKI)

    answer = response["answer"]
    assert "- 2026-10-06: ski is poor (31/100)." in answer
    # Not the catalogue row that used to supply the number.
    assert "100/100" not in answer
    assert "beach" not in answer.lower()


# --------- H7: a stored custom name beats a keyword inside that name ----


KITE_SURFING = "Is tomorrow a good day for kite surfing in Tel Aviv?"


def test_the_collision_is_real_before_anything_else_is_claimed(stub):
    """`surfing` is a whole word of `kite surfing`, so whole-word matching is no
    defence. Asserted against the catalogue rather than described, so the test
    fails if the keyword list changes under it."""
    assert (
        "surfing"
        in router.load_activity_keywords(router.config.DATA_DIR / "activities.yml")["surfing"]
    )
    assert router._mentions("is tomorrow a good day for kite surfing in tel aviv?", "surfing")


def test_a_stored_custom_activity_resolves_to_its_own_rows(monkeypatch, stub):
    route, rows, asked = stub
    rows.extend(
        verdict_row(
            day, "kite_surfing", "Kite surfing", 42, "poor", requested=True, reasons=("wind",)
        )
        for day in STORED
    )
    result, response = ask(monkeypatch, route, KITE_SURFING)

    assert result.resolution.activities == ["kite_surfing"]
    # The catalogue activity whose keyword sits inside the name is not added
    # alongside it -- if it were, the SQL filter would be dropped (it binds only
    # for exactly one activity) and both scores would reach the answer.
    assert "surfing" not in result.resolution.activities
    assert asked[-1] == "kite_surfing"
    answer = response["answer"]
    assert "- 2026-10-06: Kite surfing is poor (42/100)." in answer
    assert "Surfing is fair" not in answer
    assert "69/100" not in answer


def test_the_custom_activity_keeps_its_own_label_and_caveat(monkeypatch, stub):
    """A generic score has to say what it is a score of. The label and the
    reason both come off the row, not from the catalogue entry it collided
    with."""
    route, rows, _asked = stub
    rows.extend(
        verdict_row(
            day,
            "kite_surfing",
            "Kite surfing",
            42,
            "poor",
            requested=True,
            reasons=("scored against general outdoor comfort, not a rule tuned for this activity",),
        )
        for day in STORED
    )
    _result, response = ask(monkeypatch, route, KITE_SURFING)

    answer = response["answer"]
    assert "Kite surfing" in answer
    assert "You asked for this activity by name" in answer
    assert "general outdoor comfort" in answer


@pytest.mark.parametrize(
    "question",
    [
        "Is tomorrow a good day for kite surfing in Tel Aviv?",
        "Is tomorrow a good day for kite  surfing in Tel Aviv?",
        "Is tomorrow a good day for kite-surfing in Tel Aviv?",
        "Is tomorrow a good day for KITE SURFING in Tel Aviv?",
        "Where and when can I go kite surfing in Tel Aviv?",
    ],
)
def test_every_spelling_of_the_stored_name_resolves_the_same_way(monkeypatch, stub, question):
    """Spacing and capitalisation must not decide which activity is answered.

    The double space is the one that mattered and it failed the wrong way. A
    phrase comes back from `_candidate_phrases` rebuilt with single spaces, so a
    literal replace left "kite  surfing" unmasked -- and the keyword loop then
    resolved `surfing` *alongside* `kite_surfing`. Two activities drop the
    single-activity SQL filter, so the answer carried both scores: not a
    fallback to the old behaviour but a worse answer than either.
    """
    route, rows, _asked = stub
    rows.extend(
        verdict_row(day, "kite_surfing", "Kite surfing", 42, "poor", requested=True)
        for day in STORED
    )
    result, response = ask(monkeypatch, route, question)

    assert result.resolution.activities == ["kite_surfing"], question
    assert "69/100" not in response["answer"], "surfing's score reached the answer"
    assert "Kite surfing" in response["answer"]


def test_an_ordinary_catalogue_question_is_untouched(monkeypatch, stub):
    """The bound on the repair. The phrase IS the keyword, so nothing is probed
    and nothing is masked."""
    route, _rows, asked = stub
    result, response = ask(monkeypatch, route, "Is tomorrow good for surfing in Tel Aviv?")

    assert result.resolution.activities == ["surfing"]
    assert "- 2026-10-06: Surfing is fair (69/100)." in response["answer"]
    assert asked == ["surfing"], asked


def test_a_phrase_keyword_still_answers_about_its_catalogue_activity(monkeypatch, stub):
    """The case the `typed_activities` gate exists for, and the reason masking is
    confined to a phrase with stored rows. "a long walk" is one of hiking's own
    keywords; extracted it slugifies to `long_walk`, which has no row. Were it
    masked anyway, hiking would be lost and the answer would read "long walk:
    not on record" with hiking's score nowhere on screen."""
    route, _rows, _asked = stub
    result, response = ask(
        monkeypatch, route, "Is tomorrow a good day for a long walk in Tel Aviv?"
    )

    assert result.resolution.activities == ["hiking"]
    assert result.resolution.unknown_activities == []
    assert "- 2026-10-06: Hiking is good (74/100)." in response["answer"]


def test_a_custom_name_with_no_stored_row_is_a_gap_not_the_catalogue_answer(monkeypatch, stub):
    """The live demo failure, and the half the first repair left open.

    `aow-demo` on efaec14 answered "is tomorrow a good day for kite surfing in
    Lisbon?" with "Surfing is fair (69/100)" and one retrieved row. Nothing said
    kite surfing was not on record. That is worse than a refusal precisely
    because the number, the band and the sea caveat were all genuine -- they
    simply belonged to a different activity.

    The earlier repair fixed it only when a `kite_surfing` row already existed.
    The live failure had none, which is the ordinary case: a reviewer types the
    UI's own placeholder and nobody has requested a score for it.
    """
    route, _rows, _asked = stub
    result, response = ask(monkeypatch, route, KITE_SURFING)

    assert result.resolution.activities == []
    assert result.resolution.unknown_activities == ["kite_surfing"]
    answer = response["answer"]
    assert "kite surfing: no suitability score on record." in answer, answer
    # The substituted answer, in every form it took.
    assert "69/100" not in answer
    assert "Surfing is fair" not in answer
    assert "Surfing" not in answer
    # And rendered in code, so there is no wording for the model to invent.
    assert response["llm_called"] is False


def test_an_unscored_named_activity_retrieves_no_catalogue_rows(stub):
    """The mechanism under the answer above, and the one the live transcript
    measured directly: `rows_used.recommendations` was 18 -- one row per
    catalogue activity, none of them the activity asked about. A model handed
    that list has eighteen numbers to pick from and no row that says no."""
    route, _rows, _asked = stub
    result = route.retrieve(KITE_SURFING)

    assert result.recommendations == []
    assert result.resolution.unknown_activities == ["kite_surfing"]


def test_the_short_noun_is_answered_in_code_from_its_gap(monkeypatch, stub):
    """The other live failure, on the same mechanism. Reproduced 3/3 on
    `aow-demo`: "is tomorrow a good day to ski in Reykjavik?" was answered
    "tomorrow is a good day to ski ... based on the stored data", with no `ski`
    row anywhere and 18 unrelated rows retrieved. The fabrication was the
    VERDICT, not a number, so a check forbidding a digit beside the noun would
    have passed it.

    The model is now not called at all on this route, which is what makes the
    failure unreachable rather than merely guarded against.
    """
    route, _rows, _asked = stub
    result, response = ask(monkeypatch, route, SKI)

    assert result.recommendations == []
    assert response["llm_called"] is False
    answer = response["answer"]
    assert "ski: no suitability score on record." in answer, answer
    assert "good day to ski" not in answer.lower()
    # The heading may not describe a list of gaps as stored suitability.
    assert answer.startswith("I hold no suitability score for what you asked about in Reykjavik:")


@pytest.mark.parametrize(
    ("phrase", "activity", "line"),
    [
        ("a long walk", "hiking", "- 2026-10-06: Hiking is good (74/100)."),
        ("walking tour", "sightseeing", None),
        ("sea swim", "swimming", None),
        ("boat ride", "boat_ride", None),
        ("street market", "farmers_market", None),
        ("farmers market", "farmers_market", None),
        ("outdoor workout", "outdoor_workout", None),
    ],
)
def test_a_multi_word_catalogue_keyword_still_answers_about_its_own_activity(
    monkeypatch, stub, phrase, activity, line
):
    """The bound on the repair, and the reason it keys on the keyword list
    rather than on whether rows exist.

    Every phrase here contains a shorter keyword as a whole word -- "long walk"
    contains "walk", "walking tour" contains "walking", "sea swim" contains
    "swim" -- so every one of them reaches the shadow probe. Each is also a
    catalogue keyword in its own right, which is what sends it back to the
    keyword loop. Masking them instead would lose their activity and print
    "long walk: not on record" where hiking's score belongs.
    """
    route, _rows, _asked = stub
    result, response = ask(monkeypatch, route, f"Is tomorrow a good day for {phrase} in Tel Aviv?")

    # `in`, not `==`: "walking tour" carries two catalogue keywords ("walking"
    # is hiking's) and has always resolved both. That ambiguity predates this
    # repair and is untouched by it -- what matters here is that the phrase's
    # own activity survives and that nothing is reported as a gap.
    assert activity in result.resolution.activities, phrase
    assert result.resolution.unknown_activities == [], phrase
    if line:
        assert line in response["answer"]


def test_the_multi_word_keywords_really_do_shadow_a_shorter_one(stub):
    """The premise of the test above, asserted rather than assumed. If the
    catalogue ever stopped carrying both the long and the short form, the
    parametrisation would be passing for the wrong reason."""
    route, _rows, _asked = stub

    for phrase in ("long walk", "walking tour", "sea swim", "boat ride", "street market"):
        assert route._shadows_a_keyword(phrase), phrase
        assert router.slugify(phrase) in route.keyword_slugs, phrase
    # And the name that must NOT be treated as one.
    assert route._shadows_a_keyword("kite surfing")
    assert router.slugify("kite surfing") not in route.keyword_slugs


def test_a_name_that_shadows_a_keyword_and_is_stored_still_wins(monkeypatch, stub):
    """The first repair, unchanged by the second: a stored row for the longer
    name is answered from that row, not from the keyword inside it."""
    route, rows, _asked = stub
    rows.extend(
        verdict_row(day, "kite_surfing", "Kite surfing", 42, "poor", requested=True)
        for day in STORED
    )
    result, response = ask(monkeypatch, route, KITE_SURFING)

    assert result.resolution.activities == ["kite_surfing"]
    assert result.resolution.unknown_activities == []
    assert "- 2026-10-06: Kite surfing is poor (42/100)." in response["answer"]


def test_the_cost_of_the_repair_is_a_gap_and_never_a_borrowed_score(monkeypatch, stub):
    """Recorded as a decision, not discovered later as a defect.

    "a long run" is not one of running's keywords, so under this repair it is
    reported as not on record rather than answered with running's score. That is
    the direction this project takes everywhere -- an honest gap over a borrowed
    figure -- and it is the same judgement that makes "kite surfing" a gap. What
    must never happen is the other outcome: a number that belongs to a different
    activity, presented as the answer.
    """
    route, _rows, _asked = stub
    result, response = ask(monkeypatch, route, "Is tomorrow a good day for a long run in Tel Aviv?")

    assert result.resolution.activities == []
    assert result.resolution.unknown_activities == ["long_run"]
    assert "long run: no suitability score on record." in response["answer"]


def test_open_wording_keeps_its_ordinary_answer(monkeypatch, stub):
    """Neither repair may turn open wording into a gap report."""
    route, _rows, _asked = stub
    result, response = ask(
        monkeypatch, route, "Is tomorrow a good day for something fun in Tel Aviv?"
    )

    assert result.resolution.unknown_activities == []
    assert result.resolution.activities == []
    assert "not on record" not in response["answer"].lower()
