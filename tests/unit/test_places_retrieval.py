"""How places are retrieved, selected and deduplicated.

The bug these tests exist for did not look like a bug. The committed snapshot
held zero monuments for London and called itself complete, while Wikidata held
84 within the same 4km radius the ingestor was already asking about. Nothing
errored. The cause was two lines of retrieval policy:

  * every class was asked for under one shared `LIMIT 400`, and London matches
    623, so the endpoint returned an arbitrary 400 and whole categories fell
    off the end of the result set;
  * selection then took the first ten of each category *in arrival order*, so
    which venues a city got was whatever the endpoint happened to list first.

Both are policy, not plumbing, and policy is what regresses quietly. So each
one is pinned below against a fake endpoint: no network, no fixtures to
refresh, and a failure message that names the behaviour rather than the shape
of some JSON.
"""

from __future__ import annotations

import pytest

from services.ingestor import fetch_content
from services.ingestor.fetch_content import (
    PER_CATEGORY_LIMIT,
    WIKIDATA_CLASS_QUERY_LIMIT,
    WIKIDATA_SUBCLASS_CLASSES,
    PlaceStats,
    WikidataUnavailable,
    sparql,
    wikidata_class_query,
    wikidata_places,
)

CITY = {"slug": "testville", "lat": 10.0, "lon": 20.0, "name": "Testville"}


def binding(qid: str, name: str, *, sitelinks: int | None = 0, coord: str | None = "Point(20 10)"):
    """One SPARQL result row, shaped the way the live endpoint shapes them."""
    row: dict[str, dict[str, str]] = {
        "item": {"value": f"http://www.wikidata.org/entity/{qid}"},
    }
    if name is not None:
        row["itemLabel"] = {"value": name}
    if coord is not None:
        row["coord"] = {"value": coord}
    if sitelinks is not None:
        row["sitelinks"] = {"value": str(sitelinks)}
    return row


def fake_endpoint(monkeypatch, by_category):
    """Serve canned bindings per category, and record what was asked for."""
    asked: list[str] = []

    def _sparql(query: str, what: str, attempts: int = 5):
        asked.append(what)
        category = what.split("/", 1)[1]
        result = by_category.get(category, [])
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(fetch_content, "sparql", _sparql)
    monkeypatch.setattr(fetch_content, "WIKIDATA_CLASS_PAUSE_S", 0)
    return asked


# ------------------------------------------------ one query per class ----


def test_each_class_is_asked_for_separately():
    """The London monuments regression, stated as a property.

    A single query carrying every class shares one LIMIT between them, so a
    dense city silently loses whole categories. One class per query is what
    makes the limit a safety valve instead of a coverage decision.
    """
    query = wikidata_class_query("Q4989906", CITY, 4000)
    assert "wd:Q4989906" in query
    # No other collected class may ride along in the same query.
    others = [q for q, _c in fetch_content.WIKIDATA_CLASSES if q != "Q4989906"]
    for other in others:
        assert f"wd:{other}" not in query, "classes must not share a query, or they share a LIMIT"
    assert "VALUES ?class" not in query


def test_every_collected_class_is_actually_queried(monkeypatch):
    asked = fake_endpoint(monkeypatch, {})
    wikidata_places(CITY, 4000, "2026-09-24T00:00:00Z")
    expected = {f"testville/{category}" for _q, category in fetch_content.WIKIDATA_CLASSES}
    assert set(asked) == expected


def test_the_per_class_limit_is_a_safety_valve_not_the_budget():
    """4km of a city holds hundreds of a class, not thousands."""
    assert WIKIDATA_CLASS_QUERY_LIMIT >= 1000
    query = wikidata_class_query("Q33506", CITY, 4000)
    assert f"LIMIT {WIKIDATA_CLASS_QUERY_LIMIT}" in query


def test_the_query_asks_for_distinct_items():
    """Without DISTINCT the limit binds on duplicates instead of venues.

    Caught live: with traversal on, Rome's 64 distinct galleries came back as
    1500 rows -- one per P31/P279 path -- which hit the per-class limit, so
    the endpoint truncated *before* ranking could run. Deduplicating in Python
    is too late by then; the rows that mattered were never sent. This is the
    same failure as the old global LIMIT 400, one layer down.
    """
    for qid, _category in fetch_content.WIKIDATA_CLASSES:
        assert "SELECT DISTINCT" in wikidata_class_query(qid, CITY, 4000)


