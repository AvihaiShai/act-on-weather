# Technical decisions

The decision record behind the system described in [README.md](README.md): what
was chosen, what it was chosen over, what it costs, and which gate or drill
demonstrates it.

The README is the submission document — how to run it, what it does, and the
limitations a user meets. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) is the
system map. [ASSIGNMENT.md](ASSIGNMENT.md) holds the brief, the requirement IDs,
and which choices are ours rather than the reviewer's. This file does not repeat
their setup instructions; it explains the engineering judgment.

Everything here describes the shipped system. Section 12 keeps the reversals —
approaches that were tried and abandoned — because each one still constrains the
current design. Section 11 separates what is missing from what is deferred to a
real deployment.

**Evidence** lines name a command, test or CI job. Where there is none, the
entry says so rather than implying one.

---

## 1. Docker Compose as the run path

| Decision | Why, and over what | Cost |
|---|---|---|
| **Docker Compose**, one project, overlay files rather than Compose profiles | The reviewer runs one command on their own laptop, identically on Linux, macOS and Windows. The whole topology — networks, volumes, published ports, memory caps, role passwords — is readable in one file. Chosen over **Kubernetes/OpenShift**, which would be the real deployment but needs a cluster, a registry and a chart repository that a take-home reviewer does not have | One host. No high availability, no rolling deploy, no self-healing beyond `restart: unless-stopped` |
| **Overlay files, not profiles** | An overlay is explicit at the command line, so a reader can see exactly which file added a service. Profiles hide that in the base file | More files. Nine overlay combinations exist, so CI renders every one of them |
| **One image for the five Python services** | They differ only in their command, so one build, one dependency set and one Trivy scan covers all of them | Any service's change rebuilds the image for all five |
| **Explicit memory caps on every service** | The development host runs Docker under WSL2, which gets a fraction of host RAM. Without caps the model server starves the database | The caps total 6.7 GB and need a host that can give Docker 8 GB |

**Evidence:** `docker compose up -d` is the documented run path; the `guard` CI
job renders every overlay combination; `docker compose config --quiet` fails by
name on a missing password.

## 2. Network isolation

The strongest infrastructure claim in the system, so it is enforced by Docker
rather than by configuration discipline.

| Decision | Why, and over what | Cost |
|---|---|---|
| **`backend` is `internal: true`** and holds every application service — including the model server, the broker, the database, Prometheus and Grafana | Docker gives the network no gateway, so "cannot reach the internet" is a property of the runtime, not a setting anyone can get wrong. Chosen over **per-service firewall rules or egress proxies**, which are configuration that can drift | Anything needing a fetch has to be attached deliberately and detached again |
| **`frontend` holds the nginx proxies and nothing else** | A published port cannot come from an internal network, so exactly one hop straddles the boundary. The proxies hold no credentials, no outbound client and no application logic | One container is on a routable network by necessity |
| **Ports written long-form**, `${AOW_BIND_ADDR:-127.0.0.1}:8000:8000` | The short form binds `0.0.0.0` silently. Only the UI and API are published, plus Grafana under the monitoring overlay | Remote access needs SSH port forwarding |
| **The connected refresh builds a throwaway network**, attaches the ingestor for one fetch, then detaches and deletes it — rather than using the declared `egress` network | An egress window that exists only for the length of a fetch cannot be left open by forgetting an overlay. A detached guard closes it after `REFRESH_WINDOW_MAX_S` (default 600) even if the command is killed | After a `kill -9` the ingestor keeps its route out until that deadline. `docker network inspect <project>_refresh_egress` returning *not found* is the check |

**Evidence:** `tests/unit/test_compose_ports.py` holds the binding over every
`compose*.yml`; `demos offline` asserts `backend` is internal and that four
representative services cannot open an outbound connection; the Playwright
`ui-gate` records every request the browser issues and fails on any off-origin
one; `make refresh-check` runs the open-and-close drill with no fetch.

## 3. Delivery: RabbitMQ and the outboxes

