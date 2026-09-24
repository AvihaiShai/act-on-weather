"""The Forecast cards use the next city-local day, including at day boundaries."""

import sys
from datetime import UTC, date, datetime
from pathlib import Path

UI_DIR = Path(__file__).resolve().parents[2] / "services" / "ui"
sys.path.insert(0, str(UI_DIR))

import forecast  # noqa: E402


def test_next_forecast_uses_local_day_and_skips_past_rows():
    rows = [
        {"forecast_date": "2026-09-25", "temp_max_c": 25},
        {"forecast_date": "2026-09-23", "temp_max_c": 33},
        {"forecast_date": "2026-09-24", "temp_max_c": 28},
    ]
    instant = datetime(2026, 9, 23, 22, 30, tzinfo=UTC)
    assert forecast.next_row(rows, "Europe/Lisbon", now=instant)["temp_max_c"] == 33
    assert forecast.next_row(rows, "Asia/Jerusalem", now=instant)["temp_max_c"] == 28
    assert (
        forecast.next_row(rows, "Asia/Jerusalem", now=datetime(2026, 9, 25, tzinfo=UTC))[
            "temp_max_c"
        ]
        == 25
    )
    assert forecast.next_row(rows, "Asia/Jerusalem", now=datetime(2026, 9, 26, tzinfo=UTC)) is None


def _window():
    return [
        {
            "forecast_date": "2026-09-24",
            "temp_min_c": 18.6,
            "temp_max_c": 33.3,
            "precip_mm": 0.0,
            "wind_kmh": 15.0,
        },
        {
            "forecast_date": "2026-09-25",
            "temp_min_c": 17.2,
            "temp_max_c": 28.6,
            "precip_mm": 0.0,
            "wind_kmh": 21.1,
        },
        {
            "forecast_date": "2026-09-29",
            "temp_min_c": 12.5,
            "temp_max_c": 20.9,
            "precip_mm": 9.9,
            "wind_kmh": 21.1,
        },
        {
            "forecast_date": "2026-10-05",
            "temp_min_c": 17.9,
            "temp_max_c": 22.3,
            "precip_mm": 18.0,
            "wind_kmh": 18.5,
        },
    ]


def test_each_highlight_card_names_its_own_day():
    cards = forecast.highlights(_window(), date(2026, 9, 24))
    assert [c["label"] for c in cards] == [
        "Warmest · today",  # 33.3 on the 24th
        "Coldest · Tue 29 Sep",  # 12.5 on the 29th
        "Next rain · Tue 29 Sep",  # first wet day, not the wettest
        "Windiest · tomorrow",  # 21.1 tie -> soonest day wins
    ]
    assert [c["value"] for c in cards] == ["33°C", "12°C", "9.9 mm", "21 km/h"]


def test_dry_window_reports_no_rain_instead_of_zero():
    dry = [row | {"precip_mm": 0.0} for row in _window()]
    rain = forecast.highlights(dry, date(2026, 9, 24))[2]
    assert rain == {
        "label": "Next rain",
        "value": "None",
        "help": "First day with rain in the stored window",
    }


def test_highlights_ignore_days_before_the_city_today():
    rows = _window() + [
        {
            "forecast_date": "2026-09-23",
            "temp_min_c": 2.0,
            "temp_max_c": 40.0,
            "precip_mm": 99.0,
            "wind_kmh": 99.0,
        }
    ]
    window = forecast.upcoming_rows(rows, "Europe/Lisbon", now=datetime(2026, 9, 24, 9, tzinfo=UTC))
    assert "2026-09-23" not in [row["forecast_date"] for row in window]
    assert forecast.highlights(window, date(2026, 9, 24))[0]["value"] == "33°C"


def test_a_blank_day_past_the_provider_horizon_cannot_win_a_card():
    """London's stored window ends on an all-null row. Treating those nulls as
    numbers crashes the tab; treating them as zeroes would report a 0 km/h
    'calmest' day the provider never forecast."""
    window = _window() + [
        {
            "forecast_date": "2026-10-08",
            "temp_min_c": None,
            "temp_max_c": None,
            "precip_mm": None,
            "wind_kmh": None,
        }
    ]
    cards = forecast.highlights(window, date(2026, 9, 24))
    assert [c["value"] for c in cards] == ["33°C", "12°C", "9.9 mm", "21 km/h"]
    assert "2026-10-08" not in " ".join(c["label"] for c in cards)


def test_a_window_with_no_readings_says_so_rather_than_showing_zero():
    blank = [
        {
            "forecast_date": "2026-09-24",
            "temp_min_c": None,
            "temp_max_c": None,
            "precip_mm": None,
            "wind_kmh": None,
        }
    ]
    cards = forecast.highlights(blank, date(2026, 9, 24))
    assert [c["value"] for c in cards] == ["No data", "No data", "No data", "No data"]
    assert [c["label"] for c in cards] == ["Warmest", "Coldest", "Next rain", "Windiest"]


def test_a_sub_zero_low_never_renders_as_negative_zero():
    """Reykjavik's coldest night rounds from -0.4, and `:.0f` renders that as
    '-0', which reads as a typo rather than as freezing point."""
    window = [
        {
            "forecast_date": "2026-09-29",
            "temp_min_c": -0.4,
            "temp_max_c": 11.0,
            "precip_mm": 0.0,
            "wind_kmh": 61.0,
        }
    ]
    cards = forecast.highlights(window, date(2026, 9, 29))
    assert cards[1]["value"] == "0°C"
