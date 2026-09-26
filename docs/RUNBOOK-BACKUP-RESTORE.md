# Runbook: operational backup and restore

For the operator of a running `act-on-weather` stack. It covers backing up the
live state of the system and putting it back after that state is lost or
corrupted.

**This is not the release path.** `scripts/package-offline.sh`,
`scripts/install-offline.sh` and `scripts/restore-offline.sh` package, install
and roll back a *version* of the deployment, including the pre-upgrade dump the
installer takes on its own behalf; the two commands in this runbook back up and
restore the *data* of a stack that is already installed, on any day, with no
upgrade involved.

```
bash scripts/backup-state.sh                       # back up the `aow` stack
bash scripts/restore-state.sh backups/<id>         # restore into `aow-restore`
bash demos/06_backup_restore.sh                    # the drill that proves both
```

Everything runs in containers. There is no host `psql`, no host `sqlite3` and
no host Python, so these commands work identically on Linux, on macOS and on
Windows under Git Bash.

---

## 1. What is backed up

A backup is a directory — `./backups/<UTC timestamp>/` unless you name another
one with `--dir` — holding five artefacts and a manifest.

| Artefact | What it is | How it is taken |
|---|---|---|
| `postgres.dump` | The whole application database — all nine tables `db/migrations/001_init.sql` creates: `cities`, `weather_daily`, `recommendations`, `places`, `facts`, `events`, `itineraries`, `record_history` and the `ingest_log` idempotency ledger. `cities` is the one of the nine that is not irreplaceable: `seed_cities()` in `services/consumer/main.py` rewrites it from `data/cities.yml` at every consumer start. | `pg_dump -Fc` inside the `postgres` container. |
| `outbox-ingestor.sqlite3` | The ingestor's producer outbox. | Online copy via `sqlite3.Connection.backup()`, inside the owning container. |
| `outbox-api.sqlite3` | The API's producer outbox — user edits, itineraries, activity requests. | Same. |
| `outbox-enricher.sqlite3` | The enricher's producer outbox — model-worded recommendations on their way back. | Same. |
| `rabbitmq-definitions.json` | Exchanges, queues, bindings, policies, users, permissions. | `rabbitmqctl export_definitions`. |
| `manifest.json` | Byte size and SHA-256 of every artefact, the migration files in the tree, the image digests in use, the UTC start and finish times, and the queue depths at backup time. | Written by `backup-state.sh` and validated by it before it exits. |

Two choices in that table are worth understanding, because they are the two
places a naive backup of this system would be silently wrong.

**Postgres is dumped, not copied.** A filesystem copy of a running `pgdata`
volume is not a backup: the heap, the write-ahead log and the buffers Postgres
has not flushed are three different points in time, and the result may restore,
may refuse to start, or may start and be quietly inconsistent. `pg_dump -Fc`
goes through Postgres itself and produces one transactionally consistent
snapshot.

**The outboxes are copied through SQLite, not with `cp`.** All three run in WAL
mode (`services/common/outbox.py`), so a committed row lives in
`outbox.sqlite3-wal` until a checkpoint folds it into the main file. Copying
`outbox.sqlite3` on its own drops every commit since the last checkpoint —
which, for an outbox, is exactly the recently accepted records the whole
guarantee is about. Copying all three files instead is no better, because there
is no instant at which an outside reader can take them together.
`scripts/sqlite_backup.py` uses SQLite's own backup API: one read transaction,
one self-contained file, WAL already folded in, source opened read-only and
left writable for its owner.

## 2. What is **not** backed up

**Message bodies sitting in `aow.ingest` or `aow.dlq` are not backed up.** That
is deliberate and it is the one claim in this runbook you should not soften
when you plan a recovery.

RabbitMQ has no supported consistent export of queue contents, and this system
does not need one. A message in flight was fsynced into its producer's outbox
*before* it was published, so its bytes are in the outbox artefacts above. What
the restore does with them is section 6: `services/common/reconcile.py`
republishes exactly the confirmed envelopes the restored `ingest_log` cannot
account for, and envelopes that were accepted but never published are drained
by the producer's own publisher loop when it starts.

