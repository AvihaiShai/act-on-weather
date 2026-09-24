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
  * facts    Wikipedia REST summaries, CC BY-SA 4.0 -- the city article plus
             one article per venue in the places snapshot, resolved through
             its Wikidata sitelink
  * events   data/events.seed.jsonl -- hand-verified real listings, each row
             carrying its own source URL, plus data/events.samples.jsonl,
             whose rows are all is_sample=true and titled "Sample: ...".
             Nothing is fetched, and nothing is passed off as real.

Every row it writes carries `source`, `source_url` and `as_of`.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
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
WIKI_GEOSEARCH = "https://en.wikipedia.org/w/api.php"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

# Wikidata classes worth collecting, mapped onto the same category vocabulary
# the OSM tags use, so the two sources produce interchangeable rows.
# ORDER IS SIGNIFICANT. An item is an instance of several of these at once --
# the Uffizi is an art museum and a museum -- and it is stored once, under the
# first class here that claims it. So the list runs most specific to most
# general, and the specific category wins.
#
# This stopped being cosmetic when subclass traversal arrived. Q207694 (art
# museum) is a subclass of Q33506 (museum), verified against the endpoint, so
# a one-hop museum query matches every art gallery in the city. With `museum`
# listed first, `gallery` would have been deduplicated down to almost nothing
# while the snapshot looked fuller than before. `attraction` is last for the
# same reason in the extreme: it is general enough to swallow anything, so it
# only ever collects what no sharper category claimed.
WIKIDATA_CLASSES: list[tuple[str, str]] = [
    ("Q207694", "gallery"),  # art museum -- a subclass of museum, so listed first
    ("Q33506", "museum"),
    ("Q1060829", "concert_hall"),
    ("Q24354", "theatre"),  # theatre building
    ("Q11315", "shopping"),  # shopping centre
    ("Q330284", "market"),  # marketplace
    ("Q22698", "park"),
    ("Q483110", "stadium"),
    ("Q11707", "restaurant"),
    ("Q4989906", "monument"),
    # A coastal city's defining places were missing entirely: the vocabulary
    # had no beach, so Tel Aviv scored "a day at the beach" at 100 and could
    # not name a single beach to spend it on. These two classes are what the
    # source actually asserts -- a beach exists here, a marina exists here --
    # and nothing more. See `place_categories` in data/activities.yml for why
    # that stops short of surfing, swimming, fishing and boat hire.
    ("Q40080", "beach"),
    ("Q721207", "marina"),
    ("Q570116", "attraction"),  # tourist attraction -- the catch-all, so it is last
]
USER_AGENT = "act-on-weather/1.0 (take-home project; https://github.com/; contact via repository)"

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
    # The OSM half of the beach/marina vocabulary above. `natural=beach` is
    # almost always mapped as an area rather than a node, which the query
    # already handles: it asks for `way` as well as `node` and closes with
    # `out center`, so an area arrives with a usable centre point.
    ("natural", "beach", "beach"),
    ("leisure", "beach_resort", "beach"),
    ("leisure", "marina", "marina"),
]

# How many venues of one category a city keeps. Raised from 10 once the
# retrieval below stopped throwing away what the source already held: Rome
# matched 220 rows and stored 57, London matched 623 and stored 68. This is a
# per-city-per-category ceiling on snapshot size, not a coverage decision --
# most categories never reach it.
PER_CATEGORY_LIMIT = 25

# Safety valve per class query. Real per-class counts within 4km are in the
# low hundreds, so this should never bind; if it does, the run says so
# (`truncated_classes`) because ranking over a partial set is not the same
# thing as ranking.
WIKIDATA_CLASS_QUERY_LIMIT = 1500

# The public endpoint is a shared free service. These two knobs are the
# difference between a complete snapshot and a half-failed one.
SPARQL_ATTEMPTS = 5
WIKIDATA_CLASS_PAUSE_S = 1.5

