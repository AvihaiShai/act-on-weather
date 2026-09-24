"""Local coordinates and offline basemap for the places overview.

The browser receives Plotly traces only. No map tile URL or remote asset is
needed to pan, zoom, inspect a marker, or read the streets under it.

Two files feed the backdrop, both gzipped GeoJSON shipped inside the image:

  * `data/map/<slug>.basemap.geojson.gz` -- a 20 km OpenStreetMap extract per
    city (ODbL), staged by `services/ingestor/fetch_basemap.py`: streets,
    water, coastline and parks. This is what makes a city read as a city
    rather than as dots on blank fill, and it is what the map draws.
  * `data/map/ne_10m_coastline.geojson.gz` -- Natural Earth 1:10m, public
    domain. The fallback, drawn only for a city with no staged extract. At
    1:10m it is generalized to roughly a kilometre, which at this view is
    visibly wrong next to streets accurate to a few metres, so the two are
    never drawn together.

Both are read from disk and projected here; `data/map/SOURCE.md` records where
they came from and what was traded away to keep them small.
"""

from __future__ import annotations

import gzip
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

KM_PER_DEGREE = 111.32

# Drawing order, bottom to top. The filled layers come first so the street
# network sits on top of the park and water shapes rather than under them.
# `AREA_LAYERS` is an invariant of the staged file, not a guess: the staging
# script drops any way in those layers that OSM has not closed.
AREA_LAYERS = ("park", "water")
BASEMAP_LAYERS = ("coastline", "park", "water", "waterway", "road_minor", "road_major")

# Basemap traces are named `basemap:<layer>`. The prefix is not decoration:
# `park` is both a basemap layer and a place category, and the markers for a
# category are named after it, so without it the figure holds two traces
# called `park`.
TRACE_PREFIX = "basemap:"


def local_xy(lat: float, lon: float, center_lat: float, center_lon: float) -> tuple[float, float]:
    """Approximate east/north kilometres around one city (a small map extent)."""
    east = (lon - center_lon) * KM_PER_DEGREE * math.cos(math.radians(center_lat))
    north = (lat - center_lat) * KM_PER_DEGREE
    return east, north


