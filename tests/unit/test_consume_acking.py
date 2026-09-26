"""What the consumer does with a message it could not process.

`rabbit.consume` makes the single decision the no-data-loss claim rests on,
once per delivery:

  * the handler returned        -> `basic_ack`, the message is done
  * the handler raised `Poison` -> `basic_nack(requeue=False)`, dead-lettered
  * the handler raised anything -> `basic_nack(requeue=True)`, try again

Only the middle branch is a judgement call, and it is the one with no other
safety net. Getting it backwards does not fail loudly: a poison message that
requeues instead of dead-lettering is caught only by the queue's
`x-delivery-limit`, five redeliveries later, and never appears in the DLQ that
`tools/redrive.py` and the README's redrive path operate on. A retryable
failure that acks instead is worse and quieter -- the record is gone.

No test in this repository imported `services/common/rabbit.py` before this
file. The CI outage drills exercise the retry branch through a real broker,
but nothing anywhere exercises the poison branch, and the drills need Docker.
These are pure unit tests against a scripted channel.
"""

from __future__ import annotations

import pytest

from services.common import config, rabbit


class Channel:
    """A pika channel that replays a script and records every decision."""

    def __init__(self, deliveries):
        self._deliveries = list(deliveries)
        self.calls: list[tuple] = []
        self.qos = None
        self.declared = False

    def basic_qos(self, prefetch_count):
        self.qos = prefetch_count

    def consume(self, queue, inactivity_timeout=None):
        assert queue == config.QUEUE
        yield from self._deliveries
        # Ending the generator returns control to `consume`'s reconnect loop,
        # which is where the test's sentinel stops it.
        raise rabbit.AMQPError("scripted end of stream")

    def basic_ack(self, tag):
        self.calls.append(("ack", tag))

    def basic_nack(self, tag, requeue):
        self.calls.append(("nack", tag, requeue))


class Method:
    def __init__(self, tag, routing_key=config.RK_WEATHER):
        self.delivery_tag = tag
        self.routing_key = routing_key


class Props:
    def __init__(self, message_id):
        self.message_id = message_id


class Connection:
    def __init__(self, channel):
        self._channel = channel

    def channel(self):
        return self._channel

    def close(self):
        pass


class StopTheLoop(Exception):
    """Ends `consume`'s `while True` on its second connection attempt."""


def _drive(monkeypatch, deliveries, handler):
    """Run one pass of `consume` over a scripted set of deliveries."""
    channel = Channel(deliveries)
    attempts = []

    def connect(name=None):
        attempts.append(name)
        if len(attempts) > 1:
            raise StopTheLoop
        return Connection(channel)

    monkeypatch.setattr(rabbit, "connect", connect)
    monkeypatch.setattr(rabbit, "declare", lambda ch: setattr(ch, "declared", True))
    # The retry branch sleeps a second so a downstream outage cannot be spun
    # on. Real here would only make the suite slower.
    monkeypatch.setattr(rabbit.time, "sleep", lambda _s: None)

    with pytest.raises(StopTheLoop):
        rabbit.consume(handler)
    return channel


def test_a_handled_message_is_acked(monkeypatch):
    channel = _drive(monkeypatch, [(Method(1), Props("m-1"), b"{}")], lambda *_a: None)
    assert channel.calls == [("ack", 1)]


def test_a_poison_message_is_dead_lettered_and_never_requeued(monkeypatch):
    """The branch with no other safety net.

    `requeue=True` here would send an unprocessable message back to the queue
    it just failed out of. Nothing would notice until `x-delivery-limit`
    dead-lettered it five deliveries later, and the DLQ the redrive tool reads
    would be empty in the meantime.
    """

    def poisoned(*_args):
        raise rabbit.Poison("unknown routing key")

    channel = _drive(monkeypatch, [(Method(7), Props("m-7"), b"not json")], poisoned)

    assert channel.calls == [("nack", 7, False)]


def test_a_failed_message_is_requeued(monkeypatch):
    """A database outage must leave the message on the queue.

    This is the branch the CI drills cover against a real Postgres. It is
    repeated here because the drills need Docker, and because an `ack` in this
    branch is the one mistake that loses a record outright.
    """

    def unavailable(*_args):
        raise RuntimeError("connection to server was lost")

    channel = _drive(monkeypatch, [(Method(3), Props("m-3"), b"{}")], unavailable)

    assert channel.calls == [("nack", 3, True)]


