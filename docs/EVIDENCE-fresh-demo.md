# Evidence: fresh-environment start, rerun, offline operation and the example questions

This file records what was actually run, on what, and what came back. It is a
log, not a summary: every claim below is followed by the command that produced
it and the output that came back. Steps that could not be run are recorded as
**NOT RUN**, with the reason. Nothing that was not executed is written up as a
pass.

- **Commit under test:** `a21dff9d4b38cdfead8be717db4599fc602ba668` (`review/bootstrap-proof`, branched from `main` at `a21dff9`)
- **Scope of this branch:** `scripts/bootstrap.sh`, `tests/unit/test_bootstrap.py`, this file. `README.md` and `DEVOPS_REVIEW.md` are deliberately untouched and left for integration.
- **Date of the run:** 2026-09-25 (UTC timestamps inline)

---

## 1. Environment

### 1.1 Why a nested engine, and what "fresh" means here

The machine available for this run already had another session's stack on it:
the `aow` Compose project was running, holding host ports 8080 and 8000, and
the engine held 136 images (19.4 GB) including every `aow/*` image the build
would otherwise have had to produce. Running the proof there would have proved
nothing about a fresh environment — the pull and the build would both have been
no-ops, and the project name, ports and volumes would have collided with the
other session.

So the whole proof runs inside a **disposable nested Docker engine**
(Docker-in-Docker) with its own image store, its own volumes, its own networks
and its own port space. It is created empty and destroyed afterwards. The
outer engine's containers, images and volumes are never touched.

```sh
docker volume create aow-review-dind-lib
docker run -d --name aow-review-dind --privileged \
  -v aow-review-dind-lib:/var/lib/docker \
  -e DOCKER_TLS_CERTDIR= \
  docker:28-dind --storage-driver overlay2
```

Verified empty before anything else ran:

```
$ docker exec aow-review-dind docker info --format 'Images={{.Images}} Containers={{.Containers}}'
Images=0 Containers=0
```

The repository was transferred as a **git bundle** and cloned inside, so the
working tree is a genuine fresh clone — tracked files only, no `.env`, no
staged model, no build cache:

```
$ docker exec aow-review-dind git clone --branch review/bootstrap-proof /tmp/aow.bundle /work
$ docker exec aow-review-dind git -C /work rev-parse HEAD
a21dff9d4b38cdfead8be717db4599fc602ba668
absent:  .env
absent:  dist
absent:  model gguf
models/: No such file or directory
```

### 1.2 Versions

| layer | value |
|---|---|
| Outer host | Windows 11 Pro build 26200 |
| Outer Docker | Engine 29.8.0, Compose v5.5.1 (WSL2 backend) |
| **Nested engine (the host under test)** | **Docker Engine 28.5.2, Compose v2.40.3** |
| Nested OS | Alpine Linux v3.22, kernel 6.18.33.2-microsoft-standard-WSL2 |
| Nested CPU / memory | 32 vCPU, 15.4 GiB reported to Docker |
| Nested storage driver | overlay2, 924 GB free |
| DinD image | `docker@sha256:2a232a42256f70d78e3cc5d2b5d6b3276710a0de0596c145f627ecfae90282ac` (`docker:28-dind`) |

**Deviation from a true reviewer host, recorded honestly:** the nested engine
is Alpine and ships no `bash`, `curl` or `jq`. `bash` was installed with
`apk add bash` because `scripts/bootstrap.sh` requires it — the README already
names `bash` as the one prerequisite beyond Docker for the scripted path, and
the typed-out path in the README needs none of it. `curl` and `jq` were
installed for **my** verification of the API responses, not because the project
needs them. No other package was added.

---

## 2. First connected bootstrap

The command under test, run from the fresh clone inside the disposable engine:

```sh
bash scripts/bootstrap.sh
```

### 2.1 Run 1 — steps 1 to 3 pass, then I aborted the pull

Run 1 started `2026-09-25T11:23:36Z`. **I aborted it myself** after 460 s,
during step 4. Steps 1 to 3 are real evidence and are recorded as such; the
abort is recorded as an abort, not as a pass.

```
== 1/6  Prerequisites
   PASS Docker engine 28.5.2 is reachable
   PASS Compose plugin v2.40.3

== 2/6  Resources
   PASS Docker memory: 15.4 GiB
   fetching the pinned helper image (python:3.12-slim, ~130 MB)...
   PASS Docker free disk: 924.2 GiB

== 3/6  Configuration (.env)
   PASS created .env from .env.example with 5 generated password(s)

== 4/6  Staging
   pulling the four pinned images (~2.2 GB); this is the long one...
 edge Pulled
 rabbitmq Pulled
 postgres Pulled
```

**Why I aborted.** `ghcr.io` was serving the fourth image
(`ghcr.io/ggml-org/llama.cpp@sha256:fb8f521c…`, 1.2 GB) at **53 kB/s**,
projecting to about 6.3 hours. Measured, not guessed:

```
$ docker exec aow-review-dind sh -c 'a=$(du -sk /var/lib/docker|cut -f1); sleep 60;
                                     b=$(du -sk /var/lib/docker|cut -f1); echo "$(( (b-a)/60 ))KB/s"'
53KB/s
```

I checked whether the link or the registry was at fault, because the answer
changes what it means. The link was fine:

| from | source | throughput |
|---|---|---|
| nested engine | general CDN, 3.6 MB file | 5.3 MB/s |
| outer host | same file | 7.1 MB/s |
| nested engine | HuggingFace, model blob | 8.1 MB/s |
| nested engine | **ghcr.io, llama.cpp image** | **53 kB/s** |
| nested engine | **PyPI, wheels during the UI build** | **54 kB/s** |

So this was upstream throttling of two specific hosts on the day, not a
property of the project. It was also transient: the same image pulled to
completion on the next attempt.

**What the abort proved on its own.** A failed pull is handled correctly —
exit 1, the cause named, and a next step that is actually actionable:

```
bootstrap failed: could not pull the pinned images.
next: check the network and rerun, or rerun with --offline on a machine that is already staged
RUN1 exit=1 elapsed=460s
```

### 2.2 `.env` generation, checked rather than assumed

This is the step that writes five passwords to disk, and it had no test at all
before this branch. Inspected directly after run 1:

```
$ stat -c '%a' .env
600

$ grep -n change-me .env
1:# Copy to .env and replace every change-me value with a distinct password.
```

