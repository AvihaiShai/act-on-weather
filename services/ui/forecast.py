"""Pick the forecast days worth putting on a card, in the city's own time zone."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo


def utc_now() -> datetime:
    return datetime.now(UTC)


def local_today(timezone: str, *, now: datetime | None = None) -> date:
    return (now if now is not None else utc_now()).astimezone(ZoneInfo(timezone)).date()


def upcoming_rows(rows: list[dict], timezone: str, *, now: datetime | None = None) -> list[dict]:
    """Stored rows from the city's current day onward, in date order.

    A snapshot keeps the days it was fetched on, so the window has to be cut
    here. Without the cut a card could answer "warmest day" with yesterday.
    """
    today = local_today(timezone, now=now)
    return sorted(
        (row for row in rows if datetime.fromisoformat(row["forecast_date"]).date() >= today),
        key=lambda row: row["forecast_date"],
    )


def next_row(rows: list[dict], timezone: str, *, now: datetime | None = None) -> dict | None:
    window = upcoming_rows(rows, timezone, now=now)
    return window[0] if window else None


def day_label(day: str, today: date) -> str:
    """'today', 'tomorrow', or 'Sat 3 Oct' -- short enough to sit in a card label."""
    parsed = datetime.fromisoformat(day).date()
    offset = (parsed - today).days
    if offset == 0:
        return "today"
    if offset == 1:
        return "tomorrow"
    return f"{parsed:%a} {parsed.day} {parsed:%b}"


def _pick(window: list[dict], field: str, chooser) -> dict | None:
    """Best row for one field, ignoring rows the provider left blank.

    A stored day can arrive with null metrics when it sits past the provider's
    horizon for that variable -- London's last day is exactly this. A null is
    absent, not a zero and not a low, so it must neither win nor lose a
    comparison. Filtering per field rather than per row keeps a day that has a
    temperature but no wind eligible for the temperature cards.
    """
    rated = [row for row in window if row.get(field) is not None]
    return chooser(rated, key=lambda row: row[field]) if rated else None


def highlights(window: list[dict], today: date) -> list[dict]:
    """One card per question, each resolving to whichever day actually answers it.

    The four cards used to read off a single row, so they all carried the same
    date and three of them were answering a question nobody asked: the peak of
    the week is what matters for "how hot does it get", not tomorrow's high.
    `max`/`min` over date-sorted rows settle ties on the soonest day, which is
    the useful one when two days share a peak.
    """

    def card(name, row, fmt, note, absent) -> dict:
        if row is None:
            return {"label": name, "value": absent, "help": note}
        return {
            "label": f"{name} · {day_label(row['forecast_date'], today)}",
            "value": fmt(row),
            "help": note,
        }

    # "No rain this week" and "the provider sent no rain figures" are different
    # answers, and the second must never be displayed as the first.
    measured = [row for row in window if row.get("precip_mm") is not None]

    return [
        card(
            "Warmest",
            _pick(window, "temp_max_c", max),
            lambda row: f"{round(row['temp_max_c']):d}°C",
            "Highest daytime high in the stored window",
            "No data",
        ),
        card(
            "Coldest",
            _pick(window, "temp_min_c", min),
            lambda row: f"{round(row['temp_min_c']):d}°C",
            "Lowest overnight low in the stored window",
            "No data",
        ),
        card(
            "Next rain",
            next((row for row in measured if row["precip_mm"] > 0), None),
            lambda row: f"{row['precip_mm']:.1f} mm",
            "First day with rain in the stored window",
            "None" if measured else "No data",
        ),
        card(
            "Windiest",
            _pick(window, "wind_kmh", max),
            lambda row: f"{round(row['wind_kmh']):d} km/h",
            "Strongest daily wind in the stored window",
            "No data",
        ),
    ]
