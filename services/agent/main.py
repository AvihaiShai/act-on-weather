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
from dataclasses import replace
from datetime import date, datetime
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

    # `unknown_activities` reaches the code-rendered route only when the question
    # is ABOUT the activity and about nothing else. Three exclusions, all found by
    # asking the questions rather than by reading the branch:
    #
    #   `asks_where` -- "where can I go kite surfing in Tel Aviv?" asks for a
    #   place. Answering it with a heading about suitability scores answers a
    #   question nobody asked and throws away the places that were retrieved.
    #   It falls through, as it did before.
    #
    #   "weather" in intents -- "what is the weather tomorrow in London, and is
    #   it good for stargazing?" asks two things. `named_activity_answer` renders
    #   recommendations and gaps and never touches `result.forecast`, so the
    #   forecast row was fetched and silently discarded, and the user was told
    #   only that stargazing is not on record. The model still answers that one,
    #   with the gap in its brief and grounding check 5 armed on the noun.
    #
    #   `other_subjects` -- the same defect one intent further out, and the one
    #   the two above did not cover. `named_activity_answer` does not touch
    #   `result.events`, `result.places` or `result.facts` either, so "what events
    #   are on tomorrow in Rome, and is it a good day for a bbq?" answered the
    #   bbq and dropped the event -- while `rows_used` went on reporting the row
    #   it had thrown away. `categories` is in the test as well as the intents,
    #   because an interest word reaches the places query on its own:
    #   `Router.retrieve` reads `"places" in intents or resolution.categories`.
    #
    # A resolved activity is unaffected, and deliberately so: `named_activity_answer`
    # has always rendered those from their own rows, and it is the only thing
    # standing between a reviewer and a 1.7B model summarising seven `fair` days
    # as a good week. A mixed question naming a CATALOGUE activity still loses its
    # events the same way, which is behaviour this branch inherited rather than
    # introduced; it is recorded in the README's known limitations rather than
    # changed here, because widening this route is a different argument from
    # closing the regression above.
    other_subjects = bool({"events", "places", "facts"} & set(result.resolution.intents)) or bool(
        result.resolution.categories
    )
    answer_in_code = bool(result.resolution.activities) or (
        bool(result.resolution.unknown_activities)
        and not result.resolution.asks_where
        and not result.resolution.weather_asked
        and not other_subjects
    )
    if answer_in_code:
        # The small CPU model repeatedly turns seven "fair" daily scores into
        # a "good week". Render named verdicts from their stored rows so every
        # date, band and score survives without an invented overall verdict.
        #
        # `unknown_activities` is on this branch for the same reason, and it is
        # the stronger half. A question naming ONLY activities with no score --
        # "is tomorrow a good day to ski in Reykjavik?" -- used to fall through
        # to the model with the gap stated in the prompt and every catalogue row
        # beside it, and the model answered "it is a good day to ski", three
        # times out of three, attributing it to the stored data. There is no
        # wording for the model to get right here: the whole answer is that
        # nothing scores this activity. Rendering it in code means the model is
        # not called, so there is no verdict for it to invent.
        answer = named_activity_answer(result)
        # And the gaps, for the same reason they are appended on the model path
        # below. This route returned before that line, so a named activity asked
        # across a window running past the stored forecast listed only the
        # covered days and said nothing at all about the rest -- the one case
        # where answering in code was less truthful than answering through the
        # model. `gap_block` is empty for a fully covered window, so an ordinary
        # in-coverage question is unchanged.
        missing = grounding.gap_block(dated_gap_brief(result, brief), answer)
        joined = f"{answer}\n\n{missing}" if missing else answer
        return respond(result, joined, llm_called=False)

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
    #
    # Activity gaps are the second block and not part of the first, because they
    # are only appended when the model left the activity out altogether -- the
    # prompt asks it to say these itself and it usually does, and printing both
    # would read as a stutter. `grounding.unstated_activity_gaps` is where that
    # judgement lives. Without it, the route the fix above sends a mixed question
    # down answers the events and says nothing whatever about the activity: the
    # rows would survive and half the question still would not be answered.
    blocks = [answer]
    blocks.extend(
        block
        for block in (
            grounding.gap_block(brief, answer),
            grounding.unstated_activity_gaps(brief, answer),
        )
        if block
    )
    return respond(result, "\n\n".join(blocks), llm_called=True)


