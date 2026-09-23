"""Accept an arbitrary record into the ingestor's outbox.

    docker compose exec ingestor python -m services.tools.inject \
        --routing-key weather.daily --payload '{"deliberately": "invalid"}'

This is a test instrument, not a production path, and it exists for one reason:
the poison-message drill has to be honest. A dead letter that was injected
straight into RabbitMQ would prove nothing about the guarantee, because the
guarantee is stated from the outbox onwards. This puts the bad record through
the *same* front door as every real one, so its `message_id` is genuinely in
the accepted set and can be followed to its terminal state -- quarantined in
`aow.dlq`, then stored after a redrive.

Nothing in the running system calls it.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from ..common import config
from ..common.envelope import Envelope
from ..common.outbox import Outbox


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routing-key", required=True)
    parser.add_argument("--payload", required=True, help="JSON object")
    parser.add_argument("--city", default=None)
    parser.add_argument("--source", default="drill")
    args = parser.parse_args(argv)

    payload = json.loads(args.payload)
    envelope = Envelope.create(
        args.routing_key,
        payload,
        source=args.source,
        observed_at=datetime.now(UTC),
        city=args.city,
    )
    box = Outbox(config.OUTBOX_PATH)
    box.accept(envelope)
    counts = box.counts()
    # Printed alone on stdout so a drill script can capture it directly.
    print(envelope.message_id)
    print(f"accepted; outbox total={counts['total']} pending={counts['pending']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
