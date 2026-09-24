# act-on-weather

Weather for five cities, turned into activity recommendations by a **local**
open-weights model, with every collected record travelling through a queue into
Postgres — and the whole thing running with **no internet at all** once it has
been staged.

For example, ask it about the weather in Rome on **2026-09-24**. Its answer
includes the weather source and the time the forecast was collected. The
bundled forecast was collected on **2026-09-23** and covers **2026-09-23 to
2026-10-08**; it does not update itself during a normal run. Ask about a date
outside that window and the system says it has no forecast instead of guessing.

---

## Quick start

**The only thing you need on the host is Docker** — Docker Desktop on Windows
or macOS, Docker Engine with the Compose plugin on Linux. No `make`, no `curl`,
no `python`, nothing to install beyond that. Give Docker at least **8 GB of
memory** and a few GB of free disk, and start it before you begin.

Everything below is one recipe. The commands are character-identical on
Windows, macOS and Linux except the single line that creates `.env`. Run them
in a terminal opened in this project's folder.

### 1. Settings

On Linux and macOS:

```sh
cp .env.example .env
```

On Windows, in PowerShell:

```powershell
Copy-Item .env.example .env
```

Then open `.env` in any editor, replace **every** `change-me` with a different
password, and save. If `.env` already exists, skip the copy and keep it. The
file is gitignored; keep it private.

### 2. Stage it — once, with internet

```sh
docker compose pull postgres rabbitmq llm edge
docker compose -f compose.tools.yml run --rm stage
docker compose build
docker compose -f compose.tools.yml build demos
```

| | |
|---|---|
| `pull` | the four upstream images (~2.2 GB), every one pinned by digest |
| `run --rm stage` | downloads the model (~1.2 GB) into `models/` and checks it against **`models.lock`** — the only place in this repository where its checksum is written. A model that is already staged is re-verified rather than re-fetched, so running this twice is safe and quick. |
| `build` | the five Python services and the UI |
| `build demos` | the proof runner, built now, while there is still a network, because the proofs have to work after it goes away |

To stage from an internal mirror rather than the public internet, set
`MODEL_BASE_URL` in `.env`. The checksum check is identical either way.

### 3. Run it — no internet needed

```sh
docker compose up -d
```

### Open and stop the app

