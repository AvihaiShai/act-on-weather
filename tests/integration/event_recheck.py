"""A re-check renews a listing only when something actually verified it.

Run via ``docker compose exec -T api python - < tests/integration/event_recheck.py``.

`tests/integration/event_freshness.py` proved the freshness policy: a row
carries the day its page was read, derives an expiry from it, and leaves the
default read when that expiry passes. What it left open -- and what
DEVOPS_REVIEW records as open -- is that re-checking was manual: *"Extending a
listing's life means opening its page and patching `checked_at`, one row at a
time."*

`services/tools/event_recheck.py` closes that, and the interesting property is
not that it can renew a row. It is everything it refuses to renew. The unit
tests pin the classifier; this drill pins the two things only a real stack can
show:

  1. an `apply` really does travel PATCH -> outbox -> broker -> consumer, and
     the consumer re-derives `valid_until` from the *observation's* instant, so
     a renewed row's life is bought by the moment its page was read and by
     nothing else;
  2. a row whose fetch failed is refused end to end -- no message is accepted,
     no row moves, and the refusal is not merely a printed warning.

There is no network in CI and none is wanted here, so the evidence ledger is
built in-process from `event_recheck.evidence_line` -- the same function the
connected probe writes with -- against canned pages. What is being exercised is
the decision and the write path, both of which are offline.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

sys.path.insert(0, "/app")

from services.common import config  # noqa: E402
from services.tools import event_recheck as rc  # noqa: E402

BASE = os.environ.get("AOW_API_BASE", "http://127.0.0.1:8000")

# Two rows from the committed snapshot with fixed dates, so this keeps meaning
# the same thing after the staged forecast has expired. Deliberately not
# `theo2:laver-cup-2026`, which event_freshness.py drives in the same project.
VERIFIED_ID = "harpa:deep-purple-heidurstonleikar-2026-09-26"
REFUSED_ID = "coliseulisboa:radio-macau-2026-09-30"

# The instant the imaginary probe read the pages. A fixed, plainly historical
# value: if the consumer ever derived the expiry from `now()` instead of from
# the check date it is given, this is what would catch it.
OBSERVED_AT = "2026-09-25T09:00:00+00:00"

# A page that supports VERIFIED_ID. Only `startDate` is load-bearing; the title
# and venue are here so `compare` has something to agree with.
CONFIRMING_PAGE = """
<html><head><title>Deep Purple | Harpa</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Event",
 "name":%s, "startDate":%s,
 "eventStatus":"https://schema.org/EventScheduled",
 "location":{"@type":"Place","name":%s}}
</script></head><body></body></html>
"""


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return json.load(response)


def patch(entity_id: str, fields: dict) -> str:
    request = urllib.request.Request(
        f"{BASE}/records/events/{entity_id}",
        data=json.dumps(fields).encode(),
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)["message_id"]


def row(entity_id: str) -> dict:
    [found] = [r for r in get("/events?include_expired=true&limit=200") if r["id"] == entity_id]
    return found


def wait_until(predicate, what: str, timeout: int = 180):
    """Poll a read until the write behind it has landed.

    A patch is accepted by the API and applied by the consumer, so the two are
    deliberately not synchronous; every assertion waits for the effect rather
    than for the acknowledgement.
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


def shipped_checked_at(entity_id: str) -> str:
    """The check date this row ships with, read from the snapshot.

    From the file rather than from the live row, so a database a previous run
    left dirty is restored to the same place, and so the restore at the end is
    deterministic.
    """
    text = pathlib.Path("/app/data/snapshot/events.jsonl").read_text(encoding="utf-8")
    return next(
        json.loads(line)["checked_at"]
        for line in text.splitlines()
        if line.strip() and json.loads(line)["id"] == entity_id
    )


LEDGER = "/tmp/aow-recheck-drill.jsonl"

# --------------------------------------------------- 0. a known starting point --

for event_id in (VERIFIED_ID, REFUSED_ID):
    wait_until(
        lambda e=event_id: any(r["id"] == e for r in get("/events?include_expired=true&limit=200")),
        f"{event_id} to reach the database",
    )
    patch(event_id, {"checked_at": shipped_checked_at(event_id)})

wait_until(
    lambda: row(VERIFIED_ID)["checked_at"].startswith(shipped_checked_at(VERIFIED_ID)[:10]),
    "the rows to be restored to their shipped check dates",
)

verified_before = row(VERIFIED_ID)
refused_before = row(REFUSED_ID)

# ------------------------------------------------ 1. the evidence a probe writes --
#
# Built with the production function against canned pages: the drill must not
# be able to write an evidence line the real probe could not produce.

confirming = CONFIRMING_PAGE % (
    json.dumps(verified_before["title"]),
    json.dumps(verified_before["starts_at"]),
    json.dumps(verified_before["venue"]),
)

lines = [
    rc.evidence_line(
        verified_before,
        rc.FetchResult(
            url=verified_before["source_url"],
            fetched_at=OBSERVED_AT,
            ok=True,
            status=200,
            final_url=verified_before["source_url"],
            content_type="text/html",
            bytes=len(confirming),
            sha256="a" * 64,
            html=confirming,
        ),
    ),
    rc.evidence_line(
        refused_before,
        # The failure this whole design exists for: the fetch did not complete.
        rc.FetchResult(
            url=refused_before["source_url"],
            fetched_at=OBSERVED_AT,
            ok=False,
            error="ReadTimeout: 20s",
        ),
    ),
]
assert lines[0]["verdict"] == rc.CONFIRMED, lines[0]
assert lines[1]["verdict"] == rc.UNREACHABLE, lines[1]
assert lines[0]["renewable"] is True
assert lines[1]["renewable"] is False

