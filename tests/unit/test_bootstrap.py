"""Bootstrap must fail loudly, honestly and only when something is wrong.

Every test here drives `scripts/bootstrap.sh` against a stub `docker` on PATH.
The stub's behaviour is shaped entirely by `AOW_STUB_*` variables, so each test
can pose exactly one question -- an unsupported Compose, a crash-looping
service, a `docker run` that will not run -- and assert on what the reviewer
would see: the exit code, and the message that names the next command.

The stub is deliberately not a Docker emulator. It answers the handful of
questions bootstrap actually asks, and every answer a test does not pin down is
the benign one, so a test that passes is passing because of the thing it set.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.test_bundle_tamper import BASH

REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(BASH is None, reason="no POSIX shell with coreutils")

# `pull` as a whole word, so the check cannot be fooled either way. A bare
# `"pull" in text` also matches the `--pull never` that the offline start
# passes *to prevent* a pull, and a bare `" pull " in text` only matched
# because `--env-file` happens to precede the subcommand in every recorded
# line -- drop that and the logged call begins "pull ...", with no leading
# space, and the negative assertion would pass on a run that pulled.
PULL_CALL = re.compile(r"(^|\s)pull\s", re.M)

# `python -c` is handed to a real interpreter (see AOW_STUB_PYTHON) so that the
# .env generator is exercised for real rather than mimicked. Everything else
# is a canned answer.
STUB_DOCKER = """#!/bin/sh
log() { [ -n "$AOW_STUB_CALLS" ] && echo "$*" >> "$AOW_STUB_CALLS"; return 0; }

# Is this image reference present on the stub host? Absent by default; present
# when AOW_STUB_IMAGES_PRESENT=1 and the ref is not named in
# AOW_STUB_MISSING_IMAGES.
image_present() {
  [ "$AOW_STUB_IMAGES_PRESENT" = 1 ] || return 1
  for miss in $AOW_STUB_MISSING_IMAGES; do
    [ "$1" = "$miss" ] && return 1
  done
  return 0
}

# Run the `-c` script of a `docker run ... python -c <script>` under a real
# interpreter, so the password generator is tested and not imitated.
run_python() {
  while [ $# -gt 0 ]; do
    if [ "$1" = -c ]; then
      exec "${AOW_STUB_PYTHON:-python3}" -c "$2"
    fi
    shift
  done
  exit 0
}

case "$1" in
  info)
    [ "$2" = --format ] && echo "${AOW_STUB_MEM:-8589934592}"
    exit 0 ;;
  version) echo 29.8 ; exit 0 ;;
  image)
    # docker image inspect <ref>
    image_present "$3" ; exit $? ;;
  inspect)
    # docker inspect -f '{{.RestartCount}}' <cid>
    # AOW_STUB_INSPECT_FAILS=1 models the memoised container id having gone
    # stale -- Docker replaced the container, so the id no longer resolves and
    # inspect answers on stderr with exit 1. Also stands in for any transient
    # daemon error during the fifteen-minute wait.
    if [ "${AOW_STUB_INSPECT_FAILS:-0}" = 1 ]; then
      echo "Error: No such object: $4" >&2
      exit 1
    fi
    # AOW_STUB_RESTARTS_GROW=1 models a container that is crash-looping right
    # now: every reading is one higher than the last. Otherwise the count is
    # fixed, which models a container that restarted in the past and has been
    # stable since.
    if [ "${AOW_STUB_RESTARTS_GROW:-0}" = 1 ]; then
      n=$(cat "$AOW_STUB_CALLS.restarts" 2>/dev/null || echo "${AOW_STUB_RESTARTS:-0}")
      n=$((n + 1))
      echo "$n" > "$AOW_STUB_CALLS.restarts"
      echo "$n"
    else
      echo "${AOW_STUB_RESTARTS:-0}"
    fi
    exit 0 ;;
  pull)
    echo called > "$AOW_STUB_PULL_MARKER"
    exit 1 ;;
  run)
    log "run $*"
    [ "${AOW_STUB_RUN_FAILS:-0}" = 1 ] && { echo "docker: stub refuses to run" >&2 ; exit 125 ; }
    shift
    run_python "$@" ;;
  compose)
    log "$*"
    shift
    [ "$1" = --env-file ] && shift 2
    [ "$1" = -f ] && { STUB_TOOLS=1 ; shift 2 ; }

    if [ "$1" = version ]; then echo "${AOW_STUB_COMPOSE_VERSION:-2.40.3}" ; exit 0 ; fi

    if [ "$1" = config ] && [ "$2" = --images ]; then
      if [ "$STUB_TOOLS" = 1 ]; then
        if [ "$3" = stage ]; then printf 'python:3.12-slim@sha256:example\\n'
        else printf 'python:3.12-slim@sha256:example\\naow/demos:dev\\n' ; fi
      else
        printf 'aow/services:dev\\naow/ui:dev\\n'
      fi
      exit 0
    fi
    if [ "$1" = config ] && [ "$2" = --services ]; then
      printf '%s\\n' "${AOW_STUB_SERVICES:-migrate
