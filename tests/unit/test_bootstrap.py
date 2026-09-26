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
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.test_bundle_tamper import BASH

REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(BASH is None, reason="no POSIX shell with coreutils")

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
      if [ "$2" = -q ]; then echo "stub-cid-$3" ; exit 0 ; fi
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
    result = subprocess.run(
        [BASH, "scripts/bootstrap.sh", *args],
        cwd=REPO,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        env=env,
        check=False,
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
    assert " pull" not in calls.read_text(), "--offline ran 'docker compose pull'"


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
    assert "upgrade the Compose v2 plugin" in result.stderr
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
    assert " pull " not in calls.read_text()


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
    assert "pull" in calls.read_text()
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


def test_refresh_runs_only_after_the_health_gate(tmp_path: Path) -> None:
    # A refresh against a half-started stack fails in ways that look like a
    # network fault, so the order matters and is worth pinning.
    source = (REPO / "scripts" / "bootstrap.sh").read_text(encoding="utf-8")
    health_at = source.index('step "6/6  Waiting for the stack')
    refresh_at = source.index('step "Refresh  Fetching a fresh forecast"')
    ready_at = source.index('step "Ready"')
    assert health_at < refresh_at < ready_at


def test_a_run_without_refresh_says_the_data_was_not_fetched(tmp_path: Path) -> None:
    # The common case, and the one most likely to mislead: a reviewer who
    # cannot tell which rows were fetched today and which shipped with the
    # clone cannot judge any answer the system gives.
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, _, _ = bootstrap(
        tmp_path, "--offline", "--wait-only", "--timeout", "0", env_file=env_file
    )
    combined = result.stdout + result.stderr
    assert "freshly" not in combined.lower()


def test_the_report_never_calls_snapshot_data_fresh() -> None:
    # Three provenance branches, and the failure branch must not be reachable
    # without saying the fetch failed. Asserted on the source because the
    # failure path needs a real stack to reach at runtime.
    source = (REPO / "scripts" / "bootstrap.sh").read_text(encoding="utf-8")
    assert "REFRESH_RESULT=not-attempted" in source
    assert "REFRESH_RESULT=ok" in source
    assert "REFRESH_RESULT=failed" in source
    assert "THE FETCH FAILED" in source
    assert "the committed snapshot, as cloned" in source


def test_the_egress_window_failure_is_louder_than_a_failed_fetch() -> None:
    # refresh.sh exit 3 means the window would not close, which is worse than a
    # stale forecast: the ingestor may still reach the internet. It must not be
    # folded into the generic failure branch.
    source = (REPO / "scripts" / "bootstrap.sh").read_text(encoding="utf-8")
    assert "EGRESS WINDOW STILL OPEN" in source
    assert "docker network rm" in source


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


def test_a_failed_refresh_exits_non_zero() -> None:
    # The stack is up either way and the URLs work either way, so the report is
    # the honest signal for a human -- but a caller that only checks the status
    # has to be able to tell that the run did not do what it was asked to do.
    source = (REPO / "scripts" / "bootstrap.sh").read_text(encoding="utf-8")
    tail = source[source.index('step "Ready"') :]
    assert 'if [ "$REFRESH_RESULT" = failed ]; then' in tail
    assert "exit 2" in tail


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
    assert result.returncode == 2, combined
    assert "THE FETCH FAILED" in combined
    assert "not fresh data" in combined
    assert "fetched just now" not in combined


def test_an_unclosed_egress_window_is_reported_louder_than_a_stale_forecast(
    tmp_path: Path,
) -> None:
    # refresh.sh exit 3 is the one outcome worse than stale data: the ingestor
    # may still reach the internet, which is the property the whole design is
    # built to deny.
    result, _, _ = _refreshed(tmp_path, "3")
    combined = result.stdout + result.stderr
    assert result.returncode == 2, combined
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
    assert "http://127.0.0.3:8000/docs" in result.stdout
    assert "localhost:8080" not in result.stdout
    assert "published on 127.0.0.3 only" in result.stdout


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
