# CI/CD evidence matrix — review item #4 (S1, B1)

**As of 2026-09-24.** Every row names a gate and a run, or says plainly that it is manual.
Written for a reviewer who wants to check the claims rather than read about them.

Review item #4 asked whether CI/CD is adequate against **S1** ("Git repo with all code,
configuration files, CI/CD definitions, README") and **B1** ("full tests for all
components"). Those are separated below from the production CI/CD findings, which go
beyond what the assignment asks for. The distinction matters: S1 is met comfortably, B1
is partially met, and most of what was actually wrong was in neither.

Repository: `AvihaiShai/act-on-weather`. Workflows: `.github/workflows/ci.yml`,
`.github/workflows/release.yml`.

---

## 0. Item #4's own statements, re-verified

Item #4 was written at a point in time and several of its statements no longer hold.
Listed first, because a matrix that silently corrects its own source is not evidence.

| Statement in item #4 | Verdict | What disproves it |
|---|---|---|
| "Latest working tree **fails** its formatting gate on `services/ui/theme.py`" | **Stale** | `ruff check` and `ruff format --check` both clean on `main`; the `lint` job has been green on every run since. The failure existed only in an uncommitted working tree. |
| "action tags such as `checkout@v4` are not pinned to immutable SHAs" (F10) | **Stale** | Every `uses:` in both workflows is a 40-hex SHA, and the `guard` job **fails the build** on a bare tag. Additionally `sha_pinning_required` is now enforced at the GitHub platform level. |
| "Integration does not exercise … outage/recovery" | **Stale** | `scripts/ci-integration.sh` runs five traced outage drills plus a full restart and a `count(*) = 6` exactly-once assertion, inside `build-and-scan`, on every PR. |
| "Integration does not exercise model, UI, offline bundle or backup" | **Was true, now closed** | All four now have gates. See §3. |
| "On-prem delivery is a manual package/install procedure, not an automated deployment pipeline" | **True, and deliberately kept so** | See §4. The target is air-gapped; the final hop is a physical transfer. |

---

## 1. S1 — met

| Claim | Status | Evidence |
|---|---|---|
| Git repo with all code and configuration | **PASS** | Public repo; 8 compose files, `.env.example`, `IMAGES.lock`, `models.lock`, `ruff.toml`, `pytest.ini`, `edge/nginx.conf`, `db/migrations/` all tracked |
| CI/CD definitions present and running | **PASS** | `ci.yml` (8 jobs), `release.yml`; the current `main` push run is green, including publish |
| README present and substantive | **PASS** | `README.md`, plus `docs/ARCHITECTURE.md`, `TECHNICAL_DECISIONS.md`, `docs/RELEASE.md` |
| README counts are not stale | **PASS** | `scripts/snapshot_manifest.py --check` runs in `guard` on every PR |
| No secrets committed | **PASS** | `guard` rejects a tracked `.env` and any non-placeholder password in `.env.example`; gitleaks runs on every PR; secret scanning and push protection enabled at the repo level |

S1 is a low bar and the repository clears it. Nothing here is open.

---

## 2. B1 — partial, and honest about which parts

"Full tests for all components." 1170 unit tests run under `--network none` on every PR.
Per component:

| Component | Automated coverage today | Level |
|---|---|---|
| ingestor | unit + real broker/DB outage drills 4–5 driving its own outbox | **integration** |
| consumer | real integration (smoke, reconnect, 5 drills); direct `handle()` tests route every declared key and distinguish stored, duplicate, rejected and retry outcomes | **integration + unit** |
| enricher | outbox recovery unit test; real container starts in `build-and-scan` and its backlog metric matches a separate reader query (480 pending rows in run `36049766709`) | **partial**: model wording is exercised in the RC gate, not this per-PR probe |
| agent | 193 unit tests with a fake LLM, **plus** 8 adversarial cases against the real model | **integration (RC)** |
| api | unit + real integration; every drill accepts through it | **integration** |
| ui | unit render tests **plus** a real headless-browser gate | **integration** |
| llm (llama.cpp) | real llama.cpp + Qwen3-1.7B in the RC grounding gate | **integration (RC)** |
| migrate / db | migrations applied for real; `event_local_days.py` against real Postgres and tzdata | **integration** |
| edge / nginx | port-binding unit assertions; started for real in the browser gate | **partial** |
| rabbitmq | real broker; publisher-confirm and spool-while-down asserted | **integration** |
| outbox / reconcile | unit + injected ACKed-but-missing record, replay, and a stored-ID control | **integration** |
| backup / restore | full-stack drill destroying all volumes, asserting accepted IDs survive | **integration (RC)** |
| packaging / installer | bundle built, verified, installed and smoked in `release.yml` | **integration (release)** |