The requirement is no data loss under temporary failure. That is two separate
problems — the broker being unreachable, and the consumer failing mid-write —
and they have two different answers.

| Decision | Why, and over what | Cost |
|---|---|---|
| **RabbitMQ with quorum queues** | Per-message ack after a database commit, a real dead-letter exchange and `x-delivery-limit` are exactly the primitives needed, with the smallest operational surface. Chosen over **Kafka/Redpanda**, where offsets make per-message quarantine a piece of machinery to build, and over **Redis Streams**, whose durability story is harder to defend | One broker node. Durable, not highly available |
| **A SQLite outbox in front of the broker**, one per producer, WAL with `synchronous=FULL` | Publisher confirms cannot help when the broker is *unreachable*. The outbox turns "the broker is down" into a delay. It cannot be a table in Postgres, because the point is surviving Postgres; it cannot be an in-memory buffer, because the point is surviving the process dying. Chosen over **confirms alone**, which leave a window where an accepted record exists nowhere durable | One fsync per accepted record. The acceptance point moves from "stored" to "spooled", which is what the guarantee is stated against |
| **Three separate outboxes** (ingestor, api, enricher) on their own volumes | Each producer's durability is independent of the others' availability, and a drill can name which one it exercised | Three volumes to back up rather than one |
| **Ack only after the database transaction commits**, with the business write and the `message_id` insert into `ingest_log` in that same transaction | A crash between commit and ack redelivers a message that is already stored, and the idempotency ledger absorbs it. The reverse order loses records silently | At-least-once, so every write must be idempotent |
| **`x-delivery-limit: 5` with a fanout DLX to `aow.dlq`** | A poison message is quarantined rather than blocking the queue behind it forever | A quarantined message needs an operator to redrive it |
| **`message_id` as the idempotency key**, UUIDv5 over (routing key, natural key, as-of) for snapshot records | Replaying the committed snapshot on every boot produces the same ids, so the outbox's unique constraint absorbs the replay. A refreshed forecast has a new as-of, so it gets a new id and a real delivery. API and enricher envelopes use uuid4, because those are genuinely new events | The natural key has to be stable, which constrains the schema |
| **Eleven routing keys, enumerated**, with anything else refused as poison | A typo becomes a visible dead letter rather than a silently dropped record | Adding a message type is a deliberate two-place change |

**Guarantee boundary.** From the moment a record is fsynced into a producer
outbox it reaches one of exactly two terminal states: stored in Postgres, or
quarantined in `aow.dlq` and redrivable. Consumer, broker and database outages
delay a record; none of them lose it. Outside the boundary: a destroyed volume,
a full disk, and data never accepted in the first place — weather that was never
fetched can be re-fetched while connected. This is at-least-once eventual
storage, not exactly-once delivery, and not uninterrupted answers during an
outage.

**Evidence:** `scripts/ci-integration.sh` runs five outage drills on every PR —
consumer, broker and database stopped against the API outbox, broker and
database stopped against the ingestor's — each tracing one `message_id`, then
reconciles a confirmed envelope, restarts everything and asks a separate reader
for all traced IDs at once; a missing or duplicated ID fails the job.
`demos no-data-loss` runs four drills interactively — consumer, broker and
database outages plus a poison message, which the CI drills do not cover — each
following one accepted `message_id` to its terminal state.
`tests/unit/test_outbox.py` covers the two load-bearing
properties: accepting the same message twice is a no-op, and an
accepted-but-unpublished record survives the process dying.

## 4. Postgres, and one writer

