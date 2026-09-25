# Evidence: targeted test coverage (B1)

**Dated 2026-09-25.** Branch `review/b1-targeted-tests`, based on `a21dff9`.

B1 asks for "full tests for all components". This work does **not** claim to
close it, and nothing here should be read as "B1 is now met" —
[`CICD_EVIDENCE.md`](CICD_EVIDENCE.md) §2 already says B1 is partially met, and
that verdict is unchanged. What this pass did was narrower and, for a reviewer,
more useful: find the places where the *assignment's own required behaviour*
had no test behind it, and pin those.

Three components were audited independently against the existing suite: the
agent's routing and grounding, the queue-to-database delivery path, and the
trip planner and its UI. The audits were asked for gaps where a **real defect
would ship silently** — not for a test count.

Two of the gaps turned out not to be gaps at all. They were live defects.

---

## 0. What was already covered

Worth stating first, because the value of the gaps below depends on it. The
baseline suite is strong, and most of what was checked did not need a test.

| Area | Already pinned | Where |
|---|---|---|
| E2 / the coastal refusal | recognition, no-weather-fetch, "a beach is not a surf spot" for all four sea activities, London recorded as inland, no model call | `test_where_questions.py`, `test_coastal_evidence.py`, `test_places_vocabulary.py` |
| Grounding violations | all nine checks, each with the real observed bad wording | `test_grounding.py:246-1107` |
| Replay must not duplicate a row | unit **and** real broker/Postgres, `count(*) = 6` exactly-once from a separate reader | `test_consumer_routing.py:68`, `ci-integration.sh:216` |
| Outbox survives restart | pending rows, publish order, `published_at` still null while the broker is down | `test_outbox.py:55-91`, drill 4 |
| Reconcile ACKed-but-missing | detection, replay, and a stored-ID control | `test_outbox.py:93,113`, `ci-integration.sh:194-206` |
| Ack-after-commit under a DB outage | drills 3 and 5, which refuse to pass unless the consumer really failed against a dead database first | `ci-integration.sh:126-178` |
| Forecast card rendering | local-today cutting, null handling, plus a real browser re-proof | `test_forecast.py`, `browser_gate.py:173` |
| Planner as a pure function | ordering, tie-breaking, interest bonus, repeat penalty, venue matching | `test_planner.py`, `test_planner_venues.py` |

Baseline on `a21dff9`, run in an image built from `tests/Dockerfile`:
**1486 passed, 2 skipped** under `--network none`. The 2 skips are the README
count checks, which only run in CI's `guard` job against the full checkout.

---

## 1. Defect: a saved itinerary was lost whenever the database was down

**Component:** API write path. **Requirement touched:** M4 (all writes travel
the queue), M11 (no data loss under temporary failure), and the trip planner
flow.

`services/api/main.py`'s module docstring states the rule plainly:

> Writes do not touch the database at all. A POST, PATCH or DELETE is fsynced
> into this service's own outbox and answered `202 Accepted`.

`DELETE /itineraries/{id}` was pinned to that rule by
`test_itinerary_delete.py:11`, which makes a database read raise. `POST
/itineraries` had no such test, and it called `queries.coverage(pool.conn)`
inside the handler to stamp the plan's `as_of`.

`db.Pool.conn` reconnects through `db.connect`, which retries **forever** with
backoff. So with Postgres down the POST did not fail fast. It blocked its
uvicorn worker for the length of the outage, while DELETE and PATCH on the same
service went on accepting normally. The UI gives up at `API_TIMEOUT_S = 180`
and tells the traveller "Your change could not be saved" — and the itinerary
they just built is gone, on a service whose outbox was sitting there ready to
make it durable.

**How it was proved, before any fix.** The first version of
`tests/unit/test_itinerary_save.py` did not fail. It *hung* — which is exactly
what the worker does:

```
$ docker run --rm --network none aow/tests:b1-review \
    pytest tests/unit/test_itinerary_save.py::test_save_is_accepted_while_the_database_is_unreachable
exit=124   (killed after 60s, still running)
```

