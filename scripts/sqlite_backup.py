"""Take an online-consistent copy of a live SQLite producer outbox.

    python sqlite_backup.py SOURCE DEST

Run this INSIDE the container that owns the outbox, with that image's own
Python. It is fed to the container on stdin:

    docker compose exec -T ingestor python - /outbox/outbox.sqlite3 /tmp/copy \
      < scripts/sqlite_backup.py

Why not `cp`.

The three producer outboxes (services/common/outbox.py) run in WAL mode with
`synchronous=FULL`. A WAL database is not one file: committed transactions live
in `outbox.sqlite3-wal` until a checkpoint folds them back into the main file,
and `outbox.sqlite3-shm` is the shared index into that log. Copying only
`outbox.sqlite3` therefore silently drops every commit since the last
checkpoint -- which, for an outbox, is exactly the recently accepted records
that the no-data-loss guarantee is about. Copying all three files instead is no
better: the reader has no way to take them at one instant, so the -wal it
copies can be newer than the main file it already copied, and SQLite may refuse
to open the result or open it torn.

`sqlite3.Connection.backup()` is the supported answer. It reads the source
through SQLite itself, under a read transaction, so the copy is a single
consistent snapshot of a committed state, and the destination is written as one
self-contained file with the WAL already folded in. If a writer commits during
the copy, SQLite restarts the copy rather than producing a torn one.

The source is opened READ-ONLY, deliberately. A backup must not be able to
damage the thing it is backing up, and the guarantee boundary documented in
services/common/outbox.py belongs to the producer process, not to this script.
(Read-only here still needs the ordinary filesystem permission to touch the
-shm file, which is why this runs inside the owning container as the same uid
10001 that owns the volume -- not as an outside root process.)

Exit status is 0 only when the copy passed `PRAGMA integrity_check` and holds
at least every row the source had when the copy started. The outbox is
append-and-update-in-place; nothing is ever deleted from it, so "at least" is
the correct comparison and a concurrent accept during the copy is not an error.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def _readonly_uri(path: Path) -> str:
    """The same read-only URI form Outbox(readonly=True) uses, for the same
    reason: it is the one spelling that works for an absolute POSIX path with
    no quoting rules of its own."""
    return path.resolve().as_uri() + "?mode=ro"


def _integrity(conn: sqlite3.Connection) -> str:
    """SQLite's own verdict on the copy. A separate function so that the
    failure branch below can be exercised by a test without manufacturing a
    corrupt database file, which is not reproducible across SQLite versions."""
    return conn.execute("PRAGMA integrity_check").fetchone()[0]


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN published_at IS NULL THEN 1 ELSE 0 END) AS pending"
        " FROM outbox"
    ).fetchone()
    return {"rows": row[0] or 0, "pending": row[1] or 0}


def online_backup(source: Path | str, dest: Path | str) -> dict:
    """Copy a live outbox to `dest` and return a report on what was copied.

    Raises RuntimeError if the copy is not a usable outbox. The caller is
    expected to let that fail the backup: a backup artefact nobody checked is
    the artefact that turns out to be empty on the day it is needed.
    """
    source = Path(source)
    dest = Path(dest)
    if not source.exists():
        raise FileNotFoundError(f"no outbox at {source}")

    # A leftover destination -- or, worse, a leftover -wal beside it from an
    # earlier run -- would be merged into the new copy by SQLite and produce a
    # file that is part of one backup and part of another.
    for stale in (dest, Path(f"{dest}-wal"), Path(f"{dest}-shm")):
        if stale.exists():
            stale.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)

    src = sqlite3.connect(_readonly_uri(source), uri=True, timeout=30)
    try:
        before = _counts(src)
        dst = sqlite3.connect(dest, timeout=30)
        try:
            # pages=0 copies the whole database in one step, which is what
            # makes it a single consistent read rather than a series of them.
            src.backup(dst, pages=0)
            integrity = _integrity(dst)
            after = _counts(dst)
        finally:
            dst.close()
    finally:
        src.close()

    report = {
        "source": str(source),
        "dest": str(dest),
        "bytes": dest.stat().st_size,
        "source_rows": before["rows"],
        "source_pending": before["pending"],
        "copied_rows": after["rows"],
        "copied_pending": after["pending"],
        "integrity": integrity,
    }
    if integrity != "ok":
        raise RuntimeError(f"the copy of {source} failed integrity_check: {integrity}")
    if after["rows"] < before["rows"]:
        raise RuntimeError(
            f"the copy of {source} holds {after['rows']} rows but the source had "
            f"{before['rows']} before the copy started"
        )
    return report


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    try:
        print(json.dumps(online_backup(args[0], args[1]), sort_keys=True))
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