`manifest.json` records the depth of every queue at the moment of the backup,
so the number of message bodies a given backup did not capture is a value you
can read rather than a thing you have to infer.

Also not backed up:

* **Container images.** The manifest records the digests in use; it does not
  contain the images. A restore needs them already present on the host, which
  for an air-gapped install means the offline bundle.
* **The staged model weights** under `models/`. They are a bind mount, not a
  volume, and they are part of the install, not of the live state.
* **`.env`.** Secrets stay where they are. See the warning in section 8 about
  the RabbitMQ user.

## 3. The acceptance boundary — what is protected, from when

`services/common/outbox.py` defines the boundary for the running system, and
this runbook does not move it:

> A producer never publishes straight to RabbitMQ. It writes the envelope to a
> local SQLite file on a persistent volume, with `synchronous=FULL` so the row
> is on disk before the write returns. **That fsynced row is the acceptance
> point.** From it on, the record survives the broker being down, the consumer
> being down and the database being down. Before it, nothing is promised.

Backups add a second boundary on top of that one, in time rather than in the
pipeline:

> A backup protects everything the stack had accepted **before
> `manifest.json`'s `started_at`**. Everything accepted after that timestamp is
> outside the backup.

`started_at` is taken before the first byte is read, not after the dump
finishes, so it is the conservative end of the window: a record accepted while
the backup was running may or may not be in it, and the manifest promises only
the part that is certain. That timestamp is the RPO reference point, and
`backup_manifest.py` refuses a manifest whose `rpo_reference` says anything
else.

## 4. Taking a backup

```bash
# The default: the `aow` stack, into ./backups/<UTC timestamp>/
bash scripts/backup-state.sh

# A different stack, or a different destination
bash scripts/backup-state.sh --project aow --dir /mnt/backups/aow-2026-09-24
```

The stack must be running: `postgres` and `rabbitmq` are mandatory, and each
producer that is down is recorded in the manifest as `present: false` with the
reason rather than being quietly skipped. The last line of output is
`BACKUP_DIR=<path>`, for scripting.

Before it exits the script re-measures every file it wrote and checks it
against the manifest it just produced. A backup that cannot verify itself the
minute it is taken will not verify six weeks later either.

**Copy the directory off this host.** By default it lands next to the working
tree, on the same disk as the volumes it is protecting, which protects you
against a bad migration and a dropped table but not against a dead disk.

## 5. Retention

The policy for this system, and why:

* **Keep the last 7 backups taken on separate days, plus 4 weekly ones.**
* **Take one before every connected refresh (`make refresh`) and before every
  upgrade**, because those are the two operations that change stored data in
  bulk.
* **A backup older than about 16 days can restore user data but not useful
  weather.** The staged forecast covers roughly 16 days, so beyond that the
  `weather_daily` rows in an old dump are outside their own coverage window and
  the agent will correctly refuse to answer from them. They are still worth
  keeping for the irreplaceable part: itineraries, user corrections and the
  `record_history` behind them, none of which can be re-fetched from anywhere.
* Size makes this cheap, and no figure is quoted here on purpose. The two
  artefacts that grow are the Postgres dump and the ingestor's outbox, both of
  which scale with how many forecast days a five-city stack is holding, so a
  number written down in a runbook goes stale quietly. `backup-state.sh` prints
  the byte count and SHA-256 of every artefact as it takes it, and
  `manifest.json` keeps them: read the last backup you took rather than trusting
  a sentence. §9 transcribes one run's breakdown, tied to that run and to no
  other — it is a sample of what this stack produced once, not a retention
  figure to plan against.

Pruning is manual. Before deleting a directory, check that the seven most
recent **separate backup days** and four weekly backups remain, then remove
only the older directories selected by that review. A simple "keep the seven
newest directories" command would violate the weekly retention rule whenever
daily backups are present.

Nothing prunes automatically. This stack has no scheduler, and adding one would
mean a component that can silently stop — at which point the operator believes
in a retention policy that is not running. A line in a runbook that a human
executes is honest about what it is.

## 6. Restoring

`restore-state.sh` restores into a **separate Compose project** by default
(`aow-restore`), and refuses to destroy a target that is still running unless
you say so explicitly. Rehearse there, look at the result, and only then
decide.