**The fix.** `services/common/db.py` gains `Pool.conn_if_up`: one immediate
attempt, `None` on failure, never a sleep. `db.connect` gains an `attempts`
argument (`0`, the default, keeps the retry-forever behaviour every background
loop relies on). `services/api/main.py` stamps through a new `scoring_as_of()`
that uses it and falls back to the service's own clock — the same fallback the
handler already used when coverage held no weather row.

The fallback is pessimistic in the safe direction. A plan stamped slightly late
reads as *stale* against a newer forecast, which the UI says out loud; it never
reads as fresher than the data behind it.

**New tests** — `tests/unit/test_itinerary_save.py`, 11 cases:

- accepted with no connection available, and with a connection that breaks
  mid-read (the more common shape: Postgres going away does not mark the socket
  closed until something uses it)
- the blocking accessor is never reached — asserted by making it raise, so the
  regression fails in milliseconds instead of hanging the suite
- a broken connection is dropped rather than reused
- a record accepted during an outage still passes `schemas.validate`, so an
  outage cannot manufacture a poison message out of a good save
- a readable database still supplies the forecast's own `as_of`, so the
  fallback cannot quietly become the normal path
- a `datetime` from the driver is `.isoformat()`d, not `str()`d — `str()` on a
  `datetime` puts a space where the ISO `T` belongs, and the unit suite never
  connects to a real database, so only an explicit case pins it

---

## 2. Defect: a half-expired snapshot answered for days it had no data for

**Component:** agent retrieval and grounding. **Requirement touched:** the hard
rule that a question outside the coverage gets an explicit "no data for that
date", never a guess — in the form a reviewer is most likely to meet.

The coverage gate in `router.retrieve` refuses a question whose *whole* window
is outside the stored forecast, and `test_router.py` pins that. Nothing covered
the case in between, which is the **ordinary** state of an air-gapped stack: the
snapshot was staged on Monday, it is Thursday, and "what can I do this week in
Rome?" asks about four days that no longer have rows.

`covered_days` gated on `bool(...)` — *any* covered day passed. The queries were
then clipped to the covered range, but the full window survived everywhere the
reader and the validator look. Confirmed on this branch before the fix, with a
snapshot ending 2026-09-27 and a week running to 2026-10-01:

```
refusal: None       forecast rows: 09-25, 09-26, 09-27      gaps: []
allowed_dates contains 2026-10-01: True
violations("... On 2026-10-01 you can go sightseeing under clear skies.") -> []
```

Four of the seven days vanished with no sentence anywhere saying so, and a model
answer asserting weather on a day with no rows passed the grounding validator
clean. The only trace was the footer's coverage line, which the reader would
have to cross-check by hand against a heading claiming the full week.

**The fix.** `Retrieval` records `covered_days` / `uncovered_days`, filled only
when the question actually needed weather (a pure `where` question is answerable
from a fully expired snapshot and must not be narrowed). `grounding.build` uses
the covered days as `window_days`, which is what `Brief.allowed_dates()` unions,
so the validator stops accepting uncovered dates. `_gaps` emits a
`coverage:partial` gap naming the missing span and where the forecast ends, and
`prompt_block` shows it to the model *before* the rows, so the good answer stays
the common case rather than a validator rejection and a fallback to `render`.

The heading deliberately still states the **asked** window. Shrinking it would
misreport the question; what it may not do is state a week, show three days, and
explain nothing. The gap sentence is written by code and appended after the
model, so it survives any wording.

**New tests** — `tests/unit/test_partial_coverage.py`, 6 cases: the gap sentence
exists and names both the span and the real end of the forecast; uncovered days
are absent from `allowed_dates()`; an invented day is a violation while a
grounded sentence is not; the rendered answer carries one forecast and one
verdict line per covered day plus the limit; the prompt block warns the model;
and a **control** — a snapshot that reaches the end of the week gains no gap and
loses no day, so the fix cannot pass by always truncating.

---

## 3. Gap: the poison / retry / ack decision had no test at any level

**Component:** `services/common/rabbit.py`. **Requirement touched:** M11 and the
DLQ + redrive path the README describes.