| Decision | Why, and over what | Cost |
|---|---|---|
| **Postgres as the queryable source of truth** | The agent's coverage checks, the heatmap and the itinerary builder are all joins and aggregates over dated rows. Chosen over keeping state in the queue or a document store, either of which would push that work into application code | One database and one volume, which still need backup and restore |
| **Three roles: the owner runs migrations; `aow_writer` is the consumer alone; `aow_reader` is the API, agent and enricher** | "Only the consumer writes" is enforced by grants rather than by convention, so a mistake in a reader service fails at the database instead of corrupting data | A new write path has to go through the queue, which is the intent |
| **No schema-wide `DELETE`.** The writer's only DELETE grants are `events` (removing demo samples) and `itineraries` with `record_history` (user data) | Collected records are revised, not deleted, so history stays intact | A genuine bulk delete needs a migration |
| **The user-data wipe runs through an owner-defined `SECURITY DEFINER` function** | Rebuilding from the outbox needs one destructive step. Granting direct DELETE on every collected table to reach it would be a far wider permission than the operation needs | The function is privileged code that has to be read carefully |
| **`provider` is deliberately not in `weather_daily`'s key** | A second provider must *replace* a day, not shadow it, or the join to `recommendations` stops being unique | Two providers cannot be compared side by side |
| **Freshness guards on collected-data upserts** | Weather, place and fact updates require a newer `as_of`, so replaying an older snapshot cannot replace newer data. Events also update when their derived `valid_until` changes with the configured recheck window | The event exception can update a row without a newer `as_of`; itinerary and recommendation writes follow their own rules |
| **One history trigger over five tables, addressing the row through `to_jsonb(NEW)`** | PL/pgSQL rejects a column reference that does not exist on the table currently firing, *even in a branch never taken*, so a per-table `CASE` on column names cannot work | The history rows are JSON, not typed columns |
| **Event local days derived in SQL from the city's IANA zone** | An event starting at local midnight lands on its local day rather than the UTC one, and a multi-day run matches every day it is active on | Needs real Postgres tzdata, so it can only be tested against a live database |

**Evidence:** the base grants are in `db/migrations/001_init.sql`; the narrow
DELETE grants and wipe function are in migrations 003–005, and can be checked
with `psql`; `tests/integration/event_local_days.py` runs against a
real Postgres in CI; `demos update` shows a `PATCH` producing `revision + 1` and
a `record_history` row.

## 5. Deterministic scoring, with the model only phrasing it

The single most important design decision, and the one that keeps a 1.7B model
off the critical path.

| Decision | Why, and over what | Cost |
|---|---|---|
| **A rule engine over `data/activities.yml` is the source of truth for every verdict; the model only writes prose about rows it is handed** | The verdict is reproducible, testable and defensible in review, and a model outage degrades the wording rather than the content. Chosen over **letting the LLM decide suitability**, which is unrepeatable, untestable, and puts a small model in the path of every answer | Suitability is a heuristic over a land forecast, not a statement about safety or whether a venue is open |
| **Thresholds live in YAML, not in code** | A reviewer can read why a day scored what it did, and change a threshold without touching Python | A rule change needs a `rule_version` bump so stored scores stay interpretable |
| **Every activity is scored at weather-write time; only the top `ENRICH_TOP_N` (6) per city-day are queued for wording, the rest stored `deferred`** | Charts, the heatmap and named-activity answers need every score the city has. Wording all of them would take far longer than anyone waits on CPU. A `deferred` row is promoted on request | Most scores ship without prose until someone asks |
| **A model outage leaves rows `pending`, never blocks the weather write** | Weather is the data; wording is an enhancement. `POST /reenrich` re-words later and works fully offline | Notes can lag the scores; the UI says how many are still being prepared |
| **Retry policy split by failure class** | An *unavailable* model (connection error, timeout, 5xx, 429) is retried indefinitely at a bounded backoff and consumes no attempt; only *invalid output* counts, capped at 5. A flat attempt cap strands rows permanently through a long outage | Two failure paths to reason about instead of one |
| **The invalid-attempt count lives in the database** | A restarted enricher cannot reset it | An extra column on `recommendations` |
| **Late wording is rejected** when the weather it was written from is older than the stored row | A stale recommendation must not overwrite a fresher one after a redelivery | — |
| **Sea-dependent activities are capped at 69**, one below the `good` floor, with the cap written into the row's reasons when it binds | Nothing here measures waves, swell or water temperature. A capped score that does not say it was capped is a number quietly lowered behind the reader's back | Surfing, swimming, fishing and a boat ride can never be reported as a confident recommendation |
| **`rule_version` 4 removed the rule that inferred sea state from land wind** | The ceiling alone was not enough: a wind threshold still produced a verdict about the waves. No scoring rule may derive a sea state from a land measurement at all | — |
| **Coast-dependent activities are dropped, not scored, for inland cities** | An absent score is honest; a low score implies the activity was assessed | London has no water activities at all |

