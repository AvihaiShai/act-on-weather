"""The last operator refresh run, as a fact the UI can read (M12, F4).

`scripts/refresh.sh` is a command on the Docker host. Everything it learns --
which cities the provider answered for, the as-of before and after, the message
ids it accepted, how many of them reached Postgres, and whether the egress
window closed -- used to exist only in the terminal of whoever ran it. The
Update tab could then show how fresh the stored data was, which is a different
question, and a reviewer had no way to tell a refresh that half worked from one
that never ran.

So the wrapper files one small JSON report here on its way out, on every exit
path. The write happens inside the ingestor container, which already runs the
fetch and already has a writable volume; the api mounts the same volume
read-only and serves it at `GET /refresh/last`; the UI renders it beside the
stored freshness, clearly labelled as a different thing.

**Why not the queue and the database.** Every record the system *collects*
travels outbox -> queue -> consumer -> Postgres, and the consumer stays the only
writer (M4). This is not a collected record; it is a report about a command, and
it has to survive the case the queue cannot express: a refresh whose provider
refused every city accepts no messages at all, so there is nothing to publish
and nothing for the consumer to store. A run that fetched nothing is precisely
the run an operator needs to see. A second reason is smaller but real: this file
records that the egress window closed, and that fact is about the host's network
rather than about weather.

The cost is stated plainly: one JSON file, last-write-wins, no history, and it
is lost if the volume is removed. It is operator telemetry, not data.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import config

SCHEMA_VERSION = 1

# A report is a handful of cities and their message ids. A megabyte is already
# far more than that, and refusing early keeps a runaway writer from filling the
# volume the ingestor shares nothing else with.
MAX_BYTES = 1_000_000


def read(path: Path | None = None) -> dict[str, Any] | None:
    """The last recorded run, or None when nothing has been recorded.

    Never raises. This is read on the API's request path, and a truncated or
    hand-edited file must degrade to "no report" rather than 500 a route the UI
    calls on every rerun. A file mid-write is impossible to observe because
    `write` renames into place, but a file from an older schema is not.
    """
    path = path or config.REFRESH_STATE_PATH
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        report = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(report, dict):
        return None
    return report


def write(report: dict[str, Any], path: Path | None = None) -> Path:
    """Replace the report, atomically.

    Temp file in the same directory, then `os.replace`, so a reader either sees
    the whole previous report or the whole new one. A partial report on the one
    page a reviewer checks after a failed refresh would be worse than no report.
    """
    path = path or config.REFRESH_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(report, indent=2, sort_keys=True, default=str)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".last-run.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return path


def main(argv: list[str] | None = None) -> int:
    """`python -m services.common.refresh_state` -- read a report on stdin, file it.

    This is how scripts/refresh.sh persists its run: it pipes the JSON it has
    just printed into this module inside the ingestor container. Keeping the
    write here rather than in the shell means the path, the schema stamp and the
    atomic rename have one implementation that the unit tests cover.
    """
    del argv
    raw = sys.stdin.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        print(f"refusing a refresh report larger than {MAX_BYTES} bytes", file=sys.stderr)
        return 2
    try:
        report = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"not a JSON refresh report: {exc}", file=sys.stderr)
        return 2
    if not isinstance(report, dict):
        print("a refresh report must be a JSON object", file=sys.stderr)
        return 2
    report.setdefault("schema_version", SCHEMA_VERSION)
    path = write(report)
    print(f"recorded the refresh run in {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
