"""The deterministic suitability score.

This is the source of truth for every recommendation (M2). The LLM never
decides whether a day is good for running; it is handed the score and the
reasons and writes a sentence about them. That is what makes the system's
advice reproducible and defensible, and it is why an LLM outage degrades the
wording of a recommendation rather than its content.

Scoring: start at 100 and subtract a penalty per rule that the day violates.
Every penalty carries a short human reason, and those reasons are what the
model is given. Thresholds live in data/activities.yml, not here.
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


def band_for(score: int) -> str:
    for name, floor in BANDS:
        if score >= floor:
            return name
    return "poor"


def _clamp(value: int) -> int:
    return max(0, min(100, value))


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
        # Deliberately floored: an indoor day is never a bad option, it is only
        # a less compelling one when the weather outside is good.
        value = _clamp(max(25, 100 - comfort))
        band = band_for(value)
        # The reasons have to agree with the band, or the model is handed a
        # contradiction and will faithfully write one.
        if band == "poor":
            reasons = ["the weather outside is good, so indoors is the weaker choice"]
            if comfort_reasons:
                reasons.append("though note: " + ", ".join(comfort_reasons))
        elif comfort_reasons:
            reasons = [f"a good day to stay in: {r}" for r in comfort_reasons]
        else:
            reasons = ["nothing outside is compelling enough to leave for"]
        return Score(value, band, reasons)

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
