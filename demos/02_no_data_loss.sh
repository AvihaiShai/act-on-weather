#!/usr/bin/env bash
#
# M11: no data loss under temporary failure.
#
# Four drills, each one following a single accepted message_id to its terminal
# state. Row counts are deliberately NOT the assertion -- a lost record and a
# duplicated one cancel out in a count, and the guarantee is about a specific
# record, so a specific record is what gets traced.
#
# The guarantee, stated exactly:
#
#   From the moment a record is fsynced into a producer's outbox (on a named
#   volume), it reaches one of two terminal states: stored in Postgres, or
#   quarantined in aow.dlq awaiting a redrive. Consumer, broker and database
#   outages delay it; none of them lose it.
#
#   Not covered: a destroyed volume, a full disk, and external data that was
#   never accepted -- weather that was never fetched can be re-fetched while
#   connected, which the README says plainly.
#
source "$(dirname "$0")/lib.sh"
FAILED=0
db_down=0
restore_db() {
  if [ "$db_down" -eq 1 ]; then
    dc start postgres >/dev/null 2>&1 || true
  fi
}
cleanup_drill_rows() {
  restore_db
  # The requests prove delivery while the drill runs; they are not catalogue
  # activities and should not remain in the user's activity coverage view.
  # If the script exits during the database outage, wait for Postgres to be
  # ready again before removing the rows already accepted by earlier drills.
  for _ in $(seq 1 15); do
    if psql_q "DELETE FROM recommendations WHERE requested AND
        (city_id = 'lisbon' AND activity = 'drill_one_paddleboarding' OR
         city_id = 'rome' AND activity = 'drill_two_database_outage' OR
         city_id = 'reykjavik' AND activity = 'drill_three_aurora_watching')" >/dev/null 2>&1; then
      return
    fi
    sleep 2
  done
  note "Could not clean up drill recommendations; remove them before presenting the UI."
}
trap cleanup_drill_rows EXIT

hr "Setting the scene"
note "queue depth:   $(queue_depth aow.ingest)"
note "dead letters:  $(queue_depth aow.dlq)"
note "stored rows:   $(psql_q 'SELECT count(*) FROM ingest_log')"

# ---------------------------------------------------------------------------
hr "Drill 1 -- the consumer dies mid-flow"
note "Nothing is reading the queue. The record must wait, not vanish."
dc stop consumer >/dev/null 2>&1
note "consumer stopped"

MID1=$(curl -s -X POST "$API/recommendations" -H 'Content-Type: application/json' \
  -d '{"city":"lisbon","forecast_date":"'"$(psql_q 'SELECT min(forecast_date) FROM weather_daily')"'","activity":"drill one paddleboarding"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["message_id"])')
note "accepted message_id: $MID1"

# Wait for this record's own publish first, then read the queue. The other
# order was both the source of the flake and a weaker assertion than it looked:
# `queue_depth > 0` is satisfied by *any* message, so it could pass on somebody
# else's record while MID1 was still sitting unpublished in the API's outbox --
# and when it did, the publish check below lost the race and failed on a system
# that was working correctly.
if wait_published "$MID1" 30; then
  pass "the traced ID was published before the consumer restarted"
else
  fail "the traced ID never reached the broker during the consumer outage"
fi
# Once this record's own publish is confirmed and nothing is reading the queue,
# the message has nowhere else to be, so this is a single read rather than a
# poll: a zero here is a real finding, not a timing artefact.
DEPTH1="$(queue_depth aow.ingest)" || DEPTH1=''
case "$DEPTH1" in
  '' | *[!0-9]*)
    fail "could not read the aow.ingest depth while the consumer was stopped" ;;
  0)
    fail "the record was published but nothing is queued with the consumer stopped" ;;
  *)
    pass "the published record is waiting in the queue with no consumer (depth $DEPTH1)" ;;
esac
note "trace: $(trace "$MID1")"

dc start consumer >/dev/null 2>&1
note "consumer restarted"
if wait_stored "$MID1" 60; then
  pass "the record stored after the consumer came back"
else
  fail "the record never reached the database"
fi
note "trace: $(trace "$MID1")"

# ---------------------------------------------------------------------------
hr "Drill 2 -- the database dies while a record is accepted"
note "The API accepts into its durable outbox while Postgres is unavailable."
DAY2=$(psql_q 'SELECT min(forecast_date) FROM weather_daily')
dc stop postgres >/dev/null 2>&1
db_down=1
note "postgres stopped"

MID2=$(curl -fsS -X POST "$API/recommendations" -H 'Content-Type: application/json' \
  -d '{"city":"rome","forecast_date":"'"$DAY2"'","activity":"drill two database outage"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["message_id"])')
note "accepted with the database down: $MID2"
dc exec -T api python -c "
from services.common import config
from services.common.outbox import Outbox
row = Outbox(config.OUTBOX_PATH).status_of('$MID2')
assert row is not None, 'accepted ID missing from durable outbox'
print('   durable outbox row:', row['message_id'])
"

dc start postgres >/dev/null 2>&1
db_down=0
note "postgres restarted; waiting for the same ID to commit"
if wait_stored "$MID2" 120; then
  pass "the accepted record stored after the database came back"
else
  fail "the accepted record never reached the database"
fi
# Read from a fresh psql session, then restart the consumer and read again.
# This catches an ACK following a savepoint release instead of a real commit.
if [ "$(psql_q "SELECT count(*) FROM ingest_log WHERE message_id = '$MID2'")" = 1 ]; then
  pass "a separate database session sees the committed ID"
else
  fail "the ID is not committed for a separate reader"
