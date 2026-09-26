# Evidence: targeted test coverage (B1)

**Dated 2026-09-25. Rebased and re-verified 2026-09-26.** Branch
`review/b1-targeted-tests`, now based on **`1890eba`** (`origin/main`, PR #49,
the F10 physical air-gap proof). **Revised after review of `bf1d2a8`** — §1 and
§2.1 record what that review corrected and why the first fix was not good
enough.

The work was originally built on `a21dff9`, which `origin/main` had moved past
while it was in progress. The rebase was clean: no conflicts, no file overlap
with what `1890eba` landed, and `git range-diff` reports all three commits
identical apart from their parents. §5 carries both sets of numbers — the
current ones on `1890eba`, and the pre-rebase ones on `a21dff9`, which is the
tree the defect reproductions and mutation checks were performed against.

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

Baseline on `a21dff9`, the tree this pass was written against, run in an image
built from `tests/Dockerfile`: **1486 passed, 2 skipped** under `--network
none`. The 2 skips are the README count checks, which only run in CI's `guard`
job against the full checkout. Current figures after the rebase are in §5.

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

**Current — on the rebased branch (base `1890eba`).** Image
`aow/tests:b1-rebased`, rebuilt from scratch after the rebase.

| Gate | Command | Result |
|---|---|---|
| unit | `docker run --rm --network none aow/tests:b1-rebased` | **1554 passed, 2 skipped**, 44.9s |
| ruff check | `… ruff check services tests scripts` | **passed** |
| ruff format | `… ruff format --check services tests scripts` | **109 files already formatted** |
| snapshot manifest | `python3 scripts/snapshot_manifest.py --check` | **passed** (it counts data, not migrations) |
| integration | `AOW_CI_SERVICES_IMAGE=aow/services:b1rebased GITHUB_RUN_ID=b1rebased bash scripts/ci-integration.sh` | **exit 0**, whole script |

The integration gate was re-run on the rebased tree specifically because
migration `008` is the reason to care: `migrate` applies it against real
Postgres before anything else starts, so a migration that failed on this base
would take the whole script down. It passed, in its own Compose project
(`aow-ci-b1rebased`) with its own volumes, and tore itself down afterwards; the
separate `aow` stack running on this machine was untouched throughout. Every
step passed — the snapshot-to-history smoke, the enricher probe, reconnect, the
local-event-day and event-freshness/recheck drills, the out-of-order forecast
drill, the reconciliation audit and replay with its stored-ID control, five
outage drills across both outboxes, and the final `count(*)` gate over all six
traced IDs after a full restart:

```
PASS: lisbon 2026-09-23 kept as_of 2026-09-26 00:55:45.435720+00:00 and
      revision 2 after an older forecast was redelivered behind it
PASS: five outages across the API and ingestor outboxes each committed once
      and survived a full restart
PASS: all 6 traced IDs (...) are stored exactly once after full recovery
```

Where the 1554 comes from, measured rather than assumed: the three new test
files collect 10 + 13 + 16 = **39**, and five more were added to
`test_ui.py`, so this branch contributes **44**. Running the suite with the
three new files ignored gives 1554 − 39 = **1515**, which corroborates the
split; subtracting the five `test_ui.py` cases puts the new base's own count at
**1510** (derived, not separately measured). The suite is slower than before —
44.9s against 15.2s — because `1890eba` added `test_airgap_evidence.py` and
extended `test_bundle_tamper.py`, both of which do real archive work.

**Pre-rebase history — on base `a21dff9`.** Kept because the defect
reproductions and the mutation checks below were performed against this tree,
and their numbers refer to it. Image `aow/tests:b1-review`.

| Gate | Command | Result |
|---|---|---|
| baseline unit | `docker run --rm --network none aow/tests:b1-review` on `a21dff9` | 1486 passed, 2 skipped, 13.9s |
| unit | same, on the branch before rebasing | 1530 passed, 2 skipped, 14.3s |
| unit, re-verified 2026-09-26 | image rebuilt from scratch after a host restart | 1530 passed, 2 skipped, 15.2s |
| ruff check | `… ruff check services tests scripts` | passed |
| ruff format | `… ruff format --check services tests scripts` | 108 files already formatted |
| snapshot manifest | `python3 scripts/snapshot_manifest.py --check` | passed |
| integration | `AOW_CI_SERVICES_IMAGE=aow/services:b1review GITHUB_RUN_ID=b1followup bash scripts/ci-integration.sh` | **exit 0**, whole script |

