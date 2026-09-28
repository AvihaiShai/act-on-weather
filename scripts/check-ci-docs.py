#!/usr/bin/env python3
"""Fail if the documented CI job tables disagree with `ci.yml` itself.

Why this exists. `README.md` claimed for two merges that `restore-drill` runs on
"release candidates only", while `ci.yml` had already added a push-to-main
trigger to it, and `docs/CICD_EVIDENCE.md` -- the README's own evidence document
-- said the opposite. Nothing failed, because nothing read `ci.yml`. The other
half of the same drift was a whole job, `retraction-drill`, missing from
CICD_EVIDENCE's gate table while §5 discussed it at length.

So this reads `ci.yml` and the two tables, and compares them. `ci.yml` is the
truth here; a document is never evidence for another document.

What it checks:

  1. Both tables list exactly the jobs `ci.yml` declares -- no missing row, no
     invented row.
  2. README's prose count of jobs ("nine jobs") matches how many there are, and
     CICD_EVIDENCE's count of unconditional jobs ("six carry no `if:`") matches
     how many carry none.
  3. For each job, README's "when" cell names exactly the triggers the job's
     `if:` expression allows -- all of them, and none it does not allow. A job
     with no `if:` must be described as "every run" and must name no trigger.
  4. For the conditional jobs, CICD_EVIDENCE's "When" cell agrees with `ci.yml`
     about the one distinction that actually drifted: whether the job runs on a
     plain push to `main`. Its prose is looser than README's by design, so it is
     held to that narrower claim rather than to the full trigger set.

Deliberately not a YAML parser. It needs two things out of `ci.yml` -- the job
names and each job's `if:` text -- and an indentation-aware scan over the lines
gets both in a form that can be read out loud. Bringing in PyYAML to find a
two-space-indented key would be the less explainable choice. Standard library
only, so `python scripts/check-ci-docs.py` runs anywhere the repo does.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
README = REPO / "README.md"
EVIDENCE = REPO / "docs" / "CICD_EVIDENCE.md"

# The four triggers any job in this workflow can be gated on. Each is named by
# the substring that identifies it in an `if:` expression, and by the phrase the
# README table has to use for it. The phrases are deliberately literal: the
# point is that a human wrote the row and a machine can still check it.
TRIGGERS = {
    "push to main": {
        "in_if": "github.event_name == 'push' && github.ref == 'refs/heads/main'",
        "in_readme": "push to `main`",
    },
    "manual dispatch": {
        "in_if": "github.event_name == 'workflow_dispatch'",
        "in_readme": "`workflow_dispatch`",
    },
    "release branch": {
        "in_if": "startsWith(github.ref, 'refs/heads/release/')",
        "in_readme": "`release/*`",
    },
    "release-candidate label": {
        "in_if": "'release-candidate'",
        "in_readme": "`release-candidate`",
    },
}

# Written-out numbers, because that is how the documents say them.
NUMBER_WORDS = {
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}


def read_ci_jobs(text: str) -> dict[str, str]:
    """Return {job name: its `if:` expression, or "" when it has none}.

    Jobs are the two-space-indented keys under a top-level `jobs:`. A job's
    `if:` is a four-space-indented key inside it, and may be a folded block
    (`if: >`) continued on the following, more-indented lines.
    """
    jobs: dict[str, str] = {}
    in_jobs = False
    current: str | None = None
    collecting_if = False

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue

        # A top-level key ends the jobs block; `jobs:` begins it.
        if not line.startswith(" "):
            in_jobs = line.startswith("jobs:")
            current = None
            collecting_if = False
            continue
        if not in_jobs:
            continue

        indent = len(line) - len(line.lstrip())

        # A folded `if:` keeps absorbing lines indented deeper than its key.
        if collecting_if:
            if indent > 4:
                jobs[current] += " " + line.strip()
                continue
            collecting_if = False

        if indent == 2 and line.endswith(":"):
            current = line.strip().rstrip(":")
            jobs[current] = ""
            continue

        if indent == 4 and current is not None and line.strip().startswith("if:"):
            value = line.strip()[len("if:") :].strip()
            if value in (">", ">-", "|", "|-"):
                jobs[current] = ""
                collecting_if = True
            else:
                jobs[current] = value

    return jobs


def triggers_from_if(expression: str) -> set[str]:
    """Which of the four known triggers an `if:` expression allows."""
    return {name for name, trigger in TRIGGERS.items() if trigger["in_if"] in expression}


def read_table(text: str, after: str, path: Path) -> dict[str, str]:
    """Return {job name: its "when" column} for the first table after `after`.

    A row is `| \\`job\\` | ... |`. The job name is taken from the first cell only
    when that cell is exactly one backticked word, which skips the `|---|`
    separator without having to recognise it. The "when" column is found by its
    heading rather than by position, because the two tables order their columns
    differently -- README is job/when/what and CICD_EVIDENCE is
    gate/what/when/proof, and hardcoding an index here silently compared the
    wrong column.
    """
    start = text.find(after)
    if start < 0:
        fail(f"{path.name}: cannot find the anchor text {after!r}")
        return {}

    rows: dict[str, str] = {}
    when = None
    for line in text[start:].splitlines():
        if not line.startswith("|"):
            if rows:  # the table has ended
                break
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if when is None:
            headings = [cell.lower() for cell in cells]
            if "when" not in headings:
                fail(f"{path.name}: the table after {after!r} has no 'when' column")
                return {}
            when = headings.index("when")
            continue
        if len(cells) <= when:
            continue
        name = re.fullmatch(r"`([a-z][a-z-]*)`", cells[0])
        if name:
            rows[name.group(1)] = cells[when]
    if not rows:
        fail(f"{path.name}: found no job rows after {after!r}")
    return rows


PROBLEMS: list[str] = []


def fail(message: str) -> None:
    PROBLEMS.append(message)


def check_same_jobs(label: str, documented: dict[str, str], declared: set[str]) -> None:
    missing = sorted(declared - set(documented))
    invented = sorted(set(documented) - declared)
    if missing:
        fail(f"{label} has no row for {', '.join(missing)}, which ci.yml declares")
    if invented:
        fail(f"{label} has a row for {', '.join(invented)}, which ci.yml does not declare")


def main() -> int:
    ci_text = CI_YML.read_text(encoding="utf-8")
    readme_text = README.read_text(encoding="utf-8")
    evidence_text = EVIDENCE.read_text(encoding="utf-8")

    jobs = read_ci_jobs(ci_text)
    if not jobs:
        fail("ci.yml: no jobs found -- this checker cannot have read it correctly")
        return report()

    declared = set(jobs)
    unconditional = {name for name, expr in jobs.items() if not expr}

    readme_rows = read_table(readme_text, "`.github/workflows/ci.yml` defines", README)
    evidence_rows = read_table(evidence_text, "## 3. Gates, and the run that proves each", EVIDENCE)

    # 1. Both tables list exactly the declared jobs.
    check_same_jobs("README's job table", readme_rows, declared)
    check_same_jobs("CICD_EVIDENCE's gate table", evidence_rows, declared)

    # 2. The prose counts.
    total_word = NUMBER_WORDS.get(len(declared), str(len(declared)))
    expected = f"`.github/workflows/ci.yml` defines {total_word} jobs"
    if expected not in readme_text:
        fail(f"README should say {expected!r}: ci.yml declares " f"{len(declared)} jobs")
    uncond_word = NUMBER_WORDS.get(len(unconditional), str(len(unconditional)))
    expected = f"**{uncond_word}** carry no `if:`"
    if expected not in evidence_text:
        fail(
            f"CICD_EVIDENCE should say {expected!r}: {len(unconditional)} of "
            f"ci.yml's jobs carry no `if:`"
        )

    # 3. README's "when" cell names exactly the triggers the `if:` allows.
    for job in sorted(declared & set(readme_rows)):
        cell = readme_rows[job]
        allowed = triggers_from_if(jobs[job])
        if not jobs[job]:
            if cell != "every run":
                fail(
                    f"README's {job} row says {cell!r}; the job carries no "
                    f"`if:`, so it should say 'every run'"
                )
            named = {n for n, t in TRIGGERS.items() if t["in_readme"] in cell}
            if named:
                fail(
                    f"README's {job} row names {', '.join(sorted(named))}; the "
                    f"job carries no `if:` and runs on everything"
                )
            continue
        for name in sorted(allowed):
            if TRIGGERS[name]["in_readme"] not in cell:
                fail(
                    f"README's {job} row does not name {name} "
                    f"({TRIGGERS[name]['in_readme']}), which its `if:` allows"
                )
        for name in sorted(set(TRIGGERS) - allowed):
            if TRIGGERS[name]["in_readme"] in cell:
                fail(
                    f"README's {job} row names {name} "
                    f"({TRIGGERS[name]['in_readme']}), which its `if:` does not allow"
                )

    # 4. CICD_EVIDENCE, held to the narrower push-to-main claim.
    for job in sorted((declared - unconditional) & set(evidence_rows)):
        cell = evidence_rows[job]
        runs_on_push = "push to main" in triggers_from_if(jobs[job])
        says_push = "push to `main`" in cell
        if runs_on_push and not says_push:
            fail(
                f"CICD_EVIDENCE's {job} row does not say it runs on a push to "
                f"`main`, which its `if:` allows"
            )
        if says_push and not runs_on_push:
            fail(
                f"CICD_EVIDENCE's {job} row says it runs on a push to `main`, "
                f"which its `if:` does not allow"
            )

    return report(len(declared), len(unconditional))


def report(total: int = 0, unconditional: int = 0) -> int:
    if PROBLEMS:
        for problem in PROBLEMS:
            print(f"::error::{problem}", file=sys.stderr)
        print(
            f"\n{len(PROBLEMS)} disagreement(s) between ci.yml and the " f"documented job tables.",
            file=sys.stderr,
        )
        return 1
    print(
        f"README and docs/CICD_EVIDENCE.md agree with ci.yml: {total} jobs, "
        f"{unconditional} of them unconditional, and every documented trigger "
        f"matches its `if:`."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
