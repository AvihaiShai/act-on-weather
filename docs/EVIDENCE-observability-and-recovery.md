# Evidence: observability and operational recovery

**Dated 2026-09-24.** Closes review finding **F8** ("observability and restore
evidence are incomplete"), which recorded: no scraper, dashboard, alerting or
request/error/latency series; and no backup/restore command, drill or RPO/RTO
figure.

Every command below was run. Where something was not run, this file says so
rather than leaving the gap for a reader to discover. A script printing `PASS`
is not evidence on its own; the assertions behind each claim are named.

---

## 1. What was added

| Area | Where |
|---|---|
| Metric definitions, ASGI middleware, scrape-time collectors | [`services/common/metrics.py`](../services/common/metrics.py) |
| Instrumentation | `services/{api,agent,consumer,enricher,ingestor}/main.py` |
| Scrape config, alert rules | [`observability/prometheus/`](../observability/prometheus/) |
| Dashboards and provisioning | [`observability/grafana/`](../observability/grafana/) |
| Monitoring overlay | [`compose.observability.yml`](../compose.observability.yml) |
| Backup / restore | [`scripts/backup-state.sh`](../scripts/backup-state.sh), [`scripts/restore-state.sh`](../scripts/restore-state.sh) |
| Recovery drill | [`demos/06_backup_restore.sh`](../demos/06_backup_restore.sh) |
| Operator procedure | [`docs/RUNBOOK-BACKUP-RESTORE.md`](RUNBOOK-BACKUP-RESTORE.md) |

Monitoring is an **opt-in overlay**. `docker compose up -d` does not start
Prometheus or Grafana; `make monitor` does. The two images are pinned in
`IMAGES.lock` and travel in the offline bundle, so enabling monitoring on an
air-gapped host downloads nothing.

---

## 2. Metrics, and why the labels cannot grow without bound

Label cardinality is the failure mode that turns monitoring into an outage, so
it is a property under test rather than a convention.

* HTTP metrics label on the **matched route template** — `/weather/{city}` —
  read from the ASGI scope after routing, never `scope["path"]`. A request
  matching no route reports `route="<unmatched>"`.
* The HTTP **method** is folded to `<other>` outside nine known verbs, because
  the request line is written by the client.
* `routing_key` is folded to `<unmatched>` outside `config.ROUTING_KEYS`.
* Exception labels carry the **class name** only, never the message.

**Proven against a live stack**, not only in unit tests. Three cities and four
distinct junk paths were requested through the published edge:

```
route=/coverage                     method=GET status=200  n=1
route=/health                       method=GET status=200  n=11
route=/metrics                      method=GET status=200  n=6
route=/weather/{city}               method=GET status=200  n=3   <- 3 cities, 1 series
route=<unmatched>                   method=GET status=404  n=4   <- 4 junk paths, 1 series
```

The unit suite pins the same property (`tests/unit/test_metrics.py`:
`test_unmatched_paths_share_one_series` asserts six distinct junk paths add
exactly **one** series).

### The full contract

```
aow_http_requests_total{service,method,route,status}
aow_http_request_duration_seconds{service,method,route}
aow_http_exceptions_total{service,method,route,type}
aow_outbox_entries{service,state}                     state: pending|published
aow_outbox_oldest_pending_age_seconds{service}
aow_outbox_publish_failures_total{service}
aow_messages_published_total{service,routing_key}
aow_messages_consumed_total{routing_key,result}       result: stored|duplicate|rejected
aow_message_processing_duration_seconds{routing_key}
aow_consumer_last_message_timestamp_seconds
aow_ingestion_runs_total{source,result}               result: ok|failed
aow_ingestion_last_success_timestamp_seconds{source}
aow_enrichment_requests_total{result}                 result: ready|invalid|unavailable
aow_enrichment_duration_seconds
aow_enrichment_backlog{status}                        status: pending|deferred
```

`aow_messages_consumed_total{result="stored"}` is incremented **after** the
database transaction commits, never on the way in. A message counted on arrival
would be asserting exactly the property M11 rests on, and would be wrong every
time a commit failed. Retryable failures — a database outage — are counted as
**nothing**, because the message returns to the queue; counting there would make
an outage look like a flood of new work. Its effect is visible as queue depth
instead.

---

## 3. Deployment, scraping and exposure

`prometheus` and `grafana` sit on the `internal: true` backend and have **no
route out**. A second nginx, `edge-observability`, straddles the boundary and
publishes Grafana — the same reason `edge` exists, applied again rather than
weakened by putting Grafana on the routable bridge.

Observed with the overlay running (`docker compose ps`):

```
edge                 127.0.0.1:18000->8000/tcp, 127.0.0.1:18080->8080/tcp
edge-observability   127.0.0.1:13000->3000/tcp
grafana              3000/tcp
prometheus           9090/tcp
rabbitmq             4369/tcp, 5671-5672/tcp, 15671-15672/tcp, 15691-15692/tcp
```

(The 1xxxx host ports are a drill-only remap so the isolated project could run
beside an existing stack. The shipped overlay publishes 3000.)

Only the two edges bind the host. Prometheus, Grafana, the RabbitMQ management
UI and the RabbitMQ metrics endpoint are container-internal. Confirmed from the
host:

```
GET http://127.0.0.1:18000/metrics  -> 404      (blocked at the edge)
GET http://127.0.0.1:18000/health   -> 200      (the proxy still works)
127.0.0.1:9090   -> connection refused          (prometheus)
127.0.0.1:3000   -> connection refused          (grafana, direct)
127.0.0.1:15672  -> connection refused          (rabbitmq management)
127.0.0.1:15692  -> connection refused          (rabbitmq metrics)
GET http://127.0.0.1:13000/api/health -> 200 {"database":"ok","version":"12.2.0"}
```

`/metrics` is a scrape target, not a public route: it maps every route, error
count and latency distribution in the system. Prometheus reaches
`api:8000/metrics` directly over `backend`. `tests/unit/test_compose_ports.py`
asserts the allowed publisher set over the shipped files, so a regression fails
without a running stack.

### Scrape targets, live

```
aow-agent          up    http://agent:8100/metrics
aow-api            up    http://api:8000/metrics
aow-consumer       up    http://consumer:9100/metrics
aow-enricher       up    http://enricher:9100/metrics
aow-ingestor       up    http://ingestor:9100/metrics
prometheus         up    http://localhost:9090/metrics
rabbitmq           up    http://rabbitmq:15692/metrics
rabbitmq-queues    up    http://rabbitmq:15692/metrics/detailed?family=queue_coarse_metrics
llm                down  http://llm:8080/metrics
```

`llm` is down because the model server was not started in this drill (the
worktree has no staged GGUF). Its scrape URL is correct; its series were not
observed. `AowModelUnavailable` correctly entered `pending` as a result, which
is the rule demonstrating itself.

### Queue depth needed a second scrape job

RabbitMQ 4.x's default `/metrics` exposes `rabbitmq_queue_messages` **with no
queue label** — it is the sum across every queue. An alert on it would fire on a
healthy backlog and stay silent for a single poisoned message. Per-queue depth
comes from `/metrics/detailed`:

```
rabbitmq_detailed_queue_messages{queue="aow.ingest",vhost="/"}  = 0
rabbitmq_detailed_queue_messages{queue="aow.dlq",vhost="/"}     = 0
rabbitmq_queue_messages{job="rabbitmq"}                         = 0   <- no queue label
```

A unit test fails if anyone reverts the alerts to the aggregated name.

### Pipeline series, live

```
aow_outbox_entries{service="ingestor",state="published"} = 807
aow_outbox_entries{service="ingestor",state="pending"}   = 0
aow_outbox_oldest_pending_age_seconds{service=...}       = 0   (all three producers)
sum by (result) (aow_messages_consumed_total)            = {stored: 807}
```

Consumer `stored` (807) equals ingestor `published` (807).

### Alerts

Eleven rules, all `health=ok`, each with a `for`, a `severity` and an
annotation naming the operator action:

```
aow-availability     AowServiceDown 120s critical | AowModelUnavailable 300s warning
aow-delivery         AowQueueBacklog 600s | AowDeadLetters 300s | AowOutboxStuck 300s critical
                     AowOutboxPublishFailing 300s | AowConsumerRejecting 900s
aow-freshness        AowIngestionStale 1800s
aow-service-quality  AowServerErrors 600s critical | AowHighLatency 600s | AowEnrichmentFailing 900s
```

`AowOutboxStuck` is the M11 signal: accepted records stranded before the broker.

**There is no Alertmanager, deliberately.** An air-gapped single host has no mail
relay or webhook target, so a routing component would be decoration. Rules are
evaluated by Prometheus and surfaced through the `ALERTS` series and a dashboard
panel. Alertmanager belongs in the Kubernetes production path.

### Grafana works offline — and one knob is not documented anywhere obvious

With analytics reporting, update checks, plugin-update checks, the news feed and
plugin preinstall all disabled, Grafana **still** fetched
`https://grafana.com/api/plugins/ci/keys` every 60 seconds. That is the plugin
signature key retriever and it needs its own variable. The full set now applied:

```
GF_ANALYTICS_REPORTING_ENABLED=false
GF_ANALYTICS_CHECK_FOR_UPDATES=false
GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false
GF_NEWS_NEWS_FEED_ENABLED=false
GF_PLUGINS_PREINSTALL_DISABLED=true
GF_PLUGINS_PUBLIC_KEY_RETRIEVAL_DISABLED=true
GF_SECURITY_DISABLE_GRAVATAR=true
```

From inside the internal network: `wget https://grafana.com` → `bad address`;
`wget http://1.1.1.1/` → `Network unreachable`. Boot log has zero `level=error`
and zero `level=warn` lines.

End to end through the provisioned datasource, proving the whole chain:

```
GET /api/datasources/proxy/uid/aow-prometheus/api/v1/query?query=sum(aow_messages_consumed_total)
{"status":"success","data":{"resultType":"vector","result":[{"metric":{},"value":[...,"807"]}]}}
```

---

## 4. Backup and restore

### What is backed up

| Artefact | How | Note |
|---|---|---|
| Postgres | `pg_dump -Fc` | Logical dump, not a volume copy |
| All three producer outboxes | online SQLite `Connection.backup()` inside the owning container | Copying a live WAL file is not a backup |
| RabbitMQ topology | `rabbitmqctl export_definitions` | Exchanges, queues, bindings, policies, users |
| `manifest.json` | checksums, sizes, timestamps, queue depths, image digests | `started_at` is the RPO reference point |

**Queue message bodies are not backed up, and that is stated rather than
implied.** Confirmed envelopes come back through `reconcile.py --replay`;
unpublished ones drain from the producer outbox on restart. The manifest records
each queue's depth at backup time so the uncaptured count is a value rather than
an inference.

This is distinct from the release installer's pre-upgrade dump and rollback,
which restore a previous *release*. This restores the data a running stack
accepted.

### The drill

`bash demos/06_backup_restore.sh` — no arguments, no pre-existing stack. It
builds an isolated Compose project of its own and destroys its volumes, because
doing that to a reviewer's running stack would be indefensible.

Three id sets, each proving a different thing:

* **A** — accepted and stored before the backup. Must survive.
* **C** — accepted with the consumer stopped, so confirmed to the broker and
  **provably absent from the dump**. It returns only via `reconcile.py --replay`.
  Without set C the drill would prove the dump works and say nothing about the
  replay mechanism.
* **B** — accepted after the backup. Defines the RPO boundary.

Verification is from a **separate reader**: `psql` against the restored
database, not the API that accepted the writes, asserting
`SELECT count(*) FROM ingest_log WHERE message_id = '<id>'` **equals exactly 1**
per id, plus the matching domain row. Exactly once, not at-least-once — a row
count could hide a loss and a duplicate cancelling out.

Set B is **asserted, not merely printed**: each id must be absent (a present id
is a hard failure, because it would mean the backup window is not where the
runbook claims), each must have been accepted and committed exactly once
*before* the disruption, and the set must be non-empty — otherwise a regression
that silently stopped accepting writes would make the boundary check pass
vacuously.

### Measured run — 2026-09-24, exit 0

This is the run on the tree as merged, after F9 landed on main. An earlier run
on the same code before that merge gave RPO 24 s / RTO 31 s / 116 s, so the
figures below are representative rather than a lucky sample.

```
A  7b179b67-6a10-4737-bcc1-a3d790030b17  drill alpha one
A  8f3763c0-24ff-4c07-b36e-beff0060a7bc  drill alpha two
A  b240c991-ed9b-4abe-8d10-692c9ab05f09  drill alpha three
C  072a0d62-98de-4885-aca9-dd93edfaa851  drill confirmed unstored
B  91181902-63c2-41b0-8ca0-86669341db85  drill beta one
B  ffc5622a-fd2f-4800-bbef-ca0a5f536c8f  drill beta two

PASS ingest_log count = 1 for each of the three set A ids, with its
     recommendations row (rome/2026-09-23/drill_alpha_{one,two,three})
PASS ingest_log count = 1 for 072a0d62-...  + rome/2026-09-23/drill_confirmed_unstored
PASS absent as expected, outside the backup window: 91181902-...
PASS absent as expected, outside the backup window: ffc5622a-...

RPO reference point (manifest started_at):   2026-09-24T16:09:39Z
last set B id accepted at:                   2026-09-24T16:10:05Z
disruption at:                               2026-09-24T16:10:10Z
records lost (set B):                        2 of 2
RPO span (backup start -> last lost write):  26s
at-risk window (backup start -> disruption): 31s
RTO (restore + verification):                35s
whole drill, wall clock:                     120s
```

The disruption is total: `docker compose down -v`, destroying the database,
broker and all three outbox volumes. The drill asserts the database volume is
actually gone before restoring.

**Measured RPO 26 s, RTO 35 s, drill 120 s wall clock.** The 26 s RPO is a
property of *this drill*, not of the system: in production the RPO is the
backup interval.

---

## 5. Residual limits

Stated, not implied.

* **RabbitMQ credential asymmetry, not closed.** `import_definitions` restores
  users with the **backup's** password hashes. If `RABBITMQ_PASSWORD` changed
  since the backup, the restored broker expects the old password and every
  service fails to authenticate. Postgres does not behave this way — its role
  passwords come from the current `.env` via the migrations. Restore with the
  contemporaneous `.env`, or reset the broker user afterwards.
* **No point-in-time recovery.** Logical dumps only.
* **Backups land on the same disk** as the volumes they protect, by default.
  Copy them off; that is the operator's step and the runbook says so.
* **`manifest.json`'s image list records only running services.** The drill's
  backup is taken with the consumer stopped, so `consumer` is legitimately
  absent from it. Truthful, but not a complete inventory.
* **Restore defaults to the whole stack**, which includes `edge` (publishes
  8080/8000). An isolated restore beside a running stack needs `--services`.
  Documented, not automated.
* **A `docker compose exec` hangs indefinitely** if the daemon disappears
  mid-call. No portable `timeout` exists across Git Bash and macOS, so a CI
  gate should set a job-level timeout.
* **The consumer's ack is not covered by the `stored` counter.** The ack happens
  after the handler returns, so a crash in between counts one `stored` now and
  one `duplicate` on redelivery. That is at-least-once being honest about
  itself; `ingest_log` remains the record of what was written.
* **The operator refresh is not scraped.** `scripts/refresh.sh` runs in a
  short-lived container with no metrics server, so its ingestion runs do not
  reach `aow_ingestion_runs_total`. That path reports through
  `GET /refresh/last` instead.
* **The enrichment backlog gauge freezes during a database outage** rather than
  reading zero — it serves its last cached value, because dialling a new
  connection per scrape would park a thread per scrape.
* **Grafana's UI was not opened in a browser** under the observability edge's
  CSP. The header is served and all three dashboards load through the API with all
  panels intact, but no visual confirmation was made.
* **`llm` metrics were never observed**, only its scrape URL confirmed.
* **One machine, one daemon.** No cross-host or cross-architecture restore was
  attempted.
* **The `record_absent` path** (a producer down at backup time) is unit-tested
  but never exercised live.

---

## 6. Reproducing this

```sh
# monitoring
make monitor                       # or: docker compose -f compose.yml -f compose.observability.yml up -d
                                   # Grafana http://127.0.0.1:3000

# recovery
make backup                        # bash scripts/backup-state.sh
make restore DIR=backups/<id>      # bash scripts/restore-state.sh backups/<id>
make backup-restore                # the whole drill, ~2 minutes

# gates
docker build -q -f tests/Dockerfile -t aow/tests:dev . && docker run --rm --network none aow/tests:dev
```

Gates run on this change set: **832 unit tests** under `--network none`;
`ruff check` and `ruff format --check` over `services tests scripts` clean;
every Compose overlay renders against `.env.example`; `bash -n` over every
script; `IMAGES.lock` matches every pinned reference (9 images); `promtool check
config` and `check rules` pass.