This pre-rebase integration run is superseded by the one in the table above,
which was executed on the rebased tree. Both passed; the current one is the
evidence.

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

> **This sentence used to read:** "This run also applies migration `008`
> against real Postgres and exercises the nullable `itineraries.as_of` end to
> end." **It was false in both halves**, and it is corrected here rather than
> quietly deleted, because this is an evidence file. Migration `008` was never
> wired into the `migrate` service — `compose.yml` listed each migration with
> an explicit `-f` and stopped at `007` — so no run on this branch had ever
> applied it. And `scripts/ci-integration.sh` posted no itinerary at any point,
> so nothing exercised the column either. Both are fixed in §8, and the
> statement is now true of the run recorded there.

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
- ~~**A weather claim with no date in it is still not checked.**~~ **Closed in
  §8.** Check 4b requires a weather claim to rest on a `DayFact`, with the
  negated and `RECORD_PHRASES` hatches, so "Rome is warm and dry this week" is
  a violation against an empty brief while "I have no forecast on record" is
  not. It is all-or-nothing on the retrieved rows: a *partly* covered week
  still has `DayFact`s, so "warm and dry all week" over one is not caught. That
  residual is stated in the README rather than left implied.
- ~~**`build_itinerary` still filters its days through the global
  `queries.in_coverage`.**~~ **Closed in §8.** The planner now builds its day
  list from the rows stored for the selected city, omits the days it holds
  nothing for, reports them in `requested_days_outside_coverage` so the UI's
  existing "left out rather than guessed" warning fires, and refuses with a
  named 422 for a city it has no rows for in the window. The audit found two
  harms beyond the one recorded here: the day was drawn in the UI as a
  poor-band score pill — a data gap rendered as a verdict on the weather — and
  the phantom days were counted into `title` and `end_date`, so a *saved*
  itinerary recorded a range nothing had been scored across.
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
- **The `restore-drill` and the offline bundle must be re-run** for migration
  `008`. It is an `ALTER … DROP NOT NULL` inside a transaction, it does not
  rewrite the table, and as of §8 it is genuinely applied by the `migrate`
  service the integration gate exercises — but the release-candidate restore
  path and a packaged bundle install are separate release gates, and they are
  run against the exact merged commit, not against this branch. Whether they
  passed is recorded by those runs, not asserted here.

---

## 7. README: applied

`CLAUDE.md` requires that a behaviour change update `README.md` in the same
change. This branch initially did not, deliberately: `README.md` was held out of
its scope while another session rewrote it, and editing it in parallel would have
produced a conflict in the one file `DEVOPS_REVIEW.md` records as having already
cost work once.

That rewrite has landed — PR #51, merged as `6664454`, replacing the ~1900-line
README with 680 lines — and **the wording is now applied**, against the rewritten
sections rather than the ones it was originally drafted for. Those originals
(`## Delivery guarantees, and their boundary`, `### What gets worded, and what
does not`, `## Updating stored information (M12)`) no longer exist, so each
addition was placed by meaning and rewritten to fit the section that now carries
the subject, instead of being pasted in as a block.

| what | where it went | why there |
|---|---|---|
| Partly-expired coverage | `### The data on board, and when it goes stale` | it continues the paragraph that already ends "a question past the window is refused rather than guessed", and the table above it is the per-all-cities coverage the addition warns against reading per city |
| Out-of-order redelivery | `### The delivery guarantee, and its boundary` | it qualifies that section's own claim, that delivery is keyed on `message_id` — two refreshes of one city-day are two ids |
| The itinerary `as_of` contract | `### The delivery guarantee, and its boundary` | the load-bearing half is that the write path reads nothing, which is how a record reaches the outbox that section starts from |

Four further edits went with them, none of which were in the original draft:

- **A stale count, corrected.** The Tests section said "the unit suite is 38
  modules under `tests/unit/`". This branch adds three, so it says 41, and the
  list of what the suite covers now names the consumer's ack decision, the
  partial-coverage answer and a save accepted while the database is unreachable.
  This is the only claim in `README.md` that this branch made **false**; it was
  found by counting the modules rather than by trusting the earlier check, which
  had looked for cited *test* counts and not module counts.
- **The new drill,** in the paragraph describing what `ci-integration.sh` drives.
- **The two open defects** (§6), as entries in `## Known limitations`, phrased as
  limits rather than as bugs, since that is what the section is.
- **The B1 traceability row** now links this file alongside `CICD_EVIDENCE.md`.

