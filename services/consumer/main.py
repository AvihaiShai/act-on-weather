"""The consumer: the only role in the system with write grants (M4).

Every record -- weather, places, facts, events, itineraries, the LLM's
recommendation text and a user's correction -- arrives here through
`aow.ingest` and is written in one transaction that also records its
`message_id` in `ingest_log`. The ack happens only after that transaction
commits.

That ordering is the whole delivery story:

  * crash before commit  -> nothing was written, the broker redelivers
  * crash after commit, before ack -> the broker redelivers, `ingest_log`'s
    primary key rejects it, and the message is acked without a second write
  * database unreachable -> the handler raises, the message is requeued, and
    the consumer waits for the database to come back

So the semantics are at-least-once delivery with idempotent writes, which is
effectively-once storage. The `message_id` is the idempotency key.
"""

from __future__ import annotations

import json
import logging
import os

import psycopg
import yaml

from ..common import config, rules, schemas
from ..common.db import Pool
from ..common.envelope import Envelope
from ..common.rabbit import Poison, consume

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s consumer %(message)s",
)
log = logging.getLogger("consumer")

pool = Pool(config.writer_dsn())

RULE_VERSION, ACTIVITIES = rules.load_activities(config.DATA_DIR / "activities.yml")

# Fields a user is allowed to correct, per entity. Anything else in a PATCH is
# rejected as poison: the update path must not become an arbitrary SQL surface.
PATCHABLE = {
    "places": {"name", "category", "address", "lat", "lon"},
    "facts": {"title", "summary", "topic"},
    "events": {"title", "category", "venue", "starts_at", "ends_at"},
    "itineraries": {"title", "days"},
    "weather_daily": {
        "temp_max_c",
        "temp_min_c",
        "precip_mm",
        "precip_prob",
        "wind_kmh",
        "uv_index",
        "sunshine_hours",
    },
}


# ---------------------------------------------------------------- cities ----


def seed_cities(conn: psycopg.Connection) -> None:
    """The city list is configuration, not collected data, so it does not go
    through the queue. It is seeded here because the consumer is the only role
    that may write, and because every collected record has a foreign key to it.
    This is the one documented exception to M4.
    """
    with open(config.DATA_DIR / "cities.yml", encoding="utf-8") as fh:
        cities = yaml.safe_load(fh)["cities"]
    with conn.cursor() as cur:
        for city in cities:
            cur.execute(
                "INSERT INTO cities (id, name, country, lat, lon, timezone, aliases)"
                " VALUES (%(slug)s, %(name)s, %(country)s, %(lat)s, %(lon)s,"
                "         %(timezone)s, %(aliases)s)"
                " ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name,"
                "   country = EXCLUDED.country, lat = EXCLUDED.lat, lon = EXCLUDED.lon,"
                "   timezone = EXCLUDED.timezone, aliases = EXCLUDED.aliases",
                {**city, "aliases": city.get("aliases", [])},
            )
    conn.commit()
    log.info("seeded %d cities", len(cities))


# ------------------------------------------------------------- upserting ----


