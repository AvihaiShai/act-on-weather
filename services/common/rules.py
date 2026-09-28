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

# Whole words that name an activity done in or on water. Matched against the
# slug's own words, so `kite_surfing` and `scuba_diving` are caught and
# `surf_shop` is excluded by NOT_WATER_WORDS below.
#
# This list exists because the catalogue's coast rules reach only the catalogue.
# `requires_coast` and `score_ceiling` are properties of a row in
# data/activities.yml, and a typed activity has no row there: "scuba diving"
# asked for London went straight to `GENERIC_CFG`, which carries no ceiling, so
# a pleasant day on land stored scuba diving in London as `good`, 100/100, with
# no coast anywhere in the city's record. The four catalogue sea activities are
# capped at 69 for exactly the reason that number does not describe -- nothing
# stored measures waves, swell or water temperature -- and an inland city cannot
# even be asked the question.
#
# It is a word list and it is not complete; that is the honest shape of the
# problem, since the set of things people do in water is open. What matters is
# the direction it fails in: a word not on the list is scored generically, as
# before, and a word on it can only ever LOWER a score or refuse a row. Nothing
# here can raise one.
SEA_WORDS: frozenset[str] = frozenset(
    """
    sea ocean offshore surf surfing windsurf windsurfing kitesurf kitesurfing
    bodyboard bodyboarding dive diving scuba snorkel snorkelling snorkeling
    freediving swim swimming kayak kayaking canoe canoeing paddleboard
    paddleboarding sail sailing yachting jetski jetskiing waterskiing
    wakeboarding rafting tide tidal swell
    """.split()
)

# Words that take an activity back OUT of the list above. Every one of these was
# a false positive found by reading the list out loud against real names:
#
#   sky diving      "diving" -- and it is the one activity here furthest from water
#   indoor rowing   "rowing", on a machine
#   pool swimming   "swimming", in a pool with no sea state to be unmeasured
#   river rafting   "rafting", on fresh water with no coast involved
#   marine museum   "marine", which is why `marine` is not in the list at all
#   a surf shop     "surf", which is a shop
#   diving lesson   "diving", which may be in a pool
#
# Each false positive is not a harmless over-cap. It REFUSES the row outright for
# an inland city and then tells the traveller "London has no coast on record" as
# the reason an indoor rowing machine cannot be scored -- a fabricated causal
# claim, which is the one thing this project may not do. `bathe`/`bathing` and
# `beach`/`shore`/`coast` were dropped from the list above for the same reason:
# "sun bathing" is not swimming, and a beach day is deliberately not sea-gated
# even in the catalogue.
NOT_WATER_WORDS: frozenset[str] = frozenset(
    """
    sky indoor indoors pool gym river lake museum shop store lesson lessons
    class classes simulator machine
    """.split()
)


def names_water(slug: str) -> bool:
    """Whether an activity slug names something done in or on open water.

    Read as whole words of the slug, which is what `schemas.slugify` produced
    from what the user typed: `kite_surfing` -> {"kite", "surfing"}. A word in
    `NOT_WATER_WORDS` anywhere in the slug settles it as not water, because the
    cost of a false positive here is a refused row and an invented reason.
    """
    words = set(slug.split("_"))
    if words & NOT_WATER_WORDS:
        return False
    return bool(SEA_WORDS & words)


# Words that say the water is not the sea. They deliberately do NOT take a name
# out of `names_water`: the quality of these activities is still a property of
# water nothing here measures, so they keep the 69 ceiling and the caveat that
# explains it. What they take away is the COAST requirement, which is a
# different claim and is the one that was being made falsely.
#
# Both entries came from `distinct_names` in data/activities.yml, which is where
# the collision was found:
#
#   wild swimming   swimming in natural water. In Britain that is usually a
#                   river, a lake or a pond -- London has several and no coast.
#   ice swimming    swimming in water cold enough to carry ice, which is a lake
#                   as often as a shore.
#
# Before the split, `swimming` coast-gated both: a request for "wild swimming"
# in London stored no row at all, and the agent gave the reason as "London has
# no coast on record" -- a true sentence offered as the cause of something it
# did not cause. That is the same fabricated causal claim `NOT_WATER_WORDS`
# exists to prevent, and it is the worse half of it, because a refusal withholds
# the row as well as explaining it wrongly.
#
# Not exhaustive, and it fails in the safe direction: a name that belongs here
# and is missing is still coast-gated, which withholds a row rather than
# inventing a score for one.
#
# These are qualifiers, not nouns: each one narrows what kind of water is meant
# without naming a body of water at all. That is why `EXPLICIT_SEA_WORDS` below
# can overrule them without contradicting anything -- "wild sea swimming" is
# wild swimming IN THE SEA, and the noun decides.
INLAND_WATER_WORDS: frozenset[str] = frozenset("wild ice".split())

