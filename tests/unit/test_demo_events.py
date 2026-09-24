"""Demo mode: generated sample events are opt-in, and cannot be left behind.

`data/events.seed.jsonl` holds the real listings, each hand-checked against the
venue's or organiser's own page; `data/events.samples.jsonl` holds 45 generated
rows, which exist only so the planner and the agent can be exercised where no
verified listing was found. The rule the README states is stronger than "they
are labelled":

  * a default run never accepts them (the ingestor does not replay the file),
  * a default run never stores them (the consumer drops them at the write
    boundary, whoever produced them),
  * and a database that was in demo mode loses them the moment it is not.

These tests hold all three, plus the file-level split that makes them possible.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from services.common import config, schemas
from services.consumer import main as consumer
from services.ingestor import fetch_content
from services.ingestor import main as ingestor

# ------------------------------------------------------------- fixtures ----


def event_row(event_id: str, *, is_sample: bool) -> dict:
    return {
        "id": event_id,
        "city_id": "london",
        "title": ("Sample: evening concert" if is_sample else "Laver Cup 2026"),
        "category": "concert" if is_sample else "sport",
        "venue": "The O2 arena",
        "starts_at": "2026-09-25T20:00:00+00:00",
        "ends_at": "2026-09-25T23:00:00+00:00",
        "source": ("generated sample - NOT a real listing" if is_sample else "The O2 listing"),
        "source_url": "https://example.invalid/whatever",
        "is_sample": is_sample,
        "as_of": "2026-09-23T18:00:00+00:00",
    }


class FakeCursor:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.rowcount = 0

    def execute(self, sql, params=None):  # noqa: ANN001 - a stand-in for psycopg
        self.statements.append(" ".join(sql.split()))
        self.rowcount = 3
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self) -> None:
        self.cur = FakeCursor()
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


# ------------------------------------------------- the two snapshot files ----


def test_verified_and_generated_events_are_never_merged(tmp_path):
    """One file in, one file out, each way. The split is what lets the
    ingestor decide; merging them would make the decision unavailable."""
    seed = tmp_path / "events.seed.jsonl"
    samples = tmp_path / "events.samples.jsonl"
    seed.write_text(json.dumps(event_row("real:1", is_sample=False)) + "\n", encoding="utf-8")
    samples.write_text(json.dumps(event_row("sample:1", is_sample=True)) + "\n", encoding="utf-8")

    verified = fetch_content.load_events(seed)
    generated = fetch_content.load_sample_events(samples)

    assert [r["id"] for r in verified] == ["real:1"]
    assert [r["id"] for r in generated] == ["sample:1"]
    assert all(not r["is_sample"] for r in verified)
    assert all(r["is_sample"] for r in generated)


def test_a_sample_file_row_that_does_not_admit_it_is_dropped(tmp_path):
    """The labelling is a property of the data, so it is checked rather than
    assumed: an unmarked row in the sample file would otherwise be stored as
    though it were verified."""
    samples = tmp_path / "events.samples.jsonl"
    samples.write_text(
        json.dumps(event_row("sneaky:1", is_sample=False))
        + "\n"
        + json.dumps(event_row("sample:1", is_sample=True))
        + "\n",
        encoding="utf-8",
    )
    assert [r["id"] for r in fetch_content.load_sample_events(samples)] == ["sample:1"]


# ----------------------------------------------------- the ingestor's gate ----


def snapshot_dir(tmp_path):
    out = tmp_path / "snapshot"
    out.mkdir()
    (out / "events.jsonl").write_text(
        json.dumps(event_row("real:1", is_sample=False)) + "\n", encoding="utf-8"
    )
    (out / "events.samples.jsonl").write_text(
        json.dumps(event_row("sample:1", is_sample=True)) + "\n", encoding="utf-8"
    )
    return out


def accepted_ids(tmp_path, demo: bool, monkeypatch) -> set[str]:
    monkeypatch.setattr(config, "DEMO_EVENTS", demo)
    box = ingestor.Outbox(tmp_path / f"outbox-{demo}.sqlite3")
    ingestor.accept_snapshot(box, snapshot_dir(tmp_path))
    return {json.loads(row["body"])["payload"]["id"] for row in box.unpublished(100)}


def test_default_run_never_accepts_generated_events(tmp_path, monkeypatch):
    """Not filtered out downstream -- never turned into an envelope at all, so
    a generated row is not even owed by the outbox."""
    assert accepted_ids(tmp_path, False, monkeypatch) == {"real:1"}


def test_demo_run_accepts_both(tmp_path, monkeypatch):
    assert accepted_ids(tmp_path, True, monkeypatch) == {"real:1", "sample:1"}


# ------------------------------------------------- the consumer's boundary ----


def test_consumer_drops_a_generated_event_when_demo_mode_is_off(monkeypatch):
    """The same check at the write boundary, because "no generated row is
    stored by default" should hold for any producer, not only for ours."""
    monkeypatch.setattr(config, "DEMO_EVENTS", False)
    cur = FakeCursor()
    consumer.upsert_event(cur, schemas.Event(**event_row("sample:1", is_sample=True)))
    assert cur.statements == []


def test_consumer_stores_a_verified_event_when_demo_mode_is_off(monkeypatch):
    monkeypatch.setattr(config, "DEMO_EVENTS", False)
    cur = FakeCursor()
    consumer.upsert_event(cur, schemas.Event(**event_row("real:1", is_sample=False)))
    assert any("INSERT INTO events" in s for s in cur.statements)


def test_consumer_stores_a_generated_event_in_demo_mode(monkeypatch):
    monkeypatch.setattr(config, "DEMO_EVENTS", True)
    cur = FakeCursor()
    consumer.upsert_event(cur, schemas.Event(**event_row("sample:1", is_sample=True)))
    assert any("INSERT INTO events" in s for s in cur.statements)


