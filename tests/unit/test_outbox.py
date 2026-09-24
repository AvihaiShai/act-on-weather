"""The outbox: the point from which a record is promised to survive (M11).

These tests pin the two properties the README claims: accepting the same
message twice does not duplicate it, and an accepted-but-unpublished record
is still there after the process dies.
"""

import sqlite3

import pytest

from services.common import config
from services.common.envelope import Envelope
from services.common.outbox import Outbox
from services.common.reconcile import reconcile


def envelope(city="rome"):
    return Envelope.create(
        config.RK_WEATHER,
        {
            "city_id": city,
            "forecast_date": "2026-09-24",
            "provider": "open-meteo",
            "as_of": "2026-09-23T18:00:00+00:00",
        },
        source="ingestor",
        observed_at="2026-09-23T18:00:00+00:00",
        city=city,
    )


def test_accept_then_publish(tmp_path):
    box = Outbox(tmp_path / "outbox.sqlite3")
    message_id = box.accept(envelope())
    assert box.counts() == {"total": 1, "pending": 1}

    (row,) = list(box.unpublished())
    assert row["message_id"] == message_id
    box.mark_published(row["seq"])

    assert box.counts() == {"total": 1, "pending": 0}
    assert list(box.unpublished()) == []


def test_accepting_the_same_message_twice_is_a_no_op(tmp_path):
    """A producer that retries after a crash must not double-publish."""
    box = Outbox(tmp_path / "outbox.sqlite3")
    env = envelope()
    box.accept(env)
    box.accept(env)
    assert box.counts()["total"] == 1


def test_unpublished_rows_survive_the_process(tmp_path):
    """The broker-down drill: accept, die, come back, the record is still owed."""
    path = tmp_path / "outbox.sqlite3"
    box = Outbox(path)
    message_id = box.accept(envelope())
    box.close()

    reopened = Outbox(path)
    assert reopened.counts() == {"total": 1, "pending": 1}
    assert reopened.status_of(message_id)["published_at"] is None


def test_publish_order_is_acceptance_order(tmp_path):
    box = Outbox(tmp_path / "outbox.sqlite3")
    ids = [box.accept(envelope(city)) for city in ("rome", "london", "lisbon")]
    assert [r["message_id"] for r in box.unpublished()] == ids


def test_failure_records_the_error_and_keeps_the_row_pending(tmp_path):
    box = Outbox(tmp_path / "outbox.sqlite3")
    message_id = box.accept(envelope())
    (row,) = list(box.unpublished())
    box.mark_failed(row["seq"], "broker unreachable")

    status = box.status_of(message_id)
    assert status["published_at"] is None
    assert status["attempts"] == 1
    assert "broker unreachable" in status["last_error"]
    assert box.counts()["pending"] == 1


def test_accept_many_is_atomic(tmp_path):
    box = Outbox(tmp_path / "outbox.sqlite3")
    ids = box.accept_many([envelope("rome"), envelope("london")])
    assert len(ids) == 2
    assert box.counts()["pending"] == 2


def test_reconcile_replays_only_published_missing_original_id(tmp_path):
    box = Outbox(tmp_path / "outbox.sqlite3")
    missing, stored = envelope("rome"), envelope("london")
    for item in (missing, stored):
        box.accept(item)
    for row in box.unpublished():
        box.mark_published(row["seq"])
    sent = []

    def publish(routing_key, body, message_id):
        sent.append((routing_key, body, message_id))

    report = reconcile(box, lambda ids: {stored.message_id}, publish)
    assert report["missing_ids"] == [missing.message_id]
    assert report["stored"] == 1
    assert report["replayed"] == 1
    assert sent == [(missing.routing_key, missing.to_bytes(), missing.message_id)]
    assert box.status_of(missing.message_id)["published_at"] is not None


def test_reconcile_does_not_replay_already_stored_id(tmp_path):
    box = Outbox(tmp_path / "outbox.sqlite3")
    item = envelope()
    box.accept(item)
    box.mark_published(next(box.unpublished())["seq"])
    sent = []
    report = reconcile(box, lambda ids: {item.message_id}, lambda *args: sent.append(args))
    assert report["stored"] == 1
    assert report["replayed"] == 0
    assert sent == []


def test_readonly_outbox_never_creates_a_missing_volume(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        Outbox(path, readonly=True)
    assert not path.exists()


def test_reconcile_refuses_mismatched_envelope(tmp_path):
    box = Outbox(tmp_path / "outbox.sqlite3")
    item = envelope()
    box.accept(item)
    seq = next(box.unpublished())["seq"]
    box.conn.execute("UPDATE outbox SET body = ? WHERE seq = ?", (envelope().to_bytes(), seq))
    box.mark_published(seq)
    sent = []
    with pytest.raises(ValueError, match="mismatch"):
        reconcile(box, lambda ids: set(), lambda *args: sent.append(args))
    assert sent == []
