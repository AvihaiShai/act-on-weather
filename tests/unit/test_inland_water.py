"""The coast gate and the score ceiling are two questions, not one.

`services/common/rules.py` had a single test, `names_water(slug)`, answering
both. It decided the 69 ceiling for an activity whose quality nothing stored
measures, and it decided whether a row is stored at all for a city with no
coast. Those are different claims, and `data/activities.yml` already held the
names that pull them apart: its `distinct_names` list carries `wild swimming`
and `ice swimming`. Both contain the catalogue keyword `swimming`, so both were
coast-gated, so a request for wild swimming in London stored no row and the
agent gave the reason as "London has no coast on record" -- a true sentence
offered as the cause of something it did not cause. Wild swimming is swimming
in natural water: a river, a lake, a pond. London has those, and no coast.

So the split. `names_water` is unchanged and still asks "is this done in or on
water?", which is what caps the score and adds the sea-state caveat.
`needs_coast` is new and narrower -- "does this need the SEA?" -- and it is
what refuses a row. A false yes there is not a cautious number, it is a refusal
whose stated reason is untrue, which is the worse failure of the two.

These tests pin the gap between the two functions, the ceiling that must still
bind for an inland-water name, and the rule that every word in
`INLAND_WATER_WORDS` earns its place by changing an answer. Pure functions and
one read of `data/activities.yml`; nothing here connects to anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from services.common import rules, schemas

ROOT = Path(__file__).resolve().parents[2]
ACTIVITIES_YML = ROOT / "data" / "activities.yml"

# Warm, dry, calm and sunny: no rule in `GENERIC_CFG` is violated, so an
# uncapped score here is 100 and anything below it is the ceiling, not weather.
FINE_DAY = {
    "temp_max_c": 24.0,
    "temp_min_c": 17.0,
    "precip_mm": 0.0,
    "precip_prob": 5.0,
    "wind_kmh": 9.0,
    "uv_index": 4.0,
    "sunshine_hours": 10.0,
}


# ------------------------------------- the two questions, side by side -------
#
# One row per slug: what the water test says, and what the coast test says. The
# first two rows are the whole point of the change -- water, but not the sea.

# (slug, done in or on water, needs the sea)
WATER_TABLE = [
    ("wild_swimming", True, False),
    ("ice_swimming", True, False),
    ("scuba_diving", True, True),
    ("kite_surfing", True, True),
    ("sea_kayaking", True, True),
    ("surfing", True, True),
    ("ski", False, False),
    ("indoor_rowing", False, False),
    ("sky_diving", False, False),
    ("river_rafting", False, False),
    ("pool_swimming", False, False),
]


@pytest.mark.parametrize(("slug", "water", "coast"), WATER_TABLE)
def test_the_water_test_and_the_coast_test_answer_separately(slug, water, coast):
    assert rules.names_water(slug) is water
    assert rules.needs_coast(slug) is coast


@pytest.mark.parametrize(("slug", "water", "coast"), WATER_TABLE)
def test_the_coast_test_is_never_wider_than_the_water_test(slug, water, coast):
    """`needs_coast` may only ever remove names, never add one.

    It carries the stricter consequence -- a refused row -- so a slug that
    reached it without naming water at all would be withholding rows for a
    reason nothing in the slug supports.
    """
    if rules.needs_coast(slug):
        assert rules.names_water(slug)


# -------------------------------- the ceiling still binds for inland water ----

CEILING = rules.GENERIC_SEA_CFG["score_ceiling"]
# The exact reason `_apply_ceiling` appends for a `sea_state_unmeasured`
# config. Taken from the source rather than paraphrased: the reasons are what
# the model is handed to write from, so the wording is the behaviour.
SEA_STATE_REASON = (
    f"capped at {CEILING}: nothing in the stored data measures waves, swell or water temperature"
)


def test_the_bands_are_the_three_this_file_assumes():
    """Guard the premise. Every assertion below is about a score that may not
    reach the top band, so the top band and its floor have to be what they were
    when the ceiling was chosen."""
    assert [name for name, _floor in rules.BANDS] == ["good", "fair", "poor"]
    top_band, floor = rules.BANDS[0]
    assert top_band == "good"
    assert floor - 1 == CEILING


def test_an_inland_water_name_is_still_capped_and_still_says_why():
    """No coast is needed, but the water is still unmeasured. The ceiling is
    the half of the old behaviour that was right, and it has to survive the
    half that was not."""
    score = rules.score_requested("wild swimming", FINE_DAY, sea=rules.names_water("wild_swimming"))

    assert score.score <= CEILING
    assert score.band == "fair"
    assert score.band != rules.BANDS[0][0]
    assert SEA_STATE_REASON in score.reasons


def test_an_ordinary_land_name_scored_by_the_same_function_is_not_capped():
    """The bound on the repair: the cap is what the `sea` flag buys, not a
    blanket lowering of every typed activity."""
    score = rules.score_requested("stargazing", FINE_DAY, sea=False)

    assert score.score > CEILING
    assert score.band == rules.BANDS[0][0]
    assert not [reason for reason in score.reasons if reason.startswith("capped at")]


# ---------------------------- every word on the list has to earn its place ----


@pytest.mark.parametrize("word", sorted(rules.INLAND_WATER_WORDS))
def test_every_inland_water_word_actually_changes_an_answer(word):
    """A word that changes no answer is a claim with nothing behind it.

    Each entry has to name a real case: a slug the water test still catches and
    the coast test now lets through. This is what stops the list growing by
    guesswork.
    """
    slug = f"{word}_swimming"

    assert rules.names_water(slug) is True
    assert rules.needs_coast(slug) is False


def test_the_two_word_lists_do_the_two_different_jobs():
    """`NOT_WATER_WORDS` and `INLAND_WATER_WORDS` cannot share a word.

    The first takes a name out of the water set entirely -- no ceiling, no
    caveat, scored as ordinary outdoor comfort. The second leaves it in the
    water set and removes only the coast requirement, so the ceiling and the
    caveat that explains it stay. A word on both lists would assert both, and
    whichever test ran first would silently decide which was meant.
    """
    overlap = rules.INLAND_WATER_WORDS & rules.NOT_WATER_WORDS

    assert overlap == frozenset(), f"{sorted(overlap)} claims both jobs"


# ------------------------- read from the catalogue, not from this file --------
#
# The collision was found in `distinct_names`, so the connection is asserted
# against that list as shipped. Slugged the way `router.load_distinct_names`
# and the write path do it, so one normalisation rule covers all three.

DISTINCT_NAMES = (
    yaml.safe_load(ACTIVITIES_YML.read_text(encoding="utf-8")).get("distinct_names") or []
)


def _slug(name: str) -> str:
    return schemas.slugify(str(name).strip().lower())


def test_the_catalogue_still_declares_the_two_names_that_provoked_the_split():
    """Guard the premise: without them the check below passes vacuously."""
    assert {"wild_swimming", "ice_swimming"} <= {_slug(name) for name in DISTINCT_NAMES}


@pytest.mark.parametrize("name", DISTINCT_NAMES)
def test_a_declared_swimming_name_qualified_as_wild_or_ice_needs_no_coast(name):
    """Whether a name really needs the sea is a judgement, so this asserts the
    narrower, checkable thing instead: a declared name that is swimming in wild
    or icy water names water and does not name the sea.

    `wild` and `ice` are written out here on purpose rather than read from
    `INLAND_WATER_WORDS`. This is the one test holding the catalogue and the
    word list together, and a set that supplies its own expected values cannot
    do that.
    """
    slug = _slug(name)
    words = set(slug.split("_"))

    if "swimming" in words and words & {"wild", "ice"}:
        assert rules.names_water(slug) is True
        assert rules.needs_coast(slug) is False
    else:
        # The rest of the list is untouched by the split: the three surfing
        # names still name water and still need the sea.
        assert rules.needs_coast(slug) is rules.names_water(slug)
