"""A saved trip with no scoring as-of survives the whole path to the database.

Run inside the API container:

    docker compose exec -T api python - < tests/integration/itinerary_without_as_of.py

`POST /itineraries` accepts a body with no `as_of` and stores NULL, because the
handler must not read the database to find the value and must not invent one
either. Every part of that was asserted somewhere, and none of it was true end
to end: migration 008 drops the `NOT NULL` from `itineraries.as_of`, but the
`migrate` service's psql command never ran the file, so no database this
repository has ever started had the constraint dropped.

Nothing would have caught it. The unit tests hand `upsert_itinerary` a fake
cursor, which accepts anything. `ingest_log` is written in the same transaction
as the business row, so a rejected INSERT leaves no trace there either -- the
message is simply requeued. And NotNullViolation is neither `Poison` nor
`OperationalError`: it takes the consumer's generic branch, so the write is not
dead-lettered with a reason, it is redelivered until `x-delivery-limit` drops
it in the DLQ five deliveries later. The API says 202 and the record quietly
disappears, which is the exact failure the compatibility decision exists to
prevent.

So this drill asserts the two halves that only a real Postgres can settle:

  1. the row is committed, seen by a separate reader rather than by the API
     that accepted it, and
  2. `as_of` is NULL on it -- not now(), not the API's clock, not the coverage
     window. A substituted timestamp would pass every check in half 1 while
     putting a provenance on a stored record that nothing scored the plan
     against.

The itinerary is left in place. It is user data in a disposable CI project,
removing it would need the delete path this drill is not about, and a stray
saved trip changes nothing any later step reads.
"""

import json
import time
import urllib.request

from services.common import config
from services.common.db import connect

BASE = "http://127.0.0.1:8000"
DEADLINE_S = 180

with connect(config.reader_dsn(), autocommit=True) as observer:
    city = observer.execute("SELECT id FROM cities ORDER BY id LIMIT 1").fetchone()
    assert city, "no cities seeded"
    city_id = city["id"]

    # Deliberately no `as_of`: this is the shape a caller written before the
    # field existed sends, and the shape the API promises to accept.
    body = json.dumps(
        {
            "city": city_id,
            "title": "a plan whose caller did not say what it was scored against",
            "start_date": "2026-09-25",
            "end_date": "2026-09-26",
            "days": [],
        }
    ).encode()
    request = urllib.request.Request(
        f"{BASE}/itineraries", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 202, f"the API refused a body with no as_of: {response.status}"
        accepted = json.load(response)
    message_id, itinerary_id = accepted["message_id"], accepted["id"]

    deadline = time.monotonic() + DEADLINE_S
    while True:
        row = observer.execute(
            "SELECT as_of FROM itineraries WHERE id = %s", (itinerary_id,)
        ).fetchone()
        if row is not None:
            break
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"the itinerary was accepted as {message_id} but never committed within "
                f"{DEADLINE_S}s -- a NOT NULL on itineraries.as_of fails exactly like "
                f"this, silently, through the requeue path"
            )
        time.sleep(2)

    # The business row and the idempotency key are written in one transaction,
    # so a committed row without its ledger entry would mean the guarantee M11
    # rests on had come apart underneath this drill.
    ledger = observer.execute(
        "SELECT count(*) AS total FROM ingest_log WHERE message_id = %s", (message_id,)
    ).fetchone()
    assert ledger["total"] == 1, f"ingest_log holds {ledger['total']} rows for {message_id}"

assert row["as_of"] is None, (
    f"a caller that sent no as_of had one invented for it: {row['as_of']!r}. "
    f"A missing provenance can be read for what it is; this cannot."
)
print(f"PASS: {itinerary_id} was accepted with no as_of, committed, and stored NULL")
