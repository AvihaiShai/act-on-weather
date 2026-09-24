#!/usr/bin/env bash
#
# Operator-assisted re-check of stored event listings (F9 follow-up).
#
#   docker compose -f compose.tools.yml run --rm \
#       --entrypoint bash refresh /work/scripts/event-recheck.sh       # any OS
#   bash scripts/event-recheck.sh                                      # bash hosts
#
# It borrows the `refresh` tooling service rather than declaring one of its own:
# that service already has exactly what this needs and nothing more -- the
# Docker socket for two `docker network` calls, a foot on the stack's backend
# network, and no route out of its own. Adding a near-identical service would
# be a second thing to keep in step with it.
#
# F9 closed the freshness half of the event problem: every row carries the day
# its page was last read (`checked_at`), an expiry derived from it, and stops
# being offered as a schedule once that expiry passes. What it left open was the
# re-check itself -- extending a row's life meant opening its page by hand and
# patching `checked_at`, one row at a time.
#
# This is the connected half of closing that, and it is deliberately only half.
# It fetches, it diffs, it reports. It writes NOTHING. Renewing a row is a
# second, separate, offline command that a human runs after reading the report:
#
#   docker compose -f compose.tools.yml run --rm --entrypoint python3 refresh \
#       -m services.tools.event_recheck apply --evidence <ledger> --all-confirmed --yes
#
# Why it cannot just renew what it fetched
# ----------------------------------------
# The eleven venue sites behind this feed were probed on 2026-09-25. Three of
# them (harpa.is, theo2.co.uk, gulbenkian.pt) publish schema.org Event JSON-LD
# with a start date; gulbenkian.pt also publishes an eventStatus, and harpa.is
# does too. The other eight publish none at all -- no microdata, no
# <time datetime>, not even an ISO date in the visible text; the date exists
# only as prose in the venue's own language. On 28 of the 55 stored rows there
# is therefore nothing to verify against, and "it returned 200" is a statement
# about the venue's web server, not about whether a concert is still on.
#
# That eventStatus is not decoration. The first full run of this tool read
# schema.org/EventCancelled on both dates of a Gulbenkian concert that had been
# hand-verified as scheduled the day before, and the two rows were removed. A
# venue can cancel a show after a human checked it, which is the whole reason
# a re-check exists.
#
# The text shortcuts are worse than useless. auditorium.com serves a scheduled
# concert's page carrying the words "EVENTO ANNULLATO - Ryan Adams" in its
# related-events sidebar; coliseulisboa.com says "esgotado" (sold out) on a show
# that is going ahead. Grepping for either cancels a live event.
#
# So: what the page states in structured data is checked against what we stored,
# and everything else is reported as needing eyes. services/tools/event_recheck.py
# holds the rules and the reasoning.
#
# The egress window
# -----------------
# Only the ingestor ever gets a route out, and only for as long as a fetch
# takes. This opens the same kind of temporary bridge network scripts/refresh.sh
# does, attaches the ingestor to it, runs the probe INSIDE that container, then
# removes the network and asserts it is gone -- from the body, from an exit trap,
# and from the same detached scripts/refresh_window_guard.sh that bounds the
# refresh window, so a SIGKILL of this command cannot leave a hole.
#
# The probe itself refuses to run unless AOW_RECHECK_ALLOW_EGRESS=1, which is
# set here and nowhere else. The air-gapped runtime cannot reach that code path
# even by accident.
#
# Exit codes:
#   0  every listing was probed and the window was verified closed
#   1  the stack is not in a state where a re-check can run
#   2  the probe failed (the egress window was still closed)
#   3  the egress window could NOT be closed -- act on this
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=demos/lib.sh
. demos/lib.sh

WINDOW_MAX_S="${RECHECK_WINDOW_MAX_S:-600}"
GUARD_IMAGE="${REFRESH_GUARD_IMAGE:-aow/demos:dev}"
OUT_DIR="${RECHECK_OUT_DIR:-.recheck}"
TIMEOUT_S="${RECHECK_TIMEOUT_S:-20}"
PY_ARGS=()

