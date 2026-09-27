"""The date parser on its own -- the two gaps F-9 named, and their edges.

`services/agent/dates.py` is the one place in the agent that must never guess:
a date is cheap to get right deterministically, and a 1.7B model asked to do
calendar arithmetic will eventually get it wrong in front of a reviewer. These
tests live apart from `test_router.py` because the module imports nothing but
the standard library, so they run on a bare interpreter with no database, no
broker and no model.

The two defects, both of which answered a *different question* from the one
asked and said nothing about it:

  * a stated range resolved only its first date, so "from 2026-10-05 to
    2026-10-14" was answered as the single day 2026-10-05;
  * there was no weeks pattern at all, so "the next three weeks" fell through
    to the assumed seven days.

Neither raised and neither logged, and the footer discloses only the *assumed*
week -- so the first was invisible and the second read as a deliberate default.
That is why they are regression-tested rather than left to the router's tests.

The clock is not frozen here. Every assertion is relative to `dates.today_in`
itself, or uses dates far enough out to be unambiguous, so these keep meaning
the same thing after the staged forecast window has expired.
"""

from datetime import date, timedelta

import pytest

from services.agent import dates

# --------------------------------------------------------- stated ranges ----


def test_a_stated_range_resolves_both_ends():
    """The F-9 defect: only the first date was read, and ten days became one."""
    window = dates.parse("what is it like from 2026-10-05 to 2026-10-14?", "Europe/Rome")
    assert window.start == date(2026, 10, 5)
    assert window.end == date(2026, 10, 14)
    assert len(window.days()) == 10


def test_a_single_stated_date_is_still_a_single_day():
    """The behaviour the range fix must not disturb."""
    window = dates.parse("weather in Rome on 2027-07-04", "Europe/Rome")
    assert window.start == window.end == date(2027, 7, 4)
    assert window.label == "2027-07-04"


def test_a_range_written_backwards_is_still_that_range():
    """An inverted DateRange yields no days at all, which the router reports as
    "I have no weather data" -- a refusal covering dates it does hold."""
    window = dates.parse("anything between 2026-10-14 and 2026-10-05?", "UTC")
    assert window.start == date(2026, 10, 5)
    assert window.end == date(2026, 10, 14)
    assert window.days(), "a backwards range must not resolve to nothing"


def test_the_same_date_twice_is_one_day():
    window = dates.parse("2026-10-05 to 2026-10-05", "UTC")
    assert window.start == window.end == date(2026, 10, 5)


def test_three_stated_dates_span_all_of_them():
    window = dates.parse("2026-10-05, 2026-10-07 and 2026-10-09", "UTC")
    assert window.start == date(2026, 10, 5)
    assert window.end == date(2026, 10, 9)


def test_a_stated_range_says_both_ends_when_rendered():
    window = dates.parse("from 2026-10-05 to 2026-10-14", "UTC")
    assert str(window) == "2026-10-05 to 2026-10-14"
    assert window.label == "2026-10-05 to 2026-10-14"


def test_a_stated_range_is_not_labelled_an_assumption():
    """The router keys the "no dates in the question" footer off this word, and
    keys `asks_when` off how the label ends. A stated range is neither."""
    window = dates.parse("from 2026-10-05 to 2026-10-14", "UTC")
    assert "assumed" not in window.label


def test_a_date_shaped_string_that_is_not_a_date_does_not_raise():
    """`date(2026, 99, 99)` raises, and the old code let that out of the parser
    into the API's 500 handler. It falls through to the default instead."""
    window = dates.parse("what about 2026-99-99 in Rome?", "UTC")
    assert "assumed" in window.label


def test_one_unusable_date_does_not_hide_a_usable_one():
    window = dates.parse("2026-99-99 or 2026-10-05?", "UTC")
    assert window.start == window.end == date(2026, 10, 5)


def test_an_absurd_stated_range_stays_bounded():
    window = dates.parse("from 1900-01-01 to 2100-01-01", "UTC")
    assert len(window.days()) == dates.MAX_EXPLICIT_DAYS
    assert window.start == date(1900, 1, 1)


