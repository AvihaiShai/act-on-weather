"""Where a day's activity actually happens.

The planner's second bug, and the companion to `test_planner.py`. It chose the
day's places from the traveller's stated interests and never looked at what the
day was for, so a Tel Aviv beach day listed parks -- `outdoors` resolves to
parks and gardens -- and, until the beach vocabulary landed in the ingestor,
there was no beach row to list either.

`venue_places` is pure: an activity key, the catalogue, the city's stored rows,
and the ids already spent. No database, no LLM, no clock.

The test that matters most here is
`test_a_beach_is_not_evidence_of_surf_swimming_fishing_or_boats`. Everything
else is ordering; that one is the project's rule about not claiming what a
source did not say, pinned down.
"""

from __future__ import annotations

from services.agent.planning import MAX_VENUE_PLACES, venue_places

VENUE_META = {
    # Only beach_day claims a coastal venue, and that is the whole argument:
    # "beach" is what Wikidata Q40080 and OSM `natural=beach` assert.
    "beach_day": {"label": "A day at the beach", "place_categories": ["beach"]},
    "surfing": {"label": "Surfing"},
    "swimming": {"label": "Swimming in the sea"},
    "fishing": {"label": "Fishing"},
    "boat_ride": {"label": "A boat ride"},
    "museums": {"label": "Visiting a museum", "place_categories": ["museum"]},
    "running": {"label": "Running"},
}


def place(pid: str, name: str, category: str):
    return {"id": pid, "name": name, "category": category}


# The ids and names are the rows Wikidata actually returned for Tel Aviv at the
# ingestor's 4km radius, so this fixture cannot drift into fiction.
TEL_AVIV = [
    place("wikidata:Q56377198", "Bugrashov Beach, Tel Aviv", "beach"),
    place("wikidata:Q136117836", "Frishman Beach", "beach"),
    place("wikidata:Q56376795", "Hilton Beach, Tel Aviv", "beach"),
    place("wikidata:Q56377589", "Jerusalem Beach, Tel Aviv", "beach"),
    place("wikidata:Q7217322", "Tel Aviv Marina", "marina"),
    place("wikidata:Q1", "Yarkon Park", "park"),
    place("wikidata:Q2", "Tel Aviv Museum of Art", "museum"),
]
LONDON = [
    place("wikidata:Q3", "Hyde Park", "park"),
    place("wikidata:Q4", "British Museum", "museum"),
]


def names(rows):
    return [r["name"] for r in rows]


# ------------------------------------------------------ naming the venue ----


def test_a_beach_day_names_a_real_beach():
    picked = venue_places("beach_day", VENUE_META, TEL_AVIV)
    assert names(picked) == [
        "Bugrashov Beach, Tel Aviv",
        "Frishman Beach",
        "Hilton Beach, Tel Aviv",
    ]
    assert {r["category"] for r in picked} == {"beach"}


def test_an_indoor_activity_names_its_venue_too():
    """The fix is activity-aware matching, not a beach special case."""
    assert names(venue_places("museums", VENUE_META, TEL_AVIV)) == ["Tel Aviv Museum of Art"]


def test_at_most_three_venues_are_named():
    many = [place(f"b{i}", f"Beach {i}", "beach") for i in range(10)]
    assert len(venue_places("beach_day", VENUE_META, many)) == MAX_VENUE_PLACES


# ------------------------------------------------------- the honest gap ----


def test_a_beach_day_in_a_city_with_no_beach_names_nothing():
    """A park is not a substitute for a beach."""
    assert venue_places("beach_day", VENUE_META, LONDON) == []


def test_an_activity_with_no_venue_category_names_nothing():
    assert venue_places("running", VENUE_META, TEL_AVIV) == []


def test_a_day_with_no_scored_activity_names_nothing():
    assert venue_places(None, VENUE_META, TEL_AVIV) == []


def test_an_unknown_activity_does_not_crash():
    assert venue_places("kite_flying", VENUE_META, TEL_AVIV) == []


# -------------------------------------------- what the source did not say ----


def test_a_beach_is_not_evidence_of_surf_swimming_fishing_or_boats():
    """Tel Aviv has four beaches and a marina stored, and all four of these
    activities are scored for the city -- but "a beach is here" is not "the
    surf is rideable", "the swimming is supervised", "angling is permitted" or
    "there is a boat to hire". So they name no venue at all.

    If evidence-bearing rows arrive later -- `leisure=beach_resort` with
    `supervised=yes`, a surf-break class, a marina with boat hire -- this is
    the test to revisit, deliberately.
    """
    for activity in ("surfing", "swimming", "fishing", "boat_ride"):
        assert venue_places(activity, VENUE_META, TEL_AVIV) == [], activity


def test_a_marina_is_not_offered_as_a_beach():
    assert "Tel Aviv Marina" not in names(venue_places("beach_day", VENUE_META, TEL_AVIV))


# ----------------------------------------------- rotation and determinism ----


def test_venues_are_not_repeated_across_days():
    """A week in Tel Aviv should not open on Bugrashov seven times."""
    used: set[str] = set()
    day_one = venue_places("beach_day", VENUE_META, TEL_AVIV, used)
    used.update(p["id"] for p in day_one)
    day_two = venue_places("beach_day", VENUE_META, TEL_AVIV, used)
    assert names(day_two) == ["Jerusalem Beach, Tel Aviv"]
    assert not set(names(day_one)) & set(names(day_two))


def test_the_rotation_runs_out_honestly_rather_than_looping():
    """Four beaches, and once they are spent the answer is nothing.

    The alternative -- wrapping around, or falling back to a park -- implies a
    beach the city does not have.
    """
    used = {p["id"] for p in TEL_AVIV if p["category"] == "beach"}
    assert venue_places("beach_day", VENUE_META, TEL_AVIV, used) == []


def test_the_same_request_rebuilds_the_same_plan():
    assert venue_places("beach_day", VENUE_META, TEL_AVIV) == venue_places(
        "beach_day", VENUE_META, TEL_AVIV
    )
