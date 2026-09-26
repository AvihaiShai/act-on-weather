# Reviewer quickstart

Two commands: one for a connected machine starting from nothing, one for a
machine that was staged earlier and is now offline.

This page is for evaluating the system. It has nothing to do with the F10
physical air-gap drill or the USB artifacts in
[EVIDENCE-physical-airgap.md](EVIDENCE-physical-airgap.md) — those exist to
prove a property of the release process, not to install the software.

---

## 1. Connected machine, starting from nothing

You need **git**, **Docker** with the **Compose v2** plugin (v2.21 or newer for
this script) and roughly **8 GiB** of memory available to Docker. Disk lands on
two filesystems: about **4.3 GB on Docker's** for the images, and **~1.2 GB on
the drive holding the clone**, because the model is staged into `./models/`.
Bootstrap reports Docker's free space and warns below 6 GiB; it cannot see the
other one. On Windows, run this in Git Bash. The repository is private, so the
clone needs an account that has been granted access — use SSH
(`git@github.com:AvihaiShai/act-on-weather.git`) if HTTPS prompts you.

```bash
git clone https://github.com/AvihaiShai/act-on-weather.git && cd act-on-weather && bash scripts/bootstrap.sh --refresh
```

That one command runs six numbered steps, which is what the output counts
(`1/6` to `6/6`):

1. checks Docker is present, reachable, and is a Compose v2 plugin new enough
   for the templates this script renders;
2. reports the memory and disk Docker has (warnings, never gates);
3. creates `.env` with generated passwords — inside a pinned container with no
   network, so the values never pass through a shell variable, an argument or
   your history. If `.env` already exists it is kept untouched, and the run
   stops only if it still holds a `change-me` placeholder;
4. pulls the digest-pinned images, stages the 1.2 GB model and verifies it
   against `models.lock`, and builds the service, UI and proof images;
5. starts the stack;
6. waits until every service is healthy, then prints the URLs.

With `--refresh` there is one more stage after those six — the output labels it
`Refresh`, not `7/7`: it fetches a fresh weather forecast, then closes the
egress window again and asserts it is closed.

Budget **15–25 minutes** for a first run. That is an estimate, not a
measurement from your hardware: it is dominated by the image pull and the
1.2 GB model download, so it is really a function of your link. A second run on
the same machine skips both and finishes in well under a minute. The `llm`
container reports unhealthy for about three minutes while it loads the model;
that is normal and the wait is built in.

When it finishes it prints the UI and API addresses:

```
UI   http://localhost:8080
API  http://localhost:8000
```

If your browser resolves `localhost` to `::1` and does not fall back, use
`http://127.0.0.1:8080`.

**One caveat about the API, and it matters most on the offline machine below.**
The interactive Swagger page at `/docs` is FastAPI's default and fetches its
JavaScript from `cdn.jsdelivr.net` and a favicon from `fastapi.tiangolo.com`, so
it is the one page in this system that needs a network. Use
`http://localhost:8000/openapi.json` instead — it answers 200 and is
self-contained.

### If you would rather not fetch anything

Drop the flag. `bash scripts/bootstrap.sh` does steps 1–6 and leaves the data
exactly as it was cloned. The final report says so in as many words.

---

## 2. A machine that is already staged, and is now offline

```bash
bash scripts/bootstrap.sh --offline
```

It verifies that every image is already local and that the staged model still
matches `models.lock`, starts the stack with `--no-build --pull never`, waits
for health and prints the same addresses. It touches no network, and `--refresh`
is rejected in this mode rather than attempted and failed. Use
`/openapi.json`, not `/docs`, on this machine.

If the model is missing or its checksum does not match `models.lock`, this stops
in a second or two and says which: a missing file names the staging command, and
a corrupt one prints the expected and actual sha256. Neither starts anything.

---

## 3. What is fresh, and what is not

This is the part worth reading before you judge an answer. Everything the UI
and the agent show carries an as-of stamp, and a day with no stored row behind
it is named as a gap rather than guessed at — but you should still know which
rows came off the internet today and which shipped with the clone.

| Data | How it is updated | With `--refresh`? |
|---|---|---|
| **Weather forecast** | Fetched per city from Open-Meteo through a bounded egress window, into the producer outbox, then the queue, then the database | **Yes — this is the only thing `--refresh` fetches** |
| **Places** | Wikidata extract, committed to `data/snapshot/places.jsonl` | No |
| **City facts** | Committed to `data/snapshot/facts.jsonl` | No |
| **Events** | A **manually verified sample**, not a feed — the row count, the check date and the expiry are in `data/snapshot/MANIFEST.json`. Each row carries a source and a `checked_at`, and the consumer computes `valid_until` from that; past it the row is still stored and still counted, but is no longer returned as currently scheduled | No |
| **Sample events** (demo mode only) | Generated, every row flagged `is_sample` and titled `Sample: …` | No |
| **Marine / sea state** | **Does not exist.** There is no marine data source, so sea activities are capped below the "good" band | No |

Rebuilding the snapshot files is `make snapshot`, which rewrites files in the
repository and expects a human to review the diff before committing. It is a
maintainer step, not something a reviewer should need, and `--refresh` does not
do it.

Rechecking the event sample against its sources is `scripts/event-recheck.sh` —
also a maintainer step, and also manual by design: an air-gapped run cannot
discover that a venue cancelled a show, so the only honest thing the system can
do is state how old its reading is.

