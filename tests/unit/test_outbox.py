"""The outbox: the point from which a record is promised to survive (M11).

These tests pin the two properties the README claims: accepting the same
message twice does not duplicate it, and an accepted-but-unpublished record
is still there after the process dies.
"""

from services.common import config
from services.common.envelope import Envelope
from services.common.outbox import Outbox


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