api}"
      exit 0
    fi
    if [ "$1" = ps ]; then
      # AOW_STUB_PROJECT_EMPTY=1 models a Compose project that was never
      # started: every form of `ps` succeeds and prints nothing. That includes
      # `ps --all --format`, which is why the capability probe cannot catch
      # this case -- the template renders perfectly against no containers.
      if [ "${AOW_STUB_PROJECT_EMPTY:-0}" = 1 ]; then exit 0 ; fi
      if [ "$2" = -q ]; then echo "stub-cid-$3" ; exit 0 ; fi
      # `ps --all -q`: every container id in the project, used once to confirm
      # that an all-absent reading really is an empty project.
      if [ "$2" = --all ] && [ "$3" = -q ]; then echo "stub-cid-any" ; exit 0 ; fi
      if [ "$3" = --format ]; then
        if [ "${AOW_STUB_PS_FORMAT_FAILS:-0}" = 1 ]; then
          echo "unknown flag: --format" >&2
          exit 1
        fi
        if [ "${AOW_STUB_HEALTHY:-0}" = 1 ]; then
          stub_ps_default='migrate|exited||0
api|running|healthy|'
        else
          stub_ps_default='migrate|exited||0
api|exited||0'
        fi
        printf '%s\\n' "${AOW_STUB_PS:-$stub_ps_default}"
        exit 0
      fi
      exit 0
    fi
    # `run ... stage` is scripts/stage_model.py. Its two failures read very
    # differently and bootstrap treats them differently, so both are modelled
    # here closely enough to match on -- the text below is what a real run
    # printed on an air-gapped host and on a truncated model file.
    if [ "$STUB_TOOLS" = 1 ] && [ "$1" = run ]; then
      case " $* " in
        *' stage '*)
          if [ "${AOW_STUB_MODEL_MISSING:-0}" = 1 ]; then
            echo "models/Qwen3-1.7B-Q4_K_M.gguf is missing, fetching it from file:///dev/null/Qwen3-1.7B-Q4_K_M.gguf"
            echo "Traceback (most recent call last):" >&2
            echo "NotADirectoryError: [Errno 20] Not a directory" >&2
            exit 1
          fi
          if [ "${AOW_STUB_MODEL_CORRUPT:-0}" = 1 ]; then
            echo "models/Qwen3-1.7B-Q4_K_M.gguf does not match models.lock." >&2
            echo "  expected d2387ca2dbfee2ff" >&2
            echo "  actual   deadbeefdeadbeef" >&2
            exit 1
          fi
          echo "models/Qwen3-1.7B-Q4_K_M.gguf is already here, verifying it"
          echo "  OK  d2387ca2dbfee2ff"
          exit 0 ;;
      esac
    fi
    if [ "$STUB_TOOLS" = 1 ] && [ "$1" = run ] && [ "$3" = refresh ]; then
      exit ${AOW_STUB_REFRESH_RC:-0}
    fi
    ;;