fi
dc restart consumer >/dev/null 2>&1
if [ "$(psql_q "SELECT count(*) FROM ingest_log WHERE message_id = '$MID2'")" = 1 ]; then
  pass "the ID survived consumer restart exactly once"
else
  fail "the ID was lost or duplicated after consumer restart"
fi
note "trace: $(trace "$MID2")"

# ---------------------------------------------------------------------------
hr "Drill 3 -- the broker dies while records are being accepted"
note "This is the drill the outbox exists for: acceptance keeps working with"
note "nowhere to publish to, and the backlog replays when the broker returns."
dc stop rabbitmq >/dev/null 2>&1
note "rabbitmq stopped"

MID3=$(curl -s -X POST "$API/recommendations" -H 'Content-Type: application/json' \
  -d '{"city":"reykjavik","forecast_date":"'"$(psql_q 'SELECT min(forecast_date) FROM weather_daily')"'","activity":"drill three aurora watching"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["message_id"])')
note "accepted with the broker down: $MID3"
sleep 6

note "the accepted-but-unpublished row, on the API outbox volume:"
dc exec -T api python -c "
from services.common import config
from services.common.outbox import Outbox
row = Outbox(config.OUTBOX_PATH).status_of('$MID3')
print('   ', dict(row) if row else 'MISSING')
"
# The same `published` helper drill 1 uses, rather than a bare subscript. The
# subscript raises on anything that is not an outbox row -- a 404 body, an API
# that is not answering -- and the caller then compares an empty string, which
# fails here for the right reason but names the wrong cause. Three outcomes,
# three messages.
PUB3="$(published "$MID3")"
case "$PUB3" in
  None)
    pass "accepted, published_at IS NULL -- the record is owed, on disk" ;;
  unknown | '')
    fail "could not read the outbox row for the traced ID; the API did not answer" ;;
  *)
    fail "the record was somehow published with no broker (published_at=$PUB3)" ;;
esac

dc start rabbitmq >/dev/null 2>&1
note "rabbitmq restarted; waiting for the backlog to drain"
if wait_stored "$MID3" 120; then
  pass "the backlog replayed and the record stored"
else
  fail "the record never replayed"
fi
note "trace: $(trace "$MID3")"

# ---------------------------------------------------------------------------
hr "Drill 4 -- a poison message"
note "A record that can never be stored must be quarantined, not retried"
note "forever and not silently dropped. It enters through the outbox, so its"
note "id is genuinely in the accepted set."
BEFORE_DLQ=$(queue_depth aow.dlq)

MID4=$(dc exec -T ingestor python -m services.tools.inject \
  --routing-key weather.daily \
  --payload '{"city_id":"rome","this_field":"does not exist in the schema"}' 2>/dev/null | tr -d '\r')
note "accepted poison message_id: $MID4"

for _ in $(seq 1 30); do
  [ "$(queue_depth aow.dlq)" -gt "$BEFORE_DLQ" ] && break
  sleep 2
done
if [ "$(queue_depth aow.dlq)" -gt "$BEFORE_DLQ" ]; then
  pass "quarantined in aow.dlq (depth $BEFORE_DLQ -> $(queue_depth aow.dlq))"
else
  fail "the poison message did not reach the dead-letter queue"
fi
if dc exec -T consumer python -m services.tools.redrive --list 2>&1 | grep -F "$MID4" >/dev/null; then
  pass "the traced poison ID is in the dead-letter queue"
else
  fail "the traced poison ID is missing from the dead-letter queue"
fi
note "main queue depth is still $(queue_depth aow.ingest) -- it did not block the others"
note "what is quarantined:"
dc exec -T consumer python -m services.tools.redrive --list 2>&1 | sed 's/^/   /' | tail -5

note "the record, traced in the ingestor outbox it was accepted into:"
note "$(trace_in ingestor "$MID4")"

note "redriving without fixing the cause: it must come straight back"
dc exec -T consumer python -m services.tools.redrive 2>&1 | sed 's/^/   /' | tail -3
sleep 12
if [ "$(queue_depth aow.dlq)" -gt "$BEFORE_DLQ" ]; then
  if dc exec -T consumer python -m services.tools.redrive --list 2>&1 | grep -F "$MID4" >/dev/null; then
    pass "redriven, failed validation again, quarantined again -- no loss, no loop"
  else
    fail "the traced poison ID is missing after redrive"
  fi
else
  fail "the message disappeared on redrive"
fi

# ---------------------------------------------------------------------------
hr "Where every traced record ended up"
printf '   drill 1  %s\n' "$(trace "$MID1")"
printf '   drill 2  %s\n' "$(trace "$MID2")"
printf '   drill 3  %s\n' "$(trace "$MID3")"
printf '   drill 4  %s\n' "$(trace_in ingestor "$MID4")"
note ""
note "Drill 4 is the one record that is NOT in the database, and that is the"
note "correct outcome: it is quarantined in aow.dlq, visible, and redrivable."
note "The guarantee is stored-or-quarantined, never lost."

hr "Totals"
note "queue depth:  $(queue_depth aow.ingest)"
note "dead letters: $(queue_depth aow.dlq)"
note "stored rows:  $(psql_q 'SELECT count(*) FROM ingest_log')"
note "duplicate message_ids in the database: $(psql_q 'SELECT count(*) - count(DISTINCT message_id) FROM ingest_log') (must be 0)"

if [ "$FAILED" -eq 0 ]; then
  printf '\n\033[32mAll drills passed.\033[0m\n'
else
  printf '\n\033[31mA drill failed -- see above.\033[0m\n'
fi
exit "$FAILED"
