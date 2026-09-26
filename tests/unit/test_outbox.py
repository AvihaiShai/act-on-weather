"""The outbox: the point from which a record is promised to survive (M11).

These tests pin the two properties the README claims: accepting the same
message twice does not duplicate it, and an accepted-but-unpublished record
is still there after the process dies.
"""

import sqlite3
import sys

import pytest

from services.common import config
from services.common import reconcile as reconcile_module
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


# ------------------------------------------------- the reconcile command line --
#
# `python -m services.common.reconcile --id <message_id>` is what the backup
# runbook tells an operator to type when they are chasing one record after a
# restore. A message_id belongs to exactly one of the three producers, so two
# of the three answers they get are "not here" and "not published yet" -- both
# ordinary answers, and both of which used to arrive as a Python traceback.


class _NeverQueried:
    """Stands in for the separate Postgres connection main() opens.

    Both paths below return before any id is looked up, so this exists to be
    closed -- and to fail loudly if that ever stops being true, since the unit
    suite runs with --network none and has no database to reach."""

    def execute(self, *args, **kwargs):
        raise AssertionError("this path must not query Postgres")

    def close(self):
        self.closed = True


def run_reconcile_cli(monkeypatch, path, message_id):
    """Drive main() against a real on-disk outbox and a reader that is never
    used. Returns the exit code."""
    monkeypatch.setenv("POSTGRES_READER_PASSWORD", "unused-by-these-paths")
    monkeypatch.setattr(config, "OUTBOX_PATH", path)
    monkeypatch.setattr(reconcile_module, "connect", lambda *a, **k: _NeverQueried())
    monkeypatch.setattr(sys, "argv", ["reconcile", "--id", message_id])
    with pytest.raises(SystemExit) as exit_info:
        reconcile_module.main()
    return exit_info.value.code


def test_the_cli_reports_an_id_this_producer_does_not_have(tmp_path, monkeypatch, capsys):
    """The commonest operator input, because they have to guess which of the
    three producers owns the id. One line on stderr, exit 3, nothing on stdout
    that a caller could mistake for a report, and no traceback."""
    path = tmp_path / "outbox.sqlite3"
    box = Outbox(path)
    box.accept(envelope())
    box.close()

    code = run_reconcile_cli(monkeypatch, path, "7b179b67-0000-0000-0000-000000000000")
    captured = capsys.readouterr()

    assert code == 3
    assert captured.out == ""
    assert captured.err.strip() == (
        "reconcile: 7b179b67-0000-0000-0000-000000000000 is absent from this producer outbox"
    )
    assert "Traceback" not in captured.err


def test_the_cli_reports_an_id_that_is_still_unpublished(tmp_path, monkeypatch, capsys):
    """The second answer, and the reassuring one: the record is accepted and on
    disk, and the producer's own publisher loop still owns it. Distinguished
    from "absent" in the message, because the operator's next move is different
    -- ask the next producer, versus do nothing at all."""
    path = tmp_path / "outbox.sqlite3"
    box = Outbox(path)
    message_id = box.accept(envelope())
    box.close()

    code = run_reconcile_cli(monkeypatch, path, message_id)
    captured = capsys.readouterr()

    assert code == 3
    assert captured.out == ""
    assert captured.err.strip() == (
        f"reconcile: {message_id} is still unpublished; the normal publisher owns it"
    )
    assert "Traceback" not in captured.err


def test_reconcile_the_library_still_raises_for_both(tmp_path):
    """The CLI swallows these two; the library must not. scripts/restore-state.sh
    and the tests above it read reconcile() directly, and a function that
    returned an empty report for an id it could not find would turn a missed
    replay into a silent success."""
    box = Outbox(tmp_path / "outbox.sqlite3")
    message_id = box.accept(envelope())
    with pytest.raises(reconcile_module.NotReplayable, match="absent from this producer"):
        reconcile(box, lambda ids: set(), message_id="no-such-id")
    with pytest.raises(reconcile_module.NotReplayable, match="still unpublished"):
        reconcile(box, lambda ids: set(), message_id=message_id)
    # Still a ValueError, so anything that caught the old type keeps working.
    assert issubclass(reconcile_module.NotReplayable, ValueError)
