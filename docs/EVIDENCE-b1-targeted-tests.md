# Evidence: targeted test coverage (B1)

**Dated 2026-09-25.** Branch `review/b1-targeted-tests`, based on `a21dff9`.
**Revised after review of `bf1d2a8`** — §1 and §2.1 record what that review
corrected and why the first fix was not good enough.

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

**The first fix was not good enough, and review said so.** It kept the read and
made it non-blocking: a `Pool.conn_if_up` accessor that tried once, with the
service's own clock as the fallback. Two things were wrong with that. The
"one attempt" still had no time bound — a connection attempt to a host that
drops packets waits on the TCP stack, not on a retry loop — so the write still
depended on the database, just less often. And the clock fallback stamped the
plan with a time no forecast ever carried, which the UI then compared against
the live window and announced as *"has been refreshed since this was saved"* —
a stale-data warning manufactured out of a database outage.

**The fix as committed.** The stamp travels with the record. The plan
`agent.build_itinerary` returns already carries the as-of of the snapshot it
scored against and the UI already displays it; `render_save` now sends it, and
the handler reads nothing at all. `db.py` is back to its original form —
`conn_if_up` and the `connect(attempts=…)` path are gone, because nothing needs
them.

**The compatibility decision, for callers that omit `as_of`.** The request is
still accepted (`202`) and the record stores `NULL`; migration `008` drops the
`NOT NULL` on `itineraries.as_of`. It is deliberately not filled in. Refusing
would break callers written before the field existed, and substituting a clock
would put an invented provenance on a stored record — of the two, only a
missing timestamp can be read for what it is. The UI renders it as
*"scoring timestamp not recorded, so it cannot be compared with what is stored
now"* and skips the staleness comparison entirely.

**New tests** — `tests/unit/test_itinerary_save.py`, 13 cases: an HTTP save
through `TestClient` against a `Pool` whose every accessor raises, so touching
the database fails in milliseconds rather than hanging; the stamp is the
caller's, not the clock; the omitted-`as_of` path accepted and stored as
`None`; the payload re-validated the way the consumer will validate it; a
naive timestamp carried through rather than silently rewritten to UTC; a
clock-skewed future stamp accepted rather than quietly corrected; and
`upsert_itinerary` writing `NULL` rather than refusing it — because a record
the API accepts and the consumer rejects is a dead letter the outage created.

**And in the UI** — `tests/unit/test_ui.py`, 5 cases. The built-plan branch of
`render_plan` had never executed under test: every existing planner test
reaches the renderer through "Open", which takes the *saved* branch. The
harness now records what the app sends (`sent`) and can answer a write with a
real body (`replies`), which is what makes `POST /agent/itinerary` — a write
that returns a plan — reachable at all. The cases are: the built plan renders
its as-of, its coverage line, its outside-coverage warning and its score pill;
the save posts `days`, `city`, the dates and the scoring `as_of` to
`/itineraries`; a plan with no as-of says so instead of "as of never"; a saved
plan whose forecast has moved on raises the staleness notice naming the current
stamp; and a saved plan with no as-of is *not* called stale.

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

**The fix.** `Retrieval` records `covered_days` / `uncovered_days` and a
`weather_scoped` flag. `grounding.build` uses the covered days as
`window_days`, which is what `Brief.allowed_dates()` unions, so the validator
stops accepting uncovered dates. `_gaps` emits a `coverage:partial` gap naming
them, and `prompt_block` shows it to the model *before* the rows, so the good
answer stays the common case rather than a validator rejection and a fallback
to `render`.

The flag matters and is not decoration: a pure `where` question is answerable
from a fully expired snapshot and must never be narrowed, while "no covered day
at all" is a real answer for a weather question. Keying the decision on the
emptiness of the list instead would have restored the hole in exactly the case
§2.1 describes.

### 2.1 What review caught: the missing days are not always a tail

The first version derived the covered days from `queries.in_coverage` and
described the missing ones as a single `first`-to-`last` span ending at
`weather_last_date`. Both are wrong outside the trailing case, and review said
so.

**`coverage` is global, not per city.** `COVERAGE_SQL` reports
`MIN/MAX(forecast_date)` over the whole of `weather_daily` with no city
predicate (`services/common/queries.py:97-116`), and `in_coverage` never
receives a city (`:260`). So the window says "covered" for a day *this* city
has no row for whenever one city was ingested further ahead than another, or a
single day's ingest failed. The covered days are now taken from the forecast
rows that actually came back, which is the only version that is true per city —
and it is also what makes a missing day in the *middle* of the window visible
at all.

**The wording now names only the missing dates.** Consecutive dates collapse
into a range and non-consecutive ones do not, so a week whose middle day failed
reports `2026-09-27`, not `2026-09-25 to 2026-10-01` — which would have denied
six stored days. The explanation is chosen to match where the days sit:

| shape | sentence |
|---|---|
| all after the stored window | "The stored forecast ends on *last*" |
| all before it | "The stored forecast begins on *first*" |
| both ends, or inside the window | "The stored forecast covers *first* to *last* and has no row for *city* on them" |

The middle row is the one the old wording got actively wrong: "the forecast
ends on 2026-10-01" for days missing at the *start* of the week is both false
and points at the wrong cause.

The heading deliberately still states the **asked** window. Shrinking it would
misreport the question; what it may not do is state a week, show three days, and
explain nothing. The gap sentence is written by code and appended after the
model, so it survives any wording.

