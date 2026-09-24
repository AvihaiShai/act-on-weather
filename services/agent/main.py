"""The agent service (M7-M9).

One endpoint, `POST /ask`. It routes in code, retrieves from stored data, turns
the rows into typed facts (`grounding`), makes at most one LLM call, checks the
model's wording back against those facts, and appends a footer written in code.
A wording that claims more than the rows carry is discarded and the facts are
rendered directly instead -- the model phrases the answer, it does not decide
what is true.

The itinerary builder (`POST /itinerary`) is the same machinery with a
different renderer: it picks, per day in range, the best-scoring outdoor or
indoor activity from the stored recommendations and pairs it with places and
events that are actually on record. It never invents a stop.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from functools import lru_cache
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..common import coast, config, metrics, queries, rules
from ..common.db import Pool
from ..common.llm import LlmClient, LlmInvalidOutput, LlmUnavailable
from . import dates, grounding
from .planning import plan_day, venue_places
from .router import Retrieval, Router, footer, where_gap

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s agent %(message)s",
)
log = logging.getLogger("agent")

app = FastAPI(title="act-on-weather agent", version="1.0")

# Request metrics and the internal `/metrics` endpoint (B2). The agent has no
# outbox -- it never writes -- so it exports HTTP metrics only. Its latency
# histogram is the interesting one in this stack: a single `/ask` waits on a
# CPU llama.cpp call that the enricher is also queueing against, which is why
# the buckets in common/metrics.py run out to a minute.
metrics.mount_metrics(app, "agent")

pool = Pool(config.reader_dsn(), autocommit=True)
client = LlmClient()

# The prompt is the first line of defence and not the last one: whatever it
# says, `grounding.violations` re-checks the answer against the same rows and
# throws the wording away if it does not hold up.
SYSTEM = (
    "You answer travel and weather questions for a traveller, using ONLY the stored "
    "data you are given below the question. "
    "Hard rules: never state a fact, a place, an event or a number that is not in that "
    "data. If one specific thing that was asked for is missing, say so about that thing "
    "only -- never open with a blanket 'no record' when you have rows in front of you. "
    "The data is grouped by what kind of row it is, and the groups are not "
    "interchangeable. A PLACE is a building: you know its name and its category and "
    "nothing else, so never say a performance, a match, a meal or an exhibition is "
    "happening at one, and never describe what it is like, houses, serves or is known "
    "for. A SCHEDULED EVENT is the only thing that is on: keep it in its own category, "
    "because a tennis tournament is not a concert. "
    "When something asked about is absent, say it is not ON RECORD -- never that it "
    "is not scheduled, not happening or not taking place. The stored feed covers a "
    "few venues for a few weeks, so its silence says nothing about the city. "
    "When the data contains a suitability verdict for what was asked, state it: do not "
    "claim there is no record when a verdict is sitting in front of you. "
    "The reverse is just as strict: when the data says an activity is NOT ON RECORD, "
    "say exactly that about it and give no verdict, no weather-based reasoning and no "
    "substitute activity as though it answered the question. "
    "Never contradict a suitability verdict you are given. If a named activity "
    "has several days of scores, report every date, its band and its score out of "
    "100; do not give a single verdict for the whole range. Do not mention "
    "databases, rules or yourself. Do not add a data-freshness note and do not "
    "repeat the listed coverage gaps -- both are appended for you. "
    "Answer in at most six sentences of plain English."
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
            # Venues are counted apart from places: they are the rows the
            # `where` route stood behind as an answer, not context.
            "venues": sum(len(rows) for rows in result.venues.values()),
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

    # The `where` route, and it runs before the empty check: "I do not have a
    # verified surf spot for Tel Aviv" is the answer to that question, not a
    # failure to find rows. Rendered in code for the same reason the named
    # verdicts below are -- a model handed a list of beaches and asked where to
    # surf will offer one.
    if result.resolution.asks_where and result.resolution.activities:
        return respond(result, where_answer(result), llm_called=False)

    brief = grounding.build(result)

    if result.is_empty():
        # Nothing matched, but the question still said what it was looking for,
        # so name the gap rather than shrugging at it.
        missing = grounding.gap_block(brief)
        base = f"I have no stored records matching that for {result.resolution.city['name']}."
        return respond(result, f"{base} {missing}".strip(), llm_called=False)

    if result.resolution.activities:
        # The small CPU model repeatedly turns seven "fair" daily scores into
        # a "good week". Render named verdicts from their stored rows so every
        # date, band and score survives without an invented overall verdict.
        return respond(result, named_activity_answer(result), llm_called=False)

    try:
        parsed = client.chat_json(
            SYSTEM,
            f"Question: {body.question}\n\nStored data:\n{grounding.prompt_block(brief)}",
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
            grounding.render(brief),
            llm_called=False,
            note="The local model is unavailable, so this answer is "
            "rendered directly from the stored rows.",
        )
    except LlmInvalidOutput as exc:
        log.warning("llm output unusable: %s", exc)
        return respond(
            result,
            grounding.render(brief),
            llm_called=True,
            note="The local model returned unusable output, so this answer "
            "is rendered directly from the stored rows.",
        )

    # The wording is checked against the same typed facts it was written from.
    # This is the part a system prompt cannot do: the model does not get to
    # decide whether it stayed inside the data.
    broken = grounding.violations(answer, brief)
    if broken:
        log.warning("ungrounded answer rejected: %s", "; ".join(broken))
        return respond(
            result,
            grounding.render(brief),
            llm_called=True,
            note="The model's wording made a claim the stored rows do not support "
            f"({broken[0]}), so this answer is rendered directly from those rows.",
        )

    # Gaps are appended in code, after the model, for the same reason the as-of
    # footer is: a sentence the model cannot reword is the only kind that is
    # guaranteed to survive.
    missing = grounding.gap_block(brief, answer)
    return respond(result, f"{answer}\n\n{missing}" if missing else answer, llm_called=True)


def plain_answer(result: Retrieval) -> str:
    """The grounded answer: the same rows, formatted by code.

    It is deliberately plain. Its job is to prove that every fact in the pretty
    answer came from a row, to keep the system useful when `llm` is down, and
    to be what the traveller gets when the model's wording fails validation.
    """
    return grounding.render(grounding.build(result))


def where_answer(result: Retrieval) -> str:
    """The answer to "where can I surf in Tel Aviv?".

    A location question gets a location, or an explicit statement that the
    system holds none. What it must never get is a nearby row offered as
    though it were the answer: Tel Aviv has five beaches on record and not one
    of them is recorded as having rideable surf, so none of them is named here.
    The rule is the same one the trip planner follows -- only an activity
    declaring `place_categories` can be located -- and the retrieval that
    applies it is `Router.venues_for`.

    Weather appears only if the question also asked about timing or conditions,
    and it is labelled when it does: a suitability score rates the forecast,
    which for surfing is wind and rain, and not the sea.
    """
    city = result.resolution.city["name"]
    meta = _activity_meta()
    # Blocks, not lines: the UI renders the answer as markdown, and two
    # sentences on consecutive lines become one paragraph.
    blocks: list[str] = []

    for activity in result.resolution.activities:
        rows = result.venues.get(activity) or []
        label = (meta.get(activity) or {}).get("label", activity.replace("_", " "))
        if rows:
            lines = [f"Where to go in {city} for {label[:1].lower()}{label[1:]}:"]
            for row in rows:
                sample = " [sample data]" if row.get("is_sample") else ""
                lines.append(f"- {row['name']} ({row['category']}){sample}")
            blocks.append("\n".join(lines))
        else:
            blocks.append(where_gap(activity, city))

    if result.recommendations:
        # Only reached when the question asked about timing too. The heading
        # says what these are, so they cannot be read as an answer to "where".
        lines = [f"Stored suitability for {result.resolution.window}:"]
        for row in result.recommendations:
            lines.append(
                f"- {row['forecast_date']}: {row['activity_label']} is "
                f"{row['band']} ({row['score']}/100)."
            )
        lines.append("")
        lines.append(score_caveat(result))
        blocks.append("\n".join(lines))

    unscored = []
    for activity in result.unscored_activities:
        reason = ""
        if (meta.get(activity) or {}).get("requires_coast") and not result.resolution.city.get(
            "coastal"
        ):
            reason = f"; {city} has no coast on record"
        unscored.append(f"- {activity.replace('_', ' ')}: no suitability score on record{reason}.")
    if unscored:
        blocks.append("\n".join(unscored))

    return "\n\n".join(blocks)


def sea_state_caveat(result: Retrieval, *, lead: str = "These scores") -> str | None:
    """The sea-state caveat for this question, or None if it does not apply.

    It applies whenever the question named an activity carrying
    `sea_state_unmeasured` in data/activities.yml -- surfing, swimming,
    fishing, a boat ride. Those four are decided by the water, and this system
    ingests a land forecast and nothing else, so a score for them has to say
    what it is a score of. `coast.sea_state_caveat` names the city's forecast
    point and how far it sits from the coast reference in data/cities.yml,
    which for Rome is about 25 km.

    A beach day is deliberately not flagged and gets no caveat here: sun, heat,
    rain and wind are what make a day on the sand, and those are measured.
    """
    meta = _activity_meta()
    if not any(
        (meta.get(activity) or {}).get("sea_state_unmeasured")
        for activity in result.resolution.activities
    ):
        return None
    return coast.sea_state_caveat(result.resolution.city, lead=lead)


def score_caveat(result: Retrieval) -> str:
    """What a suitability score is, stated wherever one appears next to a
    location. The score is computed from the stored forecast -- temperature,
    rain, wind, sun -- so it rates the weather and not the venue, and for a
    sea-dependent activity it says nothing at all about the water. A reader
    comparing surf spots must not take it for a swell report.

    The sea half is asked for with the lead "They", because by then the first
    sentence has already named the scores and "These scores" twice over reads
    like two separate caveats."""
    base = "These scores rate the stored weather, not the place."
    sea = sea_state_caveat(result, lead="They")
    return f"{base} {sea}" if sea else base


def named_activity_answer(result: Retrieval) -> str:
    """The answer to "is it good for surfing in Tel Aviv tomorrow?".

    Rendered in code, not by the model, so every date keeps its own band and
    score. The caveat at the end is the half this route used to be missing:
    `where_answer` said what a coastal score does not cover and this one said
    nothing, so the question that asks for a verdict most directly -- naming
    the activity outright -- was the one answered with a bare number.
    """
    city = result.resolution.city["name"]
    lines = [f"Stored suitability for the activities you asked about in {city}:"]
    for row in result.recommendations:
        lines.append(
            f"- {row['forecast_date']}: {row['activity_label']} is "
            f"{row['band']} ({row['score']}/100)."
        )
    for activity in result.unscored_activities:
        reason = ""
        if _activity_meta().get(activity, {}).get(
            "requires_coast"
        ) and not result.resolution.city.get("coastal"):
            reason = f"; {city} has no coast on record"
        lines.append(f"- {activity.replace('_', ' ')}: no suitability score on record{reason}.")
    # Only when there is actually a score to qualify. An inland city has no
    # coastal row at all, and its answer is already the stronger statement --
    # "London has no coast on record" -- so following it with a note about what
    # its scores do not measure would be qualifying scores that do not exist.
    sea = sea_state_caveat(result, lead="They") if result.recommendations else ""
    if sea:
        # A blank line, because the UI renders this as markdown and a caveat on
        # the line after a list item would be read as part of the list.
        lines.append("")
        lines.append(sea)
    return "\n".join(lines)


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
    # A second, unfiltered read of the city. The interest-filtered list above
    # cannot answer "where is the beach" for a traveller who ticked only
    # "museums", and the day's activity is not something they chose per day.
    city_places = (
        places if not categories else queries.places(conn, body.city, categories=None, limit=200)
    )
    events = queries.events(conn, body.city, start=covered[0], end=covered[-1], limit=40)
    # Keyed by the city's local date, and a multi-day event is filed under
    # every day it runs (queries.event_days). Keying by `starts_at.date()` put
    # the Laver Cup, which opens at local midnight on the 25th, on the 24th and
    # showed it on none of its other two days -- F2.
    events_by_day: dict[str, list[dict[str, Any]]] = {}
    for row in events:
        for day in queries.event_days(row):
            events_by_day.setdefault(day.isoformat(), []).append(row)

    meta = _activity_meta()
    wanted_interests = {i.lower().replace(" ", "_") for i in body.interests}
    chosen_activities = {a.strip() for a in body.activities if a.strip()}
    if chosen_activities:
        unknown = chosen_activities - set(meta)
        if unknown:
            raise HTTPException(422, f"unknown activities: {', '.join(sorted(unknown))}")

    days = []
    used_places: set[str] = set()
    used_venues: set[str] = set()
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
        # Where the day's activity actually happens, when the sources name such
        # a place. Drawn from `city_places`, not the interest-filtered list, so
        # a beach day still names the beach for a traveller who only ticked
        # "museums" -- the day is the beach either way. Empty whenever the
        # activity declares no venue category or the city has no matching row,
        # and the UI renders that gap rather than papering over it.
        venues = venue_places(top["activity"] if top else None, meta, city_places, used_venues)
        used_venues.update(p["id"] for p in venues)
        # The same caveat the agent puts under a named-activity answer, carried
        # on the day that needs it. A plan is the one place a coastal score is
        # read as advice rather than as data, so a day whose recommendation --
        # or whose runners-up, which are rendered with their scores too --
        # depends on the sea says what the score behind it did not measure.
        # Computed here rather than in the UI because the wording belongs with
        # the data it qualifies.
        on_show = [s["activity"] for s in suggestions[:4]]
        caveat = (
            coast.sea_state_caveat(city)
            if any((meta.get(a) or {}).get("sea_state_unmeasured") for a in on_show)
            else None
        )
        # Rotate through the places so a five-day trip is not the same museum
        # five times. Deterministic, so the same request rebuilds the same plan.
        shown = used_places | {p["id"] for p in venues}
        picks = [p for p in places if p["id"] not in shown][:3]
        used_places.update(p["id"] for p in picks)
        if not picks:
            used_places.clear()
            picks = [p for p in places if p["id"] not in {v["id"] for v in venues}][:3]
        days.append(
            {
                "date": key,
                "activity": top["label"] if top else None,
                "activity_slug": top["activity"] if top else None,
                "activity_icon": top["icon"] if top else None,
                "activity_band": top["band"] if top else None,
                "activity_score": top["score"] if top else None,
                "why": top["why"] if top else None,
                # Null unless a sea-dependent activity is on show for this day.
                "activity_caveat": caveat,
                # The runners-up, so a day is a choice rather than a verdict.
                "alternatives": suggestions[1:4],
                # Venues for the day's activity, and what was looked for. The
                # second field is what lets the UI say "no beach on record"
                # instead of silently showing nothing.
                "activity_places": [
                    {
                        "id": p["id"],
                        "name": p["name"],
                        "category": p["category"],
                        "lat": p["lat"],
                        "lon": p["lon"],
                        "source_url": p["source_url"],
                        "is_sample": p["is_sample"],
                    }
                    for p in venues
                ],
                "activity_place_categories": (
                    (meta.get(top["activity"]) or {}).get("place_categories") or []
                )
                if top
                else [],
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
                # `starts_on`/`ends_on` travel with the row so the UI can say
                # "day 2 of 3" instead of repeating an undated line three
                # times, and so nothing downstream re-derives the day from the
                # UTC instant.
                "events": [
                    {
                        "id": e["id"],
                        "title": e["title"],
                        "category": e["category"],
                        "venue": e["venue"],
                        "starts_at": e["starts_at"].isoformat(),
                        "starts_on": e["starts_on"].isoformat(),
                        "ends_on": e["ends_on"].isoformat(),
                        "day_index": (day - e["starts_on"]).days + 1,
                        "day_count": (e["ends_on"] - e["starts_on"]).days + 1,
                        "source_url": e["source_url"],
                        "is_sample": e["is_sample"],
                        # The day somebody last opened this row's listing page.
                        # It travels with the event rather than only appearing
                        # in the coverage panel, because a saved itinerary is
                        # read later and on its own: a line saying a concert is
                        # on is a note of a web page, and how old that note is
                        # belongs next to it.
                        "checked_at": e["checked_at"].isoformat(),
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
