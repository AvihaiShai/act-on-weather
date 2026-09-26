#!/usr/bin/env bash
# Run the built application image against real broker and database containers.
# An independent Compose project and fresh volumes keep local stacks untouched.
set -euo pipefail

cd "$(dirname "$0")/.."
project="aow-ci-${GITHUB_RUN_ID:-$$}"
overlay="${AOW_CI_OVERLAY:-compose.ci.yml}"
env_file="$(mktemp)"
chmod 600 "$env_file"

cleanup() {
  local status=$?
  if [ "$status" -ne 0 ]; then
    docker compose -p "$project" -f compose.yml -f "$overlay" --env-file "$env_file" logs --tail=80 migrate consumer enricher api || true
  fi
  docker compose -p "$project" --env-file "$env_file" down --volumes --remove-orphans || true
  rm -f "$env_file"
}
trap cleanup EXIT

for key in POSTGRES_PASSWORD POSTGRES_WRITER_PASSWORD POSTGRES_READER_PASSWORD RABBITMQ_PASSWORD; do
  printf '%s=%s\n' "$key" "$(python3 -c 'import secrets; print(secrets.token_hex(24))')" >> "$env_file"
done

dc() { docker compose -p "$project" -f compose.yml -f "$overlay" --env-file "$env_file" "$@"; }

# The overlay selects the exact images that were scanned and later published.
dc config --quiet
dc pull postgres rabbitmq
dc up -d --no-build --pull never postgres rabbitmq migrate ingestor consumer enricher api
dc exec -T api python - < tests/integration/smoke.py
dc exec -T enricher python - < tests/integration/enricher_running.py
dc exec -T consumer python - < tests/integration/reconnect.py
# F2: the local-date derivation lives in SQL, so only a real Postgres with real
# tzdata can prove it. Asserted against the committed snapshot, whose dates are
# fixed, so this keeps holding after the staged forecast has expired.
dc exec -T api python - < tests/integration/event_local_days.py
# F9: the freshness filter is a SQL predicate over now(), and a re-check is a
# correction that travels through the outbox, the broker and the consumer. Both
# halves need a real stack, so the drill patches a listing's check date into the
# past, watches it leave the default read while staying counted, and re-checks
# it. It runs before the M11 drills below so it shares their fresh project.
dc exec -T api python - < tests/integration/event_freshness.py
# F9 follow-up: a re-check renews a listing only when its own page verified it.
# The probe needs egress and is not run here; this drives the apply path, which
# is the half that can silently extend a listing's life if it regresses.
dc exec -T api python - < tests/integration/event_recheck.py
# A saved trip whose caller sent no scoring as-of. The column is nullable only
# because migration 008 says so, and for a while nothing ran that file: the API
# answered 202 and the consumer hit NotNullViolation, which is neither Poison
# nor OperationalError, so the record was requeued to the DLQ rather than
# dead-lettered with a reason. No unit test can see it -- they hand the
# consumer a fake cursor -- and no other drill here writes an itinerary at all.
dc exec -T api python - < tests/integration/itinerary_without_as_of.py
# At-least-once plus a requeue means two refreshes of one city-day can arrive
# in the wrong order under different message_ids, which `ingest_log` does not
# deduplicate. Only `upsert_weather`'s as_of guard stops the older one being
# written, and no other check in this repository exercises it -- every drill
# below replays the *same* message rather than an older one.
dc exec -T api python - < tests/integration/stale_forecast.py
ingestor_audit="$(dc exec -T ingestor python -m services.common.reconcile)"
python3 -c 'import json,sys; r=json.loads(sys.argv[1]); assert r["mode"] == "audit" and r["scanned"] > 0, r' "$ingestor_audit"
echo "ingestor reconciliation audit: $ingestor_audit"

# ---------------------------------------------------------------------------
# M11: one acceptance per failure mode, each with its own message_id.
#
# Drills 1-3 accept through the API, drills 4-5 through the ingestor, so the
# API and ingestor outboxes are driven through an outage rather than only
# audited. In every one the record is only as safe as the fsynced outbox row
# makes it. Each is then checked twice: once on its own, and once at the end with
# the others after a full restart. The second check is the one that matters --
# a write that was acknowledged but never committed is visible to nobody and
# survives nothing, so the final gate asks a separate reader for every ID this
# run accepted. Any single missing ID fails the job.
#
# `test_day` is a date with stored weather, so the consumer takes the scoring
# path rather than the "no weather for that date" branch.
# ---------------------------------------------------------------------------
test_day="$(dc exec -T postgres psql -U aow -d aow -tAc 'SELECT min(forecast_date) FROM weather_daily' | tr -d '\r')"