# Words that name the sea itself, and so overrule an inland qualifier standing
# beside them. "wild sea swimming" is a real phrase and it names the sea twice
# over: `wild` says the water is not a pool, and `sea` says which water it is.
# Read in the order `INLAND_WATER_WORDS` used to be read alone, `wild` won and
# the request was scored for London at the 69 ceiling with a caveat about waves
# -- a capped row for a city with no coast, whose whole caveat was about a body
# of water the city does not have.
#
# THE PRECEDENCE, outermost first, and each layer is here because it fails in a
# direction the layer below it does not:
#
#   1. NOT_WATER_WORDS wins over everything, including these. "indoor sea
#      swimming" and "sea swimming pool" are scored as ordinary outdoor comfort
#      and no coast is asked for. It is the safest failure available -- a
#      generic score, no ceiling, no refusal and no claim about a coast -- so it
#      stays on the outside.
#   2. These words beat INLAND_WATER_WORDS. They fail towards a REFUSAL, which
#      is the expensive direction, so the list is kept to words that cannot
#      mean anything but the open sea.
#   3. INLAND_WATER_WORDS otherwise removes the coast requirement and keeps the
#      ceiling, as before.
#   4. Anything else is decided by `names_water`.
#
# Why only these three. `tide`, `tidal` and `swell` are all in `SEA_WORDS` and
# are all deliberately NOT here: the Thames is tidal and runs through the one
# inland city on the list, so "tidal river swimming" would be refused in London
# with "London has no coast on record" -- exactly the fabricated causal claim
# the split in `needs_coast` was written to stop. A word earns a place here by
# naming the sea and nothing else.
#
# The same "not exhaustive, fails safe" bound applies as everywhere else in this
# module, but note that it points the other way here: a sea word missing from
# this list leaves the inland qualifier winning, which caps a score instead of
# refusing a row. That is the direction this project prefers to be wrong in.
EXPLICIT_SEA_WORDS: frozenset[str] = frozenset("sea ocean offshore".split())


def needs_coast(slug: str) -> bool:
    """Whether a typed activity cannot be scored for a city with no coast.

    Narrower than `names_water`, and the two were one test until the difference
    bit. "Does the water decide how good this is?" governs the score ceiling and
    is true of any open water. "Does this need the sea?" governs whether a row
    is stored at all -- and a false yes there is not a cautious number, it is a
    refusal whose stated reason is untrue.

    An inland qualifier only wins when nothing in the same name names the sea
    outright; see `EXPLICIT_SEA_WORDS` for the full precedence and for why that
    list is as short as it is. `names_water` still has the last word either way,
    so `NOT_WATER_WORDS` cannot be overruled from here.

    Only the typed case. A catalogue activity carries `requires_coast` on its own
    row and both callers read that first; this is what they fall back to when the
    catalogue has never heard of the name.
    """
    words = set(slug.split("_"))
    if words & INLAND_WATER_WORDS and not words & EXPLICIT_SEA_WORDS:
        return False
    return names_water(slug)


# The generic measure for a typed activity that names water, in a city that has
# a coast. Same land rules -- there are no others to apply -- plus the ceiling
# and the flag that makes `_apply_ceiling` say which ceiling it is and why. 69
# is the catalogue's own number for the same situation, not a new one.
GENERIC_SEA_CFG: dict[str, Any] = {
    **GENERIC_CFG,
    "score_ceiling": 69,
    "sea_state_unmeasured": True,
}


def score_requested(activity_label: str, weather: dict[str, Any], *, sea: bool = False) -> Score:
    """Score an activity the catalogue does not carry.

    `sea` is set by the caller when the slug names water (`names_water`), and it
    swaps in the capped configuration. The caller decides rather than this
    function, because the caller is also the only place that knows the city --
    and the sibling question, "does this need a coast at all?" (`needs_coast`),
    is answered before the forecast is even read, since no weather can supply
    one.
    """
    result = score_activity(activity_label, GENERIC_SEA_CFG if sea else GENERIC_CFG, weather)
    result.reasons.append(
        "scored against general outdoor comfort, not a rule tuned for this activity"
    )
    return result
