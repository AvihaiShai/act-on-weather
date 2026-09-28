# Evidence: the live reading of `78ef8f6`

**Dated 2026-09-28 (UTC).** Executed against the running `aow-demo` stack while
`main` and `origin/main` were both
`78ef8f6e8d80c137ffa44cf0a025047c1d3afe91` (the merge of PR #67).

Until this file existed, every repair in that merge — typed-activity resolution,
the name-keyed grounding guard, the inland-coast refusal, the `rule_version`
restamp, the mixed-question fix — was backed by unit tests and exact-SHA CI and
by nothing that had been run against a stack. The internal integration handoff
for that merge said so itself: its live-phase section was headed "NOT YET RUN".
This file is that live phase.

**A note on the internal documents named below.** This file was written in a lane
that also produced working notes — per-area review handoffs, a migrations
amendment and a presenter's demo script. Those are lane scaffolding addressed to
other sessions, and they are **not part of the submission**: they live under
`.private/`, which is gitignored, so nothing here cites them by path. Where one is
named it is named as an internal note, and the reading it supplied is stated in
full so this file stands on its own without it.

**Evidence levels**, the same scale those internal notes use, so they can be read
together: **L1** executed fresh here · **L2** exact-SHA CI transcript · **L3**
older or different-SHA run · **L4** source reading · **L5** unverified
documentation or operator report. Every figure below is L1 unless the line says
otherwise, and every L1 line names the container and the command behind it. The
three readings taken from a document rather than from the stack are labelled L4
in place — the "NOT YET RUN" heading above, the migrations amendment quoted in
§5, and the demo script's expected output quoted in §6.4. Section 8 lists what is
still not verified.

## 0. What was under test, and why no rebuild was owed

| | |
|---|---|
| Compose project | `aow-demo` |
| Config path | `$WORKTREE/compose.yml` (L1 — `docker inspect aow-demo-api-1` on the `com.docker.compose.project.config_files` label) |
| Env file | `$REPO/.env`, passed with `--env-file`; the worktree holds no copy |
| Source worktree | `$WORKTREE` @ `68eacf4c147f2db8404c395fda5d7898e9255b87`, clean tree (L1 — `git -C "$WORKTREE" rev-parse HEAD`, `git status --short` empty) |
| Images | `aow/services:dev`, `aow/ui:dev`, `postgres:17-alpine`, `rabbitmq:4.3-management-alpine`, `nginx:alpine`, `ghcr.io/ggml-org/llama.cpp:server` |
| Rebuilt for this file | **nothing** |

`68eacf4` is the branch tip PR #67 merged and `78ef8f6` is the merge commit, and
they carry **the same tree**: `git rev-parse 78ef8f6^{tree}` and
`git rev-parse 68eacf4^{tree}` both print
`b4fcd3db0d715bfe7cecb4a80c5a1a33748ebadd`, and `git diff 68eacf4 78ef8f6` is
empty (L1). That is why a stack launched from that worktree is a stack running
HEAD's tree, and why nothing here needed building.

`$REPO` is this repository's primary checkout and `$WORKTREE` a second
`git worktree` checkout of it, which is where the running stack was launched
from. The two are named that way rather than written out because the absolute
paths are one operator's machine and carry nothing a reader can use: what makes
the run reproducible is the commit and the Compose project, both of which are
above.

Every Compose command below was run as

```
docker compose -p aow-demo -f "$WORKTREE/compose.yml" --env-file "$REPO/.env" ...
```

and every `psql` as `docker exec aow-demo-postgres-1 psql -U aow -d aow`.

---

## 1. The read-only baseline, and the database behind it

Each API figure was cross-checked against the table it derives from. Counts come
from `/coverage`'s `entities[].rows`, never from `curl /events | wc -l` — that
route defaults to `limit=50` and returns single-line JSON, confirmed live in §7
(H12).

**This is the baseline as it stood before this file wrote anything**, so four of
its figures are one row lower than the same figures re-read at the end. Everything
this file wrote is listed in §9; one of those writes — the `kite flying` request
for Rome that §6.4 item 3 calls for — adds a **recommendations** row, so
`recommendations`, its `rule_version` count, the `ready` half of the wording queue
and Rome's row count in §1.1 all read one higher afterwards. The **after** column
below is that later reading, cross-checked in §5.5. A reviewer re-running §1 on a
stack that has served a typed request will see the after column, not the baseline.

| reading | API | database | agreed | after the writes in §9 |
|---|---|---|---|---|
| events | 55 | 55 published (56 total, one retracted in §3.2) | yes | 55 published, 56 total |
| places | 620 | 620 | yes | 620 |
| facts | 81 | 81 | yes | 81 |
| weather rows | 80 | 80 | yes | 80 |
| recommendations | 1360 | 1360 | yes | **1361** |
| itineraries | 0 | 0 | yes | 0 |
| sample events | 0 | 0 (`count(*) filter (where is_sample)`) | yes | 0 |
| forecast window | 2026-09-23 to 2026-10-08 | `min/max(forecast_date)` identical | yes | unchanged |
| `rule_version` | 4 on every row | 1360 rows at 4, no other value | yes | **1361** rows at 4 |
| wording queue | `{"deferred": 880, "ready": 480}`, no `pending`, no `failed` | `status` split identical | yes | `{"deferred": 880, "ready": **481**}` |

API: `curl -s 127.0.0.1:8000/coverage`, `curl -s 127.0.0.1:8000/enrichment`.
Database: one `UNION ALL` of `count(*)` per table, plus `GROUP BY status` and
`GROUP BY rule_version`.

**The window covers the demo.** Today is 2026-09-28 and "tomorrow" is
2026-09-29; both fall inside 2026-09-23 to 2026-10-08. `event_freshness` reads
`current 55, expired 0, samples 0, oldest_check 2026-09-24T18:00:00Z,
next_expiry 2026-10-15T18:00:00Z, cities_covered 5`.

### 1.1 The sea cap, per city

`curl -s 127.0.0.1:8000/recommendations/<city>`, then the maximum score, the band
set and the `rule_version` set over the four sea activities (`surfing`,
`fishing`, `boat_ride`, `swimming`):

Read at the same baseline as §1, so Rome's row count is the one figure here that
moves: the `kite flying` write adds one Rome row and it reads **289** afterwards.
No sea figure changes — `kite_flying` is not a sea activity.

| city | rows | sea rows | sea max | sea bands | `rule_version` | reasons with "too flat" |
|---|---|---|---|---|---|---|
| Rome | 288 (289 after §9) | 64 | **69** | `fair` | 4 | **0** |
| Lisbon | 288 | 64 | 69 | `fair`, `poor` | 4 | 0 |
| Tel Aviv | 288 | 64 | 69 | `fair` | 4 | 0 |
| Reykjavik | 288 | 64 | 69 | `fair`, `poor` | 4 | 0 |
| London | 208 | **0** | — | — | 4 | 0 |

The withdrawn "too flat" reason is gone from the whole table, not only from the
per-city reads: `SELECT count(*) FROM recommendations WHERE reasons::text ILIKE
'%too flat%'` returns **0**.

London's 208 is 80 short of 288 because five coast-gated activities are refused
outright for a city with no coast on record rather than scored and hidden:
`beach_day`, `boat_ride`, `fishing`, `surfing`, `swimming` (L1 — set difference
of the activity keys in Rome's and London's reads; 5 x 16 days = 80).

**One thing the sea-cap gate does not cover, and a presenter should know.**
`beach_day` is coast-gated but deliberately **not** sea-capped, and it reaches
`100`/`good` in Rome, Lisbon and Tel Aviv (L1 — `GROUP BY city_id` over
`activity='beach_day'`; Reykjavik peaks at 55/`fair`). That is by design, and the
catalogue says so in place: *"`beach_day` is the exception and keeps its full
range: a day on the sand is a judgement about sun, heat, rain and wind, and those
are measured"* ([data/activities.yml:249-251](../data/activities.yml#L249-L251),
L4). So the defensible sentence is "the four **sea-dependent** activities are
capped at 69", never "a sea activity can never be good" — a reviewer who opens
the Suitability page on Rome will find a beach day at 100.

