# Release: from a commit to an installed offline bundle

This is the CD half of the project. It is deliberately split at one place:
**the workflow automates everything up to a verified, reproducible bundle and
a recorded promotion decision; a human carries the bundle to the offline host
and installs it.** An air-gapped target is what this split is designed for, so
an automated network deploy across it does not exist and is not simulated here.

One qualification, because the sentence above describes an intent rather than a
machine: **no such target exists in this project yet.** Every install recorded
so far ran on a separate Docker engine on the development machine or on a
hosted CI runner. What that leaves open, and what would close it, is
[EVIDENCE-physical-airgap.md](EVIDENCE-physical-airgap.md).

```
commit on main, CI green
        |
        v
.github/workflows/release.yml   (workflow_dispatch, automated)
  - binds to the exact commit, refuses anything not merged + CI-green
  - live-reads branch protection on main (best-effort; verified:false if
    the token cannot confirm it -- never a guessed fallback)
  - re-verifies the published image digests against the registry
  - builds the offline bundle (scripts/package-offline.sh)
  - verifies it (scripts/verify-bundle-images.sh)
  - starts a second empty Docker engine and installs the bundle there,
    --pull never, then runs the installer smoke test
  - writes promotion-record.json into the bundle, reseals SHA256SUMS
  - uploads the promotion record + manifests (NOT the multi-GB bundle)
        |
        v
Operator, on a connected staging machine   (manual, documented below)
  - re-runs scripts/package-offline.sh to produce the actual bundle bytes
  - physically carries dist/aow-<sha>/ to the offline host  [never yet performed]
        |
        v
Offline host   (manual, documented below)
  - verifies, installs, proves
```

## Why the workflow does not carry the bundle itself

`release.yml` runs `scripts/package-offline.sh` for real, in the GitHub-hosted
runner, and runs `scripts/verify-bundle-images.sh` against the result. That is
not a rehearsal -- it proves the exact recipe an operator will run also
succeeds for this exact commit, on the day it is promoted, before anyone
copies anything. But the workflow does not upload `images.tar` or the GGUF
weights as a workflow artifact:

- `images.tar` is roughly 1.2 GB and the model is another 1.28 GB, for a
  2.4 GB bundle. GitHub
  Actions artifact storage is billed per GB-day; carrying multi-GB binaries
  through it release after release is a cost with no reader, since the bundle
  is going to a machine Actions cannot reach anyway.
- The bundle's own integrity files (`SHA256SUMS`, `images.bundle.lock`) are
  **self-attesting** -- whoever could swap `images.tar` could swap the
  checksums that describe it just as easily. What actually has to leave a
  workflow artifact is the one thing that is *not* self-attesting: the
  independently re-resolved registry digests for `services` and `ui`, which
  is exactly what `promotion-record.json` carries.

So the workflow proves the recipe works and hands over a small, checkable
promotion record. The multi-GB bytes are produced again, on purpose, by the
operator, from the same inputs -- see below.

## What `release.yml` does, gate by gate

Triggered by `workflow_dispatch` with a required `sha` input (see the
workflow file for the full reasoning). In order:

Before it touches an image, the job starts pinned Docker 29.8.1 with the
containerd image store and checks the daemon reports that store. F10's proof
found that the classic store rewrites the index digests during `docker save`,
so its output cannot satisfy the bundle's digest lock.

1. Validates `sha` is a 40-character hex commit SHA.
2. Checks it out and asserts `git rev-parse HEAD` matches it.
3. Queries the Actions API for a **completed, successful** `ci.yml` run with
   `event=push` and `branch=main` for that exact SHA. `ci.yml` only publishes
   images and the `images.lock` artifact under that same gate, so this single
   check proves both "this commit is on main" and "CI passed for it."
   **A release can only be cut from a commit already merged to main** -- a
   feature-branch SHA, or a main commit whose CI run has not finished, is
   rejected here with an explicit error.
4. **Live-reads branch protection on `main`**: `gh api
   repos/<repo>/branches/main/protection`. This is best-effort, not a gate --
   it never fails the job. `GITHUB_TOKEN` has no permission scope that grants
   this read (`administration` is not one of the scopes GITHUB_TOKEN can be
   assigned at all; see the permissions comment in the workflow), so in the
   default configuration this step records `{"verified": false, "reason":
   ..., "http_status": ...}` and the promotion record says exactly that,
   rather than asserting a required-checks list it did not confirm. If a
   future run's token can read it (a repo-admin PAT swapped in for
   `secrets.GITHUB_TOKEN`, for instance), it instead records `{"verified":
   true, "required_status_checks": [...], "enforce_admins": ...}` straight
   from the API response. A separate authenticated operator read on
   2026-09-25 confirmed that `main` currently requires `lint`, `unit`,
   `guard` and `build-and-scan`, with admin enforcement enabled. That read
   does not change older promotion records: their workflow-token reads
   returned HTTP 403 and correctly say `verified: false`.
