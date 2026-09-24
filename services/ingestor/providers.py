"""Weather providers.

One interface, one implementation. Open-Meteo was chosen over OpenWeather
because it needs no API key -- which means the air-gapped bundle has no secret
to smuggle and the connected refresh has no account to expire -- and because
its free tier includes the 16-day daily forecast the brief's "this week"
questions need. The interface exists so that swapping it is a new file, not a
rewrite; the README records the comparison.

There is no marine provider, and that is a measured decision rather than an
assumed one. Open-Meteo publishes a second keyless endpoint at
``https://marine-api.open-meteo.com/v1/marine``, and it was probed on
2026-09-25 against the four coast reference points ``data/cities.yml`` names.
All four answered ``200`` with real values -- ``wave_height``,
``swell_wave_height``, ``swell_wave_period``, ``wind_wave_height`` and
``sea_surface_temperature`` -- so the near-shore grid does resolve Ostia,
Carcavelos, Gordon Beach and Nautholsvik. Two properties are why it is still
not ingested:

* **Horizon.** ``forecast_days=16`` is accepted and returns 384 hourly slots,
  but only the first 240 carry values: the wave model runs 10 days, against
  the 16 days of land forecast this system stores. Six of every sixteen days
  would have no sea data at all, so the ``score_ceiling`` in
  ``data/activities.yml`` would still be needed for them. Marine data narrows
  that gap; it does not close it.
* **Where the answer comes from.** The service replies from its own grid cell,
  not the point asked for: Ostia 2.9 km away, Carcavelos 4.6 km, Gordon Beach
  7.2 km, Nautholsvik 13.2 km. That is a second "this was not measured where
  you think" caveat stacked on the forecast-point distance the consumer
  already stores.

Against that, ingesting it completely -- a snapshot file and a manifest entry,
a table and a migration, a consumer write path, the connected refresh, scoring
and tests -- is not a small change, and ``scripts/snapshot_manifest.py``
requires every snapshot entity to cover every configured city, which marine
data for a five-city set including London cannot do. A half-integrated wave
feed, stored but unscored or scored but unrefreshable, would be worse than the
honest cap. The probe is recorded here so the next person weighs the same
numbers instead of re-deriving them.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Protocol

import requests

log = logging.getLogger(__name__)

# Overridable for the same reason MODEL_BASE_URL is: an on-prem site refreshes
# from an internal mirror rather than from the public internet. It is also what
# the forced-failure drill points at an unreachable address to prove that a
# refresh which fetches nothing still closes the egress window.
# `or`, not a default argument: an empty override means "not set", never an
# empty URL -- a blank value would otherwise fail every city at once.
OPEN_METEO_URL = os.environ.get("OPEN_METEO_URL") or "https://api.open-meteo.com/v1/forecast"

DAILY_FIELDS = [
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_sum",
    "precipitation_probability_max",
    "wind_speed_10m_max",
    "uv_index_max",
    "sunshine_duration",
    "sunrise",
    "sunset",
    "weather_code",
]


class WeatherProvider(Protocol):
    name: str

    def daily_forecast(self, city: dict[str, Any], days: int) -> list[dict[str, Any]]:
        """Return one payload per day, already shaped like schemas.WeatherDaily."""


class OpenMeteo:
    name = "open-meteo"

    def __init__(self, *, url: str = OPEN_METEO_URL, timeout: float = 20.0):
        self.url = url
        self.timeout = timeout

    def daily_forecast(self, city: dict[str, Any], days: int = 16) -> list[dict[str, Any]]:
        params = {
            "latitude": city["lat"],
            "longitude": city["lon"],
            "daily": ",".join(DAILY_FIELDS),
            "timezone": city.get("timezone", "UTC"),
            "forecast_days": days,
        }
        response = requests.get(self.url, params=params, timeout=self.timeout)
        response.raise_for_status()
        body = response.json()
        return self._to_payloads(city, body, response.url)

    # Parsing is separated from fetching so the unit tests can feed it a saved
    # response and assert the shape without any network.
    def _to_payloads(
        self, city: dict[str, Any], body: dict[str, Any], source_url: str
    ) -> list[dict[str, Any]]:
        daily = body.get("daily") or {}
        dates = daily.get("time") or []
        offset = int(body.get("utc_offset_seconds", 0))
        tz = timezone(timedelta(seconds=offset))
        # The provider stamps no generation time, so the fetch is the as-of.
        as_of = datetime.now(UTC).isoformat()

        def at(key: str, index: int):
            values = daily.get(key)
            if not values or index >= len(values):
                return None
            return values[index]

        def local(key: str, index: int) -> str | None:
            value = at(key, index)
            if not value:
                return None
            return datetime.fromisoformat(value).replace(tzinfo=tz).isoformat()

        payloads = []
        for i, day in enumerate(dates):
            sunshine = at("sunshine_duration", i)
            payloads.append(
                {
                    "city_id": city["slug"],
                    "forecast_date": day,
                    "provider": self.name,
                    "temp_max_c": at("temperature_2m_max", i),
                    "temp_min_c": at("temperature_2m_min", i),
                    "precip_mm": at("precipitation_sum", i),
                    "precip_prob": at("precipitation_probability_max", i),
                    "wind_kmh": at("wind_speed_10m_max", i),
                    "uv_index": at("uv_index_max", i),
                    # Open-Meteo reports sunshine in seconds; the schema is hours.
                    "sunshine_hours": None if sunshine is None else round(sunshine / 3600, 2),
                    "sunrise": local("sunrise", i),
                    "sunset": local("sunset", i),
                    "weather_code": at("weather_code", i),
                    "source_url": source_url,
                    "as_of": as_of,
                }
            )
        return payloads


def get_provider(name: str = "open-meteo") -> WeatherProvider:
    if name == "open-meteo":
        return OpenMeteo()
    raise ValueError(f"unknown weather provider: {name}")
