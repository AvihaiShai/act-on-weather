"""The consumer's broker callback routes every declared key and counts outcomes."""

from contextlib import nullcontext

import pytest
from prometheus_client import REGISTRY

from services.common import config
from services.common.envelope import Envelope
from services.consumer import main as consumer


class Cursor:
    def __init__(self, inserted=True):
        self.inserted = inserted
        self.writes = []

    def execute(self, sql, params):
        self.writes.append((sql, params))

    def fetchone(self):
        return {"message_id": "stored"} if self.inserted else None


class Connection:
    closed = False

    def __init__(self, cursor):
        self._cursor = cursor

    def transaction(self):
        return nullcontext()

    def cursor(self):
        return nullcontext(self._cursor)


def delivery(key):
    return Envelope.create(
        key,
        {"probe": True},
        source="unit-test",
        observed_at="2026-09-24T00:00:00+00:00",
        city="rome",
    ).to_bytes()


@pytest.mark.parametrize("key", sorted(config.ROUTING_KEYS))
def test_every_declared_key_reaches_its_handler(monkeypatch, key):
    cursor = Cursor()
    monkeypatch.setattr(consumer.pool, "_conn", Connection(cursor))
    payload = object()
    seen = []
    monkeypatch.setattr(consumer.schemas, "validate", lambda routed, raw: payload)
    if key == config.RK_WEATHER:
        monkeypatch.setattr(
            consumer, "upsert_weather", lambda cur, p: seen.append((cur, p)) or False
        )
    else:
        monkeypatch.setitem(consumer.HANDLERS, key, lambda cur, p: seen.append((cur, p)))

    consumer.handle(key, delivery(key), None)

    assert seen == [(cursor, payload)]
    assert len(cursor.writes) == 1  # idempotency write precedes dispatch


def test_duplicate_is_acked_without_dispatch(monkeypatch):
    cursor = Cursor(inserted=False)
    monkeypatch.setattr(consumer.pool, "_conn", Connection(cursor))
    monkeypatch.setattr(consumer.schemas, "validate", lambda key, raw: object())
    monkeypatch.setitem(
        consumer.HANDLERS,
        config.RK_FACT,
        lambda cur, payload: pytest.fail("duplicate reached business handler"),
    )
    consumer.handle(config.RK_FACT, delivery(config.RK_FACT), None)
    assert len(cursor.writes) == 1


def count(result):
    return (
        REGISTRY.get_sample_value(
            "aow_messages_consumed_total", {"routing_key": config.RK_FACT, "result": result}
        )
        or 0
    )


@pytest.mark.parametrize("outcome", ["stored", "duplicate", "rejected", "retry"])
def test_handle_counts_only_terminal_outcomes(monkeypatch, outcome):
    before = {name: count(name) for name in ("stored", "duplicate", "rejected")}

    def process(*_args):
        if outcome == "rejected":
            raise consumer.Poison("bad payload")
        if outcome == "retry":
            raise RuntimeError("database unavailable")
        return outcome

    monkeypatch.setattr(consumer, "process", process)
    if outcome in ("rejected", "retry"):
        with pytest.raises(consumer.Poison if outcome == "rejected" else RuntimeError):
            consumer.handle(config.RK_FACT, b"", None)
    else:
        consumer.handle(config.RK_FACT, b"", None)

    for name in before:
        assert count(name) == before[name] + (name == outcome)
