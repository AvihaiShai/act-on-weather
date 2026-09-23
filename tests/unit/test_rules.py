"""Truth table for the deterministic scorer.

This is the test that matters most in the repo: the score is the source of
truth for every recommendation, so if these cases hold, an LLM that says
something else is contradicting a number a reviewer can read off the row.
"""

from pathlib import Path

import pytest

from services.common import rules

ACTIVITIES_YML = Path(__file__).resolve().parents[2] / "data" / "activities.yml"

PERFECT_SPRING_DAY = {
    "temp_max_c": 18.0,
    "temp_min_c": 11.0,
    "precip_mm": 0.0,
    "precip_prob": 5,
    "wind_kmh": 9.0,
    "uv_index": 4.0,
    "sunshine_hours": 9.0,
}
COLD_STORM = {
    "temp_max_c": 6.0,
    "temp_min_c": 2.0,
    "precip_mm": 22.0,
    "precip_prob": 95,
    "wind_kmh": 62.0,
    "uv_index": 1.0,
    "sunshine_hours": 0.0,
}
HOT_BEACH_DAY = {
    "temp_max_c": 30.0,
    "temp_min_c": 23.0,
    "precip_mm": 0.0,
    "precip_prob": 3,
    "wind_kmh": 12.0,
    "uv_index": 8.0,
    "sunshine_hours": 11.0,
}


@pytest.fixture(scope="module")
def activities():
    _version, acts = rules.load_activities(ACTIVITIES_YML)
    return acts


@pytest.fixture(scope="module")
def rule_version():
    version, _acts = rules.load_activities(ACTIVITIES_YML)
    return version


def score(activities, key, weather):
    return rules.score_activity(key, activities[key], weather)


# ------------------------------------------------------------ the table ----


@pytest.mark.parametrize(
    "activity, weather, expected_band",
    [
        ("running", PERFECT_SPRING_DAY, "good"),
        ("running", COLD_STORM, "poor"),
        ("running", HOT_BEACH_DAY, "fair"),  # 30C is over running's 20C ceiling
        ("sightseeing", PERFECT_SPRING_DAY, "good"),
        ("sightseeing", COLD_STORM, "poor"),
        ("beach_day", HOT_BEACH_DAY, "good"),
        ("beach_day", PERFECT_SPRING_DAY, "fair"),  # pleasant, but too cool for a beach
        ("beach_day", COLD_STORM, "poor"),
        ("sunset_watching", PERFECT_SPRING_DAY, "good"),
        ("sunset_watching", COLD_STORM, "poor"),
        # Inverted on purpose: the worse it is outside, the better a day it is
        # to stay in and play.
        ("indoor_gaming", COLD_STORM, "good"),
        ("indoor_gaming", PERFECT_SPRING_DAY, "poor"),
    ],
)
def test_band_truth_table(activities, activity, weather, expected_band):
    assert score(activities, activity, weather).band == expected_band


def test_indoor_is_the_inverse_of_outdoor(activities):
    indoor_storm = score(activities, "indoor_gaming", COLD_STORM).score
    indoor_nice = score(activities, "indoor_gaming", PERFECT_SPRING_DAY).score
    outdoor_storm = score(activities, "sightseeing", COLD_STORM).score
    outdoor_nice = score(activities, "sightseeing", PERFECT_SPRING_DAY).score
    assert indoor_storm > indoor_nice
    assert outdoor_storm < outdoor_nice


def test_scores_stay_in_range(activities):
    for key in activities:
        for weather in (PERFECT_SPRING_DAY, COLD_STORM, HOT_BEACH_DAY, {}):
            result = score(activities, key, weather)
            assert 0 <= result.score <= 100
            assert result.band in {"good", "fair", "poor"}
            assert result.reasons, f"{key} produced no reason"


def test_missing_fields_do_not_crash(activities):
    """The scorer never invents a value it was not given."""
    partial = {"temp_max_c": 15.0}
    result = score(activities, "running", partial)
    assert 0 <= result.score <= 100


def test_every_penalty_carries_a_reason(activities):
    result = score(activities, "running", COLD_STORM)
    assert result.score < 40
    joined = " ".join(result.reasons)
    assert "rain" in joined and "wind" in joined


def test_deterministic(activities):
    """Same input, same output -- the property the LLM cannot be trusted for."""
    a = score(activities, "running", HOT_BEACH_DAY)
    b = score(activities, "running", HOT_BEACH_DAY)
    assert (a.score, a.band, a.reasons) == (b.score, b.band, b.reasons)


def test_requested_activity_is_scored_and_labelled():
    """A free-text activity gets a real score and an honest caveat (M2)."""
    result = rules.score_requested("kite flying", COLD_STORM)
    assert result.band == "poor"
    assert any("general outdoor comfort" in r for r in result.reasons)


def test_rule_version_is_an_int(rule_version):
    assert isinstance(rule_version, int) and rule_version >= 1
