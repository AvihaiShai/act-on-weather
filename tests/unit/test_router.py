"""Date parsing and intent matching -- the parts of the agent that are pure code.

These are the decisions the model is deliberately not allowed to make, so they
are the ones worth testing.
"""

from datetime import date, timedelta
from pathlib import Path

import pytest

from services.agent import dates, router
from services.agent.router import INTENT_WORDS, _mentions

# ------------------------------------------------------------------ dates ----


def test_tomorrow_is_tomorrow():
    window = dates.parse("What is the weather tomorrow in Rome?", "Europe/Rome")
    expected = dates.today_in("Europe/Rome") + timedelta(days=1)
    assert window.start == window.end == expected
    assert window.label == "tomorrow"


def test_this_week_is_seven_days():
    window = dates.parse("What can I do this week in London?", "Europe/London")
    assert len(window.days()) == 7
    assert window.start == dates.today_in("Europe/London")


def test_an_explicit_date_wins():
    window = dates.parse("weather in Rome on 2027-07-04", "Europe/Rome")
    assert window.start == window.end == date(2027, 7, 4)


def test_weekend_lands_on_a_saturday():
    window = dates.parse("is this weekend good for running?", "UTC")
    assert window.start.weekday() == 5
    assert len(window.days()) == 2


def test_day_after_tomorrow_beats_tomorrow():
    """Both words are present; the more specific one has to win."""
    window = dates.parse("what about the day after tomorrow?", "UTC")
    assert window.start == dates.today_in("UTC") + timedelta(days=2)


def test_the_default_says_it_is_a_default():
    window = dates.parse("what should I do in Lisbon?", "UTC")
    assert "assumed" in window.label


def test_an_unknown_timezone_does_not_raise():
    assert isinstance(dates.today_in("Mars/Olympus_Mons"), date)


def test_a_range_renders_readably():
    window = dates.parse("this week", "UTC")
    assert " to " in str(window)


# ----------------------------------------------------------------- intents ----


def test_eat_does_not_match_inside_weather():
    """The bug this test exists for: `"eat" in "weather"` is True.

    With plain substring matching every weather question silently acquired the
    `places` intent and ran a restaurant lookup nobody asked for.
    """
    assert not _mentions("what is the weather tomorrow in rome?", "eat")
    assert _mentions("where should we eat tonight?", "eat")


@pytest.mark.parametrize(
    "text, phrase, expected",
    [
        ("we like fine dining", "fine dining", True),
        ("concerts and shopping", "concerts", True),
        ("concerts and shopping", "concert", False),  # plural only, no partials
        ("a park nearby", "park", True),
        ("parking is hard", "park", False),
        ("SHOPPING in London".lower(), "shopping", True),
    ],
)
def test_word_boundary_matching(text, phrase, expected):
    assert _mentions(text, phrase) is expected


def test_weather_question_gets_only_weather_intents():
    text = "what is the weather tomorrow in rome?"
    matched = [i for i, words in INTENT_WORDS.items() if any(_mentions(text, w) for w in words)]
    assert "weather" in matched
    assert "places" not in matched
    assert "events" not in matched


def test_the_second_example_question_gets_the_right_intents():
    text = (
        "what activities can i do with my wife this week in london? "
        "we like concerts, shopping and fine dining."
    )
    matched = [i for i, words in INTENT_WORDS.items() if any(_mentions(text, w) for w in words)]
    assert "activities" in matched
    assert "places" in matched


# ------------------------------------------------- activity recognition ----
#
# The router has to notice which activity a question is about, so it can tell
# the model when it holds no row for it. Without that, asked "is it good for
# surfing in London?" with no surfing row in front of it, the model invents a
# weather-based reason why it is not -- which is exactly the hallucination the
# router-first design exists to prevent.

ACTIVITIES_YML = Path(__file__).resolve().parents[2] / "data" / "activities.yml"


@pytest.fixture(scope="module")
def keywords():
    return router.load_activity_keywords(ACTIVITIES_YML)


def detect(keywords, question: str) -> set[str]:
    text = question.lower()
    return {
        activity
        for activity, words in keywords.items()
        if any(_mentions(text, word) for word in words)
    }


@pytest.mark.parametrize(
    "question, expected",
    [
        ("Is it a good day for surfing in London tomorrow?", "surfing"),
        ("Can I go fishing in Lisbon this week?", "fishing"),
        ("I want to swim in Tel Aviv on Friday", "swimming"),
        ("Should I go to a museum in Rome?", "museums"),
        ("Where can I see a stand-up show?", "standup_comedy"),
        ("Is the mall a good idea today?", "mall"),
        ("Any good day for a boat ride?", "boat_ride"),
        ("Can I play football in Reykjavik?", "soccer"),
        ("Is tomorrow good for a run?", "running"),
    ],
)
def test_an_activity_is_recognised_from_ordinary_wording(keywords, question, expected):
    assert expected in detect(keywords, question)


def test_longer_phrases_are_matched_first(keywords):
    """'a long walk' belongs to hiking. Matching 'walk' first would attribute
    it to whichever activity happened to share the shorter word."""
    assert keywords["hiking"] == sorted(keywords["hiking"], key=len, reverse=True)
    assert "hiking" in detect(keywords, "I fancy a long walk tomorrow")


def test_a_weather_question_names_no_activity(keywords):
    """The whole-word matcher has to stay strict: a false positive here puts an
    activity the user never mentioned into the 'not on record' list."""
    assert detect(keywords, "What is the weather tomorrow in Rome?") == set()
    assert detect(keywords, "Will it rain in London this week?") == set()


