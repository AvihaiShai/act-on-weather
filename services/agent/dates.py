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

# "next three weeks" has to mean three weeks. Short counts get written as
# words at least as often as digits, and a parser that reads only digits does
# not fail loudly: it falls through to the assumed week and answers seven
# days. That silent narrowing is the defect this table exists to stop.
NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

# Longest alternatives first, so "twelve" is never read as "two".
_COUNT = r"(\d+|" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True)) + r")"
DAYS_AHEAD = re.compile(rf"\b{_COUNT}\s+days\b")
WEEKS_AHEAD = re.compile(rf"\b{_COUNT}\s+weeks\b")

# The longest range a relative phrase is allowed to build. A month: past the
# 16-day forecast horizon and past the furthest listing the event feed holds,
# so nothing stored is out of reach, while "the next 9999 days" still cannot
# hand the router a list of nine thousand days. Which of those days there is
# actually data for is the coverage gate's answer to give, not this module's.
MAX_RELATIVE_DAYS = 31

# A range written out in full is resolved exactly as written -- that is the
# point of writing it -- but bounded for the same reason.
MAX_EXPLICIT_DAYS = 366


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


def _iso_dates(text: str) -> list[date]:
    """Every ISO date written in the text, invalid ones dropped.

    The pattern matches well-formed strings that are not dates -- 2026-99-99 --
    and `date()` raises on them. Dropping one rather than raising is what keeps
    a typo out of the API's 500 handler: the question falls through to the
    other patterns and the footer says what was assumed instead.
    """
    found = []
    for year, month, day in ISO_DATE.findall(text):
        try:
            found.append(date(int(year), int(month), int(day)))
        except ValueError:
            continue
    return found


def _explicit(text: str) -> DateRange | None:
    """A date, or a range of dates, the questioner wrote out in full.

    Two dates mean both ends. "from 2026-10-05 to 2026-10-14" is ten days, and
    reading only the first of them -- which is what this used to do -- answers
    a different question from the one asked, without saying so.

    The ends are sorted, because a range written backwards still names the
    range it names, and because an inverted `DateRange` yields no days at all:
    the router would report "I have no weather data" for ten days it holds.
    """
    found = _iso_dates(text)
    if not found:
        return None
    start, end = min(found), max(found)
    if (end - start).days + 1 > MAX_EXPLICIT_DAYS:
        end = start + timedelta(days=MAX_EXPLICIT_DAYS - 1)
    if start == end:
        return DateRange(start, start, start.isoformat())
    return DateRange(start, end, f"{start.isoformat()} to {end.isoformat()}")


def _count(match: re.Match[str]) -> int:
    word = match.group(1)
    return int(word) if word.isdigit() else NUMBER_WORDS[word]


def _ahead(today: date, span: int) -> DateRange:
    """`span` days starting today, bounded by `MAX_RELATIVE_DAYS`.

    The label names the number of days this resolved to rather than the phrase
    that was typed, so a request the bound shortened cannot read back as though
    it had been honoured in full.
    """
    used = max(1, min(MAX_RELATIVE_DAYS, span))
    return DateRange(today, today + timedelta(days=used - 1), f"the next {used} days")


def parse(question: str, timezone: str = "UTC") -> DateRange:
    """Pick the range the question is about. Defaults to the coming week.

    The default is stated in the answer's footer, so a user who meant something
    else can see what was assumed rather than having to guess.
    """
    text = question.lower()
    today = today_in(timezone)

    explicit = _explicit(text)
    if explicit:
        return explicit

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

    # Weeks before days: "the next three weeks" names no number of days, and
    # falling through to the default answered seven of them in silence.
    weeks = WEEKS_AHEAD.search(text)
    if weeks:
        return _ahead(today, _count(weeks) * 7)

    days = DAYS_AHEAD.search(text)
    if days:
        return _ahead(today, _count(days))

    return DateRange(today, today + timedelta(days=6), "the coming week (assumed)")
