"""An older forecast, redelivered late, must not overwrite a newer one.

At-least-once delivery plus a requeue means the broker may hand the consumer
two refreshes of the same city-day in the wrong order. They carry different
`message_id`s, so `ingest_log`'s primary key -- the thing every other drill
leans on -- does not deduplicate them: both are new messages, and both are
stored. What protects the row is one line of SQL in `upsert_weather`:

    WHERE EXCLUDED.as_of > weather_daily.as_of

Nothing exercised it. Remove it and the whole suite stays green including all
five outage drills, because every one of those replays the *same* message
rather than an older one. The damage is not limited to a stale row:
`upsert_weather` returns whether the row changed, and a "changed" answer
re-runs `score_defaults`, which resets every stored recommendation for that day
and re-words them from the old numbers, and fires the revision trigger. A stale
forecast is copied forward as a new revision.

Three messages, because the comparison has two ways to be wrong and they need
different evidence:

  older   rejected by both `>` and `>=`, so it catches the guard being
          deleted, which is the regression that loses data outright.
  equal   rejected by `>` and accepted by `>=`, so it is the only one that
          catches the guard being weakened by one character. A re-delivered
          refresh carries the same `as_of` as the row it already wrote, so
          `>=` would rewrite and re-score that day on every redelivery --
          quietly, and for as long as the queue kept moving.

Drills 3 and 5 deliberately cause requeues, so both orders this guards against
are states this stack actually reaches.

**The city-day is synthetic, and that is deliberate.** An earlier version drove
a real row -- the first one `ORDER BY city_id, forecast_date` returned -- and
that is not recoverable. The upsert sets every column from the envelope, so
the omitted ones (wind, UV, sunshine, the weather code, the source URL) were
NULLed on a collected row; the temperatures became invented values; and the
guard being tested is precisely what stops the true row being written back
afterwards. It also left that day's recommendations re-scored from the
wreckage. It survived only because the seed happened to be lisbon while the
M11 drills below use rome, which is an accident of alphabetical order rather
than a property anyone chose.

So this writes a date no snapshot reaches instead. Nothing collected is
touched, the row is created rather than clobbered, and the guard is exercised
identically: the first message inserts, and the two behind it take the
ON CONFLICT path this file exists to pin. It does extend the reported coverage
window in the disposable CI project, which nothing later in
`scripts/ci-integration.sh` reads.

Run inside the API container, which accepts through the same outbox a user
write uses:

    docker compose exec -T api python - < tests/integration/stale_forecast.py
"""

import time
from datetime import UTC, date, datetime, timedelta

from services.common import config
from services.common.db import connect
from services.common.envelope import Envelope
from services.common.outbox import Outbox

DEADLINE_S = 180

# Far outside any forecast horizon, so it cannot collide with a collected row
# now or after a refresh, and so a human reading the table knows on sight that
# nobody measured this.
SYNTHETIC_DAY = date(2099, 12, 31)


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
        "SELECT city_id, provider FROM weather_daily ORDER BY city_id, forecast_date LIMIT 1"
    ).fetchone()
    assert seed, "no stored weather to work from"
    # A real city, because `weather_daily.city_id` references `cities`, and a
    # real provider string, so the row looks like the rows around it. The date
    # is the only synthetic part.
    city_id, day = seed["city_id"], SYNTHETIC_DAY

    # Ahead of the clock as well as of anything stored, so the first message is
    # guaranteed to land and the drill is about the order of the three rather
    # than about what was there before.
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
    # Same as_of as `fresh`, different values: this is what a plain redelivery
    # of an already-written refresh looks like from the consumer's side.
    repeat = envelope(newer, -7.5)

    # Accepted in the order the broker would redeliver them: the current
    # refresh first, then a requeued older one arriving behind it.
    box.accept(fresh)
    _wait_for(observer, [fresh.message_id])
    after_fresh = _row(observer, city_id, day)
    assert after_fresh["temp_max_c"] == 30.0, f"the newer forecast did not land: {after_fresh}"

    box.accept(stale)
    _wait_for(observer, [stale.message_id])
    after_stale = _row(observer, city_id, day)

    box.accept(repeat)
    _wait_for(observer, [repeat.message_id])
    after_repeat = _row(observer, city_id, day)

# Every message is stored -- each was delivered and acked, and that is correct.
# What must not have happened is the row moving.
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

# The one-character case. `>=` passes every assertion above and fails here.
assert after_repeat["temp_max_c"] == 30.0, (
    f"a redelivery with the same as_of rewrote the row: temp_max_c is "
    f"{after_repeat['temp_max_c']}, expected 30.0 -- the guard is `>=`, not `>`"
)
assert after_repeat["revision"] == after_fresh["revision"], (
    f"a redelivery with the same as_of bumped the revision: "
    f"{after_fresh['revision']} -> {after_repeat['revision']}, and re-scored the day with it"
)
print(
    f"PASS: {city_id} {day} kept as_of {after_fresh['as_of']} and revision "
    f"{after_fresh['revision']} after an older forecast and an equally-dated "
    f"redelivery arrived behind it"
)
