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
import re
from datetime import date
from functools import lru_cache
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..common import config, queries, rules
from ..common.db import Pool
from ..common.llm import LlmClient, LlmInvalidOutput, LlmUnavailable
from . import dates
from .planning import plan_day
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
    "The reverse is just as strict: when the data says an activity is NOT ON RECORD, "
    "say exactly that about it and give no verdict, no weather-based reasoning and no "
    "substitute activity as though it answered the question. "
    "Never contradict a suitability verdict you are given. If a named activity "
    "has several days of scores, report every date, its band and its score out of "
    "100; do not give a single verdict for the whole range. Do not mention "
    "databases, rules or yourself. Do not add a data-freshness note -- one is "
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
        if not named_verdicts_present(answer, result):
            raise LlmInvalidOutput("named activity verdicts missing or inconsistent")
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
    if result.resolution.activities:
        for row in result.recommendations:
            lines.append(
                f"- {row['forecast_date']}: {row['activity_label']} is "
                f"{row['band']} ({row['score']}/100)"
            )
        for activity in result.unscored_activities:
            lines.append(f"- {activity.replace('_', ' ')}: no suitability score on record")
    else:
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


def named_verdicts_present(answer: str, result: Retrieval) -> bool:
    """Accept model wording only if every named score survives in the answer.

    A missing date or a changed band/score is more harmful than plain wording.
    Split at ISO dates so a band from a different day cannot satisfy the check.
    """
    if not result.resolution.activities or not result.recommendations:
        return True
    chunks = re.split(r"(?=\b\d{4}-\d{2}-\d{2}\b)", answer.lower())
    expected_by_day: dict[str, set[str]] = {}
    for row in result.recommendations:
        expected_by_day.setdefault(str(row["forecast_date"]), set()).add(row["band"])
    if re.search(r"\b(good|fair|poor)\b", chunks[0]):
        return False
    for chunk in chunks[1:]:
        day = chunk[:10]
        mentioned_bands = set(re.findall(r"\b(good|fair|poor)\b", chunk))
        if not mentioned_bands <= expected_by_day.get(day, set()):
            return False
    for row in result.recommendations:
        day = str(row["forecast_date"])
        matching = [chunk for chunk in chunks if chunk.startswith(day)]
        if not any(
            row["activity_label"].lower() in chunk
            and re.search(rf"\b{re.escape(str(row['band']).lower())}\b", chunk)
            and re.search(rf"\b{row['score']}\s*/\s*100\b", chunk)
            for chunk in matching
        ):
            return False
    return not (
        len(expected_by_day) > 1
        and re.search(
            r"\b(?:good|fair|poor)\s+(?:week|period|trip)\b|"
            r"\b(?:week|period|trip)\s+(?:is|looks|will be)\s+(?:good|fair|poor)\b",
            answer.lower(),
        )
    )


# ------------------------------------------------------------- itinerary ----


class ItineraryIn(BaseModel):
    city: str
    start_date: date | None = None
    end_date: date | None = None
    interests: list[str] = Field(default_factory=list)
    # Activity slugs the traveller picked explicitly. When set, the plan is
    # built only from these -- it is a filter, not a hint.
    activities: list[str] = Field(default_factory=list)
    pace: str = Field(default="varied", pattern="^(varied|best)$")


@lru_cache(maxsize=1)
def _activity_meta() -> dict[str, dict[str, Any]]:
    """Icon, indoor flag and interest tags per activity, from the same
    data/activities.yml the rule engine scores from. Read once: the file is
    baked into the image, so it cannot change under a running container."""
    _version, activities = rules.load_activities(config.DATA_DIR / "activities.yml")
    return activities


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

    meta = _activity_meta()
    wanted_interests = {i.lower().replace(" ", "_") for i in body.interests}
    chosen_activities = {a.strip() for a in body.activities if a.strip()}
    if chosen_activities:
        unknown = chosen_activities - set(meta)
        if unknown:
            raise HTTPException(422, f"unknown activities: {', '.join(sorted(unknown))}")

    days = []
    used_places: set[str] = set()
    used_activities: dict[str, int] = {}
    for day in covered:
        key = day.isoformat()
        rows = by_day.get(key, [])
        if chosen_activities:
            rows = [r for r in rows if r["activity"] in chosen_activities]
        suggestions = plan_day(
            rows,
            meta,
            wanted_interests,
            used_activities,
            varied=body.pace == "varied",
        )
        top = suggestions[0] if suggestions else None
        if top:
            used_activities[top["activity"]] = used_activities.get(top["activity"], 0) + 1
        # Rotate through the places so a five-day trip is not the same museum
        # five times. Deterministic, so the same request rebuilds the same plan.
        picks = [p for p in places if p["id"] not in used_places][:3]
        used_places.update(p["id"] for p in picks)
        if not picks:
            used_places.clear()
            picks = places[:3]
        days.append(
            {
                "date": key,
                "activity": top["label"] if top else None,
                "activity_slug": top["activity"] if top else None,
                "activity_icon": top["icon"] if top else None,
                "activity_band": top["band"] if top else None,
                "activity_score": top["score"] if top else None,
                "why": top["why"] if top else None,
                # The runners-up, so a day is a choice rather than a verdict.
                "alternatives": suggestions[1:4],
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
        "title": f"{len(days)} {'day' if len(days) == 1 else 'days'} in {city['name']}",
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