```bash
# Rehearsal / inspection: a parallel stack, nothing else touched.
# `edge` is excluded because it publishes 8080 and 8000, which a running stack
# already holds.
bash scripts/restore-state.sh backups/20260924T101500Z \
  --services "postgres rabbitmq migrate consumer api ingestor enricher agent llm ui" \
  --disruption-at 2026-09-24T10:15:26Z

# The real thing, after the live stack is down and you have decided to discard
# whatever it now holds:
docker compose down
bash scripts/restore-state.sh backups/20260924T101500Z \
  --project aow --overwrite-live-project
```

Two guards stand in front of the `down -v`, and both print the two commands
you might have meant rather than only saying no:

* **The live project.** Without `--overwrite-live-project` the script will not
  target `aow` (or whatever `AOW_PROJECT` names) at all.
* **A target that is still running.** Without `--overwrite-running-target` the
  script will not destroy the volumes of *any other* project that currently has
  containers up. This is the one that catches a second rehearsal: restoring a
  different backup into `aow-restore` while you are still reading the first one
  would otherwise wipe it without a word. The test is running containers, not
  volumes that exist, so restoring again into a project you have stopped — or
  one that never existed — still works with no extra flag, which is exactly what
  `demos/06_backup_restore.sh` does after its own `down -v`.

Both refusals exit 2 and happen before anything is touched. That is not a
formality: the target's volumes are destroyed and recreated, so a restore aimed
at the wrong project on the strength of a path typed at 3am is how a recovery
becomes the outage.

`--disruption-at` is optional and changes nothing the restore *does*; it
changes what the last section can report. Without it the script prints
`measured RPO: not computed` and states the reference point on its own. With it
— the moment the state was lost, in the same `YYYY-MM-DDTHH:MM:SSZ` form the
manifest uses — it subtracts the backup's `started_at` and reports the RPO as a
measured span rather than a timestamp you still have to do arithmetic on. That
is the difference between "everything after 10:15:00Z is gone" and "26 s of
accepted work was lost", and it is why `demos/06_backup_restore.sh` always
passes it. If you do not know the moment, leave it out; an invented one is worse
than none.

What it does, in order:

1. **Verifies** every artefact against the manifest's SHA-256 and byte count,
   and stops if anything differs. Nothing has been touched at that point.
2. Warns if the migration files in the working tree differ from the ones
   recorded in the backup — a restored dump goes into the schema *this tree*
   creates, not the one it was written against.
3. `docker compose down -v` on the target project: **fresh volumes**.
4. Starts `postgres` and `rabbitmq`, waits for both to be healthy, and runs the
   migrations. The migrations are where `aow_writer` and `aow_reader` and their
   passwords come from; a database dump carries the grants to those roles but
   not the roles themselves, so `pg_restore` would fail without this step.
5. `pg_restore --clean --if-exists --no-owner --exit-on-error`.
6. `rabbitmqctl import_definitions`.
7. Creates the producer containers without starting them, so Compose makes the
   labelled volumes, and writes each outbox copy onto its volume owned by uid
   `10001` — the uid `services/Dockerfile` runs as. A root-owned file there
   would leave the producer unable to open its own outbox.
8. Starts the stack, then runs `services/common/reconcile.py` in each producer.
   **This is the step the dump alone cannot do.** An envelope the producer had
   confirmed to the broker, but whose database write had not happened when the
   dump was taken, is in the outbox and not in `ingest_log`; reconcile
   republishes exactly those IDs, and the consumer's `message_id` primary key
   makes a repeat harmless. The script reports what it replayed.
9. Verifies, and prints the measured RTO and the RPO the manifest defines.

## 7. Verifying a restore yourself

The restore's own verification is deliberately shallow — it proves the pieces
came back. The check that matters is one you run afterwards, from a **separate
reader**: not the API that accepted the writes, and not a row count, which
cannot tell a loss and a duplicate apart.

