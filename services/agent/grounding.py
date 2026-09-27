"""Typed facts between the stored rows and the sentence the traveller reads.

The agent used to hand the retrieved rows to the model as prose and trust a
system prompt to keep it honest. A prompt is not a check, and the model found
every gap in it:

  * asked which concerts were on in London this week, it answered with the
    Laver Cup -- a tennis tournament, category `sport`, the only event row the
    unfiltered query returned
  * asked the assignment's second example question, it offered nine concert
    halls as concerts "you can enjoy this week". Those are `places` rows: the
    system knows a name and a category for them and nothing else, least of all
    whether anything is playing
  * asked about the history of Lisbon, it described what the museums hold and
    called a district popular, from the names alone
  * asked about surfing in London, it agreed there was no record and then gave
    a weather-based verdict anyway

So the rows are assembled into typed facts here, in code, before the model is
called. Three things follow from that, and they are the fix:

  1. `build` separates a *place* from a *scheduled event* in the type system,
     not in an instruction. A `PlaceFact` has no date and cannot acquire one.
  2. `render` answers the whole question from those facts with no model at all,
     so there is always a correct answer to fall back to.
  3. `violations` checks the model's sentences against the same facts. Prose
     that asserts an event with no row behind it, or describes a place we hold
     only a name for, is thrown away and `render` is returned instead.

Gap sentences are written by code and appended after the model has spoken, the
same way the as-of footer is, so "there is no concert on record" cannot be
reworded into "there are concerts".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import yaml

from ..common import config

if TYPE_CHECKING:  # pragma: no cover - router imports this module at runtime
    from .router import Retrieval


# -------------------------------------------------------------- vocabulary --


@lru_cache(maxsize=1)
def event_types() -> dict[str, dict[str, Any]]:
    """data/event_types.yml, read once. Baked into the image, so it cannot
    change under a running container."""
    with open(config.DATA_DIR / "event_types.yml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)["event_types"]


def event_label(category: str) -> str:
    return str((event_types().get(category) or {}).get("label") or category.replace("_", " "))


def requested_event_categories(text: str) -> list[str]:
    """Which stored `events.category` values a question is asking about.

    Empty means "no particular kind", which is a different thing from "none":
    the caller retrieves every category in that case.
    """
    lowered = text.lower()
    return sorted(
        category
        for category, cfg in event_types().items()
        if any(_says(lowered, word) for word in (cfg.get("words") or []))
    )


@lru_cache(maxsize=1)
def claim_words() -> dict[str, tuple[str, ...]]:
    """Wording that asserts a scheduled event of a category. Deliberately
    narrower than the routing vocabulary -- see the note in event_types.yml."""
    return {category: tuple(cfg.get("claims") or ()) for category, cfg in event_types().items()}


# A verdict is the rule engine's to give. These are the phrasings that make one.
VERDICT_WORDS = (
    "good day",
    "good for",
    "great for",
    "ideal for",
    "perfect for",
    "best day",
    "suitable",
    "unsuitable",
    "poor for",
    "bad for",
    "worth doing",
    "suitability",
    "well suited",
    "recommended for",
    "favourable",
    "favorable",
)

# Anything that reads as the forecast. An activity with no stored score gets
# one sentence -- that there is no record -- and a clause that names it beside
# any of these is reasoning its way to the verdict it was told not to give.
WEATHER_WORDS = (
    "weather",
    "forecast",
    "temperature",
    "temperatures",
    "rain",
    "rainy",
    "wind",
    "windy",
    "sun",
    "sunny",
    "sunshine",
    "warm",
    "cold",
    "mild",
    "wet",
    "dry",
    "conditions",
    "degrees",
    "c",
    "°c",
)

# Capitalised words that are not a claim about anything: calendar vocabulary,
# units, and the handful of ordinary words a sentence can start a clause with.
# Everything else that is capitalised has to have come from a row.
NAME_STOPWORDS = frozenset(
    """
    monday tuesday wednesday thursday friday saturday sunday
    january february march april may june july august september october
    november december celsius fahrenheit centigrade utc gmt
    this that there these those they their them then than when where what
    which while with without however also both each here your yours
    """.split()
)

# The system stores a place's name and its category. A sentence that says more
# than that about a named place is saying something no row contains.
DESCRIPTION_WORDS = (
    "known for",
    "famous",
    "renowned",
    "popular",
    "iconic",
    "houses",
    "preserves",
    "showcases",
    "specialises",
    "specializes",
    "serves",
    "offers",
    "features",
    "boasts",
    "collection of",
    "must-see",
    "must see",
    "worth a visit",
)

# Quality and suitability descriptors. A `places` row carries a name and a
# category and nothing else -- no tier, no price band, no rating, no idea who a
# venue suits. These are the words a traveller types, and the model echoes them
# straight back onto the rows: asked the assignment's London question, it
# answered "Fine dining is available at ... Bar Italia", a 24-hour Soho cafe,
# from four rows that all say `restaurant` and no more. Echoing the question is
# normally harmless -- the question is in `vocabulary()` on purpose -- which is
# exactly why check 8 cannot see this one and it needs a check of its own.
QUALITY_WORDS = (
    "fine dining",
    "fine-dining",
    "upscale",
    "high-end",
    "gourmet",
    "michelin",
    "michelin-starred",
    "luxury",
    "luxurious",
    "top-rated",
    "highly rated",
    "finest",
    "premium",
    "acclaimed",
    "award-winning",
    "family-friendly",
    "romantic",
)

# Conditions this system does not ingest at all. It holds a land forecast --
# temperature, rain, wind, sunshine -- and nothing about the water, the snow or
# the air aloft. They are listed apart from `WEATHER_WORDS` because they are not
# a paraphrase of a stored row: a sentence built on one of these is not quoting
# the forecast loosely, it is describing a measurement that was never taken.
# Read only by check 5, where the activity in question has no stored score
# either, so the clause has nothing behind it in either direction.
UNMEASURED_CONDITION_WORDS = (
    "sea",
    "sea state",
    "water",
    "swell",
    "waves",
    "tide",
    "tides",
    "current",
    "currents",
    "visibility",
    "snow",
    "snowfall",
    "ice",
    "thermals",
    "updraft",
    "updrafts",
)

# Predicates that say a particular thing has been arranged to happen -- a date,
# a venue, a door time, a ticket. Deliberately much narrower than
# `WORLD_SCHEDULE_WORDS` below, which holds "there is" and "is on": both of
# those appear constantly in honest weather prose ("there is a 10% chance of
# rain", "the temperature is on the mild side") and keying anything positive on
# them would cost correct sentences their wording. Every phrase here has no
# ordinary non-event reading.
#
# Shared with the enricher and the trip planner, which word a suitability row
# and hold no event data at all, so for them any one of these is unsupportable.
SCHEDULE_PREDICATES = (
    "scheduled",
    "taking place",
    "take place",
    "takes place",
    "being held",
    "is held",
    "attend",
    "attending",
    "line-up",
    "lineup",
    "tickets",
    "doors open",
    "on stage",
    "kick-off",
    "kicks off",
    "performing",
    "showtime",
)

_NEGATION = re.compile(r"(?<!\w)(no|not|none|never|without|nor|nothing|lacks?)(?!\w)|n't")

# Predicates that say something is, or is not, actually happening. The system
# cannot make the negative of any of these: it holds a hand-checked feed of a
# few venues per city over a few weeks, so its silence about a concert is a gap
# in the feed and not an empty concert hall. "No concert is scheduled in London
# this week" is a claim about London; "no concert is on record for that week"
# is a claim about the feed, and only the second one is ours to make.
WORLD_SCHEDULE_WORDS = (
    "there is",
    "there are",
    "taking place",
    "take place",
    "takes place",
    "took place",
    "happening",
    "scheduled",
    "planned",
    "going on",
    "is on",
    "are on",
    "available",
)

# Wording that scopes a sentence to the stored feed. These are predicates, not
# mentions of the store: "the stored data shows no concert is taking place"
# names the store and still asserts something about London, which is exactly
# the sentence the model wrote and exactly the one this must not let through.
RECORD_PHRASES = (
    "on record",
    "no record",
    "not on record",
    "recorded",
    "on file",
    "in the feed",
    "stored event feed",
    "event feed",
    "i hold",
    "i have no",
    "i do not have",
    "i don't have",
    "in my records",
)

# What makes a sentence a sentence about scheduled things at all. Without this
# gate the check would reach ordinary prose; with it, it only reads clauses
# that are already talking about events.
_GENERIC_EVENT_WORDS = ("event", "events", "listing", "listings")


def _says(text: str, phrase: str) -> bool:
    """Whole-word match; `phrase` may contain spaces."""
    return re.search(rf"(?<!\w){re.escape(phrase.lower())}(?!\w)", text) is not None


def schedule_claims(text: str) -> list[str]:
    """The scheduling predicates a piece of prose uses, in the order found.

    Public because three surfaces need the same answer and only one of them has
    a `Brief`: `violations` check 11 here, the enricher, which words one
    suitability row and holds no event data at all, and the trip planner, which
    repeats the enricher's stored sentence.

    Why this and not check 1 for those two. Check 1 keys on the event-category
    claim words in data/event_types.yml, and three of the eighteen catalogue
    activities are named after an event kind -- "An open-air music festival",
    "An open-air farmers' market", "A stand-up comedy show". A category-keyed
    check applied to a single activity row would reject every honest wording of
    those three rows, in every city, on every day. The predicate is what
    separates "a good day for the open-air music festival" from "a good day to
    attend the open-air music festival", so the predicate is what is checked.
    """
    lowered = text.lower()
    return [phrase for phrase in SCHEDULE_PREDICATES if _says(lowered, phrase)]


_LEADING_ARTICLE = re.compile(r"^(a|an|the)\s+", re.IGNORECASE)


def activity_spellings(activity: str, label: str = "") -> set[str]:
    """How an answer is likely to write one activity: its slug with spaces, and
    its label with any leading article stripped.

    The article has to go. Every catalogue label that needs one carries it -- "A
    day at the beach", "An open-air music festival" -- and a sentence uses "the"
    or none at all, so matching the stored spelling verbatim matches almost
    nothing. Short spellings are dropped for the same reason `place_names` drops
    them: a four-letter token matches ordinary prose.
    """
    spellings = {activity.replace("_", " "), _LEADING_ARTICLE.sub("", label or "")}
    return {s.lower() for s in spellings if len(s) >= 5}


@lru_cache(maxsize=1)
def activity_names() -> dict[str, str]:
    """Every name an answer is likely to give a catalogue activity -> its slug,
    via `activity_spellings`. Read by check 10."""
    from .router import activity_meta

    names: dict[str, str] = {}
    for key, cfg in activity_meta().items():
        for spelling in activity_spellings(key, str((cfg or {}).get("label") or "")):
            names.setdefault(spelling, key)
    return names


def _about_events(sentence: str, asked: bool = False) -> bool:
    """True when the clause is talking about scheduled things at all.

    `asked` is set when the question itself was about events. It is what lets
    "nothing is on in London during those dates" be read as the event answer it
    is: the clause names no event word, and a bare pronoun is how the model
    most often writes the claim this check exists to catch.
    """
    if any(_says(sentence, word) for word in _GENERIC_EVENT_WORDS):
        return True
    if any(
        _says(sentence, word) for cfg in event_types().values() for word in (cfg.get("words") or ())
    ):
        return True
    return asked and any(_says(sentence, word) for word in ("nothing", "anything", "none"))


def _sentences(answer: str) -> list[str]:
    """Split on sentence enders only.

    Not on ':' or ';'. "The following concerts are scheduled: Laver Cup 2026"
    is one claim about one row, and splitting it in two hides the fact that a
    tennis tournament was just called a concert.
    """
    parts = re.split(r"(?<=[.!?])\s+|\n+", answer)
    return [p.strip() for p in parts if p.strip()]


_CLAUSE_BREAK = re.compile(r";|,?\s*(?<!\w)(?:though|although|but|however|whereas|while)(?!\w)")


def _clauses(sentence: str) -> list[str]:
    """Contrastive clauses, split apart.

    Negation is scoped to the clause that carries it. Without that, the model
    writes "you can enjoy a concert at the concert halls, though concerts are
    not on record", the negation at the end covers the whole sentence, and the
    invitation at the front goes unchecked.
    """
    parts = _CLAUSE_BREAK.split(sentence)
    return [p.strip() for p in parts if p.strip()]


@lru_cache(maxsize=1)
def _place_category_phrases() -> tuple[str, ...]:
    """The `places.category` vocabulary, spelled the way a sentence spells it.

    Masked out before the event-claim check, because "Wigmore Hall is a concert
    hall" names a building and asserts no concert. Longest first, so "sports
    centre" is removed before anything can match inside it.
    """
    with open(config.DATA_DIR / "interests.yml", encoding="utf-8") as fh:
        interests = yaml.safe_load(fh)["interests"]
    phrases = {c.replace("_", " ") for categories in interests.values() for c in categories}
    phrases |= {f"{p}s" for p in phrases}
    return tuple(sorted(phrases, key=len, reverse=True))


def _without_place_categories(sentence: str) -> str:
    for phrase in _place_category_phrases():
        sentence = re.sub(rf"(?<!\w){re.escape(phrase)}(?!\w)", " ", sentence)
    return sentence


def _without_activity_names(sentence: str, names: frozenset[str] | set[str]) -> str:
    """The same masking for the activity names a retrieval actually returned.

    Longest first, so a label is removed before a shorter name can match inside
    it.
    """
    for phrase in sorted(names, key=len, reverse=True):
        sentence = re.sub(rf"(?<!\w){re.escape(phrase)}(?!\w)", " ", sentence)
    return sentence


_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTHS = (
    "january february march april may june july august september october november december"
).split()
_LONG_DATE = re.compile(
    r"\b(?:(\d{1,2})\s+(" + "|".join(_MONTHS) + r")|(" + "|".join(_MONTHS) + r")\s+(\d{1,2}))"
    r"(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)


def _dates_in(sentence: str, candidates: list[str] | None = None) -> set[str]:
    """Every calendar day a sentence names, in ISO form.

    ISO and written dates are accepted. Year-less dates and weekdays resolve
    only when the asked window gives them exactly one possible day.
    """
    found = {f"{y}-{m}-{d}" for y, m, d in _ISO_DATE.findall(sentence)}
    possible = [date.fromisoformat(day) for day in candidates or []]
    for day_first, month_a, month_b, day_second, year in _LONG_DATE.findall(sentence):
        month = (month_a or month_b).lower()
        month_number = _MONTHS.index(month) + 1
        day_number = int(day_first or day_second)
        if year:
            found.add(f"{year}-{month_number:02d}-{day_number:02d}")
        else:
            matches = [d for d in possible if d.month == month_number and d.day == day_number]
            if len(matches) == 1:
                found.add(matches[0].isoformat())
    weekdays = "monday tuesday wednesday thursday friday saturday sunday".split()
    for weekday in re.findall(
        r"\b(?:on|this)\s+(" + "|".join(weekdays) + r")\b", sentence, re.IGNORECASE
    ):
        matches = [d for d in possible if d.weekday() == weekdays.index(weekday.lower())]
        if len(matches) == 1:
            found.add(matches[0].isoformat())
    return found


def _claim_tail(clause: str, words: tuple[str, ...]) -> str:
    """The part of a clause from its first event-claim word onward.

    `_clauses` splits on a semicolon and on the contrastive conjunctions, and
    on nothing else -- so "2026-09-26 will be sunny, and a concert is on the
    25th" is one clause carrying two dates that belong to two different facts.
    A date *before* the claim word is not where the model put the event: it is
    the window the sentence opened with, or the weather it had just finished
    describing. Reading those as the concert's date costs a correct answer its
    wording, and the model writes both shapes constantly.

    Returns "" when the clause holds no claim word at all, which is the caller
    asking about a category it never asserted.
    """
    starts = [
        m.start() for word in words for m in re.finditer(rf"(?<!\w){re.escape(word)}(?!\w)", clause)
    ]
    return clause[min(starts) :] if starts else ""


def _tokens(sentence: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z'’-]*", sentence)


_NUMBER = re.compile(r"\d[\d,. ]*")


def _numbers_in(text: str) -> set[str]:
    """Every number a piece of text states, as a bare digit string.

    Thousands separators and trailing punctuation are stripped and leading
    zeros dropped, so "2,000", "2000" and "02000" are one number, and the month
    in "2026-09-25" is the same 9 the model writes as "September 9".
    """
    found: set[str] = set()
    for raw in _NUMBER.findall(text):
        for part in re.split(r"[., ]", raw):
            digits = part.strip().lstrip("0")
            if digits:
                found.add(digits)
        joined = re.sub(r"[, ]", "", raw).rstrip(".").lstrip("0")
        if joined and "." not in joined:
            found.add(joined)
    return found


# ------------------------------------------------------------- typed facts --


@dataclass(frozen=True)
class DayFact:
    day: str
    text: str


@dataclass(frozen=True)
class VerdictFact:
    """A row of the rule engine's output. The only verdict that may be stated."""

    day: str
    activity: str
    label: str
    band: str
    score: int | None
    text: str | None