5. Downloads that run's `aow-images-<sha>` artifact and checks its recorded
   commit matches.
6. **Independently re-resolves** the `services` and `ui` image digests from
   the registry (`docker buildx imagetools inspect` against the `sha-<sha>`
   tag) and asserts they still match what the artifact says. This is the one
   gate with an anchor outside the release's own paperwork -- see
   `scripts/cd-promotion-record.py`'s `PROVENANCE_BY_ALIAS` for why the other
   images in the bundle (`postgres`, `rabbitmq`, `llm`, `edge`, `prometheus`,
   `grafana`, `stage`, `demos`) do not carry the same strength of proof.
7. Stages the model (`docker compose -f compose.tools.yml run --rm stage`,
   the same command `make stage-fetch` runs) and lets `sha256sum -c
   models.lock` fail the job if it does not match.

   **This gate needs the repository variable `MODEL_BASE_URL` set**, to a
   mirror that actually carries the pinned Q4_K_M weights. The workflow's own
   fallback points at `huggingface.co/Qwen/Qwen3-1.7B-GGUF`, which publishes
   only Q8_0 -- `compose.tools.yml` records that measurement and defaults to
   the `ggml-org` mirror for exactly this reason. With the variable unset this
   step therefore fails on an HTTP 404 rather than on a checksum mismatch,
   which is a confusing way to discover a configuration gap. It is safe either
   way: `stage_model.py` verifies the hash after download, so a wrong mirror
   can never stage the wrong weights. `ci.yml` passes the same variable with no
   fallback at all.
8. Builds the bundle: `bash scripts/package-offline.sh release/images.lock`,
   unmodified. After `docker save`, the package script fills any omitted
   services/UI config or layer blobs directly from GHCR by digest and hashes
   every fetched blob. This works around a containerd-store `docker save`
   defect seen on a clean runner; the manifest digests remain unchanged.
   Verifies the completed archive a second, explicit time: `bash
   scripts/verify-bundle-images.sh dist/aow-<sha>`.
9. Starts a second Docker 29.8.1 daemon with the containerd image store,
   checks that its engine ID differs from the packaging daemon's and that it
   has zero images and volumes, then **installs the bundle with no pulls** in
   a disposable Compose project. It sets `AOW_REQUIRE_CLEAN_IMAGE_STORE=1`
   and calls `bash dist/aow-<sha>/scripts/install-offline.sh` unmodified.
   See "What the install exercise proves" below.
10. Writes `dist/aow-<sha>/promotion-record.json` and reseals
    `dist/aow-<sha>/SHA256SUMS` to cover it.
11. Uploads `promotion-record.json`, `images.lock`, `images.bundle.lock`,
    `SHA256SUMS` and `release-version.txt` as workflow artifact
    `aow-promotion-<sha>` (90-day retention). Does **not** upload `images.tar`
    or the model weights.

The job has `timeout-minutes: 40` and a `concurrency` group keyed on `sha`
(`cancel-in-progress: false`), so a second dispatch for the same commit waits
rather than racing the first one into `dist/aow-<sha>/`, which
`package-offline.sh` refuses to overwrite.

`promotion-record.json` schema (see the script's docstring for field-level
detail): `release_commit`, `release_tag`, `generated_at`, `ci_run` (id + URL),
`release_workflow_run` (URL), `branch_protection` (`branch`, either `verified:
true` with `required_status_checks` and `enforce_admins` straight from the
live API read, or `verified: false` with `reason` and `http_status` -- see
gate 4 above -- plus `satisfied_by_ci_run`), `gates_passed` (the list above,
by name), `images` (per alias: `ref` and `provenance`, one of
`registry_reresolved_by_workflow`, `digest_pinned_pull_verified_by_docker`,
`self_attested_build` -- **do not treat every image in the bundle as equally
proven**), `model` (file + sha256), `bundle_checksums` (the sealed
`SHA256SUMS` contents at promotion-record write time, i.e. everything except
the record itself, which is added by the reseal step that follows).

## What the install exercise proves, and what it does not

Step 9 now directs the installer to a second daemon via `DOCKER_HOST`, with a
different engine ID and no images or volumes before `docker load`. Its
containerd store cannot use layers left in the packaging daemon. It uses a
throwaway Compose project (`COMPOSE_PROJECT_NAME=aow-release-<run id>`),
generated passwords and cleanup with `down --volumes --remove-orphans`.
`install-offline.sh` itself runs `--no-build --pull never` and its own
`scripts/release-smoke.py`; the workflow does not duplicate either command.

