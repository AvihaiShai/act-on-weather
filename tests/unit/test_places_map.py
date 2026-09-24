"""The places map draws a city, not markers on an empty panel.

Until the OpenStreetMap extract was staged, the only basemap geometry in the
build was Natural Earth's 1:10m coastline, and at a city-scale view that layer
is either the whole picture or entirely absent: Rome and London -- the two
cities the brief's own example questions name -- rendered as coloured dots on
flat fill with no ground reference at all. Nothing raised, so the existing
"the tab produced a figure" assertion in `test_ui.py` passed throughout.

So the regression this file guards is a visual one, and it is asserted twice:

  * against the staged files, that an inland city has street and water geometry
    inside the view the map actually opens at; and
  * against the rendered app, that those layers reach the figure.

Everything here reads files from the repo. Nothing opens a socket -- the
staging script that produced `data/map/*.basemap.geojson.gz` needs the
internet, the runtime and these tests do not.
"""

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest
import streamlit as st
import yaml
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[2]
UI_DIR = ROOT / "services" / "ui"
APP = UI_DIR / "app.py"

# `app.py` imports its siblings as top-level modules, the way `streamlit run`
# from that directory does. AppTest does not set that up, so we do.
if str(UI_DIR) not in sys.path:
    sys.path.insert(0, str(UI_DIR))

import places_map  # noqa: E402

CITIES = yaml.safe_load((ROOT / "data" / "cities.yml").read_text(encoding="utf-8"))["cities"]
SNAPSHOT_PLACES = [
    json.loads(line)
    for line in (ROOT / "data" / "snapshot" / "places.jsonl")
    .read_text(encoding="utf-8")
    .splitlines()
    if line.strip()
]

# The two the brief names, and the two with no coastline anywhere near the
# view: if the basemap ever stops shipping, these are the cities that go blank.
INLAND_VIEW = ["rome", "london"]


def city(slug: str) -> dict:
    return next(c for c in CITIES if c["slug"] == slug)


def view_of(slug: str):
    """The extent the map actually opens at, from the committed snapshot."""
    place = city(slug)
    stored = [p for p in SNAPSHOT_PLACES if p.get("city_id") == slug]
    return place, places_map.extent(float(place["lat"]), float(place["lon"]), stored)


# ------------------------------------------------------- the staged files ----


@pytest.mark.parametrize("slug", [c["slug"] for c in CITIES])
def test_every_city_ships_a_basemap_extract(slug):
    """A city in `cities.yml` with no staged extract is a blank panel in the
    UI. That is a staging omission, and it should fail here rather than be
    discovered by the reviewer clicking the tab."""
    staged = places_map.basemap(slug)
    assert staged, f"no basemap staged for {slug}"
    assert staged.properties["licence"] == "ODbL 1.0"
    assert "OpenStreetMap" in staged.properties["source"]
    assert staged.properties["as_of"], "the extract carries no as-of stamp"


@pytest.mark.parametrize("slug", INLAND_VIEW)
def test_an_inland_city_has_street_geometry_in_view(slug):
    """The failure this replaces: at a ~5 km view, Rome and London had zero
    coastline segments and no other geometry, so the panel was empty."""
    place, (x_range, y_range) = view_of(slug)
    assert not places_map.coastline_xy(float(place["lat"]), float(place["lon"]), x_range, y_range)[
        0
    ], f"{slug} unexpectedly has coastline in view -- this test is checking the wrong thing"

    projected = places_map.basemap_xy(
        slug, float(place["lat"]), float(place["lon"]), x_range, y_range
    )
    # Not "some geometry exists": a street network. A handful of vertices would
    # satisfy a truthiness check and still look like an empty page.
    roads = len(projected.get("road_major", ([], []))[0]) + len(
        projected.get("road_minor", ([], []))[0]
    )
    assert roads > 2000, f"{slug} has only {roads} street vertices in view"
    assert projected.get("water") or projected.get("waterway"), f"{slug} has no water in view"


@pytest.mark.parametrize("slug", ["reykjavik", "tel-aviv", "lisbon"])
def test_a_coastal_city_draws_its_shoreline_from_the_same_extract(slug):
    """The shoreline has to come from the same source as the streets.

    Natural Earth's 1:10m coastline is generalized to roughly a kilometre, and
    over Reykjavik at this view it ran several hundred metres inland of streets
    accurate to a few metres -- two layers visibly disagreeing about where the
    sea is. `natural=coastline` from the city's own extract cannot disagree
    with the roads beside it, so the map prefers it and keeps Natural Earth
    only for a city with no extract at all.
    """
    place, (x_range, y_range) = view_of(slug)
    projected = places_map.basemap_xy(
        slug, float(place["lat"]), float(place["lon"]), x_range, y_range
    )
    assert projected.get("coastline"), f"{slug} has no OSM shoreline in view"


def test_area_layers_are_closed_rings():
    """Parks and water are drawn with `fill='toself'`. An unclosed ring fills
    as a wedge across the city, so the staging script drops open ways and the
    loader skips any that slip through -- asserted here against real data."""
    for slug in INLAND_VIEW:
        for layer in places_map.AREA_LAYERS:
            for ring in places_map.basemap(slug).layers.get(layer, ()):
                assert ring[0] == ring[-1], f"{slug}/{layer} has an unclosed ring"


