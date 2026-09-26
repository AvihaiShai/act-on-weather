# Evidence: the physical air-gap proof (F10)

**Status: OPEN.** The preparation is complete and measured; the proof itself has
not been run, because this project has one physical machine and no disposable
second host. The release files have been verified on removable media, but the
offline Docker installation files are still missing. Section 5 names exactly
what is missing. Nothing in this document claims
a physical air gap has been demonstrated.

The staged USB holds the historical `1890eba` A/B drill set and, as of
2026-09-26, a separately verified `8975403` bundle. The latter's
`sha256(SHA256SUMS)` is
`884bf45a4a64876c534383e4f2a2626bf3f967b2807072dad5895920c04e9b15`.
The historical measurements below still refer to `1890eba`.

**VM follow-up, 2026-09-26:** On an Ubuntu 24.04.5 VM with its virtual NIC
disconnected, the `8975403` bundle verified with the digest above, installed
into an empty Docker 29.8.1 image store, and passed the stored-data and nine-page
UI smoke. The installer evidence reported exit 0 and zero image pulls. The
separate `prove-offline.sh` command then failed before its demo started:
Compose v5.5.1 rejects `docker compose run --no-build`. The fix removes the
development build recipe from the tools bundle overlay with `!reset null` and
uses `run --pull never`; release packaging now checks the rendered model. This
VM remains on the development computer, so this run does not close F10.

Everything in sections 3 and 4 was executed on 2026-09-25 and the numbers are
transcribed from the runs. Sections 6 and 7 are a plan for someone with the
hardware, and say so.

---

## 1. Two different standards, and why this document is separate

`ASSIGNMENT.md` requires the system to run **on-prem, without full internet
access** (M6). That requirement is met and evidenced elsewhere: the stack runs
with the application network marked `internal: true`, egress is demonstrated
dead from inside a container, and the offline answers carry their as-of stamps.
Nothing here revises that.

