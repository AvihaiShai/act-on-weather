#!/usr/bin/env bash
# Run the built application image against real broker and database containers.
# An independent Compose project and fresh volumes keep local stacks untouched.
set -euo pipefail

cd "$(dirname "$0")/.."
project="aow-ci-${GITHUB_RUN_ID:-$$}"
env_file="$(mktemp)"
chmod 600 "$env_file"

cleanup() {
  local status=$?
  if [ "$status" -ne 0 ]; then
    docker compose -p "$project" -f compose.yml -f compose.ci.yml --env-file "$env_file" logs --tail=80 migrate consumer api || true
  fi
  docker compose -p "$project" --env-file "$env_file" down --volumes --remove-orphans || true
  rm -f "$env_file"
}
trap cleanup EXIT

for key in POSTGRES_PASSWORD POSTGRES_WRITER_PASSWORD POSTGRES_READER_PASSWORD RABBITMQ_PASSWORD; do
  printf '%s=%s\n' "$key" "$(python3 -c 'import secrets; print(secrets.token_hex(24))')" >> "$env_file"
done

dc() { docker compose -p "$project" -f compose.yml -f compose.ci.yml --env-file "$env_file" "$@"; }

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

# Exercise an actual outage after the consumer has already connected once.
# Acceptance happens while Postgres is stopped; verification uses a separate
# reader connection, then checks persistence after a consumer restart.
test_day="$(dc exec -T postgres psql -U aow -d aow -tAc 'SELECT min(forecast_date) FROM weather_daily')"
dc stop postgres
message_id="$(dc exec -T -e AOW_TEST_DATE="$test_day" api python - < tests/integration/accept_db_down.py)"
dc start postgres
dc exec -T -e AOW_TEST_MESSAGE_ID="$message_id" api python - < tests/integration/verify_db_recovery.py
dc restart consumer
stored_count="$(dc exec -T postgres psql -U aow -d aow -tAc "SELECT count(*) FROM ingest_log WHERE message_id = '$message_id'")"
test "$stored_count" = 1
echo "PASS: accepted during database outage, committed once and survived consumer restart"
