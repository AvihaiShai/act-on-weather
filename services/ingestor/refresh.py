"""Connected refresh (M12): renew the committed snapshot instead of letting it expire.

    docker compose -f compose.yml -f compose.connected.yml up -d ingestor
    docker compose exec ingestor python -m services.ingestor.refresh

It fetches a fresh forecast and accepts it into the *same* outbox the running
ingestor is draining, so the new days travel the identical path as everything
else: outbox -> queue -> consumer -> Postgres (M4). Nothing here writes to the
database.

Offline this command is the one thing that cannot work, by design. The system
then keeps answering from what it holds and refuses dates outside the stored
coverage window rather than guessing.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from ..common import config
from ..common.outbox import Outbox
from .main import accept_live_weather, load_cities

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s refresh %(message)s")
log = logging.getLogger("refresh")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch a fresh forecast into the outbox.")
    parser.add_argument("--days", type=int, default=int(os.environ.get("FORECAST_DAYS", "16")))
    parser.add_argument("--city", action="append", help="city slug; repeatable. Default: all.")
    args = parser.parse_args(argv)

    cities = load_cities(config.DATA_DIR / "cities.yml")
    if args.city:
        wanted = set(args.city)
        cities = [c for c in cities if c["slug"] in wanted]
        missing = wanted - {c["slug"] for c in cities}
        if missing:
            log.error("unknown city slug(s): %s", ", ".join(sorted(missing)))
            return 2

    box = Outbox(config.OUTBOX_PATH)
    accepted = accept_live_weather(box, cities, args.days)
    counts = box.counts()
    log.info(
        "accepted %d forecast days; outbox total=%d pending=%d",
        accepted,
        counts["total"],
        counts["pending"],
    )
    if accepted == 0:
        log.error("nothing accepted -- is this container attached to the egress network?")
        return 1
    log.info("the running ingestor will publish these within a few seconds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