@dataclass(frozen=True)
class PlaceFact:
    """A venue. It has no date and can never be given one: a `places` row says
    that a building exists and what kind it is, and stops there."""

    id: str
    name: str
    category: str
    is_sample: bool


@dataclass(frozen=True)
class EventFact:
    """A dated listing from a named source. The only thing that may be
    described as scheduled."""

    id: str
    title: str
    category: str
    venue: str | None
    # Local calendar dates in the city's own timezone (see
    # queries.EVENTS_LOCALISED_SQL). `end_day` equals `day` for a single-day
    # event, so `days()` is one entry and the rendering stays a bare date.
    day: str
    end_day: str
    is_sample: bool

    def days(self) -> list[str]:
        start, end = date.fromisoformat(self.day), date.fromisoformat(self.end_day)
        if end < start:
            end = start
        return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]

    def when(self) -> str:
        """How a date is written in the prompt and in the answer."""
        return self.day if self.end_day == self.day else f"{self.day} to {self.end_day}"


@dataclass(frozen=True)
class BackgroundFact:
    title: str
    summary: str


@dataclass(frozen=True)
class Gap:
    """Something the question asked for that no row answers. Rendered by code
    and appended after the model, so it survives any wording."""

    subject: str
    text: str


@dataclass
class Brief:
    city: str
    window: str
    country: str = ""
    question: str = ""
    # Every day the question covers, ISO. Kept so a date in the answer can be
    # checked against something, and separate from `days` because a question
    # can cover a day the forecast has no row for.
    window_days: list[str] = field(default_factory=list)
    # Whether the question was scoped to the weather at all, and which of the
    # asked days a forecast row actually came back for. These narrow
    # `allowed_dates` and nothing else. `window_days` stays the whole asked
    # window on purpose: it is what the traveller typed and what our own gap
    # sentence prints, so it is vocabulary, and narrowing it there made the
    # figures in that sentence unquotable by the model we showed it to.
    weather_scoped: bool = False
    covered_days: list[str] = field(default_factory=list)
    uncovered_days: list[str] = field(default_factory=list)
    days: list[DayFact] = field(default_factory=list)
    verdicts: list[VerdictFact] = field(default_factory=list)
    places: list[PlaceFact] = field(default_factory=list)
    events: list[EventFact] = field(default_factory=list)
    facts: list[BackgroundFact] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    # What the question asked for, kept so the renderer and the validator agree
    # on what silence means.
    event_categories: list[str] = field(default_factory=list)
    interests: list[str] = field(default_factory=list)
    named_activities: list[str] = field(default_factory=list)
    unscored: list[str] = field(default_factory=list)

    def event_categories_present(self) -> set[str]:
        return {e.category for e in self.events}

    def venues(self) -> set[str]:
        return {e.venue.lower() for e in self.events if e.venue}

    def supporting_text(self) -> str:
        """Retrieved prose the model may paraphrase: the background summaries,
        and nothing else."""
        return " ".join(f"{f.title} {f.summary}" for f in self.facts).lower()

    def allowed_dates(self) -> set[str]:
        # For a question about the weather the window here is the days a
        # forecast row actually came back for, not the days that were asked
        # about: a half-expired snapshot must not leave the validator willing
        # to accept a sentence about a day with no data. The flag, not the
        # emptiness of the list, is what decides -- no covered day at all is a
        # real answer, and falling back to the asked window would restore the
        # hole. Only this allow-list narrows; `vocabulary` keeps the whole
        # asked window.
        window = self.covered_days if self.weather_scoped else self.window_days
        return (
            {d.day for d in self.days}
            # Every day a multi-day event runs, not only its first: naming the
            # 26th of a tournament that runs the 25th to the 27th is supported
            # by the row, and the validator must not read it as invented.
            | {day for e in self.events for day in e.days()}
            | {v.day for v in self.verdicts}
            | set(window)
        )

    def vocabulary(self) -> str:
        """Every scrap of retrieved text the answer may draw on, in one string.

        The traveller's own question is in it too: echoing back what they typed
        is never an invention.
        """
        return " ".join(
            [
                self.city,
                self.country,
                self.question,
                self.supporting_text(),
                *(d.day for d in self.days),
                *(d.text for d in self.days),
                *(p.name for p in self.places),
                *(p.category.replace("_", " ") for p in self.places),
                *(e.title for e in self.events),
                *(e.venue or "" for e in self.events),
                *(e.category for e in self.events),
                *(day for e in self.events for day in e.days()),
                *(v.day for v in self.verdicts),
                *(v.label for v in self.verdicts),
                *(v.band for v in self.verdicts),
                *(str(v.score) for v in self.verdicts if v.score is not None),
                *(v.text or "" for v in self.verdicts),
                *self.window_days,
            ]
        )

    def allowed_names(self) -> frozenset[str]:
        """Every word the answer is allowed to capitalise.

        A capitalised word from nowhere else -- "Roman", "Alfama", "European"
        -- is the model writing from its weights, which is the one thing it is
        not here to do.
        """
        return frozenset(t.lower() for t in _tokens(self.vocabulary())) | NAME_STOPWORDS

    def allowed_numbers(self) -> frozenset[str]:
        """Every quantity the rows carry, normalised.

        Scores are out of 100, so that is always allowed even when no verdict
        happens to sit at it.
        """
        return frozenset(_numbers_in(self.vocabulary())) | {"100"}