### 1.2 Migrations 001-010, by applied object

There is no `schema_migrations` ledger, so applied state is only observable
through objects. Two independent readings:

**Provenance.** `aow-demo-migrate-1` is `Exited (0)`, created
2026-09-28 03:40:10 +0300, and its `Config.Cmd` names ten files, `001_init.sql`
through `010_retraction_ledger.sql` (L1 — `docker inspect`).

**Objects.** For migration **009**: nine columns exist —
`retracted_at`, `retraction_reason` and `retracted_by` on each of `events`,
`places` and `facts`, all nullable — and the three partial indexes are present
with the predicate `WHERE (retracted_at IS NULL)`:

```
events_published_idx :: CREATE INDEX events_published_idx ON public.events
  USING btree (city_id, starts_at) WHERE (retracted_at IS NULL)
places_published_idx :: ... (city_id, category) WHERE (retracted_at IS NULL)
facts_published_idx  :: ... (city_id) WHERE (retracted_at IS NULL)
```

For migration **010**: `record_retractions` exists with its six declared columns,
and — the reading that distinguishes 010 from an earlier version of the same file
— `pg_attribute.attnotnull` for `recorded_at` is **false**, which is the
`ALTER TABLE ... ALTER COLUMN recorded_at DROP NOT NULL` at
[db/migrations/010_retraction_ledger.sql:105](../db/migrations/010_retraction_ledger.sql#L105).
Its grants are exactly the two the file writes at `:279-280`: `aow_reader`
`SELECT`, `aow_writer` `INSERT, SELECT, UPDATE`.

---

## 2. The write path: a coast activity for an inland city stores no row

This is the one behaviour where the agent half and the consumer half meet, and no
test spans both. Before: zero `surfing` rows for London.

```
curl -s -X POST 127.0.0.1:8000/recommendations -H 'content-type: application/json' \
  -d '{"city":"london","forecast_date":"2026-09-29","activity":"surfing"}'
-> 202 {"accepted":true,"message_id":"9ac514ea-c825-430f-8cd3-4995a983604b",
        "activity":"surfing","poll":"/recommendations/london"}
```

The request shape matters: `city` (not `city_id`), `forecast_date` **required**,
and `RequestIn` sets `extra="forbid"`
([services/api/main.py:351-361](../services/api/main.py#L351-L361),
[:415-419](../services/api/main.py#L415-L419), L4). The internal integration note
had drafted this call with `city_id` and no `forecast_date`; that spelling returns
422, which is why the body above is the one that was sent.

**The consumer refused it** (`docker compose ... logs consumer --since ...`):

```
2026-09-28 05:56:43,337 INFO consumer refused surfing for london: the activity
  needs a coast and the city has none on record
2026-09-28 05:56:43,341 INFO consumer stored recommendation.request
  9ac514ea-c825-430f-8cd3-4995a983604b
```

That first line is
[services/consumer/main.py:499](../services/consumer/main.py#L499) (L4).

**Nothing was stored, and the refusal is terminal rather than lost:**

| check | result |
|---|---|
| `SELECT count(*) FROM recommendations WHERE city_id='london' AND activity='surfing'` | **0** |
| London rows, before and after | 208 -> 208 |
| whole table, before and after | 1360 -> 1360 |
| `/recommendations/london` entries for `surfing` | `[]` |
| `requested` rows in that read | `[]` |
| `ingest_log` row for the envelope | present — `recommendation.request`, source `api`, `processed_at 2026-09-28 05:56:43.334888+00` |

The `ingest_log` row is the point, not a leak: the idempotency key commits in the
same transaction, so a redelivery of that message is a no-op rather than a second
chance to store the row. The refusal is a decision, recorded once.

---

## 3. The write path: R9 retraction, end to end, on a row created for it

`record_history` and `record_retractions` were **both empty** before this section
(L1 — `count(*)` = 0 on each), so this path had never run on this project. There
is no un-retract route
([services/api/main.py:628-637](../services/api/main.py#L628-L637), L4), so the
row was created to be withdrawn rather than chosen from the seed.

### 3.1 Creating the row

No API route creates a collected record, so the throwaway listing was accepted
into the **ingestor's own outbox** — the same front door every collected record
uses, and the mechanism `tests/integration/retraction_inject_event.py` uses for
the same reason. One `Envelope.create(config.RK_EVENT, ...)` with `valid_until`
derived from `config.event_valid_until(now)`, run as
`docker compose ... exec -T ingestor python - < <script>`:

```
event_id=live-evidence:throwaway-78ef8f6-2026-09-28
message_id=2f29a952-06c2-4b90-ad92-8d029c4b15b6
starts_at=2026-10-01T05:58:24.953179+00:00
outbox total=837 pending=1
```

It drained and was stored inside the poll interval:

| check | result |
|---|---|
| `events` row | `london`, category `music`, `revision 1`, `retracted_at IS NULL` |
| `ingest_log` | `2f29a952-...` / `event.record` / source `live-evidence` |
| `/coverage` events | 55 -> **56**; London 10 -> 11, `verified_events_current` 11 |
| `/events?city=london` | 11 rows, the throwaway among them |

An incidental confirmation of the three-outbox rule: `GET /outbox/2f29a952-...`
answered `"this message was never accepted here"`, because the envelope was
accepted into the *ingestor's* outbox and that route reads the *api's*.

### 3.2 Withdrawing it

```
curl -s -X POST \
  '127.0.0.1:8000/records/events/live-evidence:throwaway-78ef8f6-2026-09-28/retract' \
  -H 'content-type: application/json' \
  -d '{"reason":"live evidence lane 2026-09-28: throwaway row created to exercise R9 end to end","retracted_by":"live-evidence-lane"}'
-> 202 {"accepted":true,"message_id":"0087e918-1811-4de7-8a0d-1ce15825ac6d", ...}
```

`GET /outbox/0087e918-...` then reported the full lifecycle from the api outbox:
`accepted_at 05:58:54.725000+00`, `published_at 05:58:57.390741+00`,
`attempts 1`, `last_error null`, `stored true`, `stored_at 05:58:57.391739Z`.

**All three halves of the mechanism fired:**

| where | what it holds |
|---|---|
| the row | `revision 1 -> 2`; `retracted_at 2026-09-28 05:58:54.723891+00`; the reason, and `retracted_by live-evidence-lane` |
| `record_retractions` (010's ledger) | one row — `events` / the id / `retracted_at 05:58:54.723891+00` / the reason / `live-evidence-lane` / `recorded_at 05:58:57.391739+00` |
| `record_history` | `id 1`, entity `events`, `revision 2`, `changed_at 05:58:57.391739+00`, `old_row.retracted_at` **null** -> `new_row.retracted_at` set |

The two timestamps are the ledger's declared purpose working as designed:
`retracted_at` is when the decision was taken — the API's clock, not the
consumer's — and `recorded_at` is when this install was told, 2.67 s later. The
gap is the honest answer to "how long were we still serving it"
([db/migrations/010_retraction_ledger.sql:84-91](../db/migrations/010_retraction_ledger.sql#L84-L91),
L4).

**It left every read, and it is a mark, not a delete:**

| check | result |
|---|---|
| `/coverage` events | back to **55**; London back to 10, `verified_events_current` 10 |
| `/events?city=london` | 10 rows, throwaway absent |
| `/events?city=london&include_expired=true` | 10 rows, still absent — so it is the retraction hiding it, not the freshness filter |
| `events` table | **56 total, 55 published** — the row is still there, with its source, its as-of and its history |
| `GET /records/events/<id>/history` | revision 2, with the full `old_row`/`new_row` pair |
| the agent (`POST /agent/ask`, *"Are there any concerts in London on 2026-10-01?"*) | *"No concert is on record in London for 2026-10-01. That is the limit of the stored event feed, not evidence that none is scheduled."* — `rows_used.events 0` |

That last row is what "leaves every read" has to mean: the agent's own event
query, not only the REST route.

---

## 4. Nine UI pages in a real browser, zero off-origin requests

`ui-gate` proves this in CI on every PR. This is the same assertion against the
stack a reviewer would actually be shown, from a real Chromium on the host
reaching `127.0.0.1:8080` through `edge` — a localhost origin, which is the
origin a reviewer's own browser sends.

**First, the repo's own gate, unmodified.** `python -m tests.ui.browser_gate`
with `UI_BASE_URL=http://127.0.0.1:8080` and
`API_BASE_URL=http://127.0.0.1:8000`, exit 0:

```
PASS: page loads
PASS: main nav renders
PASS: as-of timestamp visible
forecast cards for Lisbon (local today 2026-09-28): ['Warmest - Thu 8 Oct',
  'Coldest - Fri 2 Oct', 'Next rain - tomorrow', 'Windiest - tomorrow']
  -- none precede local today
PASS: forecast cards are not stale (F6)
PASS: zero off-origin requests (145 same-origin requests seen)
PASS: all 5 browser assertions held against the real stack
```

**Then all nine pages.** That gate asserts the nav *lists* nine pages and clicks
only `Forecast`, so a second script reusing the same module's own helpers
(`TOP_LEVEL_PAGES`, `open_page`, `wait_for_app`) clicked every one and kept the
request log across all nine. It clicks nav labels only and presses no action
button inside a page.

| page | rendered | requests it added |
|---|---|---|
| Dashboard | yes | 0 |
| Forecast | yes | 0 |
| Suitability | yes | 6 |
| Trip planner | yes | 0 |
| Places map | yes | 1 |
| Ask the agent | yes | 1 |
| Update data | yes | 0 |
| Data coverage | yes | 10 |
| Monitoring | yes | 1 |

```
total requests recorded across all nine pages: 157
by netloc:  127.0.0.1:8080: 157
off-origin-by-HOST requests: 0
requests whose netloc differs from 127.0.0.1:8080: 0
PASS: nine pages rendered, zero off-origin requests
```

Every request, across every page, went to the one origin. `Places map` drew from
the bundled GeoJSON and asked no tile server; `Monitoring` reached for no
Grafana. The run was repeated after the stack restart in §5.5 and held again:
nine pages, **151** requests, zero off-origin.

**The one surface not covered by that claim**, and it must not be folded into it:
`GET /docs` on the API returns 200 with 1013 bytes referencing three off-origin
URLs — `cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js`, the
matching `.css`, and `fastapi.tiangolo.com/img/favicon.png` (L1 —
`curl -s 127.0.0.1:8000/docs`, then the `https://` hosts it names). Offline it
therefore renders blank. `/openapi.json` is 26,133 bytes and self-contained. The
honest sentence is "the UI issues no off-origin requests", not "the stack issues
none".

---

## 5. R10 settled: a plain `up` on an existing volume does re-run `migrate`

The open experiment was whether `docker compose up -d` on a pre-existing volume
applies a pending migration, or whether `service_completed_successfully` is
satisfied by a stale `migrate` exit and so gates nothing. An internal migrations
note (C3, not shipped with this submission — see the note on internal documents
above, L4) had withdrawn the "gates nothing" claim on source-reading grounds and
recorded that what replaced it was "L4 reasoning plus L1 inference, not L1
behaviour", naming `itineraries.as_of`'s `attnotnull` flag as the observable that
would settle it. It is now L1 behaviour.

The subject was the **preserved `aow` project**, whose volumes are a pre-merge
forensic artefact. Safeguards taken first:

- the existing `backups/20260927T123848Z` (project `aow`,
  `started_at 2026-09-27T12:38:48Z`) was **re-verified**: all five artefacts
  match their manifest `sha256` and byte counts, `backup_manifest.py verify`
  printed `manifest ok`, exit 0 (L1);
- a raw tar snapshot of all six core volumes was taken, with `sha256`s, while the
  project was down — `aow_pgdata` 12,755,582 B, `aow_ingestor_outbox` 350,920 B,
  `aow_api_outbox` 969 B, `aow_enricher_outbox` 58,818 B, `aow_rabbitdata`
  581,513 B, `aow_refresh_state` 97 B (L1);
- `aow-demo` was stopped first, with `stop` and never `down`, because both
  projects publish `127.0.0.1:8080` and `:8000`. Its six volumes stayed present
  throughout.

### 5.1 The control reading

`aow-postgres-1` publishes no host port, so the control was read with the demo
stack still up, by starting only that one service. Compose reported
`Starting`/`Started`, not `Recreating`.

| probe | `aow`, before | meaning |
|---|---|---|
| `itineraries.as_of` `attnotnull` | **t** (`is_nullable NO`) | 008 **not** applied |
| columns named `retracted_at` | **0** | 009 **not** applied |
| `record_retractions` in `information_schema.tables` | **0** | 010 **not** applied |
| events / places / facts | 58 / 691 / 81 | the three DB-only rows and the 71 orphan places |
| recommendations | 1360, **all `rule_version` 2** | before the sea ceiling (3) and before the wind-rule removal (4) |
| sea rows with `band='good'` | **183** | the pre-merge defect, intact |
| reasons containing "too flat" | **13** | the withdrawn reason, intact |
| `ingest_log` | 2977 | — |
| sample events | **0** | so `enforce_event_mode()` had nothing to purge |

### 5.2 The result

`docker compose -p aow -f D:\act-on-weather\compose.yml -f D:\act-on-weather\compose.observability.yml --env-file D:\act-on-weather\.env up -d`,
at 2026-09-28T06:13:00Z. The whole `up` took 9 s.

`aow-migrate-1` was **recreated**, not restarted: `Created` moved to
`2026-09-28T06:13:00.570707186Z`, where `docker ps -a` had been reporting
`Exited (0) 3 days ago`. Its command had changed — it now names ten migration
files, and the schema above shows the list it previously ran stopped at 007. It
ran in 220 ms and exited 0, and its log carries 009's `ALTER TABLE`s, three
`CREATE INDEX`es and seven `COMMENT`s, then 010's `CREATE TABLE`, the
`ALTER TABLE`, `INSERT 0 0` and two `GRANT`s.

| probe | after | verdict |
|---|---|---|
| `itineraries.as_of` `attnotnull` | **f** (`is_nullable YES`) | **008 applied** |
| columns named `retracted_at` | **4** | 009 applied (plus the ledger's own) |
| `record_retractions.recorded_at` `attnotnull` | **f** | 010 applied |

`attnotnull` flipping `t -> f` is exactly the observable that note predicted for
this experiment. **So a plain `up` on an existing volume does apply pending
migrations, and the stale exit does not gate them out.**

### 5.3 The isolating case the amendment could not run

The run above changed the migrate *command*, so on its own it proves "a changed
migration list re-runs" and not the command-only case the amendment named. A
second `up -d`, with nothing changed, settles that too:

| | before the 2nd `up` | after |
|---|---|---|
| `Created` | `06:13:00.570707186Z` | `06:13:00.570707186Z` — **same container** |
| `com.docker.compose.config-hash` | `44f2b77d0af5...32e1a6` | identical |
| `StartedAt` | `06:13:03.108118487Z` | **`06:14:29.881772111Z`** |
| `FinishedAt` | `06:13:03.328440122Z` | **`06:14:30.098619951Z`** |

Compose **restarted the existing container** and the list ran again: 140 log lines
from `001_init.sql` onward, object after object reporting
`NOTICE: relation ... already exists, skipping`, exit 0, and the schema and data
unchanged (`attnotnull f`, `record_retractions` 0 rows, recommendations 1360,
events 58). So `migrate` re-runs on a plain `up` in **both** cases — recreated
when its command changed, restarted when it had not — and the migrations are
idempotent, which is what makes that safe.

### 5.4 Nothing was re-ingested, re-scored or lost

Why this was safe to run at all, verified rather than assumed: the ingestor
derives every envelope's `message_id` from the record's natural key and its
`as_of` ([services/ingestor/main.py:84-99](../services/ingestor/main.py#L84-L99),
[:134-153](../services/ingestor/main.py#L134-L153), L4), and the committed
snapshot's `as_of` is identical in both projects
(`2026-09-23 18:16:44.438338+00`, L1). Replaying it therefore mints ids already
in the outbox and in `ingest_log`, so nothing reaches the handler — which matters
because the recommendation upsert carries **no `as_of` guard** and rewrites
`score`, `band`, `reasons` and `rule_version` unconditionally on conflict
([services/consumer/main.py:249-266](../services/consumer/main.py#L249-L266),
L4). A re-delivery would have destroyed the 183-row pre-merge defect.

Measured after the services had been up and draining for a minute:

| | control | after | |
|---|---|---|---|
| events / places / facts | 58 / 691 / 81 | 58 / 691 / 81 | unchanged |
| recommendations | 1360, all `rule_version` 2 | 1360, all `rule_version` 2 | unchanged |
| sea rows `band='good'` | 183 | **183** | unchanged |
| "too flat" reasons | 13 | **13** | unchanged |
| `ingest_log` | 2977 | 2977 | unchanged |
| `record_retractions` | — | 0 | 010's backfill wrote nothing (`INSERT 0 0`), correct with no marked row |
| `record_history` | not read in the control | 272 | all dated 2026-09-24, `max(changed_at) 2026-09-24 21:51:17.727191+00`, **none today** — so the `up` wrote no collected row |

### 5.5 Both projects afterwards

`aow` was returned to `stop`: all 14 containers `Exited`, all nine `aow_*` volumes
present. `aow-demo` was restarted with `start`; all ten containers came up
running, **seven of them reporting `healthy`**, and the API answered `/health`
within seconds. Seven is the maximum observable: `ingestor`, `consumer` and
`enricher` declare no `healthcheck` in [compose.yml](../compose.yml) (L4 — the
seven that do are `postgres`, `rabbitmq`, `llm`, `agent`, `api`, `ui` and `edge`),
so Docker reports them as `running` with no health state, and "all ten healthy"
is not a reading anyone can take. What stands in for those three is the work they
did: the enricher worded the new row (§6.4 item 4), the consumer stored and then
retracted rows (§§2–3), and the ingestor drained its outbox (§3.1).

**`aow-demo`'s data survived exactly.** A fingerprint of ten counts plus the
`rule_version` split, taken before and after the round trip, `diff`s clean: events
56 total / 55 published, places 620, facts 81, weather 80, recommendations 1361,
`record_history` 1, `record_retractions` 1, `ingest_log` 1324, itineraries 0, all
1361 rows at `rule_version` 4. The API reads held too: `/coverage` 55 events,
window unchanged, samples 0; Rome's sea max still 69/`fair`; London still carrying
no `surfing` row; the retracted throwaway still absent from `/events` even with
`include_expired=true`.

**The one change to `aow` that is not reversible, stated plainly:** its schema is
now at migration 010. Its *data* is untouched — that is the table in §5.4 — but
`itineraries.as_of` is nullable where it was `NOT NULL`, the nine retraction
columns and three partial indexes now exist, and so does `record_retractions`.
There is no un-migrate. The verified backup and the raw volume snapshot both
predate the change.

---

## 6. The demo, rehearsed against this stack

"The demo script" throughout this section and §7 means the presenter's internal
walkthrough note, which is not shipped with this submission (the note on internal
documents in the header). Every question it calls for is quoted here with the
answer the stack actually returned, so this section is readable without it.

Latency is `curl` wall clock around `POST /agent/ask`. **The script's
per-question estimates are far too pessimistic** — they were taken pre-merge, and
every deterministic answer here returned in 0.1 s.

| # | question | measured | `llm_called` | matched the script |
|---|---|---|---|---|
| 1 | a run in Rome, tomorrow | 0.1 s | false | yes — `Running is fair (55/100)` |
| 2 | a boat ride in Rome, tomorrow | 0.1 s | false | yes — 69/`fair` plus the coast caveat, verbatim |
| 3 | swimming in London, tomorrow | 0.1 s | false | yes — no score, "London has no coast on record" |
| 4 | where can I surf in Tel Aviv | 0.1 s | false | yes — refused; the five beaches not offered |
| 5 | where can I go to the beach in Tel Aviv | 0.1 s | false | yes — five venues with categories |
| 6 | weather in Rome on 2027-07-04 | 0.1 s | false | yes — template refusal, verbatim |
| 7 | what can I do in Buenos Aires | 0.1 s | false | yes — five cities, alphabetical |
| 8 | sports events in Rome this week | 3.5 s | **true** | behaviour yes, **shape no** — §6.4 |
| 9 | paragliding in Rome, tomorrow | 0.1 s | **false** | behaviour yes, **wording and labelling no** — §6.4 |
| 10 | weather in Rome between 2026-10-14 and 2026-10-05 | 26.3 s | true | yes, and the grounding note fired |
| 11 | kite flying in Rome on 2026-09-29 | 0.1 s | false | yes, after the pre-seed — §6.4 |
| 12 | sports events in London in the next three weeks | 7.7 s | true | yes — Dubois vs Wardley 2 comes back |
| 13 | trip planner, Rome 09-28 to 10-02, museums + outdoors | 0.07 s | false | yes — five day cards, score pill, `why`, sourced venues |
| bench | concerts in Tel Aviv this weekend | 3.9 s | true | yes — the honest gap sentence |

Question 10 is worth quoting, because it exercises three things at once: a
backwards range sorted rather than refused, the code-written gap for the
uncovered days, and the grounding guard catching the model.

> Rome, 2026-10-05 to 2026-10-14: ... the four covered days ... then
> `- No weather is stored for 2026-10-09 to 2026-10-14 in Rome. The stored
> forecast ends on 2026-10-08, so those days are left out rather than guessed.`
>
> note: *"The model's wording made a claim the stored rows do not support (states
> the date 2026-10-14, which no retrieved row carries), so this answer is
> rendered directly from those rows."*

Question 13's day cards carry `activity_score` and `activity_band` (**not**
`score` and `band`, which are absent), and the itinerary API stand-in behaved
exactly as documented: `POST /agent/itinerary {"city":"rome",
"start_date":"2026-10-06","end_date":"2026-10-12"}` returned 200 with days
`10-06` to `10-08` only and `requested_days_outside_coverage
['2026-10-09','2026-10-10','2026-10-11','2026-10-12']`.

### 6.1 The #67 repairs, live

| repair | question | result | `llm_called` |
|---|---|---|---|
| a typed activity the catalogue does not carry | paragliding / Rome | `- paragliding: no suitability score on record.` No number anywhere near the word | false |
| a multi-word name must not resolve to a catalogue keyword inside it | kite surfing / Lisbon **and** / Rome | `- kite surfing: no suitability score on record.` Neither borrowed the capped 69 | false |
| short nouns | `ski` / Reykjavik | `- ski: no suitability score on record.` **Caught**, not discarded | false |
| a mixed question keeps both halves | *"What events are on tomorrow in Rome, and is it a good day for a bbq?"* | **both** — the weather line, the Battisti event row, **and** the bbq gap sentence | true |

### 6.2 Two things a presenter would otherwise be caught by

**A: naming an event by title alongside a *catalogue* activity silently drops the
event half.** Asked live:

> *"Is Battisti in Classica on tomorrow in Rome, and is it a good day for
> sightseeing?"*
>
> -> `Stored suitability for the activities you asked about in Rome:`
> `- 2026-09-29: Sightseeing on foot is good (83/100).`

Nothing about the event, although `rows_used.events` still reports **1** — the row
was retrieved and then dropped. This is documented behaviour, not a regression
([services/agent/main.py:191-195](../services/agent/main.py#L191-L195),
[README.md:783-796](../README.md#L783-L796), L4), and it is a deliberate
boundary: a named catalogue activity is rendered from its own rows rather than
through a 1.7B model. But it is demo-fragile, because the event title in the
question makes it look as though the system missed something. **Ask the two
questions separately.** The same question about an activity the catalogue does
*not* score keeps both halves — §6.1, last row — so the difference can be
demonstrated on purpose if a reviewer asks.

**B: `data/retractions.jsonl` is 0 bytes.** If the demo touches withdrawn
listings, the honest sentence is that this stack **never ingested** them: a
fresh-volume project produced no message at all for a row that is absent from the
committed seed. It is not that they were retracted. The three rows in question
live in the preserved `aow` project and nowhere else — `events` there is 58
against the seed's 55 (§5.1) — and none of them carries `retracted_at` even now
that 009 is applied to that database. R9's *mechanism* is real, gated, and
demonstrated end to end in §3; R9's *data* withdraws nothing.

### 6.3 A data defect found while rehearsing, in the committed seed

The event a presenter is most likely to name by title carries a wrong character.
The stored title is

```
Roberto Fabbri presenta: Battisti in...Classica!
```

where those three dots are a single **U+2026 HORIZONTAL ELLIPSIS** (`ascii()`
8230) standing where a space belongs — character 37 of 46 (L1 — a per-character
`ascii()` scan in `psql`). It is not a console artefact and not a demo-stack
artefact: `data/events.seed.jsonl` holds
`Roberto Fabbri presenta: Battisti in\u2026Classica!` at index 36 (L1 —
`json.loads` over the committed file), so it reproduces on every fresh install,
including a reviewer's. It is the only such row: a scan of all stored titles for
`[\u0080-\u009f\u2026]` returns exactly one match, and the accented titles are
clean — the Sarah Chaksad row stores `é` and `è` correctly, code points 233 and
232. Fixing it is a seed change and is not made here.

### 6.4 Where the demo script's expected output does not match the stack

Every item here is the script being stale, not the system being wrong. Each was
read off this stack.

1. **Question 9's answer is shorter than the script quotes, and is not a model
   call.** The script expects *"paragliding: not on record -- no suitability
   score is stored for it in Rome. It is not one of the activities I score by
   default. You can have it scored against general outdoor comfort from the "Ask
   about a different activity" form, or with POST /recommendations."*, marks the
   question **(LLM)** and budgets 60 s. Live it returns two lines —
   `I hold no suitability score for what you asked about in Rome:` /
   `- paragliding: no suitability score on record.` — with `llm_called false` in
   0.1 s. The longer sentence is real, but it belongs to the *mixed* route: it
   came back on the bbq question in §6.1 word for word, with `bbq` in place of
   `paragliding`, because that question also asks about events. A question that
   asks about nothing else takes the short deterministic route instead.
2. **Question 8 returns two paragraphs, not one sentence.** Live: a model
   sentence, `No sports events are on record in Rome for 2026-09-28 to
   2026-10-04.`, then a `Not on record:` block carrying the code-written gap
   sentence the script quotes. `llm_called true`. The guard working, but the
   presenter should expect the shape.
3. **The pre-seed's poll command never matches.** The script's §1.3 filters
   `x['activity']=='kite-flying'` with a hyphen; the API slugs "kite flying" to
   **`kite_flying`** with an underscore (L1 — the 202 response body names
   `"activity":"kite_flying"`, and the stored row's `activity` is `kite_flying`).
   The script's own one-liner therefore prints `[]` forever and a presenter would
   conclude a successful pre-seed had failed.
4. **`/enrichment` reads 1 `pending` straight after the pre-seed, by design.**
   The §1.2 gate says pending must be 0, and §1.3 files a row that enters the
   queue as `pending`. Run the gate before the pre-seed, or wait: the enricher
   worded it within the session and the count returned to 0 `pending` /
   481 `ready`.
5. **The stored sentence for that row reads awkwardly, and the Suitability page
   is where a reviewer meets it.** `Fly kites in Rome on 2026-09-29 is a good
   day, with 11.6h of sunshine and light wind. The high temperature of 29C is
   above the typical range for this activity, making it comfortable to fly
   kites.` That is 1.7B output stored verbatim (`status ready`, model
   `Qwen3-1.7B-Q4_K_M`), and the second sentence contradicts itself. The
   deterministic answer beside it is correct — `kite flying is good (83/100)`
   plus the "scored against general outdoor comfort" caveat — so the honest
   framing is that the rule engine is the verdict and the model only phrases it.

---

## 7. The demo script's live hazards, checked

| | hazard | live result |
|---|---|---|
| H1 | the UI prints a resolved date range character by character | **precondition confirmed** — `dates` is a string in the agent response (`"2026-09-28 to 2026-10-04"`), which is what `', '.join(map(str, ...))` would split. The rendering itself stays L4 |
| H3 | model-path non-determinism, the grounding note | **fired live**, on question 10, with its reason stated in the note |
| H5 | the bare word "on" is an event-schedule word and *clears* the resolved activities | **stale.** *"Is it a good day for a market on Saturday in Rome?"* returned **both** — `An open-air farmers' market is good (99/100)` **and** the "no market event is on record" gap. The activity is not cleared |
| H6 | sub-4-character nouns are discarded, so "ski in Reykjavik" borrows a score — do not ask it | **stale.** `MIN_ACTIVITY_SLUG_CHARS = 2` ([services/common/schemas.py:38](../services/common/schemas.py#L38)), shared by the write and read paths and enforced at [router.py:667](../services/agent/router.py#L667) and [:711](../services/agent/router.py#L711). `ski` is caught. Safe to ask, and a good beat |
| H7 | `kite surfing` resolves to catalogue `surfing` on the read path | **stale for the read path** — the longer phrase shadows the keyword, in Rome and in Lisbon. Still type `kite flying`, because the write path's slug is §6.4 item 3 |
| H8 | the planner's `why` is unchecked model prose; demo Rome and leave "concerts" unticked | **real, and that mitigation does not work.** Rome, museums + outdoors, no concerts interest, produced *"A good day to enjoy the open-air music festival."* But the provenance is narrower than H8 states: it is the enricher's stored sentence for the **catalogue activity** `music_festival`, byte-identical to `recommendations.text` for `rome/2026-09-30/music_festival` (`status ready`, model `Qwen3-1.7B-Q4_K_M`, `activity_score 100`). No event is invented. The hazard is the definite article — a reviewer hears "*the* open-air music festival" and asks which one |
| H11 | `/docs` is blank offline | **confirmed** — §4 |
| H12 | never count rows from `/events` or `/places` on screen | **confirmed** — both default to exactly **50** rows |

One more, not in the script. The Dubois vs Wardley 2 row is **not** served a day
early. `starts_at` is `2026-10-16T23:00:00Z`, but every read converts to the
city's timezone — `(e.starts_at AT TIME ZONE c.timezone)::date`
([services/common/queries.py:63](../services/common/queries.py#L63), L4) — so the
API serves `starts_on: 2026-10-17` and `ends_on: 2026-10-17`, the date the venue
publishes, and question 12's answer named 2026-10-17 correctly. What remains true
is that the stored *instant* is a local-midnight placeholder rather than the
venue's 18:30 doors, so the caution is "do not read a stored event **time**
aloud", not "the date renders a day early". That row's `valid_until` is
`2026-10-15T18:00:00Z`, two days before the fight — confirmed live.

---

## 8. What this does not verify

- **Nothing here is evidence for F10.** Both projects ran on one Docker engine on
  one physical development PC. A second Compose project is not an air gap, and
  F10 stays open.
- **No connected refresh was run.** `make refresh` and `make snapshot` open real
  egress and would move the coverage window; the window above is the committed
  snapshot's.
- **Demo mode was never switched on.** Every reading is verified-only, samples 0.
  The `(sample)` labels and the demo-mode banner are therefore not exercised, and
  the 100-row event count is not a figure this file measured.
- **No `demos` drill was run against either project.** `no-data-loss`,
  `reenrich`, `update` and `backup-restore` stop live containers or delete rows.
- **`scripts/retraction-drill.sh` was not run.** It refuses `aow` and `aow-demo`
  by name and needs its own disposable project. §3 exercises the
  record-then-withdrawal order by hand; the withdrawal-then-record order remains
  covered only by that drill in CI (L2).
- **No backup was restored.** The backup in §5 was verified and the volume
  snapshot taken; neither was replayed, so this file is not restore evidence.
- **The command-only `migrate` finding is this engine's.** §5.3 is Compose v5.5.1
  on one Docker engine, observed twice — not a guarantee across versions.
- **`aow`'s post-migration state was not re-read after its restart.** It was read,
  then stopped.
- **The nine-page walk clicked nav labels only.** No form was submitted and no
  button pressed inside a page, so the zero-off-origin claim covers the nine
  pages as rendered, not every interaction they offer.
- **Latency is single-run.** Each figure in §6 is one measurement on an otherwise
  idle stack, not a distribution, and question 10's 26.3 s is the only model-path
  reading above 10 s.

## 9. Writes this file performed, and where

Named here rather than only in the lane's own internal notes, so a reader of the
submission need not reconstruct them.

**On `aow-demo`:** one refused `POST /recommendations` (London / `surfing` — an
`ingest_log` row, no business row); one throwaway event accepted into the
ingestor's outbox and stored, then retracted (the row marked, one `record_history`
row, one `record_retractions` row, two `ingest_log` rows, and one durable outbox
row that lives for the life of the volume); one `POST /recommendations` for
`kite flying` in Rome on 2026-09-29, which the demo script's §1.3 pre-seed calls
for (one `requested` row, since worded). One `stop` and one `start`. No `down`, no
`-v`, no volume removed, no image rebuilt, no wipe.

**On `aow`:** `up -d` twice and `stop` once, which recreated `aow-migrate-1` and
advanced the schema from 007 to 010. No business row changed — §5.4.
