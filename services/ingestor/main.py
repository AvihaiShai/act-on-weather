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
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from ..common import config, metrics
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
        # An event's `valid_until` is derived from the running recheck window
        # rather than collected (see `apply_freshness_policy`), so two accepts
        # of the same listing can legitimately differ while its `as_of` does
        # not. Without it in the id, lowering AOW_EVENT_RECHECK_DAYS would mint
        # the same message_id, the outbox would deduplicate it as a replay, and
        # the new window would never reach the database. Still deterministic:
        # the same snapshot under the same window gives the same id, which is
        # the property the delivery guarantee rests on.
        if routing_key == config.RK_EVENT and payload.get("valid_until"):
            parts.append(payload["valid_until"])
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


def apply_freshness_policy(routing_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Re-derive an event's expiry from the window this stack is running with.

    A snapshot is committed once and replayed on every boot, possibly months
    later and possibly on a machine configured differently from the one that
    built it. `checked_at` is a fact and travels unchanged; `valid_until` is
    not -- it is `checked_at` plus whatever recheck window the operator has
    chosen, so the value baked into the file is only the default the snapshot
    was built with.

    Deriving it here means the running configuration always wins. Set
    AOW_EVENT_RECHECK_DAYS=7 and restart, and every stored row's expiry moves
    on the next accept, because the payload genuinely differs and the
    consumer's upsert lets a changed `valid_until` through (see
    `consumer.upsert_event`). Without this the variable would only take effect
    at `make snapshot` time, which is not where an operator would look for it.

    Replaying the same snapshot under the same window changes nothing: the
    derived value is identical, so the message id is identical and the outbox
    deduplicates it exactly as before.
    """
    if routing_key != config.RK_EVENT or not payload.get("checked_at"):
        return payload
    checked = datetime.fromisoformat(payload["checked_at"])
    return {**payload, "valid_until": config.event_valid_until(checked).isoformat()}


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
                payloads.append(apply_freshness_policy(routing_key, json.loads(line)))
        ids = box.accept_many(list(envelopes_from(routing_key, payloads, "snapshot", salt)))
        total += len(ids)
        log.info("accepted %d %s records from %s", len(ids), routing_key, path.name)
    return total


def accept_live_weather(
    box: Outbox, cities: list[dict[str, Any]], days: int
) -> list[dict[str, Any]]:
    """Connected refresh: fetch a fresh forecast and accept it (M12).

    Every accepted day carries a new as_of, so the consumer's revision bump
    fires and the coverage window moves forward.

    Returns one result per city rather than a single total. A refresh is an
    operator action, and "203 days accepted" hides the case the operator most
    needs to see: the provider answered for four cities and refused the fifth,
    whose stored forecast is now quietly older than the rest. The accepted
    message ids come back too, so the wrapper can follow them to the database.

    One ingestion run is counted per city for the same reason: the failure that
    matters here is partial, and a single per-refresh counter would round it to
    "it worked". The city is not a label -- five cities times two outcomes would
    be fine today and would not be if the list grew -- so the per-city detail
    stays in the returned report and in `refresh_state`, and the metric carries
    only the source and the outcome.

    When `scripts/refresh.sh` runs the refresh module, this runs in a
    short-lived container that nothing scrapes, so those increments are lost.
    That path reports itself through `refresh_state.py`, which the API serves at
    `GET /refresh/last`; what these counters cover is the ingestor's own loop.
    """
    from .providers import get_provider

    provider = get_provider(os.environ.get("WEATHER_PROVIDER", "open-meteo"))
    results: list[dict[str, Any]] = []
    for city in cities:
        result: dict[str, Any] = {
            "city": city["slug"],
            "name": city.get("name", city["slug"]),
            "provider": provider.name,
            "ok": False,
            "error": None,
            "accepted": 0,
            "message_ids": [],
            "as_of": None,
            "first_date": None,
            "last_date": None,
        }
        try:
            payloads = provider.daily_forecast(city, days)
        except Exception as exc:  # noqa: BLE001 - one city must not stop the rest
            log.error("forecast fetch failed for %s: %s", city["slug"], exc)
            result["error"] = f"{type(exc).__name__}: {exc}"
            metrics.INGESTION_RUNS.labels(source=provider.name, result="failed").inc()
            results.append(result)
            continue
        if not payloads:
            log.error("forecast fetch returned no days for %s", city["slug"])
            result["error"] = "provider returned no forecast days"
            # A 200 with an empty forecast is a failed ingestion, not a quiet
            # success: nothing was accepted, so nothing will reach the database.
            metrics.INGESTION_RUNS.labels(source=provider.name, result="failed").inc()
            results.append(result)
            continue
        ids = box.accept_many(list(envelopes_from(config.RK_WEATHER, payloads, provider.name)))
        dates = sorted(p["forecast_date"] for p in payloads)
        result.update(
            ok=True,
            accepted=len(ids),
            message_ids=ids,
            as_of=max(p["as_of"] for p in payloads),
            first_date=dates[0],
            last_date=dates[-1],
        )
        results.append(result)
        # Counted after `accept_many` returned, so success means the days are
        # fsynced into the outbox and therefore owed -- not merely fetched.
        metrics.INGESTION_RUNS.labels(source=provider.name, result="ok").inc()
        metrics.INGESTION_LAST_SUCCESS.labels(source=provider.name).set(time.time())
        log.info("accepted %d forecast days for %s", len(ids), city["slug"])
    return results


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
            metrics.OUTBOX_PUBLISH_FAILURES.labels(service="ingestor").inc()
            log.warning("publish failed for %s: %s", row["message_id"], exc)
            publisher.close()
            break
        box.mark_published(row["seq"])
        # Only reached once the broker confirmed the publish, which is what
        # makes this counter mean "delivered" rather than "attempted".
        metrics.MESSAGES_PUBLISHED.labels(
            service="ingestor", routing_key=metrics.safe_routing_key(row["routing_key"])
        ).inc()
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

    metrics.start_metrics_server()
    # Export the active source before the first run. Without this zero sample,
    # a failed first ingest leaves the stale-ingestion alert with no series to
    # evaluate, and a run completed before the first scrape is invisible to
    # a rate query. The dashboard reads the process counter directly.
    if mode == "live":
        from .providers import get_provider

        source = get_provider(os.environ.get("WEATHER_PROVIDER", "open-meteo")).name
    else:
        source = "snapshot"
    metrics.INGESTION_LAST_SUCCESS.labels(source=source).set(0)
    for result in ("ok", "failed"):
        metrics.INGESTION_RUNS.labels(source=source, result=result).inc(0)
    # Exported through a separate read-only handle: the loop below owns the
    # writable one and takes no lock, because it is the only thread that touches
    # it, and handing that handle to a scrape thread would be introducing a race
    # for the sake of a gauge.
    metrics.register_outbox_file("ingestor", config.OUTBOX_PATH)

    def accept_once() -> int:
        if mode == "live":
            # accept_live_weather counts its own runs, one per city.
            return sum(r["accepted"] for r in accept_live_weather(box, cities, days))
        try:
            accepted = accept_snapshot(box, config.SNAPSHOT_DIR)
        except Exception:
            metrics.INGESTION_RUNS.labels(source="snapshot", result="failed").inc()
            raise
        metrics.INGESTION_RUNS.labels(source="snapshot", result="ok").inc()
        metrics.INGESTION_LAST_SUCCESS.labels(source="snapshot").set(time.time())
        return accepted

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