pathlib.Path(LEDGER).write_text(
    "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
)

# ------------------------------------------ 2. age both rows out of the feed ----
#
# So that "it came back" is a real transition and not a row that never left.

LONG_AGO = "2020-01-01T00:00:00+00:00"
for event_id in (VERIFIED_ID, REFUSED_ID):
    patch(event_id, {"checked_at": LONG_AGO})

current_ids = lambda: {r["id"] for r in get("/events?limit=200")}  # noqa: E731
wait_until(
    lambda: VERIFIED_ID not in current_ids() and REFUSED_ID not in current_ids(),
    "both rows to leave the default read",
)
aged = row(VERIFIED_ID)
assert aged["is_current"] is False, aged

# ------------------------------------- 3. the timed-out row is refused outright --
#
# Before the successful path, so a refusal cannot hide behind a write that
# happened anyway. `--yes` is passed: this is not a dry run being refused, it is
# a real request being turned down.

assert rc.main(["apply", "--evidence", LEDGER, "--id", REFUSED_ID, "--yes", "--api", BASE]) == 1
assert not [
    r for r in rc.read_evidence(LEDGER) if r.get("kind") == "decision"
], "a refused row still wrote a decision line"
assert row(REFUSED_ID)["checked_at"].startswith(
    "2020-01-01"
), "a row whose fetch timed out was renewed anyway"

# --------------------------------------- 4. the verified row goes through the queue --

assert rc.main(["apply", "--evidence", LEDGER, "--all-confirmed", "--yes", "--api", BASE]) == 0

decisions = [r for r in rc.read_evidence(LEDGER) if r.get("kind") == "decision"]
assert [d["event_id"] for d in decisions] == [VERIFIED_ID], decisions
decision = decisions[0]
assert decision["action"] == "recheck-accepted", decision
assert decision["checked_at"] == OBSERVED_AT, decision
# The evidence the renewal rests on is filed with the renewal, not just the
# timestamp it produced.
assert decision["evidence_sha256"] == "a" * 64, decision
assert decision["observed_at"] == OBSERVED_AT, decision

wait_until(lambda: VERIFIED_ID in current_ids(), "the re-checked row to come back")

# It travelled the same path as any other correction, and can be followed there.
followed = get(f"/outbox/{decision['message_id']}")
assert followed["stored"] is True, followed

# ------------------------------- 5. the expiry was bought by the observation ----

restored = row(VERIFIED_ID)
assert restored["is_current"] is True, restored
assert restored["checked_at"].startswith("2026-09-25T09:00"), restored["checked_at"]

# The consumer derived this, from the check date it was given -- not from now(),
# and not from anything the tool sent. `valid_until` is not patchable at all.
expected = config.event_valid_until(datetime.fromisoformat(OBSERVED_AT)).isoformat()
assert restored["valid_until"][:19] == expected[:19], (restored["valid_until"], expected)
assert restored["valid_until"] > aged["valid_until"], "the expiry did not move with the check"

# A correction like any other: revision bumped, before/after in the history.
assert restored["revision"] > aged["revision"], (restored["revision"], aged["revision"])
history = get(f"/records/events/{VERIFIED_ID}/history")
assert history, "a re-check left no history"

# The refused row is still exactly where it was left.
assert row(REFUSED_ID)["checked_at"].startswith("2020-01-01")
assert REFUSED_ID not in current_ids()

# ------------------------- 6. a human who read the page may renew it, on record --

assert (
    rc.main(
        [
            "apply",
            "--evidence",
            LEDGER,
            "--id",
            REFUSED_ID,
            "--note",
            "opened the listing by hand; 30 setembro 2026 still shown",
            "--yes",
            "--api",
            BASE,
        ]
    )
    == 0
)
wait_until(lambda: REFUSED_ID in current_ids(), "the hand-checked row to come back")

manual = [r for r in rc.read_evidence(LEDGER) if r.get("event_id") == REFUSED_ID]
manual = [r for r in manual if r.get("kind") == "decision"]
assert len(manual) == 1, manual
assert manual[0]["verdict"] == rc.UNREACHABLE, manual[0]
assert manual[0]["note"].startswith("opened the listing"), manual[0]
# The distinction that has to survive: this row was renewed because a person
# looked, and the ledger says so beside the fetch that failed.
assert manual[0]["evidence_sha256"] is None, manual[0]

# ------------------------------------------------------------------ restore ----

for event_id in (VERIFIED_ID, REFUSED_ID):
    patch(event_id, {"checked_at": shipped_checked_at(event_id)})
wait_until(
    lambda: row(VERIFIED_ID)["checked_at"].startswith(shipped_checked_at(VERIFIED_ID)[:10]),
    "the rows to be restored",
)
pathlib.Path(LEDGER).unlink(missing_ok=True)

print(
    f"PASS: {REFUSED_ID} was refused after a failed fetch and did not move; "
    f"{VERIFIED_ID} was renewed through PATCH -> outbox -> broker -> consumer "
    f"(message {decision['message_id']}), and its expiry was re-derived from the "
    f"instant its page was read, not from now(); the hand-checked renewal is on "
    f"record with its note"
)