# ------------------------------------------------------------------ build --


def build(result: Retrieval) -> Brief:
    from .router import context_recommendations

    resolution = result.resolution
    city = resolution.city or {}
    brief = Brief(
        city=str(city.get("name") or "the city"),
        window=str(resolution.window) if resolution.window else "",
        country=str(city.get("country") or ""),
        question=resolution.question,
        window_days=[d.isoformat() for d in resolution.window.days()] if resolution.window else [],
        weather_scoped=result.weather_scoped,
        covered_days=list(result.covered_days),
        uncovered_days=list(result.uncovered_days),
        event_categories=list(getattr(resolution, "event_categories", []) or []),
        interests=list(resolution.interests),
        named_activities=list(resolution.activities),
        unscored=list(result.unscored_activities),
    )

    for row in result.forecast:
        parts = []
        if row.get("temp_max_c") is not None:
            parts.append(f"high {row['temp_max_c']:.0f}C")
        if row.get("temp_min_c") is not None:
            parts.append(f"low {row['temp_min_c']:.0f}C")
        if row.get("precip_mm") is not None:
            rain = f"rain {row['precip_mm']:.1f}mm"
            if row.get("precip_prob") is not None:
                rain += f" ({row['precip_prob']}% chance)"
            parts.append(rain)
        if row.get("wind_kmh") is not None:
            parts.append(f"wind {row['wind_kmh']:.0f}km/h")
        if row.get("sunshine_hours") is not None:
            parts.append(f"sun {row['sunshine_hours']:.1f}h")
        brief.days.append(DayFact(str(row["forecast_date"]), ", ".join(parts)))

    for row in context_recommendations(result):
        brief.verdicts.append(
            VerdictFact(
                day=str(row["forecast_date"]),
                activity=str(row["activity"]),
                label=str(row["activity_label"]),
                band=str(row["band"]),
                score=row.get("score"),
                text=row.get("text"),
            )
        )

    for row in result.places:
        brief.places.append(
            PlaceFact(
                id=str(row["id"]),
                name=str(row["name"]),
                category=str(row["category"]),
                is_sample=bool(row.get("is_sample")),
            )
        )

    for row in result.events:
        brief.events.append(
            EventFact(
                id=str(row["id"]),
                title=str(row["title"]),
                category=str(row["category"]),
                venue=row.get("venue"),
                # The local dates the query already derived in the city's
                # timezone. `build` reports the row; it does not re-date it,
                # and it no longer reads the UTC instant to guess a day (F2).
                day=str(row["starts_on"]),
                end_day=str(row.get("ends_on") or row["starts_on"]),
                is_sample=bool(row.get("is_sample")),
            )
        )

    for row in result.facts:
        brief.facts.append(BackgroundFact(str(row["title"]), str(row["summary"])))

    brief.gaps = _gaps(result, brief)
    return brief