def test_hitting_the_per_class_limit_is_reported_not_swallowed(monkeypatch):
    """Ranking over a truncated set is not ranking, and the run must say so."""
    fake_endpoint(
        monkeypatch,
        {
            "museum": [
                binding(f"Q{i}", f"Museum {i}", sitelinks=i)
                for i in range(WIKIDATA_CLASS_QUERY_LIMIT)
            ]
        },
    )
    stats = PlaceStats()
    wikidata_places(CITY, 4000, "2026-09-24T00:00:00Z", stats)
    assert stats.truncated_classes == ["testville/museum"]
    assert "TRUNCATED=testville/museum" in stats.summary()


# -------------------------------------------------- deterministic order ----


def test_selection_prefers_better_known_venues_and_is_deterministic(monkeypatch):
    """Sitelinks descending, then Q-number ascending.

    Arrival order is not a ranking. Sitelink count is the source's own,
    stable signal for how well known a venue is, so a capped category keeps
    the Colosseum rather than whichever monument the endpoint listed first.
    """
    fake_endpoint(
        monkeypatch,
        {
            "museum": [
                binding("Q900", "Obscure collection", sitelinks=1),
                binding("Q100", "Famous museum", sitelinks=90),
                binding("Q500", "Mid museum", sitelinks=30),
            ]
        },
    )
    monkeypatch.setattr(fetch_content, "PER_CATEGORY_LIMIT", 2)
    rows = wikidata_places(CITY, 4000, "2026-09-24T00:00:00Z")
    museums = [r["name"] for r in rows if r["category"] == "museum"]
    assert museums == ["Famous museum", "Mid museum"]


def test_ties_break_on_qid_so_the_snapshot_is_reproducible(monkeypatch):
    fake_endpoint(
        monkeypatch,
        {
            "museum": [
                binding("Q300", "Third", sitelinks=5),
                binding("Q100", "First", sitelinks=5),
                binding("Q200", "Second", sitelinks=5),
            ]
        },
    )
    rows = wikidata_places(CITY, 4000, "2026-09-24T00:00:00Z")
    assert [r["name"] for r in rows if r["category"] == "museum"] == ["First", "Second", "Third"]


def test_an_item_with_no_sitelink_count_still_sorts(monkeypatch):
    """`wikibase:sitelinks` is OPTIONAL, so its absence must not crash a run."""
    fake_endpoint(
        monkeypatch,
        {
            "museum": [
                binding("Q100", "No sitelink data", sitelinks=None),
                binding("Q200", "Well known", sitelinks=12),
            ]
        },
    )
    rows = wikidata_places(CITY, 4000, "2026-09-24T00:00:00Z")
    assert [r["name"] for r in rows if r["category"] == "museum"] == [
        "Well known",
        "No sitelink data",
    ]


# -------------------------------------------------------- deduplication ----


def test_an_item_in_two_classes_is_stored_once(monkeypatch):
    """Wikidata items carry several P31s; a row per class would double-count.

    Category order in WIKIDATA_CLASSES is the tie-break, which makes it a
    priority order rather than an arbitrary one.
    """
    shared = binding("Q42", "Both a museum and a gallery", sitelinks=20)
    fake_endpoint(monkeypatch, {"museum": [shared], "gallery": [shared]})
    rows = wikidata_places(CITY, 4000, "2026-09-24T00:00:00Z")
    matching = [r for r in rows if r["id"] == "wikidata:Q42"]
    assert len(matching) == 1
    first_category = next(
        c for _q, c in fetch_content.WIKIDATA_CLASSES if c in {"museum", "gallery"}
    )
    assert matching[0]["category"] == first_category


def test_a_duplicate_does_not_consume_a_capped_slot(monkeypatch):
    """Deduplication runs before the cap, or a duplicate steals a real venue.

    Phrased in terms of processing order rather than two fixed categories,
    because that order is itself policy (see the specific-before-general
    comment on WIKIDATA_CLASSES) and is free to change.
    """
    order = [category for _qid, category in fetch_content.WIKIDATA_CLASSES]
    first, second = order[0], order[1]
    shared = binding("Q42", f"Claimed by {first}", sitelinks=99)
    fake_endpoint(
        monkeypatch,
        {
            first: [shared],
            second: [shared, binding("Q7", "The one that must survive", sitelinks=3)],
        },
    )
    monkeypatch.setattr(fetch_content, "PER_CATEGORY_LIMIT", 1)
    rows = wikidata_places(CITY, 4000, "2026-09-24T00:00:00Z")
    assert [r["name"] for r in rows if r["category"] == first] == [f"Claimed by {first}"]
    # Had the duplicate eaten the cap slot, this category would be empty.
    assert [r["name"] for r in rows if r["category"] == second] == ["The one that must survive"]


# ------------------------------------------------- rejected rows counted ----


