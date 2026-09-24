"""The run report is assembled from whatever the run got through (M12, F4).

`scripts/refresh_report.py` runs from the wrapper's exit trap, which means it is
called on paths where most of its inputs do not exist: a run interrupted before
the fetch has a before-state and nothing else. The property that matters is that
every one of those produces a valid report saying how far the run got, because a
refresh that died early is exactly the run an operator goes looking for.

Run as a subprocess, the way the wrapper runs it, so the environment contract is
covered too and not just the functions.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "refresh_report.py"

FETCH = {
    "provider": "open-meteo",
    "accepted": 32,
    "failed_cities": ["reykjavik"],
    "cities": [
        {
            "city": "rome",
            "name": "Rome",
            "ok": True,
            "error": None,
            "accepted": 16,
            "message_ids": ["id-rome-1", "id-rome-2"],
            "as_of": "2026-09-24T07:05:00+00:00",
            "last_date": "2026-10-09",
        },
        {
            "city": "reykjavik",
            "name": "Reykjavik",
            "ok": False,
            "error": "ConnectionError: no route",
            "accepted": 0,
            "message_ids": [],
            "as_of": None,
            "last_date": None,
        },
    ],
}


def run(state: Path, **env) -> dict:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        env={"AOW_STATE_DIR": str(state), "PATH": "", **env},
        check=True,
    )
    return json.loads(result.stdout)


@pytest.fixture
def state(tmp_path) -> Path:
    return tmp_path


def write_tsv(state: Path, name: str, rows: list[tuple]) -> None:
    state.joinpath(name).write_text(
        "".join("\t".join(str(f) for f in row) + "\n" for row in rows), encoding="utf-8"
    )


def test_an_empty_state_directory_still_produces_a_report(state):
    """The worst case: killed before it read anything. It must not crash the exit
    trap, and it must not claim a success."""
    report = run(state)
    assert report["schema_version"] == 1
    assert report["outcome"] == "unknown"
    assert report["cities"] == []
    assert report["egress_window"]["closed_and_verified"] is False


def test_a_run_interrupted_before_the_fetch_records_the_cities_it_read(state):
    write_tsv(
        state,
        "before.tsv",
        [("rome", "2026-09-23 18:16:43Z", "2026-10-08", 16)],
    )
    state.joinpath("requested").write_text("rome london", encoding="utf-8")
    report = run(state, AOW_OUTCOME="no-fetch-report", AOW_EXIT="2")

    assert report["outcome"] == "no-fetch-report"
    assert report["exit_code"] == 2
    assert [c["city"] for c in report["cities"]] == ["rome", "london"]
    rome, london = report["cities"]
    # Tri-state: never attempted is not the same as refused by the provider.
    assert rome["ok"] is None
    assert rome["as_of_before"] == "2026-09-23 18:16:43Z"
    # With no after-state, the as-of has not moved -- it must not read as null.
    assert rome["as_of_after"] == "2026-09-23 18:16:43Z"
    assert london["as_of_before"] is None


def test_a_half_successful_run_keeps_the_two_cities_apart(state):
    write_tsv(
        state,
        "before.tsv",
        [
            ("rome", "2026-09-23 18:16:43Z", "2026-10-08", 16),
            ("reykjavik", "2026-09-23 18:16:44Z", "2026-10-08", 16),
        ],
    )
    write_tsv(
        state,
        "after.tsv",
        [
            ("rome", "2026-09-24 07:05:00Z", "2026-10-09", 17),
            ("reykjavik", "2026-09-23 18:16:44Z", "2026-10-08", 16),
        ],
    )
    state.joinpath("requested").write_text("rome reykjavik", encoding="utf-8")
    state.joinpath("failed_cities").write_text("reykjavik", encoding="utf-8")
    state.joinpath("fetch.json").write_text(json.dumps(FETCH), encoding="utf-8")

    report = run(
        state,
        AOW_OUTCOME="cities-failed",
        AOW_EXIT="2",
        AOW_ACCEPTED="32",
        AOW_PUBLISHED="32",
        AOW_STORED="32",
        AOW_WINDOW="aow_refresh_egress",
        AOW_WINDOW_OPENED="1",
        AOW_WINDOW_CLOSED="1",
        AOW_HELD="4",
        AOW_WINDOW_DEADLINE="600",
    )

    assert report["failed_cities"] == ["reykjavik"]
    assert report["provider"] == "open-meteo"
    rome, reykjavik = report["cities"]

    assert rome["ok"] is True
    assert rome["accepted"] == 16
    assert rome["message_ids"] == ["id-rome-1", "id-rome-2"]
    assert (rome["as_of_before"], rome["as_of_after"]) == (
        "2026-09-23 18:16:43Z",
        "2026-09-24 07:05:00Z",
    )
    assert (rome["covers_to_before"], rome["covers_to_after"]) == ("2026-10-08", "2026-10-09")

    # The city that failed: its stored as-of has not moved, and the reason is on
    # the record. This is the pair a freshness table on its own cannot show.
    assert reykjavik["ok"] is False
    assert reykjavik["as_of_before"] == reykjavik["as_of_after"]
    assert "no route" in reykjavik["error"]

    assert report["egress_window"] == {
        "network": "aow_refresh_egress",
        "opened": True,
        "closed_and_verified": True,
        "inherited_open_window": False,
        "held_seconds": 4,
        "deadline_seconds": 600,
    }


def test_a_window_that_did_not_close_is_recorded_as_such(state):
    report = run(state, AOW_OUTCOME="window-not-closed", AOW_WINDOW_OPENED="1", AOW_EXIT="3")
    assert report["egress_window"]["opened"] is True
    assert report["egress_window"]["closed_and_verified"] is False
    assert report["exit_code"] == 3


def test_an_inherited_window_is_recorded(state):
    report = run(state, AOW_WINDOW_INHERITED="1")
    assert report["egress_window"]["inherited_open_window"] is True


def test_the_window_deadline_is_recorded_when_a_guard_enforced_one(state):
    """A run whose guard could not start is a run whose window had no bound but
    the trap. The report must not be silent about which of the two it was."""
    assert run(state, AOW_WINDOW_DEADLINE="600")["egress_window"]["deadline_seconds"] == 600
    assert run(state)["egress_window"]["deadline_seconds"] is None


def test_an_unknown_published_count_stays_unknown(state):
    """The wrapper reports `unknown` when it could not query the outbox. A number
    that is quietly wrong is worse than an admission, so it must not become 0."""
    assert run(state, AOW_PUBLISHED="unknown")["published"] == "unknown"
    assert run(state, AOW_PUBLISHED="12")["published"] == 12


def test_a_malformed_state_file_does_not_stop_the_report(state):
    state.joinpath("fetch.json").write_text("{truncated", encoding="utf-8")
    write_tsv(state, "before.tsv", [("rome", "x", "y", 1), ("broken-line",)])
    report = run(state, AOW_OUTCOME="ok")
    assert report["outcome"] == "ok"
    assert [c["city"] for c in report["cities"]] == ["rome"]