No test in the repository imported `services/common/rabbit.py`. `consume` makes
the one decision the no-data-loss claim rests on, once per delivery: handler
returned → `basic_ack`; `Poison` → `basic_nack(requeue=False)`, dead-lettered;
anything else → `basic_nack(requeue=True)`, retried.

The retry branch is exercised by the CI outage drills against a real broker. The
**poison branch is exercised by nothing** — the DLQ drill lives only in
`demos/02_no_data_loss.sh`, and CI runs `bash -n` over the demos rather than
running them. Flipping `requeue=False` to `True`, or ordering `except Exception`
before `except Poison`, is green everywhere today: an unprocessable message
would requeue, be caught only by `x-delivery-limit` five redeliveries later, and
never appear in the DLQ the redrive tool reads.

**New tests** — `tests/unit/test_consume_acking.py`, 10 cases against a scripted
channel, no Docker: each of the three branches in isolation; a `Poison`
**subclass**, so swapped `except` clauses are caught rather than passing by luck;
a mixed batch proving one bad message does not stop the ones behind it; the
`(None, None, None)` idle tick reaching neither the handler nor an ack; a
delivery with no `message_id`; the routing key and message id arriving intact;
and the queue declaration itself — dead-letter exchange, a finite delivery limit,
bounded prefetch — since `basic_nack(requeue=False)` only reaches a DLQ because
of it.

---

## 4. Gap: an older forecast redelivered late (integration)

**Component:** consumer upsert. **Requirement touched:** M11, delivery after a
temporary failure.

At-least-once plus a requeue means the broker can hand the consumer two
refreshes of the same city-day in the wrong order. They carry different
`message_id`s, so `ingest_log`'s primary key — which every other drill leans on
— does not deduplicate them. What protects the row is one line of SQL in
`upsert_weather` (`services/consumer/main.py:210`):

```sql
WHERE EXCLUDED.as_of > weather_daily.as_of
```

Nothing exercised it. Every existing drill replays the *same* message rather
than an older one. Remove that line, or weaken it to `>=`, and the entire suite
including all five outage drills stays green — while a stale forecast overwrites
a newer one, `RETURNING revision` reports a change, `score_defaults` re-runs and
resets every recommendation for that day from the old numbers, and the revision
trigger files a bogus revision. Drills 3 and 5 deliberately cause requeues, so
this reordering is a state the stack actually reaches.

This one cannot be tested honestly without real Postgres: `ON CONFLICT … DO
UPDATE … WHERE … RETURNING`, `TIMESTAMPTZ` comparison and the revision triggers
are Postgres semantics, and a SQLite stand-in would be testing a different
statement.

**New drill** — `tests/integration/stale_forecast.py`, wired into
`scripts/ci-integration.sh` before the reconciliation audit, where the stack is
already up. It accepts a newer forecast through the API's own outbox, waits for
it in `ingest_log`, then accepts an older one behind it and asserts the row kept
its `as_of`, kept its values, and did **not** bump its revision. The stale
message is expected to be stored — it was delivered and acked, and that is
correct; what must not happen is the row moving backwards.

---

## 5. Commands and results

All local gates were run in an image tagged `aow/tests:b1-review`, built from
the repository's own `tests/Dockerfile`, so the pinned ruff and pytest are the
ones CI uses. The integration drill ran in its own Compose project
(`aow-ci-b1review`) with its own volumes, and started no port-publishing
service, so a separate stack already running on this machine was untouched.

| Gate | Command | Result |
|---|---|---|
| baseline unit | `docker run --rm --network none aow/tests:b1-review` on `a21dff9` | 1486 passed, 2 skipped, 13.9s |
| unit | same, on this branch | **1513 passed, 2 skipped**, 13.4s |
| ruff check | `docker run --rm --network none aow/tests:b1-review ruff check services tests scripts` | passed |
| ruff format | `… ruff format --check services tests scripts` | 107 files already formatted |
| integration | `AOW_CI_SERVICES_IMAGE=aow/services:b1review GITHUB_RUN_ID=b1review bash scripts/ci-integration.sh` | **exit 0**, whole script |

The integration run is the existing script with the new step in it, not a
reduced version of it. Everything that passed before still passed — five outage
drills, the reconciliation audit and replay with its stored-ID control, and the
final `count(*)` gate over all six traced IDs after a full restart — and the new
step reported:

