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
#   bash scripts/retraction-drill.sh --preflight-only   # check, start nothing
#
# Environment:
#   AOW_DRILL_PROJECT     the disposable project name (default aow-retraction-drill)
#   AOW_PROJECT           the live project, never usable here (default aow)
#   AOW_DEMO_PROJECT      the live demo project, likewise (default aow-demo)
#   AOW_RESERVED_PROJECTS extra project names to refuse, space separated
#   AOW_DRILL_OVERLAY     a Compose overlay to add, e.g. compose.ci.yml on a runner
#                         whose images are already built and must not be rebuilt
#   AOW_ENV_FILE          credentials to use instead of generating a throwaway set
#   AOW_DRILL_KEEP        1 to leave the project standing for inspection. The next
#                         run then refuses until you remove it yourself
#
# Exit codes: 0 proved, 1 a drill failed, 2 refused before anything was touched.
set -euo pipefail

cd "$(dirname "$0")/.."

PROJECT="${AOW_DRILL_PROJECT:-aow-retraction-drill}"
LIVE_PROJECT="${AOW_PROJECT:-aow}"
DEMO_PROJECT="${AOW_DEMO_PROJECT:-aow-demo}"
OVERLAY="${AOW_DRILL_OVERLAY:-}"
PREFLIGHT_ONLY=0

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

usage() {
  sed -n '2,44p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --preflight-only) PREFLIGHT_ONLY=1 ;;
    -h|--help)        usage; exit 0 ;;
    *)                red "unknown argument: $1"; echo; usage >&2; exit 2 ;;
  esac
  shift
done

# ------------------------------------------------------------------ preflight --
#
# Everything in this block runs before the cleanup trap is installed, before any
# credential file is written, and before any Compose command that could change
# something. That ordering is the guarantee: by the time `down -v` can run at
# all, this drill has established that the project held nothing it did not
# create itself.
#
# It used to be one comparison against AOW_PROJECT, and that was not enough for
# two separate reasons. The live demo runs under `aow-demo`, which that check
# never mentioned. And a leftover project from an earlier run was *deleted
# automatically* on the way in -- `down -v --remove-orphans` before `up`, to stop
# a stale outbox poisoning phase 1. Convenient, and exactly the wrong trade: the
# one command in this script that destroys volumes was reachable on a project
# name nobody had looked at. Removing somebody else's state is an operator's
# decision, so now it is refused and the command to do it by hand is printed.

# Project names that are never a drill target, whatever state they are in. These
# are refused by name rather than by contents, because a live project that
# happens to be down still owns its name: creating volumes under it and then
# deleting them would leave the real stack on fresh volumes with nothing said.
#
# `aow` and `aow-demo` are listed as literals as well as through the variables.
# `LIVE_PROJECT` and `DEMO_PROJECT` read `AOW_PROJECT` and `AOW_DEMO_PROJECT`,
# which several scripts here export as a matter of course -- so in a shell where
# `AOW_PROJECT=aow-something-else` was already set, `aow` was not reserved at all
# and only the contents probe below stood between the drill and the live
# project's name. That probe does not cover a live project that has been taken
# down, which is exactly the case the comment above says this list is for.
RESERVED="aow aow-demo $LIVE_PROJECT $DEMO_PROJECT ${AOW_RESERVED_PROJECTS:-}"
for reserved in $RESERVED; do
  [ "$PROJECT" = "$reserved" ] || continue
  red "REFUSING: '$PROJECT' is a live project, not a drill target."
  cat >&2 <<EOF

This drill withdraws records -- there is deliberately no un-retract route -- and
then calls POST /user-data/wipe, which deletes every collected row and rebuilds
it. On the way out it destroys the project's volumes. Running that against a
stack you are keeping is not a proof, it is an outage.

Reserved here: $RESERVED
Give the drill a project of its own, which is the default:

  AOW_DRILL_PROJECT=aow-retraction-drill bash scripts/retraction-drill.sh
EOF
  exit 2
done

# What Compose has already created under this name. Read through the label
# Compose stamps on everything it makes, rather than through `docker compose ls`,
# which lists projects by their *running* containers: a stopped container, or a
# volume whose containers are long gone, is precisely the leftover that matters
# here and neither of them shows up there.
LABEL="label=com.docker.compose.project=$PROJECT"
EXISTING=""