# ------------------------------------------------------ leaving demo mode ----


def test_startup_purges_generated_events_when_demo_mode_is_off(monkeypatch):
    """`AOW_DEMO_EVENTS` describes the database, not just the run."""
    monkeypatch.setattr(config, "DEMO_EVENTS", False)
    conn = FakeConn()
    removed = consumer.enforce_event_mode(conn)
    assert removed == 3
    assert conn.cur.statements == ["DELETE FROM events WHERE is_sample"]
    assert conn.commits == 1


def test_startup_leaves_generated_events_alone_in_demo_mode(monkeypatch):
    monkeypatch.setattr(config, "DEMO_EVENTS", True)
    conn = FakeConn()
    assert consumer.enforce_event_mode(conn) == 0
    assert conn.cur.statements == []
    assert conn.commits == 0


# --------------------------------------------------------------- the flag ----


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_flag_accepts_the_usual_truthy_spellings(value, monkeypatch):
    monkeypatch.setenv("AOW_DEMO_EVENTS", value)
    assert _reread_flag() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  "])
def test_flag_is_off_for_everything_else(value, monkeypatch):
    monkeypatch.setenv("AOW_DEMO_EVENTS", value)
    assert _reread_flag() is False


def test_flag_is_off_when_unset(monkeypatch):
    monkeypatch.delenv("AOW_DEMO_EVENTS", raising=False)
    assert _reread_flag() is False


def _reread_flag() -> bool:
    """Re-evaluate exactly what config.py evaluates at import time, without
    reimporting the module (which would reopen the outbox and the pool)."""
    import os

    return os.environ.get("AOW_DEMO_EVENTS", "0").strip().lower() in {"1", "true", "yes", "on"}


def test_a_default_compose_run_has_no_demo_flag():
    """The flag lives only in compose.demo.yml. If it ever appears in
    compose.yml, demo mode stops being opt-in."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    assert "AOW_DEMO_EVENTS" not in (root / "compose.yml").read_text(encoding="utf-8")
    assert "AOW_DEMO_EVENTS" in (root / "compose.demo.yml").read_text(encoding="utf-8")


def test_the_committed_snapshot_keeps_them_apart():
    """The shipped files, not a fixture: every verified row in one file and
    every generated row in the other.

    The count is asserted against `data/events.seed.jsonl` rather than written
    here, because the verified set grows whenever another listing is checked;
    what must not drift is that the snapshot is that file and nothing else.
    `tests/unit/test_verified_events.py` is where each row is validated."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    verified = [
        json.loads(line)
        for line in (root / "data/snapshot/events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    generated = [
        json.loads(line)
        for line in (root / "data/snapshot/events.samples.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    seed = [
        json.loads(line)
        for line in (root / "data/events.seed.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [row["id"] for row in verified] == [row["id"] for row in seed]
    assert all(not row["is_sample"] for row in verified)
    assert not any(row["title"].startswith("Sample: ") for row in verified)
    assert generated and all(row["is_sample"] for row in generated)
    assert all(row["title"].startswith("Sample: ") for row in generated)


def test_datetimes_in_the_fixture_are_real():
    """Guards the fixture itself: a malformed starts_at would make every
    consumer assertion above vacuous."""
    event = schemas.Event(**event_row("real:1", is_sample=False))
    assert event.starts_at == datetime(2026, 9, 25, 20, 0, tzinfo=UTC)


# ------------------------------------------------ re-entering demo mode ----


def envelope_ids(rows, salt):
    return [e.message_id for e in ingestor.envelopes_from("event.record", rows, "snapshot", salt)]


def test_a_verified_event_keeps_the_same_message_id_across_runs():
    """The property the whole delivery guarantee rests on: replaying the same
    snapshot must not write the same record twice."""
    rows = [event_row("real:1", is_sample=False)]
    assert envelope_ids(rows, "") == envelope_ids(rows, "")


def test_a_generated_event_gets_a_new_message_id_per_boot():
    """Leaving demo mode deletes the samples, so coming back is a genuinely
    new delivery. Without the per-boot salt the outbox and `ingest_log` would
    -- correctly -- ignore the replay, and the samples would never return."""
    rows = [event_row("sample:1", is_sample=True)]
    first = envelope_ids(rows, "epoch-one")
    second = envelope_ids(rows, "epoch-two")
    assert first != second
    assert len(set(first + second)) == 2


def test_the_salt_is_the_only_difference():
    """A salted id must still be derived from the row, not random: two
    different sample rows under one epoch stay distinguishable, and the same
    row under one epoch is stable."""
    a = envelope_ids([event_row("sample:1", is_sample=True)], "epoch-one")
    b = envelope_ids([event_row("sample:2", is_sample=True)], "epoch-one")
    again = envelope_ids([event_row("sample:1", is_sample=True)], "epoch-one")
    assert a != b
    assert a == again


def test_re_entering_demo_mode_re_accepts_the_samples(tmp_path, monkeypatch):
    """End to end over one outbox: a second demo boot owes the sample rows
    again, and owes the verified rows exactly once."""
    monkeypatch.setattr(config, "DEMO_EVENTS", True)
    box = ingestor.Outbox(tmp_path / "outbox.sqlite3")
    snapshot = snapshot_dir(tmp_path)

    monkeypatch.setattr(ingestor, "DEMO_EPOCH", "boot-one")
    ingestor.accept_snapshot(box, snapshot)
    monkeypatch.setattr(ingestor, "DEMO_EPOCH", "boot-two")
    ingestor.accept_snapshot(box, snapshot)

    owed = [json.loads(row["body"])["payload"]["id"] for row in box.unpublished(100)]
    assert owed.count("sample:1") == 2
    assert owed.count("real:1") == 1
