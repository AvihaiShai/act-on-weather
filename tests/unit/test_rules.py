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


def test_no_activity_infers_the_sea_from_land_wind(activities):
    """The rule that replaced `min_wind_kmh` (rule_version 4).

    Surfing used to carry `min_wind_kmh: 12` and tell a calm day "only 8km/h
    of wind, too flat for this" -- a verdict on the waves assembled out of a
    wind reading taken on shore, which is the one inference
    `sea_state_unmeasured` exists to forbid. A marine model disagreed with it
    in both directions on the system's own coast points, so it is gone rather
    than retuned.

    Two assertions, because either alone can be satisfied by accident: no
    activity declares the key, and the engine no longer reads it if one did.
    """
    assert not [k for k, cfg in activities.items() if "min_wind_kmh" in cfg]

    still = {**HOT_BEACH_DAY, "wind_kmh": 0.0}
    blowing = {**HOT_BEACH_DAY, "wind_kmh": 22.0}
    # Uncapped, so the ceiling cannot hide a difference the wind rule made.
    uncapped = {k: v for k, v in activities["surfing"].items() if k != "score_ceiling"}
    assert (
        rules.score_activity("surfing", uncapped, still).score
        == rules.score_activity("surfing", uncapped, blowing).score
    )

    # And the reason a dead-calm day carries says nothing about the sea: the
    # cap, and nothing else.
    calm = score(activities, "surfing", still)
    assert calm.score == 69
    assert calm.reasons[0] == "no rule was violated"
    assert "capped at 69" in calm.reasons[1]
    assert not any("flat" in reason for reason in calm.reasons)

    # A boat ride wants calm and was never penalised for it. Unchanged.
    boat = score(activities, "boat_ride", still)
    assert boat.score == 69
    assert boat.reasons[0] == "no rule was violated"
    assert "capped at 69" in boat.reasons[1]


def test_a_min_wind_key_would_no_longer_do_anything(activities):
    """Belt and braces on the line above: even handed the old config, the
    engine scores the day on what it actually measures. This is what stops the
    rule being reintroduced by copying a block from an older revision."""
    revived = {**activities["surfing"], "min_wind_kmh": 12}
    still = {**HOT_BEACH_DAY, "wind_kmh": 0.0}
    assert (
        rules.score_activity("surfing", revived, still).score
        == score(activities, "surfing", still).score
    )


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
