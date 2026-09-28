"""F9/R9(c): a withdrawn record leaves the published output and stays out of it.

Run via ``docker compose exec -T api python - < tests/integration/retraction.py``.

The defect this closes. Records here are revised, never deleted, and the
writer holds no DELETE grant that reaches an event, a place or a fact. Until
migration 009 that left no way out for a record which turned out to be wrong
*after* it was collected: deleting its line from the snapshot stopped it
reaching a new install and did nothing at all to an install that already had
it, because a record absent from a snapshot simply produces no message. A
dress rehearsal found the consequence in the running stack -- two listings the
project's own recheck had verified as cancelled were still being served.

Only a real database can prove the fix, for two reasons. The published/
withdrawn split is a SQL predicate repeated across a dozen reads, so the
property worth testing is "the record is gone from every surface", not "one
WHERE clause works". And the second half of the property is about a rebuild:
``user_data.wipe`` deletes every collected row and replays it from the
ingestor's outbox, which is exactly the operation that used to bring the
withdrawn rows back.

So the drill is end to end, and it runs for all three collected entities,
because they are three different tables, three different read paths and three
different count sites:

  1. an event, a place and a fact are published -- visible in their own read
     and counted in /coverage;
  2. each is retracted *through the API*, which means through the outbox, the
     broker and the consumer like any other write (M4);
  3. each disappears from its read, from the per-city counts and from the
     coverage totals -- including from ``include_expired``, which is the read
     an operator uses to see what the freshness filter hid, and which must not
     become a back door to withdrawn content;
  4. the row is still in the database with its provenance intact, proved
     through the history endpoint: a retraction is a revision, not a delete;
  5. a rebuild happens -- ``POST /user-data/wipe`` -- and all three are STILL
     absent afterwards.

Step 5 is the one that would catch the tempting wrong fix. Marking the rows
and leaving the wipe alone passes steps 1-4 and silently republishes all three
the next time anybody rebuilds.

Destructive, in one direction only: it retracts three real records and there
is deliberately no un-retract route, so run it against a disposable project
rather than a stack you want to keep. It takes no backup and deletes nothing.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/app")

BASE = os.environ.get("AOW_API_BASE", "http://127.0.0.1:8000")

# One row per collected entity, all from the committed snapshot so this keeps
# meaning the same thing after the staged forecast has expired. The event is
# the same London row the freshness drill uses.
EVENT_ID = "theo2:laver-cup-2026"
CITY = "london"

REASON = "integration drill: proving a withdrawn record leaves the published output"


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return json.load(response)


def post(path: str, body: dict):
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def ids(rows) -> set[str]:
    return {row["id"] for row in rows}


def wait_until(predicate, what: str, timeout: int = 180):
    """Poll a read until the write behind it has landed.

    A retraction is accepted by the API and applied by the consumer, so the
    two are deliberately not synchronous. Every assertion waits for the
    effect rather than for the acknowledgement.
    """
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except (OSError, urllib.error.URLError) as exc:
            last = exc
        time.sleep(2)
    raise AssertionError(f"timed out waiting for {what}: {last!r}")


def events(include_expired: bool = False) -> list[dict]:
    extra = "&include_expired=true" if include_expired else ""
    return get(f"/events?city={CITY}&limit=200{extra}")


def places() -> list[dict]:
    return get(f"/places?city={CITY}&limit=500")


def facts() -> list[dict]:
    return get(f"/facts?city={CITY}&limit=200")


def coverage_counts() -> dict[str, int]:
    # `entities` is a list of rows, not a mapping -- the UI keys it the same
    # way at services/ui/app.py:108.
    return {row["entity"]: row["rows"] for row in get("/coverage")["entities"]}


def city_counts() -> dict[str, int]:
    for row in get("/coverage")["by_city"]:
        if row["city_id"] == CITY:
            return row
    raise AssertionError(f"{CITY} missing from /coverage by_city")


# ------------------------------------- 1. pick one published row per entity ----

wait_until(lambda: EVENT_ID in ids(events(include_expired=True)), "the event to reach the database")
wait_until(lambda: places(), "places to reach the database")
wait_until(lambda: facts(), "facts to reach the database")

# Chosen from what is actually published rather than hardcoded, because the
# place and fact ids are snapshot-generated and this drill should not break
# the next time the snapshot is rebuilt.
PLACE_ID = sorted(ids(places()))[0]
FACT_ID = sorted(ids(facts()))[0]

TARGETS = {"events": EVENT_ID, "places": PLACE_ID, "facts": FACT_ID}
print(f"retracting: {TARGETS}")

assert EVENT_ID in ids(events()), "the event under test is not in the default read to begin with"
assert PLACE_ID in ids(places())
assert FACT_ID in ids(facts())


def settled_counts(what: str, timeout: int = 180):
    """`coverage_counts()` and `city_counts()`, once ingestion has stopped moving.

    The waits above only prove that SOME rows have arrived. On a stack that has
    just been started -- which is exactly how `scripts/retraction-drill.sh` runs
    this file, and is the case nobody had ever exercised -- the ingestor is still
    draining its outbox, so the "before" totals were a snapshot of a moving
    number. The retraction then removed one row while ingestion added thirty-five
    more, and the assertion below read `19 -> 54` and called it a failure to
    withdraw. Nothing was wrong with the retraction.

    Two consecutive agreeing reads, three seconds apart. Cheap, and it makes the
    difference between a drill that measures a withdrawal and one that races the
    ingestor.
    """
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        current = (coverage_counts(), city_counts())
        if current == previous:
            return current
        previous = current
        time.sleep(3)
    raise AssertionError(f"timed out waiting for {what} to stop changing: {previous!r}")


before_totals, before_city = settled_counts("ingestion")
print(f"ingestion settled: {before_totals}")

# --------------------------------- 2. retract each one, through the queue ----

for entity, entity_id in TARGETS.items():
    accepted = post(f"/records/{entity}/{entity_id}/retract", {"reason": REASON})
    assert accepted["accepted"] is True, accepted
    print(f"accepted retraction of {entity} {entity_id} as {accepted['message_id']}")

# ---------------------------- 3. gone from every read, and from the counts ----

wait_until(lambda: EVENT_ID not in ids(events()), "the event to leave the default read")
wait_until(lambda: PLACE_ID not in ids(places()), "the place to leave the places read")
wait_until(lambda: FACT_ID not in ids(facts()), "the fact to leave the facts read")

# include_expired shows what the freshness filter hid. It must not show what a
# retraction hid: an expired listing is stale, a withdrawn one is wrong, and
# only the first is something an operator should be able to page back in.
assert EVENT_ID not in ids(events(include_expired=True)), (
    "a withdrawn event came back under include_expired -- the operator read is "
    "a back door into retracted content"
)

after_totals = coverage_counts()
for entity in TARGETS:
    assert after_totals[entity] == before_totals[entity] - 1, (
        f"/coverage still counts the withdrawn {entity[:-1]}: "
        f"{before_totals[entity]} -> {after_totals[entity]}"
    )

after_city = city_counts()
for key in ("events", "places", "facts"):
    assert (
        after_city[key] == before_city[key] - 1
    ), f"the per-city {key} count did not drop: {before_city[key]} -> {after_city[key]}"

# ------------------------- 4. still stored, still carrying its provenance ----

for entity, entity_id in TARGETS.items():
    revisions = get(f"/records/{entity}/{entity_id}/history")
    assert revisions, (
        f"{entity} {entity_id} has no history after being retracted -- a retraction "
        "is supposed to be a revision of the row, not a delete"
    )

# -------------------------------- 5. a rebuild does not republish any of it ----

# The operation that used to bring withdrawn rows back: every collected row is
# deleted and replayed from the ingestor's outbox.
#
# The wipe is asynchronous, and "the record is absent" is also true while the
# rebuild has simply not run yet -- so waiting for absence would pass without
# testing anything. `wipe_user_data` clears `record_history` as its last
# statement, and step 4 has just proved these rows had history, so an emptied
# history is a positive signal that the rebuild actually happened.
post("/user-data/wipe", {"confirm": "WIPE"})


def history_of(entity: str, entity_id: str) -> list:
    return get(f"/records/{entity}/{entity_id}/history")


wait_until(lambda: not history_of("events", EVENT_ID), "the rebuild to clear record_history")

# And the rebuild really did restore the collected rows, rather than leaving an
# empty database that would make every absence assertion below vacuous.
wait_until(lambda: len(events(include_expired=True)) > 1, "the rebuild to restore the event feed")
wait_until(lambda: len(places()) > 1, "the rebuild to restore places")
wait_until(lambda: len(facts()) > 1, "the rebuild to restore facts")

for entity, read, entity_id in (
    ("events", events, EVENT_ID),
    ("places", places, PLACE_ID),
    ("facts", facts, FACT_ID),
):
    assert entity_id not in ids(read()), (
        f"the rebuild republished the withdrawn {entity[:-1]} {entity_id}. "
        "This is the F9 defect: a wipe replays collected envelopes, so a "
        "retraction that is not carried across the rebuild silently undoes itself."
    )

assert EVENT_ID not in ids(
    events(include_expired=True)
), "the rebuild republished the withdrawn event under include_expired"

print("OK: event, place and fact each withdrawn from the published output, and")
print("    still absent after a full rebuild from the ingestor's outbox.")