Five `KEY=change-me` lines in `.env.example` became five distinct random
values. The single surviving `change-me` is the word inside the comment on
line 1, which is correct: the generator matches whole `KEY=change-me` lines
only, and must not rewrite prose. No password appeared anywhere in the
script's output.

Fingerprint recorded here so later runs can be checked against it:

```
sha256(.env) = 8b09ff7da78c412a8f9246fabe1185877470a85163e356c5f82640dda4fdaaf2
mtime        = 2026-09-25 11:23:44.350407436 +0000
mode         = 600
```
---

## 3. The stack starts, and the rerun is inert (M5)

### 3.1 Run 1b — staging completed, stack healthy

Rerun of the same command after the registry recovered. It took the
existing-`.env` branch, since run 1 had already created one.

```
RUN1B exit=0 elapsed=2409s
```

```
== 1/6  Prerequisites
   PASS Docker engine 28.5.2 is reachable
   PASS Compose plugin v2.40.3
== 2/6  Resources
   PASS Docker memory: 15.4 GiB
   PASS Docker free disk: 923.5 GiB
== 3/6  Configuration (.env)
   .env exists; keeping it exactly as it is. Nothing was generated.
   PASS .env has no change-me placeholders left
== 4/6  Staging
   PASS the four pinned upstream images are here
   PASS the model matches models.lock (an already-staged file is re-hashed, not re-fetched)
   PASS service, UI and demos images built
== 5/6  Start
   PASS Docker Compose started the stack
== 6/6  Waiting for the stack (up to 900s)
   PASS every service is healthy (5s)
== Ready
   UI   http://localhost:8080
   API  http://localhost:8000/docs
```

Of the 2409 s, almost all was download: the four pinned images from `ghcr.io`
and Docker Hub, and then ~38 minutes of the UI image's `pip install`, with
**PyPI serving wheels at 54 kB/s** (pyarrow alone is 50 MB). On an unthrottled
link this is a few minutes. The build itself was never the bottleneck.

Final state, from Compose:

```
agent|running|healthy|0      llm|running|healthy|0
api|running|healthy|0        migrate|exited||0
consumer|running||0          postgres|running|healthy|0
edge|running|healthy|0       rabbitmq|running|healthy|0
enricher|running||0          ui|running|healthy|0
ingestor|running||0
```

The API and UI both serve:

```
$ curl -s http://127.0.0.1:8000/health
{"status":"ok","database":true,"outbox":{"total":0,"pending":0}}
$ curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/
200
```

and the database holds real data, not an empty schema:

| table | rows |
|---|---|
| `weather_daily` | 80 (5 cities × 16 days) |
| `recommendations` | 1360 |
| `events` | 55 |
| `places` | 620 |
| `facts` | 81 |

Coverage window: **2026-09-23 to 2026-10-08**, `as_of` 2026-09-23 18:16 UTC.
The run date, 2026-09-25, sits inside it, so E1's "tomorrow" (2026-09-26) is
answerable and the out-of-coverage control is genuinely out of coverage.

### 3.2 Run 2 — the rerun changes nothing

```
RUN2 exit=0 elapsed=21s
```

```
   .env exists; keeping it exactly as it is. Nothing was generated.
   PASS .env has no change-me placeholders left
   the four pinned images are already on this host; skipping the pull.
   PASS the model matches models.lock (an already-staged file is re-hashed, not re-fetched)
   PASS every service is healthy (6s)
```

Measured before and after, rather than asserted:

| | before | after |
|---|---|---|
| `sha256(.env)` | `8b09ff7d…a4fdaaf2` | **`8b09ff7d…a4fdaaf2`** |
| `.env` mtime | `11:23:44.350407436` | **`11:23:44.350407436`** |
| `.env` mode | 600 | **600** |
| `sha256(model)` | `d2387ca2…0d9bc7b5` | **`d2387ca2…0d9bc7b5`** |
| named volumes | 6 | **6, same names** |
| `weather_daily` / `recommendations` / `events` / `places` / `facts` | 80 / 1360 / 55 / 620 / 81 | **identical** |
| duplicate `message_id`s | 0 | **0** |
| restart counts | 0 / 0 / 0 | **0 / 0 / 0** |

The `.env` mtime is unchanged to the nanosecond, so the file was not merely
rewritten with the same contents — it was never opened for writing. `ingest_log`
grew from 1025 to 1049 rows, which is the ingestor's own loop continuing to
accept records; duplicates stayed at 0, which is the idempotent upsert doing
its job.

**21 seconds** is the honest answer to "what does it cost to rerun this?"
---

## 4. Prerequisite and usage failures

Run in a second, separate clone (`/drills`) inside the same disposable engine,
so a failing run could not disturb the staged one. All four exit before any
container is created.

```sh
git clone --branch review/bootstrap-proof /tmp/aow.bundle /drills
```

| drill | command | exit | what the reviewer sees |
|---|---|---|---|
| docker absent | `env PATH=/usr/bin:/bin bash scripts/bootstrap.sh --no-start` | **1** | `bootstrap failed: docker is not on PATH.` + `next: install Docker Desktop …` |
| daemon down | `DOCKER_HOST=unix:///nonexistent/docker.sock bash scripts/bootstrap.sh --no-start` | **1** | `bootstrap failed: the Docker CLI is installed but the daemon is not reachable.` + `next: start Docker Desktop, or 'sudo systemctl start docker' …` |
| bad `--timeout` | `bash scripts/bootstrap.sh --timeout abc` | **2** | `--timeout takes a number of seconds` |
| unknown option | `bash scripts/bootstrap.sh --bogus` | **2** | `unknown option: --bogus`, then the usage text |

Full output:

```
########## DRILL A: docker not on PATH ##########

== 1/6  Prerequisites

bootstrap failed: docker is not on PATH.
next: install Docker Desktop (Windows/macOS) or Docker Engine with the Compose plugin (Linux), then rerun this script
EXIT=1

########## DRILL B: docker CLI present, daemon unreachable ##########

== 1/6  Prerequisites

bootstrap failed: the Docker CLI is installed but the daemon is not reachable.
next: start Docker Desktop, or 'sudo systemctl start docker' on Linux, then rerun this script
EXIT=1

########## DRILL C: bad --timeout value ##########
--timeout takes a number of seconds
EXIT=2

########## DRILL D: unknown option ##########
unknown option: --bogus

First run, in one command.
EXIT=2
```

These match the exit codes the script documents in its header (0 healthy,
1 a failed step, 2 bad usage), and each failure names both a cause and a next
command.
---