# Classes that may take ONE subclass hop (`wdt:P31/wdt:P279?`).
#
# An allowlist rather than a blanket `wdt:P279*`, because the transitive
# closure is not safe to trust. Measured against the live endpoint within 4km
# of Rome, Q33506 (museum) returns 72 items directly, 157 at one hop, and 2077
# at full depth -- by which point it is collecting whatever the ontology
# happens to route through "museum" rather than collecting museums.
#
# One hop is bounded and checkable, and it is where the real venues are: a
# city's museums are mostly instances of *art museum* or *archaeology museum*,
# never of "museum" itself. Measured d0 -> d1, Rome and London:
#
#   museum 72->157, 88->165     monument  39->87, 84->301
#   gallery 42->64, 38->40      park       7->28, 40->50
#   shopping 0->0,  4->13       market     0->2,  3->9
#   restaurant 6->6, 152->175   stadium    2->9,  1->1
#   theatre 35->37, 188->192    attraction 0->3,  1->4
#
# Every class currently collected qualified on that measurement, so the set is
# presently the whole vocabulary. It stays a set rather than a flag so that a
# class which later misbehaves can be dropped from it without touching the
# retrieval code, and so the ceiling on traversal depth stays visible.
#
# What keeps one hop honest is the pair below it: selection is ranked by
# sitelink count, and PER_CATEGORY_LIMIT caps each category. London's 301
# one-hop "monuments" are mostly statues and memorials; the snapshot keeps the
# 25 best known of them, not all 301.
WIKIDATA_SUBCLASS_CLASSES: set[str] = {
    "Q207694",  # gallery
    "Q33506",  # museum
    "Q1060829",  # concert_hall
    "Q24354",  # theatre
    "Q11315",  # shopping
    "Q330284",  # market
    "Q22698",  # park
    "Q483110",  # stadium
    "Q11707",  # restaurant
    "Q4989906",  # monument
    "Q40080",  # beach
    "Q721207",  # marina
    "Q570116",  # attraction
}


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


class WikidataUnavailable(Exception):
    """The endpoint never answered, as distinct from answering "nothing here"."""


