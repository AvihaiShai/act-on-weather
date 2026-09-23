"""Redrive the dead-letter queue.

    docker compose exec consumer python -m services.tools.redrive

A message lands in `aow.dlq` when it can never succeed as it stands: a payload
that fails validation, an unknown routing key, or one that exhausted the
queue's `x-delivery-limit`. Quarantining it is the point -- a poison message
must not block the ones behind it -- but quarantine is a holding state, not a
terminal one.

This is the operator's way out: fix the cause, then move the messages back onto
`aow.events` so they take the normal path again. It moves them one at a time
under publisher confirms and acks each only after its republish is confirmed,
so an interrupted redrive loses nothing and at worst repeats a message the
consumer will recognise by `message_id`.
"""

from __future__ import annotations

import argparse
import logging
import sys

from ..common import config
from ..common.envelope import Envelope
from ..common.rabbit import connect, declare

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s redrive %(message)s")
log = logging.getLogger("redrive")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Move dead letters back onto the exchange.")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--list", action="store_true", help="show what is quarantined without moving anything"
    )
    args = parser.parse_args(argv)

    conn = connect(name="aow-redrive")
    channel = conn.channel()
    declare(channel)
    channel.confirm_delivery()

    moved = skipped = listed = 0
    # A redriven message whose cause has not been fixed fails again and is back
    # in the DLQ within milliseconds -- fast enough for this same loop to pick
    # it up again. Without this set, one unfixable message is redriven until the
    # limit is reached. Each message gets exactly one attempt per run.
    seen: set[str] = set()
    # In --list mode nothing may be acked, so every message has to be held
    # unacked until the end. Nacking as we go would put each one straight back
    # at the head of the queue and we would read it again, forever.
    held: list[int] = []
    try:
        for _ in range(args.limit):
            method, props, body = channel.basic_get(config.DLQ, auto_ack=False)
            if method is None:
                break

            message_id = getattr(props, "message_id", None)
            try:
                envelope = Envelope.from_bytes(body)
                routing_key = envelope.routing_key
                message_id = envelope.message_id
                reason = None
            except Exception as exc:  # noqa: BLE001 - unparseable stays quarantined
                routing_key = getattr(method, "routing_key", "?")
                reason = str(exc)

            if message_id and message_id in seen:
                log.info("already handled %s this run; stopping", message_id)
                held.append(method.delivery_tag)
                break
            if message_id:
                seen.add(message_id)

            if args.list:
                log.info(
                    "quarantined: %s %s%s",
                    routing_key,
                    message_id,
                    f"  (unparseable: {reason})" if reason else "",
                )
                held.append(method.delivery_tag)
                listed += 1
                continue

            if reason is not None:
                log.warning("cannot parse %s (%s); leaving it quarantined", message_id, reason)
                held.append(method.delivery_tag)
                skipped += 1
                continue

            channel.basic_publish(
                exchange=config.EXCHANGE,
                routing_key=routing_key,
                body=body,
                properties=props,
                mandatory=True,
            )
            # Acked only after the republish was confirmed: if this process dies
            # in between, the message is still in the DLQ, never nowhere.
            channel.basic_ack(method.delivery_tag)
            moved += 1
            log.info("redriven %s %s", routing_key, message_id)

        for tag in held:
            channel.basic_nack(tag, requeue=True)
    finally:
        conn.close()

    if args.list:
        log.info("%d message(s) quarantined; none moved", listed)
    else:
        log.info("redriven %d, left quarantined %d", moved, skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