def plain_answer(result: Retrieval) -> str:
    """The grounded answer: the same rows, formatted by code.

    It is deliberately plain. Its job is to prove that every fact in the pretty
    answer came from a row, to keep the system useful when `llm` is down, and
    to be what the traveller gets when the model's wording fails validation.
    """
    return grounding.render(grounding.build(result))


def dated_gap_brief(result: Retrieval, brief: grounding.Brief) -> grounding.Brief:
    """The brief the named-activity gap sentence is written from.

    The same rows, minus any day the answer has just reported a score for. The
    coverage gap is derived from the forecast rows alone, and a day can carry a
    stored score whose forecast row is gone -- a day re-ingested short, or one
    whose weather row was replaced. Left alone, the appended sentence then denies
    weather for a date listed with a score three lines above it, which reads as a
    contradiction rather than as a limit. A day with no score keeps its gap,
    which is the whole point of appending one here.
    """
    dated = {str(row["forecast_date"]) for row in result.recommendations}
    if not dated.intersection(result.uncovered_days):
        return brief
    return grounding.build(
        replace(result, uncovered_days=[d for d in result.uncovered_days if d not in dated])
    )


def unscored_line(result: Retrieval, activity: str) -> str:
    """The one line an activity with no stored score gets, wherever it is
    rendered. Two callers had their own copy of it and only one of them would
    have gained the sentence below.

    The coast half answers "why not", which is the more useful half. It is read
    from the catalogue when the catalogue holds the activity, and from the
    activity's own name when it does not -- the same two-sided test the write
    path uses to decide whether to store a row at all
    (`services/consumer/main.py`, `needs_coast`). Without the second side,
    "scuba diving in London" was told only that no score was on record, while
    "surfing in London" was told the reason.

    `rules.needs_coast`, which is narrower than `rules.names_water`: this
    sentence is the one place the coast claim is made to a reader, so it has to
    be the claim that is true. "wild swimming" names water and needs no sea, and
    saying "London has no coast on record" under it would be offering a fact as
    the cause of something it did not cause.
    """
    cfg = _activity_meta().get(activity) or {}
    needs_coast = cfg.get("requires_coast") if cfg else rules.needs_coast(activity)
    reason = ""
    if needs_coast and not result.resolution.city.get("coastal"):
        reason = f"; {result.resolution.city['name']} has no coast on record"
    return f"- {activity.replace('_', ' ')}: no suitability score on record{reason}."