```
PASS: lisbon 2026-09-23 kept as_of 2026-09-25 13:56:04.926500+00:00 and
      revision 2 after an older forecast was redelivered behind it
PASS: all 6 traced IDs (...) are stored exactly once after full recovery
```

That run is against the file as committed. An earlier run of the same script
passed identically before a formatting-only change to one `assert`; it was
re-run rather than cited across the edit.

**No CI wiring was needed.** The new unit files are collected by the `unit`
job's `pytest` (`testpaths = tests/unit`), and the new drill is inside
`scripts/ci-integration.sh`, which `build-and-scan` already runs on every PR.
`bash -n scripts/*.sh` in `guard` covers the edited script.

### The tests were checked against the defects they claim to catch

A test that passes on broken code is the failure mode this repository keeps
finding in its own proofs, so each new file was run against a deliberately
reverted source:

| Mutation | Expected | Observed |
|---|---|---|
| `basic_nack(requeue=False)` → `requeue=True` in `rabbit.consume` | poison cases fail | **3 failed**, 7 passed |
| `window_days` back to the full asked window in `grounding.build` | the validator turns permissive | **2 failed**, 4 passed |
| `scoring_as_of` uses `pool.conn` again | the save is lost | **6 failed**, 5 passed |
| `WHERE EXCLUDED.as_of > weather_daily.as_of` deleted from `upsert_weather` | the integration drill fails | script **exit 1** |

The fourth is the one worth reading, because it is the case that no existing
gate caught. The whole of `ci-integration.sh` was re-run against an image built
from the mutated source, in its own project (`aow-ci-b1mut`). Every prior step
still passed — the mutation is invisible to all of them — and the new drill
failed with the numbers in it:

```
AssertionError: an older forecast overwrote a newer one:
as_of went 2026-09-25 13:53:59.503402+00:00 -> 2026-09-25 12:53:59.503402+00:00
```

Each source file was restored immediately afterwards, and `git diff` was
checked: the mutations existed only in throwaway images, never in a commit.

---

## 6. What this does not cover

Stated plainly, because B1 remains partially met and a list of what was added is
not a list of what is safe.

- **B1 is not closed.** Four behaviours were pinned. The audits produced other
  ranked gaps that were deliberately left, listed below so they are not lost.
- **Not addressed, and known:**
  - `services/tools/redrive.py` has no test. Moving its `basic_ack` above the
    `basic_publish` would drop an already-quarantined message from both the DLQ
    and the exchange; deleting its `seen` set would report "redriven N" while
    nothing moved.
  - `reconcile`'s pagination is only ever exercised with 1–2 rows, a single
    page. A break after the first page would silently audit only the first 200
    records and report `missing: 0` for a record beyond it.
  - The consumer's "never ack when the commit failed" invariant is covered by
    the Docker drills for `OperationalError`, but not for a commit-time failure
    of another shape (serialisation failure, deadlock, a trigger raising).
  - The built-plan render and save path in the UI (`render_save`) is not
    exercised by any test; only the *reopen* branch is. A change to the POST
    body it sends, or to the endpoint it posts to, would not be caught.
  - Both branches of the saved-plan staleness notice are unreachable under the
    current UI fixture, whose saved itinerary carries exactly the current
    `weather_as_of`.
  - The three near-identical publisher drain loops (API, ingestor, enricher)
    have uneven coverage; a fix applied to one and missed in another would not
    be caught.
- **The E1/E2 example answers still have no automated gate.**
  `demos/05_questions.sh` asserts them properly, but it needs a live stack with
  the real model and is run by hand. §2 above now pins the freshness half of E1
  at unit level; the full question-to-answer path is still proved only by the
  manual demo and the release-candidate `model-grounding` job.
- **`scoring_as_of`'s fallback is a clock, not a forecast stamp.** During an
  outage a saved plan is stamped with the API's own time. A cleaner fix would
  have the UI send the `as_of` it already displays for the plan it built, making
  the database read unnecessary in the normal path as well. That is a contract
  change to `ItineraryIn` and was left for the integrator.