@pytest.mark.parametrize("slug", [c["slug"] for c in CITIES])
def test_the_staged_box_is_centred_on_the_city(slug):
    """The caption under the map tells the reviewer the backdrop covers 20 km
    around the city centre. That claim comes from the file, so it is worth
    checking the file agrees -- a box staged against the wrong centre would
    draw a plausible-looking city that is not the one on the label.

    Not asserted: that every vertex is inside the box. Overpass returns a way
    whole when it crosses the boundary, so a motorway can run kilometres past
    the edge. The view clips it, and the surplus is free geometry."""
    place = city(slug)
    south, west, north, east = places_map.basemap(slug).properties["bbox"]
    assert south < float(place["lat"]) < north
    assert west < float(place["lon"]) < east
    # 20 km of latitude is 0.1796 degrees at any latitude.
    assert north - south == pytest.approx(0.1796, abs=0.001)


# ---------------------------------------------------------- the rendered app ----

FIXTURES = json.loads((ROOT / "tests" / "fixtures" / "ui_api.json").read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.ok = True
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


@pytest.fixture
def london_app(monkeypatch) -> AppTest:
    """The real app, with `/places` answering for London.

    `tests/fixtures/ui_api.json` holds Lisbon places only, and the map sizes
    its view to whatever `/places` returns -- served against London's centre
    those rows produce a 1,500 km extent, which would make any assertion about
    "geometry in view" meaningless. So this fixture serves London's own rows
    from the committed snapshot and leaves every other endpoint to the shared
    fixture.
    """
    import requests

    # Shape from the shared fixture, which is captured from the running API;
    # coordinates and names from the committed snapshot. Taking the shape from
    # the fixture means a field the API adds later is still present here, so
    # this test fails on a real mismatch rather than on its own stub.
    template = FIXTURES["places"][0]
    london = [
        {
            **template,
            "id": row["id"],
            "name": row["name"],
            "category": row["category"],
            "city_id": "london",
            "lat": row["lat"],
            "lon": row["lon"],
        }
        for row in SNAPSHOT_PLACES
        if row.get("city_id") == "london" and row.get("lat") is not None
    ]
    assert london, "the committed snapshot has no London places"

    def fake_get(url, params=None, timeout=None, **kwargs):
        path = urlparse(url).path
        key = path.strip("/").split("/")[0]
        if key == "places":
            return FakeResponse(london)
        if key not in FIXTURES:
            raise AssertionError(f"the UI called {path}, which the fixture does not cover")
        return FakeResponse(FIXTURES[key])

    def fake_request(method, url, json=None, timeout=None, **kwargs):
        return FakeResponse({"accepted": True, "message_id": "test-message-id"})

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "request", fake_request)
    st.cache_data.clear()

    # Generous: the first render parses a 3 MB coastline and a 0.5 MB extract,
    # and this runs the script twice -- once to draw, once after picking the
    # city. A timeout here would be a flaky failure, not a useful one.
    app = AppTest.from_file(str(APP), default_timeout=240).run()
    app.selectbox(key="map_city").select("London").run()
    return app


def test_the_rendered_map_puts_a_basemap_under_londons_markers(london_app):
    """The UI-level half of the regression.

    `test_ui.py` asserts that the tab produces a figure, and a figure holding
    nothing but markers passed that check for the whole of the blank-panel
    period. This reads the figure spec Streamlit actually serialises for the
    browser and asserts the backdrop is in it.

    Streamlit has no typed test wrapper for `st.plotly_chart`, so the element
    arrives as an `UnknownElement` whose `.value` is None; `.proto.spec` is the
    figure JSON, which is the payload itself rather than a proxy for it.
    """
    assert not london_app.exception, [e.value for e in london_app.exception]

    traces = [
        trace
        for chart in london_app.get("plotly_chart")
        for trace in json.loads(chart.proto.spec).get("data", [])
    ]
    # The prefix matters: `park` names both a basemap layer and a place
    # category, so an unprefixed lookup would find the marker trace instead.
    by_name = {
        trace["name"].removeprefix(places_map.TRACE_PREFIX): trace
        for trace in traces
        if str(trace.get("name", "")).startswith(places_map.TRACE_PREFIX)
    }

    for layer in ("road_major", "road_minor"):
        assert layer in by_name, f"the map drew no {layer} trace: {sorted(by_name)}"
    assert by_name.keys() & {"water", "waterway"}, "the map drew no water"

    streets = sum(len(by_name[layer]["x"]) for layer in ("road_major", "road_minor"))
    assert streets > 2000, f"only {streets} street vertices reached the figure"

    # Parks and water are filled, not outlined. A `toself` fill is what makes
    # the Thames read as water rather than as two parallel lines.
    for layer in places_map.AREA_LAYERS:
        if layer in by_name:
            assert by_name[layer]["fill"] == "toself"

    # And it came from the bundle, not from a tile server: the caption names
    # the licence, which is the one thing a tile map would not have needed.
    captions = " ".join(element.value for element in london_app.caption)
    assert "OpenStreetMap contributors, ODbL" in captions
    assert "no online map tiles" in captions
