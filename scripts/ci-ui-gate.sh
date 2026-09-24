#!/usr/bin/env bash
# Bring up the real stack and run the headless-browser gate (F11) against it.
#
# Same shape as scripts/ci-integration.sh: its own Compose project, fresh
# volumes, generated passwords, the exact images CI already built and
# scanned (--no-build --pull never). The agent, enricher and LLM are
# deliberately NOT started here -- the browser gate never opens the "Ask the
# agent" tab, only checks that it renders, and the deterministic rule
# engine that fills the Suitability tab's scores runs in the consumer, not
# behind the model. Leaving them out keeps this a fast, per-PR gate; the
# model itself gets its own gate (tests/integration/model_grounding.py, run
# by scripts/ci-model-grounding.sh) on a slower, release-candidate schedule.
set -euo pipefail

cd "$(dirname "$0")/.."
project="aow-ci-ui-${GITHUB_RUN_ID:-$$}"
overlay="${AOW_CI_OVERLAY:-compose.ci.yml}"
env_file="$(mktemp)"
chmod 600 "$env_file"

cleanup() {
  local status=$?
  if [ "$status" -ne 0 ]; then
    docker compose -p "$project" -f compose.yml -f "$overlay" --env-file "$env_file" logs --tail=120 api ui edge || true
  fi
  docker compose -p "$project" --env-file "$env_file" down --volumes --remove-orphans || true
  rm -f "$env_file"
}
trap cleanup EXIT

for key in POSTGRES_PASSWORD POSTGRES_WRITER_PASSWORD POSTGRES_READER_PASSWORD RABBITMQ_PASSWORD; do
  printf '%s=%s\n' "$key" "$(python3 -c 'import secrets; print(secrets.token_hex(24))')" >> "$env_file"
done

dc() { docker compose -p "$project" -f compose.yml -f "$overlay" --env-file "$env_file" "$@"; }

dc config --quiet
dc pull postgres rabbitmq
dc up -d --no-build --pull never postgres rabbitmq migrate ingestor consumer api ui edge

# `edge` reporting healthy only means nginx itself answered /healthz -- it
# says nothing about the services it proxies to. Wait for the two the
# browser gate actually depends on before starting the browser.
wait_healthy() {
  local service="$1" path="$2" deadline=$((SECONDS + 120))
  until dc exec -T "$service" python -c "
import urllib.request,sys
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:$path', timeout=3).status == 200 else 1)
" >/dev/null 2>&1; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "$service never became healthy on $path" >&2
      exit 1
    fi
    sleep 2
  done
}
wait_healthy api 8000/health
wait_healthy ui 8501/_stcore/health

echo "stack is up; running the browser gate"
dc build uitest
dc run --rm --no-deps uitest
echo "PASS: browser gate held against the real stack"