usage() {
  cat <<'EOF'
Re-check stored event listings against their own pages. Fetches and reports;
never writes.

  docker compose -f compose.tools.yml run --rm \
      --entrypoint bash refresh /work/scripts/event-recheck.sh [options]
  bash scripts/event-recheck.sh [options]

Options:
  --city SLUG     re-check one city; repeatable. Default: every city.
  --id EVENT_ID   re-check one listing; repeatable.
  --timeout N     per-page fetch timeout in seconds (default 20).
  --out DIR       where to write the evidence ledger (default .recheck).
  -h, --help      this text.

It writes an append-only JSONL ledger under --out, one line per listing,
carrying the source URL, the instant of the fetch, the HTTP status, the SHA-256
of the bytes read, what was extracted from them and the verdict. Then it prints
the review.

Nothing is renewed here. To renew, read the report and run:

  docker compose -f compose.tools.yml run --rm --entrypoint python3 refresh \
      -m services.tools.event_recheck apply --evidence <ledger> --all-confirmed --yes

A row whose verdict is not `confirmed` cannot be renewed that way at all. It
needs --id ID --note "what you saw on the page", which records that a human
looked, because no parser verified it.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --city) PY_ARGS+=(--city "$2"); shift 2 ;;
    --id) PY_ARGS+=(--id "$2"); shift 2 ;;
    --timeout) TIMEOUT_S="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; echo >&2; usage >&2; exit 2 ;;
  esac
done

FAILED=0
CLEANUP_FAILED=0
EGRESS_LABEL="aow.role=event-recheck-window"
CID=""
CNAME=""
EGRESS=""
GUARD_ID=""
OPENED_AT=""
HELD_SECONDS=""

# ------------------------------------------------------------ the window --
#
# Deliberately the same shape as scripts/refresh.sh, and deliberately its own
# network name. Two commands that could each open and close a window called
# `<project>_refresh_egress` would race: one closing its window would pull the
# route out from under the other's fetch, and the guard would bound whichever
# ran first. A separate name means the two are independent, and the "is anything
# else on this network?" check below still catches a genuine surprise.

attached() {
  docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}' "$CID" \
    2>/dev/null | tr -d '\r' | grep -qw "$EGRESS"
}

egress_members() {
  docker network inspect -f '{{range .Containers}}{{.Name}} {{end}}' "$EGRESS" \
    2>/dev/null | tr -d '\r'
}

network_exists() {
  docker network inspect "$EGRESS" >/dev/null 2>&1
}

# The same guard scripts/refresh.sh uses, unchanged and unforked: a detached
# container that closes this window at a deadline whatever happens to this
# process, including SIGKILL, which no trap can survive. Passed as an argument
# rather than mounted, because this process may itself be inside a container
# and does not know the host path of the working tree it is reading.
start_window_guard() {
  local src
  src="$(cat "$(dirname "$0")/refresh_window_guard.sh" 2>/dev/null)" || src=""
  if [ -z "$src" ]; then
    note "WARNING: refresh_window_guard.sh is missing -- the window has no"
    note "         deadline on this run. The trap still closes it on every exit"
    note "         short of SIGKILL."
    return 0
  fi
  GUARD_ID="$(
    docker run -d --rm \
      --label "aow.role=event-recheck-window-guard" \
      --network none \
      -v /var/run/docker.sock:/var/run/docker.sock \
      --entrypoint bash "$GUARD_IMAGE" \
      -c "$src" window-guard "$EGRESS" "$CID" "$WINDOW_MAX_S" 2>/dev/null
  )" || GUARD_ID=""
  if [ -z "$GUARD_ID" ]; then
    note "WARNING: could not start the window guard (image $GUARD_IMAGE) -- the"
    note "         window has no deadline on this run. Build it with:"
    note "         docker compose -f compose.tools.yml build demos"
    return 0
  fi
  note "window guard ${GUARD_ID:0:12} will close $EGRESS after ${WINDOW_MAX_S}s if this command cannot"
}

open_egress() {
  hr "Opening the egress window"
  # Network, then guard, then attachment. The refresh script settled this
  # ordering with a drill and the reasoning is in its comments: before the
  # network the guard finds nothing and either exits early or lingers into the
  # next run's window; after the attachment there is a gap in which a kill
  # leaves a route out nothing is watching.
  if ! network_exists; then
    docker network create --label "$EGRESS_LABEL" "$EGRESS" >/dev/null
  fi
  start_window_guard
  if ! attached; then
    docker network connect "$EGRESS" "$CID" >/dev/null
  fi
  OPENED_AT="$(date +%s)"
  if attached; then
    pass "ingestor attached to $EGRESS"
  else
    fail "could not attach the ingestor to $EGRESS"
    return 1
  fi
  local others
  others="$(egress_members | tr ' ' '\n' | grep -v '^$' | grep -vx "$CNAME" || true)"
  if [ -n "$others" ]; then
    note "WARNING: something else is on $EGRESS too: $(echo $others)."
    note "         It was not attached by this command; check it before trusting"
    note "         the boundary."
  else
    note "nothing else is on $EGRESS -- the ingestor is the only container with a route out"
  fi
}

