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

- A staging host that ever pulled the old shape must have those digests purged
  before it can build a valid bundle. A CI runner is clean per job and is
  therefore safe by construction.
- Switching the image store and *then* purging does not work: the records are
  per-store, and a purge under `overlay2` leaves the containerd store intact.
  This was observed directly — after a purge the engine reported 0 images, and
  switching back to containerd revealed 10 surviving records.
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

## 2. The environment the drill ran on

A **separate Docker engine**: a dedicated Ubuntu 24.04 WSL2 distribution
running its own `docker-ce` 29.8.1 daemon, its own `/var/lib/docker`, its own
networks, and no Docker Desktop integration — verified by a distinct engine ID
(`f99ef3b5-…` against Docker Desktop's `375fa6b6-…`) and an empty integration
socket directory. Compose v5.5.1, buildx 0.37.1, `linux/amd64`, 32 CPUs, 15 GB
RAM, 952 GB free.

Outbound network was cut inside that environment with nftables rules dropping
all traffic via `eth0` plus the WSL loopback DNS-tunnel resolver
(`10.255.255.254`), installed at hook priority −300 ahead of Docker's own
rules, which were never edited. The cut was verified positively **from inside a
container on that engine**:

```
                                     BASELINE      AIRGAP ON
  distro DNS                         RESOLVES      BLOCKED
  distro HTTPS                       REACHES       BLOCKED
  DNS   resolve cloudflare.com       RESOLVED      BLOCKED
  DNS   direct 10.255.255.254        RESOLVED      BLOCKED
  HTTP  raw-IP 1.1.1.1:80            REACHED       BLOCKED
  TCP   raw-IP 8.8.8.8:443           CONNECTED     BLOCKED
  HTTPS raw-IP 1.1.1.1:443           REACHED       BLOCKED
  ICMP  ping 1.1.1.1                 REPLY         BLOCKED
```

Not just DNS: raw IP, raw TCP and ICMP are all dead, while container-to-
container networking and published ports keep working.

The rules are in memory. WSL terminates an idle distribution, which restarts
`dockerd` and every container with `restart: unless-stopped` — and clears the
rules, fail-**open**. They are therefore installed as a systemd unit ordered
`Before=docker.service`, so the engine and everything it restarts come up with
egress already severed rather than in a brief connected window.

### What this is not

**It is not a separate physical machine, and not a separate VM.** All WSL2
distributions on this host share one Hyper-V utility VM, one kernel and one
network namespace root, so the isolation is enforced by firewall rules inside a
shared virtual machine rather than by the absence of a physical link. The
bundle was transported over a hypervisor filesystem share (drvfs), not physical
media. The WSL localhost-forwarding relay runs over vsock and stays available
under the rules, so published ports remained reachable from Windows during the
offline phase.

Hyper-V was not available to build a true VM: `Microsoft-Hyper-V` and
`Microsoft-Hyper-V-All` report `InstallState=2` (Disabled), and enabling them
needs elevation plus a reboot.

What this demonstrates: the bundle installs and runs against a **clean engine
with no image cache to fall back on and no reachable network egress** — which
is exactly the property the previous same-host drill could not establish. What
it does not demonstrate: operation on physically disconnected hardware.

One consequence of the shared namespace worth recording, because it will bite
anyone repeating this: the Windows-side development stack already holds
`127.0.0.1:8000` and `:8080`, so a release test on the distribution must set
`AOW_BIND_ADDR` to a different loopback address. `install-offline.sh` fails
with `failed to bind host port 127.0.0.1:8000/tcp: address already in use`
otherwise.

---

## 3. The drill

Three releases, each packaged by `scripts/package-offline.sh` from its own
commit, so the upgrade moves image tags and code rather than a version string.
Bundle: 1.9 GB, 151 files, transported at 76–110 MiB/s (24.5 s).

| # | Exercise | Result |
|---|---|---|
| 1 | First install, empty volumes, `--pull never` | `PASS` in 16 s. **0 pull attempts** in the installer log. weather 80, places 620, events 26, facts 81 — matching `data/snapshot/MANIFEST.json` exactly |
| 2 | Live data through the queue | a free-text recommendation, a saved itinerary and a `PATCH` — all three `message_id`s reached `stored=true`; recommendations 1360 → 1361, itineraries 0 → 1, patch history 1 row |
| 3 | Upgrade with live data | `PASS` in 14 s, 0 pulls. Pre-upgrade dump (732 KB) written **before** the new images touched the schema. Every traced ID still `stored`, patch marker and history intact, itinerary intact, requested activity scored `good` |
| 4 | Deliberately failed migration | install aborted; `migrate` exit 3; dump taken first; `weather_daily` 80 → **0**, every other table untouched |
| 5 | Image rollback to the previous release | images and migration files reverted; `weather_daily` still 0; release smoke **failed, correctly**, rather than reporting a healthy rollback |
| 6 | Data restore from the failed install's dump | `PASS` in 18 s. weather 80, places 620, events 26, facts 81; all three traced IDs `stored`; patch marker, patch history, itinerary and requested activity all back |
| 7 | Packaged offline proof runner | `scripts/prove-offline.sh offline` — exit 0, all five sections |
| 8 | The two reviewer questions | both answered from stored data with source and as-of (below) |
| 9 | Out-of-coverage question | refused in code, `llm_called: False` |
| 10 | Egress from inside the running stack | `api`, `agent`, `consumer` all `no route out (OSError)` to `1.1.1.1:443` |
| 11 | Poison path | four malformed patches reached `aow.dlq`; `aow.ingest` drained to 0 |

Steps 4–6 are the sequence that matters most, because it is the one an image
rollback cannot handle on its own. A migration that deletes and then fails
leaves the data gone after the images are back; the smoke check refuses to call
that healthy, and `scripts/restore-offline.sh` with the dump the *failed*
install took on its way in is what actually recovers it.

### The two reviewer questions, through the installed release

**"What is the weather tomorrow in Rome?"**

> Tomorrow in Rome, the weather is expected to be warm with a high of 28°C and
> a low of 20°C. There is a 11.2-hour sun period, and the chance of rain is
> 18%. The wind speed is 17 km/h.
>
> as of: weather as of 2026-09-23 18:16 UTC · forecast covers 2026-09-23 to
> 2026-10-08 · sources: open-meteo

**"What activities can I do with my wife this week in London? We like
concerts, shopping and fine dining."** — answered from rows: seven dated
forecast lines, the best-rated activity per day, venues grouped by category
with the standing caveat that the system holds a name and a category rather
than a programme, and the one scheduled event on record (`2026-09-25 Free
Friday Lunchtime Concert at LSO St Luke's`). Sources: open-meteo, Wikidata
(CC0), London Symphony Orchestra official listing.

**Out of coverage:**

> I have no weather data for 2027-07-04. The stored forecast covers 2026-09-23
> to 2026-10-08, and I do not guess beyond it. Refresh the snapshot while
> connected to extend it.
>
> local model called: False

### Timings worth knowing

| | |
|---|---|
| Bundle transport, 1.9 GB | 24.5 s (76 MiB/s) |
| `sha256sum -c SHA256SUMS`, 151 files | 4.5 s |
| `verify-bundle-images.sh` including the completeness gate | 6.5 s |
| First install to `PASS` | 16 s |
| Upgrade to `PASS` | 14 s |
| Restore to `PASS` | 18 s |
| Rollback to a *correct* failure | 508 s — the release smoke check waits out its full 480 s deadline before reporting the still-empty forecast |

That last row is honest rather than flattering. A rollback whose outcome is
already known still costs eight minutes of waiting.

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
- **Monitoring's air-gap behaviour** is verified as part of the final release
  proof, not this rehearsal.

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
