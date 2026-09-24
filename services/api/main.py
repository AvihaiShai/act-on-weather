"""The read API, and the write path that never writes.

Reads go straight to Postgres as `aow_reader`, through `common.queries` -- the
same functions the agent uses, so a number in the UI and a number in an answer
cannot disagree.

Writes do not touch the database at all. A POST or PATCH is fsynced into this
service's own outbox and answered `202 Accepted` with the `message_id`; a
background thread drains the outbox to RabbitMQ, and the consumer stores it.
That is M4 taken literally -- user edits travel the same path as collected data
-- and it is why `GET /outbox/{message_id}` exists: it is how a caller, or a
failure drill, follows one accepted record to its terminal state.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..common import config, queries, refresh_state, schemas
from ..common.db import Pool
from ..common.envelope import Envelope
from ..common.outbox import Outbox
from ..common.rabbit import Publisher, PublishError

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s api %(message)s",
)
log = logging.getLogger("api")

app = FastAPI(title="act-on-weather", version="1.0", docs_url="/docs")

pool = Pool(config.reader_dsn(), autocommit=True)
outbox = Outbox(config.OUTBOX_PATH)
_outbox_lock = threading.Lock()


# ------------------------------------------------------- publisher thread ----


def _publisher_loop() -> None:
    publisher = Publisher(name="aow-api")
    while True:
        try:
            with _outbox_lock:
                rows = list(outbox.unpublished(100))
            for row in rows:
                try:
                    publisher.publish(row["routing_key"], row["body"], row["message_id"])
                except PublishError as exc:
                    with _outbox_lock:
                        outbox.mark_failed(row["seq"], str(exc))
                    log.warning("publish failed for %s: %s", row["message_id"], exc)
                    publisher.close()
                    break
                with _outbox_lock:
                    outbox.mark_published(row["seq"])
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            log.exception("publisher loop error: %s", exc)
        time.sleep(2)


@app.on_event("startup")
def _start_publisher() -> None:
    threading.Thread(target=_publisher_loop, name="outbox-publisher", daemon=True).start()


@lru_cache(maxsize=1)
def _activity_meta() -> dict[str, dict[str, Any]]:
    """Presentation metadata for the activity catalogue -- icon, indoor flag,
    interest tags -- read from the same data/activities.yml the rule engine
    scores from, so the two cannot describe different activities."""
    from ..common import rules

    _version, activities = rules.load_activities(config.DATA_DIR / "activities.yml")
    return activities


def accept(routing_key: str, payload: dict[str, Any], city: str | None = None) -> str:
    """Validate, then durably accept. Returns the message_id the caller can follow."""
    try:
        schemas.validate(routing_key, payload)
    except Exception as exc:  # noqa: BLE001 - a bad request must not become a dead letter
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    envelope = Envelope.create(
        routing_key,
        payload,
        source="api",
        observed_at=datetime.now(UTC),
        city=city,
    )
    with _outbox_lock:
        outbox.accept(envelope)
    return envelope.message_id


# ------------------------------------------------------------------ reads ----


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        pool.conn.execute("SELECT 1")
        db_ok = True
    except Exception:  # noqa: BLE001
        pool.drop()
        db_ok = False
    with _outbox_lock:
        counts = outbox.counts()
    return {"status": "ok" if db_ok else "degraded", "database": db_ok, "outbox": counts}


@app.get("/coverage")
def get_coverage() -> dict[str, Any]:
    """What the system holds, and how old it is.

    Every page and every answer is stamped from this. It is also what makes a
    question about a date outside the window answerable with "no data" rather
    than with a guess.
    """
    return queries.coverage(pool.conn)


@app.get("/cities")
def get_cities() -> list[dict[str, Any]]:
    return queries.cities(pool.conn)


@app.get("/activities")
def get_activities(city: str | None = None) -> list[dict[str, Any]]:
    """The activity catalogue, as the rule engine actually scored it.

    Read from `recommendations`, not from data/activities.yml, so what the UI
    offers is exactly what has a stored score behind it. Filtering by city is
    what makes the coastal gate visible: Tel Aviv lists surfing, London does
    not, because London has no surfing row to list.
    """
    meta = _activity_meta()
    out = []
    for row in queries.activity_catalogue(pool.conn, city):
        cfg = meta.get(row["activity"]) or {}
        out.append(
            {
                **row,
                "icon": cfg.get("icon"),
                "indoor": bool(cfg.get("indoor")),
                "interests": cfg.get("interests") or [],
                "requires_coast": bool(cfg.get("requires_coast")),
                # No block in activities.yml means a user typed this one in on
                # the Suitability page; it was scored against general outdoor
                # comfort, and saying so is the point.
                "in_catalogue": row["activity"] in meta,
            }
        )
    return out


@app.get("/weather/{city}")
def get_weather(
    city: str,
    start: date | None = None,
    end: date | None = None,
) -> list[dict[str, Any]]:
    rows = queries.forecast(pool.conn, city, start=start, end=end)
    if not rows:
        raise HTTPException(404, f"no stored weather for {city}")
    return rows


@app.get("/scores")
def get_scores(
    city: str | None = None,
    start: date | None = None,
    end: date | None = None,
    activity: str | None = None,
) -> list[dict[str, Any]]:
    """The heatmap's source: city x day x activity suitability."""
    return queries.recommendations(pool.conn, city, start=start, end=end, activity=activity)