### What "outside coverage" actually does

Three things, and a plain run will show you all of them.

1. **A partly-covered question is not refused.** Ask about a week that runs off
   the end of the stored forecast and the answer covers the days that have rows,
   names the days that do not, and says why. The trip planner does the same: it
   plans the days it has and states the gap. Only a question with *no* covered
   day at all is refused outright, and that refusal is written in code before
   the model is reached.
2. **Coverage is per city, not one window.** The dates in the coverage banner
   are the span across all five cities, so a date inside them is not by itself
   evidence that the city you asked about has a row for it — one city can be
   ingested further ahead than another, and a single day can be missing from the
   middle. Both the agent and the planner work from the rows they actually
   retrieved for that city, not from the banner.
3. **A clone whose window has passed refuses everything.** The forecast in
   `data/snapshot/` is fixed data with a stated last date. Once today is past
   it, every weather question is out of coverage — including the two in-range
   questions inside the offline proof, which become refusals. That is the system
   working, not failing. `--refresh` on a connected machine is what moves the
   window forward and restores the demonstration; the coverage banner and
   `GET /coverage` always say where it currently ends.

### If the fetch fails

It will say so, and it will not pretend otherwise. The run names the cities the
provider did not answer for, says their stored forecast is unchanged and still
carries its older as-of, files the outcome at `GET /refresh/last`, and closes
with a report that states the refresh did not complete rather than letting the
success banner stand. **`bootstrap.sh` exits 3** (2 is reserved for bad arguments),
so a scripted run can distinguish an incomplete refresh from a usage error.

The stack is still up and still usable. The refresh can succeed for some cities
and fail for others, which is why the per-city result is worth reading — and why
every answer and chart carries its own as-of stamp rather than one figure for
the whole database. Retry on its own with:

```bash
docker compose -f compose.tools.yml run --rm refresh
```

One failure mode is louder than the rest. If the refresh cannot **close** the
egress window (exit 3), the ingestor may still be able to reach the internet,
and the report says so in red with the two commands that close it. Treat that
as something to act on before using the stack as an offline demonstration.

---

## 4. What this puts on your machine, and what it does not

| | |
|---|---|
| Compose project | `aow` — **pinned** in `compose.yml` rather than derived from the directory, so everything lands under one name. Two checkouts on one engine therefore share one project and one set of volumes: bootstrap from a second clone takes over the first stack instead of starting a second. Deliberate (it is what makes an upgrade install over the same data), but it means a scratch clone needs the variables below |
| Volumes | `aow_pgdata`, `aow_rabbitdata`, `aow_ingestor_outbox`, `aow_api_outbox`, `aow_enricher_outbox`, `aow_refresh_state` |
| Published ports | **`127.0.0.1:8080`** (UI) and **`127.0.0.1:8000`** (API), loopback only. Grafana's `127.0.0.1:3000` appears only if you opt into the monitoring overlay |
| Egress | The application network is `internal: true`. Only the `edge` proxy is on a routable network, and the ingestor joins an egress network *only* during a refresh, which closes itself and is bounded by a detached guard |

It does **not** prune images, remove volumes it did not create, or touch any
Docker resource outside the `aow` project.

**One exception, if you are running a second copy beside an existing one.** The
service images are tagged `aow/services:dev`, `aow/ui:dev` and `aow/demos:dev`
in `compose.yml`, and those tags are *not* namespaced by the Compose project.
A build in a second checkout therefore moves them, and on the containerd image
store the previous image record is dropped rather than left dangling — so it
cannot simply be re-tagged back. Containers already running are unaffected,
because they hold their own snapshot, but the next `docker compose up` in the
first checkout will use the newly built images. If that matters, run
`docker compose build` in the checkout you care about to put the tags back
where you want them. On a clean reviewer machine this cannot arise.

Isolating a second copy needs **two** variables, not one:

```bash
COMPOSE_PROJECT_NAME=aow-second AOW_PROJECT=aow-second AOW_BIND_ADDR=127.0.0.3 \
  bash scripts/bootstrap.sh --refresh
```

`COMPOSE_PROJECT_NAME` moves the containers, volumes and networks;
`AOW_BIND_ADDR` moves the published ports off `127.0.0.1`; and `AOW_PROJECT` is
what the refresh container uses to decide **which stack to fetch for**.
`bootstrap.sh` now defaults `AOW_PROJECT` from `COMPOSE_PROJECT_NAME`, so the
two cannot silently disagree — but anything invoking `scripts/refresh.sh`
directly still has to set it. Rerunning it is safe: it never
rewrites an existing `.env`, never re-downloads the model, and never removes a
volume.

To stop it and keep the data:

```bash
docker compose down
```

To remove the data as well — this deletes the database, the queue and all three
outboxes:

```bash
docker compose down -v
```

---

## 5. Where to look next

- The agent's breadth, ending with the three questions it refuses:
  `docker compose -f compose.tools.yml run --rm demos questions`. The brief's
  own two example questions are answered inside the offline proof below.
- The offline proof — four structural checks plus three questions:
  `docker compose -f compose.tools.yml run --rm demos offline`. It is not a
  physical air-gap proof and does not claim to be; the behavioural version is
  the same command with your machine's network adapter disabled.
- Monitoring, if you want it: `make monitor`, then Grafana on
  `http://127.0.0.1:3000`.
- The architecture, the delivery guarantees and the known limitations are in
  the README.