def test_every_activity_has_keywords(keywords):
    for activity, words in keywords.items():
        assert words, f"{activity} has no keywords, so it can never be recognised"


def test_keywords_do_not_collide_across_activities(keywords):
    """Two activities claiming the same word would make detection ambiguous."""
    seen: dict[str, str] = {}
    for activity, words in keywords.items():
        for word in words:
            assert word not in seen, f"{word!r} claimed by both {seen.get(word)} and {activity}"
            seen[word] = activity


def test_named_activity_keeps_all_seven_days_and_fallback_verdicts(monkeypatch):
    monkeypatch.setenv("POSTGRES_READER_PASSWORD", "unit-test")
    from services.agent import main

    today = date(2026, 9, 24)
    monkeypatch.setattr(dates, "today_in", lambda _timezone: today)
    city = {
        "id": "rome", "name": "Rome", "country": "Italy", "timezone": "Europe/Rome",
        "aliases": [], "coastal": True,
    }
    coverage = {
        "cities": [city], "weather_first_date": "2026-09-24",
        "weather_last_date": "2026-10-09", "weather_as_of": "2026-09-24",
    }
    rows = []
    for offset in range(7):
        day = today + timedelta(days=offset)
        rows.extend([
            {
                "forecast_date": day, "activity": "beach_day",
                "activity_label": "A day at the beach", "score": 95,
                "band": "good", "text": None,
            },
            {
                "forecast_date": day, "activity": "running",
                "activity_label": "Running", "score": 55 + offset,
                "band": "fair", "text": None,
            },
        ])
    monkeypatch.setattr(router.queries, "cities", lambda _conn: [city])
    monkeypatch.setattr(router.queries, "coverage", lambda _conn: coverage)
    monkeypatch.setattr(router.queries, "forecast", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(router.queries, "recommendations", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(router.queries, "events", lambda *_args, **_kwargs: [])

    result = router.Router(object()).retrieve("Is it a good week to go running in Rome?")

    assert result.resolution.activities == ["running"]
    assert len(result.recommendations) == 7
    assert {row["forecast_date"] for row in result.recommendations} == {
        today + timedelta(days=offset) for offset in range(7)
    }
    context = router.context_block(result)
    fallback = main.plain_answer(result)
    for offset in range(7):
        day = str(today + timedelta(days=offset))
        assert f"{day} Running: fair ({55 + offset}/100)" in context
        assert f"{day}: Running is fair ({55 + offset}/100)" in fallback
    assert "beach" not in context.lower()
    assert "beach" not in fallback.lower()
    assert not main.named_verdicts_present("Running looks good this week.", result)
    assert not main.named_verdicts_present(fallback + " It is a good week overall.", result)
    assert not main.named_verdicts_present(
        fallback.replace("2026-09-30: Running is fair", "2026-09-30: Running is good"),
        result,
    )
    assert main.named_verdicts_present(fallback, result)

    monkeypatch.setattr(main, "pool", type("StubPool", (), {"conn": object()})())
    monkeypatch.setattr(
        main, "Router", lambda _conn: type("StubRouter", (), {"retrieve": lambda self, _q: result})()
    )

    def unavailable(*_args, **_kwargs):
        raise main.LlmUnavailable("model stopped")

    monkeypatch.setattr(main.client, "chat_json", unavailable)
    response = main.ask(main.AskIn(question="Is it a good week to go running in Rome?"))
    assert response["llm_called"] is False
    assert response["answer"] == fallback
    assert response["rows_used"]["recommendations"] == 7


def test_open_question_context_retains_each_date():
    start = date(2026, 9, 24)
    resolution = router.Resolution(question="What can I do this week?")
    rows = [
        {
            "forecast_date": start + timedelta(days=offset),
            "activity": f"activity_{number}", "activity_label": f"Activity {number}",
            "score": 100 - number, "band": "good", "text": None,
        }
        for offset in range(7)
        for number in range(18)
    ]
    result = router.Retrieval(resolution, {}, True, recommendations=rows)
    selected = router.context_recommendations(result)
    assert len(selected) == 40
    assert {row["forecast_date"] for row in selected} == {
        start + timedelta(days=offset) for offset in range(7)
    }


@pytest.mark.parametrize("length, expected", [(1, "1 day in Rome"), (2, "2 days in Rome")])
def test_itinerary_title_uses_correct_day_count(monkeypatch, length, expected):
    monkeypatch.setenv("POSTGRES_READER_PASSWORD", "unit-test")
    from services.agent import main

    today = date(2026, 9, 24)
    city = {"id": "rome", "name": "Rome", "timezone": "Europe/Rome"}
    coverage = {
        "weather_first_date": str(today),
        "weather_last_date": str(today + timedelta(days=length - 1)),
        "weather_as_of": str(today),
    }
    monkeypatch.setattr(main, "pool", type("StubPool", (), {"conn": object()})())
    monkeypatch.setattr(main.queries, "cities", lambda _conn: [city])
    monkeypatch.setattr(main.queries, "coverage", lambda _conn: coverage)
    monkeypatch.setattr(main.queries, "recommendations", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(main.queries, "places", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(main.queries, "events", lambda *_args, **_kwargs: [])
    result = main.build_itinerary(
        main.ItineraryIn(city="rome", start_date=today, end_date=today + timedelta(days=length - 1))
    )
    assert result["title"] == expected
