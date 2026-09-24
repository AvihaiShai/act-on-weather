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
    docker compose -p "$project" -f compose.yml -f "$overlay" --env-file "$env_file" logs --tail=80 migrate consumer api || true
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
dc up -d --no-build --pull never postgres rabbitmq migrate ingestor consumer api
dc exec -T api python - < tests/integration/smoke.py
dc exec -T consumer python - < tests/integration/reconnect.py
# F2: the local-date derivation lives in SQL, so only a real Postgres with real
# tzdata can prove it. Asserted against the committed snapshot, whose dates are
# fixed, so this keeps holding after the staged forecast has expired.
dc exec -T api python - < tests/integration/event_local_days.py
ingestor_audit="$(dc exec -T ingestor python -m services.common.reconcile)"
python3 -c 'import json,sys; r=json.loads(sys.argv[1]); assert r["mode"] == "audit" and r["scanned"] > 0, r' "$ingestor_audit"
echo "ingestor reconciliation audit: $ingestor_audit"

# Exercise an actual outage after the consumer has already connected once.
# Acceptance happens while Postgres is stopped; verification uses a separate
# reader connection, then checks persistence after consumer/database restart.
test_day="$(dc exec -T postgres psql -U aow -d aow -tAc 'SELECT min(forecast_date) FROM weather_daily')"
dc stop postgres
message_id="$(dc exec -T -e AOW_TEST_DATE="$test_day" api python - < tests/integration/accept_db_down.py)"
dc start postgres
dc exec -T -e AOW_TEST_MESSAGE_ID="$message_id" api python - < tests/integration/verify_db_recovery.py
dc restart consumer postgres
dc exec -T -e AOW_TEST_MESSAGE_ID="$message_id" api python - < tests/integration/verify_db_recovery.py
stored_count="$(dc exec -T postgres psql -U aow -d aow -tAc "SELECT count(*) FROM ingest_log WHERE message_id = '$message_id'")"
test "$stored_count" = 1
echo "PASS: accepted during database outage, committed once and survived consumer/database restart"

# Reproduce the old ACKed-but-missing state in this disposable project. The
# fixture atomically commits a valid envelope with published_at set but no
# broker copy or DB row, without deleting other in-flight records.
missing_id="$(dc exec -T -e AOW_TEST_DATE="$test_day" api python - < tests/integration/inject_published_missing.py)"

audit="$(dc exec -T api python -m services.common.reconcile --id "$missing_id")"
python3 -c 'import json,sys; r=json.loads(sys.argv[1]); assert r["missing_ids"] == [sys.argv[2]] and r["replayed"] == 0, r' "$audit" "$missing_id"
echo "reconciliation audit: $audit"
replay="$(dc exec -T api python -m services.common.reconcile --replay --id "$missing_id")"
python3 -c 'import json,sys; r=json.loads(sys.argv[1]); assert r["replayed"] == 1 and r["missing_ids"] == [sys.argv[2]], r' "$replay" "$missing_id"
echo "reconciliation replay: $replay"

# An already committed ID must be skipped even when --replay is requested.
stored_audit="$(dc exec -T api python -m services.common.reconcile --replay --id "$message_id")"
python3 -c 'import json,sys; r=json.loads(sys.argv[1]); assert r["stored"] == 1 and r["replayed"] == 0, r' "$stored_audit"
echo "stored-ID control: $stored_audit"
dc exec -T -e AOW_TEST_MESSAGE_ID="$missing_id" api python - < tests/integration/verify_db_recovery.py
dc restart consumer postgres
dc exec -T -e AOW_TEST_MESSAGE_ID="$missing_id" api python - < tests/integration/verify_db_recovery.py
recovered_count="$(dc exec -T postgres psql -U aow -d aow -tAc "SELECT count(*) FROM ingest_log WHERE message_id = '$missing_id'")"
test "$recovered_count" = 1
echo "PASS: published-but-missing ID replayed once; already-stored ID skipped"