## 5. Offline operation (M6)

The engine was **physically disconnected from the network** for this section —
not simulated, not a flag. Everything from here to §6 ran with no route out.

```sh
docker network disconnect bridge aow-review-dind
```

Verified immediately afterwards, from inside the engine:

```
-- external IP --   http=000   curl exit 7   (could not connect)
-- ghcr.io --       http=000   curl exit 6   (could not resolve host)
-- pypi.org --      http=000   curl exit 6   (could not resolve host)

-- interfaces --
lo        127.0.0.1/8
docker0   172.18.0.1/16
br-…      172.19.0.1/16, 172.20.0.1/16, 172.21.0.1/16
```

No `eth0`. DNS and TCP both fail. The only interfaces left are loopback and
the engine's own internal bridges.

### 5.1 `bootstrap.sh --offline`, with no network at all

```
RUN3 (--offline, host disconnected) exit=0 elapsed=3s
```

```
== 4/6  Staging
   --offline: not pulling, not building. Checking this machine is already staged.
   PASS .env renders every Compose file
   PASS every image the stack needs is already local
   PASS the tooling images are here too
models/Qwen3-1.7B-Q4_K_M.gguf is already here, verifying it
  OK  d2387ca2dbfee2ffabce7120d3770dadca0b293052bc2f0e138fdc940d9bc7b5
   PASS the staged model matches models.lock
== 5/6  Start
   PASS Docker Compose started the stack
== 6/6  Waiting for the stack (up to 900s)
   PASS every service is healthy (0s)
```

**Three seconds, exit 0, no network.** The model was re-hashed against
`models.lock` rather than re-fetched, which is the check that means anything
offline. The two `PASS` lines about images are the split introduced in §7.5.

### 5.2 The application network has no way out

`demos/01_offline.sh` (exit 0, 40 s) opens with the structural check, which is
the one that actually carries the claim:

```
== 1. Docker itself forbids a route out of the application network
   aow_backend Internal = true
   PASS every application service is on a network with no gateway
   which containers are attached to a routable network:
   aow_frontend     aow-edge-1
   aow_egress
```

Confirmed independently for the model server, which is the container a reviewer
would most want to be sure about:

```
$ docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' <llm>
aow_backend            Internal=true

$ docker inspect -f 'gateways=[{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}]' <llm>
gateways=[]
```

One network, `internal: true`, and **no gateway at all** — so the kernel has no
default route to give it. This is enforced by Docker, not by application code.

**A caveat about that demo, recorded because it matters.** Its §2 probes each
service by running `python` inside it, falling back to `curl`. The `llm` image
has neither:

```
OCI runtime exec failed: exec: "python": executable file not found in $PATH
   llm       no route out
   agent     no route out (OSError)
   consumer  no route out (OSError)
   api       no route out (OSError)
```

For `agent`, `consumer` and `api` the `(OSError)` is a real probe that really
failed to connect. For `llm` the line is **vacuous** — it prints "no route out"
because neither probe could run, and it would print the same thing if the
container were fully connected. The claim still holds, but it holds on the
structural check above, not on that line. This is in `demos/`, outside this
branch's owned files; it is in the integrator notes at §10.5.

I also tried a shell-only egress test (`/dev/tcp`) and it proved nothing: these
images use dash/busybox, which has no `/dev/tcp`, so the control container
`edge` — which *can* route — failed it too. Recorded so nobody repeats it.

### 5.3 The example questions, offline

`verify-examples.sh` re-run with the network still cut: **all checks passed**.
E1, E2 and the out-of-coverage control all behaved exactly as in §6, with
`llm_called: true` on both examples. The local model answered with no network.

### 5.4 The demo suite, offline

| demo | ID | exit | time |
|---|---|---|---|
| `demos offline` | M6 | **0** | 40 s |
| `demos questions` | M7/M8 | **0** | 39 s |
| `demos update` | M12 | **0** | 4 s |
| `demos no-data-loss` | M11 | **1**, then 0, 0 | 67 / 76 / 67 s |

`questions` covered E1, E2, Lisbon history, London sports, Reykjavik running,
Tel Aviv beaches and surf, and three refusals (a date past the window, a city
never collected, a category with no rows). E1 and E2 both reported
`model_called=True`.

`update` (M12) is worth quoting, because on a freshly built stack its
assertions are meaningful rather than tautological:

```
== The record before
   revision 1  |  Apollo Victoria Theatre
== Submitting a correction
   {"accepted":true,"message_id":"9d029242-…","follow":"/outbox/9d029242-…"}
   202 Accepted, not 200 OK: the row is not written yet.
== Following it through
   PASS the consumer stored it
== The record after
   revision 2  |  Apollo Victoria Theatre [corrected 12:26:47Z]
   PASS revision is now 2
   PASS 1 history row(s) on record
```

### 5.5 `no-data-loss` is flaky — and the data was never lost

The first run **failed (exit 1)** on one assertion out of eleven:

```
FAIL  the traced ID never reached the broker during the consumer outage
trace: {"message_id":"e90ae89c-…","published_at":null,"stored":false}
```

Two more runs of the identical command passed, exit 0. So: **1 failure in 3
runs.**

The system was not at fault, and this is visible in the drill's own output. The
very next assertion in the failing run passed, with the same id:

```
PASS  the record stored after the consumer came back
trace: {"message_id":"e90ae89c-…","published_at":"2026-09-25T12:27:17.465981+00:00",
        "stored":true,"stored_at":"2026-09-25T12:27:18.816091Z"}
```

The record was accepted, published and stored. The assertion read
`published_at` before the ingestor's ~2-second outbox drain had run, and there
is no wait or retry around it. Every run reported
`duplicate message_ids in the database: 0`.

This matters out of proportion to its size: `no-data-loss` is the headline
proof for M11, and a reviewer running it once has a **1-in-3 chance of seeing a
red FAIL on a system that is working correctly**. It is in `demos/`, outside
this branch's owned files — integrator notes at §10.5.

The other ten assertions passed in all three runs, including the ones that
carry the guarantee: acceptance into the durable outbox while Postgres is down,
a separate database session seeing the committed id, the backlog replaying
after the broker returns (`attempts: 4`), and a poison message being
quarantined in the DLQ, redriven, and re-quarantined without loss or loop.
---

## 6. The brief's two example questions (E1, E2)

The two illustrative questions from `ASSIGNMENT.md` §1, verbatim:

> Q: What is the weather tomorrow in Rome?
> Q: What activities can I do with my wife this week in London? We like concerts, Shopping and fine dining.

They are asked directly against the API rather than through a demo, because
`demos/01_offline.sh` prints these two answers and **asserts nothing about
them** — it prints `as of: (none)` for a missing timestamp and still passes
(§10.5). The checks below are therefore made here:

```sh
curl -s -X POST http://127.0.0.1:8000/agent/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"What is the weather tomorrow in Rome?"}'
```

Each answer is required to: come back non-empty; be grounded in stored rows of
the right kind; carry an as-of timestamp and coverage window; and be written by
the local model rather than the degraded fallback.

### E1 — "What is the weather tomorrow in Rome?"

> Tomorrow in Rome, 2026-09-26, the weather will be warm with high temperatures
> (27°C) and a 3% chance of light rain. The low will be 17°C. The sun will be
> visible for 11.4 hours. The wind will be 12 km/h.

```
as_of      : weather as of 2026-09-23 18:16 UTC · forecast covers 2026-09-23 to 2026-10-08 · sources: open-meteo
city       : rome
dates      : 2026-09-26
llm_called : true
rows_used  : {"forecast":1,"recommendations":18,"places":0,"events":0,"facts":0,"venues":0}
note       : (absent)

   PASS  a non-empty answer came back
   PASS  grounded in stored forecast rows (1)
   PASS  carries an as-of timestamp / coverage window
   PASS  the local model was called (llm_called=true)
   PASS  the model's own wording passed the grounding check
```

"Tomorrow" resolved to 2026-09-26 against the run date, and that date is inside
the stored window.

### E2 — "What activities can I do with my wife this week in London?…"

> You can enjoy concerts, shopping, and fine dining in London this week.
> Concerts are scheduled at LSO St Luke's (Jerwood Hall) on 2026-09-25.
> Shopping is available at places like Smithfield Poultry Market and Coal Drops
> Yard. Fine dining options include Akoko, Alain Ducasse at The Dorchester, and
> Bar Italia. The weather is generally mild with some rain, so check the
> forecast for specific days.

```
as_of      : weather as of 2026-09-23 18:16 UTC · forecast covers 2026-09-23 to 2026-10-08 ·
             sources: open-meteo, Wikidata (CC0), London Symphony Orchestra official What's On listing
city       : london
dates      : 2026-09-25 to 2026-10-01
llm_called : true
rows_used  : {"forecast":7,"recommendations":91,"places":18,"events":1,"facts":0,"venues":0}
note       : (absent)

   PASS  a non-empty answer came back
   PASS  grounded in stored recommendations rows (91)
   PASS  carries an as-of timestamp / coverage window
   PASS  the local model was called (llm_called=true)
   PASS  the model's own wording passed the grounding check
```

Both interests in the question are answered from named stored rows, and the
`sources:` list names where each came from.

### Control — a date outside the coverage window

> I have no weather data for 2027-07-04. The stored forecast covers 2026-09-23
> to 2026-10-08, and I do not guess beyond it. Refresh the snapshot while
> connected to extend it.

```
llm_called : false
rows_used  : all zero

   PASS  used no rows, as it must for a date outside coverage
   PASS  refused in code without calling the model (llm_called=false)
```

The refusal happens in code, before the model is reached — which is the right
design: a model asked to phrase "no data" is a model given room to invent some.

### 6.1 The grounding guard, observed working — and observed missing one

This is the most interesting thing the live run turned up, and it cuts both
ways.

**It works.** Across repeated runs of E2 the guard rejected the model's wording
several times and fell back to rendering the stored rows, each time naming what
it caught:

```
"claims a festival with no stored event row"
"presents the place 'Aeolian Hall' as a scheduled event"
"describes the place 'Akoko' beyond its stored category"
```

In each case the answer a user receives is still grounded and still carries its
as-of stamp. That is the brief's "never invent events" rule enforced at
runtime, not just asserted in a README.

**It has a gap.** In 1 of 8 runs — and once more in the offline run — the model
produced this, and the guard let it through:

> Concerts are scheduled on 2026-09-25, 2026-09-26, 2026-09-27, 2026-09-29, and
> 2026-09-30.

The stored rows do not support it. Inside E2's window (2026-09-25 to
2026-10-01) London has exactly **two** event rows:

```
 2026-09-25 | Free Friday Lunchtime Concert           | concert
 2026-10-01 | Niall Horan: DINNER PARTY Live on Tour  | concert
```

There is nothing on 09-26, 09-27, 09-29 or 09-30. The answer also reported
`rows_used.events: 1`, so the four extra dates came from the model, not the
data — most likely by reading the per-day "best rated activity" recommendation
rows, which do exist for every day, as if they were scheduled events.

**Assessment.** The guard catches several classes of invention but not
date-scoped event claims, which is the class the brief calls out by name. It is
intermittent (roughly 1 in 8 on this model and prompt), so a reviewer may or
may not see it. This is in `services/agent/`, outside this branch's owned
files, so it is reported rather than fixed — but of everything found in this
review it is the one I would fix first, because it is the single rule the brief
states most plainly.

### 6.2 The model server restarted once under load

While running eight E2 requests back to back, the model server exited and was
restarted by its policy:

```
llm   RestartCount=1   OOMKilled=false   Health=healthy
agent WARNING llm unavailable: ('Connection aborted.', RemoteDisconnected(...))
agent WARNING llm unavailable: HTTP 503
```

Memory was not the cause (1.9 GiB of a 3 GiB cap) and no error survived in the
container's logs, so **I could not determine why it exited.** Recorded as an
observation, not a diagnosis.

What the system did about it is the part that matters, and it is the designed
behaviour working: the container came back within seconds under
`restart: unless-stopped`, and for the three requests in between, the agent
degraded to rendering the stored rows and kept answering with an as-of stamp
rather than failing. That is the "LLM failure never blocks" rule and the B3
recovery claim, both demonstrated by accident.
---

## 7. Defects found and fixed in `scripts/bootstrap.sh`

Nine changes, all inside this branch's owned files. Each one is a case where a
reviewer on a fresh machine would have been given a wrong answer, a silent
wait, or an instruction that does not work. Every fix has a test in
`tests/unit/test_bootstrap.py` that fails without it.

### 7.1 A stack that is healthy could be reported as unhealthy for 15 minutes

The health loop asks Compose for a Go template:

```sh
dc ps --all --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}' 2>/dev/null || true
```