def upsert_weather(cur: psycopg.Cursor, p: schemas.WeatherDaily) -> bool:
    """Returns True if the stored row actually changed."""
    cur.execute(
        """
        INSERT INTO weather_daily (city_id, forecast_date, provider, temp_min_c, temp_max_c,
            precip_mm, precip_prob, wind_kmh, uv_index, sunshine_hours, sunrise, sunset,
            weather_code, source_url, as_of)
        VALUES (%(city_id)s, %(forecast_date)s, %(provider)s, %(temp_min_c)s, %(temp_max_c)s,
            %(precip_mm)s, %(precip_prob)s, %(wind_kmh)s, %(uv_index)s, %(sunshine_hours)s,
            %(sunrise)s, %(sunset)s, %(weather_code)s, %(source_url)s, %(as_of)s)
        ON CONFLICT (city_id, forecast_date) DO UPDATE SET
            provider = EXCLUDED.provider, temp_min_c = EXCLUDED.temp_min_c,
            temp_max_c = EXCLUDED.temp_max_c, precip_mm = EXCLUDED.precip_mm,
            precip_prob = EXCLUDED.precip_prob, wind_kmh = EXCLUDED.wind_kmh,
            uv_index = EXCLUDED.uv_index, sunshine_hours = EXCLUDED.sunshine_hours,
            sunrise = EXCLUDED.sunrise, sunset = EXCLUDED.sunset,
            weather_code = EXCLUDED.weather_code, source_url = EXCLUDED.source_url,
            as_of = EXCLUDED.as_of, ingested_at = now()
        -- An older forecast never overwrites a newer one, whatever order the
        -- broker happens to redeliver in.
        WHERE EXCLUDED.as_of > weather_daily.as_of
        RETURNING revision
        """,
        p.model_dump(),
    )
    return cur.fetchone() is not None


def score_defaults(cur: psycopg.Cursor, p: schemas.WeatherDaily) -> None:
    """Create or refresh the pending recommendation row per default activity.

    The score is computed here, deterministically, and stored immediately. Only
    the wording is left `pending` for the enricher, which is why an LLM that is
    down or slow can never block or lose weather data.
    """
    weather = p.model_dump()
    for key, cfg in ACTIVITIES.items():
        result = rules.score_activity(key, cfg, weather)
        cur.execute(
            """
            INSERT INTO recommendations (city_id, forecast_date, activity, activity_label,
                requested, score, band, reasons, rule_version, status, weather_as_of)
            VALUES (%(city_id)s, %(forecast_date)s, %(activity)s, %(label)s,
                false, %(score)s, %(band)s, %(reasons)s, %(rule_version)s, 'pending', %(as_of)s)
            ON CONFLICT (city_id, forecast_date, activity) DO UPDATE SET
                score = EXCLUDED.score, band = EXCLUDED.band, reasons = EXCLUDED.reasons,
                rule_version = EXCLUDED.rule_version, weather_as_of = EXCLUDED.weather_as_of,
                -- The one invalidation rule in the system: when the weather
                -- behind a recommendation changes, its wording goes stale and
                -- is re-enriched. Nothing else resets a row to pending.
                status = 'pending', text = NULL, last_error = NULL, invalid_attempts = 0
            """,
            {
                "city_id": p.city_id,
                "forecast_date": p.forecast_date,
                "activity": key,
                "label": cfg.get("label", key),
                "score": result.score,
                "band": result.band,
                "reasons": json.dumps(result.reasons),
                "rule_version": RULE_VERSION,
                "as_of": p.as_of,
            },
        )


def upsert_by_id(cur: psycopg.Cursor, table: str, columns: list[str], payload: dict) -> None:
    placeholders = ", ".join(f"%({c})s" for c in columns)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c != "id")
    cur.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
        f" ON CONFLICT (id) DO UPDATE SET {updates}, ingested_at = now()"
        f" WHERE EXCLUDED.as_of > {table}.as_of",
        payload,
    )


PLACE_COLS = [
    "id",
    "city_id",
    "name",
    "category",
    "lat",
    "lon",
    "address",
    "source",
    "source_url",
    "is_sample",
    "as_of",
]
FACT_COLS = [
    "id",
    "city_id",
    "title",
    "summary",
    "topic",
    "source",
    "source_url",
    "is_sample",
    "as_of",
]
EVENT_COLS = [
    "id",
    "city_id",
    "title",
    "category",
    "venue",
    "starts_at",
    "ends_at",
    "source",
    "source_url",
    "is_sample",
    "as_of",
]


