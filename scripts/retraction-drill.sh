#!/usr/bin/env bash
# F9: prove a withdrawal works whichever order it and its record arrive in.
#
# Same shape as scripts/ci-integration.sh and demos/06_backup_restore.sh: its own
# Compose project, fresh volumes, generated passwords, and a cleanup trap that
# destroys everything it made. It is separate from ci-integration.sh for one
# reason -- these drills are destructive in a direction nothing undoes. There is
# deliberately no un-retract route, the withdrawn synthetic listing stays in the
# ingestor's outbox for the life of the volume, and the last phase wipes and
# rebuilds every collected row. A drill that needs its own disposable project
# does not belong in a script other people run against a stack they are keeping.
#
# What runs, in order:
#
#   tests/integration/retraction.py               the original F9 proof: an event,
#                                                 a place and a fact are withdrawn
#                                                 while this install holds them,
#                                                 and stay out across a rebuild
#   retraction_before_record.py    (api)          withdraw a record that is NOT here
#   retraction_inject_event.py     (ingestor)     the record then arrives, through
#                                                 the ingestor's own outbox
#   retraction_arrival_order.py    (api)          it is stored and published nowhere,
#                                                 and stays that way across a rebuild
#
# The first covers the arrival order that always worked; the last three cover the
# one that did not, until migration 010 gave the decision a row of its own. Run
# them together because a fix that passes only one of the two orders is the defect
# with a different sign.
#
#   bash scripts/retraction-drill.sh
#
# Environment:
#   AOW_DRILL_PROJECT   the disposable project name (default aow-retraction-drill)
#   AOW_PROJECT         the live project this must never be (default aow)
#   AOW_DRILL_OVERLAY   a Compose overlay to add, e.g. compose.ci.yml on a runner
#                       whose images are already built and must not be rebuilt
#   AOW_ENV_FILE        credentials to use instead of generating a throwaway set
#   AOW_DRILL_KEEP      1 to leave the project standing for inspection
#
# Exit codes: 0 proved, 1 a drill failed, 2 refused before anything was touched.
set -euo pipefail

cd "$(dirname "$0")/.."

PROJECT="${AOW_DRILL_PROJECT:-aow-retraction-drill}"
LIVE_PROJECT="${AOW_PROJECT:-aow}"
OVERLAY="${AOW_DRILL_OVERLAY:-}"

# No services the drill does not need. No `llm`, because nothing in the write or
# read path depends on it and the model is the slowest thing to start; no `agent`
# or `ui`, which nothing here calls; and above all no `edge`, which publishes 8080
# and 8000 -- a proof that fights a reviewer's running stack for a port is not a
# proof.
SERVICES="postgres rabbitmq migrate ingestor consumer api"

# The record under test, written down once. Three files in two containers have to
# agree on it, and integration tests here are streamed in on stdin so they cannot
# import a shared module. The `drill:` prefix says what the row is to anybody who
# finds it later.
export AOW_DRILL_CITY="london"
export AOW_DRILL_EVENT_ID="drill:arrival-order-withdrawn"
export AOW_DRILL_REASON="integration drill: withdrawn before this install ever held it"

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
note() { printf '   %s\n' "$*"; }
hr()   { printf '\n\033[1m== %s\033[0m\n' "$*"; }

# The one guard that matters. This drill withdraws records with no way back and
# then wipes and rebuilds the database, so pointing it at the live project would
# be doing deliberate damage on the strength of an environment variable. There is
# no override flag, because there is no case for one: the drill proves a property
# of the code, and a disposable project proves it just as well.
if [ "$PROJECT" = "$LIVE_PROJECT" ]; then
  red "REFUSING: '$PROJECT' is the live project."
  cat >&2 <<EOF

This drill withdraws records -- there is deliberately no un-retract route -- and
then calls POST /user-data/wipe, which deletes every collected row and rebuilds
it. Running that against a stack you are keeping is not a proof, it is an
outage.