**Evidence:** `tests/unit/test_rules.py` covers the rule engine's truth table;
`demos reenrich` stops the model server and shows weather still accepted, scored
and stored with rows left `pending` and no retry consumed; the release-candidate
`model-grounding` CI job runs the real model against hand-written rows.

## 6. The agent: routing in code, one model call

| Decision | Why, and over what | Cost |
|---|---|---|
| **The router resolves city, dates, intent and coverage in Python; the model gets at most one call, to phrase retrieved rows** | Readable, deterministic and fast enough to demonstrate. Chosen over **model tool-calling**, which on CPU meant several sequential generations per question, non-deterministic in front of a reviewer, and silent when it went wrong | Not a general-purpose assistant. It answers the question shapes the router knows, and says so otherwise |
| **Unknown cities and out-of-coverage dates are refused by template, before any model call** | A refusal is the correct answer, and it should not depend on a generation | The refusal wording is fixed |
| **Named-activity verdicts are rendered directly from stored scores** | A small model asked to summarise seven `fair` days will call it a good week | Less natural phrasing for those answers |
| **Post-hoc grounding re-checks the model's prose against the typed rows** — invented event kinds, places presented as events, verdicts with no stored score, unknown dates, proper nouns and numbers — and discards the wording on any violation | The cheapest place to catch a small model inventing something is after it speaks, against the rows it was given | A discarded answer falls back to rendered rows with a note, which reads flatter |
| **The as-of footer and coverage-gap block are appended in code, after the model** | Provenance cannot be paraphrased away | — |

**Evidence:** `demos questions` exercises the agent's breadth including the
missing-data path; `tests/unit/test_router.py` and `tests/unit/test_grounding.py`
cover intent matching and the grounding checks; the `model-grounding` job runs
the real model for release candidates.

## 7. The model and its server

| Decision | Why, and over what | Cost |
|---|---|---|
| **Qwen3-1.7B Q4_K_M on llama.cpp** | Open weights under Apache-2.0, about 1.2 GB on disk, and reliable under a JSON-schema grammar, which is what the design actually depends on — the model fills a constrained shape rather than being trusted for judgment. Chosen over **a 3–4 B model**, which writes better prose but is materially slower on the CPU-only machine a reviewer is assumed to have | Small-model prose. The design compensates by never letting the model decide anything (§5, §6) |
| **CPU only** | The reviewer's machine is assumed CPU-only and about 16 GB of RAM | No GPU override file ships. `LLM_THREADS` is the only tuning knob |
| **`--parallel 2`** | An interactive question is not stuck behind an enrichment batch | Halves the context per slot: 8192 total, 4096 each |
| **Thinking mode disabled**, set as `LLAMA_ARG_CHAT_TEMPLATE_KWARGS` rather than a CLI flag | Qwen3 is a hybrid-thinking model: left on, replies spend their token budget in `reasoning_content` and come back with empty content, which is a failure rather than a slowdown. The equivalent CLI flag loses its JSON quoting through YAML and Docker's argument splitting; the environment variable does not | The model does no visible reasoning step, which is the intent here |
| **Weights staged once and verified against `models.lock`** | The bundle names exactly what it carries, and `MODEL_BASE_URL` redirects staging at an internal mirror with an identical checksum check | Staging needs one connected step |

**Not measured reproducibly:** per-call latency and the thinking-on/off
difference were observed during development but never captured by a benchmark,
on a recorded host, with a command anyone can rerun. No figures are quoted here,
and the README was corrected to match. The `model-grounding` job checks selected
answers against supplied rows; it does not time the model.

## 8. Interface, data sources and provenance

