# act-on-weather

Weather for five cities, turned into activity recommendations by a local
open-weights model, with every collected record travelling through a queue into
Postgres. Once it has been staged, the whole stack runs with no internet at all.

Ask it about the weather in Rome on 2026-09-24 and the answer names the weather
source and the time the forecast was collected. Ask about a date outside the
stored window and it says it has no forecast rather than guessing.

---

## What it does

| | |
|---|---|
| **Collects** | daily forecasts for Rome, London, Lisbon, Tel Aviv and Reykjavík, plus places, background articles and events |
| **Decides** | a deterministic suitability score per (city, day, activity) over 18 activities, from rules in `data/activities.yml` |
| **Words** | a local Qwen3-1.7B writes a sentence or two about the day's top-scoring activities (`ENRICH_TOP_N`, default 6; the rest are scored and charted, but not worded) |
| **Answers** | an agent resolves the question in code and answers from stored rows only |
| **Plans** | a day-by-day itinerary assembled from rows that actually exist |
| **Updates** | corrections, refreshes and re-wording, all through the queue |

The deterministic score is the source of truth. The model only phrases it, so a
model outage degrades the wording and never the content.

### The data on board, and when it goes stale

The repository ships a committed snapshot in `data/snapshot/` (about 357 KB).
This is fixed data, not a live feed. The counts below are derived from the files
by `scripts/snapshot_manifest.py` and recorded in `data/snapshot/MANIFEST.json`:

| data | amount | coverage |
|---|---|---|
| Weather | 80 rows: 16-day daily forecasts for each of the 5 cities | **2026-09-23 to 2026-10-08**, collected 2026-09-23 |
| Places | 620 places | 5 cities |
| Background | 81 background articles | 5 cities |
| Events | 55 hand-verified events (10 London, 10 Rome, 8 Tel Aviv, 13 Reykjavík, 14 Lisbon) | checked 2026-09-24, expiring 2026-10-15 |
| Sample events | 45 labelled samples | demo mode only |

