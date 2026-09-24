"""Stage the offline basemap for the places map, while connected.

    docker compose -f compose.yml -f compose.connected.yml run --rm --no-deps \
        ingestor python -m services.ingestor.fetch_basemap

One bounded Overpass query per city -- a 20 km box around the city centre --
for the geometry that makes a marker plot read as a city: major and minor
streets, water, and parks. The result is written to
`data/map/<slug>.basemap.geojson.gz` and committed, exactly like the Natural
Earth coastline beside it. The runtime never calls Overpass, never calls a tile
server, and never downloads anything: the UI opens the gzipped file out of its
own image.

Source and licence: OpenStreetMap contributors, ODbL. The same source and the
same licence the places snapshot already carries when fetched with
`--places-source osm`; `data/map/SOURCE.md` records the query and the date.

This is a staging script. The reviewer never runs it.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import math
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
import yaml

from ..common import config

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s basemap %(message)s")
log = logging.getLogger("basemap")

# The same four mirrors `fetch_content` uses, for the same reason: they are a
# free shared service and any one of them may be refusing connections.
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.osm.jp/api/interpreter",
]
USER_AGENT = "act-on-weather/1.0 (take-home project; https://github.com/; contact via repository)"

KM_PER_DEGREE = 111.32

# Which OSM tags become which drawing layer. Everything else in the box is
# dropped rather than guessed at -- this is a backdrop for markers, not an
# attempt to reproduce OSM.
#
# `road_major` is the skeleton you recognise a city by; `road_minor` is the
# texture that makes the skeleton read as streets. Residential roads are
# deliberately excluded: they roughly quadruple the file for detail that is
# sub-pixel at a 10 km view.
ROAD_MAJOR = ("motorway", "trunk", "primary")
ROAD_MINOR = ("secondary", "tertiary")

# Layers drawn as filled shapes. A way in one of these that OSM has not closed
# cannot be filled, so it is dropped rather than shipped as a stray outline:
# the UI is then free to assume "park and water are polygons, everything else
# is a line" instead of branching per feature.
AREA_LAYERS = ("water", "park")

# Half-width of the staged box, in kilometres. The map view sizes itself to
# the stored places, which is ±5 km in all five cities, so 10 km leaves room
# to pan and zoom out without running off the edge of the geometry.
HALF_BOX_KM = 10.0

# Douglas-Peucker tolerance, in metres. A 20 km wide plot in a browser is
# roughly 20 m per pixel, so 12 m is under half a pixel: the simplification is
# invisible at every zoom the view offers and removes most of the vertices.
SIMPLIFY_M = 12.0

# Coordinate precision in the stored file. 5 decimal places is about 1 m,
# which is finer than the simplification above and keeps the file small.
PRECISION = 5


def load_cities() -> list[dict[str, Any]]:
    with open(config.DATA_DIR / "cities.yml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)["cities"]


def bbox(city: dict[str, Any], half_km: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) for a box of `half_km` around the centre."""
    lat, lon = float(city["lat"]), float(city["lon"])
    d_lat = half_km / KM_PER_DEGREE
    d_lon = half_km / (KM_PER_DEGREE * math.cos(math.radians(lat)))
    return (lat - d_lat, lon - d_lon, lat + d_lat, lon + d_lon)


def overpass_query(box: tuple[float, float, float, float]) -> str:
    area = "{:.5f},{:.5f},{:.5f},{:.5f}".format(*box)
    roads = "|".join(ROAD_MAJOR + ROAD_MINOR)
    # `out geom` returns each way's coordinates inline, so no second pass to
    # resolve node ids. Relations are not requested: a multipolygon lake adds
    # member-resolution code for geometry that the closed ways already cover.
    return (
        "[out:json][timeout:180];\n"
        "(\n"
        f'  way["highway"~"^({roads})$"]({area});\n'
        f'  way["natural"="water"]({area});\n'
        f'  way["natural"="coastline"]({area});\n'
        f'  way["waterway"~"^(river|canal)$"]({area});\n'
        f'  way["leisure"~"^(park|garden)$"]({area});\n'
        ");\n"
        "out geom;"
    )


class OverpassUnavailable(Exception):
    pass


def overpass_fetch(query: str) -> list[dict[str, Any]]:
    """POST to whichever mirror answers, or raise.

    A "remark" in the body means the server gave up mid-query and the short
    element list is a lie, so it is treated as a failure -- never as "this city
    has no streets".
    """
    last = "no attempt made"
    for url in OVERPASS_URLS:
        for attempt in range(2):
            try:
                response = requests.post(
                    url, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=300
                )
                response.raise_for_status()
                body = response.json()
            except (requests.RequestException, ValueError) as exc:
                last = str(exc)
                log.warning("overpass %s failed (%s)", url.split("/")[2], last[:120])
                time.sleep(10 * (attempt + 1))
                continue
            if body.get("remark"):
                last = body["remark"].strip()
                log.warning("overpass %s remarked: %s", url.split("/")[2], last[:120])
                time.sleep(10 * (attempt + 1))
                continue
            return body.get("elements", [])
    raise OverpassUnavailable(last)


def layer_of(tags: dict[str, str]) -> str | None:
    """Which drawing layer an element belongs to, or None to drop it."""
    highway = tags.get("highway")
    if highway in ROAD_MAJOR:
        return "road_major"
    if highway in ROAD_MINOR:
        return "road_minor"
    if tags.get("natural") == "water":
        return "water"
    if tags.get("natural") == "coastline":
        # OSM's own shoreline, staged for the same reason as the streets: at a
        # 10 km view Natural Earth's 1:10m line runs straight through
        # Reykjavik's street grid, and a coast that disagrees with the roads
        # beside it is worse than no coast.
        return "coastline"
    if tags.get("waterway") in ("river", "canal"):
        return "waterway"
    if tags.get("leisure") in ("park", "garden"):
        return "park"
    return None


