"""An older forecast, redelivered late, must not overwrite a newer one.

At-least-once delivery plus a requeue means the broker may hand the consumer
two refreshes of the same city-day in the wrong order. They carry different
`message_id`s, so `ingest_log`'s primary key -- the thing every other drill
leans on -- does not deduplicate them: both are new messages, and both are
stored. What protects the row is one line of SQL in `upsert_weather`:

    WHERE EXCLUDED.as_of > weather_daily.as_of

Nothing exercised it. Remove it, or weaken it to `>=`, and the whole suite
stays green including all five outage drills, because every one of those
replays the *same* message rather than an older one. The damage is not
limited to a stale row: `upsert_weather` returns whether the row changed, and
a "changed" answer re-runs `score_defaults`, which resets every stored
recommendation for that day and re-words them from the old numbers, and fires
the revision trigger. A stale forecast is copied forward as a new revision.

Drills 3 and 5 deliberately cause requeues, so the reordering this guards
against is a state this stack actually reaches.

Run inside the API container, which accepts through the same outbox a user
write uses:

    docker compose exec -T api python - < tests/integration/stale_forecast.py
"""

import time
from datetime import UTC, datetime, timedelta

from services.common import config
from services.common.db import connect
from services.common.envelope import Envelope
from services.common.outbox import Outbox

DEADLINE_S = 180


def _wait_for(observer, message_ids):
    deadline = time.monotonic() + DEADLINE_S
    while True:
        rows = observer.execute(
            "SELECT message_id FROM ingest_log WHERE message_id = ANY(%s)",
            (message_ids,),
        ).fetchall()
        seen = {row["message_id"] for row in rows}
        missing = [mid for mid in message_ids if mid not in seen]
        if not missing:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"not stored within {DEADLINE_S}s: {', '.join(missing)}")
        time.sleep(2)


def _row(observer, city_id, day):
    return observer.execute(
        "SELECT as_of, revision, temp_max_c FROM weather_daily"
        " WHERE city_id = %s AND forecast_date = %s",
        (city_id, day),
    ).fetchone()


box = Outbox(config.OUTBOX_PATH)

with connect(config.reader_dsn(), autocommit=True) as observer:
    seed = observer.execute(
        "SELECT city_id, forecast_date, as_of, provider FROM weather_daily"
        " ORDER BY city_id, forecast_date LIMIT 1"
    ).fetchone()
    assert seed, "no stored weather to work from"
    city_id, day = seed["city_id"], seed["forecast_date"]

    # Both stamps are ahead of whatever is stored, so the first one is
    # guaranteed to land and the test is about the order of the two, not about
    # the seed row.
    newer = datetime.now(UTC) + timedelta(hours=2)
    older = newer - timedelta(hours=1)

    def envelope(as_of, temp_max):
        return Envelope.create(
            config.RK_WEATHER,
            {
                "city_id": city_id,
                "forecast_date": day.isoformat(),
                "provider": seed["provider"],
                "temp_max_c": temp_max,
                "temp_min_c": 10.0,
                "precip_mm": 0.0,
                "as_of": as_of.isoformat(),
            },
            source="stale-forecast-drill",
            observed_at=as_of,
            city=city_id,
        )

    fresh = envelope(newer, 30.0)
    stale = envelope(older, -7.5)

    # Accepted in the order the broker would redeliver them: the current
    # refresh first, then a requeued older one arriving behind it.
    box.accept(fresh)
    _wait_for(observer, [fresh.message_id])
    after_fresh = _row(observer, city_id, day)
    assert after_fresh["temp_max_c"] == 30.0, f"the newer forecast did not land: {after_fresh}"

    box.accept(stale)
    _wait_for(observer, [stale.message_id])
    after_stale = _row(observer, city_id, day)

# The stale message is stored -- it was delivered and acked, and that is
# correct. What must not have happened is the row moving backwards.
assert after_stale["as_of"] == after_fresh["as_of"], (
    f"an older forecast overwrote a newer one: as_of went "
    f"{after_fresh['as_of']} -> {after_stale['as_of']}"
)
assert (
    after_stale["temp_max_c"] == 30.0
), f"stale values were written: temp_max_c is {after_stale['temp_max_c']}, expected 30.0"
assert after_stale["revision"] == after_fresh["revision"], (
    f"a no-op upsert still bumped the revision: "
    f"{after_fresh['revision']} -> {after_stale['revision']}"
)
print(
    f"PASS: {city_id} {day} kept as_of {after_fresh['as_of']} and revision "
    f"{after_fresh['revision']} after an older forecast was redelivered behind it"
)