accept_one() {
  dc exec -T -e AOW_TEST_DATE="$test_day" -e AOW_TEST_LABEL="$1" \
    api python - < tests/integration/accept_request.py | tr -d '\r'
}

outbox_json() {
  dc exec -T api python -c "import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8000/outbox/$1'))))" | tr -d '\r'
}

accept_via_ingestor() {
  dc exec -T -e AOW_TEST_LABEL="$1" \
    ingestor python - < tests/integration/accept_via_ingestor.py | tr -d '\r'
}

# The ingestor has no HTTP surface, so its outbox is read directly, read-only,
# inside its own container. Read-only matters: a drill must never be able to
# create or repair the state it is about to assert on.
ingestor_outbox_published_at() {
  dc exec -T ingestor python -c "from services.common import config; from services.common.outbox import Outbox; b = Outbox(config.OUTBOX_PATH, readonly=True); r = b.status_of('$1'); assert r is not None, 'not accepted'; print(r['published_at'])" | tr -d '\r'
}

wait_stored() {
  dc exec -T -e AOW_TEST_MESSAGE_IDS="$1" api python - < tests/integration/verify_db_recovery.py
}

# Each drill waits for its own record to be committed before the next one
# starts. That is not just tidiness: drill 3 has to reach a running consumer to
# be meaningful, and a broker that was stopped moments earlier is not yet
# carrying deliveries.

# Drill 1 -- consumer down. The broker holds the delivery until it returns.
dc stop consumer
consumer_id="$(accept_one 'ci consumer outage')"
consumer_state="$(outbox_json "$consumer_id")"
python3 -c 'import json,sys; r=json.loads(sys.argv[1]); assert r["stored"] is False, r' "$consumer_state"
dc start consumer
wait_stored "$consumer_id"
echo "drill 1 consumer-down: accepted while stopped, stored after restart -- $consumer_id"

# Drill 2 -- broker down. The outbox holds the record until the publisher can
# confirm it, so `published_at` must still be empty while RabbitMQ is stopped.
dc stop rabbitmq
broker_id="$(accept_one 'ci broker outage')"
broker_state="$(outbox_json "$broker_id")"
python3 -c 'import json,sys; r=json.loads(sys.argv[1]); assert r["published_at"] is None and r["stored"] is False, r' "$broker_state"
dc start rabbitmq
wait_stored "$broker_id"
echo "drill 2 broker-down: spooled unpublished, replayed and stored -- $broker_id"

# Drill 3 -- database down. This is the F1 reproduction, and the one drill
# whose timing carries the whole point. Accepting while Postgres is stopped
# proves nothing on its own: if the database is back before the delivery
# arrives, the consumer never drops its connection and never takes the
# reconnect path that lost the record. So the drill blocks until the consumer
# has actually tried and failed this delivery against a dead database, and only
# then restores it. Without this wait the drill passes against the F1 bug.
dc stop postgres
db_id="$(accept_one 'ci database outage')"
deadline=$((SECONDS + 180))
until dc logs --since 10m consumer 2>&1 | grep -q "redelivering $db_id"; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "drill 3 never reached the consumer while the database was down: $db_id" >&2
    exit 1
  fi
  sleep 2
done
echo "drill 3 database-down: consumer failed the delivery against a dead database, as intended"
dc start postgres
wait_stored "$db_id"
echo "drill 3 database-down: committed after reconnect -- $db_id"

# ---------------------------------------------------------------------------
# Drills 4 and 5 repeat the broker and database outages against the *ingestor's*
# outbox. That is a different volume, drained by a different loop
# (`ingestor.main.drain`) in a different process, so the API drills above say
# nothing about it. Until these existed the ingestor outbox was audited on
# every run but never driven through an outage, and the README said so.
# ---------------------------------------------------------------------------

