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
| CI/CD definitions present and running | **PASS** | `ci.yml` (8 jobs), `release.yml`; every run on `main` green |
| README present and substantive | **PASS** | `README.md`, plus `docs/ARCHITECTURE.md`, `TECHNICAL_DECISIONS.md`, `docs/RELEASE.md` |
| README counts are not stale | **PASS** | `scripts/snapshot_manifest.py --check` runs in `guard` on every PR |
| No secrets committed | **PASS** | `guard` rejects a tracked `.env` and any non-placeholder password in `.env.example`; gitleaks runs on every PR; secret scanning and push protection enabled at the repo level |

S1 is a low bar and the repository clears it. Nothing here is open.

---

## 2. B1 — partial, and honest about which parts

"Full tests for all components." 1146 unit tests run under `--network none` on every PR.
Per component:

| Component | Automated coverage today | Level |
|---|---|---|
| ingestor | unit + real broker/DB outage drills 4–5 driving its own outbox | **integration** |
| consumer | real integration (smoke, reconnect, 5 drills); unit coverage of `handle()` routing is shallow | **partial** |
| enricher | 1 unit test; container never started in CI | **partial** |
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

**Still open:** the enricher container is never started in CI, and `handle()`'s routing
table has no direct unit test. Both are recorded rather than papered over.

---

## 3. Gates, and the run that proves each

All timings from real GitHub-hosted runners, not estimates.

| Gate | What it asserts | When | Proof |
|---|---|---|---|
| `lint` | ruff check + format | every PR | ~10s, green on every run |
| `unit` | 1146 tests, `--network none` | every PR | ~1m11s |
| `guard` | no hosted-LLM SDK; no committed secret; gitleaks; every compose image, Dockerfile base and workflow action pinned by digest/SHA; `IMAGES.lock` reconciles **in both directions**; all 9 overlay combinations render; README counts match the snapshot | every PR | ~9s |
| `build-and-scan` | Trivy on both images and the filesystem; then real Postgres + RabbitMQ, 5 traced outage drills, reconciliation audit/replay, full restart, **6 traced IDs stored exactly once** | every PR | ~3m30s–4m11s |
| `ui-gate` | real browser through `edge`: tabs render, an as-of stamp is visible, no forecast card predates the city-local today (the F6 regression), and **zero off-origin requests** | every PR | 1m30s–1m35s; last run 153 same-origin, 0 external |
| `model-grounding` | 8 adversarial cases against real llama.cpp + Qwen3-1.7B | release candidate | **153s**, `PASS: 8 adversarial cases stayed grounded` |
| `restore-drill` | destroys pgdata, rabbitdata and all three outbox volumes; a **separate reader** (psql, not the API that accepted the writes) asserts each pre-backup `message_id` appears in `ingest_log` **exactly once**; post-backup IDs asserted absent *and* asserted committed before the disruption | release candidate | **110s**; measured RPO 24–26s, RTO 31–35s |
| `publish-images` | publishes only after scans and integration pass; wraps the pushed manifest in a platform-described index; asserts registry-side that each ref **is** an index with `linux/amd64`, and that `images.lock` names that same index | push to `main` | green on `8bcb21c` |

**Why `model-grounding` and `restore-drill` are release-candidate rather than per-PR:**
not cost — 153s and 110s are cheap next to `build-and-scan`. Blast radius. The restore
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

### The two install claims are not the same claim

| | What it proves | Where |
|---|---|---|
| **no-pull** | the bundle boots from its own bytes without pulling | `release.yml`, on a hosted runner that has internet throughout |
| **cannot-pull** | the bundle installs and runs on a separate engine with an empty image store and no reachable egress, verified from inside a container | a separate host, **manual** |

The second is the stronger claim and it catches a defect class the first cannot: the
no-pull check would have **passed the broken bundle described in §6**, because the
packaging run's own `docker pull` left the layers in the runner's store. A same-host
install cannot detect that even in principle. The air-gap certification is manual by
construction — a hosted runner cannot be made to lack a network it is using — and stays
described as a manual operator step rather than dressed up as a gate.

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

**Issue #4 (Node 20 annotations) is reviewed and deliberately left open**, with evidence
posted to the issue. Its premise was wrong: `actions/cache@0400d5f6` appears nowhere in
`ci.yml` and is pulled in **transitively** by `trivy-action`'s composite, so bumping our
own four pins cannot clear it. Nothing is failing — the runner already forces Node 24. The
two bumps that would change behaviour rather than quiet a log line, `gitleaks-action`
v2→v3 and `trivy-action` v0.35→v0.36, both alter a **security gate**, and bumping a scanner
to silence an annotation is how a gate stops gating.

`dependabot.yml` groups pip updates across all three directories, for **both** version and
security updates. Dependabot's first security PR failed `unit` in 21s: `requests` is
pinned in both `services/common` and `services/ui`, `tests/Dockerfile` installs them into
one interpreter, and a single-directory bump is `ResolutionImpossible`. The update was
correct in isolation and unbuildable in place — a defect in the tree, not the bot.

---

## 9. What remains manual, and why

| Item | Why it is not a gate |
|---|---|
| Air-gap install certification | A hosted runner has internet throughout. Requires a separate engine with egress dropped; manual by construction |
| Action major upgrades | A behaviour change to security gates; deliberately a human decision (§8) |
| Enricher container coverage | Not started in CI; recorded, not closed |
| `consumer.handle()` routing unit tests | Covered only where integration happens to exercise a key |

## 10. Known limits of this matrix

- **A reproducible symptom is not a diagnosis.** The empty-archive failure was reproduced
  independently by two people on two engines, and both of us then explained it with the
  wrong variable — first the published media type, later the engine version — before the
  measurement in §6 isolated store state. Each wrong explanation implied a different fix
  to the publish path. The symptom being real is not evidence that the cause has been
  found, and a fix applied to the wrong variable would have looked like it worked, because
  republishing anything also repopulates the store.
- Timings are from single runs, not averages.
- `release-smoke.py` asserts **data** for weather and scores but only **liveness** for
  agent, llm, ui and edge. It is a partial gate and `docs/RELEASE.md` says so.
- `release.yml` has been validated step-by-step against real registry and API responses,
  but a full end-to-end release run on a hosted runner had not completed at the time of
  writing; the bundle build's disk arithmetic (~5.4–5.9 GB against ~14 GB free) is
  calculated, not measured.
