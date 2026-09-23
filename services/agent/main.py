"""The agent service (M7-M9).

One endpoint, `POST /ask`. It routes in code, retrieves from stored data, makes
at most one LLM call, and appends a footer written in code.

The itinerary builder (`POST /itinerary`) is the same machinery with a
different renderer: it picks, per day in range, the best-scoring outdoor or
indoor activity from the stored recommendations and pairs it with places and
events that are actually on record. It never invents a stop.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..common import config, queries
from ..common.db import Pool
from ..common.llm import LlmClient, LlmInvalidOutput, LlmUnavailable
from . import dates
from .router import Retrieval, Router, context_block, footer

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s agent %(message)s",
)
log = logging.getLogger("agent")

app = FastAPI(title="act-on-weather agent", version="1.0")

pool = Pool(config.reader_dsn(), autocommit=True)
client = LlmClient()

SYSTEM = (
    "You answer travel and weather questions for a traveller, using ONLY the stored "
    "data you are given below the question. "
    "Hard rules: never state a fact, a place, an event or a number that is not in that "
    "data. If one specific thing that was asked for is missing, say so about that thing "
    "only -- never open with a blanket 'no record' when you have rows in front of you. "
    "Each event and place is listed with its category in brackets -- keep it in that "
    "category and do not repurpose it: a tennis tournament is not a concert, and a "
    "concert hall is not a place to watch a sunset. "
    "You know a place's name and its category and nothing else, so never describe "
    "what it is like, what it is known for, or what it is good for. "
    "When the data contains a suitability verdict for what was asked, state it: do not "
    "claim there is no record when a verdict is sitting in front of you. "
    "Never contradict a suitability verdict you are given. Do not mention scores out of "
    "100, databases, rules or yourself. Do not add a data-freshness note -- one is "
    "appended for you. Answer in at most six sentences of plain English."
)

SCHEMA = {
    "type": "object",
    # maxLength is load-bearing, not decoration: without it the model happily
    # writes past `max_tokens`, the completion is cut mid-string, and what comes
    # back is unparseable JSON. The grammar stops it while the object can still
    # be closed.
    "properties": {"answer": {"type": "string", "maxLength": 900}},
    "required": ["answer"],
    "additionalProperties": False,
}


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=500)


def respond(result: Retrieval, answer: str, *, llm_called: bool, note: str | None = None):
    payload: dict[str, Any] = {
        "answer": answer,
        "as_of": footer(result),
        "city": result.resolution.city["id"] if result.resolution.city else None,
        "dates": str(result.resolution.window) if result.resolution.window else None,
        "intents": result.resolution.intents,
        "llm_called": llm_called,
        "rows_used": {
            "forecast": len(result.forecast),
            "recommendations": len(result.recommendations),
            "places": len(result.places),
            "events": len(result.events),
            "facts": len(result.facts),
        },
    }
    if note:
        payload["note"] = note
    return payload


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        pool.conn.execute("SELECT 1")
        db = True
    except Exception:  # noqa: BLE001
        pool.drop()
        db = False
    return {"status": "ok" if db else "degraded", "database": db, "llm": client.healthy()}


@app.post("/ask")
def ask(body: AskIn) -> dict[str, Any]:
    router = Router(pool.conn)
    result = router.retrieve(body.question)

    # Out of coverage, or an unknown city: answered from a template, in code.
    # No LLM call is made at all -- there is nothing for it to phrase, and a
    # model asked to phrase "no data" is a model given the chance to invent it.
    if result.refusal:
        return respond(result, result.refusal, llm_called=False)

    if result.is_empty():
        return respond(
            result,
            f"I have no stored records matching that for {result.resolution.city['name']}.",
            llm_called=False,
        )

    try:
        parsed = client.chat_json(
            SYSTEM,
            f"Question: {body.question}\n\nStored data:\n{context_block(result)}",
            SCHEMA,
            max_tokens=700,
        )
        answer = str(parsed.get("answer", "")).strip()
        if len(answer) < 10:
            raise LlmInvalidOutput("answer too short")
    except LlmUnavailable as exc:
        # The model is the phrasing layer, not the source of truth, so its
        # absence degrades the answer rather than failing the request.
        log.warning("llm unavailable: %s", exc)
        return respond(
            result,
            plain_answer(result),
            llm_called=False,
            note="The local model is unavailable, so this answer is "
            "rendered directly from the stored rows.",
        )
    except LlmInvalidOutput as exc:
        log.warning("llm output unusable: %s", exc)
        return respond(
            result,
            plain_answer(result),
            llm_called=True,
            note="The local model returned unusable output, so this answer "
            "is rendered directly from the stored rows.",
        )

    return respond(result, answer, llm_called=True)


def plain_answer(result: Retrieval) -> str:
    """The fallback renderer: the same rows, formatted by code.

    It is deliberately plain. Its job is to prove that every fact in the pretty
    answer came from a row, and to keep the system useful when `llm` is down.
    """
    lines = [f"{result.resolution.city['name']}, {result.resolution.window}:"]
    for row in result.forecast[:8]:
        lines.append(
            f"- {row['forecast_date']}: high {row['temp_max_c']:.0f}C, "
            f"low {row['temp_min_c']:.0f}C, rain {row['precip_mm']:.1f}mm "
            f"({row['precip_prob']}%), wind {row['wind_kmh']:.0f}km/h"
        )
    best: dict[str, tuple[str, int]] = {}
    for row in result.recommendations:
        day = str(row["forecast_date"])
        if row["score"] is not None and row["score"] > best.get(day, ("", -1))[1]:
            best[day] = (row["activity_label"], row["score"])
    for day, (activity, score) in sorted(best.items())[:8]:
        lines.append(f"- {day}: best rated activity is {activity} ({score}/100)")
    for row in result.places[:10]:
        lines.append(f"- {row['name']} ({row['category']})")
    for row in result.events[:10]:
        lines.append(f"- {row['starts_at'].date()} {row['title']} ({row['category']})")
    for row in result.facts[:2]:
        lines.append(f"- {row['title']}: {row['summary'][:300]}")
    return "\n".join(lines)


# ------------------------------------------------------------- itinerary ----


class ItineraryIn(BaseModel):
    city: str
    start_date: date | None = None
    end_date: date | None = None
    interests: list[str] = Field(default_factory=list)


@app.post("/itinerary")
def build_itinerary(body: ItineraryIn) -> dict[str, Any]:
    """Assemble a day-by-day plan from stored rows only (M9).

    Returned, not saved. The UI saves it with `POST /itineraries`, which goes
    through the outbox and the queue like every other write.
    """
    conn = pool.conn
    cities = {c["id"]: c for c in queries.cities(conn)}
    if body.city not in cities:
        raise HTTPException(404, f"unknown city: {body.city}")
    city = cities[body.city]

    coverage = queries.coverage(conn)
    start = body.start_date or dates.today_in(city["timezone"])
    end = body.end_date or start
    covered = [
        d for d in dates.DateRange(start, end, "").days() if queries.in_coverage(coverage, d)
    ]
    if not covered:
        raise HTTPException(
            422,
            f"no stored weather for {start} to {end}; the forecast covers "
            f"{coverage['weather_first_date']} to {coverage['weather_last_date']}",
        )

    router = Router(conn)
    categories: list[str] = []
    for interest in body.interests:
        categories.extend(router.interests.get(interest.lower().replace(" ", "_"), []))
    categories = sorted(set(categories))

    recommendations = queries.recommendations(conn, body.city, start=covered[0], end=covered[-1])
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in recommendations:
        by_day.setdefault(str(row["forecast_date"]), []).append(row)

    places = queries.places(conn, body.city, categories=categories or None, limit=60)
    events = queries.events(conn, body.city, start=covered[0], end=covered[-1], limit=40)
    events_by_day: dict[str, list[dict[str, Any]]] = {}
    for row in events:
        events_by_day.setdefault(str(row["starts_at"].date()), []).append(row)

    days = []
    used: set[str] = set()
    for _index, day in enumerate(covered):
        key = day.isoformat()
        ranked = sorted(by_day.get(key, []), key=lambda r: r["score"] or 0, reverse=True)
        top = ranked[0] if ranked else None
        # Rotate through the places so a five-day trip is not the same museum
        # five times. Deterministic, so the same request rebuilds the same plan.
        picks = [p for p in places if p["id"] not in used][:3]
        used.update(p["id"] for p in picks)
        if not picks:
            used.clear()
            picks = places[:3]
        days.append(
            {
                "date": key,
                "activity": top["activity_label"] if top else None,
                "activity_band": top["band"] if top else None,
                "activity_score": top["score"] if top else None,
                "why": (top.get("text") or "; ".join(top.get("reasons") or [])) if top else None,
                "places": [
                    {
                        "id": p["id"],
                        "name": p["name"],
                        "category": p["category"],
                        "lat": p["lat"],
                        "lon": p["lon"],
                        "source_url": p["source_url"],
                        "is_sample": p["is_sample"],
                    }
                    for p in picks
                ],
                "events": [
                    {
                        "id": e["id"],
                        "title": e["title"],
                        "category": e["category"],
                        "venue": e["venue"],
                        "starts_at": e["starts_at"].isoformat(),
                        "source_url": e["source_url"],
                        "is_sample": e["is_sample"],
                    }
                    for e in events_by_day.get(key, [])
                ],
            }
        )

    return {
        "city": body.city,
        "city_name": city["name"],
        "title": f"{len(days)} days in {city['name']}",
        "start_date": covered[0].isoformat(),
        "end_date": covered[-1].isoformat(),
        "days": days,
        "as_of": coverage.get("weather_as_of").isoformat()
        if coverage.get("weather_as_of") and not isinstance(coverage.get("weather_as_of"), str)
        else coverage.get("weather_as_of"),
        "coverage": {
            "first": coverage["weather_first_date"],
            "last": coverage["weather_last_date"],
        },
        "requested_days_outside_coverage": [
            d.isoformat() for d in dates.DateRange(start, end, "").days() if d not in covered
        ],
    }