# Drill 4 -- ingestor accepts while the broker is down. This is the property the
# ingestor's outbox exists for: acceptance keeps working, publishing stops, and
# nothing is marked published until the broker has confirmed it. A `drain()`
# that marked rows published before confirming would pass every other check in
# this script and fail here.
dc stop rabbitmq
ing_broker_id="$(accept_via_ingestor 'ingestor broker outage')"
published_at="$(ingestor_outbox_published_at "$ing_broker_id")"
test "$published_at" = "None"
dc start rabbitmq
wait_stored "$ing_broker_id"
echo "drill 4 ingestor broker-down: spooled unpublished, drained and stored -- $ing_broker_id"

# Drill 5 -- ingestor accepts while the database is down. Same reconnect path as
# drill 3, reached from the other producer, and gated the same way: wait for the
# consumer to actually fail this delivery before restoring Postgres.
dc stop postgres
ing_db_id="$(accept_via_ingestor 'ingestor database outage')"
deadline=$((SECONDS + 180))
until dc logs --since 10m consumer 2>&1 | grep -q "redelivering $ing_db_id"; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "drill 5 never reached the consumer while the database was down: $ing_db_id" >&2
    exit 1
  fi
  sleep 2
done
echo "drill 5 ingestor database-down: consumer failed the delivery against a dead database, as intended"
dc start postgres
wait_stored "$ing_db_id"
echo "drill 5 ingestor database-down: committed after reconnect -- $ing_db_id"

drill_ids="$consumer_id,$broker_id,$db_id,$ing_broker_id,$ing_db_id"
dc exec -T -e AOW_TEST_MESSAGE_IDS="$drill_ids" api python - < tests/integration/verify_db_recovery.py

# Full restart of every service in the path, not just the one that was stopped.
# An uncommitted write survives in a session; it does not survive this.
dc restart postgres rabbitmq ingestor consumer api
dc exec -T -e AOW_TEST_MESSAGE_IDS="$drill_ids" api python - < tests/integration/verify_db_recovery.py
echo "PASS: five outages across the API and ingestor outboxes each committed once and survived a full restart"

# ---------------------------------------------------------------------------
# Reproduce the historical ACKed-but-missing state in this disposable project.
# The fixture atomically commits a valid envelope with `published_at` set but
# no broker copy and no database row, without touching other in-flight records.
# ---------------------------------------------------------------------------
missing_id="$(dc exec -T -e AOW_TEST_DATE="$test_day" api python - < tests/integration/inject_published_missing.py | tr -d '\r')"

audit="$(dc exec -T api python -m services.common.reconcile --id "$missing_id" | tr -d '\r')"
python3 -c 'import json,sys; r=json.loads(sys.argv[1]); assert r["missing_ids"] == [sys.argv[2]] and r["replayed"] == 0, r' "$audit" "$missing_id"
echo "reconciliation audit: $audit"
replay="$(dc exec -T api python -m services.common.reconcile --replay --id "$missing_id" | tr -d '\r')"
python3 -c 'import json,sys; r=json.loads(sys.argv[1]); assert r["replayed"] == 1 and r["missing_ids"] == [sys.argv[2]], r' "$replay" "$missing_id"
echo "reconciliation replay: $replay"

# An already committed ID must be skipped even when --replay is requested.
stored_audit="$(dc exec -T api python -m services.common.reconcile --replay --id "$db_id" | tr -d '\r')"
python3 -c 'import json,sys; r=json.loads(sys.argv[1]); assert r["stored"] == 1 and r["replayed"] == 0, r' "$stored_audit"
echo "stored-ID control: $stored_audit"

# ---------------------------------------------------------------------------
# Final gate. Every ID this run accepted, re-checked together after one more
# restart: any single missing ID fails the job, and so does any duplicate.
# ---------------------------------------------------------------------------
all_ids="$drill_ids,$missing_id"
dc exec -T -e AOW_TEST_MESSAGE_IDS="$all_ids" api python - < tests/integration/verify_db_recovery.py
dc restart postgres rabbitmq consumer api
dc exec -T -e AOW_TEST_MESSAGE_IDS="$all_ids" api python - < tests/integration/verify_db_recovery.py
stored_total="$(dc exec -T postgres psql -U aow -d aow -tAc "SELECT count(*) FROM ingest_log WHERE message_id = ANY(string_to_array('$all_ids', ','))" | tr -d '\r')"
test "$stored_total" = 6
echo "PASS: all 6 traced IDs ($all_ids) are stored exactly once after full recovery"