def _stale_feed_sentence(result: Retrieval, category: str | None = None) -> str:
    """The clause that says a gap is an out-of-date feed, not an empty city.

    An event row is a reading of a listing page taken on a particular day, and
    an air-gapped run cannot find out what the venue did afterwards, so a
    reading stops being offered as a schedule once it is past its recheck date
    (migration 006). When that filter is the *reason* the answer is empty, the
    reader is owed the fact: "nothing on record" invites them to conclude the
    city is quiet, whereas "the listings we hold went out of date on the 15th"
    tells them the system, not the city, is the limit -- and what would fix it.

    `category` scopes the count to the gap the sentence is being attached to.
    It matters whenever a question asks about two kinds at once: asked about
    concerts and dance, a single total would quote the same number in both
    sentences, and for a category with no stored listing at all it would
    announce a stale listing that has never existed -- turning the honest
    sentence into precisely the confusion it was written to prevent. Passing no
    category asks about the question as a whole, which is what the "no
    scheduled event of any kind" gap wants.

    Returns "" when nothing was filtered out, so the ordinary wording is
    unchanged and no sentence appears without a number behind it.
    """
    stale = result.expired_events or {}
    if category is not None:
        stale = (stale.get("by_category") or {}).get(category) or {}
    expired = int(stale.get("expired") or 0)
    if not expired:
        return ""
    checked = stale.get("last_checked")
    when = f", last checked {str(checked)[:10]}," if checked else ""
    if expired == 1:
        return (
            f" One stored listing for those dates{when} is past its recheck date "
            "and is no longer reported as current."
        )
    return (
        f" {expired} stored listings for those dates{when} are past their recheck date "
        "and are no longer reported as current."
    )


def date_runs(days: list[str]) -> str:
    """Consecutive dates as ranges, everything else listed.

    A single first-to-last span is wrong the moment the missing days are not
    one block: asked about a week whose middle day failed to ingest, "no
    weather for 2026-09-25 to 2026-10-01" denies five days that are stored.
    Only genuinely consecutive dates are collapsed.
    """
    runs: list[list[date]] = []
    for value in sorted(date.fromisoformat(d) for d in days):
        if runs and value - runs[-1][-1] == timedelta(days=1):
            runs[-1].append(value)
        else:
            runs.append([value])
    return ", ".join(str(run[0]) if len(run) == 1 else f"{run[0]} to {run[-1]}" for run in runs)


