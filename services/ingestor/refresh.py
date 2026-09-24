"""Connected refresh (M12): renew the committed snapshot instead of letting it expire.

This is the *fetch* half of the operator refresh. The half that opens and --
more importantly -- closes the egress window is `scripts/refresh.sh`, which is
what an operator actually runs:

    docker compose -f compose.tools.yml run --rm refresh

Run directly, this module assumes the ingestor is already attached to egress
and does nothing about putting it back:

    docker compose exec ingestor python -m services.ingestor.refresh --json

It fetches a fresh forecast and accepts it into the *same* outbox the running
ingestor is draining, so the new days travel the identical path as everything
else: outbox -> queue -> consumer -> Postgres (M4). Nothing here writes to the
database.

`--json` prints one machine-readable report on stdout (logs stay on stderr) so
the wrapper can tell the operator which cities came back, what the new as-of
is, and exactly which message ids were accepted. A refresh that half worked --
three cities fetched, two refused by the provider -- must not look like a
success, which is why the per-city result, not a single total, is the output.

Offline this command is the one thing that cannot work, by design. The system
then keeps answering from what it holds and refuses dates outside the stored
coverage window rather than guessing.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

from ..common import config
from ..common.outbox import Outbox
from .main import accept_live_weather, load_cities

logging.basicConfig(
    level="INFO",
    format="%(asctime)s %(levelname)s refresh %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("refresh")

# Exit codes. The wrapper maps these onto its own, so they are part of the
# interface and are documented in the README.
OK = 0
FETCH_FAILED = 1
USAGE = 2


def _now() -> str:
    return datetime.now(UTC).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch a fresh forecast into the outbox.")
    parser.add_argument("--days", type=int, default=int(os.environ.get("FORECAST_DAYS", "16")))
    parser.add_argument("--city", action="append", help="city slug; repeatable. Default: all.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="print a machine-readable report on stdout (scripts/refresh.sh uses this).",
    )
    args = parser.parse_args(argv)

    cities = load_cities(config.DATA_DIR / "cities.yml")
    if args.city:
        wanted = set(args.city)
        cities = [c for c in cities if c["slug"] in wanted]
        missing = wanted - {c["slug"] for c in cities}
        if missing:
            log.error("unknown city slug(s): %s", ", ".join(sorted(missing)))
            return USAGE

    started_at = _now()
    box = Outbox(config.OUTBOX_PATH)
    results = accept_live_weather(box, cities, args.days)
    counts = box.counts()

    accepted = sum(r["accepted"] for r in results)
    failed = [r["city"] for r in results if not r["ok"]]
    report: dict[str, Any] = {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": _now(),
        "provider": results[0]["provider"] if results else None,
        "days_requested": args.days,
        "cities": results,
        "failed_cities": failed,
        "accepted": accepted,
        "message_ids": [mid for r in results for mid in r["message_ids"]],
        "outbox": counts,
        "ok": bool(results) and not failed,
    }

    for r in results:
        if r["ok"]:
            log.info(
                "%s: accepted %d day(s), %s..%s, as_of %s",
                r["city"],
                r["accepted"],
                r["first_date"],
                r["last_date"],
                r["as_of"],
            )
        else:
            log.error("%s: %s", r["city"], r["error"])
    log.info(
        "accepted %d forecast days across %d/%d cities; outbox total=%d pending=%d",
        accepted,
        len(results) - len(failed),
        len(results),
        counts["total"],
        counts["pending"],
    )

    if args.json:
        print(json.dumps(report, indent=2))

    if not report["ok"]:
        if accepted == 0:
            log.error("nothing accepted -- is this container attached to the egress network?")
        return FETCH_FAILED
    log.info("the running ingestor will publish these within a few seconds")
    return OK


if __name__ == "__main__":
    sys.exit(main())
