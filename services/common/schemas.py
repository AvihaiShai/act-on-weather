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

__all__ = ["ValidationError", "PAYLOAD_MODELS", "validate", "slugify"]

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Free-text activity -> a stable key. 'Fine Dining!' -> 'fine_dining'."""
    slug = _SLUG_RE.sub("_", text.strip().lower()).strip("_")
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
    as_of: datetime


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


PAYLOAD_MODELS: dict[str, type[_Payload]] = {
    config.RK_WEATHER: WeatherDaily,
    config.RK_PLACE: Place,
    config.RK_FACT: Fact,
    config.RK_EVENT: Event,
    config.RK_RECOMMENDATION_REQUEST: RecommendationRequest,
    config.RK_LLM_RECOMMENDATION: LlmRecommendation,
    config.RK_ITINERARY: Itinerary,
    config.RK_PATCH: RecordPatch,
    config.RK_REENRICH: ReenrichRequest,
}


def validate(routing_key: str, payload: dict[str, Any]) -> _Payload:
    model = PAYLOAD_MODELS.get(routing_key)
    if model is None:
        raise ValueError(f"unknown routing key: {routing_key}")
    return model.model_validate(payload)