def _partial_coverage_sentence(result: Retrieval, city: str) -> str:
    """Name the missing days, and explain the absence without overclaiming.

    The explanation has to match where the days sit relative to the stored
    window. Saying "the forecast ends on X" is true for days past the end and
    false for days before the start -- and for a day inside the window with no
    row it is not merely imprecise, it points at the wrong cause entirely.
    """
    missing = result.uncovered_days
    first = str(result.coverage.get("weather_first_date") or "")
    last = str(result.coverage.get("weather_last_date") or "")
    before = [d for d in missing if first and d < first]
    after = [d for d in missing if last and d > last]

    if first and last and after and not before and len(after) == len(missing):
        why = f"The stored forecast ends on {last}"
    elif first and last and before and not after and len(before) == len(missing):
        why = f"The stored forecast begins on {first}"
    elif first and last:
        # Either both ends, or a day inside the window that has no row for this
        # city -- `coverage` is a global MIN/MAX, so being inside it proves
        # nothing about this city.
        why = f"The stored forecast covers {first} to {last} and has no row for {city} on them"
    else:
        why = "No forecast is stored at all"

    return (
        f"No weather is stored for {date_runs(missing)} in {city}. {why}, so those days "
        f"are left out rather than guessed. Refresh the snapshot while connected to extend it."
    )


def _gaps(result: Retrieval, brief: Brief) -> list[Gap]:
    from .router import activity_meta

    city = result.resolution.city or {}
    gaps: list[Gap] = []
    asked_for_events = "events" in result.resolution.intents
    present = brief.event_categories_present()

    if asked_for_events:
        if brief.event_categories:
            for category in brief.event_categories:
                if category not in present:
                    # Scoped to this category: the count has to be about the
                    # kind of event the sentence is denying, or it is a number
                    # borrowed from a different question.
                    stale = _stale_feed_sentence(result, category)
                    gaps.append(
                        Gap(
                            f"events:{category}",
                            f"No {event_label(category)} is on record in {brief.city} for "
                            f"{brief.window}. That is the limit of the stored event feed, "
                            f"not evidence that none is scheduled.{stale}",
                        )
                    )
        elif not brief.events:
            gaps.append(
                Gap(
                    "events",
                    f"No scheduled event of any kind is on record in {brief.city} for "
                    f"{brief.window}. That is the limit of the stored event feed, not "
                    f"evidence that nothing is on.{_stale_feed_sentence(result)}",
                )
            )

    if result.uncovered_days:
        gaps.append(
            Gap("coverage:partial", _partial_coverage_sentence(result, brief.city)),
        )

    if result.resolution.categories and not brief.places:
        wanted = ", ".join(i.replace("_", " ") for i in brief.interests) or "those interests"
        gaps.append(Gap("places", f"No place is on record in {brief.city} for {wanted}."))

    meta = activity_meta()
    for key in brief.unscored:
        cfg = meta.get(key) or {}
        label = cfg.get("label", key.replace("_", " "))
        if cfg.get("requires_coast") and not city.get("coastal"):
            why = f"{brief.city} has no coast on record, so it is never scored there"
        else:
            why = f"no suitability score is stored for it in {brief.city}"
        # An activity the catalogue does not hold is a different gap from a
        # catalogue activity this city has no row for, and it has a remedy the
        # traveller can act on: the build scores anything typed against a
        # general outdoor-comfort measure, on request, through the write path.
        # Naming that route is the same courtesy the unknown-city refusal pays
        # ("I have weather and tourism data for: ..."). The ask route itself
        # stays read-only -- this sentence says where to ask, it does not ask.
        route = ""
        if key not in meta:
            route = (
                " It is not one of the activities I score by default. You can have it"
                ' scored against general outdoor comfort from the "Ask about a different'
                ' activity" form, or with POST /recommendations.'
            )
        gaps.append(Gap(f"activity:{key}", f"{label}: not on record -- {why}.{route}"))

    return gaps


# ------------------------------------------------------------- the prompt --


def prompt_block(brief: Brief) -> str:
    """The rows, typed, as the model sees them.

    Every section header states what kind of row it is and what that row does
    NOT establish. The model still gets this wrong sometimes, which is why
    `violations` exists -- but stating it here is what makes a good answer the
    common case rather than the lucky one.
    """
    lines = [f"City: {brief.city}", f"Dates asked about: {brief.window}"]

    if brief.days:
        lines.append("\nStored daily forecast:")
        lines.extend(f"  {day.day}: {day.text}" for day in brief.days)

    # Said before the rows rather than after them: the model has just been told
    # which dates were asked about, and without this the next thing it sees is
    # a shorter list of days with no explanation of why it is shorter.
    partial = [g for g in brief.gaps if g.subject == "coverage:partial"]
    if partial:
        lines.append("\nDATES WITH NO STORED WEATHER -- do not describe these days at all:")
        lines.extend(f"  {gap.text}" for gap in partial)

    activity_gaps = [g for g in brief.gaps if g.subject.startswith("activity:")]
    if activity_gaps:
        lines.append("\nASKED ABOUT BUT NOT ON RECORD -- say this plainly and explain nothing:")
        lines.extend(f"  {gap.text}" for gap in activity_gaps)
        lines.append(
            "  Do not give a verdict on these, and do not reason from the weather "
            "to one. State that there is no record and move on."
        )

    if brief.verdicts:
        # The negative matters as much as the header. One of the eighteen
        # catalogue labels is literally "An open-air music festival", and a
        # 1.7B model handed `2026-09-27 An open-air music festival: good
        # (85/100)` writes prose asserting a festival is on -- which check 1
        # then rejects, throwing away the whole answer. The PLACES block below
        # has carried its negative from the start; this one did not, and the
        # asymmetry is what made the assignment's own Rome question
        # non-deterministic.
        lines.append(
            "\nSuitability scores from the rule engine (these are the verdicts). Each one "
            "rates the stored weather for an activity the traveller could choose. None of "
            "them is a thing that is scheduled, on offer or being held: never write about "
            "one as though it were an event, and never invite anyone to attend one:"
        )
        for row in brief.verdicts:
            text = f" -- {row.text}" if row.text else ""
            lines.append(f"  {row.day} {row.label}: {row.band} ({row.score}/100){text}")

    if brief.places:
        lines.append(
            "\nPLACES on record -- buildings and venues, NOT scheduled events. For each "
            "one the system holds a name and a category and nothing else: no programme, "
            "no opening hours, no description, no idea whether anything is on there. "
            "Name them as places to consider; never say something is playing, showing "
            "or being served at one, and never describe what one is like or known for. "
            "In particular, do not repeat the traveller's own description back onto one: "
            "asked about fine dining, the rows say `restaurant` and nothing more, so the "
            "honest sentence is that these are the restaurants on record:"
        )
        for row in brief.places:
            sample = " [sample data]" if row.is_sample else ""
            lines.append(f"  {row.name} -- a {row.category.replace('_', ' ')}{sample}")

    if brief.events:
        lines.append(
            "\nSCHEDULED EVENTS on record -- the complete list for that city and those "
            "dates. Each keeps its category: a sport row is not a concert:"
        )
        for row in brief.events:
            sample = " [sample data]" if row.is_sample else ""
            venue = f" at {row.venue}" if row.venue else ""
            lines.append(f"  {row.when()} {row.title} ({row.category}){venue}{sample}")

    if brief.facts:
        lines.append(
            "\nBackground on record (the only prose you may paraphrase; add nothing to it):"
        )
        for row in brief.facts:
            lines.append(f"  {row.title}: {row.summary[:400]}")

    reported = [g for g in brief.gaps if not g.subject.startswith("activity:")]
    if reported:
        lines.append(
            "\nCOVERAGE GAPS -- these sentences are appended to your answer automatically. "
            "Do not repeat them, do not soften them, and do not contradict them. In "
            "particular do not restate one as a fact about the city: what is missing is "
            "missing from the record, which is not the same as not being on:"
        )
        lines.extend(f"  {gap.text}" for gap in reported)

    return "\n".join(lines)