probe() {
  local kind="$1"
  shift
  local out
  if ! out="$("$@" 2>&1)"; then
    red "REFUSING: could not ask Docker what project '$PROJECT' already holds."
    {
      echo
      echo "  $* "
      printf '%s\n' "$out" | sed 's/^/  /'
      echo
      echo "Without an answer this drill cannot tell an empty project from one"
      echo "holding somebody's data, and it destroys volumes on the way out."
    } >&2
    exit 2
  fi
  local count
  count="$(printf '%s' "$out" | grep -c . || true)"
  [ "$count" -gt 0 ] && EXISTING="${EXISTING}  $count $kind"$'\n'
  return 0
}

probe container docker ps -aq --filter "$LABEL"
probe network docker network ls -q --filter "$LABEL"
probe volume docker volume ls -q --filter "$LABEL"

if [ -n "$EXISTING" ]; then
  red "REFUSING: project '$PROJECT' already exists."
  {
    echo
    echo "It already holds:"
    printf '%s' "$EXISTING"
    echo
    echo "This drill destroys its project's volumes when it finishes, so it will"
    echo "only run against a project it created itself. What is there now might be"
    echo "a drill left standing by AOW_DRILL_KEEP=1, or it might be a stack"
    echo "somebody is using; from here the two look identical, and deleting the"
    echo "second to get on with the first is not a trade this script will make."
    echo
    echo "Look at it:"
    echo
    echo "  docker compose -p $PROJECT ps -a"
    echo "  docker volume ls --filter $LABEL"
    echo
    echo "Then remove it yourself, or run the drill somewhere else:"
    echo
    echo "  docker compose -p $PROJECT down -v --remove-orphans"
    echo "  AOW_DRILL_PROJECT=$PROJECT-2 bash scripts/retraction-drill.sh"
  } >&2
  exit 2
fi

if [ "$PREFLIGHT_ONLY" -eq 1 ]; then
  note "preflight only: '$PROJECT' is free of containers, networks and volumes"
  note "nothing was started and nothing was written"
  exit 0
fi

# --------------------------------------------------------------- credentials --

ENV_FILE="${AOW_ENV_FILE:-}"
GENERATED_ENV=""
if [ -z "$ENV_FILE" ]; then
  ENV_FILE="$(mktemp)"
  GENERATED_ENV="$ENV_FILE"
  chmod 600 "$ENV_FILE"
  for key in POSTGRES_PASSWORD POSTGRES_WRITER_PASSWORD POSTGRES_READER_PASSWORD RABBITMQ_PASSWORD; do
    printf '%s=%s\n' "$key" "$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> "$ENV_FILE"
  done
fi

FILES=(-f compose.yml)
[ -n "$OVERLAY" ] && FILES+=(-f "$OVERLAY")

dcd() { docker compose -p "$PROJECT" "${FILES[@]}" --env-file "$ENV_FILE" "$@"; }

# Installed only now, and this is the first point in the script where anything
# can be destroyed. The preflight above has established that '$PROJECT' held no
# container, network or volume, so everything this removes is something this run
# created.
cleanup() {
  local status=$?
  if [ "$status" -ne 0 ]; then
    hr "The drill failed; recent logs"
    dcd logs --no-color --tail 60 migrate consumer api ingestor 2>&1 | sed 's/^/   /' || true
  fi
  if [ "${AOW_DRILL_KEEP:-0}" = "1" ]; then
    note "AOW_DRILL_KEEP=1: project '$PROJECT' left standing, with its volumes"
    note "the next run will refuse until you remove it:"
    note "  docker compose -p $PROJECT down -v --remove-orphans"
  else
    hr "Tearing the drill project down"
    dcd down -v --remove-orphans >/dev/null 2>&1 || true
    note "project '$PROJECT' and all of its volumes removed"
  fi
  [ -n "$GENERATED_ENV" ] && rm -f "$GENERATED_ENV"
  return "$status"
}
trap cleanup EXIT
# Ctrl-C too, not just a normal exit. Without these, interrupting a drill left
# the project standing with its volumes -- safe, because the next run refuses
# it, but the header above promises "a cleanup trap that destroys everything it
# made" and an operator who interrupts is the likeliest person to believe it.
# `exit` re-enters the EXIT trap, so the teardown itself is not duplicated.
trap 'exit 130' INT
trap 'exit 143' TERM

hr "Starting the disposable project '$PROJECT'"
dcd config --quiet
# No `down -v` here. A project that already held anything was refused above, so
# there is nothing stale to clear and nothing of anybody else's to lose.
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
