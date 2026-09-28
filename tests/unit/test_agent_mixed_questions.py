"""A question that asks two things gets two answers.

The defect. `ask()` renders a named-activity answer in code rather than through
the model, because a 1.7B model handed seven `fair` days calls it a good week.
That route was widened to cover an activity with NO stored score -- "is tomorrow
a good day to ski in Reykjavik?" -- which was right: there is no wording for the
model to get right there, and letting it try produced a favourable verdict for
an activity with no row behind it.

What the widening did not account for is that `named_activity_answer` renders
recommendations and activity gaps and nothing else. It never touches
`result.events`, `result.places` or `result.facts`. So a question that named an
unscored activity AND asked about something else took the code route and threw
the other half away:

    "What events are on tomorrow in Rome, and is it a good day for a bbq?"
    -> "I hold no suitability score for what you asked about in Rome:
        - bbq: no suitability score on record."

with the stored event fetched, counted in `rows_used`, and never mentioned. The
forecast and `asks_where` cases had already been excluded from that route for
exactly this reason; events, places and facts had not.

Two halves to the fix, and the second is the one a route guard alone would miss.
Sending the question to the model preserves the rows -- but `gap_block`
deliberately leaves activity gaps out, because `prompt_block` hands them to the
model under "say this plainly" and printing both would stutter. A model that
answers the obvious half and drops the quiet one therefore still left the bbq
unanswered, and `violations` cannot catch that: check 5 fires on a verdict the
rows do not carry, and saying nothing asserts nothing.
`grounding.unstated_activity_gaps` closes it -- one sentence per activity the
answer never names at all.

Every assertion here is behavioural: the real `Router` over canned rows, the
real `ask()`, and the model replaced by a stub that records the prompt it was
given. "The rows survived" is asserted against that prompt rather than against
the stub's own prose, so a test cannot pass because the stub happened to mention
an event.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from services.agent import dates, router
from services.common.llm import LlmUnavailable

# The clock is frozen and every date is written out, so these keep meaning
# something after the staged snapshot expires.
TODAY = date(2026, 9, 27)
TOMORROW = date(2026, 9, 28)
STORED = [TODAY, TOMORROW]

# London, because it is the one city in data/cities.yml with no coast, which is
# what the coast clause in `unscored_line` is read against.
LONDON = {
    "id": "london",
    "name": "London",
    "country": "United Kingdom",
    "timezone": "Europe/London",
    "aliases": [],
    "coastal": False,
}
TEL_AVIV = {
    "id": "tel_aviv",
    "name": "Tel Aviv",
    "country": "Israel",
    "timezone": "Asia/Jerusalem",
    "aliases": ["tel-aviv"],
    "coastal": True,
}
CITIES = [LONDON, TEL_AVIV]
COVERAGE = {
    "cities": CITIES,
    "weather_first_date": TODAY.isoformat(),
    "weather_last_date": TOMORROW.isoformat(),
    "weather_as_of": TODAY.isoformat(),
}

# One row of each kind, each with a name nothing else in the answer could
# produce, so "the row reached the answer" is a string search that cannot pass
# by accident.
EVENT_TITLE = "Autumn Jazz Night"
PLACE_NAME = "Trattoria Vecchia Roma"
FACT_TITLE = "London in brief"

# The activity the questions name. Three characters, so it also stands on the
# floor `MIN_ACTIVITY_SLUG_CHARS` was lowered to, and no catalogue keyword
# claims it.
ACTIVITY = "bbq"
# What code says about it. `grounding.gaps` writes this sentence; the point of
# asserting the wording is that it is code's own and cannot be paraphrased away.
GAP = f"{ACTIVITY}: not on record"
GAP_WHY = "no suitability score is stored for it in London"


def forecast_row(day: date) -> dict[str, Any]:
    return {
        "forecast_date": day,
        "provider": "open-meteo",
        "temp_max_c": 20.0,
        "temp_min_c": 12.0,
        "precip_mm": 0.0,
        "precip_prob": 0,
        "wind_kmh": 9.0,
        "sunshine_hours": 7.0,
        "as_of": TODAY.isoformat(),
    }


def event_row() -> dict[str, Any]:
    return {
        "id": "ev-mixed",
        "city_id": "london",
        "title": EVENT_TITLE,
        "category": "music",
        "venue": "Barbican",
        "starts_at": f"{TOMORROW}T20:00:00+01:00",
        "ends_at": None,
        "starts_on": str(TOMORROW),
        "ends_on": str(TOMORROW),
        "source": "seed",
        "source_url": "https://example.invalid/jazz",
        "is_sample": False,
        "as_of": TODAY.isoformat(),
    }


def place_row() -> dict[str, Any]:
    return {
        "id": "pl-mixed",
        "city_id": "london",
        "name": PLACE_NAME,
        "category": "fine_dining",
        "description": "A trattoria.",
        "source": "seed",
        "source_url": "https://example.invalid/trattoria",
        "as_of": TODAY.isoformat(),
        "lat": 51.5,
        "lon": -0.1,
        "is_sample": False,
    }


def fact_row() -> dict[str, Any]:
    return {
        "id": "fa-mixed",
        "city_id": "london",
        "kind": "history",
        "title": FACT_TITLE,
        "summary": "Settled by the Romans at a crossing of the Thames.",
        "source": "seed",
        "source_url": "https://example.invalid/london",
        "as_of": TODAY.isoformat(),
    }


@pytest.fixture
def route(monkeypatch):
    """The real `Router` over one row of every kind. No database, no clock.

    `recommendations` returns nothing on purpose: the activity the questions name
    has no score, which is the whole premise. The forecast DOES return rows, so
    the retrieval is not empty and `ask()` reaches the branch under test rather
    than the "no stored records" refusal above it.
    """
    monkeypatch.setattr(dates, "today_in", lambda _timezone: TODAY)
    monkeypatch.setattr(router.queries, "cities", lambda _conn: CITIES)
    monkeypatch.setattr(router.queries, "coverage", lambda _conn: COVERAGE)
    monkeypatch.setattr(router.queries, "recommendations", lambda *_a, **_k: [])
    monkeypatch.setattr(router.queries, "events", lambda *_a, **_k: [event_row()])
    monkeypatch.setattr(
        router.queries, "expired_events", lambda *_a, **_k: {"expired": 0, "last_checked": None}
    )
    monkeypatch.setattr(router.queries, "places", lambda *_a, **_k: [place_row()])
    monkeypatch.setattr(router.queries, "facts", lambda *_a, **_k: [fact_row()])
    monkeypatch.setattr(
        router.queries, "forecast", lambda *_a, **_k: [forecast_row(day) for day in STORED]
    )
    return router.Router(object())


@pytest.fixture
def agent(monkeypatch, route):
    """`ask()` with its pool and its model replaced, and the prompt recorded.

    `prompts` is what makes "the rows were not discarded" checkable without
    trusting the stub's prose: whatever the model is told is what the retrieval
    handed over.
    """
    monkeypatch.setenv("POSTGRES_READER_PASSWORD", "unit-test")
    from services.agent import main

    monkeypatch.setattr(main, "pool", type("StubPool", (), {"conn": object()})())
    monkeypatch.setattr(main, "Router", lambda _conn: route)

    class Agent:
        prompts: list[str] = []

        def reply(self, text: str):
            """The model answers with `text`."""

            def chat_json(_system, prompt, _schema, **_kwargs):
                self.prompts.append(prompt)
                return {"answer": text}

            monkeypatch.setattr(main.client, "chat_json", chat_json)
            return self

        def down(self):
            """The model cannot be reached at all."""

            def chat_json(*_a, **_k):
                raise LlmUnavailable("unit test: no model")

            monkeypatch.setattr(main.client, "chat_json", chat_json)
            return self

        def unusable(self):
            """The model answers, and the answer is too short to use."""
            return self.reply("no")

        def ask(self, question: str) -> dict[str, Any]:
            return main.ask(main.AskIn(question=question))

    agent = Agent()
    agent.prompts = []
    # A reply that answers the OTHER half and never mentions the activity. That
    # is the observed failure, not a contrived one: the events are the obvious
    # half of the question and the small model answers what is obvious.
    agent.reply(
        f"{EVENT_TITLE} is on at the Barbican on {TOMORROW}. The stored forecast "
        f"for London that day is a high of 20C with no rain."
    )
    return agent


# The four questions, and the fourth is not a duplicate of the third. A places
# lookup is reached by `"places" in intents OR resolution.categories`
# (`Router.retrieve`), so an interest word alone pulls place rows in with no
# `places` intent anywhere -- which a guard written against the intents only
# would have walked straight past.
MIXED = {
    "events": "What events are on tomorrow in London, and is it a good day for a bbq?",
    "facts": "What is the history of London, and is it a good day for a bbq?",
    "places": "What restaurants are in London, and is it a good day for a bbq?",
    "categories": "Is there fine dining in London, and is it a good day for a bbq?",
}
# What each question's own half is called in `rows_used`, and the string that
# proves its row arrived.
SUBJECT = {
    "events": ("events", EVENT_TITLE),
    "facts": ("facts", FACT_TITLE),
    "places": ("places", PLACE_NAME),
    "categories": ("places", PLACE_NAME),
}


# ------------------------------------------------- the premise, asserted ----


@pytest.mark.parametrize("kind", sorted(MIXED))
def test_each_question_really_names_a_second_subject_and_an_unscored_activity(route, kind):
    """Or the tests below pass by testing nothing.

    Both halves have to be present in the resolution: an activity the catalogue
    cannot score, and a subject other than the weather. The `categories` case is
    the one that carries no matching intent, which is why it is asserted
    separately rather than folded into the same condition.
    """
    resolution = route.resolve(MIXED[kind])

    assert resolution.unknown_activities == [ACTIVITY], resolution.unknown_activities
    assert not resolution.activities, resolution.activities
    second = bool({"events", "places", "facts"} & set(resolution.intents)) or bool(
        resolution.categories
    )
    assert second, (resolution.intents, resolution.categories)
    if kind == "categories":
        assert resolution.categories, "the interest word should reach the places query"


# --------------------------- the rows survive, and so does the gap ----


@pytest.mark.parametrize("kind", sorted(MIXED))
def test_a_mixed_question_keeps_its_other_half_and_still_states_the_activity_gap(agent, kind):
    """The blocker, end to end.

    Three things at once, because any two of them without the third is a
    different bug: the row reached the model rather than being dropped, the
    answer the traveller reads carries it, and the activity nobody can score is
    named as such in code's own words.
    """
    counted, row_text = SUBJECT[kind]

    reply = agent.ask(MIXED[kind])

    # Not the code-only route: the model was asked.
    assert reply["llm_called"] is True
    # And its wording survived, so the gap below is the appended block rather
    # than `grounding.render` -- which states every gap and would make this test
    # pass for the wrong reason.
    assert "note" not in reply, reply.get("note")
    # The row was handed over, whatever the model then did with it.
    assert reply["rows_used"][counted] >= 1, reply["rows_used"]
    assert row_text in agent.prompts[-1], agent.prompts[-1]
    # And the half the model did not answer is answered anyway.
    assert GAP in reply["answer"], reply["answer"]
    assert GAP_WHY in reply["answer"], reply["answer"]


@pytest.mark.parametrize("kind", sorted(MIXED))
def test_the_old_code_only_answer_is_gone(agent, kind):
    """The regression itself, pinned by the heading it used to print.

    `named_activity_answer` opens with this sentence when it has no rows at all,
    and seeing it here means the question went down the route that renders
    recommendations and nothing else.
    """
    reply = agent.ask(MIXED[kind])

    assert "I hold no suitability score for what you asked about" not in reply["answer"]


@pytest.mark.parametrize("kind", sorted(MIXED))
def test_no_verdict_is_invented_for_the_unscored_activity(agent, kind):
    """The gap is a gap. Nothing appended may carry a number or a band."""
    reply = agent.ask(MIXED[kind])

    appended = reply["answer"].split("Also asked about:", 1)[-1]
    assert "/100" not in appended, appended
    for band in ("excellent", "good", "fair", "poor"):
        assert f"{ACTIVITY} is {band}" not in reply["answer"].lower()


# ------------------------------------------ without a usable model at all ----


@pytest.mark.parametrize("kind", sorted(MIXED))
def test_the_fallback_answer_carries_both_halves_when_the_model_is_down(agent, kind):
    """`grounding.render` is the whole answer here, so this is the strongest
    version of the assertion: no stub prose is involved in either half."""
    _counted, row_text = SUBJECT[kind]

    reply = agent.down().ask(MIXED[kind])

    assert reply["llm_called"] is False
    assert "model is unavailable" in reply["note"]
    assert row_text in reply["answer"], reply["answer"]
    assert GAP in reply["answer"], reply["answer"]


@pytest.mark.parametrize("kind", sorted(MIXED))
def test_the_fallback_answer_carries_both_halves_when_the_model_is_unusable(agent, kind):
    """The other fallback, which takes a different branch and the same renderer.
    Asserted separately because only one of the two sets `llm_called`."""
    _counted, row_text = SUBJECT[kind]

    reply = agent.unusable().ask(MIXED[kind])

    assert reply["llm_called"] is True
    assert "unusable output" in reply["note"]
    assert row_text in reply["answer"], reply["answer"]
    assert GAP in reply["answer"], reply["answer"]


# ------------------------------------- and no sentence is printed twice ----


def test_an_answer_that_names_the_activity_gets_no_appended_block(agent):
    """The reason the appended block is conditional rather than unconditional.

    The prompt asks the model to state these gaps itself, and when it does,
    appending our own copy reads as a stutter. The test is whether the answer
    NAMES the activity, not whether it worded the gap our way -- an answer that
    names it has either stated the gap or made a claim, and a claim is check 5's
    business.
    """
    reply = agent.reply(
        f"{EVENT_TITLE} is on at the Barbican on {TOMORROW}. There is no record "
        f"here of a {ACTIVITY} in London, so I cannot say."
    ).ask(MIXED["events"])

    assert reply["llm_called"] is True
    assert "Also asked about:" not in reply["answer"], reply["answer"]
    assert ACTIVITY in reply["answer"]


# ------------------------------------------- what must not have changed ----


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Is tomorrow a good day to ski in London?", "ski: no suitability score on record."),
        # A declared `distinct_names` entry that needs the sea, in the one city
        # that has none: the answer says why, and the reason is true.
        (
            "Is tomorrow a good day for kite surfing in London?",
            "kite surfing: no suitability score on record; London has no coast on record.",
        ),
    ],
)
def test_a_question_about_the_activity_alone_is_still_answered_in_code(agent, question, expected):
    """The behaviour the widened route exists for, unchanged by the guard.

    No model call, no invented verdict, and the gap stated in the heading that
    says it is a gap.
    """
    reply = agent.ask(question)

    assert reply["llm_called"] is False
    assert "I hold no suitability score for what you asked about in London:" in reply["answer"]
    assert expected in reply["answer"], reply["answer"]
    assert "/100" not in reply["answer"]
    assert agent.prompts == [], "the model must not be called for this question"


def test_a_weather_question_naming_an_unscored_activity_keeps_its_forecast(agent):
    """The exclusion that was already there, re-asserted because the new guard
    sits beside it -- and now the activity gap is guaranteed on this route too,
    which it was not when only the model could state it.

    The reply is deliberately a forecast sentence and nothing else: it has to
    survive `violations`, or the answer becomes `grounding.render`, which states
    every gap and would prove nothing about the appended block.
    """
    reply = agent.reply(
        f"The stored forecast for London on {TOMORROW} is a high of 20C with no rain."
    ).ask("What is the weather tomorrow in London, and is it good for a bbq?")

    assert reply["llm_called"] is True
    assert "note" not in reply, reply.get("note")
    assert reply["rows_used"]["forecast"] >= 1
    assert "Stored daily forecast" in agent.prompts[-1]
    assert GAP in reply["answer"], reply["answer"]


def test_a_where_question_about_a_catalogue_activity_is_still_rendered_in_code(agent):
    """The `where` route, which runs before any of this and must keep doing so:
    "where can I surf" is answered from venue rows, not from a forecast."""
    reply = agent.ask("Where can I surf in Tel Aviv?")

    assert reply["llm_called"] is False
    assert agent.prompts == []
    assert "surf" in reply["answer"].lower()
