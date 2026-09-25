"""Tests for scripts/airgap-evidence.sh, the capture side of the F10 proof.

This script exists because docs/RELEASE-PROOF.md's evidence checklist asked for
nine artifacts and named a producing command for three of them. The rest were
hand-typed, which is how "0 pull attempts" became a sentence in a document
rather than a number from a run.

That makes the script a measuring instrument, and the failure mode that matters
is not a crash -- it is a confident zero. A capture that reports "no default
route" because `ip` is missing, or "0 pulls" because the event watcher never
attached, would support exactly the claim it cannot see. So these tests are
mostly about what the script says when it does not know.

The Docker-dependent subcommands are not exercised here; `host` and the pull
counter need a daemon, and those are measured during the drill itself and
recorded in docs/EVIDENCE-physical-airgap.md.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = "scripts/airgap-evidence.sh"


def _find_shell() -> str | None:
    """A shell that can run the bundle scripts, or None to skip.

    Same rule as tests/unit/test_bundle_tamper.py: on Windows the `bash` on
    PATH is often the WSL launcher in System32, which cannot execute anything
    in this checkout.
    """
    override = os.environ.get("AOW_TEST_BASH")
    if override:
        return override
    for candidate in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe"):
        if candidate and "system32" not in candidate.lower() and Path(candidate).exists():
            return candidate
    return None


BASH = _find_shell()
pytestmark = pytest.mark.skipif(BASH is None, reason="no usable bash on PATH")


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(REPO / SCRIPT), *args],
        cwd=cwd or REPO,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        check=False,
    )


def test_the_script_is_syntactically_valid() -> None:
    result = subprocess.run(
        [BASH, "-n", str(REPO / SCRIPT)],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_no_subcommand_prints_usage_and_fails() -> None:
    result = run()
    assert result.returncode == 2
    assert "airgap-evidence.sh host" in result.stderr


def test_an_unknown_subcommand_fails_rather_than_capturing_nothing() -> None:
    # A typo that exited 0 with no output would leave the operator holding an
    # empty evidence file and believing it was captured.
    assert run("hosts").returncode == 2


def test_bundle_refuses_a_directory_that_is_not_a_release(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("not a bundle\n", encoding="utf-8")
    result = run("bundle", str(tmp_path))
    assert result.returncode != 0
    assert "not an offline release folder" in result.stdout + result.stderr


def test_bundle_reports_the_digests_sha256sum_c_never_prints(tmp_path: Path) -> None:
    # `sha256sum -c` prints "images.tar: OK" and never the digest itself, so an
    # operator asked to compare the archive on the medium against the archive on
    # the target has nothing on either side to compare. These are those numbers.
    (tmp_path / "images.tar").write_bytes(b"pretend archive")
    (tmp_path / "release-version.txt").write_text("a" * 40 + "\n", encoding="utf-8")
    (tmp_path / "SHA256SUMS").write_text("", encoding="utf-8")

    result = run("bundle", str(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr

    archive = hashlib.sha256(b"pretend archive").hexdigest()
    sums = hashlib.sha256(b"").hexdigest()
    assert f"sha256(images.tar): {archive}" in result.stdout
    assert f"sha256(SHA256SUMS): {sums}" in result.stdout
    assert "bytes(images.tar): 15" in result.stdout
    assert f"release_version: {'a' * 40}" in result.stdout


def test_bundle_says_an_install_will_refuse_without_env(tmp_path: Path) -> None:
    # install-offline.sh's first hard stop is a missing .env, and the physical
    # procedure used to jump straight from verify to install without mentioning
    # it. The capture names the problem before the operator is standing at a
    # disconnected machine with no way to look it up.
    (tmp_path / "images.tar").write_bytes(b"x")
    (tmp_path / "release-version.txt").write_text("b" * 40 + "\n", encoding="utf-8")
    (tmp_path / "SHA256SUMS").write_text("", encoding="utf-8")

    result = run("bundle", str(tmp_path))
    assert "has_env: no -- install-offline.sh will refuse" in result.stdout

    (tmp_path / ".env").write_text("POSTGRES_PASSWORD=x\n", encoding="utf-8")
    assert "has_env: yes" in run("bundle", str(tmp_path)).stdout


def test_run_propagates_the_measured_command_exit_code() -> None:
    # The exit code is checklist item 7. A wrapper that swallowed it would make
    # every drill look like a pass.
    result = run("run", "--", "sh", "-c", "exit 7")
    assert result.returncode == 7
    assert "exit_code: 7" in result.stdout


def test_run_measures_a_transcript_and_elapsed_time() -> None:
    result = run("run", "--", "sh", "-c", "echo one; echo two")
    assert result.returncode == 0
    assert "transcript_lines: 2" in result.stdout
    assert "elapsed_seconds: " in result.stdout
    assert "started_at: " in result.stdout


def test_run_counts_pull_and_build_markers_in_the_transcript() -> None:
    # The daemon emits no event for a pull that failed, so on a host with no
    # network the event count cannot see the attempt -- only the text the tools
    # print on their way to trying it. This is that second reading, and a run
    # where the two disagree is the signature the script tells the reader to
    # look for.
    quiet = run("run", "--", "sh", "-c", "echo nothing to see")
    assert "transcript_pull_or_build_markers: 0" in quiet.stdout

    noisy = run("run", "--", "sh", "-c", 'echo " Pulling 3/5"; echo "Pulling from library/x"')
    assert "transcript_pull_or_build_markers: 2" in noisy.stdout


def test_run_states_how_a_clean_offline_result_reads() -> None:
    # Whoever reads the evidence months later is not the person who ran it.
    result = run("run", "--", "true")
    assert "A clean offline run reads: exit_code 0, completed_image_pulls 0," in result.stdout


def test_the_capture_never_asserts_a_negative_from_a_missing_tool() -> None:
    # The claim this script supports is "this host had no route out". Reporting
    # 0 default routes because `ip` is not installed would assert exactly that
    # from the absence of the tool that checks it. On this developer machine
    # `ip` genuinely is absent, which is why the case is testable here at all.
    source = (REPO / SCRIPT).read_text(encoding="utf-8")
    assert "default_routes: UNKNOWN" in source
    assert "ip is not installed" in source