def store_recommendation_request(cur: psycopg.Cursor, p: schemas.RecommendationRequest) -> None:
    """A user asked about an activity that has no rule of its own (M2).

    It is scored against general outdoor comfort from the weather already
    stored, and inserted `pending` so the enricher words it like any other.
    """
    row = cur.execute(
        "SELECT temp_max_c, temp_min_c, precip_mm, precip_prob, wind_kmh, uv_index,"
        "       sunshine_hours, as_of"
        "  FROM weather_daily WHERE city_id = %s AND forecast_date = %s",
        (p.city_id, p.forecast_date),
    ).fetchone()
    if row is None:
        # No weather for that day: store the request as failed with the reason,
        # so the UI can say "no data for that date" instead of showing nothing.
        cur.execute(
            "INSERT INTO recommendations (city_id, forecast_date, activity, activity_label,"
            "    requested, status, last_error)"
            " VALUES (%s, %s, %s, %s, true, 'failed', 'no stored weather for that date')"
            " ON CONFLICT (city_id, forecast_date, activity) DO NOTHING",
            (p.city_id, p.forecast_date, p.activity, p.activity_label),
        )
        return

    result = rules.score_requested(p.activity_label, row)
    cur.execute(
        """
        INSERT INTO recommendations (city_id, forecast_date, activity, activity_label,
            requested, score, band, reasons, rule_version, status, weather_as_of)
        VALUES (%s, %s, %s, %s, true, %s, %s, %s, %s, 'pending', %s)
        ON CONFLICT (city_id, forecast_date, activity) DO UPDATE SET
            requested = true, score = EXCLUDED.score, band = EXCLUDED.band,
            reasons = EXCLUDED.reasons, weather_as_of = EXCLUDED.weather_as_of,
            status = 'pending', text = NULL, last_error = NULL, invalid_attempts = 0
        """,
        (
            p.city_id,
            p.forecast_date,
            p.activity,
            p.activity_label,
            result.score,
            result.band,
            json.dumps(result.reasons),
            RULE_VERSION,
            row["as_of"],
        ),
    )


def store_llm_recommendation(cur: psycopg.Cursor, p: schemas.LlmRecommendation) -> None:
    """The enricher's output, arriving back through the queue like any record."""
    if p.status == "invalid":
        # The model produced unusable output. Count it here, in the database,
        # so the cap survives an enricher restart -- and give up only once the
        # count is reached. A temporary LLM outage never reaches this path.
        cur.execute(
            "UPDATE recommendations"
            "   SET invalid_attempts = invalid_attempts + 1,"
            "       last_error = %s,"
            "       status = CASE WHEN invalid_attempts + 1 >= %s THEN 'failed'"
            "                     ELSE 'pending' END"
            " WHERE city_id = %s AND forecast_date = %s AND activity = %s"
            "   AND status = 'pending'",
            (p.error, config.ENRICH_MAX_INVALID_ATTEMPTS, p.city_id, p.forecast_date, p.activity),
        )
        return

    cur.execute(
        "UPDATE recommendations SET status = %s, text = %s, model = %s, last_error = %s"
        " WHERE city_id = %s AND forecast_date = %s AND activity = %s"
        # A recommendation whose weather moved on while the LLM was thinking is
        # already pending again; do not stamp it with the stale wording.
        "   AND (%s IS NULL OR weather_as_of <= %s)",
        (
            p.status,
            p.text,
            p.model,
            p.error,
            p.city_id,
            p.forecast_date,
            p.activity,
            p.weather_as_of,
            p.weather_as_of,
        ),
    )


def apply_patch(cur: psycopg.Cursor, p: schemas.RecordPatch) -> None:
    """M12: a user correction. The trigger bumps the revision and files history."""
    allowed = PATCHABLE[p.entity]
    unknown = set(p.fields) - allowed
    if unknown:
        raise Poison(f"fields not patchable on {p.entity}: {', '.join(sorted(unknown))}")
    if not p.fields:
        raise Poison("patch carries no fields")

    assignments = ", ".join(f"{k} = %({k})s" for k in p.fields)
    params = dict(p.fields)
    if p.entity == "weather_daily":
        city_id, _, forecast_date = p.entity_id.partition("/")
        params.update({"city_id": city_id, "forecast_date": forecast_date})
        where = "city_id = %(city_id)s AND forecast_date = %(forecast_date)s"
    else:
        params["row_id"] = p.entity_id
        where = "id = %(row_id)s"

    cur.execute(f"UPDATE {p.entity} SET {assignments} WHERE {where} RETURNING revision", params)
    if cur.fetchone() is None:
        raise Poison(f"no such record: {p.entity}/{p.entity_id}")