def sparql(query: str, what: str, attempts: int = SPARQL_ATTEMPTS) -> list[dict[str, Any]]:
    """Run one SPARQL query and return its bindings, or raise.

    This exists to keep two very different outcomes apart. An empty result set
    means the city really has no marinas. An HTTP error means we do not know
    whether it has marinas. The previous code collapsed both into an empty
    list, and that is precisely how London reached a committed snapshot with
    zero monuments while Wikidata held 84 of them: a truncated, half-failed
    query looked exactly like an honest "none".

    So anything that is not a 200 carrying parseable JSON raises once the
    retries are spent, and the caller records that class as *failed* rather
    than as *empty*. The snapshot can then say what it does not know.
    """
    last = "no attempt made"
    for attempt in range(attempts):
        try:
            response = requests.get(
                WIKIDATA_SPARQL,
                params={"query": query, "format": "json"},
                headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
                timeout=90,
            )
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
            log.warning("wikidata %s: %s (attempt %d)", what, last, attempt + 1)
            time.sleep(min(8 * (attempt + 1), 45))
            continue
        # 429 is "come back shortly"; 5xx is the public endpoint shedding load.
        # Both were seen repeatedly while this was written, and both are worth
        # waiting out rather than recording as an empty city.
        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", 0) or 0) or 20 * (attempt + 1)
            log.info("wikidata %s: rate-limited, waiting %ds", what, wait)
            time.sleep(wait)
            last = "HTTP 429"
            continue
        if response.status_code >= 500:
            last = f"HTTP {response.status_code}"
            log.warning("wikidata %s: %s (attempt %d)", what, last, attempt + 1)
            time.sleep(min(8 * (attempt + 1), 45))
            continue
        try:
            response.raise_for_status()
            return response.json()["results"]["bindings"]
        except (requests.RequestException, ValueError, KeyError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            log.warning("wikidata %s: %s (attempt %d)", what, last, attempt + 1)
            time.sleep(min(8 * (attempt + 1), 45))
    raise WikidataUnavailable(f"{what}: {last}")


@dataclass
class PlaceStats:
    """What a places run actually did, including everything it threw away.

    A snapshot that reports only what it kept cannot be audited: "282 places"
    says nothing about whether 282 was the ceiling, the cap, or everything the
    source held. These counters are logged per city so the README's numbers
    can be re-derived rather than trusted.
    """

    matched: int = 0
    unnamed: int = 0
    no_coord: int = 0
    duplicate: int = 0
    over_cap: int = 0
    kept: int = 0
    failed_classes: list[str] = field(default_factory=list)
    truncated_classes: list[str] = field(default_factory=list)

    def merge(self, other: PlaceStats) -> None:
        self.matched += other.matched
        self.unnamed += other.unnamed
        self.no_coord += other.no_coord
        self.duplicate += other.duplicate
        self.over_cap += other.over_cap
        self.kept += other.kept
        self.failed_classes.extend(other.failed_classes)
        self.truncated_classes.extend(other.truncated_classes)

    def summary(self) -> str:
        parts = [
            f"kept={self.kept}",
            f"matched={self.matched}",
            f"unnamed={self.unnamed}",
            f"no_coord={self.no_coord}",
            f"duplicate={self.duplicate}",
            f"over_cap={self.over_cap}",
        ]
        if self.failed_classes:
            parts.append("FAILED=" + ",".join(sorted(set(self.failed_classes))))
        if self.truncated_classes:
            parts.append("TRUNCATED=" + ",".join(sorted(set(self.truncated_classes))))
        return " ".join(parts)


def coverage_table(rows: list[dict[str, Any]]) -> str:
    """City x category counts, rendered from the rows themselves.

    The README quotes place counts, and a hand-maintained number goes stale
    the first time anyone re-runs staging -- a review already caught the README
    claiming 282 places against a live 272. This renders the table from the
    snapshot that was actually written, so the documented figure can be
    re-derived instead of retyped.
    """
    cities = sorted({row["city_id"] for row in rows})
    categories = sorted({row["category"] for row in rows})
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (row["city_id"], row["category"])
        counts[key] = counts.get(key, 0) + 1

    width = max([len(c) for c in categories] + [8])
    header = "category".ljust(width) + "".join(c[:10].rjust(11) for c in cities) + "total".rjust(8)
    lines = [header, "-" * len(header)]
    for category in categories:
        cells = [counts.get((city, category), 0) for city in cities]
        lines.append(
            category.ljust(width)
            + "".join(str(n).rjust(11) for n in cells)
            + str(sum(cells)).rjust(8)
        )
    totals = [sum(counts.get((city, c), 0) for c in categories) for city in cities]
    lines.append("-" * len(header))
    lines.append(
        "TOTAL".ljust(width) + "".join(str(n).rjust(11) for n in totals) + str(sum(totals)).rjust(8)
    )
    return "\n".join(lines)


def wikidata_class_query(qid: str, city: dict[str, Any], radius_m: int) -> str:
    """One class, one query -- which is the whole point.

    The old code asked for every class at once under a single `LIMIT 400`.
    London matches 623, so the endpoint returned an arbitrary 400 of them and
    whole categories fell off the end; that is the London monuments bug. Per
    class, no category can crowd out another, and the limit below is a safety
    valve rather than the thing that decides coverage.

    `?sitelinks` is how many Wikipedias hold an article on the item. It is not
    a quality score, but it is a stable, source-provided proxy for how well
    known a venue is, and it is what turns "the first ten the endpoint
    happened to return" into "the ten a visitor is most likely to have heard
    of" -- deterministically, because ties break on the Q-number.
    """
    path = "wdt:P31/wdt:P279?" if qid in WIKIDATA_SUBCLASS_CLASSES else "wdt:P31"
    # DISTINCT is load-bearing, not tidiness. An item reachable by several
    # P31/P279 paths -- which is most of them once traversal is on -- comes
    # back once per path. Without it, Rome's galleries returned 1500 rows for
    # 64 distinct venues, hit the per-class limit, and were then ranked over
    # whichever arbitrary slice the endpoint had truncated to. Deduplicating
    # in Python cannot fix that: the truncation has already happened server
    # side, which is the same class of bug as the old global LIMIT 400.
    return f"""
    SELECT DISTINCT ?item ?itemLabel ?coord ?sitelinks WHERE {{
      SERVICE wikibase:around {{
        ?item wdt:P625 ?coord .
        bd:serviceParam wikibase:center "Point({city["lon"]} {city["lat"]})"^^geo:wktLiteral .
        bd:serviceParam wikibase:radius "{radius_m / 1000.0:.1f}" .
      }}
      ?item {path} wd:{qid} .
      OPTIONAL {{ ?item wikibase:sitelinks ?sitelinks . }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }}
    LIMIT {WIKIDATA_CLASS_QUERY_LIMIT}
    """


def _parse_point(value: str) -> tuple[float, float] | None:
    if not value.startswith("Point("):
        return None
    try:
        lon_s, lat_s = value[6:-1].split()
        return float(lat_s), float(lon_s)
    except ValueError:
        return None


def _qid_sort_key(qid: str) -> int:
    try:
        return int(qid[1:])
    except ValueError:
        return 1 << 62


def wikidata_places(
    city: dict[str, Any], radius_m: int, as_of: str, stats: PlaceStats | None = None
) -> list[dict[str, Any]]:
    """Places from Wikidata: one bounded query per class, ranked, deduplicated.

    Wikidata holds *notable* venues rather than every cafe. That is a real
    coverage ceiling and the README says so. What it is not is the reason the
    old snapshot was thin: Rome matched 220 rows and stored 57, London matched
    623 and stored 68. The shortfall was ours, in two places -- a global
    `LIMIT 400` shared across every class, and a first-N-arrive selection --
    and both are fixed here.

    Selection is deterministic: within a class, candidates sort by sitelink
    count descending, then by Q-number ascending. The same data yields the
    same snapshot, and the venues kept are the ones a visitor has heard of
    rather than whichever the endpoint happened to list first.

    An item that instantiates two collected classes is stored once, under
    whichever class comes first in WIKIDATA_CLASSES. That order is therefore a
    priority order, and deduplication happens before the per-category cap so a
    duplicate cannot silently consume a slot a real venue needed.
    """
    stats = stats if stats is not None else PlaceStats()
    taken: dict[str, str] = {}
    rows: list[dict[str, Any]] = []

    for qid, category in WIKIDATA_CLASSES:
        what = f"{city['slug']}/{category}"
        try:
            bindings = sparql(wikidata_class_query(qid, city, radius_m), what)
        except WikidataUnavailable as exc:
            # Recorded, never silently swallowed: a failed class is a hole in
            # the snapshot, and the run has to be able to name which one.
            log.error("wikidata: %s failed -- %s", what, exc)
            stats.failed_classes.append(what)
            continue

        if len(bindings) >= WIKIDATA_CLASS_QUERY_LIMIT:
            log.warning("wikidata: %s hit the per-class limit; ranking is over a partial set", what)
            stats.truncated_classes.append(what)
        stats.matched += len(bindings)

        candidates: list[tuple[int, int, str, str, float, float]] = []
        for binding in bindings:
            uri = binding.get("item", {}).get("value", "")
            item_qid = uri.rsplit("/", 1)[-1]
            name = binding.get("itemLabel", {}).get("value")
            # An unlabelled item comes back as its own Q-number, and a place
            # with no name is not a place a traveller can be sent to.
            if not name or name == item_qid:
                stats.unnamed += 1
                continue
            point = _parse_point(binding.get("coord", {}).get("value", ""))
            if point is None:
                # `wikibase:around` selects on P625, so this should not happen.
                # It is counted rather than assumed away.
                stats.no_coord += 1
                continue
            try:
                sitelinks = int(binding.get("sitelinks", {}).get("value", 0))
            except (TypeError, ValueError):
                sitelinks = 0
            candidates.append((-sitelinks, _qid_sort_key(item_qid), item_qid, name, *point))

        candidates.sort()
        kept_here = 0
        for _rank, _qsort, item_qid, name, lat, lon in candidates:
            if item_qid in taken:
                stats.duplicate += 1
                continue
            if kept_here >= PER_CATEGORY_LIMIT:
                stats.over_cap += 1
                continue
            taken[item_qid] = category
            kept_here += 1
            rows.append(
                {
                    "id": f"wikidata:{item_qid}",
                    "city_id": city["slug"],
                    "name": name,
                    "category": category,
                    "lat": lat,
                    "lon": lon,
                    "address": None,
                    "source": "Wikidata (CC0)",
                    "source_url": f"https://www.wikidata.org/wiki/{item_qid}",
                    "is_sample": False,
                    "as_of": as_of,
                }
            )
        log.info("places: %s -> %d kept of %d matched", what, kept_here, len(bindings))
        time.sleep(WIKIDATA_CLASS_PAUSE_S)

    stats.kept += len(rows)
    log.info("places: %s -> %d rows (%s)", city["slug"], len(rows), stats.summary())
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
        totals = PlaceStats()
        for city in cities:
            stats = PlaceStats()
            rows.extend(wikidata_places(city, radius, as_of, stats))
            totals.merge(stats)
            time.sleep(2)
        log.info("places: all cities -> %s", totals.summary())
        if totals.failed_classes:
            # Loud on purpose. The snapshot is about to be committed, and a
            # class that failed is a hole that looks exactly like a city with
            # no museums. The operator gets to decide whether to re-run.
            log.error(
                "places: %d class queries never answered -- the snapshot is INCOMPLETE for %s",
                len(totals.failed_classes),
                ", ".join(sorted(set(totals.failed_classes))),
            )
        log.info("places: coverage\n%s", coverage_table(rows))
        return rows

    tag_to_category = {(k, v): c for k, v, c in OSM_CATEGORIES}
    groups = [OSM_CATEGORIES[i : i + 4] for i in range(0, len(OSM_CATEGORIES), 4)]
    seen_ids: set[str] = set()

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
            place_id = f"osm:{element['type']}/{element['id']}"
            if place_id in seen_ids:
                continue
            if per_category.get(category, 0) >= PER_CATEGORY_LIMIT:
                continue
            seen_ids.add(place_id)
            per_category[category] = per_category.get(category, 0) + 1

            centre = element.get("center") or {}
            rows.append(
                {
                    "id": place_id,
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


def wiki_get(url: str, params: dict[str, Any] | None = None, attempts: int = 5):
    """GET from Wikipedia, honouring its rate limit.

    Wikipedia answers a burst with HTTP 429 and a Retry-After. Treating that as
    a failure is how the first run of this produced one landmark for Rome and
    none for anywhere else: a 429 means "slow down", not "this city has no
    landmarks". Backs off and retries, and returns None only once it really
    cannot get an answer.
    """
    wait = 5.0
    for attempt in range(attempts):
        try:
            response = requests.get(
                url,
                params=params,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=45,
            )
        except requests.RequestException as exc:
            log.warning("wikipedia request failed (%s); retrying", exc)
            time.sleep(wait)
            wait = min(wait * 2, 60)
            continue
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", 0)) or wait
            log.info("wikipedia rate-limited; waiting %.0fs (attempt %d)", retry_after, attempt + 1)
            time.sleep(retry_after)
            wait = min(wait * 2, 60)
            continue
        if response.status_code == 404:
            return None
        try:
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("wikipedia returned %s: %s", response.status_code, exc)
            return None
    log.error("wikipedia gave up after %d attempts: %s", attempts, url)
    return None


def wikipedia_summary(title: str, city_slug: str, topic: str, as_of: str) -> dict[str, Any] | None:
    """One Wikipedia article -> one fact row, or None if it has no summary.

    A redirect or a disambiguation page comes back with a `type` that is not
    `standard`; those are skipped rather than stored, because "Rome (disambig)"
    is not a fact about anywhere.
    """
    body = wiki_get(WIKI_SUMMARY.format(title=title.replace(" ", "_")))
    if body is None:
        return None

    if body.get("type") not in (None, "standard"):
        return None
    summary = (body.get("extract") or "").strip()
    # Under ~120 characters it is a stub, and a stub in the agent's context
    # window costs a reviewer's attention without telling them anything.
    if len(summary) < 120:
        return None

    canonical = body.get("titles", {}).get("canonical", title)
    return {
        "id": f"wikipedia:{canonical}",
        "city_id": city_slug,
        "title": body.get("title", title),
        "summary": summary,
        "topic": topic,
        "source": "Wikipedia (CC BY-SA 4.0)",
        "source_url": (
            body.get("content_urls", {})
            .get("desktop", {})
            .get("page", f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}")
        ),
        "is_sample": False,
        "as_of": as_of,
    }


def wikipedia_titles_for(qids: list[str]) -> dict[str, str]:
    """Wikidata QID -> English Wikipedia article title, via the sitelink.

    Wikipedia's own geosearch was tried first and rejected. Geographically it
    is correct and editorially it is not: within 6km of a city centre it
    returns administrative divisions ("Province of Rome"), list articles, and
    -- for Tel Aviv -- a run of articles about shootings and bombings. All of
    that is true, none of it is background for a trip planner, and a keyword
    blocklist over article titles is a guess dressed up as a filter.

    Going through the places snapshot instead means every landmark fact is
    about a venue the system already holds, selected by its Wikidata P31 class
    (museum, theatre, monument, park...), not by its distance from a point.
    """
    titles: dict[str, str] = {}
    # SPARQL VALUES clauses get slow and occasionally 500 past a few hundred
    # entries, so this goes in batches.
    for start in range(0, len(qids), 80):
        batch = qids[start : start + 80]
        values = " ".join(f"wd:{q}" for q in batch)
        query = f"""
        SELECT ?item ?article WHERE {{
          VALUES ?item {{ {values} }}
          ?article schema:about ?item ;
                   schema:isPartOf <https://en.wikipedia.org/> .
        }}
        """
        # Retried, because a single 429 or timeout here silently costs a whole
        # city its landmark facts -- which is exactly what happened to Lisbon
        # the first time this ran.
        bindings = None
        for attempt in range(4):
            try:
                response = requests.get(
                    WIKIDATA_SPARQL,
                    params={"query": query, "format": "json"},
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "application/sparql-results+json",
                    },
                    timeout=120,
                )
                if response.status_code == 429:
                    wait = int(response.headers.get("Retry-After", 0)) or 20 * (attempt + 1)
                    log.info("wikidata rate-limited on sitelinks; waiting %ds", wait)
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                bindings = response.json()["results"]["bindings"]
                break
            except (requests.RequestException, ValueError, KeyError) as exc:
                log.warning(
                    "sitelink lookup attempt %d failed for a batch of %d: %s",
                    attempt + 1,
                    len(batch),
                    exc,
                )
                time.sleep(15 * (attempt + 1))
        if bindings is None:
            log.error("sitelink lookup gave up on a batch of %d QIDs", len(batch))
            continue
        for binding in bindings:
            qid = binding["item"]["value"].rsplit("/", 1)[-1]
            url = binding["article"]["value"]
            titles[qid] = requests.utils.unquote(url.rsplit("/", 1)[-1]).replace("_", " ")
        time.sleep(2)
    return titles


def fetch_facts(
    cities: list[dict[str, Any]],
    places: list[dict[str, Any]],
    per_city: int = 14,
) -> list[dict[str, Any]]:
    """The city article, plus background on the places the system recommends.

    One summary per city was technically sourced and practically useless: the
    agent could say what Rome is and nothing about anything in it. This keeps
    the city article as `topic='history'` and adds up to `per_city` landmark
    articles as `topic='landmark'` -- one per venue already in
    data/snapshot/places.jsonl, so the itinerary and the background describe
    the same city.
    """
    as_of = now_iso()
    rows: list[dict[str, Any]] = []

    by_city: dict[str, list[dict[str, Any]]] = {}
    for place in places:
        if place["id"].startswith("wikidata:"):
            by_city.setdefault(place["city_id"], []).append(place)

    for city in cities:
        seen: set[str] = set()

        city_title = city.get("wikipedia", city["name"])
        row = wikipedia_summary(city_title, city["slug"], "history", as_of)
        if row:
            rows.append(row)
            seen.add(row["id"])
        else:
            log.error("no city summary for %s", city["slug"])

        candidates = by_city.get(city["slug"], [])
        titles = wikipedia_titles_for([p["id"].split(":", 1)[1] for p in candidates])

        kept = 0
        for place in candidates:
            if kept >= per_city:
                break
            title = titles.get(place["id"].split(":", 1)[1])
            if not title or title == city_title:
                continue
            landmark = wikipedia_summary(title, city["slug"], "landmark", as_of)
            time.sleep(1.2)
            if landmark is None or landmark["id"] in seen:
                continue
            seen.add(landmark["id"])
            rows.append(landmark)
            kept += 1
        log.info(
            "facts: %s -> 1 city article + %d landmarks (of %d places)",
            city["slug"],
            kept,
            len(candidates),
        )
    return rows


# --------------------------------------------------------------- events ----


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("//")
    ]


def load_events(seed: Path) -> list[dict[str, Any]]:
    """The verified events. Not fetched -- hand-checked and committed.

    There is no free, licensable, offline-stageable feed of concerts and
    fixtures for five cities, and the brief's rule against inventing events is
    absolute. So this reads a file of real listings, each row read off its own
    source URL by hand, `is_sample: false`. There are 39, across all five
    cities.

    The one thing it adds to a row is `valid_until`, derived from the row's own
    `checked_at` by `config.event_valid_until`. It is derived here rather than
    written into the file because the expiry is a property of the policy, not
    of the listing: change AOW_EVENT_RECHECK_DAYS and every row's expiry moves
    together, and no committed row can quietly disagree with the configured
    window.

    These are the only events a default run stores.
    """
    real = read_jsonl(seed)
    if not real:
        log.error("no event seed at %s", seed)
    rows: list[dict[str, Any]] = []
    for row in real:
        if row.get("is_sample"):
            log.error("%s is in the verified seed but marked is_sample", row["id"])
        checked = row.get("checked_at")
        if not checked:
            # Not a warning. A row nobody has undertaken to check is exactly the
            # kind of claim this feed exists to keep out, so it is dropped here
            # rather than shipped with an invented expiry.
            log.error("dropping %s: no checked_at, so it is asserted and not verified", row["id"])
            continue
        valid_until = config.event_valid_until(datetime.fromisoformat(checked))
        rows.append({**row, "valid_until": valid_until.isoformat()})
    log.info(
        "events: %d hand-verified rows, each valid for %d days after it was checked",
        len(rows),
        config.EVENT_RECHECK_DAYS,
    )
    return rows


def load_sample_events(samples: Path) -> list[dict[str, Any]]:
    """The generated samples, written to their own snapshot file.

    They are the output of `services.ingestor.make_samples` and exist so the
    planner and the agent can be exercised in all five cities rather than only
    in London. Every row is `is_sample: true` and titled "Sample: ...", and
    they are kept in a separate snapshot file so that replaying them is a
    decision the ingestor makes (demo mode) rather than a property of having a
    snapshot at all.

    A row in the sample file that does not admit to being a sample is dropped
    here rather than trusted: the labelling is a property of the data, so it is
    checked at the boundary and not merely assumed.

    The expiry is re-derived here for the same reason it is on a verified row:
    it belongs to the freshness policy, not to the file. A generated row has no
    listing page, so its `checked_at` is the moment it was generated -- which
    is exactly how long it deserves to be trusted for, since it describes a
    window of dates that is itself fixed. Without this the committed
    `valid_until` would be frozen at whenever `make samples` last ran, and
    changing AOW_EVENT_RECHECK_DAYS would move the verified feed while leaving
    demo mode on the old window.
    """
    sampled = []
    for row in read_jsonl(samples):
        if not row.get("is_sample"):
            log.error("dropping %s: it is in the sample file but not marked is_sample", row["id"])
            continue
        checked = row.get("checked_at")
        if not checked:
            log.error("dropping sample %s: no checked_at to derive an expiry from", row["id"])
            continue
        valid_until = config.event_valid_until(datetime.fromisoformat(checked))
        sampled.append({**row, "valid_until": valid_until.isoformat()})
    log.info(
        "events: %d labelled sample rows, each valid for %d days after it was generated",
        len(sampled),
        config.EVENT_RECHECK_DAYS,
    )
    return sampled


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
    parser.add_argument("--radius", type=int, default=4000, help="metres around the city centre")
    parser.add_argument(
        "--facts-per-city",
        type=int,
        default=14,
        help="how many nearby Wikipedia landmark articles to store per city",
    )
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
        # Landmark facts are looked up from the places snapshot, so read
        # back whatever is on disk when this run did not fetch places itself.
        places_path = out / "places.jsonl"
        places_rows = (
            [
                json.loads(line)
                for line in places_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if places_path.exists()
            else []
        )
        write_jsonl(out / "facts.jsonl", fetch_facts(cities, places_rows, args.facts_per_city))
    if "events" in wanted:
        # Two snapshot files, never one. events.jsonl is what a default run
        # replays; the samples sit beside it and are replayed only in demo
        # mode, so "verified" and "generated" cannot be merged by accident.
        write_jsonl(out / "events.jsonl", load_events(config.DATA_DIR / "events.seed.jsonl"))
        write_jsonl(
            out / "events.samples.jsonl",
            load_sample_events(config.DATA_DIR / "events.samples.jsonl"),
        )

    log.info("snapshot written to %s -- commit it", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