A Compose plugin too old to render that template writes nothing and exits
non-zero, and the error is discarded. Every service then matches the
`[ -z "$line" ]` branch and is reported `(no container)`, so the script waits
out the **full 900-second default** and then blames the containers:

```
still waiting after 900s for: postgres(no container) rabbitmq(no container) …
next: docker compose logs --tail 50 postgres rabbitmq migrate llm …
```

— on a stack that is up and healthy. The logs the reviewer is sent to will show
nothing wrong, because nothing is wrong except the plugin.

The version gate above it only enforces the *major* version, which is the floor
for the top-level `name:` key, not for `ps --format`, `config --images` or
`up --pull`. Rather than guess a version number, the fix probes the capability
once, before the loop, and fails in the first second with the real cause:

```
bootstrap failed: this Compose plugin (v2.20.0) cannot render 'docker compose ps --format',
which this script needs to tell a healthy service from a starting one.
next: upgrade the Compose v2 plugin (v2.21 or newer), then check it with
      'docker compose ps --format "{{.Service}}|{{.State}}"'
```

Test: `test_unsupported_compose_ps_format_fails_fast_and_says_so`.

### 7.2 A crash-looping consumer was reported as a healthy stack

`ingestor`, `consumer` and `enricher` build from `services/Dockerfile`, which
declares no `HEALTHCHECK`, and `compose.yml` gives them none. The loop treated
an empty health field as "nothing to wait for":

```sh
case "$health" in
  '' | healthy) ;;
  *) pending="$pending $svc($health)" ;;
esac
```

All three carry `restart: unless-stopped`. Docker's restart backoff starts at
100 ms and this loop samples every 5 s, so a consumer that cannot reach the
broker is in state `running` at most sample instants. One such sample was
enough to print `PASS every service is healthy`, print the URLs, and **exit 0**
— with the consumer, the only writer to the database, in a crash loop. The
reviewer gets a working UI backed by an empty database and no indication why.

This is the worst class of defect here, because it is a wrong answer rather
than a slow one, and the health wait exists precisely to catch it.

The fix uses the signal Compose does not surface in `ps`: the container's
restart counter.

**My first attempt at this fix was wrong, and the live stack caught it.**
I gated on `RestartCount > 0`. That counter is cumulative for the life of the
container, so any container that had ever restarted would be reported unhealthy
by every later run, forever. That is not hypothetical — after the no-data-loss
drill, which stops and starts `consumer` four times by design, the live stack
showed:

```
ingestor  RestartCount=0
consumer  RestartCount=6     <-- and perfectly healthy
enricher  RestartCount=0
```

Shipping that would have made `bootstrap.sh` hang for 900 s on any machine
where the proofs had been run — a worse defect than the one it fixed.

What is meaningful is a restart **while the script is watching**. The first
sample of a no-healthcheck service now only records a baseline and reports
`settling`; a later sample above that baseline reports `restarting`. The
baseline is never moved afterwards, so a service that restarts once stays
flagged for the rest of the wait rather than being declared settled between two
restarts. The cost is one extra poll interval on a healthy stack — measured at
6 s instead of 0 s, which is a fair price for the guarantee.

A second bug surfaced on the way: the helper memoised into a shell variable but
was called inside `$(…)`, which runs in a subshell and discards every update.
Every poll therefore looked like the first one, and a healthy stack waited out
the full timeout. The helpers now set globals and are called directly. Both
mistakes are the kind that only a live run finds.

**Known limit, stated rather than hidden:** Docker's restart backoff grows to
60 s, so a loop slower than the remaining wait can still go unseen. This
catches the fast loop a misconfigured broker or database produces, which is the
case that was reporting success.

Tests: `test_crash_looping_service_without_a_healthcheck_is_not_healthy`
(a growing counter → `restarting`, exit 1),
`test_a_service_that_restarted_in_the_past_is_still_healthy` (a fixed count of
6 → healthy, the regression test for the mistake above), and
`test_healthy_stack_is_reported_healthy` for the success path, which nothing
covered before — a change that made the loop never succeed would have passed
the entire old suite.

### 7.3 The `next:` line after a timeout was not a runnable command

```sh
die "still waiting after ${TIMEOUT}s for:$pending" \
    "docker compose logs --tail 50$(… ) -- or keep waiting with 'bash scripts/bootstrap.sh --wait-only --timeout 600'"
```

`die` prints its second argument verbatim after `next: `, so the reviewer saw
one line ending in ` -- or keep waiting with '…'`. Pasting it — which is what
`next:` invites — fails with `no such service: or`. The `--` makes it worse by
looking like deliberate argument-terminator syntax rather than prose.

This is the failure a reviewer is most likely to meet, because the `llm`
container legitimately reports unhealthy for about three minutes while it loads
the model. The hint now goes on its own line and `next:` carries only the
command.

Test: `test_timeout_message_offers_a_command_that_can_be_pasted`.

### 7.4 An advisory step could abort the whole run with no message

```sh
disk_bytes="$(docker run --rm --network none "$PYIMAGE" \
  python -c 'import shutil; print(shutil.disk_usage(".").free)' | tr -d '\r')"
```

The line above it guards itself with `|| echo 0`; this one does not. Under
`set -euo pipefail` an assignment takes the exit status of its command
substitution, so any failure of `docker run` kills the script — in the step the
comments call "warnings, not gates". The reviewer gets a bare
`docker: Error response from daemon: …` with no `bootstrap failed:` line and no
`next:` line, which is the exact outcome the script's error handling exists to
prevent. It is reached when the daemon refuses `--network none`, when the image
is for the wrong platform, or — most ironically — when the host is so short of
disk that no container can start.

Test: `test_unreadable_disk_measurement_warns_instead_of_aborting`.

### 7.5 `--offline` refused to start a startable stack

The offline check required every image in `compose.yml` **and** every image in
`compose.tools.yml`, as one union. `aow/demos:dev` is in the second list, and
it is built by the fourth line of README step 2 — described as "the proof
runner", which reads optional. A reviewer who skipped it, went offline and ran
`bash scripts/bootstrap.sh --offline` was refused a stack that would have
started perfectly, and told to:

```
next: rerun without --offline on a machine with a network
```

For someone who is already air-gapped, that advice is impossible to follow, and
it is unnecessary: nothing in `docker compose up` needs the proof runner.

The two lists are now separate. Images the stack needs are still fatal. The
`stage` service's image stays fatal too, because the model verification runs
inside it and a miss there would otherwise be misreported as "the model is not
staged". The proof runner is a warning that names the command to build it.

