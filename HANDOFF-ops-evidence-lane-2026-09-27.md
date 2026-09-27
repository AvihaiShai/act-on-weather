# Handoff — operations and evidence lane, 2026-09-27

Branch `ops/release-fallback-and-gates`, worktree `D:\aow-ops-evidence`, based on
`a12ae378df45e288c85ab0b710cb0e1e6d55861f` (`a12ae37`), which was also
`origin/main` at the start and end of this session.

This lane owns `.github/workflows/**`, release/CI-focused checks, and this file. It
did not touch product code, seed data, `README.md`,
`docs/HIRING_MANAGER_DRESS_REHEARSAL_REPORT.md`, or the main checkout's working
tree. Two evidence documents that describe the workflows directly were updated in
this worktree — `docs/CICD_EVIDENCE.md` and `docs/RELEASE.md` — because the change
below made specific sentences in them factually wrong. Every other document
correction is *proposed text* in section 5, for its owning lane to apply.

This file sits at the repository root, not under `docs/`, on purpose: `docs/` is
enumerated by the README's further-reading table, and an unlisted file there is the
same documentation drift this repository keeps auditing. It is lane scaffolding and
should be dropped or moved under `.private/` before the submission is packaged.

One thing here is not a file change and cannot be reverted by dropping the branch:
**GitHub branch protection on `main` was modified.** Section 2 records the
before/after responses and the exact command to undo it.

---

## 1. What changed in the tree

| File | Change |
|---|---|
| `.github/workflows/release.yml` | The model-mirror fallback literal now names `ggml-org`, not `Qwen/`. Comment rewritten; the stale parity claim it carried is gone |
| `.github/workflows/ci.yml` | New blocking `guard` step, "Model mirror defaults agree". Also `restore-drill` now runs on push to `main` |
| `docs/CICD_EVIDENCE.md` | Required-contexts passages carry the 2026-09-27 state and the before/after; `guard`'s row names the new step; the RC-tier rationale records the `restore-drill` split |
| `docs/RELEASE.md` | Step 7 no longer documents the broken fallback as expected behaviour |

`git diff --stat` against `a12ae37`: four files. No product code, no Compose file,
no migration, no script.

---

## 2. R7 — the latent release fallback, fixed

**The defect, confirmed.** `release.yml:263` at `a12ae37` read:

```yaml
MODEL_BASE_URL: ${{ vars.MODEL_BASE_URL || 'https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/resolve/main' }}
```

`compose.tools.yml:70` defaults to the `ggml-org` mirror, and its comment at
`:61-69` records why: the `Qwen/` namespace publishes only Q8_0, not the pinned
Q4_K_M. Commit `d98b882` (2026-09-24) moved `compose.tools.yml` and `ci.yml` to
`ggml-org` and left `release.yml` behind. Because `release.yml` sets the variable in
the step's own `env:`, its literal **overrides** Compose's default rather than
deferring to it, so the `ggml-org` default could never have rescued a release.

**Why no release run could have caught it.** The repository variable
`MODEL_BASE_URL` has been set to the `ggml-org` mirror since 2026-09-24T16:29:51Z
(created), 16:40:07Z (last updated) — read from
`GET /repos/AvihaiShai/act-on-weather/actions/variables`, and it is the only
repository variable. All ten most recent `release.yml` runs concluded `success`.
That history is evidence about the variable, not about the literal.

**Sourcing caveat, carried into the comments deliberately.** The "publishes only
Q8_0" and "HTTP 404" readings exist in this repository as prose only —
`compose.tools.yml:61-69` and `d98b882`'s commit message. No HTTP transcript, API
JSON or CI log excerpt was ever captured, and the CI run that found it predates the
retained Actions window. The `ggml-org` side *is* independently checkable:
`models.lock`'s digest `d2387ca2…` is exactly what that comment says the mirror
serves, and every green stage since `d98b882` matched it. The new comments
therefore attribute the `Qwen/` claim to its source instead of restating it as
measured. **Do not re-test it from here** — this project's no-egress rules apply,
and a 2026-09-24 reading would not license a present-tense claim about a
third-party repository anyway.