@app.get("/recommendations/{city}")
def get_recommendations(
    city: str,
    start: date | None = None,
    end: date | None = None,
) -> list[dict[str, Any]]:
    return queries.recommendations(pool.conn, city, start=start, end=end)


@app.get("/places")
def get_places(
    city: str | None = None,
    category: list[str] | None = Query(default=None),
    limit: int = 50,
) -> list[dict[str, Any]]:
    return queries.places(pool.conn, city, categories=category, limit=limit)


@app.get("/events")
def get_events(
    city: str | None = None,
    start: date | None = None,
    end: date | None = None,
    category: str | None = None,
    include_expired: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Scheduled listings, current by default.

    Every row was read off its own `source_url` at `checked_at` and stops being
    offered as a schedule at `valid_until` (migration 006). `include_expired`
    is the operator's view: it returns the stale readings too, each carrying
    `is_current`, so that "the feed has gone out of date" can be told apart
    from "there was never anything here". A caller that renders these rows to a
    traveller should leave it alone.
    """
    return queries.events(
        pool.conn,
        city,
        start=start,
        end=end,
        category=category,
        include_expired=include_expired,
        limit=limit,
    )


@app.get("/facts")
def get_facts(city: str | None = None, topic: str | None = None, limit: int = 20):
    return queries.facts(pool.conn, city, topic=topic, limit=limit)


@app.get("/itineraries")
def list_itineraries(city: str | None = None) -> list[dict[str, Any]]:
    return queries.itineraries(pool.conn, city)


@app.get("/itineraries/{itinerary_id}")
def get_itinerary(itinerary_id: str) -> dict[str, Any]:
    row = queries.itinerary(pool.conn, itinerary_id)
    if row is None:
        raise HTTPException(404, "no such itinerary")
    return row


@app.get("/records/{entity}/{entity_id}/history")
def get_history(entity: str, entity_id: str) -> list[dict[str, Any]]:
    if entity not in {"places", "facts", "events", "itineraries", "weather_daily"}:
        raise HTTPException(404, "unknown entity")
    return queries.history(pool.conn, entity, entity_id)


@app.get("/outbox/{message_id}")
def outbox_status(message_id: str) -> dict[str, Any]:
    """Follow one accepted record. Used by the M11 drills and by the UI's
    'your edit is on its way' state."""
    with _outbox_lock:
        row = outbox.status_of(message_id)
    if row is None:
        raise HTTPException(404, "this message was never accepted here")
    result = dict(row)
    stored = pool.conn.execute(
        "SELECT routing_key, processed_at FROM ingest_log WHERE message_id = %s",
        (message_id,),
    ).fetchone()
    result["stored"] = stored is not None
    result["stored_at"] = stored["processed_at"] if stored else None
    return result


@app.get("/refresh/last")
def refresh_last() -> dict[str, Any]:
    """What the last operator refresh actually did (M12, F4).

    A read of one JSON file on a volume this service mounts read-only, filed by
    `scripts/refresh.sh` on its way out. It is deliberately not a row: a refresh
    whose provider refused every city accepts no messages, so the queue has
    nothing to carry and the consumer has nothing to store -- see
    services/common/refresh_state.py.

    `recorded: false` rather than a 404, because "no refresh has been recorded on
    this stack" is the normal state of a fresh install and the UI has something
    to say about it. There is no matching write route: the only way to record a
    run is to run the command on the host.
    """
    report = refresh_state.read()
    if report is None:
        return {"recorded": False}
    return {"recorded": True, "report": report}


# ----------------------------------------------------------------- writes ----


class RequestIn(BaseModel):
    """The base every request body inherits.

    `extra="forbid"` because pydantic's default is to drop a field it does not
    recognise: a misspelled or unsupported key then returns 200 having been
    ignored, which is worse than a refusal. `{"days": 2}` on an itinerary
    request is the concrete case -- the plan's length comes from the date
    range, so the field was silently doing nothing.
    """

    model_config = ConfigDict(extra="forbid")


class RecommendationRequestIn(RequestIn):
    city: str
    forecast_date: date
    activity: str = Field(min_length=2, max_length=80)


@app.post("/recommendations", status_code=202)
def request_recommendation(body: RecommendationRequestIn) -> dict[str, Any]:
    """Ask for an activity that is not one of the scored defaults (M2).

    Accepted, not answered: the request goes through the queue, the consumer
    scores it against the stored weather, and the enricher words it. Poll
    `GET /recommendations/{city}` for the result.
    """
    try:
        slug = schemas.slugify(body.activity)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    message_id = accept(
        config.RK_RECOMMENDATION_REQUEST,
        {
            "city_id": body.city,
            "forecast_date": body.forecast_date.isoformat(),
            "activity": slug,
            "activity_label": body.activity.strip(),
        },
        city=body.city,
    )
    return {
        "accepted": True,
        "message_id": message_id,
        "activity": slug,
        "poll": f"/recommendations/{body.city}",
    }


class ReenrichIn(RequestIn):
    city: str | None = None
    forecast_date: date | None = None
    activity: str | None = None
    include_deferred: bool = False


@app.post("/reenrich", status_code=202)
def reenrich(body: ReenrichIn) -> dict[str, Any]:
    """M12's third update path: re-word stored recommendations.

    This is the update that needs no connectivity. It resets the wording, not
    the score -- the score came from the rule engine and only a weather
    refresh changes it -- and the enricher picks the rows up on its next poll.
    Set `include_deferred` to pull in activities the consumer ranked out of
    the wording queue for that day.
    """
    message_id = accept(
        config.RK_REENRICH,
        {
            "city_id": body.city,
            "forecast_date": body.forecast_date.isoformat() if body.forecast_date else None,
            "activity": body.activity,
            "include_deferred": body.include_deferred,
            "requested_by": "api",
        },
        city=body.city,
    )
    return {"accepted": True, "message_id": message_id, "follow": f"/outbox/{message_id}"}


@app.get("/enrichment")
def enrichment_status(city: str | None = None) -> dict[str, Any]:
    """How far the local model has got through the wording queue.

    Surfaced because with 18 activities the queue is long enough to be worth
    watching, and because `deferred` needs explaining: those rows are scored
    and charted, they were simply never sent to the model.
    """
    rows = pool.conn.execute(
        "SELECT status, COUNT(*) AS rows FROM recommendations"
        " WHERE (%(city)s::text IS NULL OR city_id = %(city)s::text)"
        " GROUP BY status ORDER BY status",
        {"city": city},
    ).fetchall()
    counts = {r["status"]: r["rows"] for r in rows}
    return {
        "counts": counts,
        "total": sum(counts.values()),
        "top_n_worded_per_day": config.ENRICH_TOP_N,
    }


class ItineraryIn(RequestIn):
    city: str
    title: str
    start_date: date
    end_date: date
    days: list[dict[str, Any]] = Field(default_factory=list)


@app.post("/itineraries", status_code=202)
def save_itinerary(body: ItineraryIn) -> dict[str, Any]:
    itinerary_id = str(uuid.uuid4())
    cov = queries.coverage(pool.conn)
    message_id = accept(
        config.RK_ITINERARY,
        {
            "id": itinerary_id,
            "city_id": body.city,
            "title": body.title,
            "start_date": body.start_date.isoformat(),
            "end_date": body.end_date.isoformat(),
            "days": body.days,
            "as_of": (cov.get("weather_as_of") or datetime.now(UTC)).isoformat()
            if not isinstance(cov.get("weather_as_of"), str)
            else cov["weather_as_of"],
        },
        city=body.city,
    )
    return {"accepted": True, "message_id": message_id, "id": itinerary_id}


@app.patch("/records/{entity}/{entity_id}", status_code=202)
def patch_record(
    entity: str,
    entity_id: str,
    fields: dict[str, Any] = Body(..., embed=False),
) -> dict[str, Any]:
    """M12: correct a stored record.

    It is accepted here and applied by the consumer, so a user edit is
    delivered, retried and idempotent exactly like a fetched record. The
    consumer bumps the revision and files the before/after into record_history.

    `fields` is the one body deliberately left open. It is a column map over
    five entities, so the allowed keys live with the consumer's `PATCHABLE`
    allow-list, next to the UPDATE that uses them; an unlisted column is a
    poison message, not a silent no-op. Every other request body forbids
    extras (see `RequestIn`).
    """
    message_id = accept(
        config.RK_PATCH,
        {"entity": entity, "entity_id": entity_id, "fields": fields, "edited_by": "api"},
    )
    return {
        "accepted": True,
        "message_id": message_id,
        "follow": f"/outbox/{message_id}",
        "history": f"/records/{entity}/{entity_id}/history",
    }


# ------------------------------------------------------------------ agent ----


class AskIn(RequestIn):
    question: str = Field(min_length=2, max_length=500)


def _agent(path: str, payload: dict[str, Any]) -> JSONResponse:
    """Proxied to the agent service, which holds the only LLM client on the read
    path. Kept here so the UI and the demo scripts have one base URL."""
    import requests

    try:
        response = requests.post(
            f"{os.environ.get('AGENT_BASE', 'http://agent:8100')}{path}",
            json=payload,
            timeout=float(os.environ.get("AGENT_TIMEOUT_S", "180")),
        )
    except requests.RequestException as exc:
        raise HTTPException(503, f"agent unavailable: {exc}") from exc
    return JSONResponse(status_code=response.status_code, content=response.json())


@app.post("/agent/ask")
def ask(body: AskIn) -> JSONResponse:
    return _agent("/ask", {"question": body.question})


class ItineraryRequestIn(RequestIn):
    city: str
    start_date: date | None = None
    end_date: date | None = None
    interests: list[str] = Field(default_factory=list)
    activities: list[str] = Field(default_factory=list)
    pace: str = Field(default="varied", pattern="^(varied|best)$")


@app.post("/agent/itinerary")
def build_itinerary(body: ItineraryRequestIn) -> JSONResponse:
    """Assemble a plan from stored rows. Returned, not saved -- saving goes
    through `POST /itineraries`, and therefore through the queue."""
    return _agent(
        "/itinerary",
        {
            "city": body.city,
            "start_date": body.start_date.isoformat() if body.start_date else None,
            "end_date": body.end_date.isoformat() if body.end_date else None,
            "interests": body.interests,
            "activities": body.activities,
            "pace": body.pace,
        },
    )
