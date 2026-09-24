"""The enricher: it words recommendations, it does not decide them.

By the time a row reaches here it already has its score, its band and its
reasons, computed deterministically by `common.rules` when the weather was
stored. The model is given those and asked for one or two sentences. It is
never asked whether the day is good.

That inversion is what makes the LLM non-critical: if `llm` is down, weather
still arrives, scores are still stored, the heatmap still renders, and the rows
simply sit `status='pending'` until it comes back. Nothing is lost and nothing
blocks (M2, M11).

It reads pending rows straight from Postgres with a SELECT-only role, and
publishes its result back into `aow.events` for the consumer to store -- so the
recommendation travels through the queue like every other record (M4), and
there is no second delivery branch that could silently drop half a fan-out.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from ..common import config
from ..common.db import Pool
from ..common.envelope import Envelope
from ..common.llm import LlmClient, LlmInvalidOutput, LlmUnavailable
from ..common.outbox import Outbox
from ..common.rabbit import Publisher, PublishError

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s enricher %(message)s",
)
log = logging.getLogger("enricher")

SYSTEM = (
    "You write one short weather recommendation for a traveller. "
    "A rule engine has already decided the verdict and given you the score, the "
    "verdict band and the reasons. Your job is only to phrase them. "
    "Rules: agree with the band -- never call a 'poor' day good or a 'good' day bad; "
    "use only the numbers you are given and invent none; do not mention scores, "
    "bands, rules or yourself; two sentences at most; plain English."
)

SCHEMA = {
    "type": "object",
    # Bounded in the grammar, not just asked for in the prompt: an unbounded
    # string runs past max_tokens and comes back as truncated, unparseable JSON.
    "properties": {"recommendation": {"type": "string", "maxLength": 400}},
    "required": ["recommendation"],
    "additionalProperties": False,
}

PENDING_SQL = """
SELECT r.city_id, r.forecast_date, r.activity, r.activity_label, r.score, r.band,
       r.reasons, r.weather_as_of, r.invalid_attempts, c.name AS city_name,
       w.temp_min_c, w.temp_max_c, w.precip_mm, w.precip_prob, w.wind_kmh,
       w.sunshine_hours
  FROM recommendations r
  JOIN cities c ON c.id = r.city_id
  LEFT JOIN weather_daily w
    ON w.city_id = r.city_id AND w.forecast_date = r.forecast_date
 WHERE r.status = 'pending'
 -- Nearest day first, best-rated activity first within it. With 18 activities
 -- in the catalogue the queue is long enough that the order is visible to a
 -- user watching the heatmap fill in, and this is the order they care about.
 -- `created_at` last, so the ordering is total and a restart resumes rather
 -- than reshuffles.
 ORDER BY r.forecast_date, r.score DESC NULLS LAST, r.created_at
 LIMIT %s