Tests: `test_offline_starts_the_stack_when_only_the_proof_runner_is_missing`
and `test_offline_still_refuses_when_an_image_the_stack_needs_is_absent`.

### 7.6 A failure of the first image list was swallowed

```sh
staged_images="$( { dc config --images; dc -f compose.tools.yml config --images; } | LC_ALL=C sort -u )" || die …
```

A brace group exits with the status of its *last* command, so `pipefail` could
not see a failure of the first. Had `dc config --images` failed, `|| die` would
not have fired and the loop would have printed `PASS every required image is
already local` having checked only the tooling images. Latent rather than live
— `dc config --quiet` above it catches a render failure first — but the `|| die`
reads as though it protects both, and after 7.5 the lists are captured
separately anyway.

### 7.7 The passwords were briefly world-readable

```sh
mv "$tmp" "$ENV_FILE"
trap - EXIT
chmod 600 "$ENV_FILE" 2>/dev/null || true
```

`$tmp` was created by a shell redirect under the ambient umask — 0644 on most
hosts — so the file holding five generated passwords was group- and
world-readable from creation until the `chmod` after the `mv`. `chmod` now
happens on `$tmp` before it is moved into place, so the file is never readable
by anyone else at any point. The surrounding block already takes considerable
care (generated inside the container, never printed, never in `argv`, never in
an environment variable, `--network none`); this was the one gap in it.

Test: `test_generated_env_is_never_world_readable` (POSIX only).

### 7.8 A missing Makefile bypassed its own error message

```sh
PYIMAGE="$(awk '$1 == "PYIMAGE" { print $3 }' Makefile)"
```

This runs at line 60, before `die` is even defined. If `Makefile` is missing or
unreadable, `awk` exits non-zero, `set -e` kills the script, and the carefully
worded `die` at step 1 — which explains the exact format the `PYIMAGE` line must
have — is unreachable. The parse is now tolerant so that the failure reaches the
check that can explain it.

### 7.9 `PASS Compose plugin vv2.39.0`

`docker compose version --short` prints `v2.39.0` on some builds. The code
already strips that `v` before comparing, but printed the unstripped string
behind a literal `v`. Cosmetic, but it is the first `PASS` a reviewer reads, and
the old test stub returned a bare `2.39.0` so CI never showed it.

### 7.10 The longest step in the script printed nothing while it ran

Not from the audit — this one I hit myself, and it cost me an hour.

```sh
note "pulling the four pinned images (~2.2 GB); this is the long one..."
dc pull --quiet postgres rabbitmq llm edge
```

With `--quiet`, the single longest step in the script — the only one that
depends on somebody else's network — produced no output whatsoever. When
`ghcr.io` throttled to 53 kB/s (§2.1), there was no way to tell a slow pull from
a hung one, and no reason to believe that waiting would help. I aborted a pull
that was in fact working.

`--quiet` is gone, so Compose prints per-layer progress, and the surrounding
note now says that a slow registry can make this step take tens of minutes and
that interrupting and rerunning is safe because finished layers are cached.

### 7.11 Carriage returns, and a portability note

`tr -d '\r'` was applied to `docker run` output elsewhere in the script but not
to `config --services` or `ps --format`, where a stray CR would make every
service read `(no container)` and produce the same silent 15-minute wait as
7.1. `scripts/event-recheck.sh:274` already strips CR from `dc ps -q`, so the
repository was inconsistent about it in the one place where it would be fatal.
Both are now stripped.

The restart-count lookup memoises container ids, because the loop polls every
5 s for up to 15 minutes. The memo is a flat string rather than an associative
array: `declare -A` needs bash 4 and macOS still ships bash 3.2, which the
README's supported-platform list includes.
---

## 8. Tests

`tests/unit/test_bootstrap.py` grew from 5 tests to 21. They need no network,
no Docker daemon and no stack: a stub `docker` on `PATH` answers the handful of
questions bootstrap asks, and each test shapes exactly one failure mode through
`AOW_STUB_*` variables.

In the test container, where the whole suite runs on Linux with no network:

```
$ docker run --rm --network none aow/tests:review
1502 passed, 2 skipped, 4 warnings in 33.82s

$ docker run --rm --network none aow/tests:review python -m pytest tests/unit/test_bootstrap.py -q
.....................                                                    [100%]
```

All 21 pass there. On the Windows host one is skipped
(`test_generated_env_is_never_world_readable`, which asserts a POSIX file
mode).

### What the old suite could not see

The old stub made three things invisible, and each hid a defect:

1. `docker run` fell through to `exit 0` with no output, so it **could never
   fail** — which is why §7.4 had no test.
2. `ps` returned only `exited` states, so `running`, `starting`, `unhealthy`
   and the empty-`{{.Health}}` branch were never exercised — which is why §7.2
   had no test.
3. **No test ever reached a healthy stack.** The only assertion about success
   was the negative `assert "every service is healthy" not in result.stdout`. A
   change that made the loop never succeed would have passed the whole suite.

The stub now runs `python -c` under a **real interpreter**, so the password
generator is exercised rather than imitated — that block writes five secrets to
disk and previously had zero coverage.

### The new tests, and the failure each one is about

| test | the real failure it catches |
|---|---|
| `test_unsupported_compose_ps_format_fails_fast_and_says_so` | an old Compose turns into a 15-minute silent wait that blames the containers |
| `test_crash_looping_service_without_a_healthcheck_is_not_healthy` | a crash-looping consumer reported as a healthy stack, exit 0 |
| `test_a_service_that_restarted_in_the_past_is_still_healthy` | the regression test for my own first fix: a cumulative counter making every later run hang |
| `test_healthy_stack_is_reported_healthy` | the success path itself, previously unasserted |
| `test_timeout_message_offers_a_command_that_can_be_pasted` | `next:` line that fails with `no such service: or` |
| `test_unreadable_disk_measurement_warns_instead_of_aborting` | an advisory step aborting the run with no message |
| `test_generated_env_replaces_every_placeholder_and_keeps_the_template` | a dropped template line, a repeated password, or a secret leaking to stdout |
| `test_generated_env_is_never_world_readable` | the umask window on the file holding five passwords |
| `test_existing_env_with_a_placeholder_is_refused_not_overwritten` | the one way a rerun goes wrong — and the file surviving it |
| `test_existing_env_is_left_byte_for_byte_alone` | the headline "safe to run twice" claim |
| `test_offline_starts_the_stack_when_only_the_proof_runner_is_missing` | refusing to start an air-gapped stack over an image it does not need |
| `test_offline_still_refuses_when_an_image_the_stack_needs_is_absent` | the other half of that, so the fix did not just disable the check |
| `test_connected_run_skips_the_pull_when_the_pinned_images_are_present` | the "rerunning is safe" claim about the pull |
| `test_connected_run_pulls_when_an_image_is_missing` | the other half, so the skip is not unconditional |
| `test_timeout_with_a_non_numeric_value_is_usage_error` | only the *missing* value was tested before |
| `test_unknown_option_is_a_usage_error_that_prints_the_usage` | exit 2 plus the usage text |