**New tests** — `tests/unit/test_partial_coverage.py`, 16 cases. The fixture
takes the exact set of days the database holds rows for, plus the global
coverage bounds, so every shape is reachable:

- **trailing** (the original case): the gap names `2026-09-28 to 2026-10-01`
  and says the forecast ends on `2026-09-27`; uncovered days are absent from
  `allowed_dates()`; an invented day is a violation while a grounded sentence
  is not; the rendered answer carries one forecast and one verdict line per
  covered day plus the limit; the prompt block warns the model
- **leading**: names `2026-09-25 to 2026-09-26` and says the forecast *begins*
  on `2026-09-27` — asserted together with `"ends on" not in sentence`
- **both ends**: two runs listed separately, and neither "begins" nor "ends"
  claimed
- **inside the window**: one day with no row for this city while the global
  bounds span the whole week — the case `in_coverage` cannot see at all — plus
  a dated claim about it rejected by `violations`
- **two separate internal gaps**: listed as `2026-09-26, 2026-09-29`, not
  collapsed into a range
- **no rows for this city at all** inside an advertised window: no day is
  assertable
- a **control** — a snapshot that reaches the end of the week gains no gap and
  loses no day, so the fix cannot pass by always truncating
- the date grouping itself, parametrised, including across a month boundary

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
| unit | same, on this branch | **1530 passed, 2 skipped**, 14.3s |
| ruff check | `docker run --rm --network none aow/tests:b1-review ruff check services tests scripts` | passed |
| ruff format | `… ruff format --check services tests scripts` | 108 files already formatted |
| snapshot manifest | `python3 scripts/snapshot_manifest.py --check` | passed (it counts data, not migrations) |
| integration | `AOW_CI_SERVICES_IMAGE=aow/services:b1review GITHUB_RUN_ID=b1followup bash scripts/ci-integration.sh` | **exit 0**, whole script |

The integration run is the existing script with the new step in it, not a
reduced version of it. Everything that passed before still passed — five outage
drills, the reconciliation audit and replay with its stored-ID control, and the
final `count(*)` gate over all six traced IDs after a full restart — and the new
step reported:

```
PASS: lisbon 2026-09-23 kept as_of 2026-09-25 14:13:49.954997+00:00 and
      revision 2 after an older forecast was redelivered behind it
PASS: all 6 traced IDs (...) are stored exactly once after full recovery
```

This run also applies migration `008` against real Postgres and exercises the
nullable `itineraries.as_of` end to end, which is why it was re-run for the
follow-up rather than cited from the first pass.

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
| `scoring_as_of` uses `pool.conn` again *(first pass, before the handler stopped reading at all)* | the save is lost | **6 failed**, 5 passed |
| `WHERE EXCLUDED.as_of > weather_daily.as_of` deleted from `upsert_weather` | the integration drill fails | script **exit 1** |
| covered days back to `queries.in_coverage`, i.e. the global window | leading, internal and per-city cases fail | **3 failed**, 13 passed |
| date runs back to one `first`-to-`last` span | both-ends and split-gap cases fail | **4 failed**, 12 passed |
| `render_save` stops sending `as_of` | the save loses its provenance | **1 failed**, 37 passed |

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
- **A weather claim with no date in it is still not checked.** The gap fixed in
  §2 covers *dated* assertions: `allowed_dates()` no longer contains a day with
  no row, so "on 2026-10-01 it is sunny" is a violation. But no check in
  `violations` requires a weather claim to rest on a `DayFact`, so undated
  prose — "Rome is warm and dry this week" — passes with an empty brief. The
  test file says so at the point where it would otherwise have asserted it,
  rather than asserting the present behaviour and freezing it. Closing it means
  a new check in `grounding.violations` shaped like check 4, with the negated
  and `RECORD_PHRASES` escape hatches so "I have no forecast on record" still
  passes. Deliberately out of scope for this follow-up.
- **`build_itinerary` still filters its days through the global
  `queries.in_coverage`** (`services/agent/main.py:404`), so the planner can
  still offer a day that this city has no row for — it renders as "no scored
  activity" rather than as a stated gap. That is the same class of defect as
  §2.1 in a different code path; it was left alone because the follow-up was
  scoped to the agent's answer path.
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
  - The three near-identical publisher drain loops (API, ingestor, enricher)
    have uneven coverage; a fix applied to one and missed in another would not
    be caught.
- **The E1/E2 example answers still have no automated gate.**
  `demos/05_questions.sh` asserts them properly, but it needs a live stack with
  the real model and is run by hand. §2 above now pins the freshness half of E1
  at unit level; the full question-to-answer path is still proved only by the
  manual demo and the release-candidate `model-grounding` job.
- **A stored itinerary can now carry no scoring as-of.** That is the deliberate
  compatibility choice in §1, not an oversight: a caller that omits `as_of`
  gets `NULL` rather than an invented timestamp. The consequence is that such a
  plan cannot be compared against the current window, so the UI states that
  instead of showing a staleness verdict. The UI itself always sends the value.
- **The `restore-drill` and the offline bundle have not been re-run** since
  migration `008`. It is an `ALTER … DROP NOT NULL` inside a transaction,
  applied by the same `migrate` service the integration gate exercised and
  passed, and it does not rewrite the table — but the release-candidate
  restore path and a packaged bundle install are release gates that this
  session did not run, and saying they passed would be a claim about something
  nobody checked.
