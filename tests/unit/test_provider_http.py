"""Exercise the real Open-Meteo adapter with a local HTTP response stub."""

import pytest
import requests

from services.common.schemas import WeatherDaily
from services.ingestor import providers

CITY = {"slug": "lisbon", "lat": 38.72, "lon": -9.14, "timezone": "Europe/Lisbon"}


def test_request_parameters_and_response_become_valid_weather_rows(monkeypatch):
    calls = []

    class Response:
        url = "https://api.open-meteo.com/v1/forecast?forecast_days=2"

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "utc_offset_seconds": 3600,
                "daily": {
                    "time": ["2026-09-25", "2026-09-26"],
                    "temperature_2m_max": [23.0, 24.0],
                    "sunshine_duration": [3600, 7200],
                    "sunrise": ["2026-09-25T07:00", "2026-09-26T07:01"],
                },
            }

    def get(url, *, params, timeout):
        calls.append((url, params, timeout))
        return Response()

    monkeypatch.setattr(providers.requests, "get", get)
    rows = providers.OpenMeteo(timeout=4).daily_forecast(CITY, days=2)

    assert len(calls) == 1
    url, params, timeout = calls[0]
    assert url == providers.OPEN_METEO_URL
    assert params["timezone"] == "Europe/Lisbon"
    assert params["forecast_days"] == 2
    assert timeout == 4
    assert len(rows) == 2
    assert rows[0]["city_id"] == "lisbon"
    assert rows[0]["sunshine_hours"] == 1.0
    assert WeatherDaily.model_validate(rows[0]).sunrise.utcoffset().total_seconds() == 3600
    assert all(WeatherDaily.model_validate(row).source_url == Response.url for row in rows)


def test_http_failure_reaches_refresh_as_a_city_failure(monkeypatch):
    class Response:
        def raise_for_status(self):
            raise requests.HTTPError("provider returned 503")

    monkeypatch.setattr(providers.requests, "get", lambda *_a, **_k: Response())
    with pytest.raises(requests.HTTPError, match="503"):
        providers.OpenMeteo().daily_forecast(CITY, days=2)