def gap_block(brief: Brief, already_said: str = "") -> str:
    """Written in code, appended after the model has spoken.

    `already_said` drops a gap the answer states verbatim. The prompt asks the
    model not to repeat these and the small model sometimes does anyway, and
    printing the same sentence twice reads like a bug rather than like rigour.
    A paraphrase still gets the sentence appended -- the exact wording is the
    part that is guaranteed.
    """
    spoken = already_said.lower()
    gaps = [
        g
        for g in brief.gaps
        if not g.subject.startswith("activity:") and g.text.lower() not in spoken
    ]
    if not gaps:
        return ""
    return "Not on record: " + " ".join(g.text for g in gaps)


# ------------------------------------------------------- the plain answer --


def render(brief: Brief) -> str:
    """The whole answer, assembled from the typed facts by code.

    This is what the traveller gets when the model is down, when its output is
    unparseable, and when it says something the rows do not support. It is
    deliberately plain: every line is one row.
    """
    # The date range is only a heading when something in the answer is dated.
    # "Tell me about the history of Lisbon" is not a question about this week,
    # and printing a forecast window over the answer said it was.
    dated = bool(brief.days or brief.verdicts or brief.events)
    lines = [f"{brief.city}, {brief.window}:" if dated else f"{brief.city}:"]

    for row in brief.facts[:3]:
        lines.append(f"- {row.title}: {row.summary[:300]}")

    for day in brief.days[:8]:
        lines.append(f"- {day.day}: {day.text}")

    if brief.named_activities:
        for row in brief.verdicts:
            lines.append(f"- {row.day}: {row.label} is {row.band} ({row.score}/100)")
    else:
        best: dict[str, tuple[str, int]] = {}
        for row in brief.verdicts:
            if row.score is not None and row.score > best.get(row.day, ("", -1))[1]:
                best[row.day] = (row.label, row.score)
        for day, (label, score) in sorted(best.items())[:8]:
            lines.append(f"- {day}: best rated activity is {label} ({score}/100)")

    if brief.places:
        lines.append(
            "Places on record (venues only -- the system holds a name and a category, "
            "not a programme):"
        )
        by_category: dict[str, list[str]] = {}
        for row in brief.places:
            name = f"{row.name} [sample data]" if row.is_sample else row.name
            by_category.setdefault(row.category.replace("_", " "), []).append(name)
        for category, names in by_category.items():
            # Semicolons, because stored names contain commas of their own --
            # "St John's, Smith Square" is one concert hall, not two.
            more = f" (+{len(names) - 6} more)" if len(names) > 6 else ""
            lines.append(f"- {category}: {'; '.join(names[:6])}{more}")

    if brief.events:
        lines.append("Scheduled events on record:")
        for row in brief.events[:10]:
            venue = f" at {row.venue}" if row.venue else ""
            sample = " [sample data]" if row.is_sample else ""
            lines.append(f"- {row.when()} {row.title} ({row.category}){venue}{sample}")

    for gap in brief.gaps:
        lines.append(f"- {gap.text}")

    return "\n".join(lines)


# -------------------------------------------------------------- the check --


