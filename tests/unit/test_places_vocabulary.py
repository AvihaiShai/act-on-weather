"""The place-category vocabulary, and the two files that have to agree with it.

This exists because of a gap that scored perfectly and answered nothing: Tel
Aviv rated "a day at the beach" at 100 on every day of the forecast while
holding zero beach rows, because neither the Wikidata class list nor the OSM
tag list had ever collected one. The scoring was right; the places behind it
did not exist.

A missing category is invisible -- it produces no error, just a thinner
snapshot -- so the invariants that would have caught it are asserted here:
every category either source can emit is reachable from data/interests.yml,
and every venue category an activity claims is one the ingestor can actually
collect. Nothing here touches the network; the live yield of these identifiers
was verified against Wikidata and Overpass when they were added, and the
numbers are recorded in the README.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from services.ingestor.fetch_content import (
    OSM_CATEGORIES,
    PER_CATEGORY_LIMIT,
    WIKIDATA_CLASSES,
    overpass_query,
)

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _load(name: str):
    with open(DATA_DIR / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def wikidata_categories() -> set[str]:
    return {category for _qid, category in WIKIDATA_CLASSES}


def osm_categories() -> set[str]:
    return {category for _k, _v, category in OSM_CATEGORIES}


# ------------------------------------------------------- the beach itself ----


def test_wikidata_collects_beaches_and_marinas():
    """Q40080 is `beach` and Q721207 is `marina`.

    Both were confirmed against the live SPARQL endpoint with the same
    4km-radius query the ingestor runs: Tel Aviv returned five named beaches
    and one marina. A wrong Q-number fails silently -- it simply matches
    nothing -- which is exactly why it is pinned in a test.
    """
    assert ("Q40080", "beach") in WIKIDATA_CLASSES
    assert ("Q721207", "marina") in WIKIDATA_CLASSES


def test_osm_collects_beaches_and_marinas():
    assert ("natural", "beach", "beach") in OSM_CATEGORIES
    assert ("leisure", "beach_resort", "beach") in OSM_CATEGORIES
    assert ("leisure", "marina", "marina") in OSM_CATEGORIES


def test_beach_query_asks_for_areas_not_just_nodes():
    """`natural=beach` is nearly always an area in OSM.

    A node-only query returns almost no beaches at all, so the `way` clause
    and the `out center` that gives an area a usable point are load-bearing,
    not incidental.
    """
    query = overpass_query([("natural", "beach")], {"lat": 32.0853, "lon": 34.7818}, 4000)
    assert 'node["natural"="beach"]["name"](around:4000,32.0853,34.7818);' in query
    assert 'way["natural"="beach"]["name"](around:4000,32.0853,34.7818);' in query
    assert "out center tags;" in query


def test_both_sources_stay_interchangeable_on_the_coastal_vocabulary():
    """The README promises a row is the same shape whichever source ran."""
    for category in ("beach", "marina"):
        assert category in wikidata_categories()
        assert category in osm_categories()


# ------------------------------------------------- the files that agree ----


def test_every_collected_category_is_reachable_from_an_interest():
    """A category no interest maps to is collected and then unreachable.

    It would sit in the database, invisible to the planner and to the agent's
    router, which is how a beach could have gone missing a second time.
    """
    interests = _load("interests.yml")["interests"]
    mapped = {category for categories in interests.values() for category in categories}
    orphans = (wikidata_categories() | osm_categories()) - mapped
    assert not orphans, f"collected but unreachable from data/interests.yml: {sorted(orphans)}"


def test_every_activity_venue_category_can_actually_be_collected():
    """An activity may only claim a venue the ingestor knows how to fetch."""
    activities = _load("activities.yml")["activities"]
    collectable = wikidata_categories() | osm_categories()
    for key, cfg in activities.items():
        for category in cfg.get("place_categories") or []:
            assert category in collectable, f"{key} wants '{category}', which no source collects"


def test_the_beach_is_an_outdoors_interest():
    assert "beach" in _load("interests.yml")["interests"]["outdoors"]


def test_per_category_limit_leaves_room_for_several_beaches():
    """One beach per city would make the planner's rotation meaningless."""
    assert PER_CATEGORY_LIMIT >= 3


# --------------------------------------------- what is deliberately absent ----


def test_water_sports_claim_no_venue_from_a_beach_row():
    """A beach is not evidence of surf, a lifeguard, angling or a boat.

    Wikidata Q40080 and OSM `natural=beach` assert that a beach is there and
    nothing else. Giving surfing a `place_categories: [beach]` would turn that
    into a claim the source never made, which is the failure mode this whole
    project is built to avoid. If someone adds evidence-bearing tags later --
    `leisure=beach_resort` with `supervised=yes`, a surf-break class -- this
    test is the place to revisit.
    """
    activities = _load("activities.yml")["activities"]
    for key in ("surfing", "swimming", "fishing", "boat_ride"):
        claimed = activities[key].get("place_categories")
        assert not claimed, f"{key} must not claim a venue category without source evidence"


def test_beach_day_does_claim_the_beach():
    activities = _load("activities.yml")["activities"]
    assert activities["beach_day"]["place_categories"] == ["beach"]
