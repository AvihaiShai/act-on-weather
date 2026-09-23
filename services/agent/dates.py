"""Turning "tomorrow" and "this week" into dates, in code.

This is deliberately not the model's job. A date is the one thing in an answer
that must be exactly right, it is cheap to get right deterministically, and a
1.7B model asked to do calendar arithmetic will eventually get it wrong in
front of a reviewer.

"Today" is resolved in the *city's* timezone, not the server's, so asking about
tomorrow in Tel Aviv from a machine in London gives the right day.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


@dataclass
class DateRange:
    start: date
    end: date
    label: str

    def days(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range((self.end - self.start).days + 1)]

    def __str__(self) -> str:
        if self.start == self.end:
            return self.start.isoformat()
        return f"{self.start.isoformat()} to {self.end.isoformat()}"


def today_in(timezone: str) -> date:
    try:
        return datetime.now(ZoneInfo(timezone)).date()
    except Exception:  # noqa: BLE001 - an unknown tz must not break the answer
        return datetime.now(UTC).date()


def parse(question: str, timezone: str = "UTC") -> DateRange:
    """Pick the range the question is about. Defaults to the coming week.

    The default is stated in the answer's footer, so a user who meant something
    else can see what was assumed rather than having to guess.
    """
    text = question.lower()
    today = today_in(timezone)

    explicit = ISO_DATE.search(text)
    if explicit:
        day = date(int(explicit.group(1)), int(explicit.group(2)), int(explicit.group(3)))
        return DateRange(day, day, day.isoformat())

    if "day after tomorrow" in text:
        day = today + timedelta(days=2)
        return DateRange(day, day, "the day after tomorrow")
    if "tomorrow" in text:
        day = today + timedelta(days=1)
        return DateRange(day, day, "tomorrow")
    if "tonight" in text or "today" in text:
        return DateRange(today, today, "today")
    if "yesterday" in text:
        day = today - timedelta(days=1)
        return DateRange(day, day, "yesterday")

    if "weekend" in text:
        # The coming Saturday and Sunday; if it is already the weekend, this one.
        ahead = (5 - today.weekday()) % 7
        saturday = today + timedelta(days=ahead)
        return DateRange(saturday, saturday + timedelta(days=1), "this weekend")

    if "next week" in text:
        monday = today + timedelta(days=(7 - today.weekday()))
        return DateRange(monday, monday + timedelta(days=6), "next week")

    if "this week" in text or "the week" in text:
        return DateRange(today, today + timedelta(days=6), "this week")

    for name, index in WEEKDAYS.items():
        if re.search(rf"\b{name}\b", text):
            ahead = (index - today.weekday()) % 7
            day = today + timedelta(days=ahead or 7 if ahead == 0 else ahead)
            return DateRange(day, day, name.capitalize())

    for pattern, _days in ((r"\bnext (\d+) days\b", None), (r"\b(\d+) days\b", None)):
        match = re.search(pattern, text)
        if match:
            count = max(1, min(16, int(match.group(1))))
            return DateRange(today, today + timedelta(days=count - 1), f"the next {count} days")

    return DateRange(today, today + timedelta(days=6), "the coming week (assumed)")