### Two existing tests strengthened

- `test_offline_bootstrap_does_not_pull_helper_for_disk_check` asserted only
  that `docker pull` had not run, while its failure message claimed
  "--offline attempted a registry pull". A `docker compose pull` would reach
  the network just as surely and was not checked. It now checks both.
- `test_offline_bootstrap_uses_local_images_and_model_source` asserted an
  exact flag-order substring of the implementation line, which would break on
  a harmless reordering while proving nothing about behaviour. Renamed to
  `test_offline_verifies_the_model_without_a_network`, it now asserts the two
  properties that matter: the stage run cannot pull, and its model source is a
  local file URL.

Lint is clean under the repository's own configuration:

```
$ python -m ruff check tests/unit/test_bootstrap.py
All checks passed!
$ python -m ruff format --check tests/unit/test_bootstrap.py
1 file already formatted
```
---

## 9. The fixes, verified against the live stack

Unit tests prove the logic; these runs prove it on a real engine with real
containers. The script under test is the committed one
(`sha256 = 22c4cd70…`, checked identical inside the engine).

### 9.1 No false positive on a container with restart history

`consumer` sitting at `RestartCount=6` after the drills, stack fully healthy:

```
$ bash scripts/bootstrap.sh --wait-only --timeout 120
   1s  waiting for: enricher(settling) ingestor(settling) consumer(settling)
   PASS every service is healthy (6s)
exit=0  elapsed=6s
```

One settling poll, then healthy. This is the run that would have hung for
900 s under my first attempt at the fix (§7.2).

### 9.2 The timeout path, and its `next:` line

Forced by pausing a service, which puts it in a state that is neither healthy
nor exited:

```
$ docker compose pause agent
$ bash scripts/bootstrap.sh --wait-only --timeout 12

nothing has failed yet; to keep waiting instead, run:
  bash scripts/bootstrap.sh --wait-only --timeout 600

bootstrap failed: still waiting after 12s for: agent(paused)
next: docker compose logs --tail 50 agent
exit=1  elapsed=15s
```

The paused service is named correctly, and the `next:` line is a command and
nothing else. Both forms run verbatim, for contrast:

```
$ docker compose logs --tail 3 agent          # the new next: line
agent-1  | INFO:     127.0.0.1:36462 - "GET /health HTTP/1.1" 200 OK
exit=0

$ docker compose logs --tail 50 agent -- or keep waiting with '…'   # the old one
no such service: or
```

The stack returned to fully healthy after `unpause`, within about 10 s.

### 9.3 `--offline` against a genuinely disconnected engine

Covered in §5.1: exit 0 in 3 s, including the new split between images the
stack needs (fatal if missing) and the proof runner (a warning).

### 9.4 The whole suite, in the container, with no network

```
$ docker build -f tests/Dockerfile -t aow/tests:review .
$ docker run --rm --network none aow/tests:review
1502 passed, 2 skipped, 4 warnings in 33.82s
```

All 21 bootstrap tests are included and pass, and on Linux none is skipped —
the POSIX file-mode test that is skipped on Windows runs here.

This also settles a loose end: `test_backup_restore.py` and
`test_refresh_report.py` failed on the Windows host with
`OSError: [WinError 6] The handle is invalid`, a Python-3.14-on-Windows
subprocess quirk in files this branch never touched. They pass in the
container, which is where the repository intends them to run.

### 9.5 What could not be verified live

| claim | status | why |
|---|---|---|
| Crash-loop detection fires on a real crash loop | **unit-tested only** | I could not induce one. The kernel shields PID 1 from default-action signals, so `kill 1` inside the container does nothing, and `docker kill` did not trigger the restart policy on this engine (container went to `exited`, `RestartCount` unchanged). The test drives the real script with a growing counter and asserts `consumer(restarting)` + exit 1. |
| Old-Compose capability probe on an actually-old plugin | **unit-tested only** | The engine ships Compose v2.40.3 and I did not install an old plugin. The test makes `ps --format` fail and asserts the message names the plugin, not the containers. |
| `chmod` before `mv` closes the umask window | **partly** | The generated `.env` was confirmed `0600` on the live run, and the POSIX-mode test passes in the container. Neither observes the window itself, which is the point of moving the call. |
---

## 10. For the integrator: README changes this branch implies

This branch deliberately does not touch `README.md` or `DEVOPS_REVIEW.md`.
Four things in the README are now either inaccurate or incomplete because of
what is in §7. They are listed smallest-blast-radius first.

### 10.1 "An existing `.env` is never read" overstates it — `README.md:58-60`

> **An existing `.env` is never read, rewritten or replaced**

The script's own header is accurate and the README's shortened version is not.
`bootstrap.sh` never reads `.env` *for its values* — it only greps for
`change-me` — but `dc()` passes `--env-file "$ENV_FILE"` to every Compose
invocation, so Compose reads and interpolates it on every step. Suggested:

> **An existing `.env` is never rewritten or replaced**, and bootstrap never
> reads it for its values — it only checks it for leftover placeholders.
> (Compose itself reads it, as it must, to render the Compose files.)

### 10.2 The `--offline` flag's description omits the image check — `README.md:66`

The table row says `--offline` checks that "`.env` renders the Compose files,
and the staged model still matches `models.lock`". It does not mention the
image check at all, which is the check most likely to stop an air-gapped
reviewer. That check now distinguishes two cases, per the fix in §7.5:

- images the stack needs (`compose.yml`) and the `stage` image — still fatal;
- the proof runner `aow/demos:dev` — now a **warning** that names the command
  to build it, because nothing in `docker compose up` needs it and an
  air-gapped reader cannot act on "rerun on a machine with a network".

### 10.3 Step 2 should say the pull can be slow, and that interrupting is safe

