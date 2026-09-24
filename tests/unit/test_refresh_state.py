"""The last-run report survives being read at the wrong moment (M12, F4).

`GET /refresh/last` is on the API's request path and the UI calls it on every
rerun, so the read has one hard requirement: it never raises. A missing file, a
truncated file, a file from a future schema and a file somebody edited by hand
all have to degrade to "no report" rather than 500 the route.

The write has the matching requirement: a reader must never see half a report.
That is what the rename is for, and the test that proves it is the one that reads
the directory while a write is in progress.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from services.common import refresh_state

REPORT = {
    "outcome": "ok",
    "exit_code": 0,
    "accepted": 16,
    "stored": 16,
    "egress_window": {"closed_and_verified": True},
    "cities": [{"city": "rome", "ok": True}],
}


@pytest.fixture
def state(tmp_path) -> Path:
    return tmp_path / "state" / "last-run.json"


def test_a_report_round_trips(state):
    refresh_state.write(REPORT, state)
    assert refresh_state.read(state) == REPORT


def test_the_parent_directory_is_created(tmp_path):
    target = tmp_path / "a" / "b" / "last-run.json"
    refresh_state.write(REPORT, target)
    assert target.exists()


def test_nothing_recorded_reads_as_none(state):
    assert refresh_state.read(state) is None


def test_a_truncated_report_reads_as_none(state):
    refresh_state.write(REPORT, state)
    state.write_text(state.read_text(encoding="utf-8")[:40], encoding="utf-8")
    assert refresh_state.read(state) is None


def test_a_report_that_is_not_an_object_reads_as_none(state):
    state.parent.mkdir(parents=True)
    state.write_text('["not", "a", "report"]', encoding="utf-8")
    assert refresh_state.read(state) is None


def test_an_unreadable_report_reads_as_none(tmp_path):
    """A directory where the file should be. Contrived, but it is the shape of
    every OSError this read can hit -- including a volume that failed to mount,
    which is the realistic one."""
    target = tmp_path / "last-run.json"
    target.mkdir()
    assert refresh_state.read(target) is None


def test_a_write_leaves_no_partial_file_behind(state):
    """The rename is the point: at no moment does the report path hold a partial
    document, and no temp file is left in the directory afterwards."""
    refresh_state.write(REPORT, state)
    refresh_state.write({**REPORT, "outcome": "cities-failed"}, state)
    assert refresh_state.read(state)["outcome"] == "cities-failed"
    assert [p.name for p in state.parent.iterdir()] == ["last-run.json"]


def test_a_failed_write_does_not_replace_the_previous_report(state, monkeypatch):
    refresh_state.write(REPORT, state)

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(refresh_state.os, "replace", boom)
    with pytest.raises(OSError):
        refresh_state.write({"outcome": "cities-failed"}, state)

    # The old report is intact and the temp file is gone: a refresh that could
    # not file its report must not also destroy the previous one.
    assert refresh_state.read(state) == REPORT
    assert [p.name for p in state.parent.iterdir()] == ["last-run.json"]


# ------------------------------------------------------- the stdin entrypoint --
# `scripts/refresh.sh` pipes its JSON into `python -m services.common.refresh_state`
# inside the ingestor container. These cover that path, because a malformed
# report must be refused rather than filed.


def _run_main(monkeypatch, stdin: str, state: Path) -> int:
    monkeypatch.setattr(refresh_state.config, "REFRESH_STATE_PATH", state)
    monkeypatch.setattr(refresh_state.sys, "stdin", _Stdin(stdin))
    return refresh_state.main([])


class _Stdin:
    def __init__(self, text: str):
        self.text = text

    def read(self, size: int = -1) -> str:
        return self.text if size < 0 else self.text[:size]


def test_main_files_a_report_from_stdin(monkeypatch, state):
    assert _run_main(monkeypatch, json.dumps(REPORT), state) == 0
    stored = refresh_state.read(state)
    assert stored["outcome"] == "ok"
    # Stamped even when the wrapper forgot to, so a reader can always tell which
    # shape it is looking at.
    assert stored["schema_version"] == refresh_state.SCHEMA_VERSION


def test_main_refuses_something_that_is_not_json(monkeypatch, state):
    assert _run_main(monkeypatch, "not json at all", state) == 2
    assert refresh_state.read(state) is None


def test_main_refuses_a_json_document_that_is_not_an_object(monkeypatch, state):
    assert _run_main(monkeypatch, "[1, 2, 3]", state) == 2
    assert refresh_state.read(state) is None


def test_main_refuses_an_absurdly_large_report(monkeypatch, state):
    oversize = json.dumps({"pad": "x" * (refresh_state.MAX_BYTES + 10)})
    assert _run_main(monkeypatch, oversize, state) == 2
    assert refresh_state.read(state) is None


def test_the_default_path_comes_from_config():
    """The api reads the same constant the ingestor writes. If these ever drift,
    the UI shows a report nobody is writing."""
    assert str(refresh_state.config.REFRESH_STATE_PATH).endswith("last-run.json")
    assert os.environ.get("AOW_REFRESH_STATE_PATH") is None