def test_unnamed_and_uncoordinated_rows_are_rejected_and_counted(monkeypatch):
    fake_endpoint(
        monkeypatch,
        {
            "museum": [
                binding("Q1", "Q1", sitelinks=4),  # label fell back to the Q-number
                binding("Q2", "No coordinate", coord=None),
                binding("Q3", "Unparseable", coord="somewhere near the river"),
                binding("Q4", "Keeps this one", sitelinks=4),
            ]
        },
    )
    stats = PlaceStats()
    rows = wikidata_places(CITY, 4000, "2026-09-24T00:00:00Z", stats)
    assert [r["name"] for r in rows] == ["Keeps this one"]
    assert stats.unnamed == 1
    assert stats.no_coord == 2
    assert stats.matched == 4
    assert stats.kept == 1


def test_rows_over_the_cap_are_counted_not_silently_dropped(monkeypatch):
    fake_endpoint(
        monkeypatch,
        {"museum": [binding(f"Q{i}", f"Museum {i}", sitelinks=i) for i in range(1, 6)]},
    )
    monkeypatch.setattr(fetch_content, "PER_CATEGORY_LIMIT", 2)
    stats = PlaceStats()
    wikidata_places(CITY, 4000, "2026-09-24T00:00:00Z", stats)
    assert stats.over_cap == 3


def test_the_default_cap_leaves_room_for_a_dense_city():
    """Ten was below what several categories in London and Rome actually hold."""
    assert PER_CATEGORY_LIMIT >= 20


# ------------------------------------------ a failure is not an empty city ----


def test_a_failed_class_is_recorded_rather_than_read_as_no_such_venues(monkeypatch):
    """The distinction the whole rewrite turns on.

    "No marinas here" and "we could not find out" must never be the same row
    count. A class whose query failed is named in the stats so the run can say
    what it does not know.
    """
    fake_endpoint(
        monkeypatch,
        {
            "museum": [binding("Q1", "A museum", sitelinks=9)],
            "monument": WikidataUnavailable("testville/monument: HTTP 502"),
        },
    )
    stats = PlaceStats()
    rows = wikidata_places(CITY, 4000, "2026-09-24T00:00:00Z", stats)
    assert [r["name"] for r in rows] == ["A museum"]
    assert stats.failed_classes == ["testville/monument"]
    assert "FAILED=testville/monument" in stats.summary()


def test_one_failed_class_does_not_abandon_the_rest_of_the_city(monkeypatch):
    fake_endpoint(
        monkeypatch,
        {
            "museum": WikidataUnavailable("boom"),
            "gallery": [binding("Q5", "Still collected", sitelinks=2)],
        },
    )
    rows = wikidata_places(CITY, 4000, "2026-09-24T00:00:00Z")
    assert [r["name"] for r in rows] == ["Still collected"]


def test_sparql_raises_after_exhausting_retries(monkeypatch):
    """It must raise, not return []. Returning [] is how a hole becomes a fact."""

    class Dead:
        status_code = 502
        headers: dict[str, str] = {}

    monkeypatch.setattr(fetch_content.requests, "get", lambda *a, **k: Dead())
    monkeypatch.setattr(fetch_content.time, "sleep", lambda _s: None)
    with pytest.raises(WikidataUnavailable):
        sparql("SELECT * WHERE {}", "testville/museum", attempts=3)


def test_sparql_waits_out_a_rate_limit_and_then_succeeds(monkeypatch):
    """429 means come back shortly, not "this city has no museums"."""
    calls = {"n": 0}

    class Limited:
        status_code = 429
        headers = {"Retry-After": "1"}

    class Fine:
        status_code = 200
        headers: dict[str, str] = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": {"bindings": [binding("Q1", "Arrived late")]}}

    def _get(*_a, **_k):
        calls["n"] += 1
        return Limited() if calls["n"] == 1 else Fine()

    monkeypatch.setattr(fetch_content.requests, "get", _get)
    monkeypatch.setattr(fetch_content.time, "sleep", lambda _s: None)
    rows = sparql("SELECT * WHERE {}", "testville/museum", attempts=3)
    assert len(rows) == 1


# ---------------------------------------------- bounded subclass traversal ----


def test_subclass_traversal_is_allowlisted_and_one_hop_only():
    """Unbounded `P279*` is not safe to trust here.

    Measured live: at full transitive depth, Q33506 (museum) went from 77
    items within 4km of Rome to 2077, pulling in whatever the ontology routes
    through "museum". One hop is bounded and checkable, so classes opt in
    individually and the path never widens beyond `wdt:P279?`.
    """
    for qid, _category in fetch_content.WIKIDATA_CLASSES:
        query = wikidata_class_query(qid, CITY, 4000)
        assert "wdt:P279*" not in query, "transitive closure is not allowlistable"
        if qid in WIKIDATA_SUBCLASS_CLASSES:
            assert "wdt:P31/wdt:P279?" in query
        else:
            assert "wdt:P279" not in query


