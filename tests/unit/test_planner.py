"""The trip planner's day-picking.

This covers the bug the planner shipped with: it took the top-scoring activity
for each day and stopped. In a warm, dry city one activity scores 100 on every
day of the forecast, so a week in Tel Aviv came back as "a day at the beach"
seven times -- and the traveller's stated interests changed nothing, because
they only ever filtered the *places* list.

`plan_day` is pure: rows in, ordered suggestions out. That is deliberate, so
the ordering can be tested here without a database, an LLM or a container.
"""

from __future__ import annotations

import pytest

from services.agent.planning import INTEREST_BONUS, REPEAT_PENALTY, plan_day

META = {
    "beach_day": {"label": "A day at the beach", "icon": "B", "interests": ["outdoors"]},
    "surfing": {"label": "Surfing", "icon": "S", "interests": ["sports", "outdoors"]},
    "museums": {
        "label": "Visiting a museum",
        "icon": "M",
        "indoor": True,
        "interests": ["museums", "history"],
    },
    "mall": {
        "label": "Shopping at the mall",
        "icon": "L",
        "indoor": True,
        "interests": ["shopping"],
    },
}


def row(activity: str, score: int, status: str = "ready", text: str = "because."):
    return {
        "activity": activity,
        "activity_label": META[activity]["label"],
        "score": score,
        "band": "good" if score >= 70 else "fair" if score >= 40 else "poor",
        "status": status,
        "text": text,
        "reasons": ["a stored reason"],
    }


PERFECT_DAY = [row("beach_day", 100), row("surfing", 100), row("museums", 59), row("mall", 45)]


def labels(suggestions):
    return [s["activity"] for s in suggestions]


# ------------------------------------------------------------- ordering ----


def test_highest_score_wins_when_nothing_else_applies():
    picks = plan_day(PERFECT_DAY, META, set(), {}, varied=False)
    assert picks[0]["score"] == 100
    assert picks[-1]["activity"] == "mall"


def test_ties_break_deterministically():
    """beach_day and surfing both score 100. Whichever wins must win every
    time, or the same request rebuilds a different plan."""
    first = labels(plan_day(PERFECT_DAY, META, set(), {}, varied=True))
    second = labels(plan_day(list(reversed(PERFECT_DAY)), META, set(), {}, varied=True))
    assert first == second


def test_a_matching_interest_lifts_an_activity():
    """The museum is 41 points behind the beach, and one matching interest is
    worth INTEREST_BONUS -- so it moves up without overtaking it."""
    neutral = labels(plan_day(PERFECT_DAY, META, set(), {}, varied=False))
    interested = labels(plan_day(PERFECT_DAY, META, {"museums"}, {}, varied=False))
    assert neutral.index("museums") >= interested.index("museums")
    assert interested[0] == neutral[0], "a nudge must not override the weather score"


def test_an_interest_can_win_a_close_call():
    close = [row("beach_day", 80), row("mall", 72)]
    assert labels(plan_day(close, META, set(), {}, varied=False))[0] == "beach_day"
    assert labels(plan_day(close, META, {"shopping"}, {}, varied=False))[0] == "mall"
    assert INTEREST_BONUS > 8


def test_matched_interests_are_reported():
    picks = plan_day(PERFECT_DAY, META, {"museums", "shopping"}, {}, varied=False)
    by_activity = {p["activity"]: p for p in picks}
    assert by_activity["museums"]["matched_interests"] == ["museums"]
    assert by_activity["mall"]["matched_interests"] == ["shopping"]
    assert by_activity["beach_day"]["matched_interests"] == []


# -------------------------------------------------------------- variety ----


def test_repeats_are_penalised_so_a_week_is_not_one_activity():
    """The regression test for the reported bug."""
    used: dict[str, int] = {}
    chosen = []
    for _day in range(4):
        pick = plan_day(PERFECT_DAY, META, set(), used, varied=True)[0]
        chosen.append(pick["activity"])
        used[pick["activity"]] = used.get(pick["activity"], 0) + 1

    assert len(set(chosen)) > 1, f"the planner repeated itself: {chosen}"
    assert chosen[0] != chosen[1]


def test_variety_off_reproduces_the_raw_ranking():
    """`pace='best'` is the honest comparison: the un-nudged best score per
    day, repeats and all. A reviewer can flip it and see the difference."""
    used = {"beach_day": 3}
    assert plan_day(PERFECT_DAY, META, set(), used, varied=False)[0]["activity"] == "beach_day"
    assert plan_day(PERFECT_DAY, META, set(), used, varied=True)[0]["activity"] != "beach_day"


def test_repeat_penalty_never_reorders_a_wide_gap():
    """One repeat must not promote something far worse; the weather still wins."""
    wide = [row("beach_day", 100), row("mall", 20)]
    used = {"beach_day": 1}
    assert plan_day(wide, META, set(), used, varied=True)[0]["activity"] == "beach_day"
    assert REPEAT_PENALTY < 80


# ---------------------------------------------------------- the payload ----


def test_each_suggestion_carries_what_the_ui_renders():
    pick = plan_day(PERFECT_DAY, META, set(), {}, varied=True)[0]
    assert set(pick) >= {
        "activity",
        "label",
        "icon",
        "indoor",
        "score",
        "band",
        "matched_interests",
        "why",
        "worded",
    }


def test_a_deferred_row_is_marked_unworded():
    """'deferred' means scored but never sent to the model. The UI has to be
    able to say so rather than showing an empty explanation."""
    rows = [row("beach_day", 100, status="deferred", text=None)]
    pick = plan_day(rows, META, set(), {}, varied=True)[0]
    assert pick["worded"] is False
    # It still gets an explanation -- the rule engine's reasons, not prose.
    assert pick["why"] == "a stored reason"


def test_no_rows_means_no_suggestions():
    assert plan_day([], META, {"museums"}, {}, varied=True) == []


def test_an_unknown_activity_does_not_crash_the_day():
    """A user-typed activity has no block in activities.yml, so it has no icon
    and no interest tags. It must still be rankable."""
    rows = [
        {
            "activity": "kite_flying",
            "activity_label": "Kite flying",
            "score": 88,
            "band": "good",
            "status": "ready",
            "text": "worded",
            "reasons": [],
        }
    ]
    pick = plan_day(rows, META, {"sports"}, {}, varied=True)[0]
    assert pick["label"] == "Kite flying"
    assert pick["icon"] is None
    assert pick["indoor"] is False
    assert pick["matched_interests"] == []


@pytest.mark.parametrize("score_value", [None, 0, 100])
def test_a_missing_score_is_treated_as_zero_not_a_crash(score_value):
    rows = [
        {
            "activity": "beach_day",
            "activity_label": "A day at the beach",
            "score": score_value,
            "band": None,
            "status": "pending",
            "text": None,
            "reasons": [],
        }
    ]
    assert plan_day(rows, META, set(), {}, varied=True)[0]["score"] == score_value