`README.md:96` presents `docker compose pull …` with no indication of
duration. On the day of this run `ghcr.io` served the 1.2 GB model-server
image at 53 kB/s (§2.1). The script now prints per-layer progress and says so;
the README's typed-out path should carry the same warning, because a reviewer
following the manual recipe gets no such note:

> This is the long step. On a slow or throttled registry it can take tens of
> minutes. Interrupting it and rerunning is safe — finished layers are cached
> and the pull resumes where it stopped.

### 10.4 The Compose floor is higher than "v2"

`README.md:25` requires "the `docker compose` subcommand, with a space",
justified by the top-level `name:` key. That is the floor for *rendering* the
files, but the bootstrap script also needs `ps --format` with a Go template,
`config --images` and `up --pull`, which are considerably later v2 features
(§7.1). The script now probes the capability rather than guessing a version, so
nothing is broken — but the README's stated requirement is lower than the real
one. Suggested: name **Compose v2.21 or newer** for the scripted path, keeping
"v2" as the floor for the typed-out path.

The same row records "Verified on Docker Engine 29.8 with Compose v5.5.1".
This run adds a second verified combination that is closer to what a Linux
reviewer is likely to have: **Docker Engine 28.5.2 with Compose v2.40.3**, on
Alpine 3.22. Worth adding, since the only currently named combination is the
newest one.

### 10.5 Not a README change, but worth the integrator's attention

`demos/05_questions.sh` **passes green with the model server completely dead.**
Nothing in it asserts `llm_called is True`, and `rows_used.recommendations > 0`
for the London question is satisfied by `status='pending'` rows that the rule
engine wrote with `text=NULL` — no model involved. A green `questions` run
proves retrieval and grounding; it does not prove the model ran.

`demos/01_offline.sh` §4 prints the two example answers and asserts **nothing**
about them — it prints `as of: (none)` for a missing timestamp and still
passes. Its own header comment claims it demonstrates both example questions
answered with an as-of stamp.

Both are outside this branch's owned files, so I have not touched them. §6
below asserts those properties directly instead, which is why that section
exists rather than leaning on the demos' exit codes.
---

## 11. Summary, and what this does not prove

### What was exercised, and the result

| # | what | result |
|---|---|---|
| 1 | Fresh clone on an engine with zero images | started, healthy, serving |
| 2 | First connected bootstrap | exit 0 (after an aborted first attempt — §2.1) |
| 3 | `.env` created: 5 distinct secrets, mode 600, none printed | verified byte by byte |
| 4 | Second run preserving `.env`, model, volumes and data | exit 0 in 21 s, nothing changed |
| 5 | `--offline` with the engine physically disconnected | exit 0 in 3 s, no network touched |
| 6 | Prerequisite failures (no docker, dead daemon) | exit 1, cause + next command |
| 7 | Usage failures (bad/absent `--timeout`, unknown flag) | exit 2, usage printed |
| 8 | Health-timeout failure | exit 1, service named, pasteable `next:` |
| 9 | `demos offline` / `questions` / `update` | exit 0 |
| 10 | `demos no-data-loss` | **flaky**: 1 fail in 3, no data ever lost (§5.5) |
| 11 | E1 Rome, E2 London, out-of-coverage control | all grounded, all timestamped |
| 12 | Same three questions, network disconnected | identical results |
| 13 | Full unit suite in the container, `--network none` | 1502 passed, 2 skipped |

### What this does not prove

Recorded so that nothing here is read as more than it is.

1. **Not a reviewer's machine.** A nested Alpine engine on a Windows host is
   not a Linux laptop or a macOS box. Compose v2.40.3 / Engine 28.5.2 is one
   combination; the README names another (29.8 / v5.5.1). Nothing was run on
   macOS, and the bash-3.2 constraint that shaped one fix (§7.11) is reasoned
   from documentation, not tested.
2. **`bash` was installed** into the engine, because `bootstrap.sh` needs it.
   The README's typed-out path, which needs no bash, was **not** exercised —
   only the scripted path.
3. **One timing sample, on a throttled link.** 2409 s for the first run is
   dominated by `ghcr.io` at 53 kB/s and PyPI at 54 kB/s. It says nothing
   useful about a normal connection, and a reviewer should not be told to
   expect 40 minutes.
4. **The UI was checked for HTTP 200 only.** No page was opened, no chart,
   map or trip planner was exercised. M9 and M10 are untouched by this
   evidence.
5. **Crash-loop detection and the old-Compose probe are unit-tested, not
   drilled** (§9.5).
6. **The grounding gap in §6.1 is characterised, not measured.** Roughly 1 in 8
   on this model, prompt and data; that is an observation from ten-odd samples,
   not a rate.
7. **The model-server restart in §6.2 is unexplained.** Not OOM, no error in
   the logs, recovered automatically. It was seen once.
8. **Data ages.** Every answer here depends on a snapshot covering 2026-09-23
   to 2026-10-08 with events valid to 2026-10-15. Run after those dates and
   E1's "tomorrow" will be correctly refused as out of coverage, and
   `demos questions` will fail — a true negative that looks like a regression.
   `make refresh`, which needs connectivity, is the fix.
9. **`README.md` and `DEVOPS_REVIEW.md` were deliberately not touched**, so the
   inaccuracies in §10 are still in the tree.

### Reproducing this

```sh
# a disposable engine, so nothing touches an existing stack
docker volume create aow-review-dind-lib
docker run -d --name aow-review-dind --privileged \
  -v aow-review-dind-lib:/var/lib/docker -e DOCKER_TLS_CERTDIR= \
  docker:28-dind --storage-driver overlay2
docker exec aow-review-dind apk add --no-cache bash curl jq

# a genuinely fresh clone inside it
git bundle create aow.bundle review/bootstrap-proof
docker exec -i aow-review-dind sh -c 'cat > /tmp/aow.bundle' < aow.bundle
docker exec aow-review-dind git clone -b review/bootstrap-proof /tmp/aow.bundle /work

# the run under test
docker exec -e NO_COLOR=1 aow-review-dind sh -c 'cd /work && bash scripts/bootstrap.sh'

# offline, for real
docker network disconnect bridge aow-review-dind
docker exec aow-review-dind sh -c 'cd /work && bash scripts/bootstrap.sh --offline'
docker exec aow-review-dind sh -c 'cd /work && docker compose --env-file .env -f compose.tools.yml run --rm demos offline'

# tear down; the outer engine is untouched throughout
docker rm -f aow-review-dind && docker volume rm aow-review-dind-lib
```
