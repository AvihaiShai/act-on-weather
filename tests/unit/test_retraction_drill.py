"""The retraction drill refuses any project it did not create itself.

What these guard. `scripts/retraction-drill.sh` ends by destroying its project's
volumes, and the drill it runs withdraws records with no un-retract route. Its
first version protected one name -- it compared the target against `AOW_PROJECT`
and stopped there -- and that left two holes of different shapes:

  * the live *demo* runs under `aow-demo`, which that comparison never mentioned;
  * a leftover project from an earlier run was deleted automatically on the way
    in, by a `down -v --remove-orphans` issued before `up` so that a stale outbox
    could not poison phase 1. That put the one volume-destroying command in the
    script behind a project name nobody had looked at.

So the preflight now refuses any project that already has a Compose container,
network or volume, and prints the command to remove it rather than running it.
The ordering is half of the property: the checks happen before the cleanup trap
is installed, before the credential file is written, and before any Compose
command at all, so by the time `down -v` is reachable the script has established
that the project held nothing it did not create.

Every test drives the real script against a stub `docker` on PATH, in the shape
`tests/unit/test_bootstrap.py` established. The stub logs every call, so a
refusal can be checked for what it did *not* do -- which is the assertion that
matters here, and the one a test of the exit code alone would miss. `DOCKER_HOST`
is pointed at a closed port as well, so a call that somehow escaped the stub
could not reach a real daemon.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_bundle_tamper import BASH

REPO = Path(__file__).resolve().parents[2]
DRILL = "scripts/retraction-drill.sh"

pytestmark = pytest.mark.skipif(BASH is None, reason="no POSIX shell with coreutils")

# Refused before anything was touched. The script documents 0/1/2 and this is the
# only code these tests accept for a refusal: exiting 1 would be indistinguishable
# from a drill that ran and failed.
REFUSED = 2

# Answers the three questions the preflight asks and nothing else. Which projects
# it claims to hold comes from AOW_STUB_EXISTING, a space-separated list, so each
# test poses one situation; every project not named there is empty, and `compose`
# is answered blandly so that a test which reaches it fails on its own assertion
# about the call log rather than on a stub error.
STUB_DOCKER = r"""#!/bin/sh
log() { [ -n "$AOW_STUB_CALLS" ] && echo "$*" >> "$AOW_STUB_CALLS"; return 0; }
log "$@"

# The project name is carried in the label filter for ps/network/volume, and in
# `-p` for compose. Pull it out of whichever is there.
project=""
prev=""
for arg in "$@"; do
  case "$arg" in
    label=com.docker.compose.project=*) project="${arg#label=com.docker.compose.project=}" ;;
  esac
  [ "$prev" = "-p" ] && project="$arg"
  prev="$arg"
done

holds=0
for existing in ${AOW_STUB_EXISTING:-}; do
  [ "$existing" = "$project" ] && holds=1
done

case "$1" in
  ps)
    # `docker ps -aq --filter label=...`: one id per line, or nothing.
    [ "$holds" = "1" ] && echo "c0ntainerid0"
    exit 0
    ;;
  network|volume)
    [ "$holds" = "1" ] && echo "$project-thing"
    exit 0
    ;;
  compose)
    exit 0
    ;;
