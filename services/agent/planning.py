"""How the trip planner chooses a day's activity.

Kept apart from `agent.main` because it is pure: stored rows in, an ordered
list of suggestions out, no database, no LLM, no clock. That is what makes it
testable in `tests/unit/test_planner.py` without a container, and it is the
part of the planner most worth testing -- the ordering is the product.

The problem it solves. The first version took the top-scoring activity for
each day and stopped. In a warm, dry city one activity scores 100 on every day
of the forecast, so a week in Tel Aviv came back as "a day at the beach" seven
times over; and the traveller's stated interests changed nothing at all,
because they only ever filtered the *places* list.

So two adjustments are applied to the deterministic score before the pick, and
neither of them touches the stored score itself. The score stays the source of
truth and is what the heatmap, the API and the agent report. This is a
presentation ordering laid over it -- and it is deterministic, so the same
request rebuilds the same plan.

`venue_places` fixes the other half of that sentence. Interests filtered the
places list, but the day's chosen activity did not, so a beach day and a museum
day in the same city were handed the same three rows.
"""

from __future__ import annotations

from typing import Any

# Both are plain integers on the same 0-100 scale as the score, so the
# trade-off is legible: a matching interest is worth about as much as one band
# of weather, and a third repeat of an activity costs more than that.
# Deliberately too small to overturn a wide gap -- a museum does not beat a
# perfect beach day because someone ticked "museums".
INTEREST_BONUS = 15
REPEAT_PENALTY = 18

# How many venue places a day may name before the list stops being a
# suggestion and starts being a directory.
MAX_VENUE_PLACES = 3


def venue_places(
    activity: str | None,
    meta: dict[str, dict[str, Any]],
    places: list[dict[str, Any]],
    used: set[str] | None = None,
) -> list[dict[str, Any]]:
    """The stored places that ARE a venue for the day's activity.

    The planner used to pick a day's places purely from the traveller's stated
    interests, with no reference to what the day was actually for. So a Tel
    Aviv day whose activity was "a day at the beach" listed parks, because
    `outdoors` resolves to parks and gardens -- and, until the beach vocabulary
    landed in the ingestor, because there was no beach row to list either.

    This is the narrow fix: an activity that declares `place_categories` in
    data/activities.yml gets the matching rows named first. Two properties
    matter more than the ordering.

    1. It claims nothing the source did not say. Only `beach_day` carries a
       coastal venue category, because "beach" is what Wikidata Q40080 and OSM
       `natural=beach` assert. Surfing, swimming, fishing and boat rides carry
       none: a beach is not evidence of surf, a lifeguard, legal angling or a
       boat for hire.
    2. Nothing exists, nothing is shown. An activity with no venue category, or
       one whose categories match no stored row, returns an empty list, and the
       caller renders the honest gap rather than substituting a park for a
       beach.

    `used` carries the place ids already spent on earlier days, so a week does
    not open on Gordon Beach seven times. Ordering is by stored `category` then
    `name` -- the order `queries.places` already returns -- so the same request
    rebuilds the same plan.
    """
    wanted = set((meta.get(activity) or {}).get("place_categories") or []) if activity else set()
    if not wanted:
        return []
    spent = used or set()
    return [p for p in places if p["category"] in wanted and p["id"] not in spent][
        :MAX_VENUE_PLACES
    ]


def plan_day(
    ranked: list[dict[str, Any]],
    meta: dict[str, dict[str, Any]],
    wanted_interests: set[str],
    used: dict[str, int],
    *,
    varied: bool,
) -> list[dict[str, Any]]:
    """Order one day's activities, best first.

    `ranked`  the day's rows from the recommendations table
    `meta`    data/activities.yml, for icon, indoor flag and interest tags
    `used`    how many earlier days already chose each activity
    `varied`  False reproduces the raw ranking, repeats and all, which is what
              the UI's "vary the plan" toggle turns off so the difference can
              be seen side by side
    """
    options = []
    for row in ranked:
        base = row["score"] or 0
        tags = set((meta.get(row["activity"]) or {}).get("interests") or [])
        matched = sorted(tags & wanted_interests)
        adjusted = base + (INTEREST_BONUS if matched else 0)
        if varied:
            adjusted -= REPEAT_PENALTY * used.get(row["activity"], 0)
        options.append((adjusted, row, matched))

    # Ties break on the activity key, so the same rows in any order produce the
    # same plan. Without it a re-run could silently reshuffle a saved itinerary.
    options.sort(key=lambda item: (-item[0], item[1]["activity"]))

    return [
        {
            "activity": row["activity"],
            "label": row["activity_label"],
            "icon": (meta.get(row["activity"]) or {}).get("icon"),
            "indoor": bool((meta.get(row["activity"]) or {}).get("indoor")),
            "score": row["score"],
            "band": row["band"],
            "matched_interests": matched,
            "why": row.get("text") or "; ".join(row.get("reasons") or []),
            # 'deferred' means the rule engine scored it but the local model was
            # never asked to word it. The UI says so rather than showing a blank.
            "worded": row.get("status") == "ready",
        }
        for _adjusted, row, matched in options
    ]