def violations(answer: str, brief: Brief) -> list[str]:
    """Sentences in `answer` that assert something no fact in `brief` carries.

    Checks, each written for a failure that was actually observed. They are
    numbered 1-12; 3b, 4b and 6b are variants of the check they sit beside,
    lettered rather than renumbered so a log line written last month still
    names the same check. All of them work on the model's prose only -- the gap
    block and the as-of footer are appended afterwards and are code's own
    words.

    10, 11 and 12 are the activity half. Until they were added, places and
    events were checked by name and an activity was only checked for existing:
    nothing compared an activity name in the sentence with the
    `VerdictFact.activity` and `.label` the brief already carried.

    The checks are deliberately narrow. A false positive costs a good answer
    its wording; it never costs the traveller a correct answer, because
    `render` says the same thing from the same rows.
    """
    # The model writes both apostrophes; `_says` matches one. Normalise once
    # rather than doubling every phrase list.
    answer = answer.replace("’", "'")
    found: list[str] = []
    present = brief.event_categories_present()
    venues = brief.venues()
    supporting = brief.supporting_text()
    claims = claim_words()
    allowed_dates = brief.allowed_dates()
    allowed_names = brief.allowed_names()
    allowed_numbers = brief.allowed_numbers()
    place_names = {p.name.lower(): p for p in brief.places if len(p.name) >= 5}
    event_titles = {e.title.lower(): e for e in brief.events if len(e.title) >= 5}
    # The same treatment for activities, which had none. Places and events have
    # been checked by name since this file was written; a verdict was only ever
    # checked for existing at all (check 4), so an answer could take one
    # activity's score and put another activity's name on it -- which is what
    # it did, returning "The suitability score for paragliding is good
    # (100/100)" off a `beach_day` row.
    #
    # Two dictionaries, because the two directions are different failures.
    # `scored_names` is what the rows do carry, and check 11 reads it.
    # `absent_activities` is every catalogue name the retrieval did NOT return,
    # and check 10 reads it. Keys already in `brief.unscored` are left out:
    # check 5 owns those and has its own two messages for them.
    scored_activities = {v.activity for v in brief.verdicts}
    verdict_spellings = {
        spelling for v in brief.verdicts for spelling in activity_spellings(v.activity, v.label)
    }
    absent_activities = {
        name: key
        for name, key in activity_names().items()
        if key not in scored_activities and key not in set(brief.unscored)
    }
    # Whether the question was about scheduled things, which is what lets check
    # 6b read a clause that says "nothing is on" without naming an event.
    asked_about_events = bool(brief.event_categories or brief.events)
    # Our own gap sentences, one at a time. The prompt shows the model these
    # and asks it not to repeat them; the small model sometimes repeats them
    # anyway, which is why `gap_block` already drops a verbatim repeat. Code's
    # own words cannot be the model's invention, and without this the dates and
    # figures they name -- which are precisely the days no row carries -- fail
    # checks 7 and 9, throw the whole answer away, and tell the operator the
    # rows do not support a sentence we wrote ourselves. A paraphrase is still
    # checked: only the wording we guarantee is exempt.
    own_words = {s.lower() for gap in brief.gaps for s in _sentences(gap.text)}

    for raw in _sentences(answer):
        if raw.lower() in own_words:
            continue
        # Checks 1-6 run per clause, so a negation in the tail of a sentence
        # cannot cover an assertion at its head.
        for clause in _clauses(raw):
            sentence = clause.lower()
            negated = bool(_NEGATION.search(sentence))
            # Two things about the clause that check 3b needs and the checks
            # around it already reason about in their own words: whether it
            # scopes itself to the stored record, and whether it is talking
            # about the forecast at all.
            on_record = any(_says(sentence, phrase) for phrase in RECORD_PHRASES)
            weather_said = any(_says(sentence, word) for word in WEATHER_WORDS)
            # "a concert hall" is a category, not a concert -- and neither is
            # an activity we retrieved a score for. Three of the eighteen
            # catalogue labels are named after an event kind, and the worst of
            # them, "An open-air music festival", used to make check 1 fire on
            # any honest sentence that named it: it is a suitability row, so no
            # festival event row exists, so the claim word looked unsupported.
            # That is what made the assignment's own Rome question fall back in
            # half its runs. Whole phrases only, exactly as the place masking
            # works, so "there is a festival on Saturday" still names a festival
            # and is still checked. What a clause may not do is say one of these
            # is *on* -- that is check 11, keyed on the predicate.
            scheduled = _without_activity_names(
                _without_place_categories(sentence), verdict_spellings
            )
            asserted = {
                category
                for category, words in claims.items()
                if any(_says(scheduled, word) for word in words)
            }
            named_places = [p for name, p in place_names.items() if _says(sentence, name)]
            named_events = [e for title, e in event_titles.items() if _says(sentence, title)]

            # 1. A scheduled event of a kind that has no row behind it.
            if not negated:
                for category in sorted(asserted - present):
                    found.append(f"claims a {event_label(category)} with no stored event row")

            # 2. A place offered as a scheduled event. A `places` row says the
            #    building exists; it says nothing about a programme.
            if asserted:
                for place in named_places:
                    if place.name.lower() not in venues:
                        found.append(f"presents the place {place.name!r} as a scheduled event")

            # 3. A stored event relabelled as another kind -- the Laver Cup, a
            #    tennis tournament, answering a question about concerts.
            for event in named_events:
                for category in sorted(asserted - {event.category}):
                    found.append(
                        f"describes {event.title!r} ({event.category}) as a "
                        f"{event_label(category)}"
                    )

            # 3b. A scheduled event placed on a day no event row covers.
            #
            #     Check 7 below cannot catch this, and that is the whole reason
            #     this one exists. Check 7 tests every date in the sentence
            #     against `allowed_dates()`, which unions the forecast days --
            #     so on any question with a forecast, every day in the window
            #     is already "allowed", and a weather row for the 26th makes
            #     "a concert on the 26th" look supported.
            #
            #     Observed on the assignment's own London question: from a
            #     single concert row on the 25th, the model answered "Concerts
            #     are scheduled on 2026-09-25, 2026-09-26, 2026-09-27,
            #     2026-09-29, and 2026-09-30" -- four invented listings, most
            #     likely read off the per-day suitability scores, which do
            #     exist for every day. Nothing in checks 1-3 fires: the
            #     category has rows, no place is named, no stored event is
            #     relabelled.
            #
            #     Only an event row can put an event on a day. `asserted &
            #     present` rather than `asserted`, because a category with no
            #     rows at all is check 1's job and should not be reported
            #     twice.
            #
            #     Three things keep it narrow, because a clause names a date
            #     for plenty of reasons that have nothing to do with an event.
            #     Only dates after the claim word count (`_claim_tail`): the
            #     clause is not split on a comma, so "2026-09-26 will be sunny,
            #     and a concert is on the 25th" would otherwise place a concert
            #     on the 26th. Weather phrasing after the event claim is cut
            #     off before its own date can be mistaken for an event date.
            #     A clause scoped to the record is left alone
            #     too: "between the 25th and the 30th the only concert on
            #     record is on the 25th" states the window and then the row,
            #     and both dates are honest.
            #
            #     Year-less dates and weekdays resolve only when the asked
            #     window identifies exactly one calendar day.
            if not negated and not on_record:
                for category in sorted(asserted & present):
                    covered = {
                        day
                        for event in brief.events
                        if event.category == category
                        for day in event.days()
                    }
                    tail = _claim_tail(scheduled, claims[category])
                    # A second weather assertion can carry its own date.
                    # Only dates before that assertion belong to the event.
                    weather_starts = [
                        m.start()
                        for word in WEATHER_WORDS
                        for m in re.finditer(rf"(?<!\w){re.escape(word)}(?!\w)", tail)
                    ]
                    if weather_starts:
                        # In "concert on the 25th, and the 26th stays dry",
                        # the second date precedes its weather adjective.
                        tail = re.split(r",\s+and\s+", tail, maxsplit=1)[0]
                        tail = tail[: min(weather_starts)]
                    for day in sorted(_dates_in(tail, brief.window_days) - covered):
                        found.append(
                            f"places a {event_label(category)} on {day}, "
                            "which no stored event row covers"
                        )

            # 4. A verdict where the rule engine gave none.
            verdict = any(_says(sentence, word) for word in VERDICT_WORDS)
            if verdict and not brief.verdicts:
                found.append("gives a suitability verdict with no stored score")

            # 4b. The weather described where no forecast row was retrieved.
            #
            #     Every other weather check needs a date to test. Check 7 tests
            #     the days a sentence names, and a sentence that names none
            #     walks past all of them: "Rome is warm and dry this week"
            #     against a brief holding no `DayFact` is the whole forecast
            #     invented, and it passed clean. The shape that produces it is
            #     ordinary rather than exotic -- `queries.coverage` is a global
            #     MIN/MAX, so the coverage gate lets the question through
            #     because some *other* city is still inside the window, the
            #     retrieval comes back with no row for this one, and the model
            #     writes the week from its weights.
            #
            #     Shaped like check 4 above: the trigger is the absence of the
            #     rows, not a bad value in the prose. The escape hatches are
            #     the ones our own gap sentence uses -- a negated clause and a
            #     clause scoped to the record are talking about the absence
            #     rather than asserting through it -- plus a weather word the
            #     retrieved background prose already carries, which is a
            #     paraphrase of a stored fact and not a forecast.
            #
            #     It is all-or-nothing on the rows, deliberately. A partially
            #     covered week has `DayFact`s, so an undated claim over it is
            #     not caught here; catching that needs a notion of which day a
            #     clause is about, which this file does not have and should not
            #     guess at.
            if not brief.days and not negated and not on_record:
                for word in WEATHER_WORDS:
                    if _says(sentence, word) and not _says(supporting, word):
                        found.append("describes the weather with no stored forecast row")
                        break

            # A claim about the entire asked period needs every day in that
            # period. An isolated, dated claim is checked against its row by
            # check 7 below; this catches the undated "warm all week" form.
            if (
                brief.weather_scoped
                and brief.uncovered_days
                and not negated
                and not on_record
                and weather_said
                and not _dates_in(sentence, brief.window_days)
                and any(
                    _says(sentence, scope)
                    for scope in (
                        "all week",
                        "this week",
                        "throughout the week",
                        "the whole week",
                        "every day",
                        "throughout the trip",
                        "the whole trip",
                    )
                )
            ):
                found.append("describes the whole period without weather for every day")

            # 5. A verdict on an activity this city has no row for. The
            #    observed shape is agreement followed by an invented
            #    weather-based reason, and it survived a check that looked only
            #    for verdict words: "which is not favorable for surfing" is the
            #    same answer in wording the list did not hold. So the weather
            #    itself is the trigger now. An activity with no score gets one
            #    sentence -- that there is no record -- and a clause naming it
            #    beside the forecast is reasoning towards the verdict either
            #    way, whether or not it lands on a word.
            for key in brief.unscored:
                if not _says(sentence, key.replace("_", " ")):
                    continue
                if verdict and not negated:
                    found.append(f"gives a verdict on {key}, which has no stored score")
                if any(_says(sentence, word) for word in WEATHER_WORDS):
                    found.append(f"reasons from the weather about {key}, which has no stored score")
                # The same shape, for conditions the system never measured at
                # all. "Visibility is fine for scuba diving" reaches none of the
                # weather words and is a stronger invention than any of them: it
                # is a reading of the water, taken from a land forecast, for an
                # activity with no row.
                for word in UNMEASURED_CONDITION_WORDS:
                    if _says(sentence, word):
                        found.append(
                            f"describes the {word} for {key}, which has no stored score and "
                            f"no measurement behind it"
                        )
                        break

            # 10. An activity named in a verdict sentence that no retrieved
            #     row scored. Keyed on the NAME, which is the thing no check
            #     did before -- and deliberately not on the verdict word alone,
            #     because `activities` is the router's fallback intent
            #     (`router.py`), so a verdict-word-keyed tightening of check 4
            #     would reject legitimate open wording such as "is tomorrow a
            #     good day to be outside in Rome?". A name makes the difference:
            #     open wording names no activity and cannot trip this.
            #
            #     Only when some verdict came back. The no-verdict case is
            #     check 4's, and reporting it twice would put two findings in a
            #     note that shows one.
            if brief.verdicts and verdict and not negated and not on_record:
                for name, key in absent_activities.items():
                    if _says(sentence, name) and name not in supporting:
                        found.append(f"gives a verdict on {key}, which no retrieved row scored")

            # 11. A suitability score narrated as a scheduled event -- the
            #     mirror of check 2, which does this for places, and the half of
            #     repair R2 that a prompt sentence alone cannot guarantee. One
            #     catalogue label is "An open-air music festival" and another is
            #     "A stand-up comedy show", so the wording is one word away, and
            #     the trip planner produced it: "a good day to attend the
            #     open-air music festival" on a day with no event row at all.
            #     A clause that names a stored event is left alone -- it is
            #     talking about that row, and checks 3 and 3b own it.
            said = schedule_claims(sentence) if not negated and not on_record else []
            if said and not named_events:
                for name in sorted(verdict_spellings):
                    if _says(sentence, name):
                        found.append(
                            f"presents the activity {name!r} as something scheduled "
                            f"({said[0]!r}); only an event row can say that"
                        )

            # 12. The traveller's own descriptor attached to a named place.
            #     Asked about fine dining, the answer named four rows that carry
            #     `category=restaurant` and nothing else -- one of them a
            #     24-hour cafe -- as fine dining. The prompt already forbade it
            #     and nothing enforced it. Check 6 could not: "is available at"
            #     is not a description word, and `DESCRIPTION_WORDS` holds verbs
            #     rather than the tiers a traveller types. A clause scoped to
            #     the record passes, which is the phrasing the prompt now asks
            #     for: "the restaurants on record are ...".
            if named_places and not negated and not on_record:
                for word in QUALITY_WORDS:
                    if not _says(sentence, word):
                        continue
                    for place in named_places:
                        if place.name.lower() in supporting:
                            continue
                        found.append(
                            f"calls the place {place.name!r} {word!r}, which its row "
                            f"does not say -- it carries only {place.category!r}"
                        )

            # 6. A description of a place we hold only a name and a category
            #    for.
            if named_places and any(_says(sentence, word) for word in DESCRIPTION_WORDS):
                for place in named_places:
                    if place.name.lower() not in supporting:
                        found.append(
                            f"describes the place {place.name!r} beyond its stored category"
                        )

            # 6b. An absence claimed about the world rather than about the
            #     record. "There are no concerts scheduled in London this week"
            #     and "the stored data indicates that no events are taking
            #     place in Rome" are both claims the feed cannot support: it
            #     covers a few venues for a few weeks, and its silence is a gap
            #     in coverage, not an empty city. The honest form is the one
            #     the gap sentences use -- "no concert is on record" -- and a
            #     clause that scopes itself that way passes.
            if (
                negated
                and _about_events(sentence, asked_about_events)
                and any(_says(sentence, word) for word in WORLD_SCHEDULE_WORDS)
                and not any(_says(sentence, phrase) for phrase in RECORD_PHRASES)
            ):
                found.append("claims nothing is scheduled, rather than nothing being on record")

        # 7. A calendar day no row carries. An event moved by a day is a worse
        #    answer than no answer, because it reads as confirmed.
        # A record-scoped absence may name the missing date honestly. It does
        # not turn that date into a forecast claim.
        unsupported: set[str] = set()
        for clause in _clauses(raw):
            wording = clause.lower()
            dates = _dates_in(clause, brief.window_days) - allowed_dates
            record_weather_gap = (
                bool(_NEGATION.search(wording))
                and any(_says(wording, word) for word in WEATHER_WORDS)
                and (
                    any(_says(wording, phrase) for phrase in RECORD_PHRASES)
                    or _says(wording, "stored weather")
                )
            )
            if record_weather_gap:
                dates -= set(brief.uncovered_days)
            unsupported.update(dates)
        for day in sorted(unsupported):
            found.append(f"states the date {day}, which no retrieved row carries")

        # 8. A proper noun that came from the model's weights rather than from
        #    a row. This is what caught the Lisbon answer inventing a Roman
        #    era, an Alfama district and a colonial period from a one-line
        #    summary that mentioned none of them.
        for index, token in enumerate(_tokens(raw)):
            if index == 0 or len(token) < 4 or not token[0].isupper():
                continue
            if token.lower() not in allowed_names:
                found.append(f"names {token!r}, which appears in no retrieved row")

        # 9. A quantity from the same place. Asked about the history of Lisbon
        #    the model wrote "a history dating back over 2,000 years" from a
        #    summary that gives a population and a river and no age at all.
        #    Smaller invented quantities are facts too: "a 40-year tradition"
        #    needs support just as much as "2,000 years" does. The question,
        #    requested dates and rendered measurements are in the vocabulary.
        for number in sorted(_numbers_in(raw)):
            if number not in allowed_numbers:
                found.append(f"states the figure {number}, which no retrieved row carries")

    # Stable and deduplicated: this string ends up in a log line and a note.
    return sorted(set(found))