```bash
P=aow-restore     # or aow, after a live restore
DC=(docker compose -p "$P" -f compose.yml)

# On an offline release install, run these from the release folder and layer the
# bundle overlay on, which is the invocation the two scripts make for themselves:
#
#   export AOW_IMAGE_VERSION="$(cat release-version.txt)"
#   DC=(docker compose -p "$P" -f compose.yml -f compose.bundle.yml)
#
# `exec` attaches to a container that is already running and finds it by Compose
# project and service name, so the image a Compose file names is not what these
# commands turn on. The overlay is included because it costs nothing and keeps
# the rendered stack identical to the one the scripts addressed; whether `exec`
# would also work without it is not something this runbook needs to rely on.
# The two go together: `compose.bundle.yml` demands `AOW_IMAGE_VERSION` and
# fails by name without it.

Q() { "${DC[@]}" exec -T postgres \
        psql -U aow -d aow -tAc "$1"; }

# For each message_id you care about: exactly one, never "at least one".
Q "SELECT count(*) FROM ingest_log WHERE message_id = '<id>'"      # must be 1

# Nothing was duplicated anywhere.
Q "SELECT count(*) - count(DISTINCT message_id) FROM ingest_log"   # must be 0

# The domain rows are there, not just the ledger.
Q "SELECT count(*) FROM weather_daily"
Q "SELECT count(*) FROM itineraries"
Q "SELECT status, count(*) FROM recommendations GROUP BY status"

# The producers can open their restored outboxes.
"${DC[@]}" exec -T api python -c \
  "from services.common import config
from services.common.outbox import Outbox
print(Outbox(config.OUTBOX_PATH, readonly=True).counts())"
```

If you know a specific `message_id` that was in flight when the loss happened,
ask the producer directly rather than guessing:

```bash
"${DC[@]}" exec -T api \
  python -m services.common.reconcile --id <message_id>
```

There are three answers, and **two of them are answers rather than failures**.
Each producer has its own outbox, so a `message_id` you hold belongs to exactly
one of `api`, `ingestor` and `enricher`, and you will usually have to ask all
three in turn to find out which.

| What you get | Exit | What it means |
|---|---|---|
| a JSON audit line | 0 | This producer owns the id. `"missing": 0` means the restored database already accounts for it; `"missing": 1`, with the id in `missing_ids`, means it does not — `--replay --id <id>` republishes exactly that envelope. |
| `reconcile: <id> is absent from this producer outbox` | 3 | Not a failure. This producer never accepted it. Ask the next one. |
| `reconcile: <id> is still unpublished; the normal publisher owns it` | 3 | Not a failure, and the best of the three: the record is in the outbox, was never published, and this producer's own publisher loop sends it when it starts. There is nothing for you to do. |

Any other non-zero exit is a real problem: 2 is argparse rejecting the
arguments, and an outbox row whose stored envelope does not match it raises with
a traceback on purpose, because that is corruption rather than a lookup miss.

## 8. The limits — read these before you rely on any of it

* **There is no point-in-time recovery.** `pg_dump` is a logical dump; nothing
  here ships WAL archiving. You can restore to a backup, not to a moment
  between two backups. Everything accepted after `started_at` is gone.
* **Weather can be re-fetched; user data cannot.** A forecast lost inside the
  RPO window comes back with `make refresh` while connected. An itinerary, a
  correction or an activity request accepted inside that window does not exist
  anywhere else.
* **Queue contents are not backed up.** Only envelopes that reached a producer
  outbox can be replayed. A message the broker held that no outbox knows about
  — there is no such message in this system by construction, but the boundary
  is stated rather than assumed.
* **`import_definitions` restores the RabbitMQ users from the backup**,
  including the `aow` user's password hash. If `RABBITMQ_PASSWORD` in `.env`
  changed after the backup was taken, the restored broker will expect the *old*
  password and the services will fail to authenticate. Postgres does not behave
  this way — its role passwords are set from the current `.env` by the
  migrations — so the two halves are asymmetric. Restore with the `.env` that
  was current when the backup was taken, or reset the broker user afterwards.
* **Backups are stored beside the stack by default.** Same host, same disk. A
  host loss takes them with it unless you copy them off.
* **The restore needs the images.** The manifest records digests; it does not
  contain images. On an air-gapped host they must already be loaded.