`POST /itineraries` also appears under `### The write API has no authentication`.
That mention is about credentials, not provenance, and is unaffected.

**Still to reconcile, at merge time rather than now.** PR #52
(`feat/reviewer-quickstart`) was open when this was written. It does **not** touch
`README.md` — its files are `docs/REVIEWER-QUICKSTART.md`, `scripts/bootstrap.sh`
and `tests/unit/test_bootstrap.py` — so there is no overlap with these edits. An
earlier revision of this section warned that it might move these lines; that
warning was wrong and is withdrawn. If anything else lands in `README.md` before
this merges, re-check the three sections above.

---

## 8. Integration round: what review of this branch found

This branch was reviewed against `main` at `44c008f` before merging, by three
independent passes over the bootstrap/operations surface, the grounding
validator across both open pull requests, and the itinerary, migration and
planner surface. Four defects were found in work this branch had already
recorded as verified. They are listed here because the point of an evidence
file is that it says what is true, including about itself.

### 8.1 Migration `008` was never applied

`compose.yml`'s `migrate` service lists every migration with an explicit `-f`
and stopped at `007`. There is no glob. So the migration this branch added had
never run anywhere — not in the integration gate, not in any drill.

The consequence inverted the compatibility guarantee §1 states. `001_init.sql`
keeps `as_of TIMESTAMPTZ NOT NULL`, so a caller omitting `as_of` was accepted
with `202`, and then `upsert_itinerary` raised `NotNullViolation` in the
consumer. That is neither `Poison` nor `OperationalError`, so it fell past both
`except` clauses into the generic requeue branch and dead-lettered after five
deliveries. Accept, then lose it quietly — the exact failure the change was
written to prevent.

Fixed by wiring the migration. More usefully, `tests/unit/test_migrations_applied.py`
now fails the build when any file in `db/migrations/` is not named in the
`migrate` command; run against the pre-fix `compose.yml` it reports
`008_itinerary_as_of_optional.sql` by name. The one-line fix closes the
instance; the guard closes the class, which is what made this invisible.

### 8.2 The test that should have caught it could not

`test_itinerary_save.py` asserted that a fake cursor received `as_of=None`.
That re-tests pydantic. Migration `008` could be deleted entirely and it still
passed, while its docstring named the live defect. It is now three things at
three honesty levels: a unit test of what the consumer passes through, a test
that reads the committed SQL so `001` and `008` cannot drift apart, and
`tests/integration/itinerary_without_as_of.py`, which saves an itinerary with
no scoring timestamp against a real Postgres and follows it to `stored: true`.

### 8.3 The planner offered days the city had no rows for

Recorded in §6 as open and closed here. See the §6 entry for the two harms
beyond the one originally described.

### 8.4 An undated weather claim was not checked

Recorded in §6 as open and closed here by check 4b.

### 8.5 The coverage-gap sentence failed the checks it was written by

Found only by looking at the two open pull requests together, and the reason
this round mattered. Narrowing `window_days` to the covered days narrowed two
different things: `allowed_dates()`, which is what the change intended, and
`vocabulary()`, which feeds the allowed names and numbers of checks 8 and 9.
At the same time this branch added a prompt section printing the uncovered
dates to the model.

So the model was handed our own gap sentence, told not to repeat it, repeated
it anyway — which `gap_block`'s docstring says it does, and which is why the
`already_said` mechanism exists — and the dates and figures in code's own
sentence were then rejected as unsupported claims. Measured: clean on base
`main`, four violations with this branch's change. The answer was discarded and
the operator log said the model had made a claim the rows do not support, which
was false.

`window_days` now stays the asked window and only `allowed_dates()` narrows,
via `covered_days`/`uncovered_days`/`weather_scoped` on `Brief`; and a sentence
that is verbatim one of our own gap sentences is skipped before any check runs.
A paraphrase is still checked, which is stated in the README as a residual.

### 8.6 Results on the integrated tree

| gate | result |
|---|---|
| unit, `--network none` | **1621 passed, 2 skipped** |
| `ruff check` / `ruff format --check` | clean, 113 files |
| `scripts/ci-integration.sh`, isolated Compose project | **exit 0**, with migration `008` genuinely applied and the itinerary-without-`as_of` drill passing |
| `bash -n` on the edited shell | clean |

Every new test was checked against the defect it claims to catch by reverting
the source hunk and confirming the failure, on this base rather than on the
base the audit was written against.