Give it a project of its own, which is the default:

  AOW_DRILL_PROJECT=aow-retraction-drill bash scripts/retraction-drill.sh
EOF
  exit 2
fi

ENV_FILE="${AOW_ENV_FILE:-}"
GENERATED_ENV=""
if [ -z "$ENV_FILE" ]; then
  ENV_FILE="$(mktemp)"
  GENERATED_ENV="$ENV_FILE"
  chmod 600 "$ENV_FILE"
  # Generated in the pinned Postgres image rather than on the host: this repo
  # does not require a host Python, and demos/06_backup_restore.sh makes the same
  # choice for the same reason.
  for key in POSTGRES_PASSWORD POSTGRES_WRITER_PASSWORD POSTGRES_READER_PASSWORD RABBITMQ_PASSWORD; do
    printf '%s=%s\n' "$key" "$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> "$ENV_FILE"
  done
fi

FILES=(-f compose.yml)
[ -n "$OVERLAY" ] && FILES+=(-f "$OVERLAY")

dcd() { docker compose -p "$PROJECT" "${FILES[@]}" --env-file "$ENV_FILE" "$@"; }

cleanup() {
  local status=$?
  if [ "$status" -ne 0 ]; then
    hr "The drill failed; recent logs"
    dcd logs --no-color --tail 60 migrate consumer api ingestor 2>&1 | sed 's/^/   /' || true
  fi
  if [ "${AOW_DRILL_KEEP:-0}" = "1" ]; then
    note "AOW_DRILL_KEEP=1: project '$PROJECT' left standing, with its volumes"
    note "remove it with: docker compose -p $PROJECT down -v --remove-orphans"
  else
    hr "Tearing the drill project down"
    dcd down -v --remove-orphans >/dev/null 2>&1 || true
    note "project '$PROJECT' and all of its volumes removed"
  fi
  [ -n "$GENERATED_ENV" ] && rm -f "$GENERATED_ENV"
  return "$status"
}
trap cleanup EXIT

hr "Starting the disposable project '$PROJECT'"
dcd config --quiet
# A previous run left standing by AOW_DRILL_KEEP would otherwise poison this one:
# its outbox already holds the withdrawn listing, so phase 1 would refuse.
dcd down -v --remove-orphans >/dev/null 2>&1 || true
# shellcheck disable=SC2086 # SERVICES is a deliberate word list
dcd up -d $SERVICES
note "services: $SERVICES"

hr "The order that already worked: withdraw records this install holds"
dcd exec -T api python - < tests/integration/retraction.py

hr "Phase 1: withdraw a record this install does not hold"
phase1="$(dcd exec -T \
  -e AOW_DRILL_CITY -e AOW_DRILL_EVENT_ID -e AOW_DRILL_REASON \
  api python - < tests/integration/retraction_before_record.py)"
printf '%s\n' "$phase1" | sed 's/^/   /'
# The last line, and only the last line, is the recorded decision date.
AOW_DRILL_RETRACTED_AT="$(printf '%s\n' "$phase1" | tail -n 1)"
export AOW_DRILL_RETRACTED_AT
[ -n "$AOW_DRILL_RETRACTED_AT" ] || { red "phase 1 printed no decision date"; exit 1; }

hr "Phase 2: the record arrives, through the ingestor's own outbox"
dcd exec -T \
  -e AOW_DRILL_CITY -e AOW_DRILL_EVENT_ID \
  ingestor python - < tests/integration/retraction_inject_event.py

hr "Phase 3: it is stored, published nowhere, and survives a rebuild"
dcd exec -T \
  -e AOW_DRILL_CITY -e AOW_DRILL_EVENT_ID -e AOW_DRILL_REASON -e AOW_DRILL_RETRACTED_AT \
  api python - < tests/integration/retraction_arrival_order.py

hr "Proved"
note "a withdrawal holds whichever order it and its record arrive in"