def simplify(points: list[tuple[float, float]], tolerance_m: float, lat: float) -> list[int]:
    """Douglas-Peucker. Returns the indices to keep, in order.

    Iterative rather than recursive: a long river in OSM is a single way with
    thousands of vertices, and Python's recursion limit is not something to
    find out about during staging.
    """
    if len(points) < 3:
        return list(range(len(points)))
    # Work in metres so the tolerance means what it says at this latitude.
    scale_x = KM_PER_DEGREE * 1000 * math.cos(math.radians(lat))
    scale_y = KM_PER_DEGREE * 1000
    xy = [((lon * scale_x), (lat_ * scale_y)) for lon, lat_ in points]

    keep = [False] * len(xy)
    keep[0] = keep[-1] = True
    stack = [(0, len(xy) - 1)]
    while stack:
        start, end = stack.pop()
        ax, ay = xy[start]
        bx, by = xy[end]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        worst, worst_index = 0.0, -1
        for index in range(start + 1, end):
            px, py = xy[index]
            if length == 0:
                distance = math.hypot(px - ax, py - ay)
            else:
                distance = abs(dy * px - dx * py + bx * ay - by * ax) / length
            if distance > worst:
                worst, worst_index = distance, index
        if worst > tolerance_m and worst_index > 0:
            keep[worst_index] = True
            stack.append((start, worst_index))
            stack.append((worst_index, end))
    return [index for index, flag in enumerate(keep) if flag]


def to_feature(element: dict[str, Any], layer: str, lat: float) -> dict[str, Any] | None:
    """One OSM way -> one GeoJSON feature, simplified and rounded.

    Closed ways in an area layer become Polygons so the UI can fill them; every
    other way is a LineString. No name, no tags, no id: the basemap carries
    geometry and nothing else, because nothing else is drawn from it.
    """
    geometry = element.get("geometry") or []
    points = [(float(p["lon"]), float(p["lat"])) for p in geometry if "lon" in p and "lat" in p]
    if len(points) < 2:
        return None
    if layer in AREA_LAYERS and points[0] != points[-1]:
        return None

    kept = [points[index] for index in simplify(points, SIMPLIFY_M, lat)]
    rounded: list[list[float]] = []
    for lon, lat_ in kept:
        point = [round(lon, PRECISION), round(lat_, PRECISION)]
        if not rounded or rounded[-1] != point:
            rounded.append(point)
    if len(rounded) < 2:
        return None

    if layer in AREA_LAYERS:
        if rounded[0] != rounded[-1]:
            rounded.append(list(rounded[0]))
        if len(rounded) < 4:
            return None
        shape = {"type": "Polygon", "coordinates": [rounded]}
    else:
        shape = {"type": "LineString", "coordinates": rounded}
    return {"type": "Feature", "properties": {"layer": layer}, "geometry": shape}


def build_city(city: dict[str, Any], half_km: float) -> dict[str, Any]:
    box = bbox(city, half_km)
    elements = overpass_fetch(overpass_query(box))
    features: list[dict[str, Any]] = []
    for element in elements:
        layer = layer_of(element.get("tags") or {})
        if layer is None:
            continue
        feature = to_feature(element, layer, float(city["lat"]))
        if feature is not None:
            features.append(feature)

    counts: dict[str, int] = {}
    vertices = 0
    for feature in features:
        counts[feature["properties"]["layer"]] = counts.get(feature["properties"]["layer"], 0) + 1
        coords = feature["geometry"]["coordinates"]
        vertices += len(coords[0]) if feature["geometry"]["type"] == "Polygon" else len(coords)
    log.info(
        "%s: %d ways -> %d features, %d vertices %s",
        city["slug"],
        len(elements),
        len(features),
        vertices,
        counts,
    )
    return {
        "type": "FeatureCollection",
        # Provenance travels with the data, the way every other row in this
        # project does. The UI reads `as_of` and shows it under the map.
        "properties": {
            "city": city["slug"],
            "source": "OpenStreetMap contributors via Overpass API",
            "source_url": "https://www.openstreetmap.org/copyright",
            "licence": "ODbL 1.0",
            "as_of": datetime.now(UTC).isoformat(),
            "bbox": [round(value, 5) for value in box],
            "half_box_km": half_km,
            "simplify_m": SIMPLIFY_M,
        },
        "features": features,
    }


def write_gzip(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0 so re-staging unchanged geometry produces an identical file and
    # `git status` stays honest about whether the data actually moved.
    with gzip.GzipFile(path, mode="wb", compresslevel=9, mtime=0) as handle:
        handle.write(json.dumps(document, separators=(",", ":")).encode("utf-8"))
    log.info("wrote %s (%.1f MB)", path, path.stat().st_size / 1e6)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", action="append", help="slug; repeatable. Default: all five.")
    parser.add_argument(
        "--half-box-km",
        type=float,
        default=HALF_BOX_KM,
        help="half-width of the staged box around each city centre",
    )
    args = parser.parse_args(argv)

    cities = load_cities()
    if args.city:
        wanted = set(args.city)
        cities = [city for city in cities if city["slug"] in wanted]
        missing = wanted - {city["slug"] for city in cities}
        if missing:
            parser.error(f"unknown city slug(s): {sorted(missing)}")

    out = config.DATA_DIR / "map"
    for city in cities:
        document = build_city(city, args.half_box_km)
        write_gzip(out / f"{city['slug']}.basemap.geojson.gz", document)
        # Overpass is a free shared service. One query per city, spaced out.
        if city is not cities[-1]:
            time.sleep(5)

    log.info("basemap written to %s -- review the diff and commit it", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