def extent(
    center_lat: float, center_lon: float, places: list[dict]
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Keep every stored place visible, with a useful minimum city-level view."""
    points = [
        local_xy(float(p["lat"]), float(p["lon"]), center_lat, center_lon)
        for p in places
        if p.get("lat") is not None and p.get("lon") is not None
    ]
    half_width = max([3.0, *(abs(x) + 0.9 for x, _ in points)])
    half_height = max([3.0, *(abs(y) + 0.9 for _, y in points)])
    return (-half_width, half_width), (-half_height, half_height)


def _map_file(name: str) -> Path:
    # The source tree and the UI image put data in different relative places.
    here = Path(__file__).resolve()
    candidates = (
        here.parent / "data/map" / name,
        here.parent.parent.parent / "data/map" / name,
    )
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[0])


@lru_cache(maxsize=1)
def _coastlines() -> tuple[tuple[tuple[float, float], ...], ...]:
    with gzip.open(_map_file("ne_10m_coastline.geojson.gz"), mode="rt", encoding="utf-8") as handle:
        features = json.load(handle)["features"]
    lines = []
    for feature in features:
        geometry = feature["geometry"]
        parts = (
            [geometry["coordinates"]]
            if geometry["type"] == "LineString"
            else geometry["coordinates"]
        )
        lines.extend(tuple((float(lon), float(lat)) for lon, lat in part) for part in parts)
    return tuple(lines)


@lru_cache(maxsize=16)
def coastline_xy(
    center_lat: float,
    center_lon: float,
    x_range: tuple[float, float],
    y_range: tuple[float, float],
) -> tuple[list[float | None], list[float | None]]:
    """Return only segments crossing this view; None separates disconnected coasts."""
    xs: list[float | None] = []
    ys: list[float | None] = []
    for line in _coastlines():
        previous = None
        for lon, lat in line:
            point = local_xy(lat, lon, center_lat, center_lon)
            if previous is not None:
                px, py = previous
                x, y = point
                if (
                    min(px, x) <= x_range[1]
                    and max(px, x) >= x_range[0]
                    and min(py, y) <= y_range[1]
                    and max(py, y) >= y_range[0]
                ):
                    xs.extend((px, x, None))
                    ys.extend((py, y, None))
            previous = point
    return xs, ys


# ------------------------------------------------------------- basemap ----


class Basemap(NamedTuple):
    """One city's staged OpenStreetMap extract, as read off disk."""

    #: `source`, `licence`, `as_of`, the staged bounding box -- written by the
    #: staging script so the UI can name its provenance without hard-coding it.
    properties: dict[str, Any]
    #: layer name -> one (lon, lat) ring or polyline per way
    layers: dict[str, tuple[tuple[tuple[float, float], ...], ...]]

    def __bool__(self) -> bool:
        return bool(self.layers)


EMPTY_BASEMAP = Basemap(properties={}, layers={})


@lru_cache(maxsize=8)
def basemap(slug: str) -> Basemap:
    """Read `data/map/<slug>.basemap.geojson.gz`, or return an empty basemap.

    A missing file is not an error: the map still draws its markers, axes and
    coastline, and the caption says there is no street layer for that city. A
    city added to `data/cities.yml` without a staging run should degrade, not
    raise, in front of the reviewer.
    """
    path = _map_file(f"{slug}.basemap.geojson.gz")
    if not path.is_file():
        return EMPTY_BASEMAP
    with gzip.open(path, mode="rt", encoding="utf-8") as handle:
        document = json.load(handle)

    layers: dict[str, list[tuple[tuple[float, float], ...]]] = {}
    for feature in document.get("features", ()):
        layer = feature.get("properties", {}).get("layer")
        if layer not in BASEMAP_LAYERS:
            continue
        geometry = feature["geometry"]
        # Geometry that disagrees with its layer is skipped rather than drawn:
        # an open way in an area layer would fill as a wedge across the city.
        if (geometry["type"] == "Polygon") != (layer in AREA_LAYERS):
            continue
        rings = geometry["coordinates"] if layer in AREA_LAYERS else [geometry["coordinates"]]
        for ring in rings:
            layers.setdefault(layer, []).append(
                tuple((float(lon), float(lat)) for lon, lat in ring)
            )
    return Basemap(
        properties=document.get("properties", {}),
        layers={name: tuple(rows) for name, rows in layers.items()},
    )


@lru_cache(maxsize=16)
def basemap_xy(
    slug: str,
    center_lat: float,
    center_lon: float,
    x_range: tuple[float, float],
    y_range: tuple[float, float],
) -> dict[str, tuple[list[float | None], list[float | None]]]:
    """Project one city's basemap into view kilometres, clipped to the view.

    Whole ways are kept or dropped by their bounding box rather than split at
    the edge: Plotly clips to the axis range anyway, and splitting a ring would
    break the `toself` fill that draws a lake as water instead of as an
    outline. Returns `{layer: (xs, ys)}` with `None` separating each way, which
    is one trace per layer rather than one per street.
    """
    projected: dict[str, tuple[list[float | None], list[float | None]]] = {}
    for layer in BASEMAP_LAYERS:
        xs: list[float | None] = []
        ys: list[float | None] = []
        for ring in basemap(slug).layers.get(layer, ()):
            points = [local_xy(lat, lon, center_lat, center_lon) for lon, lat in ring]
            left = min(x for x, _ in points)
            right = max(x for x, _ in points)
            bottom = min(y for _, y in points)
            top = max(y for _, y in points)
            if left > x_range[1] or right < x_range[0] or bottom > y_range[1] or top < y_range[0]:
                continue
            xs.extend(x for x, _ in points)
            ys.extend(y for _, y in points)
            xs.append(None)
            ys.append(None)
        if xs:
            projected[layer] = (xs, ys)
    return projected
