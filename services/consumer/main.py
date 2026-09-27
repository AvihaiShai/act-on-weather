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
import time
from datetime import datetime

import psycopg
import yaml

from ..common import coast, config, metrics, rules, schemas
from ..common.db import Pool
from ..common.envelope import Envelope
from ..common.outbox import Outbox
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
        # `AND retracted_at IS NULL` so a withdrawn sample is kept rather
        # than deleted and re-minted un-retracted on the next demo boot:
        # the re-accept uses a fresh per-process salt, so its envelope is
        # new and the original retraction is not replayed against it.
        cur.execute("DELETE FROM events WHERE is_sample AND retracted_at IS NULL")
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


# The three collected tables. Everything else is either derived (a
# recommendation), replaced day by day (weather) or the user's own (an
# itinerary), and migration 009 gave a `retracted_at` column to these three
# alone. Declared here rather than beside `apply_retraction` below because
# `upsert_by_id` is the first thing that needs it.
RETRACTABLE = {"events", "places", "facts"}


def apply_recorded_retraction(cur: psycopg.Cursor, table: str, row_id: str) -> None:
    """Mark a row that was withdrawn before this install held it (migration 010).

    A `record.retract` can arrive before the record it names, and often does:
    the curated list is applied on every boot against whatever the install has
    ingested, `POST /records/.../retract` publishes from the API's own outbox
    with no ordering relationship to the ingestor's, and a record that
    dead-letters is stored only after an operator redrives it. Before the
    ledger, such a withdrawal was spent the moment it committed -- its
    `message_id` was in `ingest_log`, so no redelivery could ever reach the
    handler again -- and the record appeared later, published, with nothing
    left to withdraw it.

    So the mark is derived from the ledger instead of only from the message.
    Called from `upsert_by_id`, which is the one and only INSERT into `events`,
    `places` and `facts` in this repository: put here rather than at the three
    handler call sites so that the property holds by construction and not by
    somebody remembering.

    `retracted_at IS NULL` keeps this from touching a row that already carries
    a withdrawal -- the ledger and the row can disagree on the wording while a
    correction is in flight, and the row's own handler is what settles that.
    """
    if table not in RETRACTABLE:
        return
    # `retraction_reason` leads the SET list so this statement does not read as
    # `UPDATE <table> SET retracted_at ...` like the other two writers of these
    # columns. They are told apart by their text in more than one test, and a
    # third statement that opens identically is a trap for the next reader.
    cur.execute(
        f"UPDATE {table} SET retraction_reason = r.retraction_reason,"
        "   retracted_by = r.retracted_by, retracted_at = r.retracted_at"
        " FROM record_retractions r"
        " WHERE r.entity = %(entity)s AND r.entity_id = %(id)s"
        f"   AND {table}.id = %(id)s AND {table}.retracted_at IS NULL",
        {"entity": table, "id": row_id},
    )
    if cur.rowcount:
        log.warning(
            "%s %s was withdrawn before this install held it; applying the "
            "recorded retraction on arrival",
            table,
            row_id,
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
    # A record that has just come into existence, or just been replaced by a
    # newer version, may already have been withdrawn -- by a `record.retract`
    # that arrived before it. Only reached when the statement above actually
    # wrote: a replay the as-of guard rejected changed nothing, so there is
    # nothing newly published to withdraw.
    if cur.rowcount:
        apply_recorded_retraction(cur, table, payload["id"])


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
    """A user asked for one activity by name (M2).

    Four outcomes. An activity the catalogue carries is scored by its own
    thresholds. An activity that needs a coast, asked for a city that has none,
    is refused rather than scored. An activity the catalogue does not carry but
    whose name says it happens in water is scored generically under the sea
    ceiling. Anything else is scored against general outdoor comfort.

    Whether a coast is needed is read from the catalogue when the catalogue has
    the activity and from the name when it does not -- see `needs_coast` below.
    Whatever is stored goes in `pending`, so the enricher words it like any
    other row, except a refusal, which is terminal.
    """
    cfg = ACTIVITIES.get(p.activity)
    # Whether this activity needs a coast, asked of the catalogue when the
    # catalogue holds the activity and of the slug's own words when it does not.
    #
    # The second half is the correction. `requires_coast` is a property of a row
    # in data/activities.yml, so this test used to be `cfg is not None and ...`
    # and every activity the catalogue does NOT carry fell past it -- including
    # the ones most obviously decided by the water. "scuba diving" asked for
    # London reached `rules.GENERIC_CFG`, which carries no `score_ceiling`, and
    # a pleasant day on land stored scuba diving in London as `good`, 100/100,
    # for a city with no coast on record. That is the same integrity breach the
    # catalogue branch below was written to close, reached by a name the
    # catalogue happens not to list.
    needs_coast = cfg.get("requires_coast") if cfg is not None else rules.names_water(p.activity)
    if needs_coast and not COASTAL.get(p.city_id, False):
        # The catalogue says this activity needs a coast and this city has none
        # on record, so there is nothing here to score it from. Storing no row
        # is not a new policy: it is the one the rest of the system already
        # states. `rules.activities_for_city` drops the activity instead of
        # scoring it, so the default path has never created such a row, and the
        # reader is built around that absence -- `router` collects an activity
        # with no row into `unscored_activities`, and the answer then says "no
        # suitability score on record; <city> has no coast on record", which is
        # a better answer than any number could be.
        #
        # What this replaces is worse than a missing row. Because this branch
        # shared an `else` with genuinely unknown activities, a request for
        # surfing in London was scored by `rules.GENERIC_CFG`, which carries no
        # `score_ceiling` -- so a pleasant day on land stored London surfing as
        # `good`, uncapped, contradicting both the 69 cap the four sea
        # activities carry and the sentence the reader is shown. Capping was
        # not an option either: `beach_day` needs a coast and has no ceiling of
        # its own, so there is no number to cap it to and inventing one is not
        # open to us.
        #
        # Writing a `failed` row instead of nothing was tried and is wrong:
        # `queries.recommendations` filters on neither status nor a null score,
        # so the row would come back as a scored one, `unscored_activities`
        # would go empty, and the answer would read "Surfing is None
        # (None/100)." in place of the coast sentence.
        #
        # This is decided before the forecast is read, because the reason does
        # not depend on the weather: there is no coast either way. Any row an
        # earlier build already stored here is stale data, and belongs to the
        # rebuild-and-rescore path, not to this one.
        log.info(
            "refused %s for %s: the activity needs a coast and the city has none on record",
            p.activity,
            p.city_id,
        )
        return

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
    #
    # The coast case has already returned above, so reaching here with
    # `cfg is None` and `needs_coast` true means one thing: a typed water
    # activity in a city that HAS a coast. It is scored against the same land
    # rules as any other typed activity -- there are no others to apply -- under
    # the ceiling the four catalogue sea activities carry, so it cannot climb
    # into the `good` band on the strength of a forecast that measures no water.
    if cfg is not None:
        result = rules.score_activity(p.activity, cfg, row)
    else:
        result = rules.score_requested(p.activity_label, row, sea=needs_coast)
    cur.execute(
        """
        INSERT INTO recommendations (city_id, forecast_date, activity, activity_label,
            requested, score, band, reasons, rule_version, status, weather_as_of)
        VALUES (%s, %s, %s, %s, true, %s, %s, %s, %s, 'pending', %s)
        ON CONFLICT (city_id, forecast_date, activity) DO UPDATE SET
            requested = true, score = EXCLUDED.score, band = EXCLUDED.band,
            reasons = EXCLUDED.reasons, weather_as_of = EXCLUDED.weather_as_of,
            -- The row is being scored again right now, so it carries the
            -- version of the engine that scored it. Leaving the old value here
            -- stamped a freshly computed score with a stale rule_version, which
            -- is the one thing a version-triggered rescore would have to trust.
            rule_version = EXCLUDED.rule_version,
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


def delete_itinerary(cur: psycopg.Cursor, p: schemas.ItineraryDelete) -> None:
    cur.execute("DELETE FROM itineraries WHERE id = %s", (p.id,))
    cur.execute(
        "DELETE FROM record_history WHERE entity = 'itineraries' AND entity_id = %s",
        (p.id,),
    )


HANDLERS[config.RK_ITINERARY_DELETE] = delete_itinerary


def apply_retraction(cur: psycopg.Cursor, p: schemas.RecordRetraction) -> None:
    """Withdraw one collected record from the published output (migration 009).

    An UPDATE, not a DELETE: the writer holds no DELETE grant on these tables
    and is not being given one. The row keeps its source, its as-of and its
    history, and the `_bump`/`_hist` triggers file the withdrawal as an
    ordinary revision, so `record_history` shows exactly when it happened.

    The decision date is never moved. `COALESCE` keeps the first
    `retracted_at` this row was given, so replaying the queue -- or a wipe --
    cannot restamp a withdrawal with the date of the replay. The reason and
    the author *can* be corrected, because getting the wording of a withdrawal
    right afterwards is a normal thing to need and the alternative is an
    operator editing the database by hand.

    A retraction naming a record this install has never held is not an error.
    The list is curated centrally and an install only has what it ingested; a
    retraction for a row in a city this deployment does not carry should do
    nothing rather than dead-letter the message. It is logged at warning
    because the other way to reach it is a typo in the curated list, and that
    should not be invisible.

    But "does nothing" used to mean "is lost". The decision is now written to
    `record_retractions` first, and unconditionally, so it outlives this
    delivery: `message_id` goes into `ingest_log` in the same transaction and
    no redelivery will ever reach this handler again, which made an early
    retraction a silent republish once its target finally arrived. The ledger
    is the durable half of the mechanism and the mark on the row is derived
    from it -- see migration 010 and `apply_recorded_retraction`.
    """
    if p.entity not in RETRACTABLE:
        raise Poison(f"cannot retract {p.entity}: only {sorted(RETRACTABLE)} are collected records")
    # The ledger first, because it is the half that has to survive. Its
    # conflict clause mirrors the row's exactly: `retracted_at` is absent from
    # the SET list, so the first decision date stands however often this is
    # replayed, and the reason and the author can still be corrected.
    cur.execute(
        "INSERT INTO record_retractions"
        " (entity, entity_id, retracted_at, retraction_reason, retracted_by)"
        " VALUES (%(entity)s, %(id)s, %(at)s, %(reason)s, %(by)s)"
        " ON CONFLICT (entity, entity_id) DO UPDATE SET"
        "   retraction_reason = EXCLUDED.retraction_reason,"
        "   retracted_by = EXCLUDED.retracted_by",
        {
            "entity": p.entity,
            "id": p.entity_id,
            "at": p.retracted_at,
            "reason": p.reason,
            "by": p.retracted_by,
        },
    )
    cur.execute(
        f"UPDATE {p.entity} SET retracted_at = COALESCE(retracted_at, %(at)s),"
        " retraction_reason = %(reason)s, retracted_by = %(by)s"
        " WHERE id = %(id)s AND (retracted_at IS NULL"
        "   OR retraction_reason IS DISTINCT FROM %(reason)s"
        "   OR retracted_by IS DISTINCT FROM %(by)s)",
        {"at": p.retracted_at, "reason": p.reason, "by": p.retracted_by, "id": p.entity_id},
    )
    if cur.rowcount:
        log.info("retracted %s %s: %s", p.entity, p.entity_id, p.reason)
    else:
        log.warning(
            "retraction for %s %s marked no row: no such row here, or already "
            "withdrawn for the same reason. The decision is recorded and will "
            "be applied if the record arrives",
            p.entity,
            p.entity_id,
        )


HANDLERS[config.RK_RETRACT] = apply_retraction


SOURCE_KEYS = (
    config.RK_WEATHER,
    config.RK_PLACE,
    config.RK_FACT,
    config.RK_EVENT,
    # A retraction is collected state, not user state: it must survive the
    # rebuild that `user_data.wipe` performs, or a wipe would silently
    # republish every record an operator has withdrawn. Replayed in a second
    # pass below, after the records themselves.
    config.RK_RETRACT,
)


def wipe_user_data(cur: psycopg.Cursor, _request: schemas.UserDataWipe) -> None:
    """Rebuild collected rows and scores from accepted ingestor messages.

    Replaying only committed source IDs preserves connected refreshes while
    removing manual patches, saved trips, visitor activities and re-wording.
    The source outbox is checked *before* deleting anything. A missing volume
    or envelope aborts the transaction rather than producing a partial wipe.
    """
    with Outbox("/source-outbox/outbox.sqlite3", readonly=True) as source_box:
        # Placeholders generated from the tuple rather than written out: the
        # literal `(?, ?, ?, ?)` this replaces silently went wrong the moment
        # a fifth source key was added.
        source_rows = source_box.conn.execute(
            "SELECT message_id, routing_key, body FROM outbox"
            f" WHERE routing_key IN ({', '.join('?' * len(SOURCE_KEYS))}) ORDER BY seq",
            SOURCE_KEYS,
        ).fetchall()

    committed = {
        row["message_id"]
        for row in cur.execute(
            "SELECT message_id FROM ingest_log" " WHERE routing_key = ANY(%s) AND source <> 'api'",
            (list(SOURCE_KEYS),),
        ).fetchall()
    }
    available = {row["message_id"] for row in source_rows}
    missing = committed - available
    if missing:
        raise RuntimeError(f"cannot wipe: {len(missing)} collected envelopes are missing")

    replay = []
    for row in source_rows:
        if row["message_id"] not in committed:
            continue
        envelope = Envelope.from_bytes(row["body"])
        if envelope.message_id != row["message_id"] or envelope.routing_key != row["routing_key"]:
            raise RuntimeError("cannot wipe: a collected envelope does not match its outbox row")
        replay.append(
            (envelope.routing_key, schemas.validate(envelope.routing_key, envelope.payload))
        )

    # Retractions are carried across the rebuild rather than re-derived from
    # it. Replaying the curated list restores every withdrawal that shipped
    # with the repository, but not one an operator made against this install
    # alone -- and a wipe that quietly republished a record somebody withdrew
    # would be the same F9 defect this mechanism exists to close, arriving by
    # a different route. Captured before the delete, re-applied after the
    # replay, so the origin of a retraction stops mattering.
    #
    # Since migration 010 the ledger covers this too, and covers it earlier:
    # pass 1 re-marks each row from `record_retractions` as it re-inserts it.
    # This is kept anyway, and is not redundant. The ledger requires a reason,
    # so a row marked in the database by hand without one is neither backfilled
    # into it nor re-marked from it, and this is the only thing that carries
    # such a row across. It does mean the count logged below is now what the
    # ledger had not already covered, which is the honest reading of "restored".
    held = {
        entity: cur.execute(
            f"SELECT id, retracted_at, retraction_reason, retracted_by FROM {entity}"
            " WHERE retracted_at IS NOT NULL"
        ).fetchall()
        for entity in ("events", "places", "facts")
    }

    cur.execute("SELECT wipe_business_rows()")

    # Two passes, records then retractions. A retraction is an UPDATE of a row
    # that must already be there, so replaying it in outbox order would depend
    # on the accept order of two different files. Splitting the pass makes the
    # rebuild order-independent by construction instead of by convention.
    records = [(rk, p) for rk, p in replay if rk != config.RK_RETRACT]
    retractions = [(rk, p) for rk, p in replay if rk == config.RK_RETRACT]

    for routing_key, payload in records:
        if routing_key == config.RK_WEATHER:
            if upsert_weather(cur, payload):
                score_defaults(cur, payload)
        else:
            HANDLERS[routing_key](cur, payload)

    for routing_key, payload in retractions:
        HANDLERS[routing_key](cur, payload)

    restored = 0
    for entity, rows in held.items():
        for row in rows:
            cur.execute(
                f"UPDATE {entity} SET retracted_at = %(at)s, retraction_reason = %(reason)s,"
                " retracted_by = %(by)s"
                " WHERE id = %(id)s AND retracted_at IS NULL",
                {
                    "at": row["retracted_at"],
                    "reason": row["retraction_reason"],
                    "by": row["retracted_by"],
                    "id": row["id"],
                },
            )
            restored += cur.rowcount

    cur.execute("DELETE FROM record_history")
    log.info(
        "wiped user data; restored %d collected messages and %d retractions",
        len(replay),
        restored,
    )


HANDLERS[config.RK_USER_DATA_WIPE] = wipe_user_data


# ----------------------------------------------------------------- handle ----


def process(routing_key: str, body: bytes, _message_id: str | None) -> str:
    """Store one delivery. Returns 'stored', or 'duplicate' if it was already in.

    Raises Poison for a message that can never succeed, and anything else for a
    failure worth retrying. `handle` below wraps this and is what the broker
    loop actually calls.
    """
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
                return "duplicate"

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
    return "stored"


def handle(routing_key: str, body: bytes, message_id: str | None) -> None:
    """`process`, with the outcome counted where the outcome is actually decided.

    The counting sits outside the transaction on purpose. `stored` is
    incremented only once `process` has returned, which is after
    `with conn.transaction()` committed -- a message counted on the way in would
    be claiming exactly the property M11 rests on, and would be wrong every time
    a commit failed. `rejected` is counted where Poison is raised, which is the
    dead-letter path and is terminal.

    A delivery that fails any other way is deliberately counted as nothing. It
    is going back on the queue and will arrive again, so counting it here would
    count one record several times over and make a database outage look like a
    flood of new work. Its effect is visible as queue depth instead, which comes
    from RabbitMQ's own exporter rather than from here -- one source of truth
    per fact.

    What remains uncounted is the gap the delivery semantics already have: the
    ack happens in `rabbit.consume` after this returns, so a crash in between
    counts one `stored` now and one `duplicate` on redelivery. That is
    at-least-once being honest about itself, and it is why `stored` is a
    throughput signal while `ingest_log` stays the record of what was written.
    """
    started = time.perf_counter()
    key = metrics.safe_routing_key(routing_key)
    result: str | None = None
    try:
        result = process(routing_key, body, message_id)
    except Poison:
        result = "rejected"
        raise
    finally:
        if result is not None:
            metrics.MESSAGES_CONSUMED.labels(routing_key=key, result=result).inc()
            metrics.MESSAGE_PROCESSING.labels(routing_key=key).observe(
                time.perf_counter() - started
            )
            metrics.CONSUMER_LAST_MESSAGE.set(time.time())


def main() -> None:
    # The consumer serves no HTTP of its own, so Prometheus gets an endpoint of
    # its own here. Started before the first database call on purpose: a
    # consumer waiting on Postgres is exactly the state worth being able to
    # scrape.
    metrics.start_metrics_server()
    conn = pool.conn
    seed_cities(conn)
    enforce_event_mode(conn)
    log.info("consuming %s", config.QUEUE)
    consume(handle, name="aow-consumer")


if __name__ == "__main__":
    main()