def test_poison_is_classified_before_the_general_failure(monkeypatch):
    """`Poison` is an `Exception`; ordering the handlers wrongly hides it.

    Asserted with a subclass, because that is what an added error type would
    be, and because a bare `Poison` would still pass if the two `except`
    clauses were swapped and the general one happened to be tried first.
    """

    class Unstorable(rabbit.Poison):
        pass

    def poisoned(*_args):
        raise Unstorable("schema rejected the payload")

    channel = _drive(monkeypatch, [(Method(9), Props("m-9"), b"{}")], poisoned)

    assert channel.calls == [("nack", 9, False)]


def test_one_bad_message_does_not_stop_the_ones_behind_it(monkeypatch):
    """The queue keeps moving: each delivery is judged on its own."""
    outcomes = {"a": None, "b": rabbit.Poison("bad"), "c": RuntimeError("db down"), "d": None}

    def handler(_key, body, _mid):
        raised = outcomes[body.decode()]
        if raised is not None:
            raise raised

    deliveries = [(Method(i), Props(f"m-{k}"), k.encode()) for i, k in enumerate(outcomes, start=1)]
    channel = _drive(monkeypatch, deliveries, handler)

    assert channel.calls == [
        ("ack", 1),
        ("nack", 2, False),
        ("nack", 3, True),
        ("ack", 4),
    ]


def test_an_idle_tick_is_neither_acked_nor_nacked(monkeypatch):
    """`inactivity_timeout` yields `(None, None, None)` to keep heartbeats up.

    Treating that as a delivery would call `basic_ack(None)` on an empty queue.
    """
    calls = []
    channel = _drive(
        monkeypatch,
        [(None, None, None), (Method(2), Props("m-2"), b"{}")],
        lambda *a: calls.append(a),
    )

    assert channel.calls == [("ack", 2)]
    assert len(calls) == 1, "the idle tick must not reach the handler"


def test_the_handler_receives_the_routing_key_and_message_id(monkeypatch):
    seen = []
    _drive(
        monkeypatch,
        [(Method(1, config.RK_RECOMMENDATION_REQUEST), Props("m-42"), b"{}")],
        lambda *a: seen.append(a),
    )

    assert seen == [(config.RK_RECOMMENDATION_REQUEST, b"{}", "m-42")]


def test_a_delivery_without_a_message_id_is_still_handled(monkeypatch):
    """`props.message_id` is absent on anything not published by this system.

    It must reach the handler as `None` and be classified normally, rather
    than raising inside `consume` -- an `AttributeError` there is caught by
    nothing and drops the connection.
    """

    class Bare:
        pass

    channel = _drive(monkeypatch, [(Method(4), Bare(), b"{}")], lambda *_a: None)

    assert channel.calls == [("ack", 4)]


def test_the_queue_dead_letters_rather_than_redelivering_forever():
    """The declaration behind the poison branch.

    `basic_nack(requeue=False)` only reaches a DLQ because the queue names a
    dead-letter exchange, and `x-delivery-limit` is what stops a message that
    keeps failing the retry branch from being redelivered without end.

    The numbers are pinned rather than bounded. A range would accept the
    regression: `x-delivery-limit` is a documented property of this stack, and
    raising it turns a poison message into a long stall while lowering it
    dead-letters records a transient outage would have redelivered
    successfully. Either is a decision, and a decision belongs in a diff.
    """
    assert rabbit.QUEUE_ARGS["x-dead-letter-exchange"] == config.DLX
    assert rabbit.QUEUE_ARGS["x-delivery-limit"] == 5
    assert rabbit.QUEUE_ARGS["x-queue-type"] == "quorum"


def test_deliveries_are_prefetched_in_a_bounded_batch(monkeypatch):
    """Unbounded prefetch hands the whole backlog to one consumer.

    Every message in flight is one that is not available to another consumer
    and must be redelivered if this one dies mid-batch.

    Pinned to the value the consumer actually sets, for the same reason as the
    delivery limit above: a bound would let it drift to 1, which serialises the
    stack, or to the whole backlog, which is the unbounded case this exists to
    forbid. Neither should be able to happen without somebody writing it down.
    """
    channel = _drive(monkeypatch, [], lambda *_a: None)

    assert channel.declared, "the queue and its dead-letter binding are declared on connect"
    assert channel.qos == 10
