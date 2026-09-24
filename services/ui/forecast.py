"""Choose the next stored forecast day in the selected city's time zone."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def utc_now() -> datetime:
    return datetime.now(UTC)


def next_row(rows: list[dict], timezone: str, *, now: datetime | None = None) -> dict | None:
    today = (now if now is not None else utc_now()).astimezone(ZoneInfo(timezone)).date()
    return min(
        (row for row in rows if datetime.fromisoformat(row["forecast_date"]).date() >= today),
        key=lambda row: row["forecast_date"],
        default=None,
    )
