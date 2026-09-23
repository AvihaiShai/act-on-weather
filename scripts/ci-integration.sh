#!/usr/bin/env bash
# Run the built application image against real broker and database containers.
# An independent Compose project and fresh volumes keep local stacks untouched.
set -euo pipefail

cd "$(dirname "$0")/.."
project="aow-ci-${GITHUB_RUN_ID:-$$}"
env_file="$(mktemp)"
chmod 600 "$env_file"

cleanup() {
  docker compose -p "$project" --env-file "$env_file" down --volumes --remove-orphans || true
  rm -f "$env_file"
}
trap cleanup EXIT

for key in POSTGRES_PASSWORD POSTGRES_WRITER_PASSWORD POSTGRES_READER_PASSWORD RABBITMQ_PASSWORD; do
  printf '%s=%s\n' "$key" "$(openssl rand -hex 24)" >> "$env_file"
done

dc() { docker compose -p "$project" -f compose.yml -f compose.ci.yml --env-file "$env_file" "$@"; }

# The overlay selects the exact images that were scanned and later published.
dc config --quiet
dc pull postgres rabbitmq
dc up -d --no-build --pull never postgres rabbitmq migrate ingestor consumer api
dc exec -T api python - < tests/integration/smoke.py
