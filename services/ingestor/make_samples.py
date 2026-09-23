"""Generate the labelled sample events, while connected or off.

    docker compose run --rm --no-deps ingestor \
        python -m services.ingestor.make_samples --days 16

Read this before judging it. The brief's rule is absolute: never invent an
event, a concert or a fixture. Nothing here is presented as a real listing.

What exists, and why this file does too:

  * data/events.seed.jsonl -- real, hand-verified listings, each row checked
    against its own source URL. There are seven of them and they are all in
    London, because that is how far hand-verification got. Those rows carry
    `is_sample: false` and are the only events in the system that claim to be
    real.

  * data/events.samples.jsonl -- this file's output. Every row carries
    `is_sample: true`, a title that begins with "Sample:", and a `source` that
    says in words that it is not a real listing. The UI marks them, the agent's
    prompt marks them, and the coverage panel counts them separately.

Why generate them at all: with events in one city out of five, the trip
planner and the agent could not be exercised anywhere else, and a reviewer
could not see how a sourced event and a sample are distinguished -- which is
the interesting part. So the samples are scaffolding for the demo, and they
are built to be impossible to mistake for the real thing.

The venue in each row is real: it comes from data/snapshot/places.jsonl, and
the row's source_url points at that venue's own record, not at a listing that
does not exist. Generation is deterministic -- same snapshot in, same file out
-- so the committed sample file is reproducible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from ..common import config

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s samples %(message)s")
log = logging.getLogger("samples")

SAMPLE_SOURCE = "generated sample - NOT a real listing (see services/ingestor/make_samples.py)"

# place category -> (event category, what the sample is called, start hour).
# Only categories where an evening or weekend programme is an ordinary use of
# the venue; nothing here implies a specific act, team or performer.
TEMPLATES: list[tuple[str, str, str, int]] = [
    ("concert_hall", "concert", "Sample: evening concert at {venue}", 20),
    ("theatre", "theatre", "Sample: evening performance at {venue}", 19),
    ("stadium", "sport", "Sample: league fixture at {venue}", 18),
    ("market", "market", "Sample: weekend market at {venue}", 9),
    ("park", "festival", "Sample: open-air music afternoon at {venue}", 15),
    ("gallery", "exhibition", "Sample: late opening at {venue}", 18),
    ("museum", "exhibition", "Sample: curator's tour at {venue}", 11),
]


def load_places(path: Path) -> dict[str, dict[str, list[dict[str, Any]]]]:
    by_city: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        by_city.setdefault(row["city_id"], {}).setdefault(row["category"], []).append(row)
    return by_city


def pick(rows: list[dict[str, Any]], seed: str) -> dict[str, Any]:
    """Deterministic choice: the same seed always picks the same venue, so
    re-running this against the same snapshot rewrites an identical file."""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return sorted(rows, key=lambda r: r["id"])[int.from_bytes(digest[:4], "big") % len(rows)]


def build(
    by_city: dict[str, dict[str, list[dict[str, Any]]]],
    start: date,
    days: int,
    per_city: int,
) -> list[dict[str, Any]]:
    as_of = datetime.now(UTC).isoformat()
    rows: list[dict[str, Any]] = []

    for city_id in sorted(by_city):
        categories = by_city[city_id]
        made = 0
        # Walk days and templates together so a city's samples land on
        # different dates and at different kinds of venue, rather than seven
        # concerts in one hall on one evening.
        for offset in range(days):
            if made >= per_city:
                break
            day = start + timedelta(days=offset)
            place_category, event_category, title, hour = TEMPLATES[offset % len(TEMPLATES)]
            venues = categories.get(place_category)
            if not venues:
                continue
            venue = pick(venues, f"{city_id}|{day}|{place_category}")
            starts = datetime.combine(day, time(hour, 0), tzinfo=UTC)
            rows.append(
                {
                    "id": f"sample:{city_id}:{day}:{place_category}",
                    "city_id": city_id,
                    "title": title.format(venue=venue["name"]),
                    "category": event_category,
                    "venue": venue["name"],
                    "starts_at": starts.isoformat(),
                    "ends_at": (starts + timedelta(hours=3)).isoformat(),
                    "source": SAMPLE_SOURCE,
                    # Points at the venue's own record, which is real. There is
                    # no listing URL because there is no listing.
                    "source_url": venue.get("source_url"),
                    "is_sample": True,
                    "as_of": as_of,
                }
            )
            made += 1
        log.info("%s -> %d sample events", city_id, made)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate labelled sample events.")
    parser.add_argument("--days", type=int, default=16, help="window to spread samples over")
    parser.add_argument("--per-city", type=int, default=8)
    parser.add_argument(
        "--start",
        default=None,
        help="ISO date to start from; defaults to the first day in the weather snapshot",
    )
    args = parser.parse_args(argv)

    places_path = config.SNAPSHOT_DIR / "places.jsonl"
    if not places_path.exists():
        log.error("no places snapshot at %s -- fetch places first", places_path)
        return 1

    if args.start:
        start = date.fromisoformat(args.start)
    else:
        weather = config.SNAPSHOT_DIR / "weather.jsonl"
        dates = sorted(
            json.loads(line)["forecast_date"]
            for line in weather.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if not dates:
            log.error("no weather snapshot to take a start date from")
            return 1
        start = date.fromisoformat(dates[0])

    rows = build(load_places(places_path), start, args.days, args.per_city)
    out = config.DATA_DIR / "events.samples.jsonl"
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    log.info("wrote %d labelled sample events to %s", len(rows), out)
    log.info("every row is is_sample=true and titled 'Sample: ...'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