| Decision | Why, and over what | Cost |
|---|---|---|
| **Streamlit for the UI** | Nine working pages — charts, a heatmap, a map, a planner and a chat — in the time a hand-written SPA would take to scaffold, and it bundles its own assets, so it works offline. Chosen over **a React SPA**, which would be more polished, but the pipeline carries more weight in this brief | A local demo tool, not a multi-user web product. Its bundle needs `'unsafe-inline'` and `'unsafe-eval'` in the CSP |
| **The map is Plotly traces over a bundled 20 km OpenStreetMap extract, never a tile URL** | Tiles are a runtime download, and the air-gap rule outranks the cartography | No residential streets, labels, buildings or routing, and nothing beyond the extract |
| **Open-Meteo for weather** | No API key, so the air-gapped bundle carries no secret and no account that can expire, and the free tier includes the 16-day daily forecast "this week" needs. Chosen over **OpenWeather**, which means a key to manage and a free tier that splits the horizon awkwardly | Bound to one provider's model and grid |
| **Wikidata SPARQL for places**, with Overpass kept behind `--places-source osm` | A free shared Overpass endpoint signals an internal timeout as HTTP 200 with an empty element list and a `remark` — a silent failure indistinguishable from "this city has no museums" — and at staging time all four public mirrors were refusing connections. The fetcher now checks the body rather than the status | Wikidata holds notable venues, so the data skews to landmarks rather than every café |
| **Collected rows carry provenance and a revision** | Weather rows record `provider`, `source_url` and `as_of`; place, fact and event rows also record `source` and `is_sample`. Sources with different coverage stay distinguishable per row | There is no dedicated licence column; the weather table has no `is_sample` flag |
| **Events carry `checked_at` (a fact) and a derived `valid_until`** | A stored event is a reading of a listing page on a particular day, not an observation. Deriving expiry means `AOW_EVENT_RECHECK_DAYS` takes effect without rebuilding the snapshot, and expired rows stay stored and counted, so a stale feed is distinguishable from a city nobody checked | The feed goes out of date on a timer by design, and extending a listing is a manual one-row operation |
| **Generated sample events are opt-in, not merely labelled** | The ingestor builds no envelope for the 45 generated sample events without `AOW_DEMO_EVENTS`, the consumer drops them at the write boundary anyway, and leftovers are deleted when demo mode ends | Two gates to keep in step |
| **Documentation counts are machine-checked against the snapshot** | Counts in prose go stale silently. A regex that stops matching anything is itself an error, so the guard cannot quietly retire | Rewording a sentence that quotes a count means updating the pattern |

**Evidence:** `scripts/snapshot_manifest.py --check` runs in the `guard` CI job
over the README, `Makefile`, `ASSIGNMENT.md`, this file and
`docs/ARCHITECTURE.md`; the `ui-gate` asserts the rendered page makes zero
off-origin requests and that an as-of chip carries a timestamp;
`tests/unit/test_places_map.py` covers the offline basemap rendering.

## 9. Operations: monitoring, backup and offline delivery

