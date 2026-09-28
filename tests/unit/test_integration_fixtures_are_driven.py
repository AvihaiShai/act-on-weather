"""Every file in tests/integration is executed by something, and by something
that runs.

`pytest.ini` sets `testpaths = tests/unit`, on purpose: the files under
`tests/integration/` need a live Postgres, a live broker and in one case a live
model, so pytest must not try to collect them. The cost of that decision is that
adding a file there runs nothing at all, silently, and `.github/workflows/ci.yml`
records the one time it happened -- "`tests/integration/retraction.py`, was run
by nothing". That was fixed by adding a job for it, which closes the instance.
This closes the class.

The four retraction fixtures are the current reminder of why the instance is not
enough. They are driven by `scripts/retraction-drill.sh` and `ci-integration.sh`
does not mention them, so a reader checking the "integration tests" script
against the directory would conclude four files were orphaned and be wrong. The
list of drivers is what has to be complete, not any one driver.

WHAT COUNTS AS RUNNING IT. Only the two shapes that actually execute a file:

    docker compose exec api python - < tests/integration/smoke.py
    command: ["python", "-m", "tests.integration.model_grounding"]

A comment line does not count, and that exclusion is the point rather than a
detail. It rules out two different things and both matter. Five of the twenty-
seven references to these files across the drivers are prose explaining why a
drill exists, and a guard that accepted those would have been satisfied by the
very comment that described the orphaned file. And a commented-out *command*
matches these patterns character for character -- `#` appears in neither -- so
a guard reading the raw text would report a fixture as driven by a step
somebody had switched off, which is this file's own silence reached from the
other side. `_code_lines` drops whole-line comments before either pattern is
applied, which is what makes the sentence above literally true.

WHAT THIS DELIBERATELY DOES NOT CHECK, and where it belongs instead. A complete
list of drivers proves nothing if one of the drivers is itself never invoked --
which is the *other* half of the case ci.yml records, since `retraction.py`
existed and had no job. Checking it means reading `.github/workflows/ci.yml`,
and `.dockerignore` excludes `.github` from every build context in the
repository, including the two service images. Copying it into the test image
alone is not available -- `.dockerignore` is per-context, not per-Dockerfile --
and un-ignoring it would widen what `aow/services` and `aow/ui` are built from
to buy one assertion. So that half belongs in the `guard` job, which runs on the
host with the whole tree, beside `scripts/check-ci-docs.py`. It is named here
rather than left out silently, because the gap this file exists to close is
precisely a check nobody wrote down.

Nothing here runs a drill. It reads the shipped scripts, Compose overlays and
Makefile as text, which is the same thing `test_compose_ports.py` and
`test_observability.py` do with their own files.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "integration"

# Anything that could plausibly execute one. Read as text, so a driver that
# stops executing a fixture stops counting for it without anyone editing this
# list.
DRIVERS = sorted(
    {
        *ROOT.glob("scripts/*.sh"),
        *ROOT.glob("demos/*.sh"),
        *ROOT.glob("compose*.yml"),
        ROOT / "Makefile",
    }
)

# `python - < tests/integration/x.py` feeds the file on stdin, which is how every
# drill that needs a running container runs one: the file is not in the image at
# all, so it cannot be named as a path inside it.
STDIN_FORM = re.compile(r"python\b[^\n|]*<\s*tests/integration/([A-Za-z0-9_]+)\.py")
# `python -m tests.integration.x` is the other half, used where the file IS in
# the image -- the model probe builds its own. The character class after `-m`
# covers both the shell spelling and the JSON-array spelling Compose uses.
MODULE_FORM = re.compile(r"-m[\"',\s]+tests\.integration\.([A-Za-z0-9_]+)")


def _code_lines(text: str) -> str:
    """The driver with its comment lines removed, before either pattern runs.

    Whole-line comments only, which is what both comment languages here -- sh
    and YAML -- use to switch a command off. A trailing `#` on a line that also
    runs something is left alone, because that line does still run it.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _fixture_files() -> list[str]:
    return sorted(path.stem for path in FIXTURES.glob("*.py") if path.name != "__init__.py")


def _executions() -> dict[str, list[str]]:
    """fixture stem -> the drivers that execute it, by repo-relative path."""
    found: dict[str, list[str]] = {}
    for driver in DRIVERS:
        if not driver.exists():
            continue
        text = _code_lines(driver.read_text(encoding="utf-8"))
        for pattern in (STDIN_FORM, MODULE_FORM):
            for stem in pattern.findall(text):
                found.setdefault(stem, []).append(driver.relative_to(ROOT).as_posix())
    return found


FIXTURE_FILES = _fixture_files()
EXECUTIONS = _executions()