* **On an offline release install, both commands find the bundle themselves.**
  The bundle loads its images as `aow-bundle/<alias>:<commit>`, not as the
  `aow/services:dev` and `aow/ui:dev` tags `compose.yml` names, so neither
  script may run the plain file. Run them from inside the release folder and
  neither needs an argument: both see `release-version.txt`, export
  `AOW_IMAGE_VERSION` from it, default the overlay to `compose.bundle.yml`, and
  pick the bundle's `aow-bundle/services:<commit>` for the helper containers
  they start outside Compose. `restore-state.sh` also adds `--pull never` to
  every `up` and `create`, so nothing reaches for a registry.

  ```bash
  cd <the release folder>        # the one holding release-version.txt
  bash scripts/backup-state.sh
  bash scripts/restore-state.sh backups/<id>
  ```

  The explicit form is the **override**, for the two cases the auto-detection
  cannot cover: running against a bundle-started stack from a checkout that has
  no `release-version.txt`, or selecting a different overlay such as CI's
  `compose.ci.yml`.

  ```bash
  export AOW_IMAGE_VERSION="<commit>"
  export AOW_SERVICES_IMAGE="aow-bundle/services:$AOW_IMAGE_VERSION"
  AOW_COMPOSE_OVERLAY=compose.bundle.yml bash scripts/backup-state.sh
  AOW_COMPOSE_OVERLAY=compose.bundle.yml bash scripts/restore-state.sh backups/<id>
  ```
* **This is proven on one machine.** The drill restores into a fresh Compose
  project on the same Docker daemon. Cross-host and cross-architecture restores
  are not exercised.
* **No absolute guarantee is claimed.** Destroyed backup media, an exhausted
  disk and data that was never accepted are outside every promise here, exactly
  as the README says for M11.

## 9. The drill, and the numbers it measured

```bash
bash demos/06_backup_restore.sh
```

It needs no arguments and no running stack. The volumes are always its own —
an isolated Compose project, torn down again on the way out. The credentials are
its own only when it has to invent them: it takes `AOW_ENV_FILE` if that is set,
then a `.env` beside the working tree if there is one, and generates a throwaway
set only when neither exists (`demos/06_backup_restore.sh`). So on a developer
machine with a `.env` it reuses those passwords in its own project. It never
starts `edge`, so it publishes no ports and cannot collide with a running
stack.

It accepts three sets of records, each traced by its own `message_id`:

* **A** — accepted and stored before the backup. After the restore each must be
  in `ingest_log` exactly once, with its `recommendations` row. This is the
  guarantee.
* **C** — accepted with the consumer stopped, confirmed to the broker, and so
  in the API's outbox but *not* in the database when the dump was taken. It
  comes back only because the restore runs `reconcile.py`.
* **B** — accepted after the backup started, and committed on the live stack
  before the disruption. After the restore these must be **absent**: that is
  the RPO boundary, asserted as a value rather than described. A B id that came
  back present would mean the backup window is not where this runbook says it
  is, and fails the drill.

Then it destroys the project's volumes (`down -v`), restores, and verifies with
`psql` from a separate session.

**Measured on the development machine** (Windows 11, Docker Desktop 4.92, WSL2
backend, CPU only) by the command above. Two runs are on record. They are kept
apart on purpose, each named by its backup's `manifest.json` `started_at` — the
one identifier the drill prints and the artefact carries. A figure is only worth
reading next to the run it came from, and these must not be merged into one
guarantee.

| Run | RPO | At-risk window | RTO | Wall clock |
|---|---|---|---|---|
| **`2026-09-24T16:09:39Z`** — the tree as merged, and the one quoted verbatim in the evidence document | **26 s** | 31 s | **35 s** | **120 s** |
| `2026-09-24T15:56:01Z` — the same drill before merge `3b40949` | 24 s | not recorded | 31 s | 110 s |

The console output of the later run is quoted verbatim in
[EVIDENCE-observability-and-recovery.md](EVIDENCE-observability-and-recovery.md)
§4, with all six `message_id`s and the per-id assertions. The earlier run is
summarised in the same document.