HANDLERS = {
    config.RK_WEATHER: None,  # handled inline, it also creates pending rows
    config.RK_PLACE: lambda cur, p: upsert_by_id(cur, "places", PLACE_COLS, p.model_dump()),
    config.RK_FACT: lambda cur, p: upsert_by_id(cur, "facts", FACT_COLS, p.model_dump()),
    config.RK_EVENT: lambda cur, p: upsert_by_id(cur, "events", EVENT_COLS, p.model_dump()),
    config.RK_RECOMMENDATION_REQUEST: store_recommendation_request,
    config.RK_LLM_RECOMMENDATION: store_llm_recommendation,
    config.RK_PATCH: apply_patch,
}


def upsert_itinerary(cur: psycopg.Cursor, p: schemas.Itinerary) -> None:
    cur.execute(
        "INSERT INTO itineraries (id, city_id, title, start_date, end_date, days, as_of)"
        " VALUES (%(id)s, %(city_id)s, %(title)s, %(start_date)s, %(end_date)s,"
        "         %(days)s, %(as_of)s)"
        " ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, days = EXCLUDED.days,"
        "   start_date = EXCLUDED.start_date, end_date = EXCLUDED.end_date,"
        "   as_of = EXCLUDED.as_of",
        {**p.model_dump(), "days": json.dumps(p.model_dump()["days"], default=str)},
    )


HANDLERS[config.RK_ITINERARY] = upsert_itinerary


# ----------------------------------------------------------------- handle ----


def handle(routing_key: str, body: bytes, _message_id: str | None) -> None:
    try:
        envelope = Envelope.from_bytes(body)
    except Exception as exc:  # noqa: BLE001 - anything unparseable is poison
        raise Poison(f"unparseable envelope: {exc}") from exc

    if envelope.routing_key not in config.ROUTING_KEYS:
        raise Poison(f"unknown routing key: {envelope.routing_key}")

    try:
        payload = schemas.validate(envelope.routing_key, envelope.payload)
    except schemas.ValidationError as exc:
        raise Poison(f"invalid {envelope.routing_key} payload: {exc}") from exc

    conn = pool.conn
    try:
        with conn.transaction(), conn.cursor() as cur:
            # The idempotency check and the business write are one
            # transaction. A redelivery after a committed write loses the
            # race here and is acked without writing anything twice.
            cur.execute(
                "INSERT INTO ingest_log (message_id, routing_key, source)"
                " VALUES (%s, %s, %s) ON CONFLICT (message_id) DO NOTHING"
                " RETURNING message_id",
                (envelope.message_id, envelope.routing_key, envelope.source),
            )
            if cur.fetchone() is None:
                log.info("duplicate %s, already stored", envelope.message_id)
                return

            if envelope.routing_key == config.RK_WEATHER:
                changed = upsert_weather(cur, payload)
                if changed:
                    score_defaults(cur, payload)
            else:
                HANDLERS[envelope.routing_key](cur, payload)
    except Poison:
        raise
    except psycopg.OperationalError:
        # The database went away. Drop the connection so the next attempt
        # reconnects, and let the message be requeued -- nothing is lost.
        pool.drop()
        raise
    log.info("stored %s %s", envelope.routing_key, envelope.message_id)


def main() -> None:
    conn = pool.conn
    seed_cities(conn)
    log.info("consuming %s", config.QUEUE)
    consume(handle, name="aow-consumer")


if __name__ == "__main__":
    main()