**F10 is a stricter standard this review imposed on itself**: that a release
bundle installs and answers questions on a *separate physical host* whose
network is *physically* disconnected. It exists because of the defect recorded
in [RELEASE-PROOF §1](RELEASE-PROOF.md#1-the-defect-this-drill-found) — an
incomplete archive installed perfectly on the machine that packaged it, because
the engine already held the layers. Every release drill before that one ran on
that machine and none of them could have caught it.

Two gates now close that defect class, and **neither is this one**:

| Gate | What it established | Why it is not a physical air gap |
|---|---|---|
| Separate Docker engine, WSL2 | A second engine with a distinct ID, empty image store, egress killed with nftables | One WSL2 VM on the development machine. Same hardware, same kernel host, isolation enforced by software the stack could in principle influence |
| CI clean-engine gate, release run [`36071276502`](https://github.com/AvihaiShai/act-on-weather/actions/runs/36071276502) for commit `b21885a` | A second daemon on a hosted runner, distinct engine ID, zero images and zero volumes, `AOW_REQUIRE_CLEAN_IMAGE_STORE=1`, install with `--pull never`, data smoke passed | A connected GitHub-hosted runner. The engine was clean; the machine was on the internet |

Both are real results and both are stronger than what preceded them. What
neither can show is that the bundle is sufficient on hardware that has never
been near this project — which is the only form of the claim a reviewer cannot
argue with.

**A VM, a second daemon, a firewall rule, a network namespace and a CI runner
are not physical air-gap proof.** If a future record describes one of those as
closing F10, that record is wrong.

---

## 2. What is being proved, in one sentence

That `dist/aow-<sha>/`, carried on removable media to a machine with no network
interface up and no prior contact with this project, verifies, installs and
answers the reviewer questions — with no successful image pull and no attempt
at one.

---

## 3. Prepared and measured, 2026-09-25

Everything in this section was executed. Commit IDs, CI run IDs, digests,
timings and exit codes are transcribed from the runs.

### 3.1 Source commit

The artifact below is built from the **merged** commit, not from the branch it
came from and not from the earlier preparation commit.

| | |
|---|---|
| Commit | `1890eba16628683c96d650eaa04479b9989c45e5` |
| Subject | Merge pull request #49 from AvihaiShai/review/f10-physical-proof |
| Branch | `main` |
| Packaged from | a detached worktree at that commit, `git status --porcelain` empty |

**The earlier `a21dff9` bundle is historical preparation evidence only.** It was
built and verified before this work merged, its `prove-offline.sh` predates both
the `--no-build` change of this round and the PR #60 fix that reverted it
(§3.4), and it does not contain `airgap-evidence.sh` or the fault-injection
tooling. It is not proof for the merged code and is not used as such anywhere in
this document.

### 3.2 The CI run that authorises a release at this commit

`ci.yml` run [`36135517229`](https://github.com/AvihaiShai/act-on-weather/actions/runs/36135517229),
conclusion **success**, `head_sha` `1890eba…`.

| Job | Result |
|---|---|
| `unit`, `lint`, `guard`, `build-and-scan`, `ui-gate`, `publish-images` | success |
| `model-grounding`, `restore-drill` | skipped in this run |

Its `aow-images-1890eba…` artifact (315 bytes as GitHub stores the zip; the
`images.lock` inside is 284 bytes,
`sha256:f28733652e2a87d1b47517ad01b1b35b374e6cffa01bff26c6e75acfdc6a4c63`) is
the `images.lock` `package-offline.sh` consumed:

```
commit   1890eba16628683c96d650eaa04479b9989c45e5
services ghcr.io/avihaishai/act-on-weather/services@sha256:27e6ffada90f078c836fb0cabef8c327001cdcd7ee8d4e1ff1f05ad3dec775c0
ui       ghcr.io/avihaishai/act-on-weather/ui@sha256:c8799a4c2319a50d40ed33863c6a00e5dd48b29470eca72d175fbc58c369cb80
```

The pull request's own checks — `lint`, `unit`, `guard`, `build-and-scan` — all
passed on run
[`36134774844`](https://github.com/AvihaiShai/act-on-weather/actions/runs/36134774844)
before the merge.

### 3.2.1 The clean-engine release gate, now run for this commit

`release.yml` run
[`36137483144`](https://github.com/AvihaiShai/act-on-weather/actions/runs/36137483144),
conclusion **success**, dispatched for `1890eba` with the label
`f10-evidence-2026-09-25`. Before this, the clean-engine gate had only ever run
for `b21885a`, which §8 had to record as a gap. It no longer is:

| Assertion | Measured |
|---|---|
| Clean install engine ID | `9567a107-f6d9-485e-9e54-8eabd06d40b7` |
| Packaging engine ID | `bab1ddb9-e66f-4080-9fbf-9a64e0823a66` — **distinct** |
| Store before load | `0 images, 0 volumes, 0 containers` |
| Release tags already held | none of 10; archive verification found their config and layers in `images.tar` |
| Install | `--pull never`, `AOW_REQUIRE_CLEAN_IMAGE_STORE=1` |
| Data smoke | `PASS: API, stored forecast, scores, agent, model, UI and edge` |
| Serving | `Release 1890eba… is serving on ports 8080 and 8000` |

Two of those lines —

```
engine: 9567a107-f6d9-485e-9e54-8eabd06d40b7 docker 29.8.1 on Alpine Linux v3.24 (containerized) x86_64
store before load: 0 images, 0 volumes, 0 containers
```

— are the instrumentation added in §4, running in the release pipeline. That is
independent confirmation that the installer now emits engine identity and a full
store census, rather than my word for it.

**This does not close F10, and must never be recorded as doing so.** It is a
second daemon on a *connected, GitHub-hosted runner*. The engine was clean; the
machine was on the internet, and it is not separate physical hardware. What it
does establish is that the bundle for this exact commit installs and serves on
an engine that has never held its images — which is the defect class from
[RELEASE-PROOF §1](RELEASE-PROOF.md#1-the-defect-this-drill-found), not the
air-gap claim.

### 3.3 The release artifact

`bash scripts/package-offline.sh` on the connected staging machine. Wall clock
**2 min 26 s**, exit **0**.

| | |
|---|---|
| Path | `dist/aow-1890eba16628683c96d650eaa04479b9989c45e5/` |
| `release-version.txt` | `1890eba16628683c96d650eaa04479b9989c45e5` |
| **`sha256(SHA256SUMS)`** — the out-of-band anchor | **`2edf2cd2fba4332ee6cb2d3cbc7e6959b2cc4be44b622b14882dc3dedb4ff6b8`** |
| `sha256(images.tar)` | `1d089e3848fa88b2f310ca851968047c6609ec0ae51ed32a874fb3e1f7c5b496` |
| `images.tar` | 1,221,857,792 bytes, 10 images |
| Folder | 218 files, 2,511,332,623 bytes (2.34 GiB) |
| `.env` | absent — `install-offline.sh` will refuse until one exists (§6.1) |

That anchor digest is newly recorded for this commit. It is the only check on
the offline host that does not come out of the folder being checked, and it has
to travel by a different route than the media to mean anything (§5.4).

**It is a property of this build, not a constant of the commit.** `docker save`
does not promise byte-identical archives across runs, and the `release.yml`
bundle differs further because it seals a `promotion-record.json` the locally
rebuilt bundle does not contain. So an operator who repackages `1890eba` will
get a different `SHA256SUMS` digest, and that is not a discrepancy to chase —
the digest to carry out of band is the one their own packaging run printed.
The `services` and `ui` images are anchored to the CI manifest, the upstream
references to committed `IMAGES.lock`, and the separately built `demos` image
to this bundle's own verified lock. A later rebuild of the same commit may have
a different `demos` digest; see [RELEASE.md](RELEASE.md).

### 3.4 Independent verification of that artifact

Run from the checkout rather than from inside the bundle, with the anchor
supplied:

```
AOW_SHA256SUMS=sha256:2edf2cd2…f6b8 \
  bash scripts/airgap-evidence.sh --out "$EV/verify.txt" \
  run -- bash scripts/verify-bundle.sh dist/aow-1890eba…/
```

| Measurement | Value |
|---|---|
| `out-of-band anchor` | **ENFORCED** |
| `images.bundle.lock` | matches the CI manifest for `1890eba…` and the committed `IMAGES.lock` |
| `images.tar` | matches `images.bundle.lock` (10 images, by verified manifest digest); every image has its config and layers, as `linux/amd64` |
| exit code | **0** |
| elapsed | 15 s |
| completed image pulls | **0** |
| transcript pull/build markers | **0** |

And the cross-check the installer cannot make for itself, since the code that
gates the bundle ships inside the bundle:

| File in bundle | vs `git show 1890eba:` |
|---|---|
| `verify-bundle.sh`, `verify-bundle-images.sh`, `bundle-image-manifests.sh` | byte-identical |
| `install-offline.sh`, `prove-offline.sh`, `airgap-evidence.sh` | byte-identical |
| `make-fault-injection-bundle.sh`, `IMAGES.lock`, `models.lock` | byte-identical |

The comparison above is a statement about packaging: every gating script in the
folder is the commit's own. It is **not** a statement that the proof wrapper
works. Its `prove-offline.sh` does differ from `a21dff9`'s, but the difference
is the addition of `--no-build` — and Compose **v5.5.1 rejects**
`docker compose run --no-build` with `unknown flag: --no-build`. So the flag is
the defect, not the fix. On a v5 target neither staged folder has a runnable
proof wrapper: `1890eba`'s aborts at the flag before the proof starts, and
`a21dff9`'s, which carries neither the flag nor `demos.build: !reset null` in
`compose.tools.bundle.yml`, falls back to *building* the demos image and so
needs the egress the proof exists to show is absent. The wrapper was actually
fixed in PR #60, merged as `fb2a1e4`: `--no-build` removed,
`demos.build: !reset null` added to the tools bundle overlay, `run --pull never`
retained, and `package-offline.sh` taught to refuse to seal a bundle whose
`demos` service still has a build recipe. Nothing before `fb2a1e4` carries that
fix.

### 3.5 The fault-injection artifact, built and its failure mode measured

Derived from the verified `1890eba` bundle in **43 s**:

| | |
|---|---|
| Source release | `1890eba…`, `sha256(SHA256SUMS)` `2edf2cd2…f6b8` (CI-anchored) |
| Artifact digest | `sha256(SHA256SUMS)` `5d253ff59e54a7249dfd9230aa2714ac4ce1339b5d96ef1c1c39ccc7db5f01ce` — **a locally mutated folder, not a release digest** |
| Verification | passed, and printed the `THIS IS A FAULT-INJECTION TEST ARTIFACT` banner |
| Images | `images.tar`, `images.bundle.lock`, `ci-images.lock`, `IMAGES.lock`, `models.lock`, `release-version.txt` byte-identical to the source |

**The installer's refusal was verified at runtime**, not only in a unit test.
Method, because it cannot be reproduced from the folder as it sits on disk:
`install-offline.sh:14` checks for `.env` *before* the fault-injection guard, so
a throwaway `cp .env.example .env` was made first, the run observed, and that
file deleted again — which is why the artifact has no `.env` now. With it in
place the script exited **1**, printed
`refusing to install a fault-injection test artifact without AOW_ALLOW_FAULT_INJECTION=1`,
and produced **zero** `Loaded image` lines: it stops before `docker load`.

Note that exit 1 and zero `Loaded image` lines are also what a *missing* `.env`
produces, so neither number discriminates on its own. The quoted message is the
evidence; the two counts only confirm nothing was loaded.

The migration was applied to a throwaway **Postgres 17** container — the same
digest-pinned image the stack uses — under `ON_ERROR_STOP=1`, because a
migration that failed in the *wrong way* would make the drill prove nothing:

```
CREATE TABLE
INSERT 0 1
ERROR:  column "this_column_does_not_exist_and_the_migration_must_fail_here"
        of relation "fault_injection_marker" does not exist
psql exit: 3
```

and afterwards, on the same database:

```
select count(*) from fault_injection_marker  ->  1
```

That last line is the point of the drill. `psql` exited non-zero, so the migrate
service fails and the upgrade stops — **and the schema change survived the
failure.** Rolling the images back cannot undo it, which is what makes the
dump-backed restore the only recovery path. The container was removed
afterwards.

### 3.5.1 The prepared drill set

All three are built and verified, waiting on the hardware in §5:

| Role | Artifact | `sha256(SHA256SUMS)` | Provenance |
|---|---|---|---|
| Release A (upgrade from) | `dist/aow-a21dff9…` | `1bb8b7e9…d205` | CI-proven, run `36072399083` |
| Release B (upgrade to) | `dist/aow-1890eba…` | `2edf2cd2…f6b8` | CI-proven, run `36135517229` |
| Fault injection (from B) | `dist/faultinj-1890eba` | `5d253ff5…01ce` | **Local mutation, not a release** |

A is used only as the *earlier* release in the upgrade sequence, which is what
that role requires. It is not the artifact under proof; B is.

### 3.5.2 The transfer medium, now actually exercised

Until 2026-09-26 this was the one hop with measured *properties* and no measured
*copy*: [RELEASE-PROOF §4](RELEASE-PROOF.md#4-what-is-still-not-proven) recorded
"a real transfer medium" as unproven, and the only transfer ever performed was a
drvfs hypervisor share. A USB stick is now attached and the copy has been made
and verified **on the medium**.

| | |
|---|---|
| Medium | `I:` `CORSAIR`, **FAT32**, 31.99 GiB, effectively empty |
| Method | `(cd src && tar -cf - .) \| (cd dst && tar -xf -)` — the documented method, not a file manager |
| Release B copy | 2 min 55 s, 218 files, 2,511,332,623 bytes |
| `sha256(images.tar)` on the medium | `1d089e3848fa88b2f310ca851968047c6609ec0ae51ed32a874fb3e1f7c5b496` — **identical to source** |
| `sha256(SHA256SUMS)` on the medium | `2edf2cd2…f6b8` — identical to source |
| Verification run **on the medium** | out-of-band anchor **ENFORCED**, exit **0**, 15 s, 0 pulls, 0 pull/build markers |

Two things this settles that the earlier size-and-mode analysis could only
predict. FAT32 carries the bundle: the largest file is the model at
1,282,439,264 bytes, comfortably under the 4 GiB per-file cap. And FAT32's
inability to store Unix modes costs nothing here, because every tracked file is
mode `100644` and every script is invoked as `bash scripts/…` rather than by its
executable bit — which is why that property was worth measuring in the first
place.

All three drill artifacts were copied the same way and **each was verified on
the medium**, with its own out-of-band anchor enforced and exit 0:

| On the medium | Anchor supplied | Result |
|---|---|---|
| `aow-1890eba…/` — release B | `sha256:2edf2cd2…f6b8` | **ENFORCED**, exit 0, 218 files, 0 pulls |
| `faultinj-1890eba/` — fault injection | `sha256:5d253ff5…01ce` | **ENFORCED**, exit 0, 220 files, 0 pulls, and the `THIS IS A FAULT-INJECTION TEST ARTIFACT` banner fired from the stick |
| `aow-a21dff9…/` — release A | `sha256:1bb8b7e9…d205` | **ENFORCED**, exit 0, 0 pulls |
| `airgap-evidence.sh` | — | Standalone copy, outside every bundle. Release A predates the tooling and its installer cannot report its own install (§6.5), so the target needs this independently of whichever bundle it is installing |

Stick occupancy **measured 2026-09-26, immediately after this copy**, with only
the three `1890eba`-era folders and the standalone capture script on it: 7.1 GiB
of 32 GiB, leaving room for the Docker `.deb` set and the `.env`. The separately
verified `8975403` bundle folder (2.34 GiB) was staged afterwards, and
**occupancy has not been re-measured since** — the stick is currently attached
to a VMware guest and cannot be read from here. 7.1 GiB is therefore the
historical figure for the A/B/fault-injection set, not current occupancy;
re-measure before relying on the remaining space.

Two details worth recording because they are easy to get wrong. Each artifact
verified against **its own** digest — passing release B's anchor to the
fault-injection folder is refused, which is what stops a mutated artifact being
presented under a release's digest. And the fault-injection banner survives the
copy, because the marker is sealed into `SHA256SUMS` rather than merely present.

**This is transport evidence, not air-gap evidence.** No target host exists yet,
nothing was installed from the medium, and F10 is unchanged. What it removes is
the risk that the bundle could not survive the hop at all.

### 3.6 The staging host, recorded so the target's can be compared against it

Captured with `bash scripts/airgap-evidence.sh host staging`:

| | |
|---|---|
| Machine | `Avihai_Shai`, ASUS, 32 GiB RAM, Windows 11 Pro 26200 |
| Engine ID | `375fa6b6-973d-435a-ad5d-9bfe642fb556` |
| Docker / Compose | 29.8.0 / 5.5.1 |
| Storage driver | `overlayfs` (containerd image store) |
| Store | **216 images, 35 volumes, 17 containers** |

That last row is the reason this machine cannot be the target. An install here
proves nothing about the archive, for the reason in
[RELEASE-PROOF §1](RELEASE-PROOF.md#1-the-defect-this-drill-found), and this
document does not record one.

### 3.7 The install was deliberately not run here

No `install-offline.sh` run appears in this document. Running it on the
packaging host would produce a passing transcript that is evidence about the
installer and nothing about the bundle — the precise mistake that
[RELEASE-PROOF §5](RELEASE-PROOF.md#5-superseded-record) supersedes an earlier
record for. The artifact is verified from its bytes (§3.4); it is installed for
the first time on the target, or not at all.

---

## 4. Tooling added so the drill produces artifacts rather than recollections

The previous checklist asked for nine pieces of evidence and named a producing
command for three. These changes close that gap; they are the part of F10 that
could be done without the hardware.

| Change | Why |
|---|---|
| `scripts/airgap-evidence.sh` (new) | `host`, `bundle` and `run` subcommands that emit engine ID, full store census, link state, bundle digests, exit codes, elapsed time and a **measured pull count**. "0 pull attempts" was previously a sentence in a document with no command behind it |
| `scripts/verify-bundle.sh` | Prints `out-of-band anchor: ENFORCED` or `NOT SUPPLIED`. Before, a skipped anchor check and a passed one produced identical output, so the strongest sentence in the release proof could not be verified from its own transcript |
| `scripts/install-offline.sh` | Prints the engine ID, Docker version, OS and a full image/volume/container census before the load. Both were previously available only from the CI clean-engine job |
| `scripts/prove-offline.sh` | Added `--no-build`. **The diagnosis, 2026-09-25, and it still holds:** `compose.tools.yml` still carries a `build:` section, so a missing `aow-bundle/demos:<sha>` tag made Compose *build* the image, and `demos/Dockerfile`'s `apk add` then needs the egress the proof exists to show is absent — turning a tag problem into a network error on the one host where that reads as a failed proof. **The remedy was wrong, and this row is kept as the historical record of it:** Compose v5.5.1 rejects `docker compose run --no-build` outright (`unknown flag: --no-build`), so on a v5 target the wrapper aborts before the proof starts. PR #60 / `fb2a1e4` replaced the flag with `run --pull never` plus `demos.build: !reset null` in `compose.tools.bundle.yml`, which *removes* the build recipe rather than asking Compose to suppress it — the same tag-becomes-a-build problem, solved without an unsupported flag |
| `scripts/airgap-evidence.sh --out FILE` | `cmd \| tee file` returns *tee's* exit status, so a failed install reads as a pass. `--out` writes the file itself and keeps the measured exit code. It also **refuses a path inside a release bundle**, because the old procedure's `tee evidence/…` created a file `SHA256SUMS` does not list — the evidence run would have destroyed the artifact it was measuring |
| `scripts/make-fault-injection-bundle.sh` (new) | Derives the deliberately-broken artifact the rollback drill needs, from a verified bundle, with sealed provenance. §6.5 |
| `verify-bundle.sh` / `install-offline.sh` fault-injection guard | A fault-injection artifact verifies — the drill installs it — so the success line must not read like a release. Both print a banner; the installer refuses outright without `AOW_ALLOW_FAULT_INJECTION=1` |
| `tests/unit/test_airgap_evidence.py` (new, 15 cases) | The capture script is a measuring instrument, so the tested failure mode is a confident zero: a missing `ip` must report `UNKNOWN`, not "no default route" |
| `tests/unit/test_bundle_tamper.py` (+9 cases) | The anchor transcript lines, the no-build fallback, and the fault-injection artifact's marking, resealing, image-anchor preservation and refusal paths |

The pull counter was validated against a real pull on the staging engine:
`completed_image_pulls: 1`, `pulled_images: 1 x hello-world`, including the
harder "image is up to date" case, which still emits the event.

It records **names, not just a count**, after a run on the development machine
reported two pulls for a command that only echoed a string. That was the
platform's own background image work landing in the measurement window, and a
bare number months later cannot be told apart from a bundle image the install
fetched. The criterion that decides a drill is therefore *which* image was
pulled: any `pulled_images` line naming an `aow-bundle/*` image is a failed
proof.

---

## 5. What is missing, exactly

Measured on this machine on 2026-09-25:

- **Physical hosts available: one.** `Avihai_Shai`. The only other Linux
  environments are WSL2 distributions (`docker-desktop`, `Ubuntu-24.04`) on that
  same machine, which are not separate hardware.
- **Removable media attached: none.** `Win32_LogicalDisk` reports four fixed
  disks (`C:`, `D:`, `E:`, `F:`, all `DriveType 3`) and no removable or optical
  volume. `Win32_DiskDrive` reports four internal Samsung SSDs and no USB
  storage. Re-checked after the merge, with the same result.
- **Reachable LAN hosts: none usable.** The IPv4 neighbour table holds the
  gateway, two ASUS network devices and the WSL virtual adapter. No second
  general-purpose machine.

The Hyper-V feature state could not be re-read without elevation. It does not
matter: a VM on this machine would not satisfy F10 either.

**To close F10, the following prerequisites must be accounted for.** The
removable-media transfer has since been completed (§3.5.2); the historical
host census above records the earlier staging day.

1. **A second physical x86_64 machine** that has never held these images:
   Ubuntu 24.04, ≥8 GiB RAM available to Docker, ≥9 GiB free disk (≈6 GiB for
   the stack, 2.4 GiB for the bundle). It must be **disposable** if the
   destructive drills in §6.5 are run, because those wipe the database by
   design and every release folder installs over the same named volumes.
2. **Removable media**, measured against the artifacts that are actually built:
   release B alone is 2,511,332,623 bytes (2.34 GiB); the full A + B +
   fault-injection set is 7,533,928,413 bytes (7.02 GiB). With the Docker `.deb`
   set alongside it, **a 16 GB stick** is the comfortable choice. The largest
   single file is the model at 1,282,439,264 bytes (1.19 GiB), under FAT32's
   4 GiB per-file cap — but exFAT avoids the question. Copy with `tar` or
   `rsync`, never a file manager (§6.2). **Prepared:** the three artifacts and
   capture script are verified on a 32 GiB FAT32 stick (§3.5.2).
3. **Docker CE installation media for an offline Ubuntu host** — the `.deb` set
   including `docker-compose-plugin`, since `apt` cannot reach the archive on a
   disconnected machine and Compose **v2** is required. This is the step most
   likely to be discovered too late, standing at an unplugged machine.
4. **An out-of-band channel for the `SHA256SUMS` digest** (§3.3). If the same
   person carries the digest and the media, the anchor checks that the media was
   not corrupted, not that it was not substituted. The recorded digests were
   enforced during staging; the target run must record how its operator obtained
   the digest separately from the medium and what trust claim follows.

---

## 6. Ready-to-run procedure

Ordered as an operator meets it. Steps 6.1–6.2 happen **connected**; the link is
cut in 6.3 and stays cut.

**Before anything else, on each machine in turn**, set the evidence directory.
It is two machines, so this is done twice — once on staging, once on the target
— and both sets of files are collected at the end:

```bash
EV=$HOME/aow-evidence && mkdir -p "$EV"
```

It must be outside any bundle folder. `--out` enforces that (§6.4), but knowing
why saves a confusing refusal at the worst moment.

### 6.1 On the connected staging machine

1. Check out the release commit and confirm the tree is clean.
2. Download the `aow-images-<sha>` artifact from that commit's successful
   `ci.yml` run.
3. `bash scripts/package-offline.sh <path>/images.lock`
4. `bash scripts/airgap-evidence.sh --out "$EV/bundle.txt" bundle dist/aow-<sha>`
   — record `sha256(SHA256SUMS)` and `sha256(images.tar)`.
5. `bash scripts/airgap-evidence.sh --out "$EV/host-staging.txt" host staging`
   — this is where the staging **engine ID** is captured. It cannot be captured
   later, and the drill needs both IDs to show they differ.
6. **Generate `.env` here, not on the target.** `install-offline.sh` refuses
   without one, and the offline host has no way to make one: the bundle carries
   the password-generator image retagged as `aow-bundle/stage:<sha>`, while
   `bootstrap.sh` looks it up by its digest-pinned `python:3.12-slim@sha256:…`
   reference and will not find it. Run `bash scripts/bootstrap.sh` connected, or
   copy `.env.example` and replace every `change-me` with a different value.
   Carry `.env` **separately from the bundle folder**. Dropping it in afterwards
   is by design, and two different scripts make that safe:
   `package-offline.sh` excludes any file named `.env` when it seals
   `SHA256SUMS`, and `verify-bundle.sh` exempts the same name from the
   unlisted-file check. Neither one does both.
7. **Download the Docker CE `.deb` set for the target's Ubuntu release**, and
   put it on the medium beside the bundle. `apt` cannot reach the archive from
   a disconnected host, so this cannot be done later, and it is the step most
   often discovered too late — standing at an unplugged machine with no way to
   install the thing everything else needs. The set is `containerd.io`,
   `docker-ce`, `docker-ce-cli`, `docker-buildx-plugin` and
   **`docker-compose-plugin`** (Compose **v2** is required; `apt install
   docker.io` is not sufficient). Record their filenames and `sha256sum`s.
8. **Copy `scripts/airgap-evidence.sh` onto the medium as a standalone file**,
   outside any bundle. It is a capture tool, not part of the artifact, and the
   target needs it before and between installs — including when the bundle
   being installed is an older release that does not contain it (§6.5).
9. Carry the `SHA256SUMS` digest out of band (§5.4).
10. If the destructive drills are planned, prepare their extra artifacts now
    (§6.5).

### 6.2 Transfer

Copy with `tar` or `rsync`, **not a file manager**, on both hops — staging →
medium and medium → target. A file manager adds `desktop.ini`, `Thumbs.db` or
`.DS_Store`, and `verify-bundle.sh` refuses the folder on any unlisted file. The
measured properties that make the copy possible are in
[RELEASE-PROOF §2](RELEASE-PROOF.md#what-the-bundle-needs-from-a-transfer-medium-measured):
largest file 1.28 GB (under FAT32's 4 GiB cap) and every tracked file mode
`100644`. The "longest path 41 characters" figure carried in
[RELEASE-PROOF §2](RELEASE-PROOF.md#what-the-bundle-needs-from-a-transfer-medium-measured)
is **wrong**: measured on this artifact the longest is 63 characters
(`./observability/grafana/provisioning/datasources/prometheus.yml`), and the
same holds for the earlier bundles it was originally measured on. The
conclusion is unaffected — FAT32 carries it comfortably — but the number should
not be quoted.

### 6.3 On the target, before anything else

1. Install `docker-ce` and the Compose v2 plugin from the carried `.deb` set.
   **Add the operator to the `docker` group and log back in** — every command
   below starts with `docker info`, and without group membership they all fail
   on permissions.
2. **Now cut the link, in hardware**: unplug ethernet, switch Wi-Fi off at the
   hardware switch or remove the adapter, and account for USB-Ethernet,
   Bluetooth PAN, tethering and any BMC/IPMI port. Not a firewall rule, not
   `ip link set down` — the point is that the isolation is not enforced by
   software the stack could influence.
3. **Set this machine's evidence directory.** The target is a different
   machine from §6.1, so it needs its own — nothing carried over:

   ```bash
   EV=$HOME/aow-evidence && mkdir -p "$EV"
   ```

   Without it, `--out "$EV/…"` expands to `/host-target.txt` and the capture
   fails on permissions, or writes to the filesystem root as root.
4. From **the copied bundle folder** (or using the standalone copy of the
   script from §6.1.8 by absolute path — either works, it only reads):

   ```bash
   bash scripts/airgap-evidence.sh --out "$EV/host-target.txt" host target
   ```

   This is the census **before the load**. It must show a different engine ID
   from §6.1.5, and `images: 0`, `volumes: 0`, `containers: 0`.
5. Photograph the disconnected link, and keep `ip -br link` from step 4. Both
   are point-in-time samples; the photograph is what makes them a claim about
   the window.

### 6.4 Install and prove

**STOP — no bundle currently on the stick can complete this step.** Every folder
staged on the CORSAIR stick — `aow-a21dff9…`, `aow-1890eba…`,
`faultinj-1890eba` and `aow-8975403…` — was packaged **before** PR #60
(`fb2a1e4`), so every one of them carries a `prove-offline.sh` that cannot run
the proof on the target's own Compose version. The `1890eba`-era wrapper passes
`docker compose run --no-build`, which Compose v5.5.1 rejects outright with
`unknown flag: --no-build`; `a21dff9`'s omits the flag but also predates
`demos.build: !reset null`, so a missing `aow-bundle/demos:<sha>` tag makes
Compose try to *build* the image and the build's `apk add` reaches for the
absent egress (§3.4, §4). Either way the last command in the block below aborts
at the wrapper, on an unplugged machine, with no way to debug it there.

**A bundle re-staged from `fb2a1e4` or later is a prerequisite for this
procedure.** A verified bundle for `f70c28d` exists **on the development machine
only**; its out-of-band anchor `sha256(SHA256SUMS)` is
`923ebaac3ef445aa25c40cbf93961c1a1cb68bc5ca57a203e232ec90281887b3`. It has
**not** been copied to the stick or to any other removable medium, so the
transfer hop (§3.5.2) has not been exercised for it. Steps 6.1–6.3 and the
verify and install commands below are unaffected; only `prove-offline.sh` is
blocked, and until a post-`fb2a1e4` bundle is on the medium the drill cannot
produce rows 15 and 16 of §7.

**Evidence goes outside the bundle, and never through a pipe.** Both rules are
corrections of an earlier draft of this procedure:

- `tee evidence/…` from inside the bundle creates a file `SHA256SUMS` does not
  list, so the next `verify-bundle.sh` refuses the folder — the evidence run
  destroying the artifact it was measuring.
- `cmd | tee file` returns *tee's* exit status, so a failed install reads as a
  pass, in the one situation where the operator can least afford to miss it.

`--out` fixes both: it writes the file itself, refuses a path inside a bundle,
and exits with the measured command's own code.

From inside the copied bundle folder, with `.env` in place:

```bash
EV=$HOME/aow-evidence            # the TARGET's copy; staging has its own
mkdir -p "$EV"

bash scripts/airgap-evidence.sh --out "$EV/bundle-on-target.txt" bundle .
# sha256(images.tar) must equal the value from §6.1.4

export AOW_SHA256SUMS=sha256:<digest carried out of band>
bash scripts/airgap-evidence.sh --out "$EV/verify.txt" \
  run -- bash scripts/verify-bundle.sh .
# must print "out-of-band anchor: ENFORCED"

AOW_REQUIRE_CLEAN_IMAGE_STORE=1 \
  bash scripts/airgap-evidence.sh --out "$EV/install.txt" \
  run -- bash scripts/install-offline.sh

# Requires a bundle packaged from fb2a1e4 or later -- see the warning above.
# Every folder on the stick today fails here at the wrapper, not in the proof.
bash scripts/airgap-evidence.sh --out "$EV/prove.txt" \
  run -- bash scripts/prove-offline.sh
```

Check `echo $?` after each, or run them under `set -e`. The exit code is now
the command's own, so a non-zero one is a real failure and must be recorded as
such rather than rerun until it passes.

`prove-offline.sh` runs five sections: Docker forbids a route out of the
application network; the services cannot reach the internet, demonstrated; no
cloud LLM anywhere in the repo; the two reviewer questions answered from stored
data ("What is the weather tomorrow in Rome?" and the London activities
question); and a question outside the coverage window, which must answer "no
data" rather than guess.

**The `backup-restore` proof used to be unsafe here, and was fixed.**
`demos/06_backup_restore.sh` starts helper containers from
`${AOW_SERVICES_IMAGE:-aow/services:dev}`. After a bundle install that tag does
not exist — the images load as `aow-bundle/services:<sha>` — and `docker run`
on a missing tag contacts the registry, which on a disconnected host is the one
thing this whole exercise exists to rule out. Nothing set the variable. Now:

- `compose.tools.bundle.yml` sets `AOW_SERVICES_IMAGE` to
  `aow-bundle/services:${AOW_IMAGE_VERSION}`;
- both `docker run` calls pass `--pull never`;
- the script checks the image is present up front and fails with a message
  naming the real problem, the same shape `scripts/restore-state.sh` uses.

The proof is still excluded from the default `offline` run, so this matters
only when the drill invokes it deliberately — which §6.5 step 8 does.

### 6.5 Destructive drills — disposable target only

These wipe the database by design, and `compose.yml` fixes the project name so
every release folder installs over the same named volumes. Only run them on a
host nobody expects to keep.

**This is a second run, and the record must say so.** §6.4 proves the release
on a pristine target: empty store, clean-store install, offline answers. That
result is destroyed the moment an upgrade sequence starts, and an engine that
already holds release B's tags will refuse
`AOW_REQUIRE_CLEAN_IMAGE_STORE=1` anyway. So the order is:

1. **Run 1 — the air-gap proof (§6.4).** Clean-store first install of release B
   on a pristine target. Capture everything in §7. This is the run that answers
   F10.
2. **Reset the target.** `docker compose down -v`, then remove every image and
   volume, or reimage the machine. Confirm with
   `airgap-evidence.sh --out "$EV/host-target-reset.txt" host target-reset` —
   it must again report `0 images, 0 volumes, 0 containers`.
3. **Run 2 — the destructive sequence below.** Capture it separately, into
   filenames that cannot be confused with Run 1's.

Two runs, two sets of evidence, no overlap. A record that blends them cannot
show that the clean-store install was ever clean.

Prepare on the connected staging machine:

- **Two distinct CI-proven releases**, A and B, each packaged from its own
  successful `ci.yml` run. Record both commits **and both migration lists** —
  the drilled A/B measurements in RELEASE-PROOF §3 predate
  `004_itinerary_delete.sql` and `005_user_data_wipe.sql` and do not cover the
  current tree.
- **A deliberately failing migration**, built by
  `scripts/make-fault-injection-bundle.sh <verified-bundle> <destination>`.

  This gap used to be open, and the reason it had to be closed by a script
  rather than by a commit is worth stating: CI can never publish images for a
  commit whose migration is written to fail — `main` is branch-protected on
  four required jobs — and `package-offline.sh` refuses any tree that is not
  exactly the commit named in its `images.lock`. So the artifact is derived
  locally, from a bundle that *was* CI-proven.

  What the script does, and what it costs:

  | | |
  |---|---|
  | Verifies the source bundle first | Deriving from a folder that never verified would prove nothing about either |
  | Adds `db/migrations/900_fault_injection.sql` | Creates a table and inserts a row, **then** fails on a missing column. The schema is already altered when it stops, which is what makes an image rollback insufficient and the dump-backed restore necessary |
  | Adds one `-f` argument to the migrate command | `compose.yml` lists migrations explicitly; there is no directory glob |
  | Writes `FAULT-INJECTION.json` | Records the source release, the **source CI-anchored digest**, every mutation, and every file left untouched |
  | Reseals `SHA256SUMS` | **This destroys the folder's self-attestation.** The digest it prints attests to a locally mutated folder and is *not* a release digest |
  | Leaves the images alone | `images.tar`, `images.bundle.lock`, `ci-images.lock`, `IMAGES.lock`, `models.lock` and `release-version.txt` are copied byte-for-byte, so every container image is still the digest-pinned set CI published |

  The marker is sealed into `SHA256SUMS`, so deleting it breaks verification.
  `verify-bundle.sh` prints a banner when it is present, and
  `install-offline.sh` **refuses to install** without
  `AOW_ALLOW_FAULT_INJECTION=1`. Inspect any such artifact with
  `bash scripts/make-fault-injection-bundle.sh --diff <dir>`.

  **Never describe this artifact as a release.** It is a test fixture that
  happens to verify.

Two things the procedure must not get wrong:

- **Carry release A's `.env` into release B's folder.** Do not generate a new
  one. `POSTGRES_PASSWORD` and `RABBITMQ_PASSWORD` are baked into the volumes
  when Postgres and RabbitMQ first initialise, and a new `.env` does not
  re-initialise them — it just fails to authenticate.

  To be precise about the failure, because the script promises less than it
  might appear to: `install-offline.sh` has **no password-mismatch check**. It
  verifies the bundle, loads the images, then runs `dc up -d` and the release
  smoke check. So a wrong `.env` is not caught by a guard at all — it surfaces
  as services that will not come up, by which time `docker load` has already
  replaced the images. The consequence is the one that matters: there is no
  early abort to rely on, so get the `.env` right before starting.
- **Drop `AOW_REQUIRE_CLEAN_IMAGE_STORE=1` for the rollback.** Re-installing A
  on an engine that already holds A's tags is a hard failure with that variable
  set. It belongs on the first install only.

Expect the rollback to take **≈508 s**: the release smoke check waits out its
full 480 s deadline before reporting the still-empty forecast. A run that looks
hung at eight minutes is not hung.

#### Release A cannot report its own install

A is an older release, and its `install-offline.sh` predates the evidence
instrumentation: **no engine line and no `store before load` census.** In the
sequence below A is what gets installed first, onto the clean target — which is
exactly where §7 rows 5, 12 and 13 want those lines.

The workaround, and it must be recorded as one: capture `host` immediately
before installing A, using the standalone script from §6.1.8:

```bash
bash /media/<stick>/airgap-evidence.sh --out "$EV/run2-00-host-before-A.txt" host target-reset
```

That yields the same engine ID and the same `0/0/0` census from outside the
installer. Rows 12 and 13 are then filled from *that* capture for Run 2, and
from `install.txt` for Run 1, where release B's own installer prints them. Say
which in the record; do not present A's install log as containing lines it
cannot contain.

#### The sequence, in order

On the reset target, still disconnected, each step under
`airgap-evidence.sh --out "$EV/run2-<step>.txt" run -- …`:

| # | Step | Expected |
|---|---|---|
| 1 | Install **A** (`AOW_REQUIRE_CLEAN_IMAGE_STORE=1`, A's own fresh `.env`) | exit 0; store census `0/0/0` before load |
| 2 | Ingest so there is data to lose — record the row counts | non-zero counts, recorded |
| 3 | Upgrade to **B** (copy A's `.env` into B's folder; **no** clean-store flag) | exit 0; pre-upgrade dump written to `backup/` — note its filename |
| 4 | Verify B serves and the row counts survived | counts match step 2 |
| 5 | "Upgrade" to the **fault-injection artifact** with `AOW_ALLOW_FAULT_INJECTION=1` | **exit non-zero**; migrate fails; a pre-upgrade dump is written first |
| 6 | Confirm the schema was altered anyway | `fault_injection_marker` exists — this is why an image rollback is not enough |
| 7 | Roll back to **B**'s images (no clean-store flag) | images revert; the smoke check still **fails**, ≈508 s, because the schema is still broken |
| 8 | Restore from the dump taken at step 5 | exit 0; row counts match step 2 again; `fault_injection_marker` gone |

Step 7 failing is the result, not a problem: it is what demonstrates that an
image rollback alone cannot recover a migration failure. Record it as a pass of
the drill and a failure of the rollback-only path. If step 7 *succeeds*, the
fault artifact did not do its job and the drill is void.

---

## 7. Evidence capture sheet

Every row has a producing command. Fill in, attach the transcripts, and record
failures rather than reruns.

| # | Evidence | Command | Value |
|---|---|---|---|
| 1 | Source commit and tree | `git rev-parse HEAD; git rev-parse HEAD^{tree}` | |
| 2 | CI run id and conclusion for that commit | `gh run list --branch main` | |
| 3 | Staging engine ID | `airgap-evidence.sh --out … host staging` | |
| 4 | Target engine ID — **must differ from 3** | `airgap-evidence.sh --out … host target` | |
| 5 | Target store before load — **must be 0 / 0 / 0** | same capture as 4 | |
| 6 | Link state on target + photograph | `ip -br link` in capture 4, plus photo | |
| 7 | `sha256(images.tar)` on staging | `airgap-evidence.sh --out … bundle dist/aow-<sha>` | |
| 8 | `sha256(images.tar)` on target — **must equal 7** | `airgap-evidence.sh --out … bundle .` | |
| 9 | Out-of-band `SHA256SUMS` digest, and how it travelled | recorded at §6.1.9 | |
| 10 | Anchor enforced | `verify.txt` must contain `out-of-band anchor: ENFORCED` | |
| 11 | Verify: exit code, elapsed | `verify.txt` | |
| 12 | Install: engine line and `store before load` census | `install.txt` | |
| 13 | Install: clean-store census line | `install.txt` | |
| 14 | Install: exit code, elapsed, **completed pulls**, **pulled image names**, **pull/build markers** | `install.txt` | |
| 15 | Prove: exit code and all five sections | `prove.txt` | |
| 16 | The two reviewer questions and the out-of-coverage answer, with as-of stamps | `prove.txt` §4 and §5 | |
| 17 | Transport wall clock | `time tar …` on both hops | |
| 18 | Target Docker version, Compose version, storage driver | capture 4 | |

### Pass / fail

The run **passes** only if all of these hold. Anything else is a recorded
result, not a pass:

- rows 3 and 4 differ;
- row 5 is `0 images, 0 volumes, 0 containers`;
- rows 7 and 8 are equal;
- row 10 says `ENFORCED`;
- every exit code is 0;
- **no `pulled_images` line names an `aow-bundle/*` image**, and pull/build
  markers is 0, in rows 14 and 15;
- the coverage question in row 16 answers "no data", not a guess.

A non-zero marker count with zero completed pulls is the signature of a pull
that was attempted and failed — which on a disconnected host means something in
the chain still wants the network, and is a **failure**, not a pass with noise.

---

## 8. What this evidence will and will not establish

**Will**: that the bundle for the tested commit is self-sufficient on hardware
that has never held these images, with no network present.

**Will not**:

- **Cover any commit but `1890eba`.** This is not a formality, and it has
  already bitten once: the `a21dff9` artifact prepared earlier in this work
  contains none of the evidence tooling and none of this round's proof-wrapper
  changes, so it was re-packaged rather than carried forward. (The wrapper
  change of that round, `--no-build`, was itself wrong and was replaced in
  PR #60 / `fb2a1e4` — a second instance of the same lesson: no bundle inherits
  a later commit's fix. See §3.4 and §6.4.) `1890eba` has its own `ci.yml` run
  `36135517229` and its own `release.yml` clean-engine run `36137483144`
  (§3.2.1) — neither of which is inherited from any earlier commit, and neither
  of which is an air-gap proof.

  One consequence to state plainly: **the commit that adds this document is not
  `1890eba`.** A document recording a proof cannot be inside the artifact it
  describes. That follow-up commit changes documentation only — no script, lock
  file or image — so the verified artifact remains the one named in §3.3. Any
  commit that changes a script, a migration, `compose.yml` or an image needs its
  own `images.lock`, its own bundle and its own proof run. An evidence record
  inherited across a merge is not evidence.
- **Establish a trust root on the offline side.** `SHA256SUMS`,
  `images.bundle.lock` and `ci-images.lock` all live in the folder they attest,
  and the verifier that runs is the bundle's own copy. §3.4's comparison against
  `git show` and the out-of-band digest are what stand in for it. There is no
  `cosign` signature and no build provenance.
- **Prove the absence of egress over the whole window.** `ip link` is a sample
  and the pull counter sees only pulls the daemon completed; Docker emits no
  event for a pull that failed. The unplugged cable is the evidence, the
  photograph is its record, and these are corroboration.
- **Exercise the transfer medium's *failure* modes.** Clean copies of all three
  drill artifacts onto FAT32 removable media are now measured and verified on
  the medium (§3.5.2), which the earlier record listed as unproven. What is
  still untested is the bad case: a torn or interrupted copy, a failing stick, a
  write that completes short. Those are caught by `SHA256SUMS` — but no recovery
  step is written for one, beyond copying again.

---

## 9. Summary

| | |
|---|---|
| Release artifact | `1890eba`: **built and independently verified**, anchor `2edf2cd2…f6b8` **ENFORCED**, exit 0. (Zero pulls, but that is a *verification* run, which never calls Docker — no install was measured here; see §3.7.) |
| Fault-injection artifact | **built, verified, refusal confirmed at runtime**, failure mode measured against a real Postgres 17: `psql` exits 3 and the schema change survives (§3.5) |
| Drill set (A, B, fault injection) | **prepared and verified** (§3.5.1) |
| Clean-engine release gate | **run for this exact commit**, release run 36137483144, distinct engine, 0/0/0 store, data smoke PASS (§3.2.1) — a connected CI runner, so **not** an air-gap proof |
| Evidence tooling | **implemented and tested**: 15 new cases for the capture script, 9 new and 1 extended in the bundle-tamper suite, all passing |
| Procedure | **substantially fixed but not yet operator-clean**: the `.env`, `docker` group, disconnection-order, evidence-path, exit-code and destructive-drill gaps are closed; an independent read found remaining defects in §6 (see `DEVOPS_REVIEW.md`) that must be fixed before anyone follows it |
| Physical proof | **OPEN** — the media is prepared, but the second physical host and offline Docker install media are missing. The target run must also record its independent digest channel (§5) |

Until that run exists, the strongest claim this project makes remains the one in
[RELEASE-PROOF §2](RELEASE-PROOF.md#what-this-is-not): a separate Docker engine
with an empty image store and no reachable egress, plus the hosted clean-engine
gate. Not separate physical hardware.