esac
exit 0
"""


def bootstrap(
    tmp_path: Path,
    *args: str,
    env_file: Path | None = None,
    images_present: bool = False,
    extra_env: dict[str, str] | None = None,
    **stub: str,
):
    """Run bootstrap against the stub; keyword overrides set AOW_STUB_* values."""
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir(exist_ok=True)
    binary = stub_dir / "docker"
    binary.write_text(STUB_DOCKER, encoding="utf-8")
    binary.chmod(0o755)
    marker = tmp_path / "pull-called"
    calls = tmp_path / "compose-calls"
    env = {
        **os.environ,
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "AOW_ENV_FILE": str(env_file or tmp_path / "missing-env"),
        "AOW_STUB_PULL_MARKER": str(marker),
        "AOW_STUB_CALLS": str(calls),
        "AOW_STUB_IMAGES_PRESENT": "1" if images_present else "0",
        "AOW_STUB_PYTHON": sys.executable,
        "AOW_STUB_HEALTHY": "0",
        "DOCKER_HOST": "tcp://127.0.0.1:1",
        **(extra_env or {}),
        **{f"AOW_STUB_{k.upper()}": v for k, v in stub.items()},
    }
    # `timeout` is not belt and braces. Several tests here hand the script
    # `--timeout 900` on purpose, because the thing under test is that it fails
    # in step 6's first second instead of its last. If one of those fixes
    # regresses, an unbounded `subprocess.run` does not fail the test -- it
    # blocks CI for fifteen minutes and then fails it. A regression should cost
    # two minutes and say `TimeoutExpired`. Every test in this file finishes in
    # seconds; nothing legitimately approaches this.
    result = subprocess.run(
        [BASH, "scripts/bootstrap.sh", *args],
        cwd=REPO,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )
    return result, marker, calls


# --------------------------------------------------------------------------
# --offline must not touch a network
# --------------------------------------------------------------------------


def test_offline_bootstrap_does_not_pull_helper_for_disk_check(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, marker, calls = bootstrap(tmp_path, "--offline", "--no-start", env_file=env_file)
    assert result.returncode == 1
    assert "not staged on this machine" in result.stderr
    assert not marker.exists(), "--offline ran 'docker pull'"
    # The marker only catches `docker pull`. A `docker compose pull` is a
    # different code path and would reach the network just as surely, so the
    # recorded Compose calls are checked too.
    assert not PULL_CALL.search(calls.read_text()), "--offline ran 'docker compose pull'"


def test_offline_verifies_the_model_without_a_network(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, marker, calls = bootstrap(
        tmp_path, "--offline", "--no-start", env_file=env_file, images_present=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()
    stage = [
        c for c in calls.read_text().splitlines() if c.rstrip().endswith(" stage") and " run " in c
    ]
    assert stage, "the model was never verified"
    # What matters is that the stage run cannot fetch anything: it is pinned to
    # a local file URL and forbidden from pulling. Asserting those two
    # properties rather than the exact flag string keeps the test about the
    # requirement instead of the spelling.
    assert "--pull never" in stage[0]
    assert "MODEL_BASE_URL=file:///dev/null" in stage[0]


def test_offline_with_the_model_absent_says_so_in_one_line(tmp_path: Path) -> None:
    """No traceback, and no "fetching it from file:///dev/null".

    With the model absent the stager has nothing to verify and falls through to
    the fetch it was pinned away from: it announces a download from
    file:///dev/null and ends in a NotADirectoryError. Observed on a real
    air-gapped run as some twenty lines of noise between the reader and the one
    fact that mattered, which was that the model is not there.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, _, _ = bootstrap(
        tmp_path,
        "--offline",
        "--no-start",
        env_file=env_file,
        images_present=True,
        model_missing="1",
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "the model is not staged on this machine" in result.stderr
    assert "Traceback" not in combined
    assert "file:///dev/null" not in combined, "it repeated the pinned-away fetch URL"
    next_line = [ln for ln in result.stderr.splitlines() if ln.startswith("next: ")]
    assert next_line[0] == "next: rerun without --offline, on a machine with a network", next_line


def test_offline_with_a_corrupt_model_still_shows_both_hashes(tmp_path: Path) -> None:
    """The other failure, and the one whose own message is the better one.

    A present-but-wrong file makes stage_model.py print the path, the expected
    hash and the actual one. Nothing this script could write improves on that,
    so it is passed through rather than replaced.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, _, _ = bootstrap(
        tmp_path,
        "--offline",
        "--no-start",
        env_file=env_file,
        images_present=True,
        model_corrupt="1",
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "does not match models.lock" in combined
    assert "expected d2387ca2dbfee2ff" in combined
    assert "actual   deadbeefdeadbeef" in combined
    assert "delete the file named above" in result.stderr


def test_offline_prints_the_model_verification_it_performed(tmp_path: Path) -> None:
    """The success output is evidence and is not swallowed by the capture.

    bootstrap holds the stager's output so it can tell the absent case from the
    corrupt one. On success it has to print it anyway: "already here, verifying
    it" plus the hash is what an offline check exists to produce.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, _, _ = bootstrap(
        tmp_path, "--offline", "--no-start", env_file=env_file, images_present=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "is already here, verifying it" in result.stdout
    assert "d2387ca2dbfee2ff" in result.stdout


def test_offline_start_refuses_to_pull_or_build(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, _, calls = bootstrap(
        tmp_path, "--offline", "--timeout", "0", env_file=env_file, images_present=True
    )
    assert result.returncode == 1
    assert "up -d --no-build --pull never" in calls.read_text()
    assert "api(exit 0)" in result.stderr


def test_offline_starts_the_stack_when_only_the_proof_runner_is_missing(tmp_path: Path) -> None:
    """A reviewer who skipped `build demos` can still start the app offline.

    `aow/demos:dev` runs the proofs; nothing in `docker compose up` needs it.
    Treating it as fatal refused to start a startable stack and told an
    air-gapped reader to "rerun on a machine with a network".
    """
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, _, _ = bootstrap(
        tmp_path,
        "--offline",
        "--no-start",
        env_file=env_file,
        images_present=True,
        missing_images="aow/demos:dev",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "aow/demos:dev" in result.stdout
    assert "build demos" in result.stdout, "the warning should say how to get it"
    assert "not staged on this machine" not in result.stderr


def test_offline_still_refuses_when_an_image_the_stack_needs_is_absent(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, _, _ = bootstrap(
        tmp_path,
        "--offline",
        "--no-start",
        env_file=env_file,
        images_present=True,
        missing_images="aow/services:dev",
    )
    assert result.returncode == 1
    assert "aow/services:dev is not staged" in result.stderr


# --------------------------------------------------------------------------
# The health wait must not answer "healthy" for a service it cannot see
# --------------------------------------------------------------------------


def test_exited_service_is_not_reported_healthy(tmp_path: Path) -> None:
    result, _, _ = bootstrap(tmp_path, "--wait-only", "--timeout", "0")
    assert result.returncode == 1
    assert "api(exit 0)" in result.stderr
    assert "every service is healthy" not in result.stdout


def test_healthy_stack_is_reported_healthy(tmp_path: Path) -> None:
    """The success path, which nothing else here covers.

    Without this, a change that made the wait loop never succeed would pass
    every other test in this file.
    """
    result, _, _ = bootstrap(
        tmp_path,
        "--wait-only",
        services="migrate\napi\nconsumer",
        ps="migrate|exited||0\napi|running|healthy|\nconsumer|running||",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "every service is healthy" in result.stdout


def test_crash_looping_service_without_a_healthcheck_is_not_healthy(tmp_path: Path) -> None:
    """A crash loop must not read as success.

    `ingestor`, `consumer` and `enricher` declare no healthcheck and carry
    `restart: unless-stopped`. Docker's restart backoff starts at 100ms and
    this loop samples every 5s, so a consumer that cannot reach the broker is
    `running` at most sample instants. Reporting that stack healthy sends the
    reviewer to a UI backed by a database nothing is writing to.

    The timeout is long enough for the loop to sample twice, because a single
    instant cannot distinguish a crash loop from a steady state.
    """
    result, _, _ = bootstrap(
        tmp_path,
        "--wait-only",
        "--timeout",
        "8",
        services="consumer",
        ps="consumer|running||",
        restarts_grow="1",
    )
    assert result.returncode == 1
    assert "consumer(restarting)" in result.stderr
    assert "every service is healthy" not in result.stdout


def test_a_service_that_restarted_in_the_past_is_still_healthy(tmp_path: Path) -> None:
    """The counter is cumulative, so "count > 0" is the wrong test.

    `RestartCount` never resets for the life of a container. The no-data-loss
    drill stops and starts `consumer` four times by design and leaves it at
    RestartCount=6 while perfectly healthy -- observed on the live stack during
    this review. Gating on a non-zero count would make bootstrap hang on every
    later run on any machine where the proofs had been run, which is worse than
    the defect it fixes.
    """
    result, _, _ = bootstrap(
        tmp_path,
        "--wait-only",
        services="consumer",
        ps="consumer|running||",
        restarts="6",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "every service is healthy" in result.stdout
    assert "restarting" not in result.stdout


def test_an_unreadable_restart_count_does_not_kill_the_run(tmp_path: Path) -> None:
    """A failed `docker inspect` must answer 0, not end the script.

    The container id is looked up once per service and memoised for the whole
    wait, so Docker replacing that container mid-poll leaves a stale id and
    `docker inspect` answers "No such object" with exit 1. The read is a
    command substitution under `set -euo pipefail`, called as a plain statement
    rather than in a condition, so before the `|| true` the shell died right
    there -- with no "bootstrap failed:" line, no "next:" line and a bare exit
    1, which is the one thing this script promises never to do. The `case` that
    was supposed to answer 0 was unreachable.
    """
    result, _, _ = bootstrap(
        tmp_path,
        "--wait-only",
        "--timeout",
        "60",
        services="consumer",
        ps="consumer|running||",
        inspect_fails="1",
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "every service is healthy" in result.stdout
    # The point is not the verdict but that a verdict was reached at all: a
    # bare exit says nothing, and saying nothing is the failure being fixed.
    assert combined.strip(), "the run produced no output at all"


def test_a_restart_count_that_cannot_be_read_is_never_a_crash_loop(tmp_path: Path) -> None:
    """The other half: answering 0 must not invent a restart either.

    A measurement that cannot be taken has to be neutral in both directions --
    it may not hold a healthy service back, and it may not report one that is
    fine as restarting.
    """
    result, _, _ = bootstrap(
        tmp_path,
        "--wait-only",
        "--timeout",
        "60",
        services="consumer\nenricher",
        ps="consumer|running||\nenricher|running||",
        inspect_fails="1",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "restarting" not in result.stdout


def test_wait_only_on_a_project_that_was_never_started_fails_immediately(
    tmp_path: Path,
) -> None:
    """`--wait-only` polls a stack; it cannot start one.

    An empty Compose project answers every `ps` form with success and no
    output, so the capability probe passes -- the template renders perfectly
    against no containers. Every service then reads "(no container)" and the
    loop used to sit out the whole deadline, fifteen minutes by default, before
    failing with a `next:` line that told the reader to run `docker compose
    logs` on containers that do not exist.

    `--timeout 900` here is the point: this must fail in the first second.
    """
    result, _, _ = bootstrap(
        tmp_path,
        "--wait-only",
        "--timeout",
        "900",
        services="postgres\napi",
        project_empty="1",
    )
    assert result.returncode == 1
    assert "nothing to wait for" in result.stderr
    next_line = [ln for ln in result.stderr.splitlines() if ln.startswith("next: ")]
    assert next_line, result.stderr
    assert next_line[0] == "next: bash scripts/bootstrap.sh", next_line
    # It must not send the reader after logs for a container that is not there.
    assert "docker compose logs" not in result.stderr
    assert "still waiting after" not in result.stderr


def test_the_never_started_hint_keeps_the_offline_flag(tmp_path: Path) -> None:
    """An air-gapped reader cannot be told to run the connected path."""
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, _, _ = bootstrap(
        tmp_path,
        "--offline",
        "--wait-only",
        "--timeout",
        "900",
        env_file=env_file,
        services="postgres\napi",
        project_empty="1",
    )
    assert result.returncode == 1
    assert "next: bash scripts/bootstrap.sh --offline" in result.stderr


def test_a_partly_started_stack_is_still_waited_for(tmp_path: Path) -> None:
    """The fast failure must not swallow a stack that is genuinely coming up.

    Only an entirely empty project is hopeless. One service without a container
    yet, while others have one, is what a stack looks like a second after `up`,
    and it has to keep waiting.
    """
    result, _, _ = bootstrap(
        tmp_path,
        "--wait-only",
        "--timeout",
        "0",
        services="postgres\napi",
        ps="api|running|healthy|",
    )
    assert result.returncode == 1
    assert "nothing to wait for" not in result.stderr
    assert "still waiting after 0s for: postgres(no container)" in result.stderr


def test_unsupported_compose_ps_format_fails_fast_and_says_so(tmp_path: Path) -> None:
    """An old Compose must fail in step 6's first second, not its last.

    When `ps --format` is unsupported the output is empty, every service reads
    "(no container)", and the script waits out the whole timeout -- by default
    a 15-minute silent hang -- before blaming the containers. The stack is fine;
    the plugin is not.
    """
    result, _, _ = bootstrap(
        tmp_path,
        "--wait-only",
        "--timeout",
        "900",
        ps_format_fails="1",
        compose_version="2.20.0",
    )
    assert result.returncode == 1
    assert "cannot render" in result.stderr
    # "install a plugin that supports it", not "upgrade": the message quotes the
    # installed version in the same breath, and the dev machine's Compose is
    # v5.5.1, where "upgrade to v2.21 or newer" reads as an instruction to
    # downgrade.
    assert "install a Compose plugin that supports it (v2.21 or newer)" in result.stderr
    assert "upgrade" not in result.stderr
    assert "no container" not in result.stderr, "it blamed the containers instead of the plugin"


def test_timeout_message_offers_a_command_that_can_be_pasted(tmp_path: Path) -> None:
    """`next:` is an invitation to copy-paste, so it must be only a command.

    The timeout branch used to append " -- or keep waiting with '...'" to the
    command, producing a `next:` line that fails with "no such service: or".
    """
    result, _, _ = bootstrap(
        tmp_path,
        "--wait-only",
        "--timeout",
        "0",
        services="llm",
        ps="llm|running|starting|",
    )
    assert result.returncode == 1
    next_line = [ln for ln in result.stderr.splitlines() if ln.startswith("next: ")]
    assert next_line, result.stderr
    command = next_line[0][len("next: ") :]
    assert command == "docker compose logs --tail 50 llm", command
    # The "keep waiting" hint is still offered, just not welded onto the command.
    assert "--wait-only" in result.stderr


# --------------------------------------------------------------------------
# Step 2 is advisory and must not abort the run
# --------------------------------------------------------------------------


def test_unreadable_disk_measurement_warns_instead_of_aborting(tmp_path: Path) -> None:
    """The resources step is documented as "warnings, not gates".

    `docker run` can fail for reasons that have nothing to do with the stack --
    a daemon that refuses `--network none`, an image built for another
    platform, or a host so full that no container can be created. An
    unguarded command substitution under `set -e` turned that into a bare
    docker error with no "bootstrap failed:" line and no "next:" line.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, _, _ = bootstrap(
        tmp_path,
        "--offline",
        "--no-start",
        env_file=env_file,
        images_present=True,
        run_fails="1",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "could not read Docker's free disk" in result.stdout


# --------------------------------------------------------------------------
# Step 3: the passwords
# --------------------------------------------------------------------------


def test_generated_env_replaces_every_placeholder_and_keeps_the_template(
    tmp_path: Path,
) -> None:
    """The block that writes five passwords to disk, exercised for real.

    The generator runs under a real interpreter here, so this asserts the
    file a reviewer actually ends up with: no placeholder left, every secret
    distinct, and not one line of the template dropped -- a renderer that lost
    a setting would otherwise fail much later and much less clearly.
    """
    env_file = tmp_path / ".env"
    result, _, _ = bootstrap(
        tmp_path, "--offline", "--no-start", env_file=env_file, images_present=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert env_file.exists(), "no .env was created"

    template = (REPO / ".env.example").read_text(encoding="utf-8")
    written = env_file.read_text(encoding="utf-8")

    placeholders = [ln for ln in written.splitlines() if ln.endswith("=change-me")]
    assert not placeholders, placeholders
    expected = [ln for ln in template.splitlines() if ln.endswith("=change-me")]
    assert expected, "the template has no placeholders, so this proves nothing"

    assert len(written.splitlines()) == len(template.splitlines())

    secrets = [
        ln.split("=", 1)[1]
        for ln in written.splitlines()
        if ln.split("=", 1)[0] + "=change-me" in expected
    ]
    assert len(secrets) == len(expected)
    assert len(set(secrets)) == len(secrets), "two services were given the same password"
    assert all(len(s) >= 24 for s in secrets), secrets
    # Never printed: the whole point of generating them inside the container.
    for secret in secrets:
        assert secret not in result.stdout
        assert secret not in result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_generated_env_is_never_world_readable(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    result, _, _ = bootstrap(
        tmp_path, "--offline", "--no-start", env_file=env_file, images_present=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert env_file.stat().st_mode & 0o077 == 0, oct(env_file.stat().st_mode)


def test_existing_env_with_a_placeholder_is_refused_not_overwritten(tmp_path: Path) -> None:
    """The one way a rerun goes wrong, and the file must survive it."""
    env_file = tmp_path / ".env"
    original = "POSTGRES_PASSWORD=real\nRABBITMQ_PASSWORD=change-me\n"
    env_file.write_text(original, encoding="utf-8")
    result, _, _ = bootstrap(
        tmp_path, "--offline", "--no-start", env_file=env_file, images_present=True
    )
    assert result.returncode == 1
    assert "placeholder password" in result.stderr
    assert env_file.read_text(encoding="utf-8") == original, "the reviewer's .env was altered"


def test_existing_env_is_left_byte_for_byte_alone(tmp_path: Path) -> None:
    """ "Safe to run twice" is the script's headline claim; this is it."""
    env_file = tmp_path / ".env"
    original = "POSTGRES_PASSWORD=kept\n# a comment mentioning change-me in prose\n"
    env_file.write_text(original, encoding="utf-8")
    result, _, _ = bootstrap(
        tmp_path, "--offline", "--no-start", env_file=env_file, images_present=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert env_file.read_text(encoding="utf-8") == original
    assert "no change-me placeholders left" in result.stdout


# --------------------------------------------------------------------------
# Step 4: the pull is skipped only when it is genuinely unnecessary
# --------------------------------------------------------------------------


def test_connected_run_skips_the_pull_when_the_pinned_images_are_present(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, _, calls = bootstrap(tmp_path, "--no-start", env_file=env_file, images_present=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "skipping the pull" in result.stdout
    assert not PULL_CALL.search(calls.read_text())


def test_connected_run_pulls_when_an_image_is_missing(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, _, calls = bootstrap(
        tmp_path,
        "--no-start",
        env_file=env_file,
        images_present=True,
        missing_images="aow/services:dev",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert PULL_CALL.search(calls.read_text())
    assert "skipping the pull" not in result.stdout


# --------------------------------------------------------------------------
# Usage
# --------------------------------------------------------------------------


def test_timeout_without_value_is_usage_error(tmp_path: Path) -> None:
    result, _, _ = bootstrap(tmp_path, "--timeout")
    assert result.returncode == 2
    assert "--timeout takes a number of seconds" in result.stderr


def test_timeout_with_a_non_numeric_value_is_usage_error(tmp_path: Path) -> None:
    result, _, _ = bootstrap(tmp_path, "--timeout", "later")
    assert result.returncode == 2
    assert "--timeout takes a number of seconds" in result.stderr


def test_unknown_option_is_a_usage_error_that_prints_the_usage(tmp_path: Path) -> None:
    result, _, _ = bootstrap(tmp_path, "--bogus")
    assert result.returncode == 2
    assert "unknown option: --bogus" in result.stderr
    assert "bash scripts/bootstrap.sh [options]" in result.stderr


def test_refresh_and_offline_are_rejected_together(tmp_path: Path) -> None:
    # --refresh needs a network and --offline promises there is none. Caught at
    # parse time rather than fifteen minutes later, at the point where the fetch
    # would fail for a reason the flags already made inevitable.
    result, _, _ = bootstrap(tmp_path, "--refresh", "--offline")
    assert result.returncode == 2
    assert "pick one" in result.stderr


def test_refresh_and_no_start_are_rejected_together(tmp_path: Path) -> None:
    # scripts/refresh.sh acts on a running stack.
    result, _, _ = bootstrap(tmp_path, "--refresh", "--no-start")
    assert result.returncode == 2
    assert "running stack" in result.stderr


def test_wait_only_and_no_start_are_rejected_together(tmp_path: Path) -> None:
    # --no-start's early exit sits inside the same block --wait-only skips, so
    # given both flags the script used to ignore --no-start silently and poll
    # for the full timeout -- the opposite of what one of the two asked for.
    result, _, _ = bootstrap(tmp_path, "--wait-only", "--no-start")
    assert result.returncode == 2
    assert "pick one" in result.stderr


# Four tests stood here that read `scripts/bootstrap.sh` as text and asserted
# that certain strings appeared in it, or that one `str.index()` was smaller
# than another. They have been removed rather than kept as a second opinion:
# a source grep passes just as happily on a branch nothing can reach, which is
# the one failure worth catching, and each was already covered by a sibling
# below that drives the real script and reads its real output.
#
#   test_refresh_runs_only_after_the_health_gate
#       -> test_a_stack_that_never_goes_healthy_does_not_reach_the_refresh
#          (no refresh call is recorded when the gate fails) and
#          test_a_successful_refresh_says_the_forecast_is_fresh
#          (the report reflects the refresh, so it ran before it)
#   test_the_report_never_calls_snapshot_data_fresh
#       -> one test per provenance branch: ..._says_the_forecast_is_fresh (ok),
#          ..._is_visible_and_not_dressed_up (failed), and
#          ..._says_the_data_was_not_fetched (not-attempted), below
#   test_the_egress_window_failure_is_louder_than_a_failed_fetch
#       -> test_an_unclosed_egress_window_is_reported_louder_than_a_stale_forecast
#   test_a_failed_refresh_exits_non_zero
#       -> both failure tests assert `returncode == 2` alongside the report
#          text, which also pins the exit as coming after the report


def test_a_run_without_refresh_says_the_data_was_not_fetched(tmp_path: Path) -> None:
    # The common case, and the one most likely to mislead: a reviewer who
    # cannot tell which rows were fetched today and which shipped with the
    # clone cannot judge any answer the system gives.
    #
    # It has to reach the report to prove anything about it. This ran with
    # `--wait-only --timeout 0` against a stub whose default `ps` leaves `api`
    # exited, so it died at the health gate and the report was never printed --
    # and then asserted that a word the script emits on no path was absent,
    # which is true of any output at all, including none.
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, _, _ = bootstrap(
        tmp_path,
        env_file=env_file,
        images_present=True,
        extra_env={"AOW_STUB_HEALTHY": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "the committed snapshot, as cloned" in result.stdout
    assert "--refresh on a connected machine" in result.stdout
    assert "fetched just now" not in result.stdout


def test_help_states_what_refresh_does_not_update(tmp_path: Path) -> None:
    # tmp_path, not Path("."): the helper writes its docker stub under whatever
    # it is given, so passing the repo root litters the working tree with a
    # stub/ directory -- which is exactly how one got committed once.
    result, _, _ = bootstrap(tmp_path, "--help")
    assert result.returncode == 0
    assert "--refresh" in result.stdout
    assert "places, city facts and events" in result.stdout.replace("\n", " ").replace(
        "              ", " "
    )
    assert "make snapshot" in result.stdout


def _refreshed(tmp_path: Path, rc: str):
    """A healthy stub stack whose refresh exits with `rc`."""
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    return bootstrap(
        tmp_path,
        "--refresh",
        env_file=env_file,
        images_present=True,
        extra_env={"AOW_STUB_HEALTHY": "1", "AOW_STUB_REFRESH_RC": rc},
    )


def test_a_successful_refresh_says_the_forecast_is_fresh(tmp_path: Path) -> None:
    result, _, calls = _refreshed(tmp_path, "0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "-f compose.tools.yml run --rm refresh" in calls.read_text()
    assert "fetched just now" in result.stdout
    # Even on success, the limits are stated.
    assert "never auto-fetched" in result.stdout


def test_a_failed_fetch_is_visible_and_not_dressed_up(tmp_path: Path) -> None:
    # refresh.sh exit 2: the fetch failed for at least one city.
    result, _, _ = _refreshed(tmp_path, "2")
    combined = result.stdout + result.stderr
    assert result.returncode == 3, combined
    assert "THE REFRESH DID NOT COMPLETE" in combined
    assert "Some cities may have" in combined
    assert "advanced while others retain older forecasts" in combined
    assert "fetched just now" not in combined


def test_an_unclosed_egress_window_is_reported_louder_than_a_stale_forecast(
    tmp_path: Path,
) -> None:
    # refresh.sh exit 3 is the one outcome worse than stale data: the ingestor
    # may still reach the internet, which is the property the whole design is
    # built to deny.
    result, _, _ = _refreshed(tmp_path, "3")
    combined = result.stdout + result.stderr
    assert result.returncode == 3, combined
    assert "EGRESS WINDOW STILL OPEN" in combined
    assert "docker network rm" in combined


def test_a_stack_that_never_goes_healthy_does_not_reach_the_refresh(
    tmp_path: Path,
) -> None:
    # A refresh against a half-started stack fails in ways that look like a
    # network fault, so the health gate must come first in practice and not
    # only in the source order.
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, _, calls = bootstrap(
        tmp_path,
        "--refresh",
        "--timeout",
        "0",
        env_file=env_file,
        images_present=True,
        extra_env={"AOW_STUB_HEALTHY": "0"},
    )
    assert result.returncode == 1
    assert "run --rm refresh" not in calls.read_text()


def test_the_refresh_targets_the_project_bootstrap_is_driving(tmp_path: Path) -> None:
    # compose.tools.yml defaults the refresh container's COMPOSE_PROJECT_NAME
    # from AOW_PROJECT, which falls back to `aow` -- not to the project this
    # script is driving. Isolating a second copy with COMPOSE_PROJECT_NAME
    # alone, which is the documented way to run one, would otherwise have the
    # fetch open an egress window on the *other* stack and refresh that one.
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, _, _ = bootstrap(
        tmp_path,
        "--refresh",
        env_file=env_file,
        images_present=True,
        extra_env={
            "AOW_STUB_HEALTHY": "1",
            "AOW_STUB_REFRESH_RC": "0",
            "COMPOSE_PROJECT_NAME": "aow-second-copy",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "refreshing the 'aow-second-copy' project" in result.stdout


def test_an_explicit_aow_project_still_wins(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, _, _ = bootstrap(
        tmp_path,
        "--refresh",
        env_file=env_file,
        images_present=True,
        extra_env={
            "AOW_STUB_HEALTHY": "1",
            "AOW_STUB_REFRESH_RC": "0",
            "COMPOSE_PROJECT_NAME": "aow-second-copy",
            "AOW_PROJECT": "chosen-explicitly",
        },
    )
    assert "refreshing the 'chosen-explicitly' project" in result.stdout


def test_the_printed_urls_follow_the_bind_address(tmp_path: Path) -> None:
    # Found by a real isolated run: the report hardcoded localhost and claimed
    # "published on 127.0.0.1 only" while the stack was actually on 127.0.0.3.
    # The isolated second copy is exactly the case where the reader is least
    # able to guess the right address.
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, _, _ = bootstrap(
        tmp_path,
        env_file=env_file,
        images_present=True,
        extra_env={"AOW_STUB_HEALTHY": "1", "AOW_BIND_ADDR": "127.0.0.3"},
    )
    assert "http://127.0.0.3:8080" in result.stdout
    assert "http://127.0.0.3:8000/openapi.json" in result.stdout
    assert "localhost:8080" not in result.stdout
    assert "published on 127.0.0.3 only" in result.stdout


def test_the_bind_address_is_read_from_the_env_file_too(tmp_path: Path) -> None:
    """The documented knob is a line in .env, and it was the one not read.

    compose.yml publishes on ${AOW_BIND_ADDR:-127.0.0.1} and .env.example ships
    AOW_BIND_ADDR as a line to edit, so `.env` -- not the shell -- is how a host
    moves that boundary. Reading only the shell printed "published on 127.0.0.1
    only" over a stack Compose had just bound to 0.0.0.0. Of every line in this
    report that is the one that must not be able to be wrong: it describes the
    exposure of an API with write routes and no authentication.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\nAOW_BIND_ADDR=0.0.0.0\n", encoding="utf-8")
    result, _, _ = bootstrap(
        tmp_path,
        env_file=env_file,
        images_present=True,
        extra_env={"AOW_STUB_HEALTHY": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "published on 0.0.0.0" in result.stdout
    assert "can expose the unauthenticated API to other machines" in result.stdout
    assert "published on 127.0.0.1 only" not in result.stdout


def test_the_shell_wins_over_the_env_file_for_the_bind_address(tmp_path: Path) -> None:
    """Compose's own precedence, so the report cannot disagree with the stack.

    For interpolation the shell environment outranks --env-file, and the report
    has to resolve the value the same way round or it would be wrong in the
    other direction.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\nAOW_BIND_ADDR=0.0.0.0\n", encoding="utf-8")
    result, _, _ = bootstrap(
        tmp_path,
        env_file=env_file,
        images_present=True,
        extra_env={"AOW_STUB_HEALTHY": "1", "AOW_BIND_ADDR": "127.0.0.4"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "http://127.0.0.4:8080" in result.stdout
    assert "published on 127.0.0.4 only" in result.stdout
    assert "0.0.0.0" not in result.stdout, "the env file overrode the shell"


def test_the_api_url_printed_is_the_one_that_works_without_a_network(
    tmp_path: Path,
) -> None:
    """/docs is the only page in this stack that is not self-contained.

    Swagger UI's bundle comes from a CDN and its icon from another host, so on
    the air-gapped machine this project exists for, /docs renders empty.
    /openapi.json is served by the api container and needs nothing, so that is
    the URL the report offers -- while still saying what /docs is for.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, _, _ = bootstrap(
        tmp_path,
        env_file=env_file,
        images_present=True,
        extra_env={"AOW_STUB_HEALTHY": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "http://localhost:8000/openapi.json" in result.stdout
    assert "needs a network" in result.stdout


def test_the_default_run_still_says_localhost(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, _, _ = bootstrap(
        tmp_path,
        env_file=env_file,
        images_present=True,
        extra_env={"AOW_STUB_HEALTHY": "1"},
    )
    assert "http://localhost:8080" in result.stdout
    assert "resolves localhost to ::1" in result.stdout


def test_nonloopback_bind_report_does_not_claim_the_api_is_local_only(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, _, _ = bootstrap(
        tmp_path,
        env_file=env_file,
        images_present=True,
        extra_env={"AOW_STUB_HEALTHY": "1", "AOW_BIND_ADDR": "0.0.0.0"},
    )
    assert result.returncode == 0
    assert "can expose the unauthenticated API to other machines" in result.stdout
    assert "never on a routable interface" not in result.stdout
    # A wildcard is what a socket binds, not somewhere a browser can go, so the
    # URL says localhost. The warning above still carries the real value.
    assert "http://localhost:8080" in result.stdout
    assert "http://0.0.0.0:8080" not in result.stdout
    assert "published on 0.0.0.0" in result.stdout
