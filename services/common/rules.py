"""The deterministic suitability score.

This is the source of truth for every recommendation (M2). The LLM never
decides whether a day is good for running; it is handed the score and the
reasons and writes a sentence about them. That is what makes the system's
advice reproducible and defensible, and it is why an LLM outage degrades the
wording of a recommendation rather than its content.

Scoring: start at 100 and subtract a penalty per rule that the day violates.
Every penalty carries a short human reason, and those reasons are what the
model is given. Thresholds live in data/activities.yml, not here.

One rule is not a penalty but a ceiling. An activity whose quality depends on
something this system never measures -- the sea, for surfing, swimming,
fishing and a boat ride -- carries `score_ceiling` and can never reach the
`good` band, however perfect the land forecast is. See `_apply_ceiling`.

Every input here is a land measurement: temperature, rain, wind, sun, UV. No
rule in this module may turn one of those into a statement about the water.
That is a stronger rule than the ceiling and it is the one that was broken --
a `min_wind_kmh` on surfing read a calm day as a flat sea, and the reason it
wrote said so in the answer. It is gone as of rule_version 4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

BANDS = (("good", 70), ("fair", 40), ("poor", 0))


@dataclass
class Score:
    score: int
    band: str
    reasons: list[str] = field(default_factory=list)


def load_activities(path) -> tuple[int, dict[str, dict[str, Any]]]:
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    return int(doc.get("rule_version", 1)), doc["activities"]


def activities_for_city(
    activities: dict[str, dict[str, Any]], *, coastal: bool
) -> dict[str, dict[str, Any]]:
    """The activities that are meaningful for one city.

    An activity carrying `requires_coast` is dropped for an inland city rather
    than scored from its forecast. Surfing in London would otherwise get a
    number, and a number the system cannot stand behind is worse than a gap
    the UI can explain.
    """
    if coastal:
        return dict(activities)
    return {k: v for k, v in activities.items() if not v.get("requires_coast")}


def band_for(score: int) -> str:
    for name, floor in BANDS:
        if score >= floor:
            return name
    return "poor"


def _clamp(value: int) -> int:
    return max(0, min(100, value))


def _apply_ceiling(value: int, cfg: dict[str, Any], reasons: list[str]) -> int:
    """Clamp a score to the activity's `score_ceiling`, and say so when it bites.

    Some activities depend on something this system does not measure at all.
    Surfing, swimming, fishing and a boat ride are decided by the water, and
    every input here is a land forecast: temperature, rain, wind, sun, UV. The
    rules can still say a day is too cold, too wet or too still, so the score
    is worth computing -- but it cannot be allowed to climb into the `good`
    band, because a reader would take that as a verdict on conditions nobody
    checked. The ceiling is 69, one point under the `good` floor in `BANDS`.

    The reason is appended only when the ceiling actually binds. A capped score
    that says nothing about the cap is a number quietly lowered behind the
    reader's back, and the reasons are what the model is handed to write from.
    An activity with no ceiling is untouched, down to the reason list.
    """
    ceiling = cfg.get("score_ceiling")
    if ceiling is None:
        return value
    ceiling = int(ceiling)
    if value <= ceiling:
        return value
    if cfg.get("sea_state_unmeasured"):
        reasons.append(
            f"capped at {ceiling}: nothing in the stored data measures waves, "
            "swell or water temperature"
        )
    else:
        reasons.append(
            f"capped at {ceiling}: the stored data does not measure everything "
            "this activity depends on"
        )
    return ceiling


def _num(weather: dict[str, Any], key: str) -> float | None:
    value = weather.get(key)
    return None if value is None else float(value)


def outdoor_comfort(weather: dict[str, Any]) -> tuple[int, list[str]]:
    """A generic 'how pleasant is it outside' measure, 0-100.

    Used directly for indoor activities, which are scored as its inverse: the
    worse it is outside, the better a day it is to stay in.
    """
    penalty = 0
    reasons: list[str] = []

    temp = _num(weather, "temp_max_c")
    if temp is not None:
        if temp < 12:
            penalty += int(min(40, (12 - temp) * 3))
            reasons.append(f"cold at {temp:.0f}C")
        elif temp > 26:
            penalty += int(min(40, (temp - 26) * 3))
            reasons.append(f"hot at {temp:.0f}C")

    precip = _num(weather, "precip_mm")
    if precip is not None and precip > 1:
        penalty += int(min(45, 15 + (precip - 1) * 5))
        reasons.append(f"{precip:.1f}mm of rain")

    wind = _num(weather, "wind_kmh")
    if wind is not None and wind > 25:
        penalty += int(min(30, (wind - 25) * 1.5))
        reasons.append(f"wind {wind:.0f}km/h")

    return _clamp(100 - penalty), reasons


def score_activity(activity: str, cfg: dict[str, Any], weather: dict[str, Any]) -> Score:
    """Score one (activity, day). `cfg` is that activity's block in activities.yml."""
    if cfg.get("indoor"):
        comfort, comfort_reasons = outdoor_comfort(weather)
        # Two knobs, because without them every indoor activity scored
        # identically on a given day and the trip planner had nothing to
        # choose between a museum, a mall and a games console.
        #   indoor_floor  -- how good this is on a perfect day outside
        #   indoor_weight -- how much bad weather improves its case
        # A museum is worth a morning whatever the sky is doing; staying in to
        # play computer games only really competes once outside is unpleasant.
        floor = float(cfg.get("indoor_floor", 25))
        weight = float(cfg.get("indoor_weight", 0.75))
        value = _clamp(int(round(floor + weight * (100 - comfort))))
        # The reasons have to agree with the band, or the model is handed a
        # contradiction and will faithfully write one.
        if not comfort_reasons:
            reasons = ["the weather outside is fine, so this is a choice rather than a refuge"]
        else:
            reasons = [f"a good day to be indoors: {r}" for r in comfort_reasons]
        # No indoor activity carries a ceiling today, but the clamp belongs on
        # both paths: an activity that gains one later must not depend on which
        # branch of this function happens to score it.
        value = _apply_ceiling(value, cfg, reasons)
        return Score(value, band_for(value), reasons)

    penalty = 0
    reasons: list[str] = []

    lo, hi = cfg.get("ideal_temp_c", [None, None])
    temp = _num(weather, "temp_max_c")
    if temp is not None and lo is not None:
        if temp < lo:
            penalty += int(min(45, (lo - temp) * 6))
            reasons.append(f"{temp:.0f}C is below the {lo}-{hi}C range for this")
        elif temp > hi:
            penalty += int(min(45, (temp - hi) * 6))
            reasons.append(f"{temp:.0f}C is above the {lo}-{hi}C range for this")

    max_precip = cfg.get("max_precip_mm")
    precip = _num(weather, "precip_mm")
    if precip is not None and max_precip is not None and precip > max_precip:
        penalty += int(min(50, 20 + (precip - max_precip) * 6))
        reasons.append(f"{precip:.1f}mm of rain, over the {max_precip}mm limit")

    max_prob = cfg.get("max_precip_prob")
    prob = _num(weather, "precip_prob")
    if prob is not None and max_prob is not None and prob > max_prob:
        penalty += int(min(25, (prob - max_prob) * 0.5))
        reasons.append(f"{prob:.0f}% chance of rain")

    # Wind is penalised in one direction only. There used to be a `min_wind_kmh`
    # branch here, carried by surfing alone, that penalised a calm day as "too
    # flat for this" -- a verdict on the waves inferred from a wind reading
    # taken on land. It contradicted the ceiling immediately below it and it
    # did not survive being checked against a marine model; see the surfing
    # block in data/activities.yml (rule_version 4) for the two days that
    # falsified it. Nothing here infers a sea state from a land measurement.
    max_wind = cfg.get("max_wind_kmh")
    wind = _num(weather, "wind_kmh")
    if wind is not None and max_wind is not None and wind > max_wind:
        penalty += int(min(30, (wind - max_wind) * 1.5))
        reasons.append(f"wind {wind:.0f}km/h, over the {max_wind}km/h limit")

    max_uv = cfg.get("max_uv")
    uv = _num(weather, "uv_index")
    if uv is not None and max_uv is not None and uv > max_uv:
        penalty += int(min(15, (uv - max_uv) * 5))
        reasons.append(f"UV index {uv:.0f}")

    min_sun = cfg.get("min_sunshine_hours")
    sun = _num(weather, "sunshine_hours")
    if sun is not None and min_sun is not None and sun < min_sun:
        penalty += int(min(30, (min_sun - sun) * 6))
        reasons.append(f"only {sun:.1f}h of sunshine, {min_sun}h wanted")

    value = _clamp(100 - penalty)
    if not reasons:
        reasons.append("no rule was violated")
    # Last, so that the cap is applied to a finished score and reads as what it
    # is: a limit on what may be claimed, not another weather penalty.
    value = _apply_ceiling(value, cfg, reasons)
    return Score(value, band_for(value), reasons)


# An activity the user typed has no thresholds of its own. Rather than invent
# some, it is scored against the generic outdoor comfort measure and labelled
# as such, so the answer never implies a specificity the system does not have.
GENERIC_CFG: dict[str, Any] = {
    "indoor": False,
    "ideal_temp_c": [12, 26],
    "max_precip_mm": 2.0,
    "max_precip_prob": 50,
    "max_wind_kmh": 30,
}


def score_requested(activity_label: str, weather: dict[str, Any]) -> Score:
    result = score_activity(activity_label, GENERIC_CFG, weather)
    result.reasons.append(
        "scored against general outdoor comfort, not a rule tuned for this activity"
    )
    return result