close_egress() {
  [ -n "$CID" ] && [ -n "$EGRESS" ] || return 0
  if attached; then
    docker network disconnect -f "$EGRESS" "$CID" >/dev/null 2>&1 || true
  fi
  if attached; then
    CLEANUP_FAILED=1
    fail "the ingestor is STILL attached to $EGRESS"
    note "recover by hand:  docker network disconnect -f $EGRESS $CID"
    return 1
  fi
  if network_exists; then
    docker network rm "$EGRESS" >/dev/null 2>&1 || true
  fi
  if network_exists; then
    CLEANUP_FAILED=1
    fail "the $EGRESS network still exists and could not be removed"
    note "something else is attached to it:$(egress_members)"
    note "recover by hand:  docker network rm $EGRESS"
    return 1
  fi
  [ -n "$OPENED_AT" ] && HELD_SECONDS=$(( $(date +%s) - OPENED_AT ))
  return 0
}

report_window_closed() {
  hr "Closing the egress window"
  if close_egress; then
    local held=""
    [ -n "$HELD_SECONDS" ] && held=" (it was open for ${HELD_SECONDS}s)"
    pass "ingestor is on the internal network only; $EGRESS no longer exists$held"
  fi
}

on_exit() {
  local rc=$?
  set +e
  if [ -n "$CID" ] && [ -n "$EGRESS" ] && { attached || network_exists; }; then
    hr "Closing the egress window (from the exit trap)"
    close_egress && pass "ingestor is on the internal network only; $EGRESS removed"
  fi
  [ "$CLEANUP_FAILED" = 1 ] && rc=3
  exit "$rc"
}
trap on_exit EXIT
trap 'echo; note "interrupted -- closing the egress window"; exit 130' INT TERM HUP

# ------------------------------------------------------------- the stack --

hr "Event re-check"

CID="$(dc ps -q ingestor 2>/dev/null | tr -d '\r' | head -n1)"
if [ -z "$CID" ]; then
  fail "no running ingestor in this Compose project -- start the stack first (docker compose up -d)"
  exit 1
fi
CNAME="$(docker inspect -f '{{.Name}}' "$CID" | tr -d '/\r')"
PROJECT="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$CID" | tr -d '\r')"
EGRESS="${PROJECT}_recheck_egress"
note "project $PROJECT, ingestor $CNAME, egress window $EGRESS"

if ! curl -sf "$API/health" >/dev/null 2>&1; then
  fail "the API is not answering at $API -- set API=, or start the stack"
  exit 1
fi

if network_exists; then
  note "WARNING: $EGRESS already exists -- a previous re-check did not close its"
  note "         window. It is closed at the end of this run either way."
fi

mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LEDGER="$OUT_DIR/recheck-$STAMP.jsonl"

# ------------------------------------------------------------- the probe --

open_egress

hr "Reading the listing pages (the only step that needs the internet)"
note "one GET per distinct source_url, ${TIMEOUT_S}s timeout, nothing is written"
PROBE_RC=0
# Inside the ingestor, because it is the only container this window gave a route
# to. Its stdout is the ledger; its stderr is the running commentary.
dc exec -T \
  -e AOW_RECHECK_ALLOW_EGRESS=1 \
  -e AOW_API_BASE="http://api:8000" \
  ingestor python -m services.tools.event_recheck probe \
    --timeout "$TIMEOUT_S" --progress "${PY_ARGS[@]+"${PY_ARGS[@]}"}" \
  >"$LEDGER" || PROBE_RC=$?

# Closed the moment the fetch returns, success or failure. Everything below is
# local file reading and needs no route out.
report_window_closed

if [ ! -s "$LEDGER" ]; then
  fail "the probe produced no evidence (exit $PROBE_RC)"
  rm -f "$LEDGER"
  exit 2
fi

# ------------------------------------------------------------ the report --

hr "What the pages say"
# Run here, in the working tree, not in a container: `review` is pure stdlib
# over one local file -- no database, no broker, no sockets at all -- and the
# tooling image already has python3. Reading the report must not depend on the
# stack still being up.
python3 -m services.tools.event_recheck review --evidence "$LEDGER"

hr "Result"
note "evidence ledger: $LEDGER"
note "Nothing was written. To renew the rows the pages themselves confirmed:"
note "  docker compose -f compose.tools.yml run --rm --entrypoint python3 refresh \\"
note "      -m services.tools.event_recheck apply --evidence $LEDGER --all-confirmed --yes"
note "A row that is not 'confirmed' needs --id ID --note \"what you saw\", because"
note "no parser verified it -- you did."

if [ "$PROBE_RC" -ne 0 ]; then
  fail "the probe exited $PROBE_RC"
  exit 2
fi
if [ "$CLEANUP_FAILED" = 1 ]; then
  exit 3
fi
pass "every listing probed; egress window closed and verified"
exit "$FAILED"
