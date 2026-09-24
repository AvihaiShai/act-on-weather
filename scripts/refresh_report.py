"""Assemble the operator refresh's run report.

Reads what `scripts/refresh.sh` collected into its state directory and prints
one JSON object on stdout. The wrapper pipes that into
`services.common.refresh_state` inside the ingestor container, which files it on
a volume the api serves read-only at `GET /refresh/last`.

This is a separate file rather than a heredoc inside the shell script for one
reason: it is the shape the UI renders, so it is worth linting, formatting and
reading on its own. It has no imports beyond the standard library, takes no
arguments, and never touches the network.

Every input is optional. A run interrupted before the fetch has a `before.tsv`
and nothing else, and must still produce a valid report saying so -- a refresh
that died early is exactly the run an operator needs to find on the page.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATE = Path(os.environ.get("AOW_STATE_DIR", "."))


def _text(name: str) -> str:
    try:
        return (STATE / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _json(name: str) -> Any:
    raw = _text(name)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _city_states(name: str) -> dict[str, dict[str, Any]]:
    """`city<TAB>as_of<TAB>covers_to<TAB>days` per line, as the shell wrote it."""
    out: dict[str, dict[str, Any]] = {}
    for line in _text(name).splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        city, as_of, covers_to, days = (part.strip() for part in parts)
        out[city] = {
            "as_of": None if as_of in {"", "none", "-"} else as_of,
            "covers_to": None if covers_to in {"", "-"} else covers_to,
            "stored_days": int(days) if days.isdigit() else 0,
        }
    return out


def _int(name: str, default: int = 0) -> int:
    value = os.environ.get(name, "")
    return int(value) if value.isdigit() else default


def _flag(name: str) -> bool:
    return os.environ.get(name, "") == "1"


def main() -> int:
    before = _city_states("before.tsv")
    after = _city_states("after.tsv")
    fetch = _json("fetch.json") or {}
    fetch_cities = {c["city"]: c for c in fetch.get("cities", []) if isinstance(c, dict)}
    requested = _text("requested").split() or sorted(set(before) | set(fetch_cities))

    cities = []
    for slug in requested:
        fetched = fetch_cities.get(slug, {})
        was, now = before.get(slug, {}), after.get(slug, {})
        cities.append(
            {
                "city": slug,
                "name": fetched.get("name") or slug,
                # None, not False: "the fetch never got to this city" and "the
                # provider refused this city" are different things on the page.
                "ok": fetched.get("ok") if slug in fetch_cities else None,
                "error": fetched.get("error"),
                "accepted": fetched.get("accepted", 0),
                "message_ids": fetched.get("message_ids", []),
                "as_of_before": was.get("as_of"),
                "as_of_after": now.get("as_of") or was.get("as_of"),
                "covers_to_before": was.get("covers_to"),
                "covers_to_after": now.get("covers_to") or was.get("covers_to"),
                "stored_days_after": now.get("stored_days", was.get("stored_days", 0)),
            }
        )

    published = os.environ.get("AOW_PUBLISHED", "unknown")
    report = {
        "schema_version": 1,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "started_at": os.environ.get("AOW_STARTED") or None,
        "outcome": os.environ.get("AOW_OUTCOME") or "unknown",
        "exit_code": _int("AOW_EXIT", -1),
        "project": os.environ.get("AOW_PROJECT_NAME") or None,
        "provider": fetch.get("provider"),
        "requested_cities": requested,
        # The whole point of the command, and the only field a reviewer has to
        # trust: did the one container that was given a route out lose it again?
        "egress_window": {
            "network": os.environ.get("AOW_WINDOW") or None,
            "opened": _flag("AOW_WINDOW_OPENED"),
            "closed_and_verified": _flag("AOW_WINDOW_CLOSED"),
            "inherited_open_window": _flag("AOW_WINDOW_INHERITED"),
            "held_seconds": _int("AOW_HELD", -1) if os.environ.get("AOW_HELD") else None,
        },
        "accepted": _int("AOW_ACCEPTED"),
        "published": published if published == "unknown" else _int("AOW_PUBLISHED"),
        "stored": _int("AOW_STORED"),
        "failed_cities": _text("failed_cities").split(),
        "cities": cities,
        "coverage_before": _json("cov_before.json"),
        "coverage_after": _json("cov_after.json"),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
