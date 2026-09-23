"""Local coordinates and coastline for the offline places overview.

The browser receives Plotly traces only. No map tile URL or remote asset is
needed to pan, zoom, inspect a marker, or view the coastline.
"""

from __future__ import annotations

import gzip
import json
import math
from functools import lru_cache
from pathlib import Path

KM_PER_DEGREE = 111.32


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


@lru_cache(maxsize=1)
def _coastlines() -> tuple[tuple[tuple[float, float], ...], ...]:
    # The source tree and the UI image put data in different relative places.
    here = Path(__file__).resolve()
    candidates = (
        here.parent / "data/map/ne_10m_coastline.geojson.gz",
        here.parent.parent.parent / "data/map/ne_10m_coastline.geojson.gz",
    )
    path = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
    with gzip.open(path, mode="rt", encoding="utf-8") as handle:
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
