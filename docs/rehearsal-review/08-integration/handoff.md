# Integration lane — handoff and live-phase runbook

| | |
|---|---|
| Lane | `docs/rehearsal-review/08-integration/` |
| Date | 2026-09-27 |
| Branch | `integrate/rehearsal-repairs-2026-09-27`, worktree `D:\aow-integrate` |
| Base | `a12ae378df45e288c85ab0b710cb0e1e6d55861f` (= `origin/main` on entry) |
| Integrated head | `a08567a9ea46ee43288deff1d2c37663c125c55a` |
| Pull request | [#66](https://github.com/AvihaiShai/act-on-weather/pull/66) |
| Live work done | **None.** No container was created, started, stopped or rebuilt. |

This file is the written half of the serialized batch. The live half was not
run, because ownership of the `aow` stack was never released to this session.
Everything below the line marked **NOT YET RUN** is instruction, not evidence.

---

## 1. What was integrated, and in what order

Merged in the order the batch prescribed. **No merge conflicts at any step.**

| | Lane | Commit | Merge |
|---|---|---|---|
| 1 | R9 retraction | `0b13463` | settled from the dirty main checkout |
| 2 | Data integrity | `bf066da` | `f95a6f6` |
| 3 | Agent (R11/R2/R3) | `197620e`, `30367b8` | `2580e56` |
| 4 | Content / dates | `81ade92` | `7cac457` |
| 5 | Operations | `35ccddf` | `a08567a` |

### R9 was settled, not rewritten

The R9 work was uncommitted in `D:\act-on-weather` when this session started —
14 modified tracked files and 4 untracked ones, last written 02:55, about ten
and a half hours before. It was transferred by patch into this worktree and
committed **byte-for-byte unchanged**. The main checkout was never staged,
reset, cleaned or committed from, and is still dirty exactly as it was.

`fix/r9-record-retraction` is a branch pointer at `0b13463` so the R9 commit can
be reviewed or dropped on its own.

### The anticipated consumer conflict did not exist

The batch expected `services/consumer/main.py` to conflict between R9 and the
data lane. It does not: R9 works in `enforce_event_mode`, `apply_retraction` and
`wipe_user_data`; the data lane works in `store_recommendation_request`. Git
merged them with no marker. **Do not record a resolved conflict here — there
was nothing to resolve.**

### The two lanes meet where they were supposed to

The agent lane's commit message lists the write-path coast precondition at
`services/consumer/main.py:387-391` as owed to the data lane, and the data lane
implements exactly that. Each side has its own tests —
`test_requested_activity.py` asserts no row is stored for a coast-only activity
in an inland city, `test_typed_activities.py` asserts an unscored activity is
reported as a gap rather than given another activity's score. **No single test
spans both halves.** That is a real gap in coverage of the joined behaviour,
and it is cheap to close later.

---

## 2. Gates on the integrated SHA

### Local, on `a08567a`

| Gate | Command | Result |
|---|---|---|
| ruff check | container, `--network none` | pass |
| ruff format --check | container, `--network none` | pass |
| Unit suite | container, `--network none` | **1842 passed, 2 skipped, 0 failed** |
| Snapshot manifest | `python scripts/snapshot_manifest.py --check` | pass |
| Shell syntax | `bash -n scripts/*.sh demos/*.sh` | pass |
| Compose renders | all nine overlay combinations | pass |
| New guard step | "Model mirror defaults agree", body extracted verbatim | exit 0, 1 literal, agrees |

Two notes on how those were obtained, because both would otherwise read as
stronger than they are:

- **The baseline had two failures.** At `a12ae37` the same container run is
  1680 passed, **2 failed** —
  `test_grounding.py::test_unambiguous_yearless_or_weekday_event_claim_is_checked`
  for both parameters. Those are a calendar-dependent flake, not a regression,
  and the agent lane's `30367b8` is what closes them. Anyone re-measuring the
  baseline after 2026-09-30 will see the same two failures and should not chase
  them.
- **The snapshot gate ran on the host, not in the pinned container.**
  `scripts/local-gates.sh` runs it in `python:3.12-slim@sha256:2f17fc04…` with
  `--pull never`, and that image is **not in this engine's store** — the gate
  fails exit 125 with "No such image" before it checks anything. The script is
  stdlib-only by design (its own docstring says so, because CI runs it on a
  bare runner), so the host run has identical inputs. To restore the container
  path, `docker pull python:3.12-slim@sha256:2f17fc04…` once.

### CI, on the exact SHA

Run [`36315062604`](https://github.com/AvihaiShai/act-on-weather/actions/runs/36315062604),
`event=pull_request`, `headSha=a08567a9ea46ee43288deff1d2c37663c125c55a`,
conclusion **success**. Read back from
`GET /commits/a08567a9…/check-runs`:

| Context | Conclusion | Time |
|---|---|---|
| `lint` | success | 9s |
| `unit` | success | 2m13s |
| `guard` | success | 17s |
| `build-and-scan` | success | 4m14s |
| `ui-gate` | success | 1m44s |
| `publish-images` | skipped | — |
| `model-grounding` | skipped | — |
| `restore-drill` | skipped | — |

The five required contexts all pass. The three skips are correct and expected:
each is `if:`-gated away from an unlabelled pull request, and none can ever be
a required check because a skipped check is not a success under branch
protection.

**Still owed on this SHA.** This changes runtime code, so the repository's own
tripwire applies: `model-grounding` and `restore-drill` are owed, and the
`bbee42c` bundle stops being current the moment this merges. Either add the
`release-candidate` label to #66 or run a `workflow_dispatch` after merge.

---

## 3. Two questions settled read-only, that were open before

- **R0's backup prerequisite is met.** `docker image inspect aow/services:dev`
  resolves, to `sha256:3245422719…`. The ops lane left this as the one thing it
  could not settle from source, and it is the tag `scripts/backup-state.sh:192`
  will inspect after resolving `{{.Config.Image}}` off a running producer. So
  `backup-state.sh` will run on the live stack **without** a hand-set
  `AOW_SERVICES_IMAGE`. (`aow/ui:dev` also resolves, `sha256:2383027455…`.)
- **Migration 009 is not applied to the live database.** `information_schema`
  reports 0 columns named `retracted_at` on `events`. The running stack predates
  R9, as expected. Nothing in this session changed that.

---

## 4. NOT YET RUN — the live phase

**Nothing below has been executed.** The `aow` stack was up throughout this
session (13 containers, observability overlay, ~2h at the time of writing) and
was left untouched. `.private/STACK-LOCK` holds `P`.

The plan below is **not the report's R0**. The report recommends `make clean`.
The operator's decision for this batch is explicitly **not to wipe or clean
`aow` at all**: instead, stand up a *separate* Compose project on fresh volumes
from the integrated SHA, and preserve the old project and its volumes intact.
That is a strictly safer route to the same demo and it forfeits none of the
forensic evidence `make clean` would have destroyed.

### 4.0 Precondition

Do not start. `P` must explicitly release the `aow` stack first, and the
exact-SHA checks in §2 must be green (they are). One owner at a time.

### 4.1 Pre-start verification — already done, re-check if anything moved

Rendered with
`docker compose -p aow-demo -f compose.yml --env-file D:\act-on-weather\.env config`:

| Item | Verified value |
|---|---|
| Project name | `aow-demo` |
| Volumes | all six renamed `aow-demo_pgdata`, `aow-demo_rabbitdata`, `aow-demo_ingestor_outbox`, `aow-demo_api_outbox`, `aow-demo_enricher_outbox`, `aow-demo_refresh_state` |
| Ports | `host_ip: 127.0.0.1`, 8080 and 8000 — **the same two as `aow`** |
| Model mount | `D:\aow-integrate\models` → `/models`, read-only |
| Environment file | `D:\act-on-weather\.env`, renders with no `${VAR:?}` error |
| Migration list | includes `009_record_retraction.sql` |
| Image tags | `aow/services:dev`, `aow/ui:dev` |

Two of those rows are hazards rather than confirmations:

- **The ports collide.** Both projects publish `127.0.0.1:8080` and
  `127.0.0.1:8000`. Only one stack can run. `aow` must be stopped before
  `aow-demo` starts.
- **The image tags are shared.** `compose.yml` hardcodes `aow/services:dev` and
  `aow/ui:dev`. Building the integrated SHA overwrites the tags the old stack's
  `Config.Image` names, which is what `backup-state.sh` resolves through. So the
  old images must be given their own tags *before* any rebuild, and the backup
  must be taken *before* that too.

Also note the worktree was prepared: `D:\aow-integrate\models` holds a
**hardlink** to the main checkout's `Qwen3-1.7B-Q4_K_M.gguf`, 1,282,439,264
bytes, one inode, no extra disk. There is deliberately **no `.env` in this
worktree** — it is passed by `--env-file` from the main checkout, so there is
only ever one copy of the secrets.

### 4.2 Sequence

```bash
# 1. Backup and forensics, while `aow` is still UP.
cd /d/act-on-weather
bash scripts/backup-state.sh                 # writes backups/, prints per-artefact bytes+sha256
```

`backup-state.sh` is self-verifying: `:396-409` re-hashes the files as they sit
on disk and runs `scripts/backup_manifest.py verify` against the manifest, on the
stated ground that "a backup that cannot verify itself the moment it is taken
will not verify six weeks later either". There is **no** separate
`scripts/verify-backup.sh` — do not go looking for one. A verified backup here
means `backup-state.sh` exited 0 with that step passing. It also refuses to run
unless `postgres` and `rabbitmq` are up (`:156-160`), which is why it comes
before the `down` and not after, and it contains no `docker pull`, so it needs
`aow/services:dev` present locally — confirmed in §3.

`backups/` does not exist yet; no backup has been taken.

The §5.1 api-outbox readings are **already captured** at
`docs/dress-rehearsal-2026-09-27-evidence/lead/2026-09-27-api-outbox-history-checks.txt`
and need not be retaken. They are also no longer at risk, because
`aow_api_outbox` is not being removed.

```bash
# 2. Preserve the old images under their own tags, so the rebuild cannot take them.
docker tag aow/services:dev aow/services:pre-integration-a12ae37
docker tag aow/ui:dev       aow/ui:pre-integration-a12ae37

# 3. Stop the old project. NO -v. Volumes and the DLQ survive.
cd /d/act-on-weather
docker compose -p aow down                   # add the observability overlay flags if `ps` shows them
docker compose -p aow ps                     # expect empty
docker volume ls | grep '^local *aow_'       # expect all six still present

# 4. Build and start the fresh project, verified-only (NO compose.demo.yml).
cd /d/aow-integrate
docker compose -p aow-demo -f compose.yml --env-file /d/act-on-weather/.env build
docker compose -p aow-demo -f compose.yml --env-file /d/act-on-weather/.env up -d
# llm stays unhealthy ~3 min while the model loads.
```

### 4.3 Baseline verification, verified-only

Record every number with the command that produced it. Wait for the wording
queue to drain first — `/enrichment` reads 480 `pending` immediately after a
fresh ingest, which is expected, not a failure. **Re-enrichment of 480 rows on
CPU is unmeasured; allow hours, not minutes, and record the wall clock.**

```bash
# Counts. /coverage entities[].rows is the whole table; do NOT use `curl /events | wc -l`
# (single-line JSON, and the route defaults to limit=50).
curl -s 127.0.0.1:8000/coverage | python3 -c "import json,sys; print({e['entity']: e['rows'] for e in json.load(sys.stdin)['entities']})"
#   expect events 55, places 620, facts 81

# Sea cap and rule_version.
curl -s 127.0.0.1:8000/recommendations/rome | python3 -c "import json,sys; r=[x for x in json.load(sys.stdin) if x['activity'] in ('surfing','fishing','boat_ride','swimming')]; print(max(x['score'] for x in r), {x['band'] for x in r}, {x['rule_version'] for x in r})"
#   expect max <= 69, no 'good', rule_version {4}

# The withdrawn falsified reason must be gone.
curl -s 127.0.0.1:8000/recommendations/rome | python3 -c "import json,sys; print(sum(1 for x in json.load(sys.stdin) if 'too flat' in str(x['reasons'])))"
#   expect 0

# Queue drained.
curl -s 127.0.0.1:8000/enrichment      # expect pending 0, failed 0
curl -s 127.0.0.1:8000/coverage        # window must still cover tomorrow
```

Then the repairs this branch actually adds, which the report's R0 checks predate:

```bash
# R11 — the fabrication. Must NOT return another activity's score.
curl -s -X POST 127.0.0.1:8000/agent/ask -H 'content-type: application/json' \
  -d '{"question":"Is tomorrow a good day for paragliding in Rome?"}'
#   expect a gap answer: no suitability score on record for paragliding.
#   A "good (100/100)" here is the original defect, unfixed.

# Data lane — a coast activity for the inland city must store NO row.
curl -s -X POST 127.0.0.1:8000/recommendations -H 'content-type: application/json' \
  -d '{"city_id":"london","activity":"surfing"}'
curl -s '127.0.0.1:8000/recommendations/london' | python3 -c "import json,sys; print([x['score'] for x in json.load(sys.stdin) if x['activity']=='surfing'])"
#   expect []  — and the consumer log to carry the refusal line

# R9 — retraction end to end, on a throwaway row of your choosing.
curl -s -X POST '127.0.0.1:8000/records/facts/<id>/retract' -H 'content-type: application/json' \
  -d '{"reason":"integration check","retracted_by":"operator"}'
#   then confirm the row leaves /coverage's count and every read, and that
#   record_history shows the revision.
```

### 4.4 Demo mode, only if the demo needs uncovered dates

Verified-only is the default and the state the baseline is recorded against.
Switch **only** if the planned demo needs days the 55 verified events do not
cover, and then:

- restart with `-f compose.yml -f compose.demo.yml`;
- record the resulting count — **100 events, not 55** — as a *separate*,
  labelled reading;
- never present a sample row as verified. Every one is `is_sample` and titled
  `Sample: …`, and the UI carries a demo-mode banner;
- go back to verified-only afterwards (`-f compose.yml` alone restarts the
  consumer, whose `enforce_event_mode()` deletes the samples) and **verify the
  sample rows are gone** — `/coverage` back to 55.

One R9 interaction to know about here: `enforce_event_mode()` now deletes
`is_sample AND retracted_at IS NULL`, so a *retracted* sample row is kept rather
than deleted and re-minted. If you retract a sample row during the demo, expect
it to persist across the mode switch.

### 4.5 Refresh — the fresh project only, and only after baseline passes

Do **not** refresh `aow`. Once `aow-demo` has passed §4.3:

```bash
cd /d/aow-integrate
docker compose -p aow-demo ... run --rm refresh     # opens a real, self-closing egress window
```

- Record the new as-of time.
- **Verify the egress window closed** — `make refresh-check` is the drill for
  "does this always put the ingestor back?"; confirm the ingestor is off the
  egress network afterwards.
- If the provider is unreachable, **keep the working demo** and disclose its
  as-of date. A failed refresh is not a reason to leave the demo broken.

---

## 5. Report corrections owed — do not edit the report from here

`docs/HIRING_MANAGER_DRESS_REHEARSAL_REPORT.md` is untracked in the main
checkout and other sessions may be reading it. Following the ops lane's
precedent, corrections go here rather than into the report.

1. **R0 was not executed, and the chosen route is not R0.** The report's R0
   recommends `make clean`. The operator decision for this batch is a separate
   `aow-demo` project on fresh volumes with `aow`'s volumes preserved. R0's
   "High" risk row and its forensic-loss paragraph do not apply to that route:
   nothing is destroyed. Record the route, not just the outcome.
2. **F-8 should be withdrawn.** The content lane checked all five spanning rows
   against each venue's own JSON-LD and printed date ranges and found them
   genuine multi-day runs; acting on F-8 would have deleted five real concert
   nights. Replacement wording is in
   `docs/rehearsal-review/07-dates-and-spans/handoff.md` §1. **That verification
   is the content lane's claim, taken against live venue pages — it is not
   reproduced here.**
3. **R9's status.** (a) disclosure and (c) mechanism are now implemented and
   committed at `0b13463`. (b), the data, is **not** addressed:
   `data/retractions.jsonl` ships **empty**, so the 2 venue-cancelled Gulbenkian
   rows and the Suzanne Dellal orphan are not withdrawn by it. The fresh-volume
   route removes them from the demo by not ingesting them, which is a different
   thing from retracting them and should be recorded as such.
4. **R11/R2/R3, R1, R6, R7** are implemented at `a08567a` with the tests and CI
   in §2. All of that is **source and CI evidence only.** No live reading exists
   for any of them. Do not promote any of it to a demonstrated pass on the
   strength of this document.
5. **R0's backup prerequisite** and **migration 009's absence from the live DB**
   are settled — §3, both read-only.
6. **F10 stays OPEN.** Nothing here touches it. A separate Compose project on
   this machine is not an air gap, and neither is a second engine.

---

## 6. Things the next session must not do

- **Do not `make clean`, `down -v`, or `POST /user-data/wipe` on `aow`.** The
  operator decision is to preserve that project and its volumes. `make clean` is
  the most destructive target in the repo.
- **Do not run both stacks at once.** They publish the same two loopback ports.
- **Do not rebuild `aow/services:dev` or `aow/ui:dev` before retagging the old
  images and taking the backup.** §4.1 says why.
- **Do not touch `D:\act-on-weather`'s working tree.** It is still dirty with
  the R9 files this branch already carries, plus the report and the evidence
  packs. It was read-only throughout this session and should stay that way until
  someone decides what to do with the untracked report and evidence directories.
- **Do not claim a live fix from this document.** Everything in §§1–3 is source,
  local-container and CI evidence. §4 onwards has not been run.
- **Do not treat the fresh `aow-demo` install as F10 proof.** Same machine, same
  engine.