The two previously missing direct checks were added in PR #37. The enricher probe
proves startup, reader access and backlog reporting; it does not claim a model
reply, since the per-PR integration stack does not start llama.cpp.

---

## 3. Gates, and the run that proves each

All timings from real GitHub-hosted runners, not estimates.

| Gate | What it asserts | When | Proof |
|---|---|---|---|
| `lint` | ruff check + format | every PR | ~10s, green on every run |
| `unit` | 1170 tests, `--network none` | every PR | 1m11s on PR #37 |
| `guard` | no hosted-LLM SDK; no committed secret; Gitleaks canary **and exact committed-tree archive scan**; every compose image, Dockerfile base and workflow action pinned by digest/SHA; `IMAGES.lock` reconciles **in both directions**; all 9 overlay combinations render; README counts match the snapshot | every PR and push | main guard run `36056250209` scanned 2.08 MB of committed content |
| `build-and-scan` | Trivy on both images and the filesystem; then real Postgres + RabbitMQ and the enricher container, 5 traced outage drills, reconciliation audit/replay, full restart, **6 traced IDs stored exactly once** | every PR | 4m7s on PR #37; enricher reported 480 pending rows |
| `ui-gate` | real browser through `edge`: tabs render, an as-of stamp is visible, no forecast card predates the city-local today (the F6 regression), and **zero off-origin requests** | every PR | 1m30s–1m35s; last run 153 same-origin, 0 external |
| `model-grounding` | 8 adversarial cases against real llama.cpp + Qwen3-1.7B | release candidate | run `36055211121`: **82s**, 8/8 grounded; upgraded cache action ran on a cache miss |
| `restore-drill` | destroys pgdata, rabbitdata and all three outbox volumes; a **separate reader** (psql, not the API that accepted the writes) asserts each pre-backup `message_id` appears in `ingest_log` **exactly once**; post-backup IDs asserted absent *and* asserted committed before the disruption | release candidate | run `36055211121`: **104s**, measured RPO 19s, RTO 20–21s |
| `publish-images` | publishes only after scans and integration pass; wraps the pushed manifest in a platform-described index; asserts registry-side that each ref **is** an index with `linux/amd64`, and that `images.lock` names that same index | push to `main` | green on `2f92999` (run `36056250209`) |

**Why `model-grounding` and `restore-drill` are release-candidate rather than per-PR:**
not cost — 82s and 104s in the latest run are cheap next to `build-and-scan`. Blast radius. The restore
drill destroys volumes, and a stateful full-stack drill is the wrong default for every
dependabot bump. One "expensive or stateful" tier, not two conventions. They run on
`workflow_dispatch`, a `release-candidate` label, or a `release/*` branch.

---

## 4. Release: automated to the air gap, and no further

`release.yml` **does not deploy**. The target is air-gapped and the last hop is a
deliberate physical transfer. Automating up to that hop and stopping is the honest
boundary; inventing an online deployment to make CD look complete would be a worse
answer, not a better one.

| Step | Gate |
|---|---|
| Bind to a commit | Asserts the `ci` workflow **succeeded** for that exact SHA on `main`. A release cannot be cut from an untested commit, nor from a feature branch, since publishing is gated on push-to-main |
| Retrieve provenance | Downloads that run's `aow-images-<sha>` artifact |
| Re-resolve digests | **Independently re-resolves each digest against the registry** rather than trusting the artifact. `SHA256SUMS` is self-attesting — whoever replaces the tar replaces the checksums with it — so the registry is the only external anchor |
| Build the bundle | Calls `scripts/package-offline.sh` **unmodified** |
| Verify the bundle | Calls `scripts/verify-bundle-images.sh` **unmodified** |
| Install it | Installs with no pulls and runs the release smoke |
| Record the decision | `promotion-record.json`, written **into** the bundle so `SHA256SUMS` covers it |

