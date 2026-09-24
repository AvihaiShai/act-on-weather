# Release proof: staging, transport, install, upgrade, rollback, recovery

What was actually run, on what, and what it did and did not establish. The
README summarises this; the detail is here so a reviewer can judge the evidence
rather than the summary.

Everything below was executed. Nothing in this document is a plan, and where a
check was not run it says so.

---

## 1. The defect this drill found

The first real install of an offline bundle on a separate Docker engine failed:

```
Error response from daemon: failed to set up container networking ...
failed to read config content: NotFound: content digest
sha256:128e8312c6e3c61b8a79934e354aa42ec3a5c7d368054aa45c042837bd17b5ba: not found
```

Auditing the archive's blob set against each image manifest:

```
services : image manifest, config_present=False, layers_missing=8/11
ui       : image manifest, config_present=False, layers_missing=7/10
postgres, rabbitmq, llm, edge, stage, demos: complete
```

The two application images were in `images.tar` as a manifest blob and nothing
else — no config, no layers. `docker save` had exited 0. `sha256sum -c
SHA256SUMS` passed on all 151 files. `verify-bundle-images.sh` passed. By every
check the release had, it was a good bundle.

**Cause.** CI publishes `services` and `ui` as a bare
`application/vnd.docker.distribution.manifest.v2+json` — a single manifest, no
index, and **no platform descriptor anywhere**. A containerd-image-store
`docker save` cannot resolve a platform export target for that shape, so it
writes the manifest and stops. The failure is visible only if you ask for the
platform explicitly:

```
$ docker save --platform linux/amd64 <the image>
no suitable export target found: image ... was found but does not provide
the specified platform (linux/amd64)
```

on an image whose own config says `linux/amd64` and which `docker image
inspect` reports as `linux/amd64` with 10 layers.

**Not environment-specific.** Reproduced on Docker Desktop 29.8.0 and on a
separate `docker-ce` 29.8.1, both containerd-backed, after a full `docker rmi`
plus `docker pull --platform linux/amd64` that reported "Pull complete".

**Why no previous drill caught it.** Loading the same broken archive into the
engine that *packaged* it and running `docker create` succeeds for both images,
because that engine already holds the layer content from its own `docker pull`.
Every earlier release drill installed on the packaging machine. A same-host
install cannot detect this class of defect even in principle.

This is why the `a129bb6` drill previously recorded in the README is not
evidence that its archive was complete. That run is left in the history below
with that correction attached, rather than removed.

### The two fixes, and why both were needed

| Approach | Content | Provenance |
|---|---|---|
| containerd store, manifest as published today | **lost** — config and layers absent | preserved |
| classic (`overlay2`) store | preserved | **lost** — all eight digests re-serialised, so `images.bundle.lock` anchors nothing |
| containerd store, published as an OCI index with a platform | preserved | preserved |

Measured, packaging the same release on a classic-store engine:

```
services: images.tar has sha256:4e50346ff98a92e1..., release expects sha256:ae4e76cefc0c3fb0...
ui:       images.tar has sha256:0f969e7774c76959..., release expects sha256:aa0557fd8e554045...
postgres, rabbitmq, llm, edge, stage: same, all eight differ
```

So the classic store is not a workaround: it trades an unloadable bundle for an
unanchored one. The fix is the publish format, and it was verified empirically
on a store that had never held the old record:

```
services (index-wrapped)
  top type : application/vnd.docker.distribution.manifest.list.v2+json
  child sha256:ae4e76cefc0c... platform={architecture: amd64, os: linux} present=True
  -> config_present=True layers=11 missing=0 platform=linux/amd64   LOADABLE
ui (index-wrapped)
  -> config_present=True layers=10 missing=0 platform=linux/amd64   LOADABLE
```

240 MB archive rather than 15 KB, with the index digest preserved for
anchoring.

### A staging-host precondition that must be stated

A containerd store that has once held the platform-less record for a child
manifest keeps exporting an empty archive for it, **even after the image is
republished as an index and re-pulled by tag**. The first attempt at the test
above did exactly that and produced `config_present=False, layers missing
11/11` — the stale record wins. Purging the store (`docker system prune -af`
*while the containerd store is active*) and re-pulling produced the complete
archive.

Consequences:

- A staging host that ever pulled the old shape must have those records purged
  before it can build a valid bundle. A CI runner is clean per job, and a
  reviewer's machine never pulled the pre-fix images at all, so both are safe
  by construction. The only engines that can hit this are the two used to
  investigate it.