| Decision | Why, and over what | Cost |
|---|---|---|
| **Monitoring is an opt-in overlay**, not part of `up -d` | A reviewer who does not want a time-series database and a dashboard server on their laptop never pays for them, and both images travel in the bundle so enabling it offline downloads nothing | A reviewer may never see it unless they run `make monitor` |
| **Prometheus and Grafana sit on the internal network**, with every Grafana phone-home switched off and dashboards file-provisioned with `allowUiUpdates: false` | The dashboards are reviewable text in the repository rather than UI state, and an air-gapped start cannot stall on a plugin fetch | An edit made in the browser is refused rather than silently reverted |
| **No Alertmanager.** 11 alert rules evaluate to a series shown on a dashboard | A single air-gapped host has no mail relay or webhook to route to; shipping a router with nowhere to route would be decoration | Nothing alerts off-host. Someone has to look |
| **Backups are logical `pg_dump -Fc` through the database**, plus online-consistent SQLite copies of all three outboxes and the broker's topology | A dump restores across engine versions; a copy of the data directory does not. Queue message bodies are deliberately not backed up, because every in-flight message was fsynced to a producer outbox first and reconciliation replays what the restored `ingest_log` cannot account for | Logical dumps, not point-in-time recovery. They land on the same disk as the volumes they protect unless an operator copies them off |
| **Restore verifies every artefact against the manifest before touching anything**, and restores into a separate Compose project unless told otherwise | A restore that fails halfway is worse than one that refuses to start. Migrations run first, because a dump carries grants but not roles | A restore after a password change needs the contemporaneous `.env` |
| **The offline bundle is checksums and digest chaining — nothing is signed** | Honest about what it is: `SHA256SUMS` is self-attesting, so the only out-of-band anchor is an operator carrying its digest separately. Chosen over implying provenance the repository cannot produce | No cosign signature, no SLSA provenance. A real deployment would add them |
| **Bundle images are verified by reading the tar, not the daemon** | A `docker save` from a containerd image store can emit a manifest whose blobs are not all present, and `docker load` exits 0 on it. An engine that packaged the bundle already holds the layers, so it installs a broken archive happily | An extra verification step, and a packaging machine that needs registry access to repair an incomplete save |
| **`.github/workflows/release.yml` deploys nothing** | It re-resolves every image digest from the registry, packages, verifies, installs onto a nested empty engine and records what passed. Crossing to an air-gapped host is a manual carry by design, because nothing can safely automate across that gap | "CD" here means package, verify and record — not deliver. The carry itself has never been performed (§11) |
| **The rollback drill uses a deliberately broken bundle, made by hand** | A migration written to fail cannot come out of CI — `main` is branch-protected and the packager refuses any tree that is not the commit named in its image lock — so the artefact is derived locally from a CI-proven bundle. Only the tree is altered; the images stay the digest-pinned set CI published | Resealing `SHA256SUMS` destroys the folder's self-attestation, so its digest is **not** a release digest. A sealed `FAULT-INJECTION.json` marker makes the verifier say so loudly, and the installer refuses it without an explicit opt-in |
| **Air-gap evidence is captured by a command into a file**, not retyped into a document | Several of the artefacts a proof needs — engine identity, the image-store census before the load, link state, the transferred archive's digest, the pull count — had no producing command, which is how "0 pull attempts" became a sentence rather than a measurement. Writing to `--out` rather than a pipe matters too: `cmd \| tee file` returns `tee`'s status, so a failed install would read as a pass | Another script to maintain, and its output has to live outside the folder whose unlisted-file check would otherwise reject it |

**Evidence:** `docs/EVIDENCE-observability-and-recovery.md` records executed
evidence for the metrics stack and a destructive restore;
`demos backup-restore` and the release-candidate `restore-drill` CI job run a
restore after destroying every volume; `docs/RELEASE-PROOF.md` states what the
release drills covered and what they did not.

## 10. Small decisions that were not obvious

Each of these cost real debugging time, and each would be easy to undo by
accident.

| Decision | Why |
|---|---|
| **`/outbox` and `/refresh` are created and chowned in the Dockerfile** | Docker gives a fresh named volume the ownership of the image's directory at that path. Without the `mkdir`, the volume is root-owned and a non-root service cannot write to it — and it fails only on a first run with a fresh volume, which is exactly the run a reviewer does |
| **nginx resolves its upstreams through a variable** | nginx refuses to *start* if a `proxy_pass` hostname cannot be resolved at config load, which would make `edge` crash-loop whenever a backend is not yet up |
| **Streamlit's `serverAddress`/`serverPort` point at 8080, not 8501** | Its XSRF check compares the websocket Origin against those. Without it the page hangs on "Connecting…" — invisibly, until first boot in a browser |
| **Intent matching is word-boundary, not substring** | `"eat"` is inside `"weather"`, so every weather question silently acquired the `places` intent. There is a regression test named after it |
| **The API's `/metrics` is 404'd at the proxy** | Prometheus scrapes `api:8000` directly over the internal network, so nothing needs the endpoint exposed on the published port |
| **The refresh report is a JSON file on a volume, not a database row** | A refused refresh accepts no messages, so the queue has nothing to carry. It is operator telemetry — last write wins, no history, and it goes with the volume |
| **The offline proof runs with `--no-build`, not just `--pull never`** | `compose.tools.yml` still carries a `build:` for the proof runner, so without it a missing image tag makes Compose *build* instead of failing — and the build's `apk add` then needs exactly the egress the proof exists to show is absent, so a tag problem would have been reported as a network error. A proof has to be able to fail for the reason it is testing |

