"""Date parsing and intent matching -- the parts of the agent that are pure code.

These are the decisions the model is deliberately not allowed to make, so they
are the ones worth testing.
"""

from datetime import date, timedelta

import pytest

from services.agent import dates
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