def test_allowlisted_classes_are_ones_we_collect():
    unknown = WIKIDATA_SUBCLASS_CLASSES - {q for q, _c in fetch_content.WIKIDATA_CLASSES}
    assert not unknown, f"allowlisted but not collected: {sorted(unknown)}"


def test_the_general_catch_all_class_is_ranked_last():
    """`attraction` must not outrank a sharper category during deduplication.

    Q570116 (tourist attraction) is general enough that one subclass hop
    reaches museums, parks and monuments alike. Listed early it would absorb
    them and flatten the category vocabulary into a single bucket.
    """
    categories = [category for _qid, category in fetch_content.WIKIDATA_CLASSES]
    assert categories[-1] == "attraction"


def test_a_subclass_of_another_collected_class_is_listed_first():
    """Verified against the endpoint: Q207694 (art museum) ⊂ Q33506 (museum).

    With `museum` first, a one-hop museum query claims every art gallery and
    `gallery` deduplicates down to near nothing -- while the total row count
    goes *up*, so the snapshot looks healthier as the category empties.
    """
    categories = [category for _qid, category in fetch_content.WIKIDATA_CLASSES]
    assert categories.index("gallery") < categories.index("museum")


# ------------------------------- a venue is not a schedule (see also F3) ----


def test_a_place_row_carries_no_opening_hours_or_availability(monkeypatch):
    """A concert hall is not evidence of a concert this week.

    The ingestor's half of that rule is simply never to collect the fields
    that would imply it. A row says a venue exists, where it is, who says so
    and as of when -- and nothing about whether it is open, bookable, or
    hosting anything. The grounding rules that keep the *agent* from implying
    a schedule live in the planner and router; this is the boundary that
    stops such a claim entering the snapshot in the first place.
    """
    fake_endpoint(monkeypatch, {"concert_hall": [binding("Q1", "A concert hall", sitelinks=30)]})
    rows = wikidata_places(CITY, 4000, "2026-09-24T00:00:00Z")
    assert rows, "expected the fake endpoint to yield a row"
    forbidden = {
        "opening_hours",
        "hours",
        "availability",
        "available",
        "open",
        "schedule",
        "starts_at",
        "ends_at",
        "event",
        "tickets",
        "booking",
    }
    for row in rows:
        leaked = forbidden & set(row)
        assert not leaked, f"a place row must not claim {sorted(leaked)}"
        # What it must carry instead: provenance and an as-of date.
        assert row["source"] and row["source_url"] and row["as_of"]
        assert row["is_sample"] is False


def test_the_osm_path_does_not_copy_schedule_tags():
    """OSM elements often carry `opening_hours`; the row builder ignores them.

    Asserted against the tag map rather than a live fetch: only the tags
    listed there are ever read, so a schedule tag cannot arrive by accident.
    """
    collected_keys = {key for key, _value, _category in fetch_content.OSM_CATEGORIES}
    assert "opening_hours" not in collected_keys
    assert collected_keys <= {"amenity", "tourism", "historic", "leisure", "shop", "natural"}


def test_osm_query_groups_do_not_duplicate_a_place_or_consume_its_cap(monkeypatch):
    shared = {
        "type": "node",
        "id": 42,
        "lat": 10.0,
        "lon": 20.0,
        "tags": {"name": "Shared", "amenity": "theatre", "natural": "beach"},
    }
    distinct = {
        "type": "node",
        "id": 43,
        "lat": 10.1,
        "lon": 20.1,
        "tags": {"name": "Distinct", "amenity": "theatre"},
    }
    monkeypatch.setattr(
        fetch_content,
        "OSM_CATEGORIES",
        [
            ("amenity", "theatre", "theatre"),
            ("amenity", "cafe", "cafe"),
            ("tourism", "museum", "museum"),
            ("leisure", "park", "park"),
            ("natural", "beach", "beach"),
        ],
    )
    batches = iter([[shared], [shared, distinct]])
    monkeypatch.setattr(fetch_content, "overpass_fetch", lambda _query: next(batches))
    monkeypatch.setattr(fetch_content.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(fetch_content, "PER_CATEGORY_LIMIT", 2)

    rows = fetch_content.fetch_places([CITY], 4000, "osm")

    assert [row["id"] for row in rows] == ["osm:node/42", "osm:node/43"]