def unscored_date_lines(result: Retrieval) -> list[str]:
    """One line per named activity that has some stored scores but not all.

    The gap this closes is a day, not an activity. `unscored_activities` covers
    an activity with no row anywhere, and `grounding.gap_block` covers a day the
    stored forecast never reached. Between them sits the day this city *does*
    have weather for and this activity has no score on -- a typed activity
    requested for one day of the week, or a day the enricher has not scored yet.
    Both dated answers listed the rows they had and said nothing about the rest.

    Measured against `covered_days`, which is derived from the forecast rows that
    actually came back for this city, so a day named here is a day the answer
    could otherwise have been expected to speak about.
    """
    covered = set(result.covered_days)
    if not covered:
        return []
    lines: list[str] = []
    for activity in result.resolution.activities:
        rows = [row for row in result.recommendations if row["activity"] == activity]
        if not rows:
            # No row at all: already stated through `unscored_activities`.
            continue
        absent = sorted(covered - {str(row["forecast_date"]) for row in rows})
        if absent:
            lines.append(
                f"- {rows[0]['activity_label']}: no suitability score is stored for "
                f"{grounding.date_runs(absent)}."
            )
    return lines


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
        # The heading is the requested window, so a day inside it with no score
        # has to be named here or the list reads as though it covered the lot.
        lines.extend(unscored_date_lines(result))
        lines.append("")
        lines.append(score_caveat(result))
        blocks.append("\n".join(lines))

    unscored = [unscored_line(result, activity) for activity in result.unscored_activities]
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

    An activity the traveller typed has no row in data/activities.yml, so the
    flag cannot be read off one -- but `rules.GENERIC_SEA_CFG` gives it the same
    69 ceiling when its name says water, and a capped score that does not say why
    it is capped is a number quietly lowered behind the reader's back. Scuba
    diving in Tel Aviv was reported as "fair (69/100)" with no explanation while
    catalogue surfing at the identical 69 carried one. The same two-sided test
    the write path uses decides it here.
    """
    meta = _activity_meta()
    if not any(
        (meta.get(activity) or {}).get("sea_state_unmeasured")
        if activity in meta
        else rules.names_water(activity)
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


def requested_caveat(rows: list[dict[str, Any]]) -> str | None:
    """What a score for a typed activity is a score of, or None.

    An activity the traveller asked for by name is scored against a general
    outdoor-comfort measure rather than a rule written for it -- `GENERIC_CFG`
    in `common.rules` -- and `score_requested` records that on the row as a
    reason. Until this function existed the label reached no screen at all: the
    API returns `reasons`, nothing rendered it, and a generic score was shown
    beside eighteen tuned ones with nothing to tell them apart. Read off the
    rows rather than composed here, for the same reason the as-of footer is: the
    sentence has to be the one the rule engine actually attached.
    """
    said: list[str] = []
    for row in rows:
        if not row.get("requested"):
            continue
        for reason in row.get("reasons") or []:
            if reason not in said:
                said.append(str(reason))
    if not said:
        return None
    return "You asked for this activity by name, so: " + "; ".join(said) + "."


def named_activity_answer(result: Retrieval) -> str:
    """The answer to "is it good for surfing in Tel Aviv tomorrow?".

    Rendered in code, not by the model, so every date keeps its own band and
    score. The caveat at the end is the half this route used to be missing:
    `where_answer` said what a coastal score does not cover and this one said
    nothing, so the question that asks for a verdict most directly -- naming
    the activity outright -- was the one answered with a bare number.

    It now also answers for an activity the catalogue does not hold but a
    previous request already scored. The router resolves such a noun to its
    stored rows (`Router.typed_activities`), so those rows -- and only those --
    are what this reports, with `requested_caveat` saying what kind of score it
    is. Before that, the noun resolved to nothing, the retrieval was never
    narrowed, and the model was handed one row per catalogue activity to pick a
    number from.

    DECIDED, AND THE ANSWER IS NO: the enricher's stored wording is not printed
    here. `queries.recommendations` already selects `r.text` -- the sentence the
    local model wrote about this exact row -- so rendering it would be one line,
    and the argument for it is real: this is the question a reviewer is most
    likely to ask, it is the one route that never calls the model, and a
    reviewer who only asks activity questions never sees the model's prose
    through the agent at all.

    It is still no, for two reasons that do not cancel out.

    First, what that column has been checked against. `enricher.validate_text`
    runs `grounding.schedule_claims` and nothing else -- shape, length, and the
    one claim a suitability row can never support -- because the enricher holds
    no retrieved rows to check anything wider against. `grounding.violations`,
    which every other piece of model prose the agent prints must survive, has
    never run on it. Printing it here would put the least-checked model output
    in the system into the most-scrutinised answer in the system, and it would
    arrive below a heading that says these are stored suitability figures.

    Second, what this route is for. It exists because the model answered "it is
    a good day to ski in Reykjavik" with no ski row anywhere, three times out of
    three, and because a 1.7B model summarises seven `fair` days as a good week.
    Both failures are verdicts in prose beside correct numbers, which is exactly
    the shape `text` has.

    The wording is not hidden, either. The UI's Suitability page prints it in
    full, per row and with the model that wrote it, and the trip planner carries
    it into each day (`planning.grounded_why`, which re-runs `schedule_claims` and
    falls back to the rule engine's own reasons if it fails). So the demand
    behind this is met by looking there, rather than by weakening the one route
    that renders only what the rule engine decided. The model is still reached
    from the agent by every question that is not solely about a named activity:
    weather, events, places, background and mixed questions all call
    `client.chat_json`.
    """
    city = result.resolution.city["name"]
    # The heading has to be true of what follows it. With no row at all, every
    # line below is a gap, and calling that list "stored suitability" would be
    # the one sentence in a refusal that still sounds like an answer.
    lines = [
        f"Stored suitability for the activities you asked about in {city}:"
        if result.recommendations
        else f"I hold no suitability score for what you asked about in {city}:"
    ]
    for row in result.recommendations:
        lines.append(
            f"- {row['forecast_date']}: {row['activity_label']} is "
            f"{row['band']} ({row['score']}/100)."
        )
    for activity in result.unscored_activities:
        lines.append(unscored_line(result, activity))
    lines.extend(unscored_date_lines(result))
    # Only when there is actually a score to qualify. An inland city has no
    # coastal row at all, and its answer is already the stronger statement --
    # "London has no coast on record" -- so following it with a note about what
    # its scores do not measure would be qualifying scores that do not exist.
    generic = requested_caveat(result.recommendations) if result.recommendations else None
    sea = sea_state_caveat(result, lead="They") if result.recommendations else ""
    for caveat in (generic, sea):
        if caveat:
            # A blank line, because the UI renders this as markdown and a caveat
            # on the line after a list item would be read as part of the list.
            lines.append("")
            lines.append(caveat)
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
    requested = list(dates.DateRange(start, end, "").days())

    # Which days this *city* has a forecast row for, taken from the rows
    # themselves rather than from the coverage window.
    #
    # `COVERAGE_SQL` reports MIN/MAX over `weather_daily` with no city
    # predicate, and `queries.in_coverage` never sees a city, so the window
    # calls a day covered whenever *any* city holds a row for it. Planning off
    # that offered days this city has nothing for, and every consequence was a
    # claim the data does not support: the UI drew them as "no scored activity"
    # under a poor-band score pill, which reads as a verdict on the weather
    # rather than as a gap; `requested_days_outside_coverage` came back empty,
    # so the one warning that would have said so never fired; and the title and
    # `end_date` counted them, so a saved plan recorded a range nothing was
    # scored across. It happens whenever one city was ingested further ahead
    # than another, after a partial refresh, or when a single day failed to
    # ingest -- and a day missing from the middle of the window is invisible to
    # any first-to-last comparison, however it is scoped.
    forecast_rows = queries.forecast(conn, body.city, start=start, end=end)
    covered, missing = queries.stored_forecast_days(requested, forecast_rows)
    uncovered = [day.isoformat() for day in missing]
    weather_by_day = {str(row["forecast_date"]): row["as_of"] for row in forecast_rows}
    if not covered:
        raise HTTPException(
            422,
            f"no stored weather for {city['name']} between {start} and {end}; the forecast "
            f"covers {coverage['weather_first_date']} to {coverage['weather_last_date']} "
            f"across all cities",
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
                "weather_as_of": weather_by_day[key].isoformat()
                if not isinstance(weather_by_day[key], str)
                else weather_by_day[key],
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
        "as_of": max(
            datetime.fromisoformat(str(weather_by_day[day.isoformat()]).replace("Z", "+00:00"))
            for day in covered
        ).isoformat(),
        "coverage": {
            "first": coverage["weather_first_date"],
            "last": coverage["weather_last_date"],
        },
        # Every asked-for day this city has no row for, whether it fell outside
        # the stored window or inside it. The UI renders this as "No stored
        # weather for: ... Those days are left out rather than guessed", which
        # is the same sentence the agent's partial-coverage gap uses -- one
        # wording for one fact, wherever the reader meets it.
        "requested_days_outside_coverage": uncovered,
    }
