"""The operational backup and restore of live state (F8).

Three things are worth testing offline, and they are the three things that
would otherwise only be discovered during a real restore:

  * the online SQLite copy, against a real WAL database with a live writer
    still holding it open -- the case where `cp` quietly produces a file that
    is missing every commit since the last checkpoint;
  * the manifest, because a restore refuses to run when it does not validate,
    so a manifest that wrongly validates is a restore that proceeds from a
    corrupt dump;
  * the two shell entry points, for syntax and for the presence of the two
    guards that make the restore safe to hand to an operator.

Nothing here starts a container. The unit suite runs with `--network none` and
has no Docker socket, so the end-to-end proof is demos/06_backup_restore.sh,
not this file. What this file can do is make sure the pieces that drill relies
on are correct before the drill spends four minutes finding out.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from services.common.envelope import Envelope
from services.common.outbox import Outbox

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    """Import one of the backup helpers by path.

    `scripts/` is not a package -- it is a directory of standalone tools that
    run inside containers -- so the other tests here run them as subprocesses.
    These two are libraries as well as tools, and the functions are the point,
    so they are loaded directly rather than exercised only through a CLI.
    """
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"aow_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


sqlite_backup = _load("sqlite_backup")
backup_manifest = _load("backup_manifest")


# ------------------------------------------------------- the online copy ----


def _envelope(label: str) -> Envelope:
    return Envelope.create(
        "recommendation.request",
        {"city_id": "rome", "activity": label},
        source="api",
        observed_at="2026-09-24T00:00:00+00:00",
        city="rome",
    )


def test_online_backup_of_a_live_wal_outbox(tmp_path: Path) -> None:
    """The case `cp` gets wrong: a WAL database with uncheckpointed commits.

    The source connection stays open for the whole test, which is what makes
    this a live database rather than a closed one -- the rows accepted below
    are committed but are still sitting in outbox.sqlite3-wal, so a copy of
    outbox.sqlite3 alone would not contain them.
    """
    source = tmp_path / "outbox.sqlite3"
    box = Outbox(source)
    ids = [box.accept(_envelope(f"activity {n}")) for n in range(25)]
    box.mark_published(1)
    assert (tmp_path / "outbox.sqlite3-wal").exists(), "the fixture is not in WAL mode"

    dest = tmp_path / "copy.sqlite3"
    report = sqlite_backup.online_backup(source, dest)

    assert report["integrity"] == "ok"
    assert report["source_rows"] == 25
    assert report["copied_rows"] == 25
    assert report["copied_pending"] == 24
    assert report["bytes"] == dest.stat().st_size > 0

    # The copy is a usable outbox in its own right: every message_id is there,
    # and the published/unpublished split survived.
    copied = Outbox(dest, readonly=True)
    try:
        assert [row["message_id"] for row in copied.published_after(0)] == [ids[0]]
        assert copied.counts() == {"total": 25, "pending": 24}
        for message_id in ids:
            assert copied.status_of(message_id) is not None
    finally:
        copied.close()

    # And the source is untouched and still writable, which is the property
    # that lets this run against a producer that is serving traffic.
    box.accept(_envelope("accepted after the backup"))
    assert box.counts() == {"total": 26, "pending": 25}
    box.close()


def test_online_backup_leaves_no_stale_wal_beside_the_copy(tmp_path: Path) -> None:
    """A second backup over the same destination must not merge two databases.

    A leftover `-wal` from an earlier copy belongs to a different database; if
    it survived into the next backup SQLite would fold it in and the artefact
    would be part of one backup and part of another.
    """
    source = tmp_path / "outbox.sqlite3"
    box = Outbox(source)
    box.accept(_envelope("first"))
    dest = tmp_path / "copy.sqlite3"
    sqlite_backup.online_backup(source, dest)
    Path(f"{dest}-wal").write_bytes(b"not a write-ahead log")

    box.accept(_envelope("second"))
    report = sqlite_backup.online_backup(source, dest)

    assert report["copied_rows"] == 2
    assert not Path(f"{dest}-wal").exists() or Path(f"{dest}-wal").read_bytes() != (
        b"not a write-ahead log"
    )
    box.close()


def test_online_backup_refuses_a_missing_source(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        sqlite_backup.online_backup(tmp_path / "nothing.sqlite3", tmp_path / "copy.sqlite3")


def test_online_backup_raises_when_the_copy_fails_integrity_check(
    tmp_path: Path, monkeypatch
) -> None:
    """integrity_check is not decoration: a copy that fails it must raise.

    The verdict is faked rather than manufactured by corrupting bytes, because
    a deliberately corrupted SQLite file is not reproducible across versions --
    but the branch it guards is the one that decides whether a bad artefact is
    written and then trusted for six weeks.
    """
    source = tmp_path / "outbox.sqlite3"
    box = Outbox(source)
    box.accept(_envelope("one"))
    box.close()

    monkeypatch.setattr(
        sqlite_backup, "_integrity", lambda conn: "*** in database main *** Page 3 is never used"
    )
    with pytest.raises(RuntimeError, match="integrity_check"):
        sqlite_backup.online_backup(source, tmp_path / "copy.sqlite3")


def test_online_backup_raises_when_the_copy_lost_rows(tmp_path: Path, monkeypatch) -> None:
    """The other half of the same guard. Rows are never deleted from an outbox,
    so a copy holding fewer rows than the source had when the copy started can
    only mean the copy is wrong -- and a short outbox is precisely the artefact
    whose damage would not be noticed until a replay came up empty."""
    source = tmp_path / "outbox.sqlite3"
    box = Outbox(source)
    for n in range(3):
        box.accept(_envelope(f"row {n}"))
    box.close()

    real_counts = sqlite_backup._counts
    seen: list[dict[str, int]] = []

    def fake_counts(conn):
        result = real_counts(conn)
        seen.append(result)
        # The first call reads the source, the second reads the copy.
        return result if len(seen) == 1 else {"rows": 1, "pending": 1}

    monkeypatch.setattr(sqlite_backup, "_counts", fake_counts)
    with pytest.raises(RuntimeError, match="1 rows but the source had 3"):
        sqlite_backup.online_backup(source, tmp_path / "copy.sqlite3")


# ---------------------------------------------------------- the manifest ----


def _manifest() -> dict:
    """A manifest of the shape scripts/backup-state.sh writes."""
    digest = "a" * 64
    return {
        "manifest_version": 1,
        "backup_id": "20260924T101500Z",
        "started_at": "2026-09-24T10:15:00Z",
        "finished_at": "2026-09-24T10:15:42Z",
        "rpo_reference": "2026-09-24T10:15:00Z",
        "rpo_note": "Everything this stack accepted after started_at is outside this backup.",
        "compose_project": "aow",
        "database": "aow",
        "artefacts": [
            {
                "name": "postgres.dump",
                "kind": "postgres-custom-dump",
                "service": "postgres",
                "present": True,
                "bytes": 918_273,
                "sha256": digest,
                "container_path": "-",
            },
            {
                "name": "rabbitmq-definitions.json",
                "kind": "rabbitmq-definitions",
                "service": "rabbitmq",
                "present": True,
                "bytes": 4_096,
                "sha256": "b" * 64,
                "container_path": "-",
            },
            {
                "name": "outbox-ingestor.sqlite3",
                "kind": "sqlite-outbox",
                "service": "ingestor",
                "present": True,
                "bytes": 262_144,
                "sha256": "c" * 64,
                "container_path": "/outbox/outbox.sqlite3",
            },
            {
                "name": "outbox-api.sqlite3",
                "kind": "sqlite-outbox",
                "service": "api",
                "present": True,
                "bytes": 32_768,
                "sha256": "d" * 64,
                "container_path": "/outbox/outbox.sqlite3",
            },
            {
                "name": "outbox-enricher.sqlite3",
                "kind": "sqlite-outbox",
                "service": "enricher",
                "present": False,
                "reason": "the enricher container is not running",
            },
        ],
        "not_backed_up": {
            "what": "RabbitMQ message bodies in flight",
            "why": "the bytes are already in the producer outboxes",
            "queues_at_backup_time": [
                {"queue": "aow.ingest", "messages": 0},
                {"queue": "aow.dlq", "messages": 2},
            ],
        },
        "migrations": [
            {"file": "001_init.sql", "sha256": "e" * 64},
            {"file": "002_activities.sql", "sha256": "f" * 64},
        ],
        "images": [
            {"service": "postgres", "image": "postgres:17-alpine@sha256:x", "image_id": "sha256:y"}
        ],
    }


def test_a_well_formed_manifest_validates() -> None:
    assert backup_manifest.validate(_manifest()) == []


def test_every_required_artefact_must_be_declared() -> None:
    """Silence is the failure mode this catches. An outbox that was never
    copied, and is simply not mentioned, reads exactly like one that did not
    need copying."""
    manifest = _manifest()
    manifest["artefacts"] = [a for a in manifest["artefacts"] if a["name"] != "outbox-api.sqlite3"]
    problems = backup_manifest.validate(manifest)
    assert "outbox-api.sqlite3 is not declared at all" in problems


def test_an_absent_artefact_must_give_a_reason() -> None:
    manifest = _manifest()
    for artefact in manifest["artefacts"]:
        if artefact["name"] == "outbox-enricher.sqlite3":
            del artefact["reason"]
    assert "outbox-enricher.sqlite3 is absent and gives no reason" in backup_manifest.validate(
        manifest
    )


@pytest.mark.parametrize(
    "field",
    ["started_at", "finished_at"],
)
def test_timestamps_must_be_iso_8601_utc(field: str) -> None:
    for bad in ("2026-09-24 10:15:00", "2026-09-24T10:15:00+02:00", "24/09/2026", ""):
        manifest = _manifest()
        manifest[field] = bad
        if field == "started_at":
            manifest["rpo_reference"] = bad
        problems = backup_manifest.validate(manifest)
        assert any(field in problem for problem in problems), (field, bad)


def test_the_rpo_reference_must_be_the_start_of_the_run() -> None:
    """If it were the finish, an operator would read the RPO off the wrong end
    of the backup and believe records accepted mid-run were captured."""
    manifest = _manifest()
    manifest["rpo_reference"] = manifest["finished_at"]
    assert "rpo_reference must equal started_at" in backup_manifest.validate(manifest)


def test_a_present_artefact_needs_a_checksum_and_a_size() -> None:
    manifest = _manifest()
    manifest["artefacts"][0]["sha256"] = "not-a-digest"
    manifest["artefacts"][0]["bytes"] = 0
    problems = backup_manifest.validate(manifest)
    assert "postgres.dump has no SHA-256 checksum" in problems
    assert "postgres.dump has no positive byte size" in problems


def _observed_from(manifest: dict) -> dict[str, tuple[str, int]]:
    return {a["name"]: (a["sha256"], a["bytes"]) for a in manifest["artefacts"] if a.get("present")}


def test_compare_accepts_a_directory_that_matches() -> None:
    manifest = _manifest()
    assert backup_manifest.compare(manifest, _observed_from(manifest)) == []


def test_compare_rejects_a_changed_dump() -> None:
    """The guard the restore hangs on: one flipped byte and the checksum no
    longer matches, so restore-state.sh stops before it touches a volume."""
    manifest = _manifest()
    observed = _observed_from(manifest)
    observed["postgres.dump"] = ("9" * 64, 918_273)
    problems = backup_manifest.compare(manifest, observed)
    assert len(problems) == 1
    assert problems[0].startswith("postgres.dump checksum mismatch")


def test_compare_rejects_a_truncated_dump_of_the_right_shape() -> None:
    manifest = _manifest()
    observed = _observed_from(manifest)
    observed["postgres.dump"] = (manifest["artefacts"][0]["sha256"], 12)
    assert any(
        "size mismatch" in problem for problem in backup_manifest.compare(manifest, observed)
    )


def test_compare_rejects_a_missing_file_and_an_unexpected_one() -> None:
    manifest = _manifest()
    observed = _observed_from(manifest)
    del observed["outbox-api.sqlite3"]
    observed["outbox-enricher.sqlite3"] = ("1" * 64, 10)
    observed["something-else.tar"] = ("2" * 64, 10)
    problems = backup_manifest.compare(manifest, observed)
    assert (
        "outbox-api.sqlite3 is declared in the manifest but missing from the directory" in problems
    )
    assert (
        "outbox-enricher.sqlite3 is declared absent but a file of that name is present" in problems
    )
    assert "something-else.tar is in the directory but not declared in the manifest" in problems


def test_the_cli_verifies_a_manifest_from_the_environment(monkeypatch) -> None:
    """How both shell scripts actually call it: the module on the container's
    stdin, the manifest in AOW_MANIFEST, the measurements as arguments."""
    manifest = _manifest()
    monkeypatch.setenv("AOW_MANIFEST", json.dumps(manifest))
    observed = [
        f"--observed={name}={sha}:{size}" for name, (sha, size) in _observed_from(manifest).items()
    ]
    assert backup_manifest.main(["verify", *observed]) == 0
    bad = [arg.replace("a" * 64, "0" * 64) for arg in observed]
    assert backup_manifest.main(["verify", *bad]) == 1


def test_the_cli_rejects_a_malformed_observation(monkeypatch) -> None:
    monkeypatch.setenv("AOW_MANIFEST", json.dumps(_manifest()))
    assert backup_manifest.main(["verify", "--observed=postgres.dump=short:12"]) == 2


# ------------------------------------------------------- the shell scripts ----

BACKUP_SH = ROOT / "scripts" / "backup-state.sh"
RESTORE_SH = ROOT / "scripts" / "restore-state.sh"


@pytest.mark.parametrize("script", [BACKUP_SH, RESTORE_SH])
def test_the_scripts_are_syntactically_valid(script: Path) -> None:
    """`bash -n` here rather than only in a reviewer's shell: these two run on
    the worst day, and a typo in an error path is exactly the kind of thing
    that is never executed until it matters."""
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", [BACKUP_SH, RESTORE_SH])
def test_the_scripts_fail_fast(script: Path) -> None:
    assert "set -euo pipefail" in script.read_text(encoding="utf-8")


def test_the_restore_refuses_a_checksum_mismatch() -> None:
    """Asserted over the text, because the behaviour needs Docker and this
    suite has none. What is being pinned is that the verification is wired to
    an exit and not merely printed: a restore that logs a mismatch and carries
    on is the failure mode the whole manifest exists to prevent."""
    text = RESTORE_SH.read_text(encoding="utf-8")
    assert "python - verify" in text, "the manifest is never verified"
    assert "does not match its manifest; nothing has been changed" in text
    # The refusal must come before anything destructive. `down -v` is the first
    # irreversible act in the script, so the verification has to precede it.
    assert text.index("does not match its manifest") < text.index("down -v")


def test_the_restore_refuses_the_live_project_without_an_explicit_flag() -> None:
    text = RESTORE_SH.read_text(encoding="utf-8")
    assert "--overwrite-live-project" in text
    assert 'if [ "$PROJECT" = "$LIVE_PROJECT" ]; then' in text
    assert 'if [ "$OVERWRITE_LIVE" -ne 1 ]; then' in text
    assert "REFUSING:" in text
    # Default target, so an operator who types nothing gets the safe thing.
    assert 'PROJECT="aow-restore"' in text


def test_the_scripts_need_no_host_tooling() -> None:
    """Docker only. A host psql, sqlite3 or python3 would make the runbook
    untrue on the two platforms this project promises to work on."""
    for script in (BACKUP_SH, RESTORE_SH):
        text = script.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            # Comments and the scripts' own printed prose talk *about* these
            # tools; only lines that would actually run one are of interest.
            if stripped.startswith(("#", "say ", "note ", "printf ", "echo ", "die ", "fail ")):
                continue
            for forbidden in ("python3 ", "sqlite3 ", "sha256sum", "shasum"):
                assert forbidden not in stripped, f"{script.name}: {line}"
            # `psql` and `pg_restore` are fine -- inside a container.
            if "psql " in stripped or "pg_dump " in stripped or "pg_restore " in stripped:
                assert "exec -T postgres" in stripped or stripped.startswith(
                    "dc exec"
                ), f"{script.name} runs a Postgres client outside a container: {line}"


def test_the_backup_declares_every_artefact_the_manifest_requires() -> None:
    """The shell writes the manifest; the Python validates it. If the two ever
    disagree about the artefact names, every backup fails its own round-trip
    check -- so they are pinned together here rather than at 3am."""
    text = BACKUP_SH.read_text(encoding="utf-8")
    assert "postgres.dump" in text
    assert "rabbitmq-definitions.json" in text
    # The three outbox artefacts are written from one loop, so what has to
    # match the manifest's required names is the loop's service list.
    assert "for svc in ingestor api enricher; do" in text
    assert 'name="outbox-$svc.sqlite3"' in text
    for required in backup_manifest.REQUIRED_ARTEFACTS:
        if required.startswith("outbox-"):
            assert required.removeprefix("outbox-").removesuffix(".sqlite3") in text, required
        else:
            assert required in text, required


def test_the_backup_does_not_claim_to_back_up_queue_contents() -> None:
    """The one claim that must never drift, because it is the claim an
    operator would plan a recovery around."""
    text = BACKUP_SH.read_text(encoding="utf-8")
    assert "NOT the messages" in text or "not backed up" in text
    assert "export_definitions" in text
    assert "not_backed_up" in text


def test_python_is_new_enough_for_the_helpers() -> None:
    """sqlite3.Connection.backup arrived in 3.7 and the images pin 3.12; this
    is a cheap guard against the helpers being run somewhere older."""
    assert sys.version_info >= (3, 9)
    assert hasattr(sqlite3.Connection, "backup")


def test_the_manifest_fixture_here_matches_the_shape_the_shell_writes() -> None:
    """A fixture that has drifted from the producer tests nothing. These keys
    are the ones scripts/backup-state.sh prints literally."""
    text = BACKUP_SH.read_text(encoding="utf-8")
    for key in _manifest():
        assert f'"{key}"' in text, key
    unchanged = copy.deepcopy(_manifest())
    assert backup_manifest.validate(unchanged) == []