- **`docker rmi <tag>` does not clear it.** Removing both the alias tag and the
  `repo@digest` reference and re-pulling still produced an empty archive, three
  times, which is what made this look like a property of the image rather than
  of the store. The records only cleared when the images were removed **by
  image ID** as well. Anyone diagnosing this has to purge by ID or prune, or
  they will reach the wrong conclusion — as both people looking at it here did,
  in opposite directions, before the store state was isolated as the variable.
- Switching the image store and *then* purging does not work: the records are
  per-store, and a purge under `overlay2` leaves the containerd store intact.
  This was observed directly — after a purge the engine reported 0 images, and
  switching back to containerd revealed 10 surviving records.
- The media type is **not** the variable, and an earlier draft of this document
  said it was. On the same engine, `application/vnd.oci.image.index.v1+json`
  exported correctly while
  `application/vnd.docker.distribution.manifest.list.v2+json` did not, which
  looked decisive; it was a coincidence of which images had stale records. A
  freshly published image of the *same* Docker media type exports correctly on
  that engine, and so does the one that failed, once its records are purged.
- The staging engine's image store is a release-critical property, so
  `scripts/verify-bundle-images.sh` now fails packaging rather than relying on
  anyone remembering it.

### The check that closes it

`scripts/verify-bundle-images.sh` gained a completeness gate. For each alias it
resolves the manifests the archive holds, requires one of them to have its
config blob and every layer blob physically present, and reads that config to
confirm the platform is the release's own.

A multi-platform image stays legal: `docker save` writes the upstream index
listing every platform but stores only the one it exported, so absent
non-amd64 children are expected. The rule is therefore per image, not per blob.

One extra pass over the archive, no scratch disk, 6.5 s on a 1.9 GB bundle.
Against the broken bundle:

```
images.tar cannot load services as linux/amd64:
    ae4e76cefc0c: 9 of 12 blobs (its config and layers) are not in the archive
images.tar cannot load ui as linux/amd64:
    aa0557fd8e55: 8 of 11 blobs (its config and layers) are not in the archive
```

---

## 2. Staging and offline environments

**Staging (connected).** Windows 11, Docker Desktop 29.8.0, containerd image
store, `linux/amd64`. This is where `scripts/package-offline.sh` ran.