# ----------------------------------------------------------------- weeks ----


def test_next_three_weeks_is_not_seven_days():
    """The F-9 defect, in the words a reviewer would use."""
    window = dates.parse("what can I do over the next three weeks in London?", "Europe/London")
    assert len(window.days()) == 21
    assert len(window.days()) != 7


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("the next two weeks", 14),
        ("the next 2 weeks", 14),
        ("anything in the next four weeks?", 28),
        ("plans for the next 3 weeks", 21),
    ],
)
def test_a_count_of_weeks_is_that_many_weeks(question, expected):
    assert len(dates.parse(question, "UTC").days()) == expected


def test_a_spelled_out_count_of_days_is_that_many_days():
    """The table that fixes weeks fixes days too: "the next three days" fell
    through to the assumed week for exactly the same reason."""
    assert len(dates.parse("the next three days", "UTC").days()) == 3


def test_twelve_weeks_is_not_read_as_two_weeks():
    """Longest alternative first. "two" is a prefix hazard for "twelve"."""
    window = dates.parse("the next twelve weeks", "UTC")
    assert len(window.days()) == dates.MAX_RELATIVE_DAYS


def test_a_week_count_beyond_the_bound_is_bounded_and_says_so():
    """The bound may shorten a range. It may not let the label go on claiming
    the whole of it was answered."""
    window = dates.parse("the next ten weeks", "UTC")
    assert len(window.days()) == dates.MAX_RELATIVE_DAYS
    assert window.label == f"the next {dates.MAX_RELATIVE_DAYS} days"


def test_the_bound_reaches_past_the_furthest_stored_listing():
    """`data/events.seed.jsonl` holds a London fixture on 2026-10-17, twenty
    days past the rehearsal date. A three-week question has to be able to see
    it, which is why the bound is a month and not the 16-day forecast."""
    assert dates.MAX_RELATIVE_DAYS >= 21


def test_next_week_singular_is_still_the_calendar_week():
    """The weeks pattern requires a count, so it must not swallow this one."""
    window = dates.parse("what about next week?", "UTC")
    assert window.label == "next week"
    assert len(window.days()) == 7


def test_this_week_is_still_seven_days_from_today():
    window = dates.parse("what can I do this week in London?", "Europe/London")
    assert len(window.days()) == 7
    assert window.start == dates.today_in("Europe/London")


# ------------------------------------------------------- relative phrases ----


def test_a_day_count_is_that_many_days():
    window = dates.parse("the next 5 days", "UTC")
    assert len(window.days()) == 5
    assert window.start == dates.today_in("UTC")
    assert window.label == "the next 5 days"


def test_an_absurd_day_count_stays_bounded():
    window = dates.parse("the next 9999 days", "UTC")
    assert len(window.days()) == dates.MAX_RELATIVE_DAYS


def test_zero_days_is_still_one_day():
    window = dates.parse("the next 0 days", "UTC")
    assert window.start == window.end


def test_weeks_win_over_days_when_both_are_written():
    """The larger stated unit is the ask; the smaller is an aside."""
    window = dates.parse("the next 2 weeks, not 3 days", "UTC")
    assert len(window.days()) == 14


# -------------------------------------------------------------- timezone ----


def test_today_is_resolved_in_the_citys_own_zone():
    """Asking about Tel Aviv from a machine in Honolulu has to give Tel Aviv's
    today. The zones are most of a day apart, so the two are equal or one
    apart -- assert the relationship, never a literal."""
    difference = dates.today_in("Asia/Jerusalem") - dates.today_in("Pacific/Honolulu")
    assert difference in (timedelta(0), timedelta(days=1))


def test_an_unknown_timezone_does_not_raise():
    assert isinstance(dates.today_in("Mars/Olympus_Mons"), date)


def test_a_relative_range_starts_on_the_citys_today():
    for timezone in ("Europe/London", "Asia/Jerusalem", "Atlantic/Reykjavik"):
        window = dates.parse("the next three days", timezone)
        assert window.start == dates.today_in(timezone)
