"""Payload models, one per routing key.

The consumer validates every payload against the model for its routing key
before it touches the database. A payload that fails here is a poison message:
it is dead-lettered with the validation error, never retried forever and never
half-written.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import config

__all__ = [
    "ValidationError",
    "PAYLOAD_MODELS",
    "validate",
    "slugify",
    "ACTIVITY_FILLER_WORDS",
    "MIN_ACTIVITY_SLUG_CHARS",
]

# The shortest slug that may be treated as an activity, on either route.
#
# It has to live beside `slugify` for the same reason the filler list does. The
# write path used to enforce its floor with `Field(min_length=2)` on the text the
# traveller typed, and the read path enforced its own on the slug -- which agreed
# only while slugify preserved length. Once slugify started trimming filler they
# stopped agreeing: `POST /recommendations` with "a x" passed `min_length=2`,
# stored a row under the one-character slug `x`, and the read path then discarded
# `x` as too short, so the row was unreachable by the question that asked for it.
# That is the `a_picnic` defect again, one trim later.
MIN_ACTIVITY_SLUG_CHARS = 2

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Words that carry no activity meaning of their own and that a name may open or
# close with. They are dropped so that one activity has one key, however it was
# typed.
#
# This list lives here, beside `slugify`, because it has to be the SAME list on
# both routes. The agent's extraction already skipped opening filler -- "is
# tomorrow a good day for a picnic?" yields the candidate "picnic" -- while the
# write path slugified the form field verbatim. So `POST /recommendations` with
# "a picnic" stored `a_picnic`, and the question asking about it looked up
# `picnic`, found nothing, and reported an activity the user had just had scored
# as not on record. `services/agent/router.py` imports this name rather than
# keeping its own copy, so the two lists cannot drift apart again.
ACTIVITY_FILLER_WORDS: frozenset[str] = frozenset(
    """
    a an the some any go going goes gone went do doing does be being been
    have has had get getting got take taking try trying enjoy enjoying
    my our your out
    """.split()
)


def slugify(text: str) -> str:
    """Free-text activity -> a stable key. 'Fine Dining!' -> 'fine_dining'.

    Leading and trailing filler is dropped, so "a picnic", "go picnic" and
    "picnic" are one activity rather than three. Only the ends are trimmed:
    filler inside a name is part of it ("watch the sunset"), and removing it
    would merge names that are genuinely different.

    Raises `ValueError` when nothing is left -- "the", "a go" -- which
    `POST /recommendations` already turns into a 422 rather than storing a row
    keyed on a filler word.
    """
    words = [word for word in _SLUG_RE.split(text.strip().lower()) if word]
    while words and words[0] in ACTIVITY_FILLER_WORDS:
        words.pop(0)
    while words and words[-1] in ACTIVITY_FILLER_WORDS:
        words.pop()
    slug = "_".join(words)
    if not slug:
        raise ValueError("activity is empty after normalisation")
    return slug[:64]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WeatherDaily(_Payload):
    city_id: str
    forecast_date: date
    provider: str
    temp_min_c: float | None = None
    temp_max_c: float | None = None
    precip_mm: float | None = None
    precip_prob: int | None = None
    wind_kmh: float | None = None
    uv_index: float | None = None
    sunshine_hours: float | None = None
    sunrise: datetime | None = None
    sunset: datetime | None = None
    weather_code: int | None = None
    source_url: str | None = None
    as_of: datetime


class Place(_Payload):
    id: str
    city_id: str
    name: str
    category: str
    lat: float | None = None
    lon: float | None = None
    address: str | None = None
    source: str
    source_url: str | None = None
    is_sample: bool = False
    as_of: datetime


class Fact(_Payload):
    id: str
    city_id: str
    title: str
    summary: str
    topic: str = "history"
    source: str
    source_url: str | None = None
    is_sample: bool = False
    as_of: datetime


class Event(_Payload):
    """A dated listing, and the two timestamps that say how far to trust it.

    `checked_at` is when somebody last opened this row's `source_url` and read
    the title, the date and the hall back off it. `valid_until` is when that
    reading stops counting: a listing can be cancelled, moved or rescheduled
    after it was checked, and nothing in an air-gapped run can notice. Past
    `valid_until` the row is still stored and still shown in the coverage
    panel, but it is no longer offered as a currently scheduled event -- see
    `queries.events`, which filters on it by default.

    Both are required rather than optional. A row with no checked-at date is
    asserted rather than verified, and a row with no expiry is a claim about a
    schedule that nobody has undertaken to re-check.
    """

    id: str
    city_id: str
    title: str
    category: str
    venue: str | None = None
    starts_at: datetime
    ends_at: datetime | None = None
    source: str
    source_url: str | None = None
    is_sample: bool = False
    as_of: datetime
    checked_at: datetime
    valid_until: datetime


class RecommendationRequest(_Payload):
    """A user asked for an activity that is not one of the scored defaults."""

    city_id: str
    forecast_date: date
    activity: str
    activity_label: str


class LlmRecommendation(_Payload):
    """What the enricher produced, on its way back in through the queue.

    `invalid` means this attempt produced unusable output. The consumer counts
    those, and only the count -- not the enricher's memory -- decides when a row
    gives up, so a restarted enricher cannot reset the cap.
    """

    city_id: str
    forecast_date: date
    activity: str
    status: str = Field(pattern="^(ready|failed|invalid)$")
    text: str | None = None
    model: str | None = None
    error: str | None = None
    weather_as_of: datetime | None = None


class Itinerary(_Payload):
    id: str
    city_id: str
    title: str
    start_date: date
    end_date: date
    days: list[dict[str, Any]] = Field(default_factory=list)
    # The as-of of the forecast the plan's scores were computed from. Supplied
    # by the caller, because only the caller knows which snapshot it scored
    # against; `None` when it did not say. Nullable rather than defaulted,
    # because the alternative is stamping a plan with a timestamp nothing
    # scored it against -- an invented provenance is worse than a missing one,
    # and the UI can say "not recorded" but cannot un-mislead a wrong date.
    as_of: datetime | None = None


class ItineraryDelete(_Payload):
    id: str


class UserDataWipe(_Payload):
    requested_by: str = "ui"


class ReenrichRequest(_Payload):
    """M12, third update path: re-word stored recommendations.

    Every field is optional and narrows the selection; an empty request means
    "everything". `include_deferred` is what promotes rows the consumer ranked
    out of the wording queue, so an activity nobody has asked about can still
    be worded on demand.
    """

    city_id: str | None = None
    forecast_date: date | None = None
    activity: str | None = None
    include_deferred: bool = False
    requested_by: str = "ui"


class RecordPatch(_Payload):
    """M12: a user correction to a stored record, travelling like any other."""

    entity: str = Field(pattern="^(places|facts|events|itineraries|weather_daily)$")
    entity_id: str
    fields: dict[str, Any]
    edited_by: str = "ui"


class RecordRetraction(_Payload):
    """Withdraw one collected record from the published output.

    Only the three collected entities can be retracted. Weather is replaced
    day by day rather than withdrawn, a recommendation is derived from a rule
    and a forecast rather than collected, and an itinerary is the user's own
    and already has a delete of its own (`ItineraryDelete`).

    `reason` is required and has no default on purpose. A withdrawal is a
    claim that something we published was wrong, and it is the one kind of
    change nobody can reconstruct afterwards from the data itself -- the row
    it refers to still looks exactly as it did. One line saying which source
    said what, on what date, is the whole of the audit trail.

    `retracted_at` is when the decision was taken, supplied by whoever took
    it, never `now()` at the consumer: replaying the queue must not restamp a
    withdrawal with the clock of the replay.
    """

    entity: str = Field(pattern="^(events|places|facts)$")
    entity_id: str
    reason: str = Field(min_length=1)
    retracted_at: datetime
    retracted_by: str = "operator"


PAYLOAD_MODELS: dict[str, type[_Payload]] = {
    config.RK_WEATHER: WeatherDaily,
    config.RK_PLACE: Place,
    config.RK_FACT: Fact,
    config.RK_EVENT: Event,
    config.RK_RECOMMENDATION_REQUEST: RecommendationRequest,
    config.RK_LLM_RECOMMENDATION: LlmRecommendation,
    config.RK_ITINERARY: Itinerary,
    config.RK_ITINERARY_DELETE: ItineraryDelete,
    config.RK_USER_DATA_WIPE: UserDataWipe,
    config.RK_PATCH: RecordPatch,
    config.RK_REENRICH: ReenrichRequest,
    config.RK_RETRACT: RecordRetraction,
}


def validate(routing_key: str, payload: dict[str, Any]) -> _Payload:
    model = PAYLOAD_MODELS.get(routing_key)
    if model is None:
        raise ValueError(f"unknown routing key: {routing_key}")
    return model.model_validate(payload)