The promotion record states **per image** whether a digest was registry-re-resolved or
only self-attested from the archive, because the `demos` image is built on the staging
machine and not published by CI. A reader must not infer uniform provenance from a bundle
that does not have it. Branch protection is read **live** at release time and recorded as
`verified: false` with a reason if the read fails, rather than carrying a constant that
would keep asserting a setting nobody checked.

The first complete hosted release run, [`36048802334`](https://github.com/AvihaiShai/act-on-weather/actions/runs/36048802334),
completed on `b6b38f9`: bundle build, archive verification, no-pull install,
data smoke, promotion record and artifact upload all passed. Disk free space was
**86 GB before packaging, 78 GB after packaging, and 78 GB after install**;
the bundle occupied **2.4 GB**. This replaces the earlier disk estimate with a
measurement from the runner actually used. The uploaded record revealed a
separate defect: it wrote `model.sha256` as `"#"` and used the comment in
`models.lock` as the model filename. PR #37 made the parser require one valid
sha256sum entry and match the bundle's independently generated model checksum.
The first run's record remains historical evidence of that failure. Release
[`36052133435`](https://github.com/AvihaiShai/act-on-weather/actions/runs/36052133435)
then targeted merged commit `804b5df` with the updated artifact actions. Its
first attempt stopped during model staging at exit 137 after about 705 MiB;
the unchanged retry completed the full bundle, verification, no-pull install,
smoke and upload. The downloaded artifact records the correct model path and
SHA `d2387ca2…d9bc7b5`, matching `models.lock` and the bundle's checksum;
`SHA256SUMS` also covers the promotion record itself. All 12 recorded release
gates are present. The record still says branch protection `verified: false`
because the workflow token's live read received HTTP 403.

PR #40 raised the **one-shot** model-staging container's memory cap from
256 MiB to 2 GiB after the unexplained exit 137. The identical rerun and a
local full download at 256 MiB both passed, so the cap change is headroom,
not a proven root-cause repair. Staging exits before the runtime services
start. Hosted release [`36055978771`](https://github.com/AvihaiShai/act-on-weather/actions/runs/36055978771)
exercised this configuration on merged commit `862a08f`: staging, bundle
build and verification, no-pull install, and the data smoke all passed;
the runner had **78 GB free after install**. Its downloaded promotion artifact
has 12 distinct recorded gates. The model path and SHA match `models.lock`
and the bundle checksum, `SHA256SUMS` seals the record, and the recorded CI
run is the exact commit's successful push run `36055198116`. The live branch
protection read remains explicitly unverified (HTTP 403), as in the earlier
record.

After PR #42 added the committed-tree secret scan, release
[`36057235570`](https://github.com/AvihaiShai/act-on-weather/actions/runs/36057235570)
targeted its merged commit `2f92999`. The same build, verification, no-pull
install and data smoke passed. Runner disk free was **86 GB before packaging,
78 GB after packaging, and 78 GB after install**. The uploaded record was
downloaded and checked for its 12 distinct gates, exact successful push CI run
`36056250209`, model path and SHA against `models.lock` and the bundle, and
its own SHA256SUMS entry. The live protection read again returned HTTP 403
and is recorded as unverified.

### The two install claims are not the same claim

| | What it proves | Where |
|---|---|---|
| **no-pull** | the bundle boots from its own bytes without pulling | `release.yml`, on a hosted runner that has internet throughout |
| **cannot-pull** | the bundle installs and runs on a separate engine with an empty image store and no reachable egress, verified from inside a container | a separate engine, **manual** |

The second is the stronger claim and it catches a defect class the first cannot: the
no-pull check would have **passed the broken bundle described in §6**, because the
packaging run's own `docker pull` left the layers in the runner's store. A same-host
install cannot detect that even in principle. The air-gap check is manual by
construction — a hosted runner cannot be made to lack a network it is using — and stays
described as a manual operator step rather than dressed up as a gate.

#### cannot-pull: the run

Executed by the offline-release work, not by this session; recorded here with
attribution. Full timings and residual limits in [`RELEASE-PROOF.md`](RELEASE-PROOF.md).

Two commits packaged, each from its own green `main` CI build:

| | commit | CI run |
|---|---|---|
| A | `f19224130aa859257f71f276f9bac53d30c7bb2e` | `36037543463` |
| B | `bf4a2dfd49bc20c1a09f1a2abf646b6891395a7c` | `36041468062` |

**Independently re-checked by this session** before citing: both runs are `success` with
`guard`, `unit`, `lint`, `build-and-scan`, `ui-gate` and `publish-images` all green, and
for both commits `services` and `ui` are published as a platform-described index
(`manifest.list.v2+json`, `linux/amd64`). That is the half of the claim that lives in CI;
everything below is the offline host's.

- **Engine separation.** `docker-ce` 29.8.1, engine id `f99ef3b5…`, distinct from Docker
  Desktop's `375fa6b6…`. **0 images and 0 volumes before the install**, so `docker load`
  was the only possible source. Packaged on Docker Desktop 29.8.0 and installed on the
  other engine — two engines, which is the point.
- **Egress actually cut**, verified at install time from a container on a *routable*
  network rather than the stack's `internal: true` backend, so it tests the host firewall
  and not Docker's own isolation: `HTTPS raw-IP BLOCKED`, `HTTP raw-IP BLOCKED`,
  `DNS ghcr.io BLOCKED`.
- **Verification before load.** `verify-bundle.sh` exit 0 in 2 s with the checksum carried
  out of band: `SHA256SUMS` matched, `images.bundle.lock` matched the CI manifest and the
  committed `IMAGES.lock`, `images.tar` matched by verified manifest digest (10 images),
  and — the check added after §6 — *"images.tar is complete: every image has its config
  and layers, as linux/amd64"*. 195 files verified.
- **Install:** exit 0 in 40 s, **0 pull attempts** in the log, 10 images loaded.
  **Smoke:** `PASS: API, stored forecast, scores, agent, model, UI and edge`; stored counts
  matched `MANIFEST.json`; `aow_backend Internal=true`.
- **Upgrade A → B with live data:** PASS in 38 s, 0 pulls, pre-upgrade dump taken before
  the new images touched the schema, three traced `message_id`s still `stored`.
- **Deliberately failed migration:** aborted, `migrate` exit 3, its own dump taken first,
  `weather_daily` 80 → 0, every other table untouched. **Image rollback:** reverted, and
  the smoke **failed correctly**, waiting out its full 480 s deadline. **Restore:** PASS in
  18 s, all counts and all three traced IDs back.
- **Monitoring from the bundle:** 0 pulls, Grafana `database: ok`, 11 alert rules, seven
  scrape targets up, `grafana.com` and `1.1.1.1` unreachable from the container, 0 error or
  warn lines.
- **`prove-offline.sh offline`:** exit 0; E1 and E2 answered from stored data with source
  and as-of; an out-of-window question refused with `local model called: False`.

**The limit, carried deliberately rather than buried.** This is a separate Docker engine
and a separate Linux userspace — **not** a separate physical machine and not a separate
VM. All WSL2 distributions share one utility VM, kernel and network-namespace root, so the
cut is firewall-enforced inside a shared VM, and the bundle travelled over drvfs rather
than physical media. What it establishes is a clean engine with **no image cache to fall
back on and no reachable egress**, which is precisely what the broken bundle failed against
and what a no-pull run on a connected runner would have passed. It is not a certification
that the stack runs on hardware that has never seen a network.

---

## 5. Enforcement — `main` did not require CI

**The single largest finding.** `main` had no branch protection and no rulesets. Every
gate in this repository was advisory: a direct push merged without CI, and all seven PRs
to that point were merged by an admin with nothing forcing checks green.

Now enforced, and verified by an actual rejected push from an **admin** account:

```
remote: error: GH006: Protected branch update failed for refs/heads/main.
remote: - Changes must be made through a pull request.
remote: - 4 of 4 required status checks are expected.
```

Required contexts: `lint`, `unit`, `guard`, `build-and-scan` — deliberately only the four
that **always** report. A required context that can be skipped deadlocks merges, which is
why the conditional RC jobs are not required.

Two caveats stated rather than hidden:

- `strict: false` — a PR need not be up to date with `main`. Deliberate: several branches
  were converging, and strict mode serialises them into a rebase queue.
- `required_approving_review_count: 0` — a PR is mandatory, an approval is not, because on
  a single-maintainer repository requiring one deadlocks.

**So the enforced property is "every commit on `main` arrived by pull request and passed
four checks", not "every commit was reviewed."** The release documentation should say the
former.

Also enabled, all previously off: secret scanning, push protection, Dependabot security
updates, vulnerability alerts, and `sha_pinning_required` (which refuses unpinned actions
at the platform level, beneath the repo's own guard job).
`secret_scanning_non_provider_patterns` and validity checks **would not enable on this
plan** — a platform limit, recorded rather than omitted.

---

## 6. The defect this work existed to find

Every offline bundle built on a **containerd image store** was silently missing both
application images, and every check in place passed it.

CI published a classic single manifest with no index and no platform descriptor. On a
containerd store — the default on Docker Desktop and recent docker-ce — `docker save` of
such an image **exits 0** and writes 10,240 bytes with 2 blobs, for an 11-layer ~247 MB
image. `package-offline.sh` uses `docker save`. `verify-bundle-images.sh` passed the
result and `sha256sum -c` passed on all 151 files.

The registry was never at fault: all 12 blobs the manifest references return HTTP 206. A
platform-less manifest simply cannot be platform-matched on export. So the images are
**not rebuilt** — `docker buildx imagetools create` wraps the pushed manifest in an index
carrying `linux/amd64` and pointing at the same child digest, leaving the bytes Trivy
scanned and the integration test exercised untouched.

Three things are worth keeping from how this was found:

1. **A poisoned store gives a false negative.** Once a containerd store holds a
   platform-less record for a child manifest digest, an index-wrapped pull of that same
   child **keeps exporting empty** — the stale record wins. The fix looked broken until it
   was tested on a genuinely purged store.

   Confirmed directly afterwards, on Docker Desktop 29.8.0 with a containerd store — the
   same engine and version on which the wrapped image had appeared unexportable. Pulled
   fresh from `main`, the current image exports completely:

   ```
   type: application/vnd.docker.distribution.manifest.list.v2+json
   child sha256:808b86405188d903d  config_present=True  layers=11  missing=0
   VERDICT: LOADABLE as linux/amd64
   ```

   That matters because it rules out a competing explanation. The same engine, the same
   media type and the same command produce a complete archive for an image the store has
   never held in the bare form, and an empty one for an image it has. The variable is
   store state, not the engine and not the manifest media type.

   **`docker rmi <tag>` does not clear it.** Removing the tag — even `-f`, even removing
   both the tag and the digest reference — leaves the record in place and the archive
   still comes back empty. It clears only when the image is removed **by image ID**, or by
   a prune. This is the detail that produced the misdiagnosis: three consecutive attempts,
   each preceded by an `rmi` that looked thorough, all returned 10,240 bytes, which reads
   as "this image cannot be exported" rather than "this store still remembers it".
   Anyone reproducing this has to purge by ID or they will reach the same wrong
   conclusion.

   **Who is affected:** only a machine that pulled the pre-fix bare manifests — which
   means the machines used to investigate this, and not a reviewer's. A fresh machine and
   a CI runner are both clean by construction. A long-lived staging host that ever pulled
   the bad shape keeps producing broken bundles until those digests are purged, so the
   staging engine's history is a release-critical property and is recorded as one.
2. **Digests must be resolved registry-side.** `docker pull` + `RepoDigests` returned the
   index on one engine and the platform child on another, and — worse — returned
   *different answers for two images in the same run*, because `grep -m1` over
   `RepoDigests` can see both the child and the index after a push followed by a pull.
   `imagetools inspect --format '{{.Manifest.Digest}}'` never consults the local store, so
   it cannot disagree with itself. **Any command that resolves a digest through the local
   image store gives a store-dependent, sometimes order-dependent answer.**
3. **A wrong lock recreates the defect.** `package-offline.sh` pulls the reference straight
   out of `release/images.lock`, so a child digest there returns the platform-less manifest
   and rebuilds the empty-archive bug — failing three steps downstream, looking like a
   packaging fault. Hence the assertion in §3 that the lock names an index.

---

## 7. Gates that printed instead of asserting

The review's standard — "a scripted demo that prints success without asserting its claims
is not sufficient" — applied to this repository's own proofs:

- **`demos/05_questions.sh`** printed questions and answers and asserted nothing, while
  being listed in `make demo` as a proof, where it could only ever pass. Its closing note
  claimed all three edge questions were answered without calling the model; probing the
  live stack, that is true for two. It now has 8 expectations and a note that matches
  reality. Writing them found a second thing: a "where" question counts under
  `rows_used.venues`, not `.places`.
- **`make verify`** runs the four cheap PR gates the way CI runs them — separately, judged
  by exit code. This exists because a session pushed a branch that failed
  `ruff format --check` after reading the same gates as green locally: chained commands,
  combined output grepped for a success line, and the format check reports failure by
  printing something the grep did not match.
- **The platform-descriptor assertion itself** failed on its first real run with
  `JSONDecodeError` — it piped the manifest into `python3 - <<PY`, where the heredoc is
  already stdin, so the JSON was discarded. It reported the publish format as broken while
  the format was correct. A gate that fails on correct inputs is as much a defect as one
  that passes on broken ones.

---

## 8. Token scope, pins, and Dependabot

`packages: write` previously existed on **every pull-request run**, because job-level
permissions are unconditional while only the publish steps were `if:`-gated. Publishing is
now a separate job, so that token scope exists only on push-to-main. Every job carries a
`timeout-minutes` and a concurrency group (`main` excluded from `cancel-in-progress`: a
cancelled run there would leave a commit's image manifest unpublished).

That exclusion alone did not protect a *pending* run: GitHub replaced the
pending push-to-main run `36053918189` when a manual RC run entered the same
concurrency group. PR #41 gives each `main` run a unique group while retaining
superseded-run cancellation for PR branches. The affected push CI was rerun
successfully. On merged commit `862a08f`, push run `36055198116` and manual
RC run `36055211121` both progressed without replacing each other; the push
run published its image artifact. This matters because release gating requires
a successful push-to-main run and `aow-images-<sha>` for that exact commit.

**Issue #4 (Node 20 annotations) is closed**, and both halves of the reasoning that first
kept it open are kept here, because one of them was wrong. The wrong half was its premise
that our own four pins were the source: `actions/cache@0400d5f6` appears nowhere in
`ci.yml` and was pulled in **transitively** by `trivy-action`'s composite, so no sweep of
our own pins could have cleared it. The right half was the order of work. The two bumps
that change behaviour rather than quiet a log line, `gitleaks-action` v2→v3 and
`trivy-action` v0.35→v0.36, both alter a **security gate**, and bumping a scanner to
silence an annotation is how a gate stops gating.

So the sweep was taken as two attributable changes rather than one. #39 moved the core and
artifact actions (`checkout` v7.0.1, `setup-python` v7.0.0, `upload-artifact` v7.0.1,
`download-artifact` v8.0.1, `cache` v6.1.0); #38 moved the two scanners, in a commit each.
Evidence for the sweep and its follow-up:

- **The annotation is gone.** Run `36050580394` (`804b5df`) carries six Node 20 warnings;
  run `36055198116` (`862a08f`) carries none. `model-grounding` and `restore-drill` are
  skipped on an ordinary push, so a push run alone would not have exercised every pin
  — dispatch run `36055211121` on the same commit covers those two, `actions/cache`
  included.
- **Nothing node20 is left in the transitive closure either**, which is the part the issue
  said a version sweep could not reach. `trivy-action` v0.36.0 pulls `setup-trivy` v0.2.6
  and `actions/cache` v5.0.5; `setup-trivy` pulls `cache/restore`, `cache/save` and
  `checkout` v6.0.1. All node24, and that is the bottom of the tree.
- **Trivy still gates.** The bundled binary moved to v0.70.0 and all three scans report
  zero, so the newer scanner surfaced no new HIGH/CRITICAL to suppress.
- **Gitleaks detects a positive control.** The v3 action runs the pinned CLI,
  and the canary committed with the upgrade proves that binary detects a
  generated token. The committed-tree scan below separately proves it reads
  the source being released.
- **The artifact chain survived the majors.** `if-no-files-found` and `retention-days` are
  unchanged in `upload-artifact` v7, and the least-common-ancestor rule still puts
  `images.lock` at the artifact root, so `package-offline.sh` finds what it expects. The
  v5 path-behaviour break in `download-artifact` is by artifact **ID**; every download here
  is by **name**. The one new behaviour that reaches us is v8's `digest-mismatch: error`
  default, which fails a corrupt image tar at the download rather than at `docker load`.

The canary earned its place immediately, though not in the way it was meant to. It passes
while proving only that the binary detects a token in a directory — and on a merge push
the action's own generated range (`--no-merges --first-parent`) scanned **zero commits**
and still reported success. That is not a v3 regression; v2 generated the identical range.
The committed-tree scan in #42 closes it, and it is the argument for positive controls:
the gate the canary vindicated was the one that was not running.

The pinning guard had a matching blind spot, fixed here. It globbed
`.github/workflows/*.yml` only, so a workflow added as `*.yaml` would have been skipped in
silence rather than checked, and an empty glob would have reported success. It now reads
both suffixes and fails when it finds no workflow at all. It still cannot see transitive
pins — a composite action's internals are fetched at run time, never read from this tree
— which is exactly why the `actions/cache` above needed a human to catch.

`dependabot.yml` groups pip updates across all three directories, for **both** version and
security updates. Dependabot's first security PR failed `unit` in 21s: `requests` is
pinned in both `services/common` and `services/ui`, `tests/Dockerfile` installs them into
one interpreter, and a single-directory bump is `ResolutionImpossible`. The update was
correct in isolation and unbuildable in place — a defect in the tree, not the bot.
Grouped PR #21 raised `requests` consistently in all three requirements files,
passed the new security scanners and every per-PR gate, and was merged; the
open Dependabot alert count fell from six to zero. The older single-directory
PR #10 was closed as superseded.

---

## 9. What remains manual, and why

| Item | Why it is not a gate |
|---|---|
| Air-gap install certification | A hosted runner has internet throughout. Requires a separate engine with egress dropped; manual by construction. **Executed** — see §4 |

## 10. Known limits of this matrix

- **A reproducible symptom is not a diagnosis.** The empty-archive failure was reproduced
  independently by two people on two engines, and both of us then explained it with the
  wrong variable — first the published media type, later the engine version — before the
  measurement in §6 isolated store state. Each wrong explanation implied a different fix
  to the publish path. The symptom being real is not evidence that the cause has been
  found, and a fix applied to the wrong variable would have looked like it worked, because
  republishing anything also repopulates the store.

  The misdiagnosis had a clean positive control (an OCI-index image exported correctly)
  and a clean negative (ours did not), and still drew the wrong line between them, because
  the two samples differed in **two** ways at once — media type and store history — and
  only one was varied. What settled it was a positive control that held media type fixed
  and varied store history alone: a *freshly published* image of the same media type on
  the same engine.
- Timings are from single runs, not averages.
- `release-smoke.py` asserts **data** for weather and scores but only **liveness** for
  agent, llm, ui and edge. It is a partial gate and `docs/RELEASE.md` says so.
- The first full hosted release run completed on `b6b38f9` but its promotion
  record's model field was malformed. PR #37 repaired the writer; the corrected
  artifact was inspected from the successful retry of run `36052133435` (§4).
  Its first attempt exited 137 while staging the model. A local reproduction
  at the same 256 MiB container limit passed, so the cause remains unproven.
  PR #40 increased staging headroom, but a successful run cannot by itself
  establish which resource caused the original kill.
