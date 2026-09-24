"""The Forecast cards use the next city-local day, including at day boundaries."""

import sys
from datetime import UTC, datetime
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
