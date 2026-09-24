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


# ---------------------------------------------- the wider catalogue (v2) ----


def test_coastal_activities_are_dropped_inland(activities):
    """An inland city gets no surfing row at all, rather than a surfing score
    derived from a forecast that says nothing about surf."""
    inland = rules.activities_for_city(activities, coastal=False)
    coastal = rules.activities_for_city(activities, coastal=True)

    assert coastal == activities
    assert set(inland) < set(activities)
    for key in ("surfing", "swimming", "beach_day", "fishing", "boat_ride"):
        assert key in coastal
        assert key not in inland
    # The land-based ones must survive the filter, or an inland city would
    # lose its whole catalogue.
    for key in ("running", "museums", "sightseeing"):
        assert key in inland


def test_surfing_wants_wind_and_a_flat_day_is_penalised(activities):
    """`min_wind_kmh` is the one rule that penalises too *little* of something.

    Read below the ceiling. Surfing is capped at 69 because nothing here
    measures the sea (`test_coastal_evidence.py`), and on a warm dry day both a
    flat sea and a blowing one land on that cap, so the wind rule is invisible
    in the final number. It still decides the day the moment anything else
    costs a point, and it still says so in the reasons -- so the comparison is
    made against the same config with the ceiling lifted, and the reason is
    asserted on the shipped one.
    """
    flat = {**HOT_BEACH_DAY, "wind_kmh": 2.0}
    blowing = {**HOT_BEACH_DAY, "wind_kmh": 22.0}
    uncapped = {k: v for k, v in activities["surfing"].items() if k != "score_ceiling"}

    assert (
        rules.score_activity("surfing", uncapped, flat).score
        < rules.score_activity("surfing", uncapped, blowing).score
    )
    assert any("flat" in r for r in score(activities, "surfing", flat).reasons)
    # The same flat day must not be penalised for a boat ride, which wants
    # calm: its score is held down by the sea-state cap and by nothing else.
    boat = score(activities, "boat_ride", flat)
    assert boat.score == 69
    assert boat.reasons[0] == "no rule was violated"
    assert "capped at 69" in boat.reasons[1]


def test_indoor_activities_do_not_all_score_alike(activities):
    """The bug this fixed: every indoor activity returned the same number, so
    the planner had nothing to choose between a museum and a games console."""
    indoor = [k for k, cfg in activities.items() if cfg.get("indoor")]
    assert len(indoor) >= 4

    for weather in (PERFECT_SPRING_DAY, COLD_STORM):
        scores = {k: score(activities, k, weather).score for k in indoor}
        assert len(set(scores.values())) > 1, f"all indoor scores identical for {weather}"

    # A museum outranks staying in to game whatever the weather; the gap is
    # what narrows when the weather turns.
    nice_gap = (
        score(activities, "museums", PERFECT_SPRING_DAY).score
        - score(activities, "indoor_gaming", PERFECT_SPRING_DAY).score
    )
    storm_gap = (
        score(activities, "museums", COLD_STORM).score
        - score(activities, "indoor_gaming", COLD_STORM).score
    )
    assert nice_gap > storm_gap > 0


def test_every_activity_has_a_label_and_an_icon(activities):
    """Both are rendered by the UI; a missing one shows up as a blank cell."""
    for key, cfg in activities.items():
        assert cfg.get("label"), f"{key} has no label"
        assert cfg.get("icon"), f"{key} has no icon"
        assert isinstance(cfg.get("interests", []), list)


def test_catalogue_covers_the_requested_activities(activities):
    """The activities this build was asked for, indoor and out."""
    expected = {
        "surfing",
        "fishing",
        "hiking",
        "swimming",
        "boat_ride",
        "soccer",
        "farmers_market",
        "outdoor_workout",
        "music_festival",
        "museums",
        "art_gallery",
        "mall",
        "standup_comedy",
    }
    assert expected <= set(activities)