The two straddle merge commit `3b40949` (2026-09-24T16:08:11Z, PR #11), which
added `db/migrations/006_event_validity.sql` and `007_city_coast.sql` and about
65 seeded event rows. The later run's backup started 88 seconds after that
merge; the earlier one predates it. `scripts/restore-state.sh` runs *this tree's*
migrations inside the window it reports as the RTO, so more migrations and a
larger dump are a **plausible** mechanism for 31 s → 35 s. One sample on each
side of a merge, on a shared development daemon, cannot attribute the
difference, and it is not claimed as a measured cause.

CI figures exist too and are deliberately not in the table above, because a
GitHub-hosted runner is a different machine and not comparable. Two runs are on
record, kept apart the same way, each named by its run id, its job, its backup's
`started_at` and its commit:

* **CI run `36256406183`, job `restore-drill`, `started_at`
  2026-09-26T16:45:31Z, commit `bbee42c`** — the current commit. RPO span
  (backup start → last lost write) **15 s**; at-risk window (backup start →
  disruption) **19 s**; 2 of 2 set B records lost, which is the designed result.
  **Two RTO numbers, from two different vantage points, and not
  interchangeable:** `scripts/restore-state.sh` self-reported `RTO_SECONDS=20`,
  which is its own invocation to its own verification passing — the window
  described in §6 step 9 — while the drill's summary reported **22 s**, which is
  that same restore plus the drill's own per-id `psql` assertions from a
  separate reader (§7). **Two wall clocks, likewise distinct:** **92 s** for the
  drill body, and **104 s** for the whole CI job step around it. Neither pair
  may be collapsed into a single figure.
* CI run `36055211121`, `restore-drill`, `started_at` 2026-09-24T20:31:40Z,
  commit `862a08f` — RPO 15 s, at-risk window 19 s, RTO 20 s, 104 s. It was
  recorded before the two RTO vantage points were written down separately, so
  read its single RTO as the one figure it names and nothing more.

Both are recorded in [CICD_EVIDENCE.md](CICD_EVIDENCE.md) §3.

### The backup-size breakdown and row count

Transcribed from the one run recorded with them: **CI run `36256406183`, job
`restore-drill`, `started_at` 2026-09-26T16:45:31Z, commit `bbee42c`**. That is
a GitHub-hosted runner, and the drill's own isolated Compose project — **not**
the development machine of the table above, and **not** a production stack — so
these bytes and rows belong to that run alone.

| Artefact | Bytes, as `scripts/backup-state.sh` printed them |
|---|---|
| `outbox-ingestor.sqlite3` | 753,664 |
| `postgres.dump` | 113,623 |
| `outbox-api.sqlite3` | 20,480 |
| `outbox-enricher.sqlite3` | 20,480 |
| `rabbitmq-definitions.json` | 1,441 |

Those five counts are what the script logged, each beside its SHA-256. **Their
total, 909,688 bytes (888.4 KiB), is arithmetic done here: the script prints the
five per-artefact counts and no sum, so the total is not itself a logged
figure.** The ordering is the one §5 predicts — the ingestor's outbox and the
Postgres dump are the two artefacts that grow with how many forecast days a
five-city stack is holding, and the other three are effectively fixed.

Row counts from the same run. `ingest_log` held **836 rows** once the snapshot
had finished loading and before the drill accepted anything: a count for the
drill's own isolated project, not a production figure. At backup time the
ingestor's outbox held 836 envelopes, the API's 4 and the enricher's 0, each
copied with `pending: 0` and `integrity: ok`. One message was sitting in
`aow.ingest` and none in `aow.dlq`, and the backup recorded both as **not
captured**, because message bodies are never backed up (§2). After the restore
and `reconcile.py`, `ingest_log` held 840 rows: the 836 baseline, the three
set A records accepted before the backup, and the one set C envelope that
reconcile replayed.

The RPO figures are a property of *these drills*, not of the system: they are
simply how long each run kept writing after its backup. In production the RPO is
the interval between backups, because that is the window in which accepted work
has nowhere else to come back from. What the drill proves is that the boundary
sits exactly at `manifest.json`'s `started_at` and not somewhere vaguer.

Re-measure after any change to the stack's size. The RTO is dominated by
`pg_restore` and by how long the consumer takes to drain whatever reconcile
replayed, and both grow with the data.