Open **<http://localhost:8080>**. The API documentation is at
<http://localhost:8000/docs>. Both are published on **loopback only** — this
machine can reach them and nothing else on the network can, because the API
accepts writes and authenticates nobody. [Security](#security) says what would
have to be true before that changes, and how to reach the stack from another
machine without changing it. The binding is IPv4 loopback, so on the rare host
whose browser resolves `localhost` to `::1` and does not fall back, use
**<http://127.0.0.1:8080>** instead. The first start takes several minutes while the
model loads and the saved data enters the database. Check progress with
`docker compose ps`; an exited `migrate` container with code 0 is normal. If
the page does not load after a few minutes, run `docker compose logs --tail 50`
to see the startup messages.

Stop it with `docker compose down`, and start it again later with
`docker compose up -d`. Both keep the database and queue data. Do not use
`docker compose down -v` unless you mean to delete those volumes.

### Shorthand

`make` is a convenience for hosts that have it, and nothing requires it. Each
target is the Docker command next to it, and the Docker command is what works
everywhere:

| shorthand | what it actually runs |
|---|---|
| `make stage` | the four staging commands above |
| `make up` / `make down` | `docker compose up -d` / `docker compose down` |
| `make up-demo` | `docker compose -f compose.yml -f compose.demo.yml up -d` |
| `make test` | `docker build -q -f tests/Dockerfile -t aow/tests:dev .` then `docker run --rm aow/tests:dev` |
| `make grounding` | the pre-release model gate — see [The grounding gate](#the-grounding-gate-before-a-release) |
| `make offline` | `docker compose -f compose.tools.yml run --rm demos offline` |
| `make questions` | `… run --rm demos questions` |
| `make no-data-loss` | `… run --rm demos no-data-loss` |
| `make update` | `… run --rm demos update` |
| `make reenrich` | `… run --rm demos reenrich` |
| `make demo` | `… run --rm demos all` |
| `make refresh` | `docker compose -f compose.tools.yml run --rm refresh` |
| `make refresh-check` | `docker compose -f compose.tools.yml run --rm refresh --check` |
| `make snapshot` | `docker compose -f compose.yml -f compose.connected.yml run --rm --no-deps ingestor python -m services.ingestor.fetch_content` |
| `make manifest` | `docker run --rm -v "$PWD:/work" -w /work python:3.12-slim@sha256:… python scripts/snapshot_manifest.py` |

Both forms run the same scripts from this same working tree — `demos/*.sh` is
one implementation, and the container is only a shell to run it in. The stack
must already be up; the proofs reach it over the internal network, through the
same `edge` proxy the browser uses.

**The `demos` container mounts the Docker socket**, which is root-equivalent
access to the host's Docker daemon. That is deliberate and it is the point: the
drills stop the consumer, stop the broker and restart the model server, so
driving Docker *is* the proof. It is why the proof runner is a `docker compose
run` you type on purpose and never part of `up`, and why it lives in
`compose.tools.yml` rather than in `compose.yml`. Nothing in the running stack
has the socket. If you would rather not grant it, run the same scripts directly
on a host with bash: `bash demos/01_offline.sh`.

### Demo mode

A default run stores the **26 hand-verified events** — 11 London, 6 Reykjavík,
4 Rome, 4 Lisbon, 1 Tel Aviv — and answers "none on record" for any city, date
or category the feed does not cover. To see the trip planner and the agent
working with a denser event calendar, switch to **demo mode**:

```sh
docker compose -f compose.yml -f compose.demo.yml up -d
```

Demo mode adds 45 **generated sample events**, labelled wherever they appear.
The UI also shows a demo banner. Run `docker compose up -d` to return to
verified events only; the generated events are removed from the database. The
section
[Events: verified, and generated](#events-verified-and-generated) says exactly
what they are and why they exist.

The saved data is included in `data/snapshot/`. Once staging has downloaded the
images and the model, a normal run needs no internet. A fresh clone alone is
**not** ready for offline use, because the images and the model are not in Git.

---

## What it does

| | |
|---|---|
| **Collects** | 16-day daily forecasts for Rome, London, Lisbon, Tel Aviv and Reykjavík; 620 places, 81 background articles, and **26 verified events** (+ 45 labelled samples in demo mode) |
| **Decides** | a deterministic suitability score per (city, day, activity) across **18 activities**, from rules in `data/activities.yml` |
| **Words** | a local Qwen3-1.7B writes one or two sentences about each score |
| **Answers** | an agent resolves the question in code and answers from stored rows only |
| **Plans** | a day-by-day itinerary assembled from rows that actually exist |
| **Updates** | corrections and refreshes, both travelling through the queue |

### Tabs

Seven, across the top of the page. Each one draws under the coverage strip in the
header, which carries the as-of stamp and the forecast window.

* **Forecast** — temperature and rainfall per city, with the stored rows behind it.
* **Suitability** — a city × day × activity heatmap over all 18 activities, plus
  a box to ask about *any* activity you type, not just the ones with rules.
* **Trip planner** — a day-by-day plan with three suggestions per day, weighted
  by the interests and activities you pick. See
  [Choosing a day's activity](#choosing-a-days-activity). A saved plan is stored
  as a row like any other, so the list underneath reopens one with its days as
  they were saved — and says so when the forecast has moved on since.
* **Places map** — filter stored places by city, category or name; inspect their
  coordinates and source links, and highlight stops from the current itinerary.
  Streets, water and parks are staged with the coastline and bundled locally,
  so pan and zoom work without map tiles.
* **Ask the agent** — chat, with a panel showing exactly which rows the answer used.
* **Update data** — all three M12 update paths in one place: the operator
  refresh (per-city freshness and the command that changes it; the page
  states that it does not fetch), correcting a stored record, and re-wording
  with the local model.
* **Data coverage** — what is held, per record type and per city, which rows are
  labelled samples, and how far the wording queue has got.

The UI is Streamlit. Its own toolbar is switched off in
`services/ui/.streamlit/config.toml` (`toolbarMode = "minimal"`): that toolbar's
"Deploy" button offers to publish the app to Streamlit Community Cloud, which is
not ours, does nothing useful here, and in an air-gapped demo reads as an offer
to deploy this stack.

The styling (`services/ui/theme.py`) is one inline stylesheet — no CSS file to
serve, no webfont, no icon set, no CDN, because the runtime rule is zero network
calls. Icons are unicode glyphs from `data/activities.yml`. If the stylesheet
failed to apply, every number and timestamp would still be on the page.

### Choosing a day's activity

The rule engine gives every (city, day, activity) a score, and that score is the
source of truth everywhere — the heatmap, the API and the agent all report it
unmodified. The trip planner applies two adjustments **on top of it**, purely to
order a day's suggestions (`services/agent/planning.py`):

* an activity matching a stated interest gains `INTEREST_BONUS` (15)
* an activity already used on an earlier day loses `REPEAT_PENALTY` (18) per use

Both are small enough that the weather still decides: ticking "museums" does not
beat a perfect beach day. The repeat penalty exists because without it a warm,
dry city returns the same answer every day — a week in Tel Aviv in September
came back as "a day at the beach" seven times, since it scores 100 on all seven.
The **Vary the plan across days** toggle turns the penalty off, so the raw
ranking can be seen side by side. Both modes are deterministic: the same request
rebuilds the same plan.

### Activities that are not scored everywhere

Five activities — surfing, swimming, the beach, fishing and a boat ride — carry
`requires_coast` in `data/activities.yml` and are only scored for cities marked
`coastal` in `data/cities.yml`. London is not, so it has 13 activities scored
rather than 18, and **no surfing row at all**. A score for surf derived from an
inland forecast is a number the system cannot stand behind.

The agent enforces the same boundary. It matches the activity a question names
against the `keywords` in `data/activities.yml` and renders the stored daily
scores directly. An unscored activity is reported as having no score on record;
for coastal activities in inland cities, it also says why. This avoids a
misleading overall verdict from the small model: in a live seven-day running
question, it called a week with seven `fair` scores a “good week.” Open-ended
questions still use the local model to phrase the retrieved data.

### “Where can I surf?” is a different question from “is it good for surfing?”

A question naming a place word — `where`, `nearest`, `which beach` — is routed
to locations, not to the forecast. It is answered from the same `place_categories`
rule the trip planner uses: an activity is located only where the source's own
class **is** the venue, so “where can I go to the beach in Tel Aviv?” names the
five stored beaches, and “where can I surf in Tel Aviv?” answers

> I do not have a verified surf spot for Tel Aviv: no source I hold records
> where to do it.

Wikidata `Q40080` and OSM `natural=beach` assert that a beach is there. They do
not assert that the surf is rideable, the water lifeguarded, the angling
permitted or a boat for hire — so surfing, swimming, fishing and boat rides name
no venue, and no beach is offered in place of one.

A bare `where` question fetches **no weather at all**, so it cannot be answered
with a week of scores, and it still answers after the stored forecast window has
run out. Add a date or ask about conditions (“where **and when** can I surf?”)
and the scores appear below the location, under a line saying what they are:
they rate the stored forecast, and nothing in the data measures the waves.

*Known limitation:* no source in the snapshot records surf breaks, dive sites or
boat hire. Adding one — a sourced surf-spot layer with the same `source` and
`as_of` as every other row — is what would turn that refusal into a location.

### What gets worded, and what does not

Scoring 18 activities for 5 cities over 16 days is ~1,300 rows and costs
microseconds. Asking a CPU-bound 1.7B model to write a sentence about each of
them is ~1,300 generations through a slot the agent also shares — hours.

So the consumer ranks each city-day and marks only the top `ENRICH_TOP_N`
(default 6) as `pending`; the rest are stored `deferred`. A deferred row is
scored, charted and answerable — it was simply never queued for prose. Asking
about that activity by name promotes it back to `pending`, and so does the
**Re-word with the model** control on the Update tab. The UI says which is
which rather than showing a blank explanation.

The `llm` service runs with `--parallel 2` for the same reason: with one slot an
interactive question queued behind an enrichment batch and timed out as though
the model were down.

---

## Architecture

**Start with the [architecture guide](docs/ARCHITECTURE.md).** It has the
GitHub-rendered system diagram, message lifecycle, user and infrastructure
flows, technology choices, and the limits to explain to a reviewer. Its
Mermaid diagrams live in Git as editable text.

Eleven containers. `postgres`, `rabbitmq`, `migrate` (one-shot), `llm`,
`ingestor`, `consumer`, `enricher`, `api`, `agent`, `ui`, `edge`.

### The data flow, exactly

1. The **ingestor** reads the committed snapshot (or fetches live, when
   attached to `egress`) and **fsyncs each record into a local SQLite outbox**
   on a named volume. *That write is the acceptance point.*
2. A publisher loop drains the outbox to RabbitMQ under **publisher confirms**
   with `mandatory=True`. A record is marked published only once the broker
   confirms it.
3. The **consumer** — the only role in the system holding write grants —
   validates the payload, then writes the business row **and** the
   `message_id` into `ingest_log` in **one transaction**, and acks only after
   that commits.
4. Storing weather also computes and stores the **rule-based score** for each
   supported activity immediately. The top `ENRICH_TOP_N` per city-day are
   `pending` for model wording; the rest are scored and `deferred`.
5. The **enricher** polls pending rows, makes **one** grammar-constrained call
   to the local model, fsyncs the result into its own outbox, and publishes it
   back into the exchange. Its original `message_id` survives broker failure.
6. The **api**, **agent**, and **enricher** read as `aow_reader`, which holds
   `SELECT` and nothing else.

### Networks

| network | who is on it | why |
|---|---|---|
| `backend` | everything | `internal: true` — Docker itself gives it no gateway |
| `frontend` | `edge` only | Docker cannot publish a port from an internal network, so exactly one container straddles the boundary |
| `egress` | nobody, by default | the operator refresh attaches the ingestor to it for the length of one fetch, then detaches it again and asserts it detached |

Only 8080 and 8000 are published, and both only on `127.0.0.1`. Not the
database, not the broker, not the management UI, not the model server.

The loopback part is not incidental. Docker's short form, `8000:8000`, binds
`0.0.0.0` — on any host with a LAN address that publishes the API to every
client that can route to it, and the API's write routes ask for no credentials.
So the binding is spelled out: `${AOW_BIND_ADDR:-127.0.0.1}:8000:8000`. The UI
is bound the same way, because it reaches those write routes through `api` and
an open 8080 would be the same hole with one more hop in front of it.
`tests/unit/test_compose_ports.py` holds this over every `compose*.yml`, so a
regression to the short form fails the build rather than the reviewer's
network.

---

## Delivery guarantees, and their boundary

**The guarantee.** From the moment a record is fsynced into a producer's outbox
on a named volume, it reaches one of exactly two terminal states:

* **stored** in Postgres, or
* **quarantined** in `aow.dlq`, visible and redrivable.

Consumer, broker and database outages *delay* a record. None of them lose it.

**How.** At-least-once delivery plus idempotent writes, which together give
effectively-once storage. The idempotency key is `message_id`, inserted into
`ingest_log` in the same transaction as the business write. Concretely:

| failure | what happens |
|---|---|
| consumer crashes **before** commit | nothing was written; the broker redelivers |
| consumer crashes **after** commit, **before** ack | the broker redelivers; `ingest_log`'s primary key rejects it; acked without a second write |
| database unreachable | the handler raises, the message is requeued, the consumer waits and reconnects |
| broker unreachable | publishing stops, **accepting does not**; records accumulate on the producer's volume and replay |
| poison message | fails validation before any write, dead-letters to `aow.dlq`, does not block the queue |
| model down or slow | irrelevant to weather: the score is already stored, only the wording waits |

**What is *not* covered**, stated plainly: a destroyed volume, a full disk, and
data that was never accepted in the first place. Weather that was never fetched
can be re-fetched while connected — `make refresh`. There is no absolute
guarantee here and the code does not pretend otherwise.

**Proof, not assertion:**

```sh
docker compose -f compose.tools.yml run --rm demos no-data-loss
```

Four drills — consumer down, database down, broker down and poison message. Each follows a
**single accepted `message_id`** to its terminal state. Row counts are
deliberately not the assertion: a loss and a duplicate cancel out in a count.

**The same guarantee is a CI gate.** `scripts/ci-integration.sh` runs against a
real broker and database in a throwaway Compose project, and no image is
published unless it passes. It accepts one record per failure mode — consumer
stopped, broker stopped, database stopped — each with its own `message_id`,
then restarts every service in the path and asks a **separate reader
connection** for all of them at once. Any single missing ID fails the job, and
so does any duplicate.

The database drill waits for the consumer to log a failed delivery before
restoring Postgres. That wait is the drill: if the database comes back before
the delivery arrives, the consumer never drops its connection, never takes the
reconnect path, and the drill passes against a broken build. Verified by
rebuilding the service image with the reconnect fix removed — the gate then
fails naming the lost ID, while the consumer-down and broker-down drills still
pass, because neither touches that path.

### Reconcile accepted records after an incident

Run this after an outage, and also after upgrading from a build that had the
database reconnect defect — a record acknowledged by that build could be absent
from Postgres with nothing to show for it, and this audit is what finds it.

Keep the Postgres, RabbitMQ, and all three outbox volumes. After the services
recover, run this read-only audit in **each** producer container:

```sh
docker compose exec -T api python -m services.common.reconcile
docker compose exec -T ingestor python -m services.common.reconcile
docker compose exec -T enricher python -m services.common.reconcile
```

Each JSON report lists confirmed outbox IDs absent from committed `ingest_log`.
An ID may still be in `aow.ingest` or `aow.dlq`; inspect those queues and the
consumer logs before replaying it. A queued ID is safe to replay because the
consumer's `ingest_log` primary key permits only one business write. Investigate
a poison ID in the DLQ before any redrive; if its DLQ copy is gone but its
outbox envelope remains, replaying that original ID will quarantine it again.
Normal unpublished rows are handled by the producer loop and are outside this
audit.

Replay a selected missing ID from the producer that accepted it:

```sh
docker compose exec -T api python -m services.common.reconcile --replay --id MESSAGE_ID
```

Use `ingestor` or `enricher` in place of `api` for their IDs. The command
queries Postgres through a separate reader connection, skips an ID already in
`ingest_log`, and republishes the **original envelope and ID** under a RabbitMQ
publisher confirm. A replay confirmation means the broker accepted it; wait for
the consumer, then rerun the audit or query `ingest_log` to verify exactly one
row. If publishing fails, rerun the command after RabbitMQ recovers. A database
commit racing the query is also harmless because the consumer's write is
idempotent. The command requires `--id` for replay to make each recovery choice
explicit. Do not delete an outbox volume until every accepted ID has a verified
terminal state or has been recorded as unrecoverable.

For an existing `aow` stack still running an older image, the same command can
run from a separate helper container without recreating any service. Build the
helper from this checkout, then mount the relevant producer volume read-only:

```sh
docker build -f services/Dockerfile -t aow/services:reconcile .
docker run --rm --network aow_backend --env-file .env -v aow_api_outbox:/outbox:ro aow/services:reconcile python -m services.common.reconcile --id MESSAGE_ID
```

Add `--replay` before `--id` after checking the audit result. Substitute
`aow_ingestor_outbox` or `aow_enricher_outbox` for their IDs; for a custom
Compose project, substitute its network and volume prefix. This helper uses
the existing broker and database while leaving the running services alone.

Older enricher results created before its outbox was added have no durable
producer envelope to reconcile. If the source recommendation is still pending,
the enricher can generate a new result, with a new ID; its old ID cannot be
reconstructed. Likewise, an accepted ID whose outbox volume was destroyed and
which is absent from both Postgres and RabbitMQ cannot be replayed by this tool.

---

## Technical choices, and what was rejected

| choice | why | rejected |
|---|---|---|
| **Open-Meteo** | no API key, so the air-gapped bundle has no secret to carry and no account to expire; the free tier includes the 16-day daily forecast that "this week" needs | **OpenWeather** — a key to manage, and its free tier splits the forecast horizon awkwardly |
| **RabbitMQ**, quorum queues | per-message ack after DB commit, a real DLX, and `x-delivery-limit` are exactly the primitives M11 needs, with the smallest operational surface | **Kafka/Redpanda** — offset-based, so per-message quarantine needs machinery; heavier for one node. **Redis Streams** — durability story is weaker and harder to defend |
| **Outbox before the broker** | publisher confirms alone cannot help if the broker is *unreachable*. The outbox is what makes "the broker is down" a delay rather than a loss | **confirms only** — leaves a window where an accepted record exists nowhere durable |
| **Deterministic score, model phrases it** | the verdict is reproducible, testable and defensible; a model outage degrades the wording, never the content | **LLM decides suitability** — unrepeatable, untestable, and it would put a 1.7B model on the critical path |
| **Router-first agent** | city, dates and coverage resolved in readable code; at most one model call to phrase retrieved rows. E1 answers in ~5 s | **model tool-calling** — 3–4 sequential generations on CPU (30–90 s), non-deterministic in front of a reviewer, and silent when it goes wrong |
| **Qwen3-1.7B Q4_K_M** | runs on CPU at ~1.2 s per call, Apache-2.0, 1.2 GB on disk, reliable under a JSON-schema grammar | a 3–4 B model — better prose, but 3–4× the latency on the reviewer's likely CPU-only machine |
| **Streamlit** | seven working tabs in the time a hand-written SPA would take to scaffold, and it bundles its own assets, so it works offline | a React SPA — more polish, but the brief weighs the pipeline more heavily |
| **Docker Compose** | one command, identical on Linux, macOS and Windows | Kubernetes — the production path, described below, not the demo path |

**Thinking mode is disabled on the model** (`enable_thinking: false`). Left on,
Qwen3 spends its whole token budget in `reasoning_content` and returns empty
content: measured 4.2 s and no answer, versus 1.2 s and a clean one.

---

## Offline operation, and its limits

```sh
docker compose -f compose.tools.yml run --rm demos offline
```

That script proves four things structurally — `aow_backend` really is
`internal`, no service can open an outbound connection, no hosted-model SDK or
endpoint exists anywhere in the source, and the model file is loaded from a
local bind mount — and then answers both of the brief's example questions plus
one deliberately out-of-range question.

**The real proof is behavioural: disable the host's network adapter and run it
again.** Everything above still passes.

**What offline cannot do**, and what happens instead:

| | |
|---|---|
| Fetch new weather | the coverage window stops moving; questions beyond it are **refused**, not guessed |
| Discover new places or events | the system says it has no record, rather than inventing one |
| Correct a stored record | **works offline** — it is a local write through the local queue |
| Re-word recommendations | **works offline** — the model is local |

When the snapshot goes stale, use the connected refresh below to re-fetch the
forecast and move the window forward. Until then, every answer and every chart
carries the as-of stamp that says how old it is.

---

## Data sources and licences

| data | source | licence | how |
|---|---|---|---|
| Weather | [Open-Meteo](https://open-meteo.com/) | CC BY 4.0 | fetched by `services/ingestor/fetch_content.py`, committed to `data/snapshot/weather.jsonl` |
| Places | **Wikidata** (default) or OpenStreetMap via Overpass | CC0 / ODbL © OpenStreetMap contributors | same script; every row keeps its own source URL |
| Map backdrop | OpenStreetMap via Overpass | ODbL © OpenStreetMap contributors | a 20 km extract per city — streets, water, coastline, parks — staged by `services/ingestor/fetch_basemap.py` into `data/map/<city>.basemap.geojson.gz`; 1.2 MB for all five |
| Map fallback shoreline | [Natural Earth 1:10m](https://www.naturalearthdata.com/downloads/10m-physical-vectors/) | public domain | bundled in `data/map/`; drawn only for a city with no staged extract. Source revision and checksum in `data/map/SOURCE.md` |
| Background | Wikipedia REST summaries | CC BY-SA 4.0 | same script; the city article plus one article per venue, resolved through its Wikidata sitelink |
| Events (verified) | venue listings | see each row's `source_url` | **hand-verified**, in `data/events.seed.jsonl`. 26 rows across all five cities (london 11, reykjavik 6, rome 4, lisbon 4, tel-aviv 1). **The only events a default run stores.** |
| Events (generated samples) | generated from the places snapshot | n/a | `data/events.samples.jsonl`, every row `is_sample` and titled *Sample: …*. **Demo mode only** (`make up-demo`). |

**Which day an event is on.** Instants are stored as `timestamptz` and never
rewritten, but the *day* an event belongs to is its day in the city, resolved
through that city's IANA zone — not UTC, and not the database session's zone.
The Laver Cup starts at `2026-09-25T00:00+01:00`, which is `2026-09-24T23:00Z`;
in London it is on the 25th, and "any sports events tomorrow?" asked on the
24th has to find it. A multi-day event is on **every** day it runs, so the
tournament appears on the 25th, 26th and 27th of an itinerary, labelled *day n
of 3*. The active window is half-open, `[starts_at, ends_at)`: one billed as
ending at local midnight ends the previous day rather than opening the next.
`GET /events?start=&end=` takes local dates and matches on overlap, and every
row carries `timezone`, `starts_on` and `ends_on` so nothing downstream
re-derives the day. Proved in `tests/integration/event_local_days.py` (real
Postgres, real tzdata) and `tests/unit/test_event_days.py`.

**Why Wikidata and not OpenStreetMap for places.** OSM is the better source and
the code for it is still there (`--places-source osm`). It is not the default
because at staging time all four public Overpass mirrors were either refusing
connections or timing out — reproducibly, over an hour. Wikidata's SPARQL
endpoint answered in seconds. The trade is real and visible in the data:
Wikidata holds *notable* venues, so you get the Royal Academy of Music Museum
and Harrods, not every café on the street. Each row records which source it
came from, and the agent's footer names it, so nothing here is guesswork about
provenance.

**Beaches, and what a beach row does not prove.** The first vocabulary had no
coastal category at all, which produced the one gap a reviewer noticed
unaided: Tel Aviv scored *a day at the beach* at 100 on every day of the
forecast and could not name a single beach to spend it on, because the planner
fell back to whatever matched the traveller's interests — parks. Wikidata
`Q40080` (beach) and `Q721207` (marina), and OSM `natural=beach`,
`leisure=beach_resort` and `leisure=marina`, now collect them. What that yields
at the staged 4 km radius, verified against both live endpoints:

| city | beaches | marinas | note |
|---|---|---|---|
| Tel Aviv | 5 | 1 | Bugrashov, Frishman, Hilton, Jerusalem, Metzitzim |
| Reykjavík | 1 | 0 | Kirkjusandur |
| Rome | 0 | 0 | Lido di Ostia is ~25 km out, well outside the radius |
| Lisbon | 0 | 0 | Carcavelos and Caparica are likewise out of range |
| London | 0 | 0 | inland, and gated as such |

Rome and Lisbon are the honest case: both are `coastal: true`, both still score
the coastal activities, and neither can name a venue for them. The planner says
so rather than offering a park.

**A beach is evidence of a beach and nothing else.** Only *a day at the beach*
claims `place_categories: [beach]`. Surfing, swimming, fishing and a boat ride
deliberately claim no venue, because `Q40080` and `natural=beach` assert that a
beach is there — not that the surf is rideable, the swimming supervised, the
angling permitted or a boat available to hire. Naming Gordon Beach as a surf
spot would be exactly the kind of invention the rest of this system is built to
avoid. If evidence-bearing rows arrive later (`supervised=yes`, a surf-break
class, a marina with hire), `data/activities.yml` is where that decision gets
revisited, and `tests/unit/test_planner_venues.py` is the test that has to be
changed on purpose.

**Why the background data is tied to the places.** The first attempt used
Wikipedia's geosearch, which is geographically correct and editorially useless:
within 6km of a city centre it returns administrative divisions ("Province of
Rome"), list articles, and — for Tel Aviv — a run of articles about shootings
and bombings. All true, none of it background for a trip planner, and a keyword
blocklist over article titles is a guess dressed up as a filter. Resolving the
Wikidata sitelink for each venue already in `places.jsonl` instead means every
landmark article is about somewhere the planner can actually send you, selected
by its Wikidata P31 class rather than by its distance from a point.

### Events: verified, and generated

**Nothing is passed off as real.** There is no free, licensable,
offline-stageable feed of concerts and fixtures for five cities, and fabricating
them was not an option. So there are two event files, kept apart at every layer:

* `data/events.seed.jsonl` — 26 **real** listings, each checked by hand
  against its own source URL. `is_sample: false`. These are the only events the
  system claims are real. Every city has at least one, but the depth is uneven
  (london 11, reykjavik 6, rome 4, lisbon 4, tel-aviv 1), because that is how
  far hand-verification got. **They are what a default run stores.**
* `data/events.samples.jsonl` — 45 rows generated by
  `services/ingestor/make_samples.py`. Every row is `is_sample: true`, its title
  begins with **"Sample:"**, and its `source` says in words that it is not a
  real listing. The venue in each row is real — it comes from the places
  snapshot — and its URL is that venue's own record, rendered as a **venue
  reference** rather than as the event's source, because there is no listing to
  point at. Generation is deterministic, so the committed file is reproducible.

They exist because with events in one city out of five, the planner and the
agent cannot be exercised anywhere else, and a reviewer cannot see how a sourced
event and a generated one are told apart — which is the interesting part.

**They are opt-in, and they do not linger.** Three separate places enforce that,
because "no fabricated row is in the database" is a claim worth more than one
check:

| | |
|---|---|
| **Snapshot** | two files, never merged: `data/snapshot/events.jsonl` (7) and `data/snapshot/events.samples.jsonl` (45) |
| **Ingestor** | replays the sample file only when `AOW_DEMO_EVENTS` is set, so a default run never even *accepts* a generated row |
| **Consumer** | the only role with write grants, so it is the last word: it drops a sample row that arrives while demo mode is off, whoever produced it, and on startup it deletes every `is_sample` row it finds |

The consumer's startup sweep is the part that matters: `AOW_DEMO_EVENTS`
describes the **database**, not just the run. Run `make up-demo`, look around,
then `make up` — the samples are gone, not merely hidden. Demo mode is also
visible while you are in it: the UI carries a banner naming the count and
saying which rows are real.

The loader drops any row in the sample file that does not admit to being a
sample, so the labelling is checked at the boundary rather than assumed. In demo
mode the samples are marked in the UI, in the agent's prompt, in its footer, and
counted separately in the coverage tab — which reports **"26 verified + 45
samples"**, never a single total of 71.

A city with no events on record produces "none on record", never a
plausible-sounding invention.

Re-fetch the whole snapshot with `make snapshot`, review the diff, and commit it.

---

## Updating stored information (M12)

Three paths, two of which work offline:

1. **A correction** — `PATCH /records/{entity}/{id}` → 202 + a `message_id` →
   queue → consumer → `revision + 1` and a `record_history` row. Works offline.
   `docker compose -f compose.tools.yml run --rm demos update` demonstrates it
   end to end.
2. **A connected refresh** — re-fetches the forecast and moves the coverage
   window forward. Needs an internet connection.
3. **Re-enrichment** — `POST /reenrich` → 202 → queue → consumer flips the
   selected recommendations back to `pending`, and the enricher rewords them.
   Scores are untouched: they are the rule engine's output, and only a weather
   refresh changes them. Set `include_deferred` to pull in the activities the
   consumer ranked out of the wording queue. Works offline. This also happens
   on its own whenever a refresh changes the weather behind a row.
   `docker compose -f compose.tools.yml run --rm demos reenrich` demonstrates
   it, including a full model outage.

All three are on the **Update data** tab in the UI, which is where a reviewer
should look first: it names each path, says which work air-gapped, and shows
the exact command for the one that cannot.

### The operator refresh

One command, on Windows PowerShell, macOS Terminal or Linux, while connected:

```sh
docker compose -f compose.tools.yml run --rm refresh
```

`make refresh` is the shorthand. It runs [`scripts/refresh.sh`](scripts/refresh.sh),
which:

1. records what is stored now, per city — as-of and last day covered;
2. attaches **only the ingestor container** to the `egress` network;
3. runs the fetch inside it, into the same outbox every other record uses;
4. **closes that window again and asserts it closed**, from a `trap`, so a
   failed fetch, a `Ctrl-C` or a crash mid-way ends the same way a success does;
5. follows the accepted message ids to the broker and to `ingest_log`;
6. prints per-city success or failure, the as-of before and after, the accepted
   message ids, and how many of them are stored versus still in flight.

It exits non-zero when any city fails (`2`), when the egress window could not be
closed (`3`) — the one outcome that needs a human — or when the rows were
accepted but nothing reached the database in time (`4`).

```sh
# open and close the window without fetching: the drill for
# "does this always put the ingestor back?". Needs no internet.
docker compose -f compose.tools.yml run --rm refresh --check

# one city, a shorter horizon, and a shorter wait for the consumer
docker compose -f compose.tools.yml run --rm refresh --city rome --days 7 --wait 60
```

The window is opened with `docker network connect` and closed with
`docker network disconnect`, rather than by recreating the container under
`compose.connected.yml`. That adds and removes one interface on one container:
no restart mid-refresh, no re-accepting the whole snapshot, and nothing that
depends on which filesystem the command was typed on.

Typed by hand it is three commands, and the **third** is the one that matters —
it is what puts the ingestor back:

```sh
docker compose -f compose.yml -f compose.connected.yml up -d ingestor
docker compose exec ingestor python -m services.ingestor.refresh
docker compose up -d ingestor
```

The wrapper exists because a step an operator has to remember is a step that
gets skipped, and because a failed fetch or a closed terminal skips it too.

`OPEN_METEO_URL` points the fetch at an internal mirror instead of the public
API, for a site that has a mirror but no route to the internet.

**There is no refresh button in the UI, on purpose.** A button would need
either the Docker socket inside the UI container or an unauthenticated endpoint
that runs host commands; both are a worse problem than the one they solve. The
**Update data → Operator refresh** tab therefore shows what it can show
honestly: per-city freshness straight from the database, the exact command, and
a plain statement that displaying the command has not fetched anything.

A user edit is accepted, not applied: `202`, never `200`. The UI says so too.
There is exactly one write path into this database.

---

## Security

* Every upstream image and Dockerfile base is pinned **by digest** (`IMAGES.lock`, enforced in CI).
* The model is verified against `models.lock` before it is used.
* Containers run as **uid 10001** wherever the base image allows.
* **Three database roles**: the owner runs migrations; `aow_writer` (consumer
  only) may `INSERT`/`UPDATE`, plus `DELETE` on `events` only so it can remove
  generated demo rows when demo mode ends; `aow_reader` (api, agent, enricher)
  may only `SELECT`. Enforced by grants, not convention.
* Secrets live only in a gitignored `.env`; `.env.example` is committed.
* Only 8080 and 8000 are published, **and only on `127.0.0.1`** — see below.
* **No container in the running stack can reach the Docker socket.** The one
  container that mounts it is the proof runner in `compose.tools.yml`, which
  exists only while a drill is running and is never started by `up` — see
  [Shorthand](#shorthand) for why it needs the daemon and how to avoid it.
* CI runs blocking **Trivy** (`HIGH,CRITICAL`) on both images and the filesystem,
  **gitleaks**, and a guard that fails the build if a hosted-model SDK or
  endpoint ever appears in the source.

### The write API has no authentication, and the binding is what stands in for it

Stated plainly, because it is the one real hole in this design and the fix for
it is a deployment decision rather than a patch.

The API accepts writes — `POST /recommendations`, `POST /itineraries`,
`POST /reenrich`, `PATCH /records/{entity}/{id}` — and asks no caller for
credentials. Any client that can open a socket to port 8000 can queue a
correction, a recommendation or a batch of enrichment work. Nothing downstream
distinguishes those messages from the ingestor's: they carry the same envelope,
take the same queue and reach the same consumer.

**What protects it here is that nothing off this machine can open that socket.**
`${AOW_BIND_ADDR:-127.0.0.1}` binds both published ports to loopback. The
threat model that matches is the one that is actually true of a take-home demo:
one workstation, one operator, the UI in the same Compose project reaching the
API over the internal network rather than over the published port. Under that
model an unauthenticated write API is no weaker than the shell prompt already
sitting in front of it, and adding a token would be security theatre — a shared
secret in a `.env` file on the same disk, protecting the machine from itself.

**What that model does not survive is a second user.** Before this is published
beyond the trusted host — a shared on-prem server, a jump host, anything with a
routable address — the write path needs all of:

* **Authentication** against the organisation's own identity provider (OIDC at
  the edge, or Kerberos/LDAP where that is what exists). Not a bearer token
  checked in a config file, and not a check in the Streamlit UI: the UI is a
  client of this API, so a control that lives only in the UI is bypassed by
  `curl`.
* **Authorisation by role**, because the routes are not equally dangerous. A
  reader needs the `GET` routes only; a planner may `POST /itineraries`; a data
  steward may `PATCH /records/...`; an operator may `POST /reenrich`, which
  commits the whole model backlog to work. Enforced in the API, where the route
  is known, with the edge doing authentication and identity propagation only.
* **An identity on every accepted message.** The envelope has `source`, and a
  user edit currently sets it to the service. It would carry the authenticated
  principal, which makes `record_history` an audit trail rather than a change
  log.
* **Rate limiting** on the write routes. `POST /reenrich` is the one that
  matters: it is cheap to call and expensive to serve.

None of that is implemented, and a token check or a UI-only guard was
deliberately not implemented in its place — either would read as authentication
without being it. The
[production path](#production-path-kubernetes--openshift) is where this
belongs: an OIDC-authenticating ingress in front of the service, with the role
check in the API.

Until then: **`AOW_BIND_ADDR` widens the binding, and widening it is the point
at which this system becomes multi-user without having become multi-user safe.**

If the stack runs on a remote host — a VM, a lab machine — reach it by
forwarding the ports over SSH rather than by widening the binding:

```sh
ssh -L 8080:127.0.0.1:8080 -L 8000:127.0.0.1:8000 user@host
```

That borrows SSH's authentication, which is a real one, and leaves the ports
closed to everyone else. It borrows no authorisation: anyone who can open that
tunnel has every route, including the write routes. It is the right answer for
one operator on a remote box and the wrong one for a team.

---

## Tests and CI

```sh
docker build -q -f tests/Dockerfile -t aow/tests:dev .
docker run --rm aow/tests:dev
```

Dependencies are baked into that image at build time, so the run itself makes
no network call; CI runs the same container with `--network none`. `make test`
is the shorthand.

Unit tests cover the rule engine's truth table (including that indoor
activities really are scored as the inverse of outdoor ones), envelope
round-tripping and rejection of malformed messages, payload validation, and the
outbox's two load-bearing properties: accepting the same message twice is a
no-op, and an accepted-but-unpublished record survives the process dying. Also
covered are the agent's date parsing and word-boundary intent matching, the
planner, API responses, UI rendering, and the demo event boundary.

CI (`.github/workflows/ci.yml`) runs lint, unit tests and guards, then builds
the service and UI images once, scans them and the repository, and runs a fresh
Compose integration test against those images. That test checks snapshot →
RabbitMQ → Postgres → API, stored suitability scores, and a correction through
the outbox and queue into the audit history. It also accepts a record while
Postgres is stopped, verifies its commit through a separate reader after
recovery, and checks it survives consumer/database restart exactly once. It
also recreates a confirmed outbox ID absent from both Postgres and the queue,
then proves replay stores it once while an already-stored ID is skipped. It
uses a separate Compose project and removes its temporary volumes afterward. CI has
the internet; the runtime does not. The guard job enforces the offline model
boundary.

The rest of the guard job is there because this repository makes claims that
rot quietly. It fails the build if a pulled image or a Dockerfile base is not
pinned by digest; if a pinned digest disagrees with `IMAGES.lock`, so the lock
cannot become documentation of a release nobody runs; if any workflow action is
on a movable tag rather than a commit SHA, since those actions run with this
workflow's token; and if any count in the README or the Makefile disagrees with
`data/snapshot/MANIFEST.json`, which `scripts/snapshot_manifest.py` derives
from the snapshot files. That last one is not hypothetical: a README quoting
289 of them shipped against a snapshot holding 620.

### The grounding gate, before a release

Two gates need something CI does not have — the local model, and a real
Postgres with real tzdata — so they are run by hand before a release rather
than on every push.

```sh
make grounding                                    # the model gate
docker compose exec -T api python - < tests/integration/event_local_days.py
```

`make grounding` starts a second `llm` in its own Compose project (`aow-f3`),
never touching a running stack, and replays the questions that produced
ungrounded answers in review: the assignment's London example, concerts only
with and without a concert on record, a sports row that must not answer a
concert question, the exact "are there any sports events tomorrow in London?",
surfing in a city with no surf score, and the history of Lisbon. Each case
passes only if what the traveller would receive is supported by the retrieved
rows — either because the model stayed inside them, or because its wording was
rejected and the rows were rendered instead. Each also carries a correct
hand-written answer that must **not** be rejected, so a validator that simply
refused everything would fail here rather than look perfect. The command exits
non-zero on any failure and tears its project down either way.

The second command asserts that an event falls on the day it falls on **in the
city**: a listing that opens at local midnight, one that runs across several
days, and one in another timezone, plus five boundary cases evaluated against
Postgres itself. It is also part of the CI integration job, because the stack
it needs is already up there.

On a push to `main`, **only after those gates pass**, CI publishes the same
tested images to GHCR with a `sha-<commit>` tag. Its `aow-images-<commit>` run
artifact contains `images.lock` with the registry digests. Pull requests never
publish. The local model is too large for a useful per-PR full-stack run; the
integration test exercises the queue and database path without it.

### Offline release and installation

The repository is private, so sign in to GHCR on a connected staging machine
with permission to read its packages. Download `aow-images-<commit>` from the
successful `main` workflow run, check out that exact commit in a clean clone,
stage the pinned model, and build the transport folder:

```sh
RUN_ID=123456789  # replace with the successful main workflow run ID
COMMIT=$(git rev-parse HEAD)
gh run download "$RUN_ID" -n "aow-images-$COMMIT" -D release
docker compose --env-file .env.example pull postgres rabbitmq llm edge
docker compose -f compose.tools.yml --env-file .env.example run --rm stage
bash scripts/package-offline.sh release/images.lock
```

`dist/aow-<commit>/` contains the exact CI images (plus the digest-pinned
upstream images) in `images.tar`, the verified model, the Compose files, code,
migrations, snapshot, an installer, and two files that say what the rest is
supposed to be: `SHA256SUMS` over **every** file in the folder, and
`images.bundle.lock`, which records the registry digest each bundled image was
tagged from. Copy the folder to the on-prem **Linux/amd64 Docker host**. There,
fill in a new `.env` and run:

```sh
cd aow-<commit>
cp .env.example .env           # set distinct passwords
bash scripts/install-offline.sh
```

The installer checks the bundle before it changes anything on the host:
`SHA256SUMS` for every file, `models.lock` for the model, and
`scripts/verify-bundle-images.sh`, which reads the manifest digests out of
`images.tar` and compares them with `images.bundle.lock`. The two checks answer
different questions. `SHA256SUMS` answers *did these bytes arrive intact*; it
cannot answer *are these the bytes CI built*, because anyone replacing the
archive would replace the checksum file with it. The digest check answers the
second question, against digests that came out of the CI run artifact.

Then it loads the images, starts Compose with `--no-build --pull never`, and
runs `scripts/release-smoke.py`: API health, stored weather and scores, the
agent, the local model, the UI and the edge.

**Upgrades and rollback.** `compose.yml` fixes the project name, so every
release folder installs over the same Postgres, RabbitMQ and outbox volumes —
that is what makes an upgrade an upgrade rather than a second empty system.
Because of that, an install onto a running system is a change to live data, so
the installer takes a `pg_dump` into `backup/` **before** it loads the new
images, and refuses to continue if that dump comes back empty. Keep the
previous release folder; it is the rollback unit.

If a release fails, roll it back in two steps, because they undo two different
things:

```sh
cd ../aow-<previous-commit>
bash scripts/install-offline.sh                       # 1. code and images back
bash scripts/restore-offline.sh \
  ../aow-<failed-commit>/backup/<project>-<stamp>.sql # 2. data back, if needed
```

Step 1 reverts the images and the migration files. It does not revert what the
failed migration already did to the database — a migration that dropped or
deleted something stays dropped or deleted, and the release smoke check will
fail on the way out rather than report a healthy rollback. Step 2 is for that
case, and the dump it wants is the one the **failed** install took on its way
in, which is why it lives in the failed release's folder. The outbox volumes
are deliberately left alone: they hold records that were accepted but not yet
published, and replaying them after the restore is the point.

**Two different offline claims, kept apart.** A machine that has completed
[staging](#2-stage-it--once-with-internet) runs the whole system *and* every
proof with the network off, because staging built the proof runner too. The
transport folder is narrower: `images.tar` holds the six runtime images only.
The `stage` and `demos` tool images are **not** in it. The demo scripts
themselves travel with the folder, so on a bundle-installed host they run if
that host has `bash`, `curl` and `python3` — which is the per-OS dependency
this whole section exists to avoid. Putting those two images in the release
would fix it; that is a change to `scripts/package-offline.sh` which has not
been made or verified here.

**What was actually run, and where.** The release path was exercised end to end
from the digest manifest of a green `main` run, on a Windows Docker Desktop
host with a Linux/amd64 engine. Packaging took 3m15s and produced a 1.8 GB
folder (539 MB `images.tar`, 1.2 GB model, 129 checksummed files). The install
ran in its own Compose project against fresh volumes
(`COMPOSE_PROJECT_NAME=aow-rel`, `AOW_BIND_ADDR=127.0.0.2` in that folder's
`.env`, which is also how you stand a release test beside a running stack), and
these are the results:

| Exercise | Result |
|---|---|
| First install, empty volumes | 61 s to `PASS`, all 11 services up |
| Upgrade over the running install | 49 s; pre-upgrade dump written first, 4012 lines, all nine tables |
| `SHA256SUMS`, all 129 files | verified; appending one line to a migration failed the check |
| `images.bundle.lock` vs `images.tar` | 6/6 digests matched; a wrong digest and a missing entry both failed |
| `--pull never` with an image deleted | Compose refused — "No such image" — and reached no registry |
| Egress from agent, api, consumer, ingestor | `errno 101`; `aow-rel_backend` reports `Internal=true` |
| E1 through the installed release | answered from stored data, with source and as-of |
| Failed upgrade (a migration that deletes and then errors) | install aborted; forecast rows 80 → 0 |
| Rollback: previous folder's installer | images and migrations reverted; smoke **failed**, correctly, on the still-empty forecast |
| `scripts/restore-offline.sh` with the failed release's dump | 11 s; every row back (80 forecast, 620 place and 81 fact rows, as that bundle's own snapshot holds); smoke passed |

Two limits on that. The isolation is a **second Compose project on the same
machine**, not a physically disconnected host: the Docker network is
`internal: true` and the containers cannot route out, but the host NIC stayed
up and the folder was never transferred anywhere. And the bundle was built from
the last published `main` commit, so the release tooling in it is this branch's
copy laid over that bundle rather than a bundle that commit produced — the next
bundle cut from `main` produces `images.bundle.lock` itself.

---

## Requirements traceability

IDs are from `ASSIGNMENT.md`, which decomposes the brief. "Verify" is a command
you can run.

| ID | Requirement | Where it lives | Verify |
|---|---|---|---|
| M1 | Weather for five cities from an external API | `services/ingestor/providers.py` (Open-Meteo), `data/cities.yml` | `curl localhost:8000/coverage` → 80 rows, 5 cities |
| M2 | LLM recommendation: is the weather suitable for the activity | `common/rules.py` scores it, `services/enricher/` words it; free-text via `POST /recommendations` | `docker compose -f compose.tools.yml run --rm demos reenrich`; the Suitability page |
| M3 | Local open-weights LLM, no external API | `llm` (llama.cpp + Qwen3-1.7B), `common/llm.py` is the only client | `docker compose -f compose.tools.yml run --rm demos offline` §3 |
| M4 | All collected data → queue → database | outbox → `aow.events` → consumer; consumer is the only writer | `docker compose -f compose.tools.yml run --rm demos no-data-loss`; `psql` grants |
| M5 | Containerized, one uniform way to run | `compose.yml` runs it; `compose.tools.yml` stages it and runs the proofs. One quick start, the same commands on Windows, macOS and Linux, with Docker as the only host dependency; `Makefile` is a shorthand and is required by nothing | the [Quick start](#quick-start), ending in `docker compose up -d` |
| M6 | Runs on-prem without full internet | `backend` is `internal: true`; committed snapshot | `docker compose -f compose.tools.yml run --rm demos offline`, ideally with the host NIC down |
| M7 | Agent answering varied questions from stored data | `services/agent/` | `docker compose -f compose.tools.yml run --rm demos questions` |
| M8 | Tourism: history, places, sports events | `facts`, `places`, `events` tables | `docker compose -f compose.tools.yml run --rm demos questions` (Lisbon history, London sports) |
| M9 | Itinerary for chosen destinations | `POST /agent/itinerary`, the Trip planner page | build, save and reopen a plan in the UI |
| M10 | Good data visualization | forecast chart, city×day×activity heatmap, offline places map, coverage banner | the Forecast, Suitability and Places map tabs |
| M11 | Temporary failures without data loss | outbox, confirms, ack-after-commit, DLQ + redrive, outbox↔`ingest_log` reconciliation | `docker compose -f compose.tools.yml run --rm demos no-data-loss`; the CI gate in `scripts/ci-integration.sh` |
| M12 | Update stored information | `PATCH /records/...`, the operator refresh (`scripts/refresh.sh`), re-enrichment | `docker compose -f compose.tools.yml run --rm demos update`; `… run --rm refresh --check` for the egress window |
| S1 | Repo with code, config, CI/CD, README | `.github/workflows/ci.yml`; release tooling in `scripts/`: `package-offline.sh`, `verify-bundle-images.sh`, `install-offline.sh`, `restore-offline.sh` | `gh run list`; [Offline release and installation](#offline-release-and-installation), including the upgrade-and-rollback drill |
| S2 | README: startup, architecture, choices and reasoning | this file | you are reading it |
| B1 | Full tests for all components | **partial** — unit tests plus a CI Compose integration test; the full model and UI flows remain demo checks | `docker run --rm aow/tests:dev`; CI integration job |
| B2 | LLM observability metrics | **not attempted** — `llm` exposes llama.cpp's own `--metrics`, unscraped | — |
| B3 | Automatic recovery from failures | **partial, and not as a bonus feature** — reconnect-with-backoff everywhere, `restart: unless-stopped`, healthchecks, automatic re-enrichment | `docker compose -f compose.tools.yml run --rm demos reenrich`, then `… demos no-data-loss` |

---

## Reproducing every claim in this file

Every proof runs the same way on every operating system, against a stack that
is already up:

```sh
docker compose -f compose.tools.yml run --rm demos offline       # M6
docker compose -f compose.tools.yml run --rm demos questions     # M7/M8
docker compose -f compose.tools.yml run --rm demos no-data-loss  # M11
docker compose -f compose.tools.yml run --rm demos update        # M12
docker compose -f compose.tools.yml run --rm demos reenrich      # the model is not a dependency
docker compose -f compose.tools.yml run --rm demos all           # all of them, in order
```

| | |
|---|---|
| `offline` | air-gapped operation, and the no-guessing rule |
| `questions` | agent breadth, including what it refuses |
| `no-data-loss` | four drills, each tracing one accepted `message_id` |
| `update` | an edit through the queue, with its history |
| `reenrich` | the local model is a presentation layer, not a dependency |

The unit tests need no stack and no network:

```sh
docker build -q -f tests/Dockerfile -t aow/tests:dev .
docker run --rm aow/tests:dev
```

The loopback binding is one of them — it reads the Compose files rather than a
running stack, so it holds for every overlay and for a host that has never
started this project:

```sh
docker run --rm aow/tests:dev pytest tests/unit/test_compose_ports.py -v
docker compose config | grep -A 4 'ports:'   # host_ip: 127.0.0.1, twice
```

The release bundle can be checked on the offline host without starting
anything, which is also what `scripts/install-offline.sh` does before it
touches the daemon:

```sh
cd aow-<commit>
sha256sum -c SHA256SUMS                  # every file in the folder, code included
sha256sum -c models.lock                 # the model
bash scripts/verify-bundle-images.sh .   # images.tar against images.bundle.lock
```

The numbers this file quotes about the snapshot come from the snapshot:

```sh
docker run --rm -v "$PWD:/work" -w /work \
  python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 \
  python scripts/snapshot_manifest.py --check
```

`make offline`, `make demo`, `make test`, `make manifest` and the rest are
shorthands for these; see [Shorthand](#shorthand). The proof runner mounts the
Docker socket, for the reason given there.

---

## Known limitations

Stated, not implied:

* **Single-replica broker and database.** Fine for this; not an HA design.
* **Per-message accounting covers the producer outboxes, not the whole
  system.** `services.common.reconcile` audits every confirmed outbox envelope
  against `ingest_log` and can replay a missing one, so an accepted record can
  always be accounted for. There is still no equivalent reconciler proving
  every *enrichment* was delivered.
* **The outage gate drives the API outbox, not the ingestor's.** The three
  drills accept through the API, so it is the API's outbox that is proven
  across a broker and a database outage. The ingestor's outbox is audited on
  every CI run -- every confirmed envelope reconciled against `ingest_log` --
  but this gate never stops a dependency underneath the ingestor and replays
  through it. Same code path on both sides, so the risk is small; it is still
  audited rather than exercised, and the claim stops there.
* **The enricher polls** rather than binding to the weather stream. That is a
  deliberate trade: no second delivery branch means no silent partial fan-out.
* **A user-entered activity is scored against general outdoor comfort**, not a
  rule tuned for it, and the answer says so. It is not a new data fetch.
* **No marine data.** Surfing is scored, in coastal cities only, from wind,
  temperature and precipitation — never from wave height, swell or sea state.
  The score says whether the day is pleasant to be on the water, not whether
  the surf is any good, and no wave source was staged to say otherwise.
* **No surf spots, dive sites or boat hire on record.** The places snapshot
  names beaches and marinas, which is not the same claim, so “where can I surf
  in Tel Aviv?” is answered with an explicit “I do not have a verified surf
  spot,” never with a beach. A sourced surf-spot layer, carrying the same
  `source` and `as_of` as every other row, is what would close this.
* **The verified event set is 26 rows, and it is thin and uneven.** Every
  city has at least one listing (london 11, reykjavik 6, rome 4, lisbon 4,
  tel-aviv 1), but it is a hand-checked snapshot of a few venues per city over
  a few weeks, not a feed. A default run answers "none on record" for most
  dates and categories, and the trip planner has few events to place.
  `make up-demo` fills the gap with labelled generated rows for
  demonstration; it does not close it.
* **The places map is a bundled extract, not a map service.** Stored places are
  drawn as points on a local equirectangular projection over a 20 km
  OpenStreetMap extract per city — main and secondary streets, rivers,
  coastline, water and parks, staged once while connected and read from the
  image thereafter. A tile map was planned and cut, deliberately: tiles are a
  runtime download and the air-gap rule outranks the cartography. What that
  costs, and it is visible: no residential streets, no buildings, no labels, no
  route directions, no building-level zoom, geometry simplified to about 12 m,
  and nothing at all beyond 20 km from the city centre. Water mapped in OSM as
  a multipolygon relation — the Thames is the one that shows — draws as its
  centreline rather than as a filled channel.
* **The agent routes deterministically in code** and uses the model only to
  phrase retrieved rows. It is not a general-purpose assistant, and that is the
  point.
* **Writes are eventually consistent** — 202, then the queue.
* **The write API has no authentication or authorisation**, and is kept safe
  only by being bound to loopback. That is sufficient for a single-workstation
  demo and insufficient for anything shared; what a shared deployment would
  need is spelled out under
  [Security](#the-write-api-has-no-authentication-and-the-binding-is-what-stands-in-for-it).
  Remote access over SSH port-forwarding works and inherits SSH's
  authentication, but not its authorisation: a forwarded port is full operator
  access to every route.
* **`edge` is the one container on a routable network**, by necessity.
* **The CSP carries `'unsafe-inline'` and `'unsafe-eval'`** because Streamlit's
  bundle requires them. Noted rather than quietly included.
* **The offline release scripts need a Linux/amd64 Docker engine.**
  `scripts/package-offline.sh` and `scripts/install-offline.sh` refuse to run
  on any other engine architecture and need `bash` and `sha256sum` on the host.
  Git Bash supplied those tools for the Windows Docker Desktop test; Apple
  Silicon's native arm64 engine is not supported by this bundle. The quick
  start and the proofs have no such restriction. This applies only to building
  and installing the transport folder, and the
  [production path](#production-path-kubernetes--openshift) below is the answer
  for a real on-prem install. `scripts/restore-offline.sh` additionally needs
  the release folder's `.env` to be the one the dump was taken under.
* **The release has been installed in isolation, not on a disconnected host.**
  Package, whole-folder checksums, image-digest verification, `--pull never`,
  first install, upgrade, a failed upgrade and a restore-backed rollback all
  ran — but in a second Compose project on the staging machine, with the host
  NIC up. The containers had no route out (`internal: true`, `errno 101` from
  every service), which is a strong simulation and not a separate-host
  air-gap certification. [Offline release and
  installation](#offline-release-and-installation) lists exactly what ran.
* **Rollback is two commands, not one, and the second needs a dump.** An image
  rollback cannot undo a migration, so a release that migrates destructively is
  recoverable only from the `pg_dump` the failed install took on its way in.
  That dump is automatic, but its retention is not: nothing prunes `backup/`,
  and a host that runs out of disk there will fail the next upgrade at the dump
  step rather than half-way through it.
* **The proof runner holds the Docker socket** while a drill runs. It is a
  deliberate, explicit invocation and nothing in the running stack has the
  socket, but it is real host access and is named here rather than buried.
* **The bonus items (B1–B3) are partial**: CI covers the queue/database/API
  integration path, but not the full model and UI flows. There is no
  Prometheus/Grafana stack; `llm` exposes llama.cpp's own `--metrics`.

---

## Production path (Kubernetes / OpenShift)

**Compose is the demo vehicle, not the shipping mechanism.** It is here because
one `docker compose up -d` on the reviewer's own laptop is the most honest way
to show that this works, and because it is the same stack on every OS. It is
not what I would deploy.

**The shipping unit is images in an internal registry.** Nothing is built on
the target and nothing is pulled from the public internet:

* CI publishes the tested, scanned images by digest. A connected mirror host
  copies those digests, and the pinned upstream ones, into the internal
  registry — `skopeo copy --all docker://ghcr.io/…@sha256:…
  docker://harbor.internal/aow/…`, or `oc mirror` with an ImageSetConfiguration
  on OpenShift, which also writes the `ImageDigestMirrorSet` that redirects
  every pull at the cluster.
* The model is not an image. It goes into the registry as an OCI artefact, or
  onto a PVC seeded by a Job, verified against `models.lock` either way — the
  same check the `stage` container runs here.
* **A Helm chart** is the deployable: one values file per environment, image
  digests as values so a rollback is a value change, and the schema migration
  as a `pre-install`/`pre-upgrade` hook Job.
* **A default-deny egress `NetworkPolicy`** on the namespace is the real
  version of `internal: true`, with one explicit allow for the ingestor during
  a scheduled refresh, and one for DNS.

That path needs a registry, a chart repository and cluster access to
demonstrate, none of which a take-home reviewer has. What is demonstrable here
is the property underneath it — digest pinning, checksum verification, and a
runtime with no route out — and that is what the proofs exercise.

The rest of what would change:

* **Postgres** via an operator (CloudNativePG) with a PVC, backups and a
  replica; **RabbitMQ** via the cluster operator with a quorum of three.
* **Outbox volumes** become PVCs with `ReadWriteOnce`; the ingestor and API
  become StatefulSets, because an outbox is state.
* **Secrets** move to Vault or sealed secrets; the three database roles stay as
  they are, because that separation is the useful part.
* **The loopback binding is replaced, not carried over.** A `Service` plus an
  OIDC-authenticating `Ingress`/`Route` does there what `127.0.0.1` does here,
  with the per-route role check in the API — see
  [Security](#the-write-api-has-no-authentication-and-the-binding-is-what-stands-in-for-it)
  for what that check has to cover. Exposing this stack in a cluster without it
  would be strictly worse than the demo, because the cluster has other tenants.
* Healthchecks become readiness and liveness probes, with the model load
  covered by a `startupProbe`.
* Prometheus scrapes the services (B2, not attempted here), and Loki takes the
  logs.

---

## Where the assignment's requirements live

`ASSIGNMENT.md` holds the brief, our decomposition into requirement IDs, and —
in its own section — the design choices that are **ours** rather than the
brief's. This README does not restate them; where it says "our choice", that is
where the reasoning is recorded.