**Offline host.** A *separate Docker engine*: a dedicated Ubuntu 24.04 WSL2
distribution running its own `docker-ce` 29.8.1 daemon, its own
`/var/lib/docker`, its own networks, and no Docker Desktop integration —
verified by a distinct engine ID (`f99ef3b5-300a-47a5-96e2-73fc62aa9b06`
against Docker Desktop's `375fa6b6-973d-435a-ad5d-9bfe642fb556`) and an empty
integration socket directory. Compose v5.5.1, `linux/x86_64`, overlayfs,
15 GB RAM, 952 GB free. Before each install it held **0 images and 0 volumes**,
so `docker load` was the only thing that could supply an image.

Outbound network was cut with nftables rules dropping all traffic via `eth0`
plus the WSL loopback DNS-tunnel resolver (`10.255.255.254`), at hook priority
−300 ahead of Docker's own rules, which were never edited. Measured at install
time, from the distribution:

```
DNS    : BLOCKED
TCP443 : BLOCKED
```

and from a container **on a routable network** — deliberately not the stack's
`internal: true` backend, so this tests the host firewall rather than Docker's
own isolation:

```
HTTPS raw-IP 1.1.1.1 : BLOCKED
HTTP  raw-IP 1.1.1.1 : BLOCKED
DNS   ghcr.io        : BLOCKED
```

The rules are in memory, and WSL terminates an idle distribution — which
restarts `dockerd` and every container with `restart: unless-stopped`, and
clears the rules **fail-open**. An earlier run of this drill hit exactly that.
They are therefore installed as a systemd unit ordered `Before=docker.service`,
so the engine and everything it restarts come up with egress already severed
rather than in a brief connected window.

### What this is not

**Not a separate physical machine, and not a separate VM.** All WSL2
distributions on this host share one Hyper-V utility VM, one kernel and one
network namespace root, so the cut is enforced by firewall rules inside a
shared virtual machine rather than by the absence of a physical link. The
bundle travelled over a hypervisor filesystem share (drvfs), not physical
media. The WSL localhost-forwarding relay runs over vsock and stays available
under the rules, so published ports remained reachable from Windows during the
offline phase. Hyper-V was not available to build a true VM:
`Microsoft-Hyper-V` and `Microsoft-Hyper-V-All` report `InstallState=2`
(Disabled), and enabling them needs elevation plus a reboot.

What this establishes: the bundle installs and runs against a **clean engine
with no image cache to fall back on and no reachable egress**, which is
precisely the property the previous same-host drill could not establish and the
property that exposed §1. What it does not establish: operation on physically
disconnected hardware.

One consequence of the shared namespace that will bite anyone repeating this:
the Windows-side development stack already holds `127.0.0.1:8000` and `:8080`,
so a release test on the distribution must set `AOW_BIND_ADDR` to another
loopback address. Otherwise `install-offline.sh` fails with
`failed to bind host port 127.0.0.1:8000/tcp: address already in use`. This run
used `127.0.0.3`.

---

## 3. The drill

Two releases, each packaged from its own green `main` CI build, plus one
fault-injection release derived locally — CI would never publish a migration
written to fail, so that one is explicitly not a CI artefact.

| | release A | release B |
|---|---|---|
| commit | `f19224130aa859257f71f276f9bac53d30c7bb2e` | `bf4a2dfd49bc20c1a09f1a2abf646b6891395a7c` |
| CI run | `36037543463` (`push`; guard, unit, lint, build-and-scan, publish-images and ui-gate all green) | `36041468062` |
| `SHA256SUMS` digest | `sha256:6dd3652ea70b2af1d358ea2cad52a09bf86d4d230f188dba951fababd9fd3ab9` | `sha256:52e5b1c75064d76f5e46cb604f5c481cfb1fbdb78099b66829c94dc44d068d2f` |
| bundle | 2.4 GB, 194 files, `images.tar` 1,231,824,896 bytes, 10 images | same shape |

Both artifacts' `services` and `ui` digests were checked against the registry
before packaging: both `application/vnd.docker.distribution.manifest.list.v2+json`,
both equal to what the release tag resolves to.

### What ran

| # | Exercise | Result |
|---|---|---|
| 1 | Transport, 2.4 GB | 11 s, 218 MiB/s |
| 2 | `verify-bundle.sh`, with the `SHA256SUMS` digest carried out of band as `AOW_SHA256SUMS` | exit 0 in 2 s; 194 files + the model verified; out-of-band digest matched; 10 images by verified manifest digest; **archive complete for `linux/amd64`** |
| 3 | First install, empty volumes, `--pull never` | `PASS` in **40 s**, **0 pull attempts**, 10 images loaded |
| 4 | Stored data | weather 80, recommendations 1360, events 39, places 620, facts 81 — matching `data/snapshot/MANIFEST.json`; `aow_backend` `Internal=true` |
| 5 | Egress from a container on a routable network | HTTPS, HTTP and DNS all blocked (§2) |
| 6 | Packaged proof runner, `prove-offline.sh offline` | exit 0, all five sections |
| 7 | Both reviewer questions | answered from stored data with source and as-of (below) |
| 8 | Out-of-coverage question | refused in code, `local model called: False` |
| 9 | Monitoring overlay from the bundle | 0 pull attempts; Grafana 12.2.0 `database: ok`; 11 alert rules in 4 groups; `aow-api`, `aow-agent`, `aow-consumer`, `aow-enricher`, `aow-ingestor`, `rabbitmq` and `rabbitmq-queues` all `up`; **0 error or warn lines** in Grafana's log |
| 10 | Grafana's seven air-gap variables | all set as intended, read off the running container; `grafana.com` and `1.1.1.1` both unreachable from it |
| 11 | Published ports | only `edge` (`127.0.0.3:8000`, `127.0.0.3:8080`) and `edge-observability` (`127.0.0.3:3000`); Prometheus and Grafana stay on the internal network |
| 12 | Live data through the queue | a free-text recommendation, a saved itinerary and a `PATCH`, all three `message_id`s traced to `stored`; recommendations 1360 → 1361, itineraries 0 → 1 |
| 13 | **Upgrade A → B with that data in place** | `PASS` in **38 s**, 0 pulls; pre-upgrade dump (699,129 bytes) written **before** the new images touched the schema; every traced ID still `stored`, patch marker and history intact, itinerary intact, requested activity still scored `good`; every application container now on a release-B alias |
| 14 | **Deliberately failed migration** | install aborted, `migrate` exit 3, its own dump (711,508 bytes) taken first; `weather_daily` 80 → **0**, every other table untouched (recommendations 1361, places 620, facts 81, events 39, itineraries 1, `ingest_log` 1087, `record_history` 1) |
| 15 | **Image rollback to B** | images and migration files reverted; `weather_daily` still 0; release smoke **failed, correctly**, rather than reporting a healthy rollback |
| 16 | **Restore from the failed install's dump** | `PASS` in **18 s**; weather 80, places 620, events 39, facts 81; all three traced IDs `stored`; patch marker, patch history, itinerary and requested activity all back |

Steps 14–16 are the sequence that matters most, because it is the one an image
rollback cannot handle alone. A migration that deletes and then fails leaves
the data gone after the images are back; the smoke check refuses to call that
healthy, and `scripts/restore-offline.sh` with the dump the **failed** install
took on its way in is what actually recovers it.

The fault-injection release's `migrate` ran
`001_init`, `002_activities`, `003_demo_events`, `006_event_validity`,
`007_city_coast` and then `900_failed_upgrade` — so the bundle carried the
final migrations, not a stale set.

### The two reviewer questions, through the installed release

**"What is the weather tomorrow in Rome?"**

> The weather tomorrow in Rome is expected to be high 28°C and low 20°C with a
> 11.2-hour sun. There is a 0.1mm rainfall chance, and the wind speed is
> 17 km/h. The suitability verdict for the weather is good.
>
> as of: weather as of 2026-09-23 18:16 UTC · forecast covers 2026-09-23 to
> 2026-10-08 · sources: open-meteo

**"What activities can I do with my wife this week in London? We like concerts,
shopping and fine dining."** — delivered from rows: seven dated forecast lines,
the best-rated activity per day, venues grouped by category with the standing
caveat that the system holds a name and a category rather than a programme, and
the one scheduled event on record (`2026-09-25 Free Friday Lunchtime Concert at
LSO St Luke's`). Sources: open-meteo, Wikidata (CC0), London Symphony Orchestra
official listing.

**Out of coverage:**

> I have no weather data for 2027-07-04. The stored forecast covers 2026-09-23
> to 2026-10-08, and I do not guess beyond it. Refresh the snapshot while
> connected to extend it.
>
> local model called: False

### Timings

| | |
|---|---|
| Transport, 2.4 GB over drvfs | 11 s (218 MiB/s) |
| `verify-bundle.sh` (checksums, model, lock anchoring, digests, completeness) | 2 s |
| First install to `PASS` | 40 s |
| Upgrade to `PASS` | 38 s |
| Failed upgrade to abort | 27 s |
| Restore to `PASS` | 18 s |
| Monitoring overlay to healthy | ~25 s |
| Rollback to a *correct* failure | **508 s** — the release smoke check waits out its full 480 s deadline before reporting the still-empty forecast |

That last row is honest rather than flattering: a rollback whose outcome is
already determined still costs eight minutes of waiting. It is a knob worth
adding if this is ever run often.

---

## 4. What is still not proven

- **Not a physical air gap.** Section 2 states exactly what the isolation is.
- **The `demos` proof-runner image is self-attested.** It is built during
  packaging, so its entry in `images.bundle.lock` is read from the archive
  itself rather than from a CI run, unlike `services` and `ui`.
- **`SHA256SUMS` is self-attesting.** Whoever can rewrite the folder can
  rewrite the checksums with it. What narrows that is the digest anchoring in
  `scripts/verify-bundle.sh`: an attacker must also match a digest recorded in
  a CI run artifact and one committed to `IMAGES.lock`. The only anchor outside
  the folder is the digest of `SHA256SUMS` itself, which packaging prints and
  the installer checks when it is passed as `AOW_SHA256SUMS`.
- **Layer blobs are checked for presence, not re-hashed, before load.** A
  changed layer breaks `SHA256SUMS`; an internally inconsistent archive is
  refused at load, because `install-offline.sh` no longer trusts `docker load`
  to exit non-zero.

---

## 5. Superseded record

The README previously carried a release drill for commit `a129bb6`, run as a
second Compose project on the packaging machine. It reported a clean pass
across first install, upgrade, failed migration, rollback and restore.

That record is superseded. Its functional results are consistent with what is
recorded above, but its central claim — that the archive was good — could not
have been tested by that method, for the reason in §1: the engine it installed
on already held the layers. It should be read as evidence about the installer
and the recovery path, not about the bundle.
