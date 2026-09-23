"""Weather providers.

One interface, one implementation. Open-Meteo was chosen over OpenWeather
because it needs no API key -- which means the air-gapped bundle has no secret
to smuggle and the connected refresh has no account to expire -- and because
its free tier includes the 16-day daily forecast the brief's "this week"
questions need. The interface exists so that swapping it is a new file, not a
rewrite; the README records the comparison.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Protocol

import requests

log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

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
