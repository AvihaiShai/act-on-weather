# Release: from a commit to an installed offline bundle

This is the CD half of the project. It is deliberately split at one place:
**the workflow automates everything up to a verified, reproducible bundle and
a recorded promotion decision; a human carries the bundle to the offline host
and installs it.** The target is air-gapped, so an automated network deploy
to it does not exist and is not simulated here.

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
  - installs the bundle into a throwaway Compose project, --pull never,
    and lets its own last step run the smoke test (scripts/install-offline.sh)
  - writes promotion-record.json into the bundle, reseals SHA256SUMS
  - uploads the promotion record + manifests (NOT the multi-GB bundle)
        |
        v
Operator, on a connected staging machine   (manual, documented below)
  - re-runs scripts/package-offline.sh to produce the actual bundle bytes
  - physically carries dist/aow-<sha>/ to the offline host
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

- `images.tar` is roughly 1.9 GB and the model is another 1.28 GB. GitHub
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
   from the API response.
5. Downloads that run's `aow-images-<sha>` artifact and checks its recorded
   commit matches.
6. **Independently re-resolves** the `services` and `ui` image digests from
   the registry (`docker buildx imagetools inspect` against the `sha-<sha>`
   tag) and asserts they still match what the artifact says. This is the one
   gate with an anchor outside the release's own paperwork -- see
   `scripts/cd-promotion-record.py`'s `PROVENANCE_BY_ALIAS` for why the other
   images in the bundle (`postgres`, `rabbitmq`, `llm`, `edge`, `stage`,
   `demos`) do not carry the same strength of proof.
7. Stages the model (`docker compose -f compose.tools.yml run --rm stage`,
   the same command `make stage-fetch` runs) and lets `sha256sum -c
   models.lock` fail the job if it does not match.
8. Builds the bundle: `bash scripts/package-offline.sh release/images.lock`,
   unmodified. Verifies it a second, explicit time: `bash
   scripts/verify-bundle-images.sh dist/aow-<sha>`.
9. **Installs the bundle and proves it serves data**, with no pulls, in a
   disposable Compose project (`bash dist/aow-<sha>/scripts/install-offline.sh`,
   unmodified). See "What the install exercise proves" below.
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

Step 9 above installs the bundle this same job just built into a throwaway
Compose project (`COMPOSE_PROJECT_NAME=aow-release-<run id>`, fresh volumes,
generated passwords, torn down afterward with `down --volumes
--remove-orphans` whether it passed or failed) and brings the stack up with
`--no-build --pull never` -- that flag is `install-offline.sh`'s own, not a
second mechanism added here, so a missing image is a hard failure, not a
silent pull. Its own last line then runs the smoke test
(`scripts/release-smoke.py`) against the running stack, so release.yml does
not invoke it separately.

That smoke test asserts more than liveness, but not a deep functional check:
it confirms `/health` is ok, `/coverage` reports the `weather` entity has
`rows > 0` across all 5 cities, `/weather/rome` and `/scores?city=rome` both
return a body -- real data reached Postgres through the queue and the rule
engine produced scores from it -- and it separately checks liveness only
(`/health`, no data assertion) for the agent, the LLM server, the UI and the
edge proxy. It does not exercise agent tool-calling, does not check an LLM
recommendation was written, and does not render the UI. Read
`scripts/release-smoke.py` directly for the exact checks.

**This does not duplicate F10's air-gap rig**, and is not trying to. That rig
is a physically separate `docker-ce` host with `nftables` dropping DNS,
raw-IP and ICMP -- a real network-isolation certification a GitHub-hosted
runner cannot reproduce, because the runner itself has internet access
throughout this job. What step 9 proves instead is narrower and cheaper to
run on every release: *the exact tar this job just built and verified boots
the stack and the stack holds real data, using no image the bundle did not
already carry.* That is worth checking every time even though it is not an
air-gap proof; the air-gap proof is F10's `scripts/prove-offline.sh`, run by
the operator on the offline host as documented below.

## Operator procedure: staging machine

Once a release is promoted (the workflow above succeeded), reproduce the
actual bundle on a machine that can reach the internet and has a
Linux/amd64 Docker engine:

```bash
git checkout <sha>                       # the exact commit the promotion record names
bash scripts/package-offline.sh release/images.lock   # release/images.lock: the file
                                                        # from the aow-promotion-<sha>
                                                        # artifact, or re-download it
                                                        # from the ci.yml run named in
                                                        # promotion-record.json
```

This reproduces `dist/aow-<sha>/` with the same pinned images and model,
verified against the same `images.lock` the release workflow checked. Compare
`dist/aow-<sha>/promotion-record.json`'s `images` block against what you
built, if you want a second confirmation beyond the script's own checks.

## Operator procedure: transfer

Copy the whole `dist/aow-<sha>/` folder to the offline host by whatever
physical or air-gapped-network means your environment allows (USB drive,
one-way transfer station, sneakernet). Nothing in this repository automates
that hop, and nothing should: automating a network path into an air-gapped
host would defeat the point of it being air-gapped.

## Operator procedure: offline host

From inside the copied `dist/aow-<sha>/` folder:

```bash
sha256sum -c SHA256SUMS                  # bytes arrived intact, including promotion-record.json
bash scripts/verify-bundle-images.sh .   # images.tar matches images.bundle.lock, by manifest digest
cp .env.example .env                     # then set real passwords in .env
bash scripts/install-offline.sh          # docker load, migrate, start, smoke-test
bash scripts/prove-offline.sh            # run the packaged offline proof
```

`install-offline.sh` and `prove-offline.sh` are documented in full in the
[README](../README.md); this page only orders the steps. If `install-offline.sh`
finds a running previous release under the same Compose project, it takes a
`pg_dump` backup before touching anything -- see `scripts/restore-offline.sh`
if a rollback is needed afterward.

## What this boundary does and does not claim

- Automated and proven by CI/CD: the commit is tested, the images it
  publishes are independently re-verified against the registry at release
  time (not just trusted from a file), the bundle-building recipe is
  exercised end to end for that exact commit, and the promotion decision is
  recorded with enough detail to reconstruct it later.
- Manual and documented, not automated: reproducing the bundle bytes on a
  staging machine, the physical transfer, the install, and the proof. This is
  the honest shape of an air-gapped release -- nothing in this repository
  claims to deploy across that gap, because nothing safely can.
