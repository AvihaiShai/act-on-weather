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
from datetime import datetime

import psycopg
import yaml

from ..common import coast, config, rules, schemas
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

# city_id -> has a coast. Filled by seed_cities from data/cities.yml, which is
# also where the DB column comes from, so the two cannot disagree.
COASTAL: dict[str, bool] = {}

# Fields a user is allowed to correct, per entity. Anything else in a PATCH is
# rejected as poison: the update path must not become an arbitrary SQL surface.
PATCHABLE = {
    "places": {"name", "category", "address", "lat", "lon"},
    "facts": {"title", "summary", "topic"},
    # `checked_at` is patchable on purpose: re-opening a listing page and
    # confirming that it is still on is a correction like any other, and it is
    # the only way an operator can extend a row's life without editing the seed
    # and re-ingesting. `valid_until` is deliberately NOT patchable -- it is
    # derived from `checked_at` by the policy in config.EVENT_RECHECK_DAYS, and
    # a hand-set expiry would let a row outlive the check that justifies it.
    "events": {"title", "category", "venue", "starts_at", "ends_at", "checked_at"},
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


def coast_columns(city: dict) -> dict[str, object]:
    """The four `cities.coast_*` values for one entry of data/cities.yml.

    The distance is derived, never read: data/cities.yml holds the two
    coordinates and nothing else, so there is no committed number that can
    survive somebody correcting one of them. A city with no `coast` block --
    every inland one -- gets four nulls, which is how the reading code tells
    "no coast on record" from "a coast 25 km away".

    Nothing here asserts anything about the water. The distance says where the
    forecast that scores surfing was actually taken, which for Rome is about
    25 km from the sea; what the sea is doing is not measured anywhere in this
    system, and data/activities.yml caps the affected scores because of it.
    """
    block = city.get("coast") or {}
    if not block:
        return {
            "coast_name": None,
            "coast_lat": None,
            "coast_lon": None,
            "coast_distance_km": None,
        }
    return {
        "coast_name": block["name"],
        "coast_lat": float(block["lat"]),
        "coast_lon": float(block["lon"]),
        "coast_distance_km": round(
            coast.haversine_km(
                float(city["lat"]), float(city["lon"]), float(block["lat"]), float(block["lon"])
            ),
            3,
        ),
    }


def seed_cities(conn: psycopg.Connection) -> None:
    """The city list is configuration, not collected data, so it does not go
    through the queue. It is seeded here because the consumer is the only role
    that may write, and because every collected record has a foreign key to it.
    This is the one documented exception to M4.

    A coastal city also carries a `coast` block naming a real point on its
    coast. Its distance from the city's forecast point is computed here rather
    than read from the file, so the stored number cannot fall out of step with
    the two coordinates it comes from -- see `coast_columns`.
    """
    with open(config.DATA_DIR / "cities.yml", encoding="utf-8") as fh:
        cities = yaml.safe_load(fh)["cities"]
    with conn.cursor() as cur:
        for city in cities:
            cur.execute(
                "INSERT INTO cities (id, name, country, lat, lon, timezone, aliases, coastal,"
                "                    coast_name, coast_lat, coast_lon, coast_distance_km)"
                " VALUES (%(slug)s, %(name)s, %(country)s, %(lat)s, %(lon)s,"
                "         %(timezone)s, %(aliases)s, %(coastal)s,"
                "         %(coast_name)s, %(coast_lat)s, %(coast_lon)s, %(coast_distance_km)s)"
                " ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name,"
                "   country = EXCLUDED.country, lat = EXCLUDED.lat, lon = EXCLUDED.lon,"
                "   timezone = EXCLUDED.timezone, aliases = EXCLUDED.aliases,"
                "   coastal = EXCLUDED.coastal, coast_name = EXCLUDED.coast_name,"
                "   coast_lat = EXCLUDED.coast_lat, coast_lon = EXCLUDED.coast_lon,"
                "   coast_distance_km = EXCLUDED.coast_distance_km",
                {
                    **city,
                    "aliases": city.get("aliases", []),
                    "coastal": bool(city.get("coastal", False)),
                    **coast_columns(city),
                },
            )
    conn.commit()
    COASTAL.clear()
    COASTAL.update({c["slug"]: bool(c.get("coastal", False)) for c in cities})
    # The third number is the one worth reading: a city marked coastal with no
    # coast reference point is a claim nothing can check.
    log.info(
        "seeded %d cities (%d coastal, %d with a coast reference point)",
        len(cities),
        sum(COASTAL.values()),
        sum(1 for c in cities if c.get("coast")),
    )


def enforce_event_mode(conn: psycopg.Connection) -> int:
    """Demo rows must not survive a return to a default run.

    Generated sample events (`is_sample`) are a demonstration aid, not data the
    system claims. Gating them at the ingestor stops a default run from
    *accepting* them, but a database that once ran in demo mode would still be
    holding them. So the consumer -- the only role with write grants, which is
    what keeps this inside M4 -- deletes them on startup whenever demo mode is
    off.

    The effect is that `AOW_DEMO_EVENTS` describes the database, not just the
    run: start without it and the sample rows are gone, however they got there.
    """
    if config.DEMO_EVENTS:
        log.warning("DEMO MODE: generated sample events are permitted in this database")
        return 0
    with conn.cursor() as cur:
        cur.execute("DELETE FROM events WHERE is_sample")
        removed = cur.rowcount
    conn.commit()
    if removed:
        log.info("removed %d generated sample events left over from a demo run", removed)
    return removed


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
    """Create or refresh a recommendation row per activity for one city-day.

    The score is computed here, deterministically, for *every* activity the
    city supports, and stored immediately. Only the wording is left to the
    enricher, which is why an LLM that is down or slow can never block or lose
    weather data.

    What the enricher is asked to word is capped. Scoring 18 activities is
    microseconds; wording 18 x 5 cities x 16 days is ~1,300 CPU LLM calls per
    refresh, through a single llama.cpp slot the agent also uses. So the day's
    scores are ranked and only the top `ENRICH_TOP_N` go out as 'pending'. The
    rest are stored 'deferred': scored, charted and answerable, just not
    queued for prose. Asking for one by name flips it back to 'pending'
    (`store_recommendation_request`), so nothing is unreachable.
    """
    weather = p.model_dump()
    supported = rules.activities_for_city(ACTIVITIES, coastal=COASTAL.get(p.city_id, False))

    scored = [(key, cfg, rules.score_activity(key, cfg, weather)) for key, cfg in supported.items()]
    # Rank by score, then by key so a tie resolves the same way on every replay
    # -- a re-ingest must not shuffle which rows are worded.
    scored.sort(key=lambda item: (-item[2].score, item[0]))
    worded = {key for key, _cfg, _result in scored[: config.ENRICH_TOP_N]}

    for key, cfg, result in scored:
        cur.execute(
            """
            INSERT INTO recommendations (city_id, forecast_date, activity, activity_label,
                requested, score, band, reasons, rule_version, status, weather_as_of)
            VALUES (%(city_id)s, %(forecast_date)s, %(activity)s, %(label)s,
                false, %(score)s, %(band)s, %(reasons)s, %(rule_version)s,
                %(status)s, %(as_of)s)
            ON CONFLICT (city_id, forecast_date, activity) DO UPDATE SET
                activity_label = EXCLUDED.activity_label,
                score = EXCLUDED.score, band = EXCLUDED.band, reasons = EXCLUDED.reasons,
                rule_version = EXCLUDED.rule_version, weather_as_of = EXCLUDED.weather_as_of,
                -- The one invalidation rule in the system: when the weather
                -- behind a recommendation changes, its wording goes stale and
                -- is re-enriched. Nothing else resets a row to pending.
                -- A row a user asked for by name keeps its place in the
                -- queue however it ranks today.
                status = CASE WHEN recommendations.requested THEN 'pending'
                              ELSE EXCLUDED.status END,
                text = NULL, last_error = NULL, invalid_attempts = 0
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
                "status": "pending" if key in worded else "deferred",
                "as_of": p.as_of,
            },
        )


def upsert_by_id(
    cur: psycopg.Cursor,
    table: str,
    columns: list[str],
    payload: dict,
    *,
    also_when: str = "",
) -> None:
    """Idempotent upsert, keyed on the record id.

    The `WHERE EXCLUDED.as_of > <table>.as_of` guard is what makes a replay
    free: the same snapshot delivered twice writes nothing the second time, so
    a redelivered message cannot bump a revision or file a spurious history row.

    `also_when` widens that guard for a column whose value is *derived* rather
    than collected, and which can therefore legitimately change while the
    record itself has not. An event's `valid_until` is the case this exists
    for: it comes from the configured recheck window, so lowering
    AOW_EVENT_RECHECK_DAYS and re-ingesting has to move every stored expiry.
    Under the as-of guard alone it moved none of them, and the configured
    window and the stored window would disagree with nothing to say so.
    """
    placeholders = ", ".join(f"%({c})s" for c in columns)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c != "id")
    guard = f"EXCLUDED.as_of > {table}.as_of"
    if also_when:
        guard = f"({guard} OR {also_when})"
    cur.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
        f" ON CONFLICT (id) DO UPDATE SET {updates}, ingested_at = now()"
        f" WHERE {guard}",
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
    # See migration 006. `checked_at` is when the listing was last read off its
    # own page, and `valid_until` is when that reading stops being offered as a
    # current schedule. Both travel on the message so the freshness policy is
    # decided once, by the producer, rather than re-derived by every reader.
    "checked_at",
    "valid_until",
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

    # If the user named an activity the catalogue already knows, score it with
    # its own thresholds rather than the generic outdoor-comfort fallback --
    # and, if it is deferred for this day, this promotes it back to 'pending'
    # so the model words the thing that was actually asked about.
    cfg = ACTIVITIES.get(p.activity)
    if cfg is not None and (not cfg.get("requires_coast") or COASTAL.get(p.city_id, False)):
        result = rules.score_activity(p.activity, cfg, row)
    else:
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


def request_reenrich(cur: psycopg.Cursor, p: schemas.ReenrichRequest) -> None:
    """M12: re-word stored recommendations with the local model.

    Scores are untouched -- they are the rule engine's output and re-running
    them is what a weather refresh does. This resets only the wording, which
    is the update path that works with no connectivity at all.

    A row whose wording gave up ('failed') gets its attempt counter cleared;
    otherwise it would come straight back as failed on the next reply.
    """
    statuses = ["ready", "failed"]
    if p.include_deferred:
        statuses.append("deferred")

    clauses = ["status = ANY(%(statuses)s)"]
    params: dict[str, object] = {"statuses": statuses}
    if p.city_id:
        clauses.append("city_id = %(city_id)s")
        params["city_id"] = p.city_id
    if p.forecast_date:
        clauses.append("forecast_date = %(forecast_date)s")
        params["forecast_date"] = p.forecast_date
    if p.activity:
        clauses.append("activity = %(activity)s")
        params["activity"] = p.activity

    cur.execute(
        "UPDATE recommendations"
        "   SET status = 'pending', text = NULL, model = NULL,"
        "       last_error = NULL, invalid_attempts = 0"
        f" WHERE {' AND '.join(clauses)}",
        params,
    )
    log.info("re-enrichment queued %d recommendation(s)", cur.rowcount)


def apply_patch(cur: psycopg.Cursor, p: schemas.RecordPatch) -> None:
    """M12: a user correction. The trigger bumps the revision and files history."""
    allowed = PATCHABLE[p.entity]
    unknown = set(p.fields) - allowed
    if unknown:
        raise Poison(f"fields not patchable on {p.entity}: {', '.join(sorted(unknown))}")
    if not p.fields:
        raise Poison("patch carries no fields")

    fields = dict(p.fields)
    if p.entity == "events" and "checked_at" in fields:
        # Re-checking a listing is the point of patching `checked_at`, and a
        # re-check that did not move the expiry would be a no-op: the row would
        # carry a fresh check date and still be filtered out as stale. So the
        # derived column moves with it, by the same policy the ingestor uses,
        # which is also why `valid_until` is not patchable on its own.
        checked = fields["checked_at"]
        if isinstance(checked, str):
            try:
                checked = datetime.fromisoformat(checked)
            except ValueError as exc:
                # Poison, not a retry. `fields` is the one request body left
                # open, so this is the first place a caller-supplied string is
                # parsed rather than handed to the database, and a value that
                # is not a timestamp will never become one: requeuing it would
                # spin forever instead of dead-lettering with a reason.
                raise Poison(f"checked_at is not a timestamp: {checked!r}") from exc
        if not isinstance(checked, datetime):
            raise Poison(f"checked_at is not a timestamp: {checked!r}")
        fields["valid_until"] = config.event_valid_until(checked)

    assignments = ", ".join(f"{k} = %({k})s" for k in fields)
    params = dict(fields)
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


def upsert_event(cur: psycopg.Cursor, p) -> None:
    """Store an event, unless it is a generated sample and demo mode is off.

    The ingestor already decides not to replay the sample file, so in practice
    nothing gets here. This is the same check repeated at the write boundary,
    because "no generated row is stored by default" is a claim the README
    makes, and it should hold for any producer, not only for the one we wrote.
    Dropping is deliberate: a sample arriving with demo mode off is a
    configuration mismatch, not a corrupt message, so it is not poison.
    """
    if getattr(p, "is_sample", False) and not config.DEMO_EVENTS:
        log.warning("dropping generated sample event %s: demo mode is off", p.id)
        return
    # `valid_until` is derived from the configured recheck window rather than
    # collected, so a re-ingest that carries a different expiry for an
    # otherwise unchanged row has to be allowed through. Replaying the same
    # snapshot under the same window still writes nothing, because then neither
    # half of the guard is true.
    upsert_by_id(
        cur,
        "events",
        EVENT_COLS,
        p.model_dump(),
        also_when="EXCLUDED.valid_until IS DISTINCT FROM events.valid_until",
    )


HANDLERS = {
    config.RK_WEATHER: None,  # handled inline, it also creates pending rows
    config.RK_PLACE: lambda cur, p: upsert_by_id(cur, "places", PLACE_COLS, p.model_dump()),
    config.RK_FACT: lambda cur, p: upsert_by_id(cur, "facts", FACT_COLS, p.model_dump()),
    config.RK_EVENT: lambda cur, p: upsert_event(cur, p),
    config.RK_RECOMMENDATION_REQUEST: store_recommendation_request,
    config.RK_LLM_RECOMMENDATION: store_llm_recommendation,
    config.RK_PATCH: apply_patch,
    config.RK_REENRICH: request_reenrich,
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
    enforce_event_mode(conn)
    log.info("consuming %s", config.QUEUE)
    consume(handle, name="aow-consumer")


if __name__ == "__main__":
    main()
