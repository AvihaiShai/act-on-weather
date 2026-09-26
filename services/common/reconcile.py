"""Audit confirmed outbox envelopes against committed ingest_log IDs.

Run in each producer container. The default is read-only; --replay publishes
only IDs that a separate Postgres connection cannot see. The consumer's
ingest_log primary key makes a concurrent commit/redelivery harmless.

Exit codes of the command line:

  0  an audit (or a replay) ran; the JSON report is on stdout
  2  argparse rejected the arguments
  3  the --id given is not an envelope this producer can replay -- either it is
     absent from this producer's outbox, or it is still unpublished. Both are
     answers rather than failures: each producer has its own outbox, so an
     operator holding one message_id may have to ask all three in turn.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable

from . import config
from .db import connect
from .envelope import Envelope
from .outbox import Outbox
from .rabbit import Publisher

Lookup = Callable[[list[str]], set[str]]
Publish = Callable[[str, bytes, str], None]


class NotReplayable(ValueError):
    """The --id an operator named is not an envelope this producer can replay.

    A ValueError subclass so that callers which already catch ValueError keep
    working, and its own type so that main() can turn exactly these two cases
    into one line on stderr instead of a traceback. Everything else reconcile()
    raises -- an outbox row that does not match the envelope stored in it -- is
    corruption, and a traceback is the right answer to that.
    """


def reconcile(
    box: Outbox,
    stored_ids: Lookup,
    publish: Publish | None = None,
    *,
    message_id: str | None = None,
) -> dict:
    """Return an audit and optionally confirm replay of missing original bytes."""
    result = {"scanned": 0, "stored": 0, "missing": 0, "replayed": 0, "missing_ids": []}
    cursor = 0
    while True:
        if message_id is None:
            rows = box.published_after(cursor)
        else:
            row = box.published_id(message_id)
            rows = [row] if row is not None else []
        if not rows:
            break
        cursor = rows[-1]["seq"]
        ids = [row["message_id"] for row in rows]
        committed = stored_ids(ids)
        for row in rows:
            result["scanned"] += 1
            if row["message_id"] in committed:
                result["stored"] += 1
                continue
            # Refuse corrupt or mismatched outbox rows before doing any replay.
            envelope = Envelope.from_bytes(row["body"])
            if (
                envelope.message_id != row["message_id"]
                or envelope.routing_key != row["routing_key"]
            ):
                raise ValueError(f"outbox envelope mismatch at seq {row['seq']}")
            result["missing"] += 1
            result["missing_ids"].append(row["message_id"])
            if publish is not None:
                publish(row["routing_key"], row["body"], row["message_id"])
                result["replayed"] += 1
        if message_id is not None:
            break
    if message_id is not None and result["scanned"] == 0:
        status = box.status_of(message_id)
        if status is None:
            raise NotReplayable(f"{message_id} is absent from this producer outbox")
        raise NotReplayable(f"{message_id} is still unpublished; the normal publisher owns it")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", action="store_true", help="confirm-publish missing IDs")
    parser.add_argument("--id", dest="message_id", help="audit or replay one accepted ID")
    args = parser.parse_args()
    if args.replay and args.message_id is None:
        parser.error("--replay requires --id; audit all producers before selecting IDs")
    # Never create an empty replacement if an operator mounted the wrong or a
    # destroyed volume. Both audit and replay only read the original envelope.
    box = Outbox(config.OUTBOX_PATH, readonly=True)
    observer = connect(config.reader_dsn(), autocommit=True)
    publisher = Publisher(name="aow-reconcile") if args.replay else None
    try:

        def stored_ids(ids: list[str]) -> set[str]:
            rows = observer.execute(
                "SELECT message_id FROM ingest_log WHERE message_id = ANY(%s)", (ids,)
            ).fetchall()
            return {row["message_id"] for row in rows}

        try:
            report = reconcile(
                box,
                stored_ids,
                publisher.publish if publisher else None,
                message_id=args.message_id,
            )
        except NotReplayable as exc:
            # One line, not a traceback. An operator with a single message_id
            # has to guess which of the three producers owns it, so this is the
            # answer they will see twice out of three times, and a stack trace
            # at that moment reads like a broken tool.
            print(f"reconcile: {exc}", file=sys.stderr)
            raise SystemExit(3) from None
        report["mode"] = "replay" if args.replay else "audit"
        print(json.dumps(report, sort_keys=True))
    finally:
        if publisher is not None:
            publisher.close()
        observer.close()
        box.close()


if __name__ == "__main__":
    main()