esac
exit 0
"""

# A stub that fails the three read-only probes, for the case where the daemon
# cannot be asked at all.
STUB_DOCKER_BROKEN = r"""#!/bin/sh
log() { [ -n "$AOW_STUB_CALLS" ] && echo "$*" >> "$AOW_STUB_CALLS"; return 0; }
log "$@"
echo "Cannot connect to the Docker daemon at unix:///var/run/docker.sock." >&2
exit 1
"""


def run_drill(
    tmp_path: Path,
    *args: str,
    project: str | None = None,
    existing: str = "",
    stub: str = STUB_DOCKER,
    extra_env: dict[str, str] | None = None,
):
    """Run the real drill script against the stub. Returns (result, calls_text)."""
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir(exist_ok=True)
    binary = stub_dir / "docker"
    binary.write_text(stub, encoding="utf-8", newline="\n")
    binary.chmod(0o755)
    calls = tmp_path / "docker-calls"
    env_file = tmp_path / "drill.env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "AOW_STUB_CALLS": str(calls),
        "AOW_STUB_EXISTING": existing,
        # Supplied so the script never writes a credential file of its own. A
        # refusal must happen before this would matter either way.
        "AOW_ENV_FILE": str(env_file),
        # Belt and braces: if a call escaped the stub it must not find a daemon.
        "DOCKER_HOST": "tcp://127.0.0.1:1",
    }
    if project is not None:
        env["AOW_DRILL_PROJECT"] = project
    env.update(extra_env or {})
    result = subprocess.run(
        [BASH, DRILL, *args],
        cwd=REPO,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        env=env,
        check=False,
        # Nothing here legitimately takes seconds: every run either refuses or
        # stops at --preflight-only. A hang means the script reached `up`, and a
        # regression should say TimeoutExpired rather than block CI.
        timeout=120,
    )
    return result, (calls.read_text(encoding="utf-8") if calls.exists() else "")


def assert_touched_nothing(calls: str) -> None:
    """The assertion the exit code cannot make.

    A refusal is only a refusal if it happened before anything was created or
    removed. The three read-only probes are the whole of what may appear.
    """
    for line in calls.splitlines():
        assert not line.startswith(
            "compose"
        ), f"the script ran a Compose command before refusing: docker {line}"
        assert " down " not in f" {line} ", f"the script issued a down: docker {line}"
        assert " up " not in f" {line} ", f"the script issued an up: docker {line}"
        assert " rm " not in f" {line} ", f"the script removed something: docker {line}"


# --------------------------------------------------------- reserved by name ----

# Refused whatever state they are in. A live project that happens to be down
# still owns its name: creating volumes under it and then destroying them on the
# way out would leave the real stack on fresh volumes with nothing said.


@pytest.mark.parametrize("project", ["aow", "aow-demo"])
def test_the_live_projects_are_refused_even_when_empty(tmp_path, project):
    result, calls = run_drill(tmp_path, project=project, existing="")

    assert result.returncode == REFUSED, result.stdout + result.stderr
    assert "not a drill target" in result.stdout + result.stderr
    assert_touched_nothing(calls)


@pytest.mark.parametrize("project", ["aow", "aow-demo"])
def test_the_live_projects_are_refused_when_they_hold_something(tmp_path, project):
    """Same answer by a different route, and the message must still be the
    by-name one: "this is live" is more use to an operator than "this exists"."""
    result, calls = run_drill(tmp_path, project=project, existing=project)

    assert result.returncode == REFUSED
    assert "not a drill target" in result.stdout + result.stderr
    assert_touched_nothing(calls)


def test_the_demo_project_is_named_in_the_refusal():
    """`aow-demo` was the hole the first version had: it protected AOW_PROJECT and
    nothing else. Asserted against the source so the default cannot quietly go."""
    source = (REPO / DRILL).read_text(encoding="utf-8")
    assert 'DEMO_PROJECT="${AOW_DEMO_PROJECT:-aow-demo}"' in source
    assert 'RESERVED="$LIVE_PROJECT $DEMO_PROJECT' in source


def test_a_further_reserved_name_can_be_added_without_editing_the_script(tmp_path):
    result, calls = run_drill(
        tmp_path,
        project="aow-staging",
        extra_env={"AOW_RESERVED_PROJECTS": "aow-staging aow-spare"},
    )

    assert result.returncode == REFUSED
    assert "not a drill target" in result.stdout + result.stderr
    assert_touched_nothing(calls)


# ------------------------------------------------- refused for holding state ----


@pytest.mark.parametrize("kind", ["ps", "network", "volume"])
def test_any_project_that_already_holds_something_is_refused(tmp_path, kind):
    """One probe at a time, so each of the three is load-bearing on its own. A
    project with only a leftover volume -- no container, no network -- is exactly
    the case `docker compose ls` would call absent."""
    stub = STUB_DOCKER.replace(
        'case "$1" in',
        f'[ "$1" != "{kind}" ] && holds=0\ncase "$1" in',
        1,
    )
    result, calls = run_drill(
        tmp_path, project="someone-elses-stack", existing="someone-elses-stack", stub=stub
    )

    assert result.returncode == REFUSED, result.stdout + result.stderr
    assert "already exists" in result.stdout + result.stderr
    assert_touched_nothing(calls)


def test_a_leftover_drill_is_not_deleted_automatically(tmp_path):
    """The regression this file exists for. The previous version cleared a
    leftover project with `down -v --remove-orphans` before `up`, to stop a stale
    outbox poisoning phase 1. Now it stops, and prints the command."""
    result, calls = run_drill(
        tmp_path, project="aow-retraction-drill", existing="aow-retraction-drill"
    )
    output = result.stdout + result.stderr

    assert result.returncode == REFUSED
    assert_touched_nothing(calls)
    assert "down -v --remove-orphans" in output, (
        "the refusal must name the command that removes the leftover, or an "
        "operator is left to work it out"
    )
    assert (
        "docker compose -p aow-retraction-drill ps -a" in output
    ), "the refusal must say how to look at what is there before removing it"


def test_the_default_project_is_not_special_cased(tmp_path):
    """It would be tempting to let the drill reuse its own default name, since
    that is usually its own leftover. It is only usually."""
    result, _calls = run_drill(tmp_path, existing="aow-retraction-drill")

    assert result.returncode == REFUSED


def test_a_daemon_that_cannot_be_asked_is_a_refusal_not_an_assumption(tmp_path):
    """The dangerous default would be to read a failed probe as "empty". Silence
    from Docker is not evidence that the project holds nothing."""
    result, calls = run_drill(tmp_path, project="aow-retraction-drill", stub=STUB_DOCKER_BROKEN)
    output = result.stdout + result.stderr

    assert result.returncode == REFUSED, output
    assert "could not ask Docker" in output
    assert (
        "Cannot connect to the Docker daemon" in output
    ), "the operator needs Docker's own words, not a paraphrase"
    assert_touched_nothing(calls)


# ------------------------------------------------------------ a free project ----


def test_a_fresh_project_passes_the_preflight_and_starts_nothing_on_its_own(tmp_path):
    """--preflight-only exists so this is checkable without a stack: it runs the
    same checks and stops at the point the real run would begin creating things."""
    result, calls = run_drill(
        tmp_path, "--preflight-only", project="aow-drill-fresh", existing="aow-something-else"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "is free of containers, networks and volumes" in result.stdout
    assert_touched_nothing(calls)


def test_the_preflight_asks_about_containers_networks_and_volumes(tmp_path):
    """All three, against the project under test. A preflight that checked only
    containers would wave through the leftover volumes that hold the data."""
    _result, calls = run_drill(tmp_path, "--preflight-only", project="aow-drill-fresh", existing="")
    label = "label=com.docker.compose.project=aow-drill-fresh"

    assert f"ps -aq --filter {label}" in calls
    assert f"network ls -q --filter {label}" in calls
    assert f"volume ls -q --filter {label}" in calls


def test_the_preflight_runs_before_the_trap_and_before_any_down():
    """Ordering is half the property, and no stub run can prove a negative about
    code it never reached -- so this one is asserted against the source.

    The refusals above establish that a held project is refused. This
    establishes that the refusal is *early*: there is no `trap`, no `down` and no
    generated credential before the checks, so the volume-destroying command
    cannot be reached on a project the preflight has not cleared.
    """
    source = (REPO / DRILL).read_text(encoding="utf-8")
    preflight = source.index(
        "# ------------------------------------------------------------------ preflight --"
    )
    cleared = source.index('if [ "$PREFLIGHT_ONLY" -eq 1 ]')
    trap = source.index("trap cleanup EXIT")
    down = source.index("down -v --remove-orphans >/dev/null")
    mktemp = source.index('ENV_FILE="$(mktemp)"')

    assert preflight < cleared < trap, "the cleanup trap is installed before the preflight"
    assert cleared < down, "a down -v is reachable before the preflight has cleared the project"
    assert cleared < mktemp, "a credential file is written before the preflight"


def _commands(text: str) -> str:
    """The script without its prose. It explains itself at length, including
    saying where a `down -v` deliberately no longer is, so a substring search
    over the whole text matches its own rationale."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_nothing_is_torn_down_on_the_way_in():
    """The `down -v --remove-orphans` that used to sit between `config` and `up`
    is gone. Only the cleanup function may destroy volumes now."""
    source = (REPO / DRILL).read_text(encoding="utf-8")
    after_start = _commands(source.split('hr "Starting', 1)[1])
    tears_down = "down -v" in after_start
    assert not tears_down, "the drill tears something down after starting, outside cleanup()"

    cleanup = source.split("cleanup() {", 1)[1].split("\ntrap ", 1)[0]
    assert "down -v --remove-orphans" in cleanup, "cleanup no longer removes what it created"
