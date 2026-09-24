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

_NEGATION = re.compile(r"(?<!\w)(no|not|none|never|without|nor|nothing|lacks?)(?!\w)|n't")


def _says(text: str, phrase: str) -> bool:
    """Whole-word match; `phrase` may contain spaces."""
    return re.search(rf"(?<!\w){re.escape(phrase.lower())}(?!\w)", text) is not None


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


_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTHS = (
    "january february march april may june july august september october november december"
).split()
_LONG_DATE = re.compile(
    r"\b(?:(\d{1,2})\s+(" + "|".join(_MONTHS) + r")|(" + "|".join(_MONTHS) + r")\s+(\d{1,2}))"
    r"(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)


def _dates_in(sentence: str) -> set[str]:
    """Every calendar day a sentence names, in ISO form.

    Both spellings the model uses -- "2026-09-25" and "September 25, 2026" --
    because a date the rows do not carry is the same invention either way.
    """
    found = {f"{y}-{m}-{d}" for y, m, d in _ISO_DATE.findall(sentence)}
    for day_first, month_a, month_b, day_second, year in _LONG_DATE.findall(sentence):
        if not year:
            continue
        month = (month_a or month_b).lower()
        day = day_first or day_second
        found.add(f"{year}-{_MONTHS.index(month) + 1:02d}-{int(day):02d}")
    return found


def _tokens(sentence: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z'’-]*", sentence)


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
    day: str
    is_sample: bool


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
        return (
            {d.day for d in self.days}
            | {e.day for e in self.events}
            | {v.day for v in self.verdicts}
            | set(self.window_days)
        )

    def allowed_names(self) -> frozenset[str]:
        """Every word the answer is allowed to capitalise.

        Built from the rows themselves plus the traveller's own question, so
        echoing what they typed is never an invention. A capitalised word from
        nowhere else -- "Roman", "Alfama", "European" -- is the model writing
        from its weights, which is the one thing it is not here to do.
        """
        vocabulary = " ".join(
            [
                self.city,
                self.country,
                self.question,
                self.supporting_text(),
                *(p.name for p in self.places),
                *(p.category.replace("_", " ") for p in self.places),
                *(e.title for e in self.events),
                *(e.venue or "" for e in self.events),
                *(e.category for e in self.events),
                *(v.label for v in self.verdicts),
                *(v.band for v in self.verdicts),
                *(v.text or "" for v in self.verdicts),
            ]
        )
        return frozenset(t.lower() for t in _tokens(vocabulary)) | NAME_STOPWORDS


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
                # Rendered from whatever the row carries. Which calendar day a
                # stored timestamp belongs to is F2's question, not this
                # module's: `build` reports the row, it does not re-date it.
                day=str(row["starts_at"].date()),
                is_sample=bool(row.get("is_sample")),
            )
        )

    for row in result.facts:
        brief.facts.append(BackgroundFact(str(row["title"]), str(row["summary"])))

    brief.gaps = _gaps(result, brief)
    return brief


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
                    gaps.append(
                        Gap(
                            f"events:{category}",
                            f"No {event_label(category)} is on record in {brief.city} for "
                            f"{brief.window}. That is the limit of the stored event feed, "
                            f"not evidence that none is scheduled.",
                        )
                    )
        elif not brief.events:
            gaps.append(
                Gap(
                    "events",
                    f"No scheduled event of any kind is on record in {brief.city} for "
                    f"{brief.window}. That is the limit of the stored event feed, not "
                    f"evidence that nothing is on.",
                )
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
        gaps.append(Gap(f"activity:{key}", f"{label}: not on record -- {why}."))

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

    activity_gaps = [g for g in brief.gaps if g.subject.startswith("activity:")]
    if activity_gaps:
        lines.append("\nASKED ABOUT BUT NOT ON RECORD -- say this plainly and explain nothing:")
        lines.extend(f"  {gap.text}" for gap in activity_gaps)
        lines.append(
            "  Do not give a verdict on these, and do not reason from the weather "
            "to one. State that there is no record and move on."
        )

    if brief.verdicts:
        lines.append("\nSuitability scores from the rule engine (these are the verdicts):")
        for row in brief.verdicts:
            text = f" -- {row.text}" if row.text else ""
            lines.append(f"  {row.day} {row.label}: {row.band} ({row.score}/100){text}")

    if brief.places:
        lines.append(
            "\nPLACES on record -- buildings and venues, NOT scheduled events. For each "
            "one the system holds a name and a category and nothing else: no programme, "
            "no opening hours, no description, no idea whether anything is on there. "
            "Name them as places to consider; never say something is playing, showing "
            "or being served at one, and never describe what one is like or known for:"
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
            lines.append(f"  {row.day} {row.title} ({row.category}){venue}{sample}")

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
            "Do not repeat them, do not soften them, and do not contradict them:"
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
            lines.append(f"- {row.day} {row.title} ({row.category}){venue}{sample}")

    for gap in brief.gaps:
        lines.append(f"- {gap.text}")

    return "\n".join(lines)


# -------------------------------------------------------------- the check --


def violations(answer: str, brief: Brief) -> list[str]:
    """Sentences in `answer` that assert something no fact in `brief` carries.

    Six checks, each written for a failure that was actually observed. All of
    them work on the model's prose only -- the gap block and the as-of footer
    are appended afterwards and are code's own words.

    The checks are deliberately narrow. A false positive costs a good answer
    its wording; it never costs the traveller a correct answer, because
    `render` says the same thing from the same rows.
    """
    found: list[str] = []
    present = brief.event_categories_present()
    venues = brief.venues()
    supporting = brief.supporting_text()
    claims = claim_words()
    allowed_dates = brief.allowed_dates()
    allowed_names = brief.allowed_names()
    place_names = {p.name.lower(): p for p in brief.places if len(p.name) >= 5}
    event_titles = {e.title.lower(): e for e in brief.events if len(e.title) >= 5}

    for raw in _sentences(answer):
        # Checks 1-6 run per clause, so a negation in the tail of a sentence
        # cannot cover an assertion at its head.
        for clause in _clauses(raw):
            sentence = clause.lower()
            negated = bool(_NEGATION.search(sentence))
            # "a concert hall" is a category, not a concert.
            scheduled = _without_place_categories(sentence)
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

            # 4. A verdict where the rule engine gave none.
            verdict = any(_says(sentence, word) for word in VERDICT_WORDS)
            if verdict and not brief.verdicts:
                found.append("gives a suitability verdict with no stored score")

            # 5. A verdict on an activity this city has no row for. The
            #    observed shape is agreement followed by an invented
            #    weather-based reason.
            if verdict and not negated:
                for key in brief.unscored:
                    if _says(sentence, key.replace("_", " ")):
                        found.append(f"gives a verdict on {key}, which has no stored score")

            # 6. A description of a place we hold only a name and a category
            #    for.
            if named_places and any(_says(sentence, word) for word in DESCRIPTION_WORDS):
                for place in named_places:
                    if place.name.lower() not in supporting:
                        found.append(
                            f"describes the place {place.name!r} beyond its stored category"
                        )

        # 7. A calendar day no row carries. An event moved by a day is a worse
        #    answer than no answer, because it reads as confirmed.
        for day in sorted(_dates_in(raw) - allowed_dates):
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

    # Stable and deduplicated: this string ends up in a log line and a note.
    return sorted(set(found))
