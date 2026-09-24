"""The ingestor: the only service with a route out, and the only producer of
collected data.

It does two independent things, deliberately kept separate:

  1. ACCEPT  -- turn a source (a committed snapshot offline, or Open-Meteo when
     connected) into envelopes and fsync them into the outbox. This is the
     acceptance point: after it returns, the record is owed.
  2. PUBLISH -- drain unpublished outbox rows to RabbitMQ under publisher
     confirms, marking each one published only once the broker has confirmed it.

Because those are separate, a broker outage stops (2) and leaves (1) working:
records pile up on the volume and replay when the broker returns. That is
drill 3 of M11, and it is the whole reason the outbox exists.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from ..common import config
from ..common.envelope import Envelope
from ..common.outbox import Outbox
from ..common.rabbit import Publisher, PublishError

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s ingestor %(message)s",
)
log = logging.getLogger("ingestor")

# A deterministic namespace, so replaying the same snapshot produces the same
# message_ids and the outbox's UNIQUE constraint silently deduplicates them.
# A refreshed forecast has a new as_of, so it gets a new id and a new delivery.
NS = uuid.UUID("6f3a1e8c-0b5d-4d3a-9d2f-5c7b1a9e4d20")

SNAPSHOT_FILES = {
    "weather.jsonl": config.RK_WEATHER,
    "places.jsonl": config.RK_PLACE,
    "facts.jsonl": config.RK_FACT,
    "events.jsonl": config.RK_EVENT,
}

# Generated sample events. Replayed only in demo mode (config.DEMO_EVENTS), so
# a default run never even accepts them -- they are not filtered out later,
# they are never turned into envelopes in the first place.
DEMO_SNAPSHOT_FILES = {
    "events.samples.jsonl": config.RK_EVENT,
}

# One per process, and used only for the file above. See envelopes_from.
DEMO_EPOCH = uuid.uuid4().hex


def deterministic_id(routing_key: str, *parts: Any) -> str:
    return str(uuid.uuid5(NS, "|".join([routing_key, *[str(p) for p in parts]])))


def load_cities(path: Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)["cities"]


def natural_key(routing_key: str, payload: dict[str, Any]) -> tuple:
    if routing_key == config.RK_WEATHER:
        return (payload["city_id"], payload["forecast_date"])
    return (payload["id"],)


def envelopes_from(
    routing_key: str,
    payloads: Iterable[dict[str, Any]],
    source: str,
    salt: str = "",
):
    """`salt` deliberately breaks the determinism, for one caller only.

    Every real record gets an id derived from its natural key and its as_of, so
    replaying the same snapshot produces the same ids and the outbox silently
    deduplicates. That is the property the delivery guarantee rests on.

    Generated sample events are the exception, because they are the one thing
    here that can be *removed*: leaving demo mode deletes them. Re-entering it
    is then a genuinely new delivery -- the old message_id is in the outbox as
    published and in `ingest_log` as written, so an unsalted replay would be
    correctly ignored and the samples would never come back. Salting per boot
    mints new ids, and the upsert on `events.id` keeps the table at 45 rows
    however many times demo mode is toggled.
    """
    for payload in payloads:
        parts = [*natural_key(routing_key, payload), payload["as_of"]]
        if salt:
            parts.append(salt)
        yield Envelope.create(
            routing_key,
            payload,
            source=source,
            observed_at=payload["as_of"],
            city=payload.get("city_id"),
            message_id=deterministic_id(routing_key, *parts),
        )


# ------------------------------------------------------------- accepting ----


def accept_snapshot(box: Outbox, snapshot_dir: Path) -> int:
    """Offline default: replay the committed snapshot into the outbox."""
    total = 0
    files = [(name, rk, "") for name, rk in SNAPSHOT_FILES.items()]
    if config.DEMO_EVENTS:
        files += [(name, rk, DEMO_EPOCH) for name, rk in DEMO_SNAPSHOT_FILES.items()]
        log.warning("DEMO MODE: generated sample events will be accepted and stored")
    for filename, routing_key, salt in files:
        path = snapshot_dir / filename
        if not path.exists():
            log.warning("snapshot file missing, skipping: %s", path)
            continue
        payloads = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                payloads.append(json.loads(line))
        ids = box.accept_many(list(envelopes_from(routing_key, payloads, "snapshot", salt)))
        total += len(ids)
        log.info("accepted %d %s records from %s", len(ids), routing_key, path.name)
    return total


def accept_live_weather(box: Outbox, cities: list[dict[str, Any]], days: int) -> int:
    """Connected refresh: fetch a fresh forecast and accept it (M12).

    Every accepted day carries a new as_of, so the consumer's revision bump
    fires and the coverage window moves forward.
    """
    from .providers import get_provider

    provider = get_provider(os.environ.get("WEATHER_PROVIDER", "open-meteo"))
    total = 0
    for city in cities:
        try:
            payloads = provider.daily_forecast(city, days)
        except Exception as exc:  # noqa: BLE001 - one city must not stop the rest
            log.error("forecast fetch failed for %s: %s", city["slug"], exc)
            continue
        ids = box.accept_many(list(envelopes_from(config.RK_WEATHER, payloads, provider.name)))
        total += len(ids)
        log.info("accepted %d forecast days for %s", len(ids), city["slug"])
    return total


# ------------------------------------------------------------ publishing ----


def drain(box: Outbox, publisher: Publisher, limit: int = 200) -> int:
    """Publish what is owed. Returns how many were confirmed this pass."""
    published = 0
    for row in box.unpublished(limit):
        try:
            publisher.publish(row["routing_key"], row["body"], row["message_id"])
        except PublishError as exc:
            # The row stays unpublished on purpose: this is the broker-down
            # path, and it must look like a delay, not a loss.
            box.mark_failed(row["seq"], str(exc))
            log.warning("publish failed for %s: %s", row["message_id"], exc)
            publisher.close()
            break
        box.mark_published(row["seq"])
        published += 1
    return published


def main() -> int:
    mode = os.environ.get("INGEST_MODE", "snapshot")
    days = int(os.environ.get("FORECAST_DAYS", "16"))
    interval = float(os.environ.get("INGEST_INTERVAL_S", "0"))
    cities = load_cities(config.DATA_DIR / "cities.yml")

    box = Outbox(config.OUTBOX_PATH)
    publisher = Publisher(name="aow-ingestor")
    log.info("starting in %s mode, outbox=%s", mode, config.OUTBOX_PATH)

    def accept_once() -> int:
        if mode == "live":
            return accept_live_weather(box, cities, days)
        return accept_snapshot(box, config.SNAPSHOT_DIR)

    accepted = accept_once()
    log.info("accepted %d records; outbox %s", accepted, box.counts())

    last_ingest = time.monotonic()
    while True:
        published = drain(box, publisher)
        if published:
            log.info("published %d; outbox %s", published, box.counts())
        if interval and time.monotonic() - last_ingest >= interval:
            accept_once()
            last_ingest = time.monotonic()
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main() or 0)
