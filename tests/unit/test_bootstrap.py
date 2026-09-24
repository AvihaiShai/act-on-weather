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
      printf 'migrate|exited||0\\napi|exited||0\\n'
      exit 0
    fi
    ;;
esac
exit 0
"""


def bootstrap(
    tmp_path: Path, *args: str, env_file: Path | None = None, images_present: bool = False
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
        "DOCKER_HOST": "tcp://127.0.0.1:1",
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
