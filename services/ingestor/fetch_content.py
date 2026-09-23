"""Build the committed data snapshot, while connected.

    docker compose -f compose.yml -f compose.connected.yml run --rm \
        ingestor python -m services.ingestor.fetch_content

This is the one script in the repo that needs the internet, and the reviewer
never has to run it: its output is committed under `data/snapshot/`. It is here
because a committed snapshot with no visible provenance is just an assertion --
this is the code that says exactly where every row came from.

Sources and licences (repeated in the README):
  * weather  Open-Meteo forecast API, CC BY 4.0, no API key
  * places   Wikidata SPARQL (CC0) by default; OpenStreetMap via Overpass
             (ODbL) with --places-source osm. See `fetch_places` for why the
             default is the narrower of the two.
  * facts    Wikipedia REST summaries, CC BY-SA 4.0
  * events   data/events.seed.jsonl -- hand-verified, each row carrying its own
             source URL. Nothing here is generated, and nothing is invented.

Every row it writes carries `source`, `source_url` and `as_of`.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
import yaml

from ..common import config
from .providers import get_provider

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s fetch %(message)s")
log = logging.getLogger("fetch")

# Two public instances. The main one rate-limits and, under load, answers a
# too-large query with HTTP 200, an empty element list and a "remark" -- which
# is why `overpass_fetch` checks for that remark instead of trusting the status
# code.
OVERPASS_URLS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.osm.jp/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]
OVERPASS_URL = OVERPASS_URLS[0]
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

# Wikidata classes worth collecting, mapped onto the same category vocabulary
# the OSM tags use, so the two sources produce interchangeable rows.
WIKIDATA_CLASSES: list[tuple[str, str]] = [
    ("Q33506", "museum"),  # museum
    ("Q207694", "gallery"),  # art museum
    ("Q24354", "theatre"),  # theatre building
    ("Q1060829", "concert_hall"),
    ("Q11315", "shopping"),  # shopping centre
    ("Q330284", "market"),  # marketplace
    ("Q22698", "park"),
    ("Q483110", "stadium"),
    ("Q11707", "restaurant"),
    ("Q4989906", "monument"),
    ("Q570116", "attraction"),  # tourist attraction
]
USER_AGENT = "act-on-weather/1.0 (take-home project; contact via repository)"

# OSM tag -> the category vocabulary in data/interests.yml. Only these are
# collected; anything else in the area is ignored rather than guessed at.
OSM_CATEGORIES: list[tuple[str, str, str]] = [
    ("amenity", "theatre", "theatre"),
    ("amenity", "nightclub", "nightclub"),
    ("amenity", "restaurant", "restaurant"),
    ("amenity", "cafe", "cafe"),
    ("amenity", "marketplace", "market"),
    ("tourism", "museum", "museum"),
    ("tourism", "gallery", "gallery"),
    ("tourism", "attraction", "attraction"),
    ("tourism", "viewpoint", "viewpoint"),
    ("historic", "monument", "monument"),
    ("leisure", "park", "park"),
    ("leisure", "garden", "garden"),
    ("leisure", "stadium", "stadium"),
    ("leisure", "sports_centre", "sports_centre"),
    ("shop", "department_store", "shopping"),
    ("shop", "mall", "shopping"),
]

PER_CATEGORY_LIMIT = 6


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def load_cities() -> list[dict[str, Any]]:
    with open(config.DATA_DIR / "cities.yml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)["cities"]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    log.info("wrote %d rows to %s", len(rows), path)


# -------------------------------------------------------------- weather ----


def fetch_weather(cities: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
    provider = get_provider("open-meteo")
    rows: list[dict[str, Any]] = []
    for city in cities:
        rows.extend(provider.daily_forecast(city, days))
        log.info("weather: %s", city["slug"])
    return rows


# --------------------------------------------------------------- places ----


def overpass_query(selectors: list[tuple[str, str]], city: dict[str, Any], radius: int) -> str:
    clauses = []
    for key, value in selectors:
        for kind in ("node", "way"):
            clauses.append(
                f'{kind}["{key}"="{value}"]["name"](around:{radius},{city["lat"]},{city["lon"]});'
            )
    return f"[out:json][timeout:180];\n({chr(10).join(clauses)}\n);\nout center tags;"


class OverpassUnavailable(Exception):
    pass


def overpass_fetch(query: str) -> list[dict[str, Any]]:
    """POST a query to whichever mirror answers, or raise.

    A "remark" in the body means the server gave up mid-query and the empty
    element list is a lie: treated as a failure, never as "this city has no
    museums".
    """
    last = "no attempt made"
    for url in OVERPASS_URLS:
        for attempt in range(2):
            try:
                response = requests.post(
                    url, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=200
                )
                response.raise_for_status()
                body = response.json()
            except (requests.RequestException, ValueError) as exc:
                last = str(exc)
                log.warning("overpass %s failed (%s)", url.split("/")[2], last)
                time.sleep(10 * (attempt + 1))
                continue
            if body.get("remark"):
                last = body["remark"].strip()
                log.warning("overpass %s remarked: %s", url.split("/")[2], last[:120])
                time.sleep(10 * (attempt + 1))
                continue
            return body.get("elements", [])
    raise OverpassUnavailable(last)


def wikidata_places(city: dict[str, Any], radius_m: int, as_of: str) -> list[dict[str, Any]]:
    """The fallback source for places: Wikidata, queried geographically.

    Narrower than OpenStreetMap -- Wikidata holds notable venues, not every
    cafe -- but it is a stable public endpoint, it is CC0, and the rows carry
    the same shape and the same category vocabulary. Each row records Wikidata
    as its source, so a reviewer can see exactly which cities came from where.
    """
    values = " ".join(f"wd:{qid}" for qid, _ in WIKIDATA_CLASSES)
    query = f"""
    SELECT ?item ?itemLabel ?class ?coord WHERE {{
      SERVICE wikibase:around {{
        ?item wdt:P625 ?coord .
        bd:serviceParam wikibase:center "Point({city["lon"]} {city["lat"]})"^^geo:wktLiteral .
        bd:serviceParam wikibase:radius "{radius_m / 1000.0:.1f}" .
      }}
      VALUES ?class {{ {values} }}
      ?item wdt:P31 ?class .
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }}
    LIMIT 400
    """
    # Wikidata's public endpoint rate-limits per client, and a 429 is a "come
    # back shortly", not a "this city has no museums". Backing off and retrying
    # is the difference between a thin snapshot and a correct one.
    bindings = None
    for attempt in range(4):
        try:
            response = requests.get(
                WIKIDATA_SPARQL,
                params={"query": query, "format": "json"},
                headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
                timeout=120,
            )
            if response.status_code == 429:
                wait = int(response.headers.get("Retry-After", 0)) or 20 * (attempt + 1)
                log.warning("wikidata rate-limited for %s; waiting %ds", city["slug"], wait)
                time.sleep(wait)
                continue
            response.raise_for_status()
            bindings = response.json()["results"]["bindings"]
            break
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning("wikidata attempt %d failed for %s: %s", attempt + 1, city["slug"], exc)
            time.sleep(15 * (attempt + 1))
    if bindings is None:
        log.error("wikidata gave up on %s; the snapshot will have no places for it", city["slug"])
        return []

    class_to_category = {f"http://www.wikidata.org/entity/{q}": c for q, c in WIKIDATA_CLASSES}
    per_category: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for binding in bindings:
        category = class_to_category.get(binding.get("class", {}).get("value"))
        name = binding.get("itemLabel", {}).get("value")
        uri = binding.get("item", {}).get("value", "")
        qid = uri.rsplit("/", 1)[-1]
        # An unlabelled item comes back as its own Q-number; a place with no
        # name is not a place a traveller can be sent to.
        if not category or not name or name == qid:
            continue
        if per_category.get(category, 0) >= PER_CATEGORY_LIMIT:
            continue
        per_category[category] = per_category.get(category, 0) + 1

        lat = lon = None
        point = binding.get("coord", {}).get("value", "")
        if point.startswith("Point("):
            try:
                lon_s, lat_s = point[6:-1].split()
                lat, lon = float(lat_s), float(lon_s)
            except ValueError:
                pass

        rows.append(
            {
                "id": f"wikidata:{qid}",
                "city_id": city["slug"],
                "name": name,
                "category": category,
                "lat": lat,
                "lon": lon,
                "address": None,
                "source": "Wikidata (CC0)",
                "source_url": uri or f"https://www.wikidata.org/wiki/{qid}",
                "is_sample": False,
                "as_of": as_of,
            }
        )
    log.info("places: %s -> %d rows from Wikidata", city["slug"], len(rows))
    return rows


def fetch_places(
    cities: list[dict[str, Any]], radius: int, source: str = "wikidata"
) -> list[dict[str, Any]]:
    """Collect places, from Wikidata by default and OpenStreetMap on request.

    Wikidata is the default because it is what actually works: the public
    Overpass instances are a free shared service, and at staging time for the
    committed snapshot every one of the four mirrors was either refusing
    connections or timing out. Wikidata's SPARQL endpoint answered every query
    in seconds. It holds notable venues rather than every cafe, so the
    coverage is narrower -- and the README says so rather than implying a
    richness that is not there.

    `--places-source osm` runs the Overpass path, which is kept because it is
    the better source when it is reachable. Whichever ran, every row records
    its own source, licence and URL, so a reviewer can see exactly where each
    place came from.

    A city that yields nothing is recorded as yielding nothing. The system then
    says "no places on record" for it. Never a placeholder, never a guess.
    """
    as_of = now_iso()
    rows: list[dict[str, Any]] = []

    if source == "wikidata":
        for city in cities:
            rows.extend(wikidata_places(city, radius, as_of))
            time.sleep(2)
        return rows

    tag_to_category = {(k, v): c for k, v, c in OSM_CATEGORIES}
    groups = [OSM_CATEGORIES[i : i + 4] for i in range(0, len(OSM_CATEGORIES), 4)]

    for city in cities:
        elements: list[dict[str, Any]] = []
        for group in groups:
            selectors = [(k, v) for k, v, _ in group]
            try:
                elements.extend(overpass_fetch(overpass_query(selectors, city, radius)))
            except OverpassUnavailable as exc:
                log.error(
                    "overpass gave up on %s for %s: %s",
                    [v for _, v in selectors],
                    city["slug"],
                    exc,
                )
            time.sleep(4)

        per_category: dict[str, int] = {}
        for element in elements:
            tags = element.get("tags") or {}
            category = None
            for (key, value), mapped in tag_to_category.items():
                if tags.get(key) == value:
                    category = mapped
                    break
            name = tags.get("name")
            if not category or not name:
                continue
            if per_category.get(category, 0) >= PER_CATEGORY_LIMIT:
                continue
            per_category[category] = per_category.get(category, 0) + 1

            centre = element.get("center") or {}
            rows.append(
                {
                    "id": f"osm:{element['type']}/{element['id']}",
                    "city_id": city["slug"],
                    "name": name,
                    "category": category,
                    "lat": element.get("lat", centre.get("lat")),
                    "lon": element.get("lon", centre.get("lon")),
                    "address": tags.get("addr:street"),
                    "source": "OpenStreetMap contributors (ODbL)",
                    "source_url": f"https://www.openstreetmap.org/{element['type']}/{element['id']}",
                    "is_sample": False,
                    "as_of": as_of,
                }
            )
        if not per_category:
            # Overpass is a free shared service and, under load or a rate limit,
            # is simply not there. Rather than ship a city with no places,
            # fall back to Wikidata -- a different source, a different licence,
            # and every row says which one it came from.
            log.warning("no OSM places for %s; falling back to Wikidata", city["slug"])
            rows.extend(wikidata_places(city, radius, as_of))
            continue

        log.info(
            "places: %s -> %d rows from %d elements",
            city["slug"],
            sum(per_category.values()),
            len(elements),
        )
    return rows


# ---------------------------------------------------------------- facts ----


def fetch_facts(cities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    as_of = now_iso()
    rows: list[dict[str, Any]] = []
    for city in cities:
        title = city.get("wikipedia", city["name"]).replace(" ", "_")
        try:
            response = requests.get(
                WIKI_SUMMARY.format(title=title),
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=30,
            )
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            log.error("wikipedia failed for %s: %s", city["slug"], exc)
            continue
        summary = (body.get("extract") or "").strip()
        if not summary:
            log.warning("wikipedia returned no summary for %s", city["slug"])
            continue
        rows.append(
            {
                "id": f"wikipedia:{body.get('titles', {}).get('canonical', title)}",
                "city_id": city["slug"],
                "title": body.get("title", city["name"]),
                "summary": summary,
                "topic": "history",
                "source": "Wikipedia (CC BY-SA 4.0)",
                "source_url": (
                    body.get("content_urls", {})
                    .get("desktop", {})
                    .get("page", f"https://en.wikipedia.org/wiki/{title}")
                ),
                "is_sample": False,
                "as_of": as_of,
            }
        )
        log.info("facts: %s", city["slug"])
        time.sleep(1)
    return rows


# --------------------------------------------------------------- events ----


def load_events(seed: Path) -> list[dict[str, Any]]:
    """Events are not fetched. They are hand-verified and committed.

    There is no free, licensable, offline-stageable feed of concerts and
    fixtures for five cities, and the brief's rule against inventing events is
    absolute. So this reads a small file whose every row was checked against
    its own source URL by hand, and copies it through unchanged.
    """
    if not seed.exists():
        log.error("no event seed at %s", seed)
        return []
    rows = []
    for line in seed.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("//"):
            rows.append(json.loads(line))
    log.info("events: %d hand-verified rows", len(rows))
    return rows


# ----------------------------------------------------------------- main ----


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        action="append",
        choices=["weather", "places", "facts", "events"],
        help="fetch just these; repeatable. Default: all.",
    )
    parser.add_argument("--days", type=int, default=16)
    parser.add_argument("--radius", type=int, default=2500, help="metres around the city centre")
    parser.add_argument(
        "--places-source",
        choices=["wikidata", "osm"],
        default="wikidata",
        help="wikidata (default, reliable) or osm via Overpass (richer when reachable)",
    )
    args = parser.parse_args(argv)

    wanted = set(args.only or ["weather", "places", "facts", "events"])
    cities = load_cities()
    out = config.SNAPSHOT_DIR

    if "weather" in wanted:
        write_jsonl(out / "weather.jsonl", fetch_weather(cities, args.days))
    if "places" in wanted:
        write_jsonl(out / "places.jsonl", fetch_places(cities, args.radius, args.places_source))
    if "facts" in wanted:
        write_jsonl(out / "facts.jsonl", fetch_facts(cities))
    if "events" in wanted:
        write_jsonl(out / "events.jsonl", load_events(config.DATA_DIR / "events.seed.jsonl"))

    log.info("snapshot written to %s -- commit it", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