**Coverage is frozen until you refresh it.** A normal run never fetches, so the
forecast window and the event listings age from the dates above. After
2026-10-08 every weather question is out of coverage; after 2026-10-15 every
verified listing has expired and a default run reports no current events. The
expiry is derived, not written into the rows: `AOW_EVENT_RECHECK_DAYS` (default
21) from each listing's `checked_at`. Every answer and every chart carries its
as-of stamp, and a question past the window is refused rather than guessed.
[Connected refresh](#connected-refresh) moves the weather window forward.

The verified event set is 55 rows across all five cities, and it is a
hand-checked sample of venue and organiser listings rather than a feed: 47
concerts, 4 dance, 2 sport (both London), 1 theatre, 1 comedy. Extending a
listing is a manual, one-row-at-a-time operation.

---

## Prerequisites

**Docker is the only thing the documented run path needs** — Docker Desktop on
Windows or macOS, Docker Engine with the Compose plugin on Linux.

| | |
|---|---|
| **Compose v2** | the `docker compose` subcommand, with a space. Standalone `docker-compose` v1 cannot read the top-level `name:` key these files use. Verified on Docker Engine 29.8 with Compose v5.5.1. |
| **Memory** | 8 GB for Docker. The `compose.yml` caps total 6.7 GB, 3 GB of it the model server. |
| **Disk** | about 6 GB of images and model, plus ~1.2 GB more if you build the test image. |
| **CPU** | CPU-only, and that is the only mode. No GPU override ships. `LLM_THREADS` in `.env` (default 4) is the knob. |
| **Internet** | for staging only. Everything after it runs with the network off. |
| **Ports** | 8080 (UI) and 8000 (API), on `127.0.0.1` only. Nothing else is published. |
| **Architecture** | verified on linux/amd64. The pinned digests are multi-arch manifests that include arm64, but that combination is untested. The offline release bundle is amd64-only and refuses anything else. |

Some optional paths need more than Docker: `bash` for `make bootstrap`,
`make verify`, `make backup` and `make restore`; `python3` on the host as well
for `make verify`; and `git`, `gh`, `python3` and `sha256sum` for building a
release bundle.

---

## Setup

### The short path

If your host has `bash` (the Git Bash that came with `git` counts on Windows):

```sh
bash scripts/bootstrap.sh
```

That runs every step below, and adds the checks a person otherwise does by eye:
Docker is installed and its daemon is up, Compose v2 is present, the memory and
disk are there, `.env` has real passwords rather than `change-me`, and the stack
has actually turned healthy. It ends by printing the URLs. `make bootstrap` is
the same thing.

A plain run fetches nothing, so the coverage window is whatever the clone
shipped with (see [The data on board](#the-data-on-board-and-when-it-goes-stale)).
To start from current weather instead, add `--refresh`:

```sh
bash scripts/bootstrap.sh --refresh
```

Either way the closing report states which of the two you got, so a stale
forecast is never mistaken for a fresh one. If the fetch fails, it says so and
exits non-zero while leaving the stack up and usable.

It creates `.env` only if there is none, generating a distinct random password
for each `change-me` inside the pinned `python:3.12-slim` image with no network.
**An existing `.env` is never read, rewritten or replaced.** Re-running is safe:
it never regenerates a password, never re-downloads the model, and never removes
a volume.

| flag | |
|---|---|
| `--refresh` | once the stack is healthy, fetch a fresh forecast, then close the egress window and assert it is closed. Needs a network, so it is refused together with `--offline`. It fetches the forecast and nothing else — see [Known limitations](#known-limitations) |
| `--offline` (`--skip-stage`) | skip the connected commands and check instead that this machine is already staged |
| `--no-start` | stop after staging |
| `--wait-only` | poll a stack that is already up |
| `--timeout N` | seconds to wait for health (default 900; `llm` reports unhealthy for about 3 minutes while it loads the model) |

### The same thing by hand

**1. Settings.** On Linux and macOS:

```sh
cp .env.example .env
```

On Windows, in PowerShell:

```powershell
Copy-Item .env.example .env
```

Then open `.env`, replace **every** `change-me` with a different password, and
save. The file is gitignored; keep it private.

**2. Stage it, once, with internet.**

```sh
docker compose pull postgres rabbitmq llm edge
docker compose -f compose.tools.yml run --rm stage
docker compose build
docker compose -f compose.tools.yml build demos
```

`pull` fetches the four upstream images (~2.2 GB), every one pinned by digest.
`stage` downloads the model (~1.2 GB) into `models/` and checks it against
`models.lock`; an already-staged model is re-verified rather than re-fetched, so
running it twice is safe and quick. `build` builds the five Python services and
the UI. `build demos` builds the proof runner now, while there is still a
network, because the proofs have to work after it goes away. To stage from an
internal mirror instead of the public internet, set `MODEL_BASE_URL` in `.env`;
the checksum check is identical either way.

**3. Run it.** No internet needed:

```sh
docker compose up -d
```

To check the staging first, without starting anything that stays up and without
a network: `docker compose config --quiet` (fails by name on any password still
missing from `.env`, which is the usual way a fresh clone goes wrong) and
`docker compose -f compose.tools.yml run --rm stage` (re-hashes the staged
model). `make preflight` runs both.

---

## Using it

Open **<http://localhost:8080>**. The machine-readable API schema is at
<http://localhost:8000/openapi.json>. There is also a Swagger page at `/docs`,
but it is FastAPI's default, which pulls its JavaScript and CSS from a CDN, so
**`/docs` is the one part of this system that does not work offline.** Both
ports are published on loopback only; see [Security](#security). The binding is
IPv4 loopback, so on a host whose browser resolves `localhost` to `::1` without
falling back, use <http://127.0.0.1:8080>.

The first start takes several minutes while the model loads and the snapshot
enters the database. `docker compose ps` shows progress; an exited `migrate`
container with code 0 is normal. If the page does not load after a few minutes,
`docker compose logs --tail 50` shows the startup messages.

Nine tabs: **Dashboard** (the best stored activity per city for a chosen day),
**Forecast** (temperature and rainfall per city), **Suitability** (a
city × day × activity heatmap, plus a box for any activity you type),
**Trip planner** (a day-by-day plan, saved as a row like any other),
**Places map** (stored places on a tile-free basemap bundled into the image),
**Ask the agent** (chat, with a panel showing which rows the answer used),
**Update data** (the three update paths below), **Data coverage** (what is held,
per record type and per city) and **Monitoring** (links only; it renders no
metrics itself).

### Demo mode

A default run stores the verified events only, and answers "none on record" for
any city, date or category the feed does not cover. For a denser event calendar:

```sh
docker compose -f compose.yml -f compose.demo.yml up -d
```

That adds the 45 generated sample events alongside the 55 real listings, labelled
wherever they appear, with a demo banner in the UI. `docker compose up -d` returns to
verified events only and removes the generated rows from the database.

### Stopping and resetting

```sh
docker compose down      # keeps the database and queue data
docker compose up -d     # start again
docker compose down -v   # DELETES this project's Postgres, RabbitMQ and outbox volumes
```

`make clean` is the last one. Saved itineraries, user edits, queued messages and
edit history live in those named volumes; they are not in Git and not in the
release bundle, so a new installation starts with no saved trips. The **Wipe all
user data** button on **Update data** is the softer reset: it clears saved trips,
manual corrections and edit history, and rebuilds the collected data from the
ingestor's retained source messages.

---

## Architecture

Eleven containers: `postgres`, `rabbitmq`, `migrate` (one-shot), `llm`,
`ingestor`, `consumer`, `enricher`, `api`, `agent`, `ui`, `edge`.
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) is the full guide**, with the
rendered diagrams, the message lifecycle and the technology notes.

### The data flow

1. The **ingestor** reads the committed snapshot (or fetches live, when attached
   to an egress network) and fsyncs each record into a local SQLite outbox on a
   named volume. **That write is the acceptance point.**
2. A publisher loop drains the outbox to RabbitMQ under publisher confirms with
   `mandatory=True`. A record is marked published only once the broker confirms
   it.
3. The **consumer**, the only role holding write grants, validates the payload,
   then writes the business row **and** the `message_id` into `ingest_log` in one
   transaction, and acks only after that commits.
4. Storing weather immediately computes and stores the rule-based score for every
   supported activity. The top `ENRICH_TOP_N` per city-day (default 6) are
   `pending` for wording; the rest are scored and `deferred`.
5. The **enricher** polls pending rows, makes one grammar-constrained call to the
   local model, fsyncs the result into its own outbox, and publishes it back into
   the exchange.
6. The **api** and **agent** read as `aow_reader`, which holds `SELECT` and
   nothing else. Every API write is fsynced to the api's own outbox and answered
   `202` with a `message_id`, so writes are eventually consistent and nothing but
   the consumer writes to Postgres.

One durable topic exchange (`aow.events`) feeds one quorum queue (`aow.ingest`)
with a dead-letter exchange to `aow.dlq` and `x-delivery-limit: 5`. The three
producer outboxes are SQLite in WAL mode with `synchronous=FULL`, each on its own
named volume.

### Networks and ports

| network | who is on it | why |
|---|---|---|
| `backend` | every application service | `internal: true`, so Docker itself gives it no gateway |
| `frontend` | `edge`, plus `edge-observability` when the monitoring overlay runs | Docker cannot publish a port from an internal network, so a proxy has to straddle the boundary |
| `egress` | nobody | declared for the connected overlay. A default `up -d` attaches no container to it; only `compose.connected.yml` puts the ingestor on it |
| `<project>_refresh_egress` | nobody, between refreshes | the operator refresh creates it, attaches the ingestor for one fetch, then detaches it and deletes the network |

**No application service has a route out** — not the model server, the agent, the
broker or the database. The only containers on the routable bridge are the nginx
proxies, because a published port requires one; they hold no outbound client, no
credentials and no application logic.

Only 8080 and 8000 are published, both on `127.0.0.1`. Not the database, the
broker, the management UI, the agent or the model server. The binding is spelled
out as `${AOW_BIND_ADDR:-127.0.0.1}:8000:8000` rather than the short form, which
would bind `0.0.0.0`; `tests/unit/test_compose_ports.py` holds that over every
`compose*.yml`.

### Technical choices

| choice | why | rejected |
|---|---|---|
| **Open-Meteo** | no API key, so the air-gapped bundle carries no secret and no account to expire; the free tier includes the 16-day daily forecast that "this week" needs | **OpenWeather** — a key to manage, and a free tier that splits the horizon awkwardly |
| **RabbitMQ**, quorum queues | per-message ack after a database commit, a real dead-letter exchange and `x-delivery-limit` are exactly the primitives the no-loss requirement needs, with the smallest operational surface | **Kafka/Redpanda** — offset-based, so per-message quarantine needs machinery, and heavier for one node. **Redis Streams** — a weaker durability story |
| **An outbox in front of the broker** | publisher confirms cannot help if the broker is unreachable; the outbox makes "the broker is down" a delay rather than a loss | **confirms only** — leaves a window where an accepted record exists nowhere durable |
| **Deterministic score, model phrases it** | the verdict is reproducible, testable and defensible, and a model outage degrades only the wording | **the LLM decides suitability** — unrepeatable, untestable, and it puts a 1.7B model on the critical path |
| **Router-first agent** | city, dates and coverage resolved in readable code, with at most one model call to phrase retrieved rows | **model tool-calling** — several sequential generations on CPU, non-deterministic in front of a reviewer |
| **Qwen3-1.7B Q4_K_M** on llama.cpp | Apache-2.0, 1.2 GB on disk, fast enough on CPU to answer interactively, and reliable under a JSON-schema grammar | a 3–4 B model — better prose, but materially slower on a CPU-only machine |
| **Streamlit** | nine working pages in the time an SPA would take to scaffold, and it bundles its own assets, so it works offline | a React SPA — more polish, but the pipeline carries more weight here |
| **Docker Compose** | one command, identical on Linux, macOS and Windows | Kubernetes — a [production path](#production-path), not the demo path |

Thinking mode is disabled on the model (`enable_thinking: false`). Left on,
Qwen3 spends its token budget in `reasoning_content` and returns empty content —
a failure rather than a slowdown. Per-call latency was never captured by a
benchmark anyone can rerun, so no timing figures are quoted here; see
[TECHNICAL_DECISIONS.md](TECHNICAL_DECISIONS.md).

### The delivery guarantee, and its boundary

From the moment a record is fsynced into a producer's outbox, it reaches one of
exactly two terminal states: **stored** in Postgres, or **quarantined** in
`aow.dlq`, visible and redrivable. Consumer, broker and database outages delay a
record; none of them lose it. The mechanism is at-least-once delivery plus
idempotent writes, keyed on the `message_id` that is inserted into `ingest_log`
in the same transaction as the business write.

**Not covered**, stated plainly: a destroyed volume, a full disk, and data that
was never accepted in the first place. Weather that was never fetched can be
re-fetched while connected. There is no absolute guarantee here.

---

## Offline operation

```sh
docker compose -f compose.tools.yml run --rm demos offline
```

That proof checks four things structurally — `backend` really is `internal`, four
representative services (the model server, the agent, the consumer and the API)
each fail to open an outbound connection, no hosted-model SDK or endpoint exists
anywhere in the source, and the model is loaded from a local bind mount — and
then answers two in-range questions and one deliberately out-of-range one. Its
in-range questions are relative to the stored window, so after 2026-10-08 they
become refusals too and a connected refresh is what restores the demonstration.
The stronger proof is behavioural: disable the host's network adapter and run it
again.

| | |
|---|---|
| Fetch new weather | **needs internet.** Offline the window stops moving, and questions beyond it are refused |
| Discover new places or events | **needs internet.** Offline the system says it has no record rather than inventing one |
| Correct a stored record | **works offline** — a local write through the local queue |
| Re-word recommendations | **works offline** — the model is local |

A fresh clone on its own is **not** ready for offline use: the images and the
model are not in Git.

### Installing a packaged release

Follow the [offline-host procedure](docs/RELEASE.md#operator-procedure-offline-host)
from inside a verified release bundle. Set real passwords in `.env` on the first
install; on an upgrade, carry the previous release's `.env` forward. The installer
prints the Docker engine ID and the image, volume and container counts before
loading the bundle. It refuses a fault-injection test artifact unless
`AOW_ALLOW_FAULT_INJECTION=1` is set deliberately. The packaged
`scripts/prove-offline.sh` runs with `--no-build --pull never`, so a missing image
fails the proof instead of starting a build or a pull.

### Connected refresh

Three update paths, two of which work offline:

1. **A correction.** `PATCH /records/{entity}/{id}` → `202` and a `message_id` →
   queue → consumer → `revision + 1` and a `record_history` row. Works offline.
2. **A connected refresh.** Re-fetches the forecast and moves the coverage window
   forward. Needs an internet connection.
3. **Re-enrichment.** `POST /reenrich` flips the selected recommendations back to
   `pending` and the enricher rewords them. Scores are untouched, since only a
   weather refresh changes them. Works offline.

All three are on the **Update data** tab, which names each one, says which work
air-gapped, and shows the command for the one that cannot.

The refresh is one command, while connected:

```sh
docker compose -f compose.tools.yml run --rm refresh
```

`make refresh` is the shorthand; `--city SLUG`, `--days N` and `--wait N` narrow
it. It does not use the declared `egress` network. It creates an ephemeral
`<project>_refresh_egress`, attaches the ingestor for the length of one fetch,
then detaches it and deletes the network, and asserts both. A detached guard
closes the window after `REFRESH_WINDOW_MAX_S` (default 600) even if the command
itself is killed — so a `kill -9` of the refresh leaves the ingestor attached for
up to that deadline, which is the residual; `docker network inspect
<project>_refresh_egress` returning *not found* is the check.
`make refresh-check` runs the open-and-close drill with no fetch and no internet.
The last run is reported at `GET /refresh/last`, which is one file with no
history.

---

## Tests and operational tools

```sh
docker build -q -f tests/Dockerfile -t aow/tests:dev .
docker run --rm aow/tests:dev
```

Dependencies are baked into that image at build time, so the run itself makes no
network call; CI runs the same container with `--network none`. `make test` is
the shorthand. The unit suite is 38 modules under `tests/unit/`, covering the
rule engine's truth table, envelope round-tripping and the rejection of malformed
messages, payload validation, the outbox's two load-bearing properties (accepting
the same message twice is a no-op, and an accepted-but-unpublished record
survives the process dying), the agent's date parsing and intent matching, the
planner, API responses, UI rendering, the compose port bindings, and the snapshot
counts quoted in this file. The release artefacts have their own suites:
`test_bundle_tamper.py`, `test_bundle_archive.py` and `test_airgap_evidence.py`
cover bundle integrity, archive completeness and the evidence-capture tool.

`make verify` runs the four gates that can run locally: `ruff check`,
`ruff format --check`, the unit tests, and `scripts/snapshot_manifest.py
--check`. As the script itself puts it, passing it does not promise CI is green;
failing it promises CI is not.

`.github/workflows/ci.yml` defines eight jobs:

| job | when | what |
|---|---|---|
| `lint` | every run | `ruff check` and `ruff format --check` |
| `unit` | every run | the test image, run with `--network none` |
| `guard` | every run | the claim checks below |
| `build-and-scan` | every run | builds the service and UI images, runs three blocking Trivy scans, then `scripts/ci-integration.sh` against a real broker and database |
| `ui-gate` | every run | a Playwright browser gate: the pages render, a "Weather as of" chip carries a timestamp, and the browser makes **zero off-origin requests** |
| `publish-images` | push to `main` | pushes the images built and scanned above to GHCR |
| `model-grounding` | release candidates only — `workflow_dispatch`, a `release/*` branch, or the `release-candidate` label | the real model against hand-written rows |
| `restore-drill` | release candidates only, same condition | a restore after destroying every volume |

`publish-images` needs `build-and-scan` only, so the browser gate runs alongside
it rather than blocking it.

`.github/workflows/release.yml` is the CD half, on manual dispatch against a
merged SHA whose CI passed: it re-verifies the published image digests against
the registry, builds and verifies the offline bundle, proves a no-pull install on
a clean engine, and records the promotion. See [docs/RELEASE.md](docs/RELEASE.md).

`scripts/ci-integration.sh` drives five outage drills in a throwaway Compose
project, each with its own traced `message_id`: through the API with the consumer
stopped, the broker stopped and the database stopped, and through the ingestor
with the broker stopped and the database stopped. It then reconciles and replays
a confirmed envelope, restarts everything, and asks a separate reader connection
for all of the traced IDs at once. Any missing ID fails the job, and so does any
duplicate.

The `guard` job is what keeps this README honest. It fails the build if a pulled
image or a Dockerfile base is not pinned by digest, if a digest disagrees with
`IMAGES.lock`, if a workflow action sits on a movable tag rather than a commit
SHA, if a tracked `.env` appears or `.env.example` stops holding placeholders, if
gitleaks finds a secret (checked against a planted canary, so a silent scanner
fails too), if a hosted-model SDK or endpoint appears in the source, or if any
count quoted in `README.md`, the `Makefile`, `ASSIGNMENT.md`,
`TECHNICAL_DECISIONS.md` or `docs/ARCHITECTURE.md` disagrees with
`data/snapshot/MANIFEST.json`. That last one is not hypothetical: an earlier
README put the place count at 289 while the snapshot already held 620.

### Drills and operator commands

| command | what it does |
|---|---|
| `docker compose -f compose.tools.yml run --rm demos no-data-loss` | four drills — consumer down, database down, broker down, poison message — each following a single accepted `message_id` to its terminal state |
| `… run --rm demos questions` | the agent answering, including the missing-data path |
| `… run --rm demos update` | a correction end to end |
| `… run --rm demos reenrich` | re-enrichment, including a full model outage |
| `… run --rm demos backup-restore` (`make backup-restore`) | the backup and restore drill, with its measured RPO and RTO |
| `make dlq` / `make redrive` | list and redrive quarantined messages |
| `make backup` / `make restore DIR=…` | `pg_dump` plus the three outboxes and the broker definitions; restore defaults to an isolated Compose project |
| `make monitor` / `make monitor-down` | start and stop the opt-in Prometheus and Grafana overlay, Grafana at <http://127.0.0.1:3000> |

**The `demos` and `refresh` containers mount the Docker socket**, which is
root-equivalent access to the host's daemon. That is deliberate: the drills stop
the consumer, stop the broker and restart the model server, so driving Docker is
the proof. It is why they are a `docker compose run` you type on purpose and
never part of `up`, and why they live in `compose.tools.yml`. Nothing in the
running stack has the socket.

Monitoring is opt-in so a reviewer who does not want a time-series database and a
dashboard server on their laptop never pays for them. Both images are pinned by
digest and travel in the offline bundle, so turning monitoring on for the first
time on an air-gapped host downloads nothing. Prometheus scrapes the services and
llama.cpp's own `--metrics`; three dashboards (Service health, Pipeline, LLM
observability) and eleven alert rules are provisioned.

---

## Security

* Every upstream image and Dockerfile base is pinned **by digest**
  (`IMAGES.lock`, enforced in CI). The model is verified against `models.lock`
  before it is used.
* The images in the running stack run as **uid 10001** (`services/Dockerfile`,
  `services/ui/Dockerfile`). Upstream images run at their own defaults, so
  `docker top` shows Grafana as 472, Prometheus as 65534 and the nginx master as
  root. The proof-runner image (`demos/Dockerfile`) runs as root by necessity —
  see the Docker-socket note above.
* **Three database roles**: the owner runs migrations; `aow_writer` (the consumer
  only) may `INSERT`/`UPDATE` plus narrow `DELETE` grants; `aow_reader` (api,
  agent, enricher) may only `SELECT`. Enforced by grants.
* Secrets live only in a gitignored `.env`; `.env.example` is committed and holds
  placeholders only.
* The base stack publishes two ports and the monitoring overlay adds a third, all
  on `127.0.0.1` by default.
* The UI is served under a `default-src 'self'` CSP that blocks every off-origin
  request in the browser. It carries `'unsafe-inline'` and `'unsafe-eval'`
  because Streamlit's bundle requires them. The API's `/metrics` is 404'd at the
  proxy; Prometheus scrapes it over `backend` instead.
* CI runs blocking **Trivy** (`HIGH,CRITICAL`) on both images and the filesystem,
  **gitleaks** with a planted canary, and a guard that fails the build if a
  hosted-model SDK or endpoint ever appears in the source. The Trivy scans set
  `ignore-unfixed: true`, so a HIGH or CRITICAL CVE with no available fix does not
  block the build.
* **Grafana runs with anonymous Viewer access enabled**, which is safe only
  because port 3000 is bound to loopback: anyone who can reach it can query the
  whole Prometheus data source without credentials. The admin account
  (`GRAFANA_ADMIN_PASSWORD` from `.env`, which has no default) is still needed to
  change settings.
* Release bundles are checksummed and their images verified by manifest digest,
  but **nothing is signed**: there is no cosign signature and no SLSA provenance.

### The write API has no authentication

Stated plainly, because it is the one real hole in this design and the fix for it
is a deployment decision rather than a patch.

The API accepts writes — `POST /recommendations`, `POST /itineraries`,
`DELETE /itineraries/{id}`, `POST /reenrich`, `PATCH /records/{entity}/{id}`,
`POST /user-data/wipe` — and asks no caller for credentials. `POST /agent/ask`
and `POST /agent/itinerary` are unauthenticated too, and each costs a model
generation. Any client that can open a socket to port 8000 can queue a
correction, a recommendation or a batch of enrichment work, and nothing
downstream distinguishes those messages from the ingestor's.

**What protects it here is that nothing off this machine can open that socket.**
`${AOW_BIND_ADDR:-127.0.0.1}` binds both published ports to loopback. Under the
threat model that actually matches a take-home demo — one workstation, one
operator, the UI reaching the API over the internal network rather than over the
published port — a token in a `.env` file on the same disk would be security
theatre.

**What that model does not survive is a second user.** Before this is published
beyond the trusted host, the write path needs authentication against the
organisation's own identity provider, authorisation by role (the routes are not
equally dangerous), an authenticated principal on every accepted message, and
rate limiting on `POST /reenrich`. None of that is implemented, and a token check
or a UI-only guard was deliberately not implemented in its place, because either
would read as authentication without being it.

`AOW_BIND_ADDR` widens the binding, and widening it is the point at which this
system becomes multi-user without having become multi-user safe. To reach a stack
on a remote host, forward the ports over SSH instead:

```sh
ssh -L 8080:127.0.0.1:8080 -L 8000:127.0.0.1:8000 user@host
```

That borrows SSH's authentication, which is a real one, and leaves the ports
closed to everyone else. It borrows no authorisation: anyone who can open that
tunnel has every route.

---

## Known limitations

* **Single-replica broker and database.** Fine for this; not an HA design.
* **`/docs` is the one page that needs a network.** FastAPI's default Swagger UI
  loads from a CDN, and the API port carries no CSP. Use
  `GET /openapi.json` on an air-gapped host.
* **The places map is a bundled 20 km extract per city, not a tile service.** No
  residential streets, no buildings, no labels and no routing; the geometry is
  simplified, and there is nothing at all beyond 20 km of the city centre.
* **No marine data, and coastal scores are capped because of it.** Surfing,
  swimming, fishing and a boat ride are scored in coastal cities from wind,
  temperature and precipitation, never from wave height, swell period or water
  temperature. The score is capped at **69**, one point below the `good` band, so
  none of them is ever reported as a confident recommendation, and the reason
  travels in the row. No scoring rule may infer a sea state from a land
  measurement. Open-Meteo's keyless marine endpoint was probed rather than
  assumed: it answers for all four coast points, but forecasts 10 days against the
  16 stored, and from a model cell 2.9–13.2 km from the named point, so staging it
  would narrow this gap rather than close it. The measured numbers are recorded in
  `services/ingestor/providers.py`.
* **A coastal city's forecast point is not its coast.** Each coastal city names a
  real coast point in `data/cities.yml` and the consumer stores the great-circle
  distance to it: Tel Aviv 1.4 km, Reykjavík 2.6 km, Lisbon 17.8 km, Rome 24.7 km.
  Every answer carrying one of those scores names the point and the distance.
* **No surf spots, dive sites or boat hire on record.** The places snapshot names
  beaches and marinas, which is not the same claim, so "where can I surf in Tel
  Aviv?" is answered with an explicit "I do not have a verified surf spot", never
  with a beach.
* **Places come from Wikidata, not OpenStreetMap.** OSM is the better source and
  the code for it is still there (`--places-source osm`), but at staging time all
  four public Overpass mirrors were refusing connections or timing out. Wikidata
  holds notable venues, so the data skews to landmarks. Each row records its own
  source.
* **A user-entered activity is scored against general outdoor comfort**, not a
  rule tuned for it, and the answer says so.
* **The enricher polls** rather than binding to the weather stream. A deliberate
  trade: no second delivery branch means no silent partial fan-out.
* **Per-message accounting starts at an accepted outbox envelope.** Enrichment
  results created before the enricher had an outbox have no durable producer
  envelope to reconcile.
* **The ingestor drills accept through a test fixture, not a real fetch.** The
  connected fetch path is not exercised in CI, which has no egress.
* **A refresh moves the weather forward and nothing else.** `--refresh`,
  `make refresh` and the refresh container re-fetch the forecast. Places,
  background articles and events are not re-fetched by any of them: places and
  background come from the committed snapshot, and the 55 verified events are a
  hand-checked sample that is extended one row at a time. Rebuilding the
  snapshot is `make snapshot`, a maintainer step that rewrites files in the
  repository and expects the diff to be reviewed; re-checking the event
  listings is `scripts/event-recheck.sh`, also manual. So a long-running
  install keeps an accurate forecast while its events quietly expire, which is
  [the bargain described above](#the-data-on-board-and-when-it-goes-stale) and
  is visible in every as-of stamp.
* **No physical air-gap proof.** Offline operation is proven on a separate Docker
  engine with an empty image store, no pulls and no reachable egress, and by a
  per-release clean-engine CI gate. Neither is separate physical hardware, and
  neither closes this: a VM, a second Docker daemon, a CI runner and a firewall
  rule are all explicitly recorded as *not* closing it. The drill is prepared and
  its artifacts are built, but it has not been run — it needs a disposable second
  physical host. See
  [docs/EVIDENCE-physical-airgap.md](docs/EVIDENCE-physical-airgap.md), which
  states exactly what is still missing.

---

## Production path

**Not implemented. This section describes what would change, not what ships.**
There are no Kubernetes or OpenShift manifests, no Helm chart and no
`NetworkPolicy` in this repository. Compose is the demo vehicle because one
`docker compose up -d` on the reviewer's own laptop is the most honest way to
show that this works, and because it is the same stack on every OS. It is not
what I would deploy.

What a real deployment would use: the tested, scanned images published by digest
and mirrored into an internal registry (`skopeo copy`, or `oc mirror` on
OpenShift); the model as an OCI artefact or a PVC seeded by a Job, verified
against `models.lock` either way; a Helm chart with image digests as values, so a
rollback is a value change, and the migration as a `pre-install`/`pre-upgrade`
hook; a default-deny egress `NetworkPolicy` as the real version of
`internal: true`; Postgres and RabbitMQ via their operators, with PVCs and a
quorum of three; the outbox volumes as `ReadWriteOnce` PVCs with their producers
as StatefulSets; secrets in Vault or sealed secrets; healthchecks as readiness,
liveness and startup probes; and a `ServiceMonitor` scraped by the platform's own
Prometheus.

**The loopback binding would be replaced, not carried over**: a `Service` plus an
OIDC-authenticating `Ingress`/`Route`, with the per-route role check in the API.
Exposing this stack in a shared cluster without that would be strictly worse than
the demo.

That path needs a registry, a chart repository and cluster access to demonstrate,
none of which a take-home reviewer has. What is demonstrable here is the property
underneath it — digest pinning, checksum verification, and a runtime with no
route out — and that is what the proofs exercise.

---

## Requirements traceability

IDs are from [ASSIGNMENT.md](ASSIGNMENT.md), which decomposes the brief and
records which design choices are ours rather than the brief's. "Verify" is a
command you can run.

| ID | Requirement | Where it lives | Verify |
|---|---|---|---|
| M1 | Weather for five cities from an external API | `services/ingestor/providers.py` (Open-Meteo), `data/cities.yml` | `curl localhost:8000/coverage` |
| M2 | LLM recommendation: is the weather suitable for the activity | `common/rules.py` scores it, `services/enricher/` words it | `… demos reenrich`; the Suitability tab |
| M3 | Local open-weights LLM, no external API | `llm` (llama.cpp + Qwen3-1.7B); `common/llm.py` is the only client | `… demos offline` |
| M4 | All collected data → queue → database | outbox → `aow.events` → consumer, the only writer | `… demos no-data-loss`; the `psql` grants |
| M5 | Containerized, one uniform way to run | `compose.yml`; `compose.tools.yml` stages it and runs the proofs | [Setup](#setup), ending in `docker compose up -d` |
| M6 | Runs on-prem without full internet | `backend` is `internal: true`; the committed snapshot | `… demos offline`, ideally with the host NIC down; the air-gap scope, the prepared artifacts and what is still open are recorded in [docs/EVIDENCE-physical-airgap.md](docs/EVIDENCE-physical-airgap.md) |
| M7 | Agent answering varied questions from stored data | `services/agent/` | `… demos questions` |
| M8 | Tourism: history, places, sports events | the `facts`, `places` and `events` tables | `… demos questions` |
| M9 | Itinerary for chosen destinations | `POST /agent/itinerary`, the Trip planner tab | build, save and reopen a plan in the UI |
| M10 | Good data visualization | forecast chart, suitability heatmap, offline places map, coverage banner | the Forecast, Suitability and Places map tabs |
| M11 | Temporary failures without data loss | outbox, confirms, ack-after-commit, DLQ and redrive, reconciliation | `… demos no-data-loss`; the CI gate in `scripts/ci-integration.sh` |
| M12 | Update stored information | `PATCH /records/...`, the operator refresh, re-enrichment | `… demos update`; `make refresh-check` |
| S1 | Repo with code, config, CI/CD, README | `.github/workflows/ci.yml` and `release.yml`; release tooling in `scripts/`, including `airgap-evidence.sh` (captures engine identity, image/volume census, link state, bundle digests and exit codes) and `make-fault-injection-bundle.sh` (derives the deliberately-broken artifact for the rollback drill) | `gh run list`; [docs/RELEASE.md](docs/RELEASE.md) |
| S2 | README: startup, architecture, choices and reasoning | this file | you are reading it |
| B1 | Full tests for all components | **partial** — offline unit and Compose integration tests run in CI, with a real browser gate on each PR and a real-model grounding gate for release candidates; component and end-to-end depth remains incomplete | `docker run --rm aow/tests:dev`; [CI/CD evidence](docs/CICD_EVIDENCE.md) |
| B2 | LLM observability metrics | **done** — Prometheus scrapes request/error/latency series from every service plus llama.cpp's own `--metrics`; 11 alert rules and three provisioned Grafana dashboards, all offline | `make monitor`, then Grafana at <http://127.0.0.1:3000> |
| B3 | Automatic recovery from failures | **partial** — reconnect with backoff, `restart: unless-stopped`, healthchecks, automatic re-enrichment, and an operator backup/restore with a measured RPO and RTO | `make backup-restore`; then `… demos no-data-loss` |

---

## Data sources and licences

| data | source | licence |
|---|---|---|
| Weather | [Open-Meteo](https://open-meteo.com/) | CC BY 4.0 |
| Places | Wikidata (default) or OpenStreetMap via Overpass | CC0 / ODbL © OpenStreetMap contributors |
| Map backdrop | OpenStreetMap via Overpass, a 20 km extract per city (1.2 MB for all five) | ODbL © OpenStreetMap contributors |
| Map fallback shoreline | [Natural Earth 1:10m](https://www.naturalearthdata.com/downloads/10m-physical-vectors/) | public domain |
| Background articles | Wikipedia REST summaries | CC BY-SA 4.0 |
| Events (verified) | venue and organiser listings, hand-verified | see each row's `source_url` |
| Events (samples) | generated from the places snapshot | n/a |

Weather, places and background articles are fetched by
`services/ingestor/fetch_content.py` and committed to `data/snapshot/`; that is
what `make snapshot` rebuilds. The map backdrop is fetched separately by
`fetch_basemap.py` into `data/map/` and is not part of `make snapshot`. The
verified events live in `data/events.seed.jsonl` and ship as
`data/snapshot/events.jsonl` (55). The samples are 45 rows generated by
`services/ingestor/make_samples.py`, every row `is_sample` and titled
*Sample: …*; they ship as `data/snapshot/events.samples.jsonl` (45) and are
stored in demo mode only.

## Further reading

| document | what is in it |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | the full architecture guide: diagrams, message lifecycle, user and infrastructure flows |
| [docs/REVIEWER-QUICKSTART.md](docs/REVIEWER-QUICKSTART.md) | the shortest path from a clean machine to a running stack, and exactly which data is fetched and which is a committed snapshot |
| [docs/RELEASE.md](docs/RELEASE.md) | building, verifying and installing the offline bundle |
| [docs/RELEASE-PROOF.md](docs/RELEASE-PROOF.md) | what was actually run for staging, transport, install, upgrade and rollback, and what remains unproven |
| [docs/EVIDENCE-physical-airgap.md](docs/EVIDENCE-physical-airgap.md) | the physical air-gap proof: what was prepared and measured, and what is still missing to run it (status: open) |
| [docs/RUNBOOK-BACKUP-RESTORE.md](docs/RUNBOOK-BACKUP-RESTORE.md) | operator backup and restore, with the measured RPO and RTO |
| [docs/CICD_EVIDENCE.md](docs/CICD_EVIDENCE.md) | the CI/CD evidence matrix: which gate proves which claim |
| [docs/EVIDENCE-observability-and-recovery.md](docs/EVIDENCE-observability-and-recovery.md) | executed evidence for the metrics stack and for backup and restore |
| [docs/EVIDENCE-f9-events-and-coastal.md](docs/EVIDENCE-f9-events-and-coastal.md) | event validity mechanics and the sea-state claims |
| [ASSIGNMENT.md](ASSIGNMENT.md) | the brief, the requirement IDs, and which choices are ours |
| [TECHNICAL_DECISIONS.md](TECHNICAL_DECISIONS.md) | the decision record behind the choices above |
