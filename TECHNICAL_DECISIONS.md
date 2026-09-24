# Technical decisions

The README's *Technical choices* table gives the headline decisions and their
rejected alternatives. This file records the rest: the choices that are only
visible in the code, and — more usefully — the six decisions that were **made
or reversed during the build**, with what forced each one.

[ASSIGNMENT.md](ASSIGNMENT.md) remains the source of truth for the brief, our
decomposition into requirement IDs, and which choices are ours rather than the
reviewer's.

---

## What changed during the build, and why

These are the ones worth reading. Each was a working assumption that met
reality.

### 1. Places moved from OpenStreetMap to Wikidata

**Assumed:** Overpass, bounded queries, retries.
**Found:** the first responses were **HTTP 200 with an empty element list and a
`remark` saying the server had timed out internally** — a silent failure that
looks exactly like "this city has no museums". After fixing that (check the
remark, split into groups of four selectors, add three mirrors), all four public
instances began refusing connections or timing out, reproducibly, for over an
hour.
**Decided:** Wikidata SPARQL as the default, Overpass kept behind
`--places-source osm`.
**Cost:** Wikidata holds notable venues, not every café — narrower data, and the
README says so. Each row records its own source and licence, so the two are
distinguishable in the database.
**Lesson kept in the code:** a free shared endpoint's success status is not
evidence of success. `overpass_fetch` checks the body.

### 2. The agent stopped using the model to choose what to look up

**Assumed:** tool-calling with `--jinja`, a call budget, a retry.
**Found:** on CPU that is three or four sequential generations per question —
30–90 s — and non-deterministic in front of a reviewer.
**Decided:** the router resolves city, dates, intents and coverage in code, runs
the SQL, and spends at most one model call phrasing the rows. Named activity
verdicts are rendered directly from their stored daily scores.
**Result:** E1 answers in ~5 s, E2 in ~20 s, and a question outside coverage is
refused *without a model call at all*.

### 3. The enricher stopped being bound to the weather stream

**Assumed:** a second queue binding on `weather.daily` feeding the enricher.
**Found:** that creates a second delivery branch, and a partial fan-out fails
silently — half the records enriched, nothing to detect it with.
**Decided:** the consumer creates `pending` rows; the enricher **polls** them.
One delivery path, one thing to reason about.

### 4. The retry policy split by failure class

**Assumed:** a flat `attempts < N` cap.
**Found:** that strands rows permanently during a long model outage — the exact
opposite of the durability story the README tells.
**Decided:** an *unavailable* model (connection error, timeout, 5xx) is retried
indefinitely at a bounded rate and consumes no attempt. Only *invalid output*
counts against the cap, and the count lives in the database so a restarted
enricher cannot reset it.

### 5. Qwen3's thinking mode had to be disabled

**Measured:** with thinking on, the model spends its whole token budget in
`reasoning_content` and returns **empty content** — 4.2 s and no answer. With
`enable_thinking: false`, 1.2 s and a clean one.
**Set as an env var**, not a CLI flag, because the flag's JSON argument loses its
quoting through YAML and Docker's argument splitting.

### 6. Two bugs the drills caught, not the tests

* **The history trigger** referenced `NEW.forecast_date` in a `CASE` branch that
  only applies to `weather_daily`. PL/pgSQL rejects a column reference that does
  not exist on the table currently firing, *even in a branch never taken*, so
  every `facts` patch failed. It now addresses the row through `to_jsonb(NEW)`.
  The failure was visible as a message being correctly requeued for 40 seconds
  and then stored intact once fixed — which is the M11 guarantee working.
* **The redrive loop** redrove the same unfixable message 50 times in one run: a
  redriven message that fails again is back in the DLQ within milliseconds, fast
  enough for the same loop to re-read it. Each message now gets one attempt per
  run.

---

## Decisions visible only in the code

| Decision | Why |
|---|---|
| **`message_id` is a UUIDv5** over (routing key, natural key, as-of) for snapshot records | Replaying the committed snapshot on every boot produces the same ids, so the outbox's unique constraint deduplicates them. A refreshed forecast has a new as-of, so it gets a new id and a real delivery. |
| **`provider` is not in `weather_daily`'s primary key** | A second provider must *replace* a day, not shadow it — otherwise the join to `recommendations` stops being unique. |
| **`recommendations` is keyed `(city_id, forecast_date, activity)`** | An activity the user types is then a row like any other, with no special case anywhere. |
| **Weather upsert carries `WHERE EXCLUDED.as_of > weather_daily.as_of`** | At-least-once delivery means redelivery in any order; an older forecast must never overwrite a newer one. |
| **Three database roles, with narrow write grants** | Collected records are revised, not deleted. The consumer's `aow_writer` role can delete `events` rows only to remove generated demo samples when demo mode ends (`003_demo_events.sql`). The API, agent and enricher use a SELECT-only role. |
| **The outbox is SQLite with `synchronous=FULL`** | The point is to survive Postgres being unreachable, so it cannot be a table in Postgres; and to survive the process dying, so it cannot be a buffer in memory. One fsync per accepted record is the cost we chose. |
| **`check_same_thread=False` on the outbox connection** | The API accepts on request threads and publishes on a background one. Every caller holds a lock; nothing is actually concurrent. |
| **The API returns `202`, never `200`, for a write** | The row genuinely is not written yet. `GET /outbox/{message_id}` is how a caller — or a drill — follows it. |
| **nginx resolves upstreams through a variable** | nginx refuses to *start* if a `proxy_pass` hostname cannot be resolved at config load, which would make `edge` crash-loop whenever a backend is not up yet. |
| **Streamlit's `serverAddress`/`serverPort` point at 8080, not 8501** | Its XSRF check compares the websocket Origin against those. Without it the page hangs on "Connecting…" — invisibly, until first boot in a browser. |
| **`/outbox` is created and chowned in the Dockerfile** | Docker gives a fresh named volume the ownership of the image's directory at that path. Without it the volume is root-owned and a non-root service cannot accept anything. |
| **Intent matching is word-boundary, not substring** | `"eat"` is inside `"weather"`, so every weather question silently acquired the `places` intent. There is a regression test named after it. |
| **Indoor activities are scored as the inverse of outdoor comfort, floored at 25** | Bad weather makes staying in *more* suitable, which is what the brief's own example implies — and the reasons text has to agree with the band, or the model is handed a contradiction and will faithfully write one. |

---

## What was cut, and what it cost

Pre-agreed cut order, taken in this order as the deadline approached:

| Cut | Cost |
|---|---|
| Itinerary editing → generate-and-save only | M9's editing affordance. Building and saving both work. |
| The markers map page | Visual breadth on M10; the forecast chart, the heatmap and the coverage banner carry it. |
| Marine data and surfing | No marine provider, so surfing is not among the scored defaults. Typed as a free-text activity it is answered from the weather on hand, with that caveat stated. |
| B1, B3 (bonuses) | No integration-test container beyond CI's Compose job; the demo scripts are the integration evidence. |
| B2 (bonus) | Met. Prometheus and Grafana ship as an opt-in overlay (`compose.observability.yml`), pinned and air-gapped, with request/error/latency and pipeline metrics, 11 alert rules and two provisioned dashboards. No Alertmanager: a single air-gapped host has nowhere to route to. |

Never cut, and all verified: one-command startup, the full outbox → queue →
consumer → Postgres path, the coverage gate and as-of footers, both example
questions answering, `PATCH` → revision 2 with history, Trivy in CI, the README.
