"""Validate a backup manifest, and check it against the artefacts on disk.

    python backup_manifest.py validate < manifest.json
    python backup_manifest.py verify --observed postgres.dump=<sha256>:<bytes>

`scripts/backup-state.sh` writes the manifest and `scripts/restore-state.sh`
checks it before it touches anything. Both run this module inside a container,
because this repository has no host-side Python: the module arrives on the
container's stdin and the manifest arrives in AOW_MANIFEST, since only one of
the two can have stdin.

Why the check exists at all. A restore that proceeds from a truncated or
half-written dump is worse than a restore that refuses: it produces a database
that looks restored, that an operator will believe, and that is missing rows
nobody can now name. So the manifest records a SHA-256 and a byte count for
every artefact, and a restore that cannot reproduce both of them stops.

This is deliberately a schema check and a checksum comparison, and nothing
else. It cannot tell you that a dump restores cleanly -- only a restore can do
that, which is why demos/06_backup_restore.sh actually performs one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

# Every backup must declare all five, present or not. An artefact simply
# missing from the manifest is indistinguishable from one the operator never
# noticed was missing, so absence is recorded rather than omitted:
# `present: false` with a reason.
REQUIRED_ARTEFACTS = (
    "postgres.dump",
    "rabbitmq-definitions.json",
    "outbox-ingestor.sqlite3",
    "outbox-api.sqlite3",
    "outbox-enricher.sqlite3",
)

# The manifest's timestamps are the RPO reference point an operator reads off
# it, so "some time near then" is not good enough: one spelling, UTC, seconds.
ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

MANIFEST_VERSION = 1


def validate(manifest: Any) -> list[str]:
    """Return a list of human-readable problems; empty means the manifest is
    structurally sound. It says nothing about whether the files match."""
    problems: list[str] = []
    if not isinstance(manifest, dict):
        return ["the manifest is not a JSON object"]

    if manifest.get("manifest_version") != MANIFEST_VERSION:
        got = manifest.get("manifest_version")
        problems.append(f"manifest_version is {got!r}, expected {MANIFEST_VERSION}")
    for field in ("backup_id", "compose_project"):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            problems.append(f"{field} is missing or empty")
    for field in ("started_at", "finished_at"):
        value = manifest.get(field)
        if not isinstance(value, str) or not ISO_UTC.match(value):
            problems.append(f"{field} is not an ISO-8601 UTC timestamp (YYYY-MM-DDTHH:MM:SSZ)")
    # The whole point of recording the start: everything the system accepted
    # after it is outside this backup. If rpo_reference disagreed with
    # started_at an operator would read the RPO off the wrong end of the run.
    if manifest.get("rpo_reference") != manifest.get("started_at"):
        problems.append("rpo_reference must equal started_at")

    artefacts = manifest.get("artefacts")
    if not isinstance(artefacts, list):
        return [*problems, "artefacts is missing or is not a list"]

    seen: set[str] = set()
    for index, item in enumerate(artefacts):
        if not isinstance(item, dict):
            problems.append(f"artefacts[{index}] is not an object")
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            problems.append(f"artefacts[{index}] has no name")
            continue
        if name in seen:
            problems.append(f"{name} is declared twice")
        seen.add(name)
        for field in ("kind", "service"):
            if not isinstance(item.get(field), str) or not item[field]:
                problems.append(f"{name} has no {field}")
        present = item.get("present")
        if not isinstance(present, bool):
            problems.append(f"{name} does not say whether it is present")
            continue
        if present:
            if not SHA256.match(str(item.get("sha256", ""))):
                problems.append(f"{name} has no SHA-256 checksum")
            if not isinstance(item.get("bytes"), int) or item["bytes"] <= 0:
                problems.append(f"{name} has no positive byte size")
        elif not item.get("reason"):
            problems.append(f"{name} is absent and gives no reason")

    for required in REQUIRED_ARTEFACTS:
        if required not in seen:
            problems.append(f"{required} is not declared at all")

    migrations = manifest.get("migrations")
    if not isinstance(migrations, list) or not migrations:
        problems.append("migrations is missing or empty")
    else:
        for index, item in enumerate(migrations):
            if not isinstance(item, dict) or not item.get("file"):
                problems.append(f"migrations[{index}] has no file")
            elif not SHA256.match(str(item.get("sha256", ""))):
                problems.append(f"migrations[{index}] ({item['file']}) has no SHA-256")

    if not isinstance(manifest.get("images"), list) or not manifest["images"]:
        problems.append("images is missing or empty")

    return problems


def compare(manifest: Any, observed: dict[str, tuple[str, int]]) -> list[str]:
    """Compare the manifest's artefacts against what is actually on disk.

    `observed` maps an artefact name to (sha256, bytes) as measured now. An
    artefact the manifest declares present and that was not measured is a
    problem; one that was measured and is not declared is also a problem,
    because it means the restore is about to work from a directory that is not
    the one the manifest describes.
    """
    problems: list[str] = []
    declared: dict[str, dict] = {}
    for item in manifest.get("artefacts", []):
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            declared[item["name"]] = item

    for name, item in declared.items():
        if not item.get("present"):
            if name in observed:
                problems.append(f"{name} is declared absent but a file of that name is present")
            continue
        if name not in observed:
            problems.append(f"{name} is declared in the manifest but missing from the directory")
            continue
        got_sha, got_bytes = observed[name]
        if got_sha != item.get("sha256"):
            problems.append(
                f"{name} checksum mismatch: manifest {item.get('sha256')}, on disk {got_sha}"
            )
        if got_bytes != item.get("bytes"):
            problems.append(
                f"{name} size mismatch: manifest {item.get('bytes')} bytes, on disk {got_bytes}"
            )

    for name in observed:
        if name not in declared:
            problems.append(f"{name} is in the directory but not declared in the manifest")

    return problems


def parse_observed(values: list[str]) -> dict[str, tuple[str, int]]:
    observed: dict[str, tuple[str, int]] = {}
    for value in values:
        name, _, rest = value.partition("=")
        digest, _, size = rest.partition(":")
        if not name or not SHA256.match(digest) or not size.isdigit():
            raise ValueError(f"--observed expects NAME=<sha256>:<bytes>, got {value!r}")
        observed[name] = (digest, int(size))
    return observed


def _load_manifest() -> Any:
    raw = os.environ.get("AOW_MANIFEST")
    if raw is None:
        raw = sys.stdin.read()
    return json.loads(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a backup manifest.")
    parser.add_argument("mode", choices=("validate", "verify"))
    parser.add_argument(
        "--observed",
        action="append",
        default=[],
        metavar="NAME=SHA256:BYTES",
        help="an artefact as it was actually measured on disk",
    )
    args = parser.parse_args(argv)

    try:
        manifest = _load_manifest()
    except json.JSONDecodeError as exc:
        print(f"the manifest is not valid JSON: {exc}", file=sys.stderr)
        return 1

    problems = validate(manifest)
    if args.mode == "verify":
        try:
            problems += compare(manifest, parse_observed(args.observed))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"{len(problems)} problem(s) with this backup", file=sys.stderr)
        return 1
    print("manifest ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