**What has actually passed this gate, and for which commit.** Release run
[`36222925158`](https://github.com/AvihaiShai/act-on-weather/actions/runs/36222925158)
validated the second-daemon step for commit `bfbb48a`, whose `sha` input is
anchored by its own `aow-promotion-<sha>` artifact: the runner logged
different engine IDs, an empty image and volume store before loading, complete
archive verification for every image alias in the bundle, and a passing data
smoke. Earlier hosted release runs installed on the packaging daemon, so their
passes do not supply this evidence.

That evidence covers `bfbb48a` and nothing else. **A commit later than the one
named above carries no clean-engine proof until `release.yml` is dispatched for
it**, because this gate runs only on that manual dispatch -- not on push, and
not on merge. Read the promotion record of the release you are installing rather
than assuming the head of `main` has been promoted.

That smoke test asserts more than liveness, but not a deep functional check:
it confirms `/health` is ok, `/coverage` reports the `weather` entity has
`rows > 0` across all 5 cities, `/weather/rome` and `/scores?city=rome` both
return a body -- real data reached Postgres through the queue and the rule
engine produced scores from it -- and it separately checks liveness only
(`/health`, no data assertion) for the agent, the LLM server, the UI and the
edge proxy. It does not exercise agent tool-calling, does not check an LLM
recommendation was written, and does not render the UI. Read
`scripts/release-smoke.py` directly for the exact checks.

**Three claims live near each other here, and merging them would be the
single most misleading thing this document could do.** They are:

| claim | what establishes it | what it is not |
|---|---|---|
| **The archive is self-contained** | `scripts/verify-bundle-images.sh`, by manifest digest, from the bundle's own bytes | a statement about any host; it is a property of a file, checkable anywhere |
| **It installs on an empty image store with no pulls** | the gate above -- a second daemon, distinct engine ID, zero images and volumes, `--pull never` | **not** an air-gapped install. It runs on a **connected GitHub-hosted runner**, in a second privileged daemon on the same VM. The engine is clean; the machine is on the internet |
| **A physical air gap** | nothing in this repository | see [EVIDENCE-physical-airgap.md](EVIDENCE-physical-airgap.md). A VM, a second daemon, a CI runner and a firewall rule are each recorded there as *not* closing it |

So the phrase "air-gapped install proof" appears nowhere in this document for
the middle row, and should not be added to it. F10's manual rig in
`docs/RELEASE-PROOF.md` §2 went further than the CI gate -- a separate Docker
engine with nftables blocking DNS, raw-IP TCP and HTTPS, probed from a container
on a routable network -- and that rig was still not separate physical hardware.
The clean-engine gate adds install evidence for the exact tar built by its own
run; it replaces neither the manual no-egress test nor a drill on physically
disconnected hardware.

"Air-gapped" is reserved in this project for two things: the runtime property
Docker enforces on every host (`backend` is `internal: true`, so no application
service has a route out), and F10 once it is genuinely closed.

## Operator procedure: staging machine

Once a release is promoted (the workflow above succeeded), reproduce the
actual bundle on a machine that can reach the internet and has a
Linux/amd64 Docker engine with the containerd image store enabled. Download
`images.lock` from the promotion artifact into `release/`, and check out the
exact promoted commit with no tracked changes. The model must also be staged
before packaging:

```bash
git checkout <sha>                       # the exact commit the promotion record names
git status --porcelain                   # inspect and clear tracked changes
docker info --format '{{json .DriverStatus}}'  # confirm io.containerd.snapshotter.v1
docker compose -f compose.tools.yml --env-file .env.example run --rm stage
bash scripts/package-offline.sh release/images.lock
```

`package-offline.sh` also refuses to overwrite an existing `dist/aow-<sha>/`.
It packages the committed tree, so untracked source changes are not shipped.
This reproduces `dist/aow-<sha>/` with the same pinned images and model,
verified against the same `images.lock` the release workflow checked. The rebuilt folder does **not** contain `promotion-record.json`: only
`release.yml` writes one, into its own copy of the bundle, which is deleted
with the runner. Download it from the `aow-promotion-<sha>` workflow artifact
and drop it into `dist/aow-<sha>/` if you want a second confirmation beyond the
script's own checks -- `scripts/verify-bundle.sh` tolerates it there as the one
file the documented procedure legitimately adds.

Compare its `images` block against what you built. Compare the **locks**, not
the tar: `images.bundle.lock` and `models.lock` are content-addressed and must
match exactly, but `SHA256SUMS` will not, because `docker save` output is not
bit-reproducible across engines -- something this project measured rather than
assumed (see `docs/RELEASE-PROOF.md`, where packaging the same release on a
classic-store engine re-serialised all eight image digests).
The archive completion step uses the existing `docker login ghcr.io`
credentials (including Docker credential helpers), or anonymous access when
the package is public. `GHCR_USER` and `GHCR_TOKEN` can override the Docker
login; the release workflow supplies its job token explicitly.

## Operator procedure: transfer

Copy the whole `dist/aow-<sha>/` folder to the offline host by whatever
physical or air-gapped-network means your environment allows (USB drive,
one-way transfer station, sneakernet). Nothing in this repository automates
that hop, and nothing should: automating a network path into an air-gapped
host would defeat the point of it being air-gapped.

**Four parts of this are human acts that no script performs.** They are listed
because each one is easy to assume has been handled:

- **The transfer itself.** No tool in this repository moves the bundle. A
  person carries it.
- **The bundle must arrive extracted**, as a directory, not as an archive of a
  directory. `verify-bundle.sh` and `install-offline.sh` both run from inside
  `dist/aow-<sha>/` and read its files by name.
- **Copy it with `tar` or `rsync`, not a desktop file manager.** The bundle is
  verified byte for byte against `SHA256SUMS`, and a file manager is free to
  normalise line endings, drop modes or skip a file it considers unimportant.
  Any of those turns a good bundle into a failed verification.
- **The out-of-band `SHA256SUMS` digest has to travel by a different route**
  than the bundle. `verify-bundle.sh` prints that digest and, given
  `AOW_SHA256SUMS`, checks it -- but a digest that arrived on the same USB
  stick as the files it describes anchors nothing, and the script says so in
  its transcript rather than letting a skipped check read like a passed one.

## Operator procedure: offline host

From inside the copied `dist/aow-<sha>/` folder:

```bash
bash scripts/verify-bundle.sh .          # the whole gate: SHA256SUMS, no unlisted
                                         # file, models.lock, images.bundle.lock
                                         # anchored to ci-images.lock and the
                                         # committed IMAGES.lock, and images.tar
                                         # checked by manifest digest
# if the packaging run's SHA256SUMS digest reached you out of band, pin it:
AOW_SHA256SUMS=sha256:<digest> bash scripts/verify-bundle.sh .
cp .env.example .env                     # FIRST install only -- see below.
                                         # On an upgrade, copy the PREVIOUS
                                         # release folder's .env across instead.
bash scripts/install-offline.sh          # docker load, migrate, start, smoke-test
bash scripts/prove-offline.sh            # run the packaged offline proof
```

`.env` is written by hand, on the offline host, in the release folder. Nothing
generates it there: the bundle ships `.env.example` with placeholders, and a
first install means opening it and replacing every `change-me` with a different
password.

**On an upgrade, reuse the previous release's `.env` verbatim, and leave the
previous release running while you do it.** Both halves are requirements:

- A new release installs over the **same volumes** -- that is what makes it an
  upgrade rather than a parallel install -- so the database still holds the
  roles the old passwords created. Regenerated passwords therefore produce a
  stack that cannot authenticate against its own data, after the installer has
  already dumped the old database and loaded gigabytes of images. The installer
  refuses that combination up front instead.
- The pre-upgrade `pg_dump` is what makes the migration reversible. It is taken
  whether the previous release is running or stopped, so stopping it first no
  longer costs you the rollback -- but leaving it running is still the
  documented order. See `scripts/restore-offline.sh` for the rollback itself.

The [README](../README.md#installing-a-packaged-release) summarizes these
guards; the command sequence above is the offline-host procedure.

The installer also checks the host before it commits to anything: the
architecture and toolchain it actually needs (Linux with GNU coreutils and
`bash` 4.4 or newer, not merely amd64), the prerequisites, and that `.env`
renders the Compose files -- all **before** the multi-gigabyte `docker load`,
rather than after it.

## What this boundary does and does not claim

- Automated and proven by CI/CD, **on a connected hosted runner**: the commit
  is tested, the images it publishes are independently re-verified against the
  registry at release time (not just trusted from a file), the bundle-building
  recipe is exercised end to end for that exact commit, the bundle installs on
  an empty image store with no pulls, and the promotion decision is recorded
  with enough detail to reconstruct it later. All of that is per-commit: it
  says nothing about a commit no one has dispatched the workflow for.
- Manual and documented, not automated: reproducing the bundle bytes on a
  staging machine, the install, and the proof. This is the honest shape of an
  air-gapped release -- nothing in this repository claims to deploy across that
  gap, because nothing safely can.
- Documented but **never performed**: the physical transfer itself, and the
  install on separate physical hardware. It is listed apart from the row above
  deliberately -- the other manual steps have been run by hand many times, and
  this one has not been run at all. See
  [EVIDENCE-physical-airgap.md](EVIDENCE-physical-airgap.md).