"""


def build_prompt(row: dict[str, Any]) -> str:
    reasons = row.get("reasons") or []
    lines = [
        f"City: {row['city_name']}",
        f"Date: {row['forecast_date']}",
        f"Activity: {row['activity_label']}",
        f"Verdict band: {row['band']} (score {row['score']} out of 100)",
        "Reasons the rule engine gave: " + ("; ".join(reasons) if reasons else "none"),
    ]
    facts = []
    if row.get("temp_max_c") is not None:
        facts.append(f"high {row['temp_max_c']:.0f}C")
    if row.get("temp_min_c") is not None:
        facts.append(f"low {row['temp_min_c']:.0f}C")
    if row.get("precip_mm") is not None:
        facts.append(f"{row['precip_mm']:.1f}mm rain")
    if row.get("precip_prob") is not None:
        facts.append(f"{row['precip_prob']}% chance of rain")
    if row.get("wind_kmh") is not None:
        facts.append(f"wind {row['wind_kmh']:.0f}km/h")
    if row.get("sunshine_hours") is not None:
        facts.append(f"{row['sunshine_hours']:.1f}h sunshine")
    if facts:
        lines.append("Measured for that day: " + ", ".join(facts))
    lines.append("Write the recommendation.")
    return "\n".join(lines)


def validate_text(text: Any) -> str:
    if not isinstance(text, str):
        raise LlmInvalidOutput("recommendation is not a string")
    text = " ".join(text.split())
    if len(text) < 15:
        raise LlmInvalidOutput(f"recommendation too short: {text!r}")
    if len(text) > 600:
        text = text[:600].rsplit(" ", 1)[0] + "..."
    return text


def accept_result(box: Outbox, payload: dict[str, Any], city: str) -> None:
    envelope = Envelope.create(
        config.RK_LLM_RECOMMENDATION,
        payload,
        source="enricher",
        observed_at=payload.get("weather_as_of") or "",
        city=city,
    )
    box.accept(envelope)


def drain(box: Outbox, publisher: Publisher) -> bool:
    """Confirm owed results before polling for more model work."""
    for row in box.unpublished():
        try:
            publisher.publish(row["routing_key"], row["body"], row["message_id"])
        except PublishError as exc:
            box.mark_failed(row["seq"], str(exc))
            log.warning("cannot publish %s: %s", row["message_id"], exc)
            publisher.close()
            return False
        box.mark_published(row["seq"])
    return True


def enrich_one(row: dict[str, Any], client: LlmClient, box: Outbox) -> str:
    """Returns 'ready', 'invalid', or raises LlmUnavailable to pause the batch."""
    base = {
        "city_id": row["city_id"],
        "forecast_date": row["forecast_date"].isoformat(),
        "activity": row["activity"],
        "weather_as_of": row["weather_as_of"].isoformat() if row["weather_as_of"] else None,
    }
    try:
        parsed = client.chat_json(SYSTEM, build_prompt(row), SCHEMA)
        text = validate_text(parsed.get("recommendation"))
    except LlmInvalidOutput as exc:
        log.warning(
            "invalid output for %s/%s/%s: %s",
            row["city_id"],
            row["forecast_date"],
            row["activity"],
            exc,
        )
        accept_result(box, {**base, "status": "invalid", "error": str(exc)[:400]}, row["city_id"])
        return "invalid"

    accept_result(
        box,
        {**base, "status": "ready", "text": text, "model": config.LLM_MODEL},
        row["city_id"],
    )
    return "ready"


def main() -> None:
    pool = Pool(config.reader_dsn(), autocommit=True)
    client = LlmClient()
    box = Outbox(config.OUTBOX_PATH)
    publisher = Publisher(name="aow-enricher")
    log.info(
        "polling pending recommendations every %.0fs (batch %d)",
        config.ENRICH_POLL_SECONDS,
        config.ENRICH_BATCH,
    )

    outage_backoff = 5.0
    while True:
        if not drain(box, publisher):
            time.sleep(5)
            continue
        try:
            rows = pool.conn.execute(PENDING_SQL, (config.ENRICH_BATCH,)).fetchall()
        except Exception as exc:  # noqa: BLE001 - the database will come back
            log.warning("cannot read pending rows (%s); retrying", exc)
            pool.drop()
            time.sleep(5)
            continue

        if not rows:
            time.sleep(config.ENRICH_POLL_SECONDS)
            continue

        ready = invalid = 0
        for row in rows:
            try:
                outcome = enrich_one(row, client, box)
                if not drain(box, publisher):
                    time.sleep(5)
                    break
            except LlmUnavailable as exc:
                # Temporary by definition. Stop the batch, wait, and come back
                # to the SAME rows: no attempt is consumed, so however long the
                # model is down, nothing is stranded or marked failed.
                log.warning("llm unavailable (%s); retrying in %.0fs", exc, outage_backoff)
                time.sleep(outage_backoff)
                outage_backoff = min(outage_backoff * 2, config.ENRICH_POLL_SECONDS)
                break
            outage_backoff = 5.0
            ready += outcome == "ready"
            invalid += outcome == "invalid"

        if ready or invalid:
            log.info("enriched %d, invalid %d", ready, invalid)
        # A short pause between batches keeps a single-threaded llama.cpp
        # responsive to the agent, which shares it.
        time.sleep(1)


if __name__ == "__main__":
    main()
