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
from typing import Any

import yaml

from ..common import config, queries
from . import dates

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


# Deliberately not a synonym list the model can extend: these are the only
# categories the database actually holds.
def load_interests(path) -> dict[str, list[str]]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)["interests"]


def _mentions(text: str, phrase: str) -> bool:
    """Whole-word match. `phrase` may contain spaces ('fine dining')."""
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


@dataclass
class Resolution:
    question: str
    city: dict[str, Any] | None = None
    city_mentioned: str | None = None
    window: dates.DateRange | None = None
    intents: list[str] = field(default_factory=list)
    interests: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)


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

    def is_empty(self) -> bool:
        return not any((self.forecast, self.recommendations, self.places, self.events, self.facts))


class Router:
    def __init__(self, conn):
        self.conn = conn
        self.interests = load_interests(config.DATA_DIR / "interests.yml")

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
        for intent, words in INTENT_WORDS.items():
            if any(_mentions(text, word) for word in words):
                resolution.intents.append(intent)
        if not resolution.intents:
            resolution.intents = ["weather", "activities"]

        for interest, categories in self.interests.items():
            spaced = interest.replace("_", " ")
            if _mentions(text, spaced) or _mentions(text, interest):
                resolution.interests.append(interest)
                resolution.categories.extend(categories)
        # "fine dining" and "restaurants" are the same rows; do not ask twice.
        resolution.categories = sorted(set(resolution.categories))
        return resolution

    # -- retrieving --------------------------------------------------------
    def retrieve(self, question: str) -> Retrieval:
        resolution = self.resolve(question)
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
        # for is refused here, in code, before the model is ever called.
        weather_needed = {"weather", "activities"} & set(resolution.intents)
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
        city_id = resolution.city["id"]

        if "weather" in resolution.intents or "activities" in resolution.intents:
            result.forecast = queries.forecast(self.conn, city_id, start=start, end=end)
        if "activities" in resolution.intents or "weather" in resolution.intents:
            result.recommendations = queries.recommendations(
                self.conn, city_id, start=start, end=end
            )
        if "places" in resolution.intents or resolution.categories:
            result.places = queries.places(
                self.conn, city_id, categories=resolution.categories or None, limit=18
            )
        if "events" in resolution.intents or "activities" in resolution.intents:
            result.events = queries.events(
                self.conn, city_id, start=window.start, end=window.end, limit=12
            )
        if "facts" in resolution.intents:
            result.facts = queries.facts(self.conn, city_id, limit=3)

        return result


# ------------------------------------------------------------- rendering ----


def context_block(result: Retrieval) -> str:
    """The rows, flattened into text. This is the ONLY source of fact the model
    is given -- there is no retrieval inside the prompt and no general knowledge
    it is invited to add."""
    city = result.resolution.city
    lines = [
        f"City: {city['name']}, {city['country']}",
        f"Dates asked about: {result.resolution.window}",
    ]

    if result.forecast:
        lines.append("\nStored daily forecast:")
        for row in result.forecast:
            parts = [f"  {row['forecast_date']}:"]
            if row["temp_max_c"] is not None:
                parts.append(f"high {row['temp_max_c']:.0f}C")
            if row["temp_min_c"] is not None:
                parts.append(f"low {row['temp_min_c']:.0f}C")
            if row["precip_mm"] is not None:
                parts.append(f"rain {row['precip_mm']:.1f}mm")
            if row["precip_prob"] is not None:
                parts.append(f"({row['precip_prob']}% chance)")
            if row["wind_kmh"] is not None:
                parts.append(f"wind {row['wind_kmh']:.0f}km/h")
            if row["sunshine_hours"] is not None:
                parts.append(f"sun {row['sunshine_hours']:.1f}h")
            lines.append(" ".join(parts))

    if result.recommendations:
        lines.append("\nSuitability scores from the rule engine (these are the verdicts):")
        for row in result.recommendations[:40]:
            text = f" -- {row['text']}" if row.get("text") else ""
            lines.append(
                f"  {row['forecast_date']} {row['activity_label']}: "
                f"{row['band']} ({row['score']}/100){text}"
            )

    if result.places:
        lines.append("\nPlaces on record (use only these names):")
        for row in result.places:
            sample = " [sample data]" if row.get("is_sample") else ""
            lines.append(f"  {row['name']} ({row['category']}){sample}")

    if result.events:
        lines.append("\nEvents on record (use only these; invent nothing):")
        for row in result.events:
            sample = " [sample data]" if row.get("is_sample") else ""
            venue = f" at {row['venue']}" if row.get("venue") else ""
            lines.append(
                f"  {row['starts_at'].date()} {row['title']} " f"({row['category']}){venue}{sample}"
            )
    elif "events" in result.resolution.intents:
        lines.append("\nEvents on record: none for that city and date range.")

    if result.places == [] and result.resolution.categories:
        lines.append("\nPlaces on record for those interests: none.")

    if result.facts:
        lines.append("\nBackground on record:")
        for row in result.facts:
            lines.append(f"  {row['title']}: {row['summary'][:400]}")

    return "\n".join(lines)


def footer(result: Retrieval) -> str:
    """Written in code, appended after the model has spoken, so it cannot be
    reworded, rounded or dropped."""
    coverage = result.coverage
    parts = []
    as_of = coverage.get("weather_as_of")
    if as_of:
        parts.append(
            f"weather as of {as_of:%Y-%m-%d %H:%M UTC}"
            if not isinstance(as_of, str)
            else f"weather as of {as_of}"
        )
    if coverage.get("weather_first_date"):
        parts.append(
            f"forecast covers {coverage['weather_first_date']} to "
            f"{coverage['weather_last_date']}"
        )
    # Read off the rows that were actually used, never hardcoded: places come
    # from Wikidata or OpenStreetMap depending on which answered at staging
    # time, and a footer that names the wrong one is worse than no footer.
    sources: list[str] = []
    if result.forecast:
        sources.append(result.forecast[0].get("provider") or "stored forecast")
    for rows in (result.places, result.facts, result.events):
        for row in rows:
            source = row.get("source")
            if source and source not in sources:
                sources.append(source)
    if sources:
        parts.append("sources: " + ", ".join(sources))
    if result.events and any(e.get("is_sample") for e in result.events):
        parts.append("some event rows are labelled sample data")
    return " · ".join(parts)
