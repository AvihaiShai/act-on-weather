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
]

PER_CATEGORY_LIMIT = 10


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
    absolute. So this reads a file and copies its rows through unchanged: real
    listings, each row checked against its own source URL by hand,
    `is_sample: false`. There are seven, and they are all in London.

    These are the only events a default run stores.
    """
    real = read_jsonl(seed)
    if not real:
        log.error("no event seed at %s", seed)
    for row in real:
        if row.get("is_sample"):
            log.error("%s is in the verified seed but marked is_sample", row["id"])
    log.info("events: %d hand-verified rows", len(real))
    return real


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
    """
    sampled = []
    for row in read_jsonl(samples):
        if not row.get("is_sample"):
            log.error("dropping %s: it is in the sample file but not marked is_sample", row["id"])
            continue
        sampled.append(row)
    log.info("events: %d labelled sample rows", len(sampled))
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
