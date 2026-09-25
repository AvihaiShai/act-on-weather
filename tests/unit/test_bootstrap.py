"""Bootstrap must honor offline mode and report failed services accurately."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_bundle_tamper import BASH

REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(BASH is None, reason="no POSIX shell with coreutils")

STUB_DOCKER = """#!/bin/sh
case "$1" in
  info)
    if [ "$2" = --format ]; then echo 8589934592; fi
    ;;
  version) echo 29.8 ;;
  image) [ "$AOW_STUB_IMAGES_PRESENT" = 1 ]; exit $? ;;
  pull)
    echo called > "$AOW_STUB_PULL_MARKER"
    exit 1
    ;;
  compose)
    echo "$*" >> "$AOW_STUB_CALLS"
    shift
    if [ "$1" = --env-file ]; then shift 2; fi
    if [ "$1" = version ]; then echo 2.39.0; exit 0; fi
    if [ "$1" = config ] && [ "$2" = --images ]; then
      printf 'aow/services:dev\\naow/ui:dev\\n'
      exit 0
    fi
    if [ "$1" = -f ] && [ "$3" = config ] && [ "$4" = --images ]; then
      printf 'python:3.12-slim@sha256:example\\naow/demos:dev\\n'
      exit 0
    fi
    if [ "$1" = config ] && [ "$2" = --services ]; then
      printf 'migrate\\napi\\n'
      exit 0
    fi
    if [ "$1" = ps ]; then
      if [ "$AOW_STUB_HEALTHY" = 1 ]; then
        printf 'migrate|exited||0\\napi|running|healthy|\\n'
      else
        printf 'migrate|exited||0\\napi|exited||0\\n'
      fi
      exit 0
    fi
    if [ "$1" = -f ] && [ "$3" = run ] && [ "$5" = refresh ]; then
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
):
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "docker"
    stub.write_text(STUB_DOCKER, encoding="utf-8")
    stub.chmod(0o755)
    marker = tmp_path / "pull-called"
    calls = tmp_path / "compose-calls"
    env = {
        **os.environ,
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "AOW_ENV_FILE": str(env_file or tmp_path / "missing-env"),
        "AOW_STUB_PULL_MARKER": str(marker),
        "AOW_STUB_CALLS": str(calls),
        "AOW_STUB_IMAGES_PRESENT": "1" if images_present else "0",
        "AOW_STUB_HEALTHY": "0",
        "DOCKER_HOST": "tcp://127.0.0.1:1",
        **(extra_env or {}),
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


def test_offline_bootstrap_does_not_pull_helper_for_disk_check(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, marker, _ = bootstrap(tmp_path, "--offline", "--no-start", env_file=env_file)
    assert result.returncode == 1
    assert "not staged on this machine" in result.stderr
    assert not marker.exists(), "--offline attempted a registry pull"


def test_offline_bootstrap_uses_local_images_and_model_source(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, marker, calls = bootstrap(
        tmp_path, "--offline", "--no-start", env_file=env_file, images_present=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()
    assert "--pull never --no-deps -e MODEL_BASE_URL=file:///dev/null stage" in calls.read_text()


def test_offline_start_disallows_pull_and_build(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
    result, _, calls = bootstrap(
        tmp_path, "--offline", "--timeout", "0", env_file=env_file, images_present=True
    )
    assert "up -d --no-build --pull never" in calls.read_text()
    assert "api(exit 0)" in result.stderr


def test_exited_service_is_not_reported_healthy(tmp_path: Path) -> None:
    result, _, _ = bootstrap(tmp_path, "--wait-only", "--timeout", "0")
    assert result.returncode == 1
    assert "api(exit 0)" in result.stderr
    assert "every service is healthy" not in result.stdout


def test_timeout_without_value_is_usage_error(tmp_path: Path) -> None:
    result, _, _ = bootstrap(tmp_path, "--timeout")
    assert result.returncode == 2
    assert "--timeout takes a number of seconds" in result.stderr


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


def test_help_states_what_refresh_does_not_update() -> None:
    result, _, _ = bootstrap(Path("."), "--help")
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