**The fix chosen, and the one rejected.** Two options were on the table: point the
literal at `ggml-org`, or drop the `||` so a missing variable fails loudly the way
`ci.yml:696` does. A third was measured and rejected: passing
`${{ vars.MODEL_BASE_URL }}` through with no literal at all would also work,
because GitHub expands an unset variable to the empty string and Compose's
`${MODEL_BASE_URL:-…}` treats empty and unset alike — verified on Compose v5.5.1:

```
$ docker compose -f compose.tools.yml --env-file .env.example config | grep MODEL_BASE_URL
      MODEL_BASE_URL: https://huggingface.co/ggml-org/Qwen3-1.7B-GGUF/resolve/main     # unset
$ MODEL_BASE_URL= docker compose -f compose.tools.yml --env-file .env.example config | grep MODEL_BASE_URL
      MODEL_BASE_URL: https://huggingface.co/ggml-org/Qwen3-1.7B-GGUF/resolve/main     # set empty
```

It was rejected on explainability: it makes `release.yml` and `ci.yml` look
identical while behaving differently — `ci.yml` runs `stage_model.py` on the
runner's ambient Python, so an empty variable there is a hard error, while
`release.yml` goes through Compose and silently gets the default. Two identical
lines with different outcomes is not a file anyone should have to defend out loud.

So the literal stays and now reads `ggml-org`, **and the duplication is
machine-checked**, because a second copy of one URL is how this happened.

### The new `guard` step, with both controls exercised

`ci.yml` gains "Model mirror defaults agree": it parses
`${MODEL_BASE_URL:-…}` out of `compose.tools.yml`, then fails if any hardcoded
`https://huggingface.co/...` literal in any `.github/workflows/*.yml|*.yaml` differs
from it. Run exactly as CI will, from the repository root:

```
$ python - <<'PY'   # the step's body, extracted verbatim
compose.tools.yml default: https://huggingface.co/ggml-org/Qwen3-1.7B-GGUF/resolve/main
1 hardcoded model mirror reference(s) across 2 workflow file(s), all agreeing with compose.tools.yml
EXIT=0
```

**Positive control — the pre-fix tree must fail, and does.** Same step body, run
against a copy whose `release.yml` is `git show a12ae37:.github/workflows/release.yml`:

```
compose.tools.yml default: https://huggingface.co/ggml-org/Qwen3-1.7B-GGUF/resolve/main
::error::workflow model mirror disagrees with compose.tools.yml (https://huggingface.co/ggml-org/Qwen3-1.7B-GGUF/resolve/main): .github\workflows\release.yml:263: https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/resolve/main
EXIT=1
```

**Anti-vacuity control — no baseline must fail, not pass.** Same body against a copy
whose `compose.tools.yml` has the `${MODEL_BASE_URL:-…}` default flattened to a
plain value:

```
::error::compose.tools.yml no longer declares a ${MODEL_BASE_URL:-...} default; nothing to reconcile against
EXIT=1
```

It lives in `guard` rather than in `tests/unit/` for a concrete reason:
`.dockerignore:2` excludes `.github`, deliberately, so the test image cannot see the
workflows at all. Putting it in `guard` needs no change to `.dockerignore` or
`tests/Dockerfile`, and it sits beside "Workflow actions are pinned by commit",
which is the same shape of check over the same files.

**R7 can now be closed in the report.** Validation is a source read, as R7 itself
says; the three runs above are the reading.

---

## 3. R6 / F-4 — gates and required contexts

### 3.1 The always-run set, verified

`ci.yml` has eight jobs. Five carry no `if:` — `lint` (`:36`), `unit` (`:48`),
`guard` (`:60`), `build-and-scan` (`:333`), `ui-gate` (`:408`, line number now
shifted by the new guard step). `release.yml` has one job, `release`, on
`workflow_dispatch` only, so it can never report on a pull request. No job has a
`name:` key, so every status-check context equals its job id verbatim.

Measured rather than inferred, on two real commits:

| Head | `lint`/`unit`/`guard`/`build-and-scan`/`ui-gate` | `publish-images` | `model-grounding`/`restore-drill` |
|---|---|---|---|
| `97fa8d9` (PR #65 head, `pull_request`) | success | **skipped** | skipped |
| `a12ae37` (`push` to main) | success | success | skipped |

So `ui-gate` does report on a pull request, and `publish-images` does not — which
is exactly why `ci.yml:440-442` warns against requiring the latter.

### 3.2 Branch protection changed — before and after

Access was available: the authenticated token is `AvihaiShai` with
`permissions.admin: true` on the repository. `GET /rulesets` returned `[]` both
before and after, so classic protection is the only mechanism in play.

**Before** (`GET /repos/AvihaiShai/act-on-weather/branches/main/protection`, exit 0):

```json
{"required_status_checks":{"strict":false,"contexts":["lint","unit","guard","build-and-scan"],"checks":[{"context":"lint","app_id":15368},{"context":"unit","app_id":15368},{"context":"guard","app_id":15368},{"context":"build-and-scan","app_id":15368}]},"required_pull_request_reviews":{"dismiss_stale_reviews":true,"require_code_owner_reviews":false,"require_last_push_approval":false,"required_approving_review_count":0},"required_signatures":{"enabled":false},"enforce_admins":{"enabled":true},"required_linear_history":{"enabled":false},"allow_force_pushes":{"enabled":false},"allow_deletions":{"enabled":false},"block_creations":{"enabled":false},"required_conversation_resolution":{"enabled":false},"lock_branch":{"enabled":false},"allow_fork_syncing":{"enabled":false}}
```

**The change.** A `PATCH` to the `required_status_checks` **sub-resource only**, so
nothing else in the protection object was sent:

```
gh api --method PATCH \
  repos/AvihaiShai/act-on-weather/branches/main/protection/required_status_checks \
  --input - <<'JSON'
{"strict": false,
 "checks": [{"context": "lint", "app_id": 15368},
            {"context": "unit", "app_id": 15368},
            {"context": "guard", "app_id": 15368},
            {"context": "build-and-scan", "app_id": 15368},
            {"context": "ui-gate", "app_id": 15368}]}
JSON
```

Response, exit 0:

```json
{"strict":false,"contexts":["lint","unit","guard","build-and-scan","ui-gate"],"checks":[{"context":"lint","app_id":15368},{"context":"unit","app_id":15368},{"context":"guard","app_id":15368},{"context":"build-and-scan","app_id":15368},{"context":"ui-gate","app_id":15368}]}
```

**After**, re-read in full. A field-by-field comparison of the before and after
responses with `required_status_checks` removed from both is **identical** —
`strict: false`, `enforce_admins: true`, `dismiss_stale_reviews: true`,
`required_approving_review_count: 0`, `allow_force_pushes: false`,
`allow_deletions: false`, `required_linear_history: false`, `lock_branch: false`,
`required_signatures: false`, `block_creations: false`,
`required_conversation_resolution: false`, `allow_fork_syncing: false`.

**To undo**, re-send the same `PATCH` with the four-entry `checks` array.

**`required_approving_review_count` was deliberately left at 0.** R6 names it
alongside `ui-gate`, but they are not the same kind of finding. GitHub does not
count a pull request author's own approval, and this repository has one author, so
requiring one approval would block every merge rather than review anything. It is a
single-maintainer limitation to state in the interview, not a setting to raise.
Recommend R6 be marked done on the `ui-gate` half and reframed on this half.

### 3.3 F-4 — implemented for one job, declined for the other

F-4 asks for `restore-drill` and `model-grounding` on push to `main`. Cost, measured
from run `36256406183` (`workflow_dispatch`, `bbee42c`) where both actually ran:

| Job | Window | Duration |
|---|---|---|
| `lint` / `guard` / `unit` | 16:42:24Z → 16:42:30Z / 16:42:36Z / 16:44:35Z | 6s / 12s / 131s |
| `restore-drill` | 16:44:38Z → 16:46:37Z | **119s** |
| `model-grounding` | 16:44:39Z → 16:47:09Z | **150s** |
| `build-and-scan` | 16:44:38Z → 16:48:55Z | 257s |
| `ui-gate` | 16:48:59Z → 16:50:46Z | 107s |

Both RC jobs start after `lint`/`unit`/`guard` and finish **before**
`build-and-scan`, so they are entirely inside its window and contribute nothing to
the critical path. Whole-run wall clocks bear it out: that run took 506s
(16:42:20Z → 16:50:46Z), against 524s for push run `36255839516` on the *same
commit* without them (16:32:46Z → 16:41:30Z, which did include `publish-images`).
Added runner time is ~4.5 minutes per push.

**Decision: `restore-drill` yes, `model-grounding` no.**

- `restore-drill` runs on push to `main` as of this branch. It adds no wall clock
  and no external dependency the always-run jobs do not already have — PyPI for the
  service-image build it does itself, and pinned digests for everything it starts.
  Its blast radius is its own CI Compose project.
- `model-grounding` stays release-candidate-only. It needs the 1.2 GB model, so a
  cache eviction (Actions caches are evicted on inactivity) puts a third-party
  download on the routine path, and `ci.yml:696` passes `MODEL_BASE_URL` with **no**
  fallback by design. A cleared repository variable or a HuggingFace outage would
  then redden `main` for a reason unrelated to the commit. Today that exposure is
  confined to moments when a maintainer is watching, and that is worth keeping.

**Neither job can become a required status check**, before or after this change:
both are skipped on an unlabelled pull request, and a skipped check is not a success
under branch protection — the deadlock `ci.yml:440-442` describes. F-4 buys
post-merge detection on `main`, not a gate. Recommend F-4 be marked partially done
with that split and that reason.

---

## 4. Report closeout audit — what is closable from source, and what is not

The task asked which §11.2 items can be settled by source/evidence review and which
need a new live reading. Four were audited. **Nothing below was applied to the
report**; section 5 has the proposed text.

| Item | Verdict |
|---|---|
| §11.2(4) F1/F4 have no status label | **Closable from source.** Both labels are derivable from sentences already in §5.1 |
| §11.2(3) unsaved outbox counts | **Closable from source, as a documentation fix.** Exactly two numbers lack a transcript, and they are now identified precisely |
| §11.2(6)/R10 migration provenance | **Split.** What the repository *declares* is fully closable; the one behavioural question R10 isolates still needs a live `up`, and must not be run from the dirty tree |
| §11.2(5) R0 backup prerequisite | **Needs one live reading**, and it is trivial and read-only |

### 4.1 F1 and F4 status cells — closable

The F1-F10 matrix is at report `:759-767`. F1 (`:761`) and F4 (`:763`) both have the
bare status cell `see 5.1`. Every other row uses a bold, labelled verdict —
`**Partial — two current gaps**` (`:762`), `**Pass on implementation, Partial on
enforcement**` (`:764`), `**Partial — deliberate, disclosed**` (`:765`),
`**Partial on implementation · Fail on demonstration**` (`:766`), `**OPEN — live
physical check Not run**` (`:767`). A proposed cell should match that shape.

For **F1**, §5.1 carries the Pass at `:771` ("the strongest part of the submission,
and it was verified live tonight"), `:876-877` (the durable-acceptance boundary
stated precisely), `:884-887` (the closing arithmetic) and `:919-924` (live code
byte-identical to HEAD, re-verified by sha256). The single named gap is `:926-928`:
live `reconcile.py` is 108 lines against HEAD's 137, so `bbee42c`'s `NotReplayable`
and `SystemExit(3)` are absent from the running stack and could not be exercised.
Nothing in §5.1 supports Partial or Fail for the mechanism itself.

For **F4**, the Pass on closure is at `:930` and `:936-938` (verified live that no
window is open: `aow_refresh_egress` does not exist, `aow_backend` is
`internal=true`, `aow_egress` has zero containers). The demonstration half cannot be
Pass: the matrix row itself says "No new refresh was performed", `:945-947` records
that a routine refresh publishes `RK_WEATHER` only, and `:948-955` that
`GET /refresh/last` returns `recorded: false`. **Note for whoever writes the cell:**
the F4 row's fourth requirement, "partial-provider reporting", has *no*
verdict-bearing sentence anywhere in §5.1 — do not let a status cell silently claim
it.

Caution: the working file `docs/rehearsal-review/06-counts/closeout-auditor-1-structure.md:77-94`
is the source of this finding but its line numbers are stale (it cites the table at
736 and the rows at 738/740). The current locations are `:759-767`, `:761`, `:763`.

### 4.2 The unsaved outbox counts — now pinned down exactly

The existing note at report `:791-796` disclaims transcripts for three things only:
the 1400/0 reading, the 480/0 reading, and the `/health` body. §11.2(3) says the
`85 weather.daily` and `81 fact.record` legs are also unbacked. That is confirmed,
and it is narrower than it sounds.

Of the closing arithmetic `85 = 80 + 5` at `:884-887`, three legs are cited:
`ingest_log` `weather.daily` = 80 (`agentB/10-db-integrity.txt:61`), `weather_daily`
= 80 (`:45`), DLQ = 5 (`agentC/prom-rules.json`, `AowDeadLetters`, `"value":"5e+00"`).
Of `81 = 81 = 81`, two are cited: `ingest_log` `fact.record` = 81 (`:60`) and
`facts` = 81 (`:48`). The **outbox** leg of each — the per-routing-key envelope count,
which is the load-bearing one — is backed by nothing on disk. A word-boundary grep
for `85` across the whole 44-file evidence pack returns exactly one hit, and it is
an RGBA alpha value in `agentA/live_ui_app.py:979`. There is no per-routing-key
outbox breakdown anywhere in the pack; the only saved outbox readings are whole-file
totals.

Also worth recording, because it strengthens rather than weakens the existing note:
the 1400/0 and 480/0 figures *do* have a transcript, just a later and independent
one — `lead/2026-09-27-api-outbox-history-checks.txt:83-93` — and that file says so
about itself at `:158-162`.

So §11.2(3) closes by extending one parenthetical to name the two figures. Promoting
85 and 81 from asserted to cited would need a new live reading, and **that reading
must be taken before R0 destroys the outboxes.**

### 4.3 R10 — the declared side is closable; the behavioural side is not

Confirmed from source at `a12ae37`:

- `db/migrations/` holds nine files on disk but `git ls-files` returns **eight**;
  `009_record_retraction.sql` is untracked (`??`).
- `git show a12ae37:compose.yml | grep -n migrations` gives **eight** `-f` lines at
  `:120-127`. The working tree has nine at `:120-128`, the single added line being
  009. So R10's citation `compose.yml:120-127` is correct at `a12ae37` and
  off-by-one against the dirty tree.
- `tests/unit/test_migrations_applied.py` exists, 96 lines, five tests, and asserts
  the `-f` list against `db/migrations/` in both directions plus order and
  no-duplicates, with an explicit anti-vacuity test at `:59-61`. **It is a static
  YAML-versus-glob check and touches no database**, so it is not evidence that
  anything is applied. In the current dirty tree it passes only because both sides
  gained 009.
- No applied-migrations ledger exists anywhere: `git grep` for
  `schema_migrations|applied_migrations|migration_ledger|schema_version_table` finds
  nothing, `db/migrations/` creates nine business tables and no ledger, and the live
  side measures `ledger_tables` = 0 at `agentB/10-db-integrity.txt:13-17`.

R10's own text is unusually careful about measured versus inferred, and that should
be preserved as-is: two first-draft claims are explicitly withdrawn, the `make up`
prediction is labelled "Prediction, not yet observed", and the Compose recreation
behaviour is cited to third-party source rather than measured here.

**Not closable:** what a plain `docker compose up -d` does to a pre-existing volume.
§11.2(6) is right that it must not be run from the current working tree, because
`compose.yml:128` there would apply the untracked 009 as a side effect. **Do not let
a proposed 009 become an applied migration** by way of an observation run.

### 4.4 R0's backup prerequisite — one read-only live command away

`scripts/backup-state.sh` resolves its helper image in exactly three steps:
`AOW_SERVICES_IMAGE` (`:166`, defaulting to the **empty string**, not a tag) → the
`{{.Config.Image}}` of the first running `api`/`ingestor`/`consumer`/`enricher`
(`:168-174`) → a guess, `aow-bundle/services:$AOW_IMAGE_VERSION` if
`release-version.txt` exists else `aow/services:dev` (`:185-191`). It then gates on
a local-only `docker image inspect` at `:192-193` and dies exit 1 with "the helper
image … is not present; build it or set AOW_SERVICES_IMAGE". **There is no
`docker pull` anywhere in the file** and neither `docker run` passes `--pull`. It
also refuses to run at all unless `postgres` and `rabbitmq` are up (`:156-160`), so
it cannot be run after `make down` — which confirms §7.0's ordering.

Settled from source: the precedence, the no-pull behaviour, the exact failure mode,
and that the *guess* branch is never reached while a producer is running. On the live
rehearsal stack those containers are running and their `Config.Image` is
`aow/services:dev` (`agentB/00-provenance.txt:11-12`), so that is what `:192` will
inspect.

**Not settled from source:** whether the *tag* `aow/services:dev` still resolves on
this engine. The evidence pack measures only that the four running image **digests**
are absent (`agentB/00-provenance.txt:14-18`). One read-only
`docker image inspect aow/services:dev` settles it. **The claim recorded at
`.private/vm-upgrade-commands.md:334-347` does not cover this case** — it is scoped
to an offline *release install* with `release-version.txt` present, and this
checkout has no such file.

Separately, `scripts/backup-state.sh:241` does print per-artefact byte counts on
every run — `say "$name  ${bytes} bytes  sha256:${sha}"` — so that claim is closable
from source outright.

---

## 5. Proposed text for documents this lane does not own

### 5.1 Report — F1 and F4 status cells (§11.2 item 4)

F1 (`:761`), replacing `see 5.1`:

> `**Pass on the durability mechanism — one named gap: live `reconcile.py` is not HEAD**`

F4 (`:763`), replacing `see 5.1`:

> `**Pass on closure design · Partial on demonstration — no refresh was run**`

Both keep the existing "see 5.1" pointer wording in the Evidence column untouched.
If a shorter cell is wanted, `**Pass — one named gap**` and
`**Pass on implementation · Partial on demonstration**` match the house format.

### 5.2 Report — extend the outbox transcript note (§11.2 item 3)

Replacing the parenthetical at `:791-796`:

> *(No `sqlite3` transcript for this first reading was saved to the evidence
> directory. The 1400/0 and 480/0 figures were independently re-read on 2026-09-27
> and agree — `lead/2026-09-27-api-outbox-history-checks.txt:83-93`, which says as
> much about itself at `:158-162` — but that later reading does not retroactively
> supply the missing transcript, and none exists for the `/health` body either. Two
> further figures are asserted rather than cited: the **85** `weather.daily` and
> **81** `fact.record` envelope counts in the closing arithmetic below. No
> per-routing-key outbox breakdown exists anywhere in the evidence pack — only
> whole-file totals — so those two legs rest on the unsaved 00:31-00:55 reading. The
> other legs are cited: `ingest_log` 80 and 81 at `agentB/10-db-integrity.txt:60-61`,
> `weather_daily` 80 at `:45`, `facts` 81 at `:48`, and DLQ 5 in
> `agentC/prom-rules.json`. Re-taking the two outbox counts requires a live reading,
> and it must happen **before** R0 destroys the outboxes.)*

### 5.3 Report — R6 and R7 status

R6: mark the `ui-gate` half **done** — required contexts are now five, read from the
API on 2026-09-27, before/after recorded in
`HANDOFF-ops-evidence-lane-2026-09-27.md` §2 and `docs/CICD_EVIDENCE.md` §5.
Reframe the `required_approving_review_count: 0` half: it is a single-maintainer
limitation, not a setting to raise, because GitHub does not count an author's own
approval.

R7: mark **done** on branch `ops/release-fallback-and-gates`. The literal now names
`ggml-org` and a blocking `guard` step asserts it cannot drift from
`compose.tools.yml` again, with a positive control and an anti-vacuity control both
exercised. Note in the row that the `Qwen/`-publishes-only-Q8_0 reading remains
prose-sourced, never a transcript.

F-4: mark **partially done** — `restore-drill` on push to `main`, `model-grounding`
deliberately not, for the reasons in §3.3. Add that neither can ever be a required
context.

### 5.4 Report §5.2 — two citation corrections

- "`publish-images` on push to main only (`:446`)" — `:446` is the line of that
  job's `if:`. The job key is at `:443`. Either say "its `if:` at `:446`" or cite
  `:443`.
- "The workflow explains at `:437-442` why `publish-images` must not be required" —
  the substantive warning is `:440-442`; `:437-439` is the tail of the preceding
  paragraph about artifact naming plus a bare `#`.

Both line numbers shift downward once this branch merges, because the new `guard`
step adds lines above them. Re-read rather than transcribing.

### 5.5 `CLAUDE.md` (gitignored, main checkout — not edited here)

`CLAUDE.md:159-160` states that "The required `lint`, `unit`, `guard` and
`build-and-scan` settings were read from the GitHub branch-protection API on
2026-09-26". That is now the historical state. Proposed replacement for its owner:

> The required contexts are `lint`, `unit`, `guard`, `build-and-scan` and `ui-gate`,
> read from the GitHub branch-protection API on 2026-09-27 after `ui-gate` was added;
> the tree alone cannot prove them. `required_approving_review_count` is 0 and stays
> so: GitHub does not count an author's own approval and this repository has one
> author.

The same four-context claim appears at `.private/plans/OPEN-ISSUES.md:1072-1076`, for
that file's owner.

### 5.6 `docs/EVIDENCE-fresh-demo.md` — untouched

The report's §5.5 "new, cosmetic (P3)" item about `:224` quoting an old bootstrap
transcript that prints `/docs` is real but belongs to the content lane; this lane did
not touch that file.

---

## 6. Validation actually run, and what remains external

Docker was used for one read-only `compose config` render and nothing else. No
container was created, started or stopped; the live stack was not touched; no
release was triggered.

| Check | Command | Result |
|---|---|---|
| Both workflows parse | `python -c` PyYAML `safe_load` over `.github/workflows/*.yml` | OK — `ci.yml` 8 jobs, `release.yml` 1 job; every `run:` is a string |
| Job gating is as intended | same, printing each job's `if:`/`needs:` | 5 jobs with no `if:`; `restore-drill`'s new condition parses as one expression |
| New guard step, fixed tree | step body extracted verbatim, run from the repo root | exit 0, 1 literal found, agrees |
| New guard step, positive control | same body against `a12ae37`'s `release.yml` | exit 1, names `release.yml:263` and both URLs |
| New guard step, anti-vacuity control | same body with the Compose default flattened | exit 1, "nothing to reconcile against" |
| Guard body is valid Python | `python -m py_compile` | OK |
| Compose empty-vs-unset semantics | `docker compose -f compose.tools.yml --env-file .env.example config` with the variable unset, empty, and set | ggml-org / ggml-org / the set value |
| File hygiene on all four edited files | CR count, trailing whitespace, tabs, final newline | 0 / 0 / 0 / present |
| Context names are real | `GET /commits/{sha}/check-runs` on `97fa8d9` and `a12ae37` | `ui-gate` success on both; `publish-images` skipped on the PR head |
| Branch protection before/after | `GET`, `PATCH`, `GET` on `branches/main/protection` | §2.2; everything outside `required_status_checks` identical |
| No competing rulesets | `GET /rulesets` | `[]` before and after |

**Not run, and still owed externally:**

- `ruff check` / `ruff format --check` and the unit suite. Both need the pinned
  `tests/Dockerfile` image, and `scripts/local-gates.sh` builds it with Docker; that
  build was out of scope here. The only Python added is inside a workflow heredoc,
  which `ruff` does not lint anyway, and it compiles.
- `guard`'s nine `docker compose config` renders — unchanged by this branch, since no
  Compose file was touched.
- **A real CI run on this branch.** The new `guard` step and the `restore-drill`
  condition have not executed on a runner. Open the pull request and read `guard`'s
  "Model mirror defaults agree" step and, after merge, confirm `restore-drill`
  reports on the push to `main` rather than skipping.
- **A release run.** Not triggered, deliberately. Note that a green release still
  proves nothing about the fixed literal while `vars.MODEL_BASE_URL` is set; the
  proof is the `guard` step, not the run.

---

## 7. Things a next session must not do

- **Do not treat a clean-engine install as physical F10 proof.** Release run
  `36256978423` loaded into a second daemon on one hosted VM and
  `.github/workflows/release.yml:294-297` disqualifies itself in its own comment.
  That line was read again here and is unchanged by this branch.
- **Do not let the proposed migration 009 become an applied one.** It is untracked;
  the working tree's `compose.yml:128` wires it in. Any `up` meant only to observe
  R10 must come from a clean checkout of `a12ae37`.
- **Do not re-test the `Qwen/` mirror from this machine.** No-egress rules apply, and
  the claim is prose-sourced by design now.
- **Remember branch protection changed outside git.** Dropping this branch does not
  revert it; §2.2 has the undo command.
