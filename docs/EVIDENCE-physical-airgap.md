# Evidence: the physical air-gap proof (F10)

**Status: OPEN.** The preparation is complete and measured; the proof itself has
not been run, because this project has one physical machine and no removable
media. Section 5 names exactly what is missing. Nothing in this document claims
a physical air gap has been demonstrated.

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

### 3.1 Source commit

| | |
|---|---|
| Commit | `a21dff9d4b38cdfead8be717db4599fc602ba668` |
| Tree | `4c4ea989380ec7411d787acf3cb35713aa68449a` |
| Subject | Merge pull request #48 from AvihaiShai/docs/clean-engine-proof-sep25 |
| Committed | 2026-09-25T02:22:29+03:00 |
| Ancestor of `origin/main` | yes |
| Working tree at package time | clean (`git status --porcelain` empty) |

### 3.2 The CI run that authorises a release at this commit

`ci.yml` run [`36072399083`](https://github.com/AvihaiShai/act-on-weather/actions/runs/36072399083),
conclusion **success**, `head_sha` `a21dff9…`.

| Job | Result |
|---|---|
| `unit`, `lint`, `guard`, `build-and-scan`, `ui-gate`, `publish-images` | success |
| `model-grounding`, `restore-drill` | skipped in this run |

Its `aow-images-a21dff9…` artifact (316 bytes,
`sha256:6f6f36df91a06d66450795705e6bda0347350fd96f3924a2ff791cfa5c4c4f0f`) is
the `images.lock` that `package-offline.sh` consumes:

```
commit   a21dff9d4b38cdfead8be717db4599fc602ba668
services ghcr.io/avihaishai/act-on-weather/services@sha256:6de4cc97229f97a88c34d320277d9f88d25aff58fbfa1f1f84802d672faaf4fe
ui       ghcr.io/avihaishai/act-on-weather/ui@sha256:da9c87563061e661d0a1483fc9361c8f2c15cb640587801d58e05df9a8285d5b
```

**Note what is not covered.** The `release.yml` clean-engine gate has run for
`b21885a`, not for `a21dff9`. See §8.

### 3.3 The release artifact

Built with `bash scripts/package-offline.sh <images.lock>` on the connected
staging machine. Wall clock **16 min 09 s**, exit **0**.

| | |
|---|---|
| Path | `dist/aow-a21dff9d4b38cdfead8be717db4599fc602ba668/` |
| `release-version.txt` | `a21dff9d4b38cdfead8be717db4599fc602ba668` |
| **`sha256(SHA256SUMS)`** — the out-of-band anchor | **`1bb8b7e9f581e80ada23565ed35009d47bd39d9529c331ba52a90a636b9ad205`** |
| `sha256(images.tar)` | `b1e0916fcb431d8d61c02143e43d19aa714c050dc5c17d8f1f6956aba26ee6d7` |
| `images.tar` | 1,221,854,720 bytes, 10 images |
| Model | `Qwen3-1.7B-Q4_K_M.gguf`, 1,282,439,264 bytes, verified against `models.lock` |
| Folder | 214 files, 2,511,260,696 bytes (2.34 GiB) |
| `.env` | absent — `install-offline.sh` will refuse until one exists (§6.1) |

The `sha256(SHA256SUMS)` value above is the **only** check on the offline host
that does not come out of the folder being checked. It has to travel by a
different route than the media to mean anything. See §7.

### 3.4 Independent verification of that artifact

Run from the repository checkout rather than from inside the bundle, with the
anchor supplied:

```
AOW_SHA256SUMS=sha256:1bb8b7e9…d205 \
  bash scripts/airgap-evidence.sh run -- bash scripts/verify-bundle.sh dist/aow-a21dff9…/
```

| Measurement | Value |
|---|---|
| `out-of-band anchor` | **ENFORCED** — `SHA256SUMS` matched the digest supplied separately |
| `images.bundle.lock` | matches the CI manifest for `a21dff9…` and the committed `IMAGES.lock` |
| `images.tar` | matches `images.bundle.lock` (10 images, by verified manifest digest); every image has its config and layers, as `linux/amd64` |
| exit code | 0 |
| elapsed | 15 s |
| completed image pulls | **0** |
| transcript pull/build markers | **0** |

And a cross-check the installer cannot make for itself — the bundle's own
verifier is the code that gates the bundle, so it was compared against the
commit rather than trusted:

| File in bundle | vs `git show a21dff9:` |
|---|---|
| `scripts/verify-bundle.sh` | byte-identical |
| `scripts/verify-bundle-images.sh` | byte-identical |
| `scripts/bundle-image-manifests.sh` | byte-identical |
| `scripts/install-offline.sh` | byte-identical |
| `scripts/prove-offline.sh` | byte-identical |
| `IMAGES.lock`, `models.lock` | byte-identical |

### 3.5 The staging host, recorded so the target's can be compared against it

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

### 3.6 The install was deliberately not run here

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
| `scripts/prove-offline.sh` | Added `--no-build`. `compose.tools.yml` still carries a `build:` section, so a missing `aow-bundle/demos:<sha>` tag made Compose *build* the image, and `demos/Dockerfile`'s `apk add` then needs the egress the proof exists to show is absent — turning a tag problem into a network error on the one host where that reads as a failed proof |
| `tests/unit/test_airgap_evidence.py` (new, 11 cases) | The capture script is a measuring instrument, so the tested failure mode is a confident zero: a missing `ip` must report `UNKNOWN`, not "no default route" |
| `tests/unit/test_bundle_tamper.py` (+2 cases) | Asserts the anchor transcript lines, and that the offline proof cannot fall back to building |

The pull counter was validated against a real pull on the staging engine:
`completed_image_pulls: 1` for `docker pull hello-world`, including the harder
"image is up to date" case, which still emits the event.

---

## 5. What is missing, exactly

Measured on this machine on 2026-09-25:

- **Physical hosts available: one.** `Avihai_Shai`. The only other Linux
  environments are WSL2 distributions (`docker-desktop`, `Ubuntu-24.04`) on that
  same machine, which are not separate hardware.
- **Removable media attached: none.** `Win32_LogicalDisk` reports four fixed
  disks (`C:`, `D:`, `E:`, `F:`, all `DriveType 3`) and no removable or optical
  volume.
- **Reachable LAN hosts: none usable.** The IPv4 neighbour table holds the
  gateway, two ASUS network devices and the WSL virtual adapter. No second
  general-purpose machine.

The Hyper-V feature state could not be re-read without elevation. It does not
matter: a VM on this machine would not satisfy F10 either.

**To close F10, four things are needed and none of them can be produced from
this repository:**

1. **A second physical x86_64 machine** that has never held these images:
   Ubuntu 24.04, ≥8 GiB RAM available to Docker, ≥9 GiB free disk (≈6 GiB for
   the stack, 2.4 GiB for the bundle). It must be **disposable** if the
   destructive drills in §6.5 are run, because those wipe the database by
   design and every release folder installs over the same named volumes.
2. **Removable media**, ≥4 GiB for one bundle, ≥8 GiB for the three-bundle
   destructive set.
3. **Docker CE installation media for an offline Ubuntu host** — the `.deb` set
   including `docker-compose-plugin`, since `apt` cannot reach the archive on a
   disconnected machine and Compose **v2** is required. This is the step most
   likely to be discovered too late, standing at an unplugged machine.
4. **An out-of-band channel for the `SHA256SUMS` digest** (§3.3). If the same
   person carries the digest and the media, the anchor checks that the media was
   not corrupted, not that it was not substituted. Say which of the two the run
   claims.

---

## 6. Ready-to-run procedure

Ordered as an operator meets it. Steps 6.1–6.2 happen **connected**; the link is
cut in 6.3 and stays cut.

### 6.1 On the connected staging machine

1. Check out the release commit and confirm the tree is clean.
2. Download the `aow-images-<sha>` artifact from that commit's successful
   `ci.yml` run.
3. `bash scripts/package-offline.sh <path>/images.lock`
4. `bash scripts/airgap-evidence.sh bundle dist/aow-<sha> | tee evidence/bundle.txt`
   — record `sha256(SHA256SUMS)` and `sha256(images.tar)`.
5. `bash scripts/airgap-evidence.sh host staging | tee evidence/host-staging.txt`
   — this is where the staging **engine ID** is captured. It cannot be captured
   later, and the drill needs both IDs to show they differ.
6. **Generate `.env` here, not on the target.** `install-offline.sh` refuses
   without one, and the offline host has no way to make one: the bundle carries
   the password-generator image retagged as `aow-bundle/stage:<sha>`, while
   `bootstrap.sh` looks it up by its digest-pinned `python:3.12-slim@sha256:…`
   reference and will not find it. Run `bash scripts/bootstrap.sh` connected, or
   copy `.env.example` and replace every `change-me` with a different value.
   Carry `.env` **separately from the bundle folder**; `verify-bundle.sh` exempts
   any file named `.env` from both `SHA256SUMS` and the unlisted-file check, so
   dropping it in afterwards is by design.
7. Carry the `SHA256SUMS` digest out of band (§5.4).
8. If the destructive drills are planned, prepare their extra artifacts now
   (§6.5).

### 6.2 Transfer

Copy with `tar` or `rsync`, **not a file manager**, on both hops — staging →
medium and medium → target. A file manager adds `desktop.ini`, `Thumbs.db` or
`.DS_Store`, and `verify-bundle.sh` refuses the folder on any unlisted file. The
measured properties that make the copy possible are in
[RELEASE-PROOF §2](RELEASE-PROOF.md#what-the-bundle-needs-from-a-transfer-medium-measured):
largest file 1.28 GB (under FAT32's 4 GiB cap), every tracked file mode `100644`,
longest path 41 characters.

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
3. `bash scripts/airgap-evidence.sh host target | tee evidence/host-target.txt`
   — this is the census **before the load**. It must show a different engine ID
   from §6.1.5, and `images: 0`, `volumes: 0`, `containers: 0`.
4. Photograph the disconnected link, and keep `ip -br link` from step 3. Both
   are point-in-time samples; the photograph is what makes them a claim about
   the window.

### 6.4 Install and prove

From inside the copied bundle folder, with `.env` in place:

```bash
bash scripts/airgap-evidence.sh bundle . | tee evidence/bundle-on-target.txt
# sha256(images.tar) must equal the value from §6.1.4

export AOW_SHA256SUMS=sha256:<digest carried out of band>
bash scripts/airgap-evidence.sh run -- bash scripts/verify-bundle.sh . \
  | tee evidence/verify.txt
# must print "out-of-band anchor: ENFORCED"

AOW_REQUIRE_CLEAN_IMAGE_STORE=1 \
  bash scripts/airgap-evidence.sh run -- bash scripts/install-offline.sh \
  | tee evidence/install.txt

bash scripts/airgap-evidence.sh run -- bash scripts/prove-offline.sh \
  | tee evidence/prove.txt
```

`prove-offline.sh` runs five sections: Docker forbids a route out of the
application network; the services cannot reach the internet, demonstrated; no
cloud LLM anywhere in the repo; the two reviewer questions answered from stored
data ("What is the weather tomorrow in Rome?" and the London activities
question); and a question outside the coverage window, which must answer "no
data" rather than guess.

**Do not run the `backup-restore` proof in this drill without preparation.**
`demos/06_backup_restore.sh` calls `docker run` on `${AOW_SERVICES_IMAGE:-aow/services:dev}`
with no `--pull never` and no presence check, and nothing in the bundle overlays
sets that variable. On a disconnected host it attempts a registry pull. It is
excluded from the default `offline` proof; set `AOW_SERVICES_IMAGE` to the
loaded `aow-bundle/services:<sha>` tag first, or leave it out and say so.

### 6.5 Destructive drills — disposable target only

These wipe the database by design, and `compose.yml` fixes the project name so
every release folder installs over the same named volumes. Only run them on a
host nobody expects to keep.

Prepare on the connected staging machine:

- **Two distinct CI-proven releases**, A and B, each packaged from its own
  successful `ci.yml` run. Record both commits **and both migration lists** —
  the drilled A/B measurements in RELEASE-PROOF §3 predate
  `004_itinerary_delete.sql` and `005_user_data_wipe.sql` and do not cover the
  current tree.
- **A deliberately failing migration.** There is no reproducible recipe for
  this today, and it is the largest gap in §6.5: CI will never publish images
  for a commit containing a migration written to fail, and `package-offline.sh`
  refuses any tree that is not exactly the commit in `images.lock`. The bundle
  must therefore be hand-assembled and its `SHA256SUMS` re-sealed, which is
  itself outside the verified path. Writing that recipe is open work.

Two things the procedure must not get wrong:

- **Carry release A's `.env` into release B's folder.** Do not generate a new
  one. `POSTGRES_PASSWORD` and `RABBITMQ_PASSWORD` are baked into the volumes at
  creation, and `install-offline.sh` aborts on a mismatch *after* `docker load`
  has already replaced the images.
- **Drop `AOW_REQUIRE_CLEAN_IMAGE_STORE=1` for the rollback.** Re-installing A
  on an engine that already holds A's tags is a hard failure with that variable
  set. It belongs on the first install only.

Expect the rollback to take **≈508 s**: the release smoke check waits out its
full 480 s deadline before reporting the still-empty forecast. A run that looks
hung at eight minutes is not hung.

---

## 7. Evidence capture sheet

Every row has a producing command. Fill in, attach the transcripts, and record
failures rather than reruns.

| # | Evidence | Command | Value |
|---|---|---|---|
| 1 | Source commit and tree | `git rev-parse HEAD; git rev-parse HEAD^{tree}` | |
| 2 | CI run id and conclusion for that commit | `gh run list --branch main` | |
| 3 | Staging engine ID | `airgap-evidence.sh host staging` | |
| 4 | Target engine ID — **must differ from 3** | `airgap-evidence.sh host target` | |
| 5 | Target store before load — **must be 0 / 0 / 0** | same capture as 4 | |
| 6 | Link state on target + photograph | `ip -br link` in capture 4, plus photo | |
| 7 | `sha256(images.tar)` on staging | `airgap-evidence.sh bundle dist/aow-<sha>` | |
| 8 | `sha256(images.tar)` on target — **must equal 7** | `airgap-evidence.sh bundle .` | |
| 9 | Out-of-band `SHA256SUMS` digest, and how it travelled | recorded at §6.1.7 | |
| 10 | Anchor enforced | `verify.txt` must contain `out-of-band anchor: ENFORCED` | |
| 11 | Verify: exit code, elapsed | `verify.txt` | |
| 12 | Install: engine line and `store before load` census | `install.txt` | |
| 13 | Install: clean-store census line | `install.txt` | |
| 14 | Install: exit code, elapsed, **completed pulls**, **pull/build markers** | `install.txt` | |
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
- **completed image pulls is 0 and pull/build markers is 0** in rows 14 and 15;
- the coverage question in row 16 answers "no data", not a guess.

A non-zero marker count with zero completed pulls is the signature of a pull
that was attempted and failed — which on a disconnected host means something in
the chain still wants the network, and is a **failure**, not a pass with noise.

---

## 8. What this evidence will and will not establish

**Will**: that the bundle for the tested commit is self-sufficient on hardware
that has never held these images, with no network present.

**Will not**:

- **Cover any commit but the one tested.** This is not a formality. The
  `release.yml` clean-engine gate has run for `b21885a`; the artifact prepared
  here is `a21dff9`, which is a *later merge commit* and has no release-workflow
  proof of its own. Likewise, the fixes in §4 live on
  `review/f10-physical-proof` and are **not inside the `a21dff9` bundle** — its
  `prove-offline.sh` is byte-identical to the committed `a21dff9` version, which
  lacks `--no-build`. **Whichever commit finally merges this work needs its own
  `images.lock`, its own bundle and its own proof run.** An evidence record
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
- **Exercise the transfer medium's failure modes.** A torn or interrupted copy
  is caught by `SHA256SUMS` — but no recovery step is written for one.

---

## 9. Summary

| | |
|---|---|
| Release artifact for `a21dff9` | **built and independently verified**, anchor enforced, 0 pulls |
| Evidence tooling | **implemented and tested**, 13 new/changed test cases passing |
| Procedure | **complete and ready to run**, with the `.env`, offline-Docker, `docker` group, disconnection-order and destructive-drill gaps closed |
| Physical proof | **OPEN** — blocked on a second physical host, removable media, offline Docker install media and an out-of-band channel (§5) |

Until that run exists, the strongest claim this project makes remains the one in
[RELEASE-PROOF §2](RELEASE-PROOF.md#what-this-is-not): a separate Docker engine
with an empty image store and no reachable egress, plus the hosted clean-engine
gate. Not separate physical hardware.