## 11. Limits

Separated into what is missing from the shipped system and what is deliberately
left to a real deployment. The README's *Known limitations* carries the full
user-facing list.

**Shipped, with a known gap**

- **`/docs` is the one part of the system that does not work offline.** FastAPI's default Swagger UI pulls its JavaScript and CSS from a CDN, and the API port carries no CSP. `GET /openapi.json` serves the same schema from the machine and is the offline path. Replacing the page with self-hosted assets was not done; the working alternative was documented instead.
- **Single host, single broker, single database.** Durable, not highly available.
- **Offline use does not move the forecast window forward.** A question past the stored window is refused rather than guessed, and a refresh needs an operator and temporary connectivity.
- **No marine data.** Sea-dependent activities are scored from land weather and capped below `good`.
- **Events are a hand-checked sample, not a feed**, uneven by category and expiring on a timer, with no automated re-check — the sources are ordinary venue websites with no common format.
- **Itinerary editing is not implemented.** Building, saving and deleting are.
- **Writes are eventually consistent** — `202`, then the queue.
- **Per-message accounting starts at an accepted outbox envelope.** Enrichment results created before the enricher had an outbox have no producer envelope to reconcile.
- **Some paths have no automated coverage**: DLQ redrive, the user-data wipe replay, the refresh egress guard, and the connected half of the event re-check. The ingestor's drills accept through a test fixture, because CI has no egress.
- **The offline release scripts need a Linux/amd64 Docker engine** and `bash`. The Compose run path does not.
- **The offline release has never been installed on separate physical hardware, and the physical carry has never been performed.** Every install recorded so far ran on a separate Docker engine on the development machine or on a hosted CI runner — which is enough to catch a bundle that only installs where it was packaged, and is not a physical air gap. `docs/EVIDENCE-physical-airgap.md` states what is staged, what is blocked on hardware, and what would close it.

**Deferred to a real deployment, not attempted here**

- **Authentication and authorisation on the write API.** Loopback binding is the whole control. A token check or a UI-only guard was deliberately *not* added in its place, because either reads as authentication without being it. What a shared deployment needs is set out in the README's *Production path*.
- **Signing and provenance** for release artefacts.
- **High availability, point-in-time recovery, and off-host backups.**
- **Kubernetes/OpenShift manifests, a chart, and a default-deny egress `NetworkPolicy`** as the real version of `internal: true`. None of it is in the repository.

## 12. Reversals worth keeping

Approaches that were tried and abandoned. Each is here because the reason still
constrains the current design, not as a changelog.

| Was | Is | Why it changed |
|---|---|---|
| Overpass as the places source | Wikidata SPARQL, Overpass optional | A free shared endpoint's success status is not evidence of success — it returned HTTP 200 with an empty body and a `remark` on internal timeout. The fetcher now checks the body |
| The model chooses what to look up, via tool calling | The router resolves everything in code, one model call for wording | Several sequential generations per question on CPU, non-deterministic, and silent when it went wrong |
| The enricher binds to the weather stream | The consumer creates `pending` rows; the enricher polls them | A second queue binding is a second delivery branch, and a partial fan-out fails silently with nothing to detect it |
| A flat attempt cap on enrichment | Split by failure class (§5) | A flat cap strands rows permanently through a long model outage — the opposite of the durability the system claims |
| A tile-served markers map | A bundled OpenStreetMap extract | Tiles are a runtime download, and the air-gap rule outranks the cartography |
| Monitoring cut as a bonus | Shipped as an opt-in overlay | It was the cheapest way to make the delivery guarantees observable rather than merely asserted |
