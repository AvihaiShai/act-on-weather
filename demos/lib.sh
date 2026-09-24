#!/usr/bin/env bash
# Shared helpers for the demo scripts and for scripts/refresh.sh. Sourced,
# not run. One implementation of "talk to this stack": the same project
# selection, the same .env handling and the same psql/outbox helpers, so an
# operator command and a proof cannot disagree about which stack they mean.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Every script talks to the stack through these two, so a reviewer can point
# them at a different env file or a different host without editing anything.
: "${AOW_ENV_FILE:=.env}"
: "${API:=http://localhost:8000}"

dc() {
  if [ -f "$AOW_ENV_FILE" ]; then
    docker compose --env-file "$AOW_ENV_FILE" "$@"
  else
    docker compose "$@"
  fi
}

psql_q() { dc exec -T postgres psql -U "${POSTGRES_USER:-aow}" -d "${POSTGRES_DB:-aow}" -tAc "$1"; }

queue_depth() {
  dc exec -T rabbitmq rabbitmqctl list_queues name messages --quiet \
    | awk -v q="$1" '$1 == q { print $2 }'
}

# Follow one accepted message_id to wherever it actually got to. This is the
# assertion the failure drills make: not "the row counts match" -- which can
# hide a loss and a duplicate cancelling out -- but "this exact record, which
# the system promised to keep, ended up stored".
trace() {
  curl -s "$API/outbox/$1" || echo '{"error":"not found"}'
}

# The ingestor keeps its own outbox on its own volume, and the API cannot read
# it -- mounting another service's live WAL database read-only is exactly the
# shortcut that breaks under load. So a record accepted there is traced by
# asking that service about it.
trace_in() {
  dc exec -T "$1" python -c "
import json
from services.common import config
from services.common.outbox import Outbox
row = Outbox(config.OUTBOX_PATH).status_of('$2')
print(json.dumps(dict(row) if row else {'error': 'not accepted here'}))
" 2>/dev/null | tr -d '\r'
}

stored() {
  curl -s "$API/outbox/$1" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("stored"))' 2>/dev/null \
    || echo "unknown"
}

wait_stored() {
  local mid="$1" limit="${2:-60}" i=0
  while [ "$i" -lt "$limit" ]; do
    [ "$(stored "$mid")" = "True" ] && return 0
    sleep 2; i=$((i + 2))
  done
  return 1
}

hr() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
note() { printf '   %s\n' "$*"; }
pass() { printf '\033[32m   PASS\033[0m %s\n' "$*"; }
fail() { printf '\033[31m   FAIL\033[0m %s\n' "$*"; FAILED=1; }
