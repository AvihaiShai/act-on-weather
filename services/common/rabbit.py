"""RabbitMQ topology, publishing and consuming.

The delivery story, in one place:

  * one durable topic exchange `aow.events`; one quorum queue `aow.ingest`
    bound to `#`, so every record kind takes the same path
  * the queue dead-letters to `aow.dlx` -> `aow.dlq`, and carries a
    `x-delivery-limit`, so a message that keeps failing is quarantined rather
    than looping forever
  * publishing uses publisher confirms and `mandatory=True`: an unroutable or
    unconfirmed message raises, the outbox row stays unpublished, and the
    publisher loop retries it
  * consuming acks manually, and only after the database transaction has
    committed

pika, synchronous, because a blocking connection per single-purpose process is
the version of this that can be read and defended line by line.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable

import pika
from pika.exceptions import AMQPError, NackError, UnroutableError

from . import config

log = logging.getLogger(__name__)

# pika logs every socket and channel event at INFO, which buries the lines that
# actually matter during a failure drill.
logging.getLogger("pika").setLevel(logging.WARNING)

QUEUE_ARGS = {
    "x-queue-type": "quorum",
    "x-dead-letter-exchange": config.DLX,
    # After this many delivery attempts the message is dead-lettered instead of
    # being requeued again. Without it, one poison message blocks the queue.
    "x-delivery-limit": 5,
}


def connect(url: str | None = None, *, name: str = "aow") -> pika.BlockingConnection:
    """Connect, retrying with backoff. Used on startup and after every drop."""
    url = url or config.rabbit_url()
    params = pika.URLParameters(url)
    params.client_properties = {"connection_name": name}
    params.heartbeat = 30
    params.blocked_connection_timeout = 60
    delay = 1.0
    while True:
        try:
            return pika.BlockingConnection(params)
        except AMQPError as exc:
            log.warning("rabbitmq unreachable (%s); retrying in %.0fs", exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)


def declare(channel: pika.adapters.blocking_connection.BlockingChannel) -> None:
    """Declare the whole topology. Idempotent, and run by every service on start.

    Every service declaring it means the stack comes up correctly whatever
    order the containers happen to start in.
    """
    channel.exchange_declare(config.EXCHANGE, exchange_type="topic", durable=True)
    channel.exchange_declare(config.DLX, exchange_type="fanout", durable=True)
    channel.queue_declare(config.DLQ, durable=True, arguments={"x-queue-type": "quorum"})
    channel.queue_bind(config.DLQ, config.DLX)
    channel.queue_declare(config.QUEUE, durable=True, arguments=QUEUE_ARGS)
    channel.queue_bind(config.QUEUE, config.EXCHANGE, routing_key="#")


class Publisher:
    """A confirm-mode channel that reconnects on its own."""

    def __init__(self, *, name: str = "aow-publisher"):
        self.name = name
        self.conn: pika.BlockingConnection | None = None
        self.channel = None

    def _ensure(self):
        if self.conn is not None and self.conn.is_open and self.channel.is_open:
            return self.channel
        self.conn = connect(name=self.name)
        self.channel = self.conn.channel()
        declare(self.channel)
        self.channel.confirm_delivery()
        return self.channel

    def publish(self, routing_key: str, body: bytes, message_id: str) -> None:
        """Publish one message, or raise.

        Raising is the point: the caller leaves the outbox row unpublished and
        tries again, which is how a broker outage becomes a delay instead of a
        loss.
        """
        channel = self._ensure()
        channel.basic_publish(
            exchange=config.EXCHANGE,
            routing_key=routing_key,
            body=body,
            properties=pika.BasicProperties(
                delivery_mode=2,  # persist to disk
                message_id=message_id,
                content_type="application/json",
            ),
            mandatory=True,  # no queue bound -> raises, never silently dropped
        )

    def close(self) -> None:
        try:
            if self.conn is not None and self.conn.is_open:
                self.conn.close()
        except AMQPError:
            pass


PublishError = (AMQPError, UnroutableError, NackError, OSError)


def consume(
    handler: Callable[[str, bytes, str | None], None], *, name: str = "aow-consumer"
) -> None:
    """Consume `aow.ingest` forever, acking only after `handler` returns.

    `handler(routing_key, body, message_id)` either returns (ack), raises
    `Poison` (dead-letter immediately, no requeue) or raises anything else
    (requeue and try again -- this is what makes a database outage survivable).
    """
    while True:
        conn = connect(name=name)
        try:
            channel = conn.channel()
            declare(channel)
            channel.basic_qos(prefetch_count=10)
            for method, props, body in channel.consume(config.QUEUE, inactivity_timeout=5):
                if method is None:
                    continue  # idle tick, keeps heartbeats flowing
                try:
                    handler(method.routing_key, body, getattr(props, "message_id", None))
                except Poison as exc:
                    log.error("poison message %s: %s", getattr(props, "message_id", "?"), exc)
                    channel.basic_nack(method.delivery_tag, requeue=False)
                except Exception as exc:  # noqa: BLE001 - deliberate catch-all
                    log.warning("redelivering %s: %s", getattr(props, "message_id", "?"), exc)
                    channel.basic_nack(method.delivery_tag, requeue=True)
                    time.sleep(1)  # do not spin on a downstream outage
                else:
                    channel.basic_ack(method.delivery_tag)
        except AMQPError as exc:
            log.warning("consumer connection lost (%s); reconnecting", exc)
        finally:
            with contextlib.suppress(AMQPError):
                conn.close()
        time.sleep(1)


class Poison(Exception):
    """The message will never succeed: dead-letter it rather than retry."""
