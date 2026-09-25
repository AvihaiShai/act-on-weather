"""The agent's router: code decides what to look up, the model only phrases it.

The alternative -- giving a 1.7B model tool definitions and letting it plan --
was tried and rejected. On CPU it costs three or four sequential generations
per question (30-90 s), it is non-deterministic in front of a reviewer, and
when it goes wrong it goes wrong silently. Here, city, dates, intent and
coverage are resolved by readable code, the queries are ordinary SQL, and the
model gets exactly one call with the retrieved rows in front of it.

What that buys, concretely:

  * the agent cannot answer about a date the system has no data for, because
    the coverage gate returns a template answer and never reaches the model
  * it cannot invent an event or a restaurant, because the only names in the
    prompt are names that came out of the database
  * the as-of footer is written in code, so it cannot be paraphrased away
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import yaml

from ..common import config, queries
from . import dates, grounding

log = logging.getLogger("agent.router")

INTENT_WORDS: dict[str, tuple[str, ...]] = {
    "weather": (
        "weather",
        "forecast",
        "rain",
        "temperature",
        "hot",
        "cold",
        "wind",
        "sunny",
        "sunshine",
        "umbrella",
        "degrees",
    ),
    "events": (
        "event",
        "events",
        "match",
        "matches",
        "fixture",
        "game",
        "games",
        "concert",
        "concerts",
        "gig",
        "tournament",
        "race",
        "sport",
        "sports",
    ),
    "places": (
        "place",
        "places",
        "restaurant",
        "restaurants",
        "dining",
        "eat",
        "food",
        "museum",
        "museums",
        "shopping",
        "shop",
        "shops",
        "bar",
        "club",
        "park",
        "gallery",
        "theatre",
        "see",
        "visit",
        "attraction",
        "attractions",
    ),
    "facts": (
        "history",
        "historical",
        "historic",
        "about",
        "known for",
        "tell me about",
        "founded",
        "culture",
    ),
    "activities": (
        "activity",
        "activities",
        "do",
        "should i",
        "good day",
        "suitable",
        "worth",
        "recommend",
        "plan",
        "itinerary",
        "trip",
    ),
}

# A question can ask three different things about one activity, and the router
# only ever heard one of them:
#
#   "is it good for surfing tomorrow?"  -> when.  A suitability verdict.
#   "where can I surf in Tel Aviv?"     -> where. A location.
#   "where and when can I surf?"        -> both.
#
# `where` matched no intent word at all, so the second question was answered
# with seven days of scores and no location: the system answering the question
# it had an answer for rather than the one that was asked.
WHERE_WORDS: tuple[str, ...] = (
    "where",
    "whereabouts",
    "near",
    "nearest",
    "closest",
    "spot",
    "spots",
    "location",
    "locations",
    "venue",
    "venues",
    "which beach",
    "which beaches",
    "what beach",
    "which museum",
    "which park",
)

# Timing words. Their absence is what keeps a bare "where can I surf?" from
# being answered with a week of weather -- see `Resolution.asks_when`, which
# also counts any date the question named.
WHEN_WORDS: tuple[str, ...] = (
    "when",
    "what day",
    "which day",
    "best day",
    "best time",
    "conditions",
    "good day",
    "worth it",
)

# How many venues one answer names. The planner's cap is three, because a day
# in an itinerary is a suggestion; a direct "where" question is asking for the
# list, so it gets a longer one.
MAX_WHERE_PLACES = 6


# Words that make a question about the city itself rather than about one of its
# buildings. `facts` holds one `history` row per city among dozens of
# `landmark` ones, and ordering by title buries it.
HISTORY_WORDS = ("history", "historical", "historic", "founded", "heritage", "past")

# A category can also name a scored activity (comedy, markets). These words
# make it a question about a dated listing instead of activity suitability.
EVENT_SCHEDULE_WORDS = ("on", "scheduled", "happening", "show", "shows", "playing")


# Deliberately not a synonym list the model can extend: these are the only
# categories the database actually holds.
def load_interests(path) -> dict[str, list[str]]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)["interests"]


def _mentions(text: str, phrase: str) -> bool:
    """Whole-word match. `phrase` may contain spaces ('fine dining')."""
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


@lru_cache(maxsize=1)
def activity_meta() -> dict[str, dict[str, Any]]:
    """data/activities.yml, read once. Same file the rule engine scores from,
    so the router cannot describe an activity the scorer does not have."""
    with open(config.DATA_DIR / "activities.yml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)["activities"]


_LEADING_ARTICLE = re.compile(r"^(a|an|the)\s+", re.IGNORECASE)


def where_noun(activity: str) -> str:
    """What an answer calls the place you do this activity.

    `where_noun` in activities.yml when the activity sets one, otherwise its
    first venue category, otherwise a phrase built from the label. It is only
    ever wording -- nothing here decides whether a location can be given.
    """
    cfg = activity_meta().get(activity) or {}
    if cfg.get("where_noun"):
        return str(cfg["where_noun"])
    categories = cfg.get("place_categories") or []
    if categories:
        return str(categories[0]).replace("_", " ")
    label = str(cfg.get("label") or activity.replace("_", " "))
    return f"place for {_LEADING_ARTICLE.sub('', label).lower()}"


def where_gap(activity: str, city: str) -> str:
    """The sentence for an activity the system cannot put on a map.

    Two different gaps, and the answer says which one it is. A venue category
    that matched no row means this city has none; no venue category at all
    means no source the system holds records that kind of place anywhere, and
    the honest answer is not a nearby beach.
    """
    cfg = activity_meta().get(activity) or {}
    noun = where_noun(activity)
    if cfg.get("place_categories"):
        return f"I have no {noun} on record for {city}."
    return f"I do not have a verified {noun} for {city}: no source I hold records where to do it."


def load_activity_keywords(path) -> dict[str, list[str]]:
    """activity slug -> the words a user types for it, from activities.yml.

    Sorted longest first, so "a long walk" is matched before "walk" and the
    question is attributed to hiking rather than to whichever activity happens
    to share a shorter word with it.
    """
    with open(path, encoding="utf-8") as fh:
        activities = yaml.safe_load(fh)["activities"]
    return {
        key: sorted(cfg.get("keywords") or [], key=len, reverse=True)
        for key, cfg in activities.items()
    }


@dataclass
class Resolution:
    question: str
    city: dict[str, Any] | None = None
    city_mentioned: str | None = None
    window: dates.DateRange | None = None
    intents: list[str] = field(default_factory=list)
    interests: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    # Activity slugs the question actually named, e.g. {"surfing"} for
    # "can I surf in London?". Used to notice when the answer is missing.
    activities: list[str] = field(default_factory=list)
    # `events.category` values the question asked about, e.g. ["concert"] for
    # "which concerts are on this week?". Empty means no particular kind, which
    # is why the retrieval below filters only when this is non-empty: an open
    # question should still see everything that is on.
    event_categories: list[str] = field(default_factory=list)
    # "where can I surf?" asks for a place, not a verdict.
    asks_where: bool = False
    # True when the question named a date, or asked about timing or conditions.
    # A question that asks only `where` gets no weather in its answer, because
    # it did not ask for any.
    asks_when: bool = False

    @property
    def where_only(self) -> bool:
        """A location question with no timing in it. The weather lookups are
        skipped entirely for these, so the answer cannot lead with scores the
        user never asked for -- and so a location question still answers after
        the stored forecast window has run out."""
        return self.asks_where and not self.asks_when


@dataclass
class Retrieval:
    resolution: Resolution
    coverage: dict[str, Any]
    in_coverage: bool
    forecast: list[dict[str, Any]] = field(default_factory=list)
    recommendations: list[dict[str, Any]] = field(default_factory=list)
    places: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)
    refusal: str | None = None
    # Activities the question named that this city has no scored row for --
    # almost always a coastal activity asked about an inland city. Reported to
    # the model explicitly, because the failure mode otherwise is not silence:
    # asked "is it good for surfing in London?" with no surfing row in front of
    # it, the model helpfully invents a weather-based reason why it is not.
    unscored_activities: list[str] = field(default_factory=list)
    # The `where` route's answer: activity slug -> the stored rows that ARE a
    # venue for it. Only activities declaring `place_categories` in
    # activities.yml can appear here, so a beach is never returned as a surf
    # spot; see `venues_for`.
    venues: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Activities the question asked WHERE about and the system cannot locate,
    # either because no source it holds records a venue for that activity or
    # because this city has no such row. Rendered as an explicit gap.
    unlocated_activities: list[str] = field(default_factory=list)
    # Filled only when an event retrieval came back empty: how many listings
    # for the same city, window and categories the freshness filter removed,
    # and when the newest of them was last checked. It turns a bare "nothing on
    # record" into a statement about the feed rather than about the city.
    expired_events: dict[str, Any] = field(default_factory=dict)
    # Whether this question was scoped to the weather at all. A pure `where`
    # question is answerable from a fully expired snapshot, so it must never be
    # narrowed by the two lists below -- and an empty `covered_days` means
    # "this question asked about weather and got no day", which is a different
    # thing from "weather was never in scope". Only the flag tells them apart.
    weather_scoped: bool = False
    # Days inside the asked window that the stored forecast has a row for, and
    # days it does not, both derived from the rows that came back. The
    # whole-window case is a refusal above; this is the partial case, which is
    # what a snapshot looks like a few days after it was staged and is
    # therefore the ordinary state rather than the odd one.
    covered_days: list[str] = field(default_factory=list)
    uncovered_days: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            (
                self.forecast,
                self.recommendations,
                self.places,
                self.events,
                self.facts,
                self.venues,
            )
        )


class Router:
    def __init__(self, conn):
        self.conn = conn
        self.interests = load_interests(config.DATA_DIR / "interests.yml")
        self.activity_keywords = load_activity_keywords(config.DATA_DIR / "activities.yml")

    # -- resolving ---------------------------------------------------------
    def resolve(self, question: str) -> Resolution:
        text = question.lower()
        resolution = Resolution(question=question)

        for city in queries.cities(self.conn):
            names = [city["id"], city["name"].lower(), *(city.get("aliases") or [])]
            for name in names:
                if re.search(rf"\b{re.escape(str(name).lower())}\b", text):
                    resolution.city = city
                    resolution.city_mentioned = str(name)
                    break
            if resolution.city:
                break

        timezone = resolution.city["timezone"] if resolution.city else "UTC"
        resolution.window = dates.parse(question, timezone)

        # Matched on word boundaries, not as substrings. Plain `in` looks
        # harmless and is not: "eat" is inside "weather", so every weather
        # question silently acquired the `places` intent and a restaurant
        # lookup it never asked for.
        resolution.asks_where = any(_mentions(text, word) for word in WHERE_WORDS)
        # A named date is itself a timing question: "where can I surf on
        # Saturday" wants both halves. The parser's default range is labelled
        # "(assumed)" precisely so it can be told apart from one the user gave.
        resolution.asks_when = not resolution.window.label.endswith("(assumed)") or any(
            _mentions(text, word) for word in WHEN_WORDS
        )

        for intent, words in INTENT_WORDS.items():
            if any(_mentions(text, word) for word in words):
                resolution.intents.append(intent)
        # Asking where is asking about places, whatever else the sentence
        # contains. Without this, "where should I go in Rome?" fell through to
        # the weather default below and answered with a forecast.
        if resolution.asks_where and "places" not in resolution.intents:
            resolution.intents.append("places")
        # Asking about the weather is asking about conditions, so it counts as
        # the `when` half even when no date was named.
        resolution.asks_when = resolution.asks_when or "weather" in resolution.intents
        # The weather+activities default is NOT applied here. It moved below,
        # after the event kind and the named activities have been resolved, so
        # that "any comedy on this week?" resolves to events rather than being
        # defaulted into a forecast before the question has been read out.

        for interest, categories in self.interests.items():
            spaced = interest.replace("_", " ")
            if _mentions(text, spaced) or _mentions(text, interest):
                resolution.interests.append(interest)
                resolution.categories.extend(categories)
        # "fine dining" and "restaurants" are the same rows; do not ask twice.
        resolution.categories = sorted(set(resolution.categories))

        # Resolve the event kind before matching activity names: "any comedy
        # on?" and "what markets are on?" name both an event category and a
        # scored activity, but ask for a scheduled listing.
        resolution.event_categories = grounding.requested_event_categories(text)
        scheduled_events = bool(resolution.event_categories) and any(
            _mentions(text, word) for word in EVENT_SCHEDULE_WORDS
        )
        if scheduled_events and "events" not in resolution.intents:
            resolution.intents.append("events")

        for activity, keywords in self.activity_keywords.items():
            if any(_mentions(text, word) for word in keywords):
                resolution.activities.append(activity)
        # Naming an activity is asking whether to do it, whatever else the
        # sentence looks like. Without this, "can I surf tomorrow?" carries no
        # activity intent and never retrieves the verdict it is asking for.
        if scheduled_events and "activities" not in resolution.intents:
            resolution.activities.clear()
        elif resolution.activities and "activities" not in resolution.intents:
            resolution.intents.append("activities")
        if not resolution.intents:
            resolution.intents = ["weather", "activities"]
        return resolution

    def _facts(self, city_id: str, text: str, limit: int = 3) -> list[dict[str, Any]]:
        """Background rows, with the city's own history first when that is what
        was asked.

        `facts` are ordered by title, and a city holds one `history` row among
        dozens of `landmark` ones. Asked about the history of Lisbon, the agent
        used to be handed three alphabetically-first museum descriptions and
        nothing about the city -- which is how the model ended up writing the
        history itself.
        """
        rows: list[dict[str, Any]] = []
        if any(_mentions(text, word) for word in HISTORY_WORDS):
            rows = queries.facts(self.conn, city_id, topic="history", limit=limit)
        seen = {row["id"] for row in rows}
        for row in queries.facts(self.conn, city_id, limit=limit):
            if len(rows) >= limit:
                break
            if row["id"] not in seen:
                rows.append(row)
        return rows

    # -- retrieving --------------------------------------------------------
    def retrieve(self, question: str) -> Retrieval:
        resolution = self.resolve(question)
        text = question.lower()
        coverage = queries.coverage(self.conn)
        window = resolution.window

        if resolution.city is None:
            known = ", ".join(c["name"] for c in coverage["cities"])
            return Retrieval(
                resolution,
                coverage,
                False,
                refusal=(
                    f"I do not hold data for that city. I have weather and "
                    f"tourism data for: {known}."
                ),
            )

        # The coverage gate. A question about a day the system has no weather
        # for is refused here, in code, before the model is ever called. A pure
        # location question is exempt: "where can I surf in Tel Aviv?" needs no
        # forecast, so a stale snapshot must not turn it into a refusal.
        weather_needed = (
            bool({"weather", "activities"} & set(resolution.intents)) and not resolution.where_only
        )
        covered_days = [d for d in window.days() if queries.in_coverage(coverage, d)]
        in_cov = bool(covered_days)
        if weather_needed and not in_cov:
            first, last = coverage["weather_first_date"], coverage["weather_last_date"]
            return Retrieval(
                resolution,
                coverage,
                False,
                refusal=(
                    f"I have no weather data for {window}. The stored forecast "
                    f"covers {first} to {last}, and I do not guess beyond it. "
                    f"Refresh the snapshot while connected to extend it."
                ),
            )

        start = covered_days[0] if covered_days else window.start
        end = covered_days[-1] if covered_days else window.end
        result = Retrieval(resolution, coverage, in_cov)
        result.weather_scoped = bool(weather_needed)
        city_id = resolution.city["id"]

        # A question that asked only where is not asked about the weather, so
        # none of it is fetched. That is what stops "where can I surf?" being
        # answered with a week of suitability scores.
        wants_weather = not resolution.where_only and (
            {"weather", "activities"} & set(resolution.intents)
        )
        if wants_weather:
            result.forecast = queries.forecast(self.conn, city_id, start=start, end=end)
            result.recommendations = queries.recommendations(
                self.conn,
                city_id,
                start=start,
                end=end,
                activity=resolution.activities[0] if len(resolution.activities) == 1 else None,
            )
            if resolution.activities:
                # A named activity is the subject of the question, not one
                # option among the catalogue. Keep every covered day for it.
                named = set(resolution.activities)
                result.recommendations = [
                    row for row in result.recommendations if row["activity"] in named
                ]

            # Which days the answer may speak about, taken from the rows that
            # actually came back rather than from the coverage window.
            #
            # `queries.coverage` reports a global MIN/MAX over `weather_daily`
            # across every city, and `in_coverage` never sees the city, so the
            # window above says "covered" for a day that this city has no row
            # for -- whenever one city was ingested further ahead than another,
            # or a single day failed to ingest. Deriving this from the returned
            # rows is the only version that is true per city, and it is also
            # what makes a missing day in the *middle* of the window visible.
            stored = {str(row["forecast_date"]) for row in result.forecast}
            result.covered_days = [d.isoformat() for d in window.days() if d.isoformat() in stored]
            result.uncovered_days = [
                d.isoformat() for d in window.days() if d.isoformat() not in stored
            ]

        # The `where` route. An activity the question named is located from its
        # own declared venue categories, not from the general places list --
        # which is the difference between naming a museum for "where are the
        # museums" and offering a beach for "where can I surf".
        if resolution.asks_where and resolution.activities:
            result.venues, result.unlocated_activities = self.venues_for(
                city_id, resolution.activities
            )
        elif "places" in resolution.intents or resolution.categories:
            # Spread the budget across the categories the question named. E2
            # asks about concerts, shopping and fine dining, and a flat limit
            # returned eighteen concert halls and nothing else.
            result.places = queries.places(
                self.conn,
                city_id,
                categories=resolution.categories or None,
                limit=18,
                per_category=6 if len(resolution.categories) > 1 else None,
            )

        if "events" in resolution.intents or (
            "activities" in resolution.intents and not resolution.where_only
        ):
            result.events = queries.events(
                self.conn,
                city_id,
                start=window.start,
                end=window.end,
                categories=resolution.event_categories or None,
                limit=12,
            )
            if not result.events:
                # Only when the retrieval came back empty, so the ordinary path
                # still costs one query. What this buys is the difference
                # between "no concert is on record" and "the concert listings
                # on record for these dates have not been re-checked since 24
                # September", which is the same distinction the coverage window
                # already makes for weather and is the one a reader can act on.
                result.expired_events = queries.expired_events(
                    self.conn,
                    city_id,
                    start=window.start,
                    end=window.end,
                    categories=resolution.event_categories or None,
                )
        if "facts" in resolution.intents:
            result.facts = self._facts(city_id, text)

        # The activity coverage gate, and the sibling of the date gate above.
        # An activity the question named but this city holds no row for is
        # recorded here so `context_block` can say so in as many words. Only
        # meaningful when scores were actually looked up: in the `where` route
        # they never are, and calling every activity unscored would be a lie.
        if wants_weather:
            scored = {row["activity"] for row in result.recommendations}
            result.unscored_activities = [a for a in resolution.activities if a not in scored]

        return result

    def venues_for(
        self, city_id: str, activities: list[str]
    ) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        """Stored places that ARE a venue for each named activity.

        The rule is the one the trip planner already follows (see
        `planning.venue_places`): only an activity declaring `place_categories`
        in data/activities.yml can be located, because only there is the
        source's own class the venue by definition. Surfing, swimming, fishing
        and boat rides declare none -- Wikidata Q40080 and OSM `natural=beach`
        assert that a beach is there, not that the surf is rideable, the water
        lifeguarded, the angling permitted or a boat for hire -- so they come
        back unlocated and the answer says so.

        Returns (venues by activity, activities that could not be located).
        """
        meta = activity_meta()
        venues: dict[str, list[dict[str, Any]]] = {}
        unlocated: list[str] = []
        for activity in activities:
            categories = (meta.get(activity) or {}).get("place_categories") or []
            rows = (
                queries.places(
                    self.conn, city_id, categories=list(categories), limit=MAX_WHERE_PLACES
                )
                if categories
                else []
            )
            if rows:
                venues[activity] = rows
            else:
                # Either no source records a venue for this activity at all, or
                # this city holds no such row. Both are gaps, and `where_answer`
                # tells them apart when it explains.
                unlocated.append(activity)
        return venues, unlocated


# ------------------------------------------------------------- rendering ----


def context_block(result: Retrieval) -> str:
    """The rows, flattened into text for the model.

    Assembled by `grounding.build` into typed facts first, so a place and a
    scheduled event are different things here and not two paragraphs the model
    is asked to keep apart. This is the ONLY source of fact the model is given:
    there is no retrieval inside the prompt and no general knowledge it is
    invited to add.
    """
    return grounding.prompt_block(grounding.build(result))


def context_recommendations(result: Retrieval) -> list[dict[str, Any]]:
    """Fit the prompt while retaining every date in a multi-day question.

    Named activities already have a small, focused result set. For an open
    question, take the best rows from each day in rounds instead of taking
    forty rows from the start of the date range.
    """
    rows = result.recommendations
    if result.resolution.activities or len(rows) <= 40:
        return rows
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_day.setdefault(str(row["forecast_date"]), []).append(row)
    for day_rows in by_day.values():
        day_rows.sort(key=lambda row: (-(row["score"] or 0), row["activity"]))
    selected: list[dict[str, Any]] = []
    while len(selected) < 40 and any(by_day.values()):
        for day_rows in by_day.values():
            if day_rows and len(selected) < 40:
                selected.append(day_rows.pop(0))
    return sorted(selected, key=lambda row: (row["forecast_date"], row["activity"]))


def footer(result: Retrieval) -> str:
    """Written in code, appended after the model has spoken, so it cannot be
    reworded, rounded or dropped."""
    coverage = result.coverage
    parts = []
    # Stamp the weather only when the answer used it. A pure location answer
    # does not, and footing it with a forecast window implies the answer
    # depended on a snapshot it never read -- which is the same mistake in
    # provenance that the `where` route fixes in the answer above it. A refusal
    # keeps the stamp: "I have no weather for that date" is a statement about
    # the coverage window, so the window is exactly what it rests on.
    used_weather = bool(result.forecast or result.recommendations or result.refusal)
    # `dates.parse` promises the assumed range is stated rather than left for
    # the reader to guess, and it was not: str(window) drops the label. Asked
    # "are there any sports events in London in October?", the parser falls
    # back to the coming week and the answer used to talk about October.
    #
    # Gated on the answer actually being about a date range. "Where can I surf
    # in Tel Aviv?" names no date and needs none, and telling its reader which
    # week the answer assumed would invent a scope the answer never had.
    window = result.resolution.window
    dated = used_weather or result.events or "events" in result.resolution.intents
    if dated and window is not None and "assumed" in window.label:
        parts.append(f"no dates in the question, so this covers {window}")
    as_of = coverage.get("weather_as_of")
    if used_weather and as_of:
        parts.append(
            f"weather as of {as_of:%Y-%m-%d %H:%M UTC}"
            if not isinstance(as_of, str)
            else f"weather as of {as_of}"
        )
    if used_weather and coverage.get("weather_first_date"):
        parts.append(
            f"forecast covers {coverage['weather_first_date']} to "
            f"{coverage['weather_last_date']}"
        )
    # Read off the rows that were actually used, never hardcoded: places come
    # from Wikidata or OpenStreetMap depending on which answered at staging
    # time, and a footer that names the wrong one is worse than no footer.
    sources: list[str] = []
    venue_rows = [row for rows in result.venues.values() for row in rows]
    if result.forecast:
        sources.append(result.forecast[0].get("provider") or "stored forecast")
    place_as_of = [row.get("as_of") for row in (*venue_rows, *result.places) if row.get("as_of")]
    if result.resolution.asks_where and not used_weather and not place_as_of:
        # A `where` answer that found no place still rests on a snapshot -- the
        # one it searched -- and saying when that was taken is the difference
        # between "there is no surf spot" and "none had been collected by this
        # date".
        for row in coverage.get("entities") or []:
            if row.get("entity") == "places" and row.get("as_of"):
                place_as_of.append(row["as_of"])
    if not used_weather and place_as_of:
        # Hard rule 2 still applies to an answer with no weather in it: the
        # places have their own as-of, and it is the one this answer rests on.
        newest = max(place_as_of, key=str)
        parts.append(
            f"places as of {newest:%Y-%m-%d}"
            if not isinstance(newest, str)
            else f"places as of {newest}"
        )
    for rows in (venue_rows, result.places, result.facts, result.events):
        for row in rows:
            source = row.get("source")
            if source and source not in sources:
                sources.append(source)
    if sources:
        parts.append("sources: " + ", ".join(sources))
    if result.events and any(e.get("is_sample") for e in result.events):
        parts.append("some event rows are labelled sample data")
    return " · ".join(parts)