def test_the_premise_this_guard_rests_on_still_holds():
    """Two things, and without either of them this file is asserting nothing.

    `testpaths` must still exclude the integration directory -- if it were ever
    widened to `tests`, pytest would collect these files itself and the whole
    orphan problem would be someone else's. And the directory must still hold
    files, so an empty glob cannot pass every check below vacuously.
    """
    pytest_ini = (ROOT / "pytest.ini").read_text(encoding="utf-8")

    assert "testpaths = tests/unit" in pytest_ini
    assert len(FIXTURE_FILES) >= 15, FIXTURE_FILES


@pytest.mark.parametrize("stem", FIXTURE_FILES)
def test_every_integration_fixture_is_executed_by_some_driver(stem):
    """The whole point. A new file under tests/integration that nothing runs
    fails here, on a pull request, rather than sitting green for a release."""
    assert stem in EXECUTIONS, (
        f"tests/integration/{stem}.py is executed by nothing. Drive it from a "
        f"script under scripts/, or delete it -- a file that runs nowhere is "
        f"worse than no file, because it reads as coverage."
    )


@pytest.mark.parametrize("stem", sorted(EXECUTIONS))
def test_every_driven_name_is_a_file_that_exists(stem):
    """The other direction. A rename that missed a driver leaves a command
    pointing at nothing; `set -euo pipefail` catches that, but only when the
    drill runs, which for `retraction-drill` and `model-grounding` means a
    release candidate."""
    assert (
        stem in FIXTURE_FILES
    ), f"{EXECUTIONS[stem]} runs tests/integration/{stem}.py, which does not exist"


def test_the_drivers_are_the_three_scripts_and_the_one_overlay_this_file_names():
    """The list of drivers is what the docstring reasons about, so it is pinned.

    Three shell scripts -- two under `scripts/`, one under `demos/` -- and one
    Compose overlay. A fourth script appearing here is fine and is not a failure
    of anything, but it is the moment to check that the workflow invokes it,
    which is the half this file cannot see (see the docstring). Failing here is
    how that check gets remembered.
    """
    drivers = sorted({driver for drivers in EXECUTIONS.values() for driver in drivers})

    assert drivers == [
        "compose.model-probe.yml",
        "demos/06_backup_restore.sh",
        "scripts/ci-integration.sh",
        "scripts/retraction-drill.sh",
    ], drivers


def test_a_commented_out_command_does_not_count_as_a_driver():
    """The exclusion the docstring claims, asserted rather than assumed.

    `#` appears in neither pattern, so both match a disabled command exactly as
    they match the live one -- which means the filter, not the regex, is what
    excludes it. Pinned here because the failure it prevents is invisible: a
    drill step commented out "for now" would leave its fixture reading as driven
    while nothing ran it, which is the exact silence this file exists to break.
    """
    live = "dc exec -T api python - < tests/integration/smoke.py"
    module = '    command: ["python", "-m", "tests.integration.model_grounding"]'

    assert STDIN_FORM.search(live)
    assert MODULE_FORM.search(module)
    # The patterns alone cannot tell the two apart.
    assert STDIN_FORM.search("  # " + live)
    assert MODULE_FORM.search("#" + module)
    # The filter can.
    assert _code_lines("  # " + live) == ""
    assert _code_lines("#" + module) == ""
    assert _code_lines(live) == live
    # And it leaves a live command that merely ends in a comment alone.
    assert _code_lines(live + "  # the smoke fixture") == live + "  # the smoke fixture"


def test_the_five_prose_references_are_the_reason_the_filter_exists():
    """The other half of the same exclusion, measured rather than asserted.

    Five lines across the drivers name a fixture without running it, and one of
    them is `retraction-drill.sh`'s own inventory comment -- a guard that
    accepted any mention would have been satisfied by a file's description of
    itself. If this count moves, read the new line before changing the number:
    it is either more prose, which is fine, or a real command this guard is
    now failing to see.
    """
    mention = re.compile(r"tests[/.]integration")
    prose = [
        driver.relative_to(ROOT).as_posix()
        for driver in DRIVERS
        if driver.exists()
        for line in driver.read_text(encoding="utf-8").splitlines()
        if mention.search(line)
        and not (STDIN_FORM.search(line) or MODULE_FORM.search(line))
        and line.lstrip().startswith("#")
    ]

    # The files, not the line numbers: an unrelated edit above one of these
    # comments must not turn this into a failure that means nothing.
    assert prose == [
        "compose.model-probe.yml",
        "scripts/ci-model-grounding.sh",
        "scripts/ci-model-grounding.sh",
        "scripts/ci-ui-gate.sh",
        "scripts/retraction-drill.sh",
    ], prose


def test_the_four_retraction_fixtures_are_the_documented_case():
    """The instance that motivated the class, pinned so the file keeps its
    example. These are driven by `retraction-drill.sh` and by nothing in
    `ci-integration.sh`, which is exactly the shape that made the gap invisible.
    """
    retraction = [stem for stem in FIXTURE_FILES if stem.startswith("retraction")]

    assert len(retraction) == 4, retraction
    for stem in retraction:
        assert EXECUTIONS[stem] == ["scripts/retraction-drill.sh"], EXECUTIONS[stem]
