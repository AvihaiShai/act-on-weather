#!/usr/bin/env bash
#
# The operator refresh (M12). One command, and the only supported way to open
# the egress window on a running stack.
#
#   docker compose -f compose.tools.yml run --rm refresh        # any OS
#   bash scripts/refresh.sh                                     # bash hosts
#
# What it does, in order:
#
#   1. records what is stored now, per city (as-of, last day covered);
#   2. creates a bridge network and attaches ONLY the ingestor container to it;
#   3. runs `services.ingestor.refresh --json` inside it, which fetches the
#      forecast and fsyncs it into the outbox -- it never writes to the DB;
#   4. closes the egress window again and ASSERTS it is closed;
#   5. follows the accepted message ids to the broker and to `ingest_log`;
#   6. prints per-city success/failure, before/after as-of, the accepted ids
#      and how many of them are stored versus still in flight;
#   7. files that same report where the UI can read it, at GET /refresh/last,
#      so the page can show what the last run did and not just how fresh the
#      stored data is. `--check` files nothing; it changes nothing.
#
# Steps 4 and 7 also run from a trap, so an interrupt (Ctrl-C), a failed fetch
# or a crash mid-way still ends with the ingestor back on the internal network
# and with a record of how far the run got.
#
# SIGKILL beats any trap, so the window does not rely on one. Between creating
# the network and attaching the ingestor to it, this starts a detached guard
# (scripts/refresh_window_guard.sh) that closes the window after
# REFRESH_WINDOW_MAX_S (default 600) whatever happened to this process. A normal
# run closes its own window in seconds and the guard exits having done nothing.
# The next run also detects and closes an inherited window, but the guard is what
# makes the lifetime bounded rather than merely likely.
#
# It acts on the `aow` project by default. AOW_PROJECT picks a different stack
# and AOW_API a different API address; see compose.tools.yml.
#
# Why `docker network connect` rather than a Compose overlay: it adds the one
# interface and removes it again without recreating the container. A recreate
# would restart the ingestor mid-refresh and re-accept the whole snapshot, and
# -- run from inside the tools container -- would resolve the overlay's bind
# mounts against the wrong filesystem. This touches one network attachment on
# one container and nothing else.
#
# The window is its own bridge network, created here and REMOVED again at the
# end, rather than the `egress` network compose.yml declares. Two reasons: a
# plain `docker compose up -d` never creates that one (no service uses it, so
# Compose does not bother), and a network that does not exist afterwards is a
# stronger thing to assert than an attachment that is merely absent. The
# declared `egress` network still exists for the manual sequence and for
# `make snapshot`, which the README documents.
#
# Exit codes:
#   0  every requested city refreshed, egress window verified closed
#   1  the stack is not in a state where a refresh can run
#   2  the fetch failed for at least one city (egress window still closed)
#   3  the egress window could NOT be closed -- act on this
#   4  accepted and queued, but nothing had reached the database in time
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=demos/lib.sh
. demos/lib.sh

WAIT_S="${REFRESH_WAIT_S:-180}"
# The hard lifetime of the egress window, enforced from outside this process by
# scripts/refresh_window_guard.sh. Generous: the fetch itself is bounded at 20s
# per city by the provider timeout, so a five-city refresh cannot take more than
# about two minutes even when every city times out.
WINDOW_MAX_S="${REFRESH_WINDOW_MAX_S:-600}"
GUARD_IMAGE="${REFRESH_GUARD_IMAGE:-aow/demos:dev}"
CHECK=0
PY_ARGS=()
CITY_FILTER=()

usage() {
  cat <<'EOF'
Operator refresh -- fetch a newer forecast through a temporary egress window.

  docker compose -f compose.tools.yml run --rm refresh [options]
  bash scripts/refresh.sh [options]

Options:
  --city SLUG     refresh one city; repeatable. Default: every city.
  --days N        forecast days to request (default 16).
  --wait N        seconds to wait for the accepted rows to reach the database
                  (default 180, or REFRESH_WAIT_S).
  --check         open and close the egress window without fetching anything.
                  The drill for "does this always put the ingestor back?".
  -h, --help      this text.

Environment:
  API             where to reach the API (default http://localhost:8000; the
                  tools container sets http://edge:8000, or AOW_API).
  AOW_PROJECT     which Compose project to act on (default aow). Read by
                  compose.tools.yml, which sets COMPOSE_PROJECT_NAME and picks
                  the stack's backend network from it.
  OPEN_METEO_URL  refresh from an internal mirror instead of the public API.
  REFRESH_WINDOW_MAX_S
                  hard lifetime of the egress window in seconds (default 600),
                  enforced by a detached guard so that even a SIGKILL of this
                  command cannot leave the window open past it.
  REFRESH_GUARD_IMAGE
                  image the guard runs in (default aow/demos:dev, the same image
                  this command runs in).

Every run but --check files its report at GET /refresh/last, which the UI shows
under Update data -> Operator refresh.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1; shift ;;
    --wait) WAIT_S="$2"; shift 2 ;;
    --days) PY_ARGS+=(--days "$2"); shift 2 ;;
    --city) PY_ARGS+=(--city "$2"); CITY_FILTER+=("$2"); shift 2 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; echo >&2; usage >&2; exit 2 ;;
  esac
done

FAILED=0
CLEANUP_FAILED=0
EGRESS_LABEL="aow.role=operator-refresh-window"
CID=""
CNAME=""
EGRESS=""
OPENED_AT=""
GUARD_ID=""

# What this run will file about itself (step 7). Kept in a temp directory the
# exit trap reads, so a run that is interrupted halfway still records the half
# it got through rather than nothing at all.
RUN_STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
STATE_DIR="$(mktemp -d)"
PERSIST=1
WINDOW_OPENED=0
WINDOW_CLOSED=0
WINDOW_INHERITED=0
HELD_SECONDS=""
REPORTED=0

# ------------------------------------------------------------ the window --

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

# The bound. Detached, so that the window has a hard lifetime no matter what
# happens to this process -- including SIGKILL, which no trap in here can
# survive. Called from open_egress between the network's creation and the
# ingestor's attachment; see the note there for why that is the only ordering
# that works. See also scripts/refresh_window_guard.sh.
#
# The script is passed to bash as an argument rather than mounted, because this
# process is itself inside a container and does not know the host path of the
# working tree it is reading. It stays a reviewable file in the repo either way.
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
      --label "aow.role=operator-refresh-window-guard" \
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
  # Order matters, and the drill is what settled it. The network is created
  # first, then the guard, then the attachment:
  #
  #   * guard before the network, and its first look finds nothing. It then has
  #     to sit in a timed wait for a window to appear, which makes it linger for
  #     the length of that wait after a fast run and lets it adopt a LATER run's
  #     window under this run's deadline. Both observed.
  #   * guard after the attachment, and there is a gap in which a kill leaves a
  #     window nothing is watching.
  #
  # Between create and connect there is no route out for anyone -- the network
  # exists and has no members -- so a kill there is harmless, and the next run
  # removes the empty network.
  if ! network_exists; then
    docker network create --label "$EGRESS_LABEL" "$EGRESS" >/dev/null
  fi
  start_window_guard
  # Already attached means a previous run was killed before it could close its
  # window. Adopt it rather than refusing: this run closes it either way, which
  # is the whole point of running this command instead of typing the sequence.
  if ! attached; then
    docker network connect "$EGRESS" "$CID" >/dev/null
  fi
  OPENED_AT="$(date +%s)"
  WINDOW_OPENED=1
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

# Idempotent, and safe to call when the window was never opened. Every exit
# path goes through it: detach the ingestor, then delete the network, then
# assert both.
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
  WINDOW_CLOSED=1
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

# ------------------------------------------------------- recording the run --
#
# Everything above happens in somebody's terminal. This files it where the UI
# can read it, so that "what did the last refresh actually do?" has an answer on
# the page as well as in the scrollback. It is one JSON object, written inside
# the ingestor container onto a named volume that the api mounts read-only, and
# served at GET /refresh/last. services/common/refresh_state.py says why this is
# a file and not a row.
#
# Called once, from the exit trap, after the window has been dealt with -- so it
# runs on every path, and so it can state whether the window closed. A run that
# was interrupted before the fetch records the part it reached; `--check`
# records nothing, because it changes nothing and overwriting the last real
# refresh with a window test would lose the more useful of the two.
persist_report() {
  [ "$PERSIST" = 1 ] || return 0
  [ "$REPORTED" = 1 ] && return 0
  REPORTED=1
  [ -n "$CID" ] || return 0

  local outcome
  if [ "$CLEANUP_FAILED" = 1 ]; then
    outcome="window-not-closed"
  elif [ ! -s "$STATE_DIR/fetch.json" ]; then
    outcome="no-fetch-report"
  elif [ -n "$(cat "$STATE_DIR/failed_cities" 2>/dev/null)" ]; then
    outcome="cities-failed"
  elif [ "${TOTAL_IDS:-0}" -gt 0 ] && [ "${STORED:-0}" -eq 0 ]; then
    outcome="accepted-not-stored"
  elif [ "${STORED:-0}" -lt "${TOTAL_IDS:-0}" ]; then
    outcome="ok-with-rows-in-flight"
  else
    outcome="ok"
  fi

  local report
  report="$(
    AOW_STARTED="$RUN_STARTED" \
    AOW_OUTCOME="$outcome" \
    AOW_EXIT="${1:-}" \
    AOW_PROJECT_NAME="$PROJECT" \
    AOW_WINDOW="$EGRESS" \
    AOW_WINDOW_OPENED="$WINDOW_OPENED" \
    AOW_WINDOW_CLOSED="$WINDOW_CLOSED" \
    AOW_WINDOW_INHERITED="$WINDOW_INHERITED" \
    AOW_WINDOW_DEADLINE="$([ -n "$GUARD_ID" ] && echo "$WINDOW_MAX_S" || echo "")" \
    AOW_HELD="${HELD_SECONDS:-}" \
    AOW_ACCEPTED="${TOTAL_IDS:-0}" \
    AOW_PUBLISHED="${PUBLISHED:-unknown}" \
    AOW_STORED="${STORED:-0}" \
    AOW_STATE_DIR="$STATE_DIR" \
    python3 "$(dirname "$0")/refresh_report.py" 2>/dev/null
  )" || report=""

  if [ -z "$report" ]; then
    note "could not assemble the run report -- the run itself is unaffected"
    return 0
  fi
  if printf '%s' "$report" \
    | dc exec -T ingestor python -m services.common.refresh_state >/dev/null 2>&1; then
    note "filed this run at GET /refresh/last (outcome: $outcome)"
  else
    note "could not file the run report in the ingestor (outcome: $outcome)"
    note "the refresh itself is unaffected; GET /refresh/last still shows the previous run"
  fi
}

on_exit() {
  local rc=$?
  set +e
  # The backstop. If the body already closed the window this is a no-op; if it
  # died before getting there, this is what saves the boundary.
  if [ -n "$CID" ] && [ -n "$EGRESS" ] && { attached || network_exists; }; then
    hr "Closing the egress window (from the exit trap)"
    close_egress && pass "ingestor is on the internal network only; $EGRESS removed"
  fi
  [ "$CLEANUP_FAILED" = 1 ] && rc=3
  # After the window, never before it: the boundary matters more than the
  # bookkeeping, and this records whether the boundary held.
  persist_report "$rc"
  rm -rf "$STATE_DIR" 2>/dev/null
  exit "$rc"
}
trap on_exit EXIT
trap 'echo; note "interrupted -- closing the egress window"; exit 130' INT TERM HUP

# ------------------------------------------------------------- the stack --

json_field() {
  python3 -c 'import json,sys;print(json.load(sys.stdin).get(sys.argv[1]) or "-")' "$1" 2>/dev/null \
    || echo "-"
}

# as-of and last covered day for one city, from what is stored right now.
city_state() {
  curl -s "$API/weather/$1" | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    rows = []
if not isinstance(rows, list) or not rows:
    print("none|-|0")
else:
    stamp = max(str(r["as_of"]) for r in rows)[:19].replace("T", " ") + "Z"
    print("%s|%s|%d" % (stamp,
                        max(str(r["forecast_date"]) for r in rows), len(rows)))
' 2>/dev/null || echo "none|-|0"
}

hr "Operator refresh"

CID="$(dc ps -q ingestor 2>/dev/null | tr -d '\r' | head -n1)"
if [ -z "$CID" ]; then
  fail "no running ingestor in this Compose project -- start the stack first (docker compose up -d)"
  exit 1
fi
CNAME="$(docker inspect -f '{{.Name}}' "$CID" | tr -d '/\r')"
PROJECT="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$CID" | tr -d '\r')"
EGRESS="${PROJECT}_refresh_egress"
note "project $PROJECT, ingestor $CNAME, egress window $EGRESS"

# A previous run that was SIGKILLed can only show up here: the window it made
# is still there. Say so rather than silently inheriting it, and close it now
# whether or not the rest of this run gets that far.
if network_exists; then
  WINDOW_INHERITED=1
  note "WARNING: $EGRESS already exists -- a previous refresh did not close its"
  note "         window. It is closed at the end of this run either way."
  if attached; then
    note "         The ingestor is attached to it right now."
  fi
fi

# Everything this script asks the API for is a GET -- health, cities, coverage
# and stored weather -- reached over the internal network rather than through a
# published port. Nothing here writes: the refresh is accepted into the
# ingestor's outbox, not posted. If the edge ever requires credentials for
# reads, this check is where that surfaces first.
if ! curl -sf "$API/health" >/dev/null 2>&1; then
  fail "the API is not answering at $API -- set API=, start the stack, or pass credentials if it now needs them"
  exit 1
fi

CITIES="${CITY_FILTER[*]:-}"
if [ -z "$CITIES" ]; then
  CITIES="$(curl -s "$API/cities" | python3 -c \
    'import json,sys;print(" ".join(c["id"] for c in json.load(sys.stdin)))')"
fi

printf '%s' "$CITIES" >"$STATE_DIR/requested"

hr "Stored now"
BEFORE_COV="$(curl -s "$API/coverage")"
printf '%s' "$BEFORE_COV" >"$STATE_DIR/cov_before.json"
note "weather as-of $(printf '%s' "$BEFORE_COV" | json_field weather_as_of), covering $(printf '%s' "$BEFORE_COV" | json_field weather_first_date) .. $(printf '%s' "$BEFORE_COV" | json_field weather_last_date)"
declare -A BEFORE
printf '   %-12s %-21s %-12s %s\n' "city" "as-of (UTC)" "covers-to" "days"
for c in $CITIES; do
  BEFORE[$c]="$(city_state "$c")"
  IFS='|' read -r b_asof b_last b_rows <<<"${BEFORE[$c]}"
  printf '   %-12s %-21s %-12s %s\n' "$c" "$b_asof" "$b_last" "$b_rows"
  # The same numbers the run report carries, written as they are printed, so
  # what the page shows and what the terminal showed cannot drift apart.
  printf '%s\t%s\t%s\t%s\n' "$c" "$b_asof" "$b_last" "$b_rows" >>"$STATE_DIR/before.tsv"
done

if [ "$CHECK" = 1 ]; then
  # A window test is not a refresh. It records nothing, because overwriting the
  # last real refresh with "someone checked the window" would lose the report an
  # operator came to the page for.
  PERSIST=0
  note "--check: opening and closing the window without fetching anything"
  open_egress
  report_window_closed
  hr "Result"
  if [ "$CLEANUP_FAILED" = 0 ]; then
    pass "the egress window opens and closes cleanly"
  else
    fail "the egress window did not close"
  fi
  exit "$FAILED"
fi

# ------------------------------------------------------------- the fetch --

open_egress

hr "Fetching (the only step that needs the internet)"
# In the state directory, not /tmp: the exit trap reads it to build the run
# report, so an interrupt after the fetch still records what the fetch returned.
REPORT_FILE="$STATE_DIR/fetch.json"
FETCH_RC=0
# The mirror override is passed through only when one is actually set. An empty
# -e would *unset* the provider's default URL inside the ingestor, and every
# city would fail with "no scheme supplied" -- which is how this line was first
# written, and what the drill caught.
EXEC_ENV=()
[ -n "${OPEN_METEO_URL:-}" ] && EXEC_ENV=(-e "OPEN_METEO_URL=$OPEN_METEO_URL")
dc exec -T "${EXEC_ENV[@]+"${EXEC_ENV[@]}"}" ingestor \
  python -m services.ingestor.refresh --json "${PY_ARGS[@]+"${PY_ARGS[@]}"}" \
  >"$REPORT_FILE" || FETCH_RC=$?

# Closed as soon as the fetch returns, success or failure. Everything below
# reads the stack from the inside and needs no route out.
report_window_closed

if [ ! -s "$REPORT_FILE" ]; then
  fail "the fetch produced no report (exit $FETCH_RC) -- nothing was accepted"
  exit 2
fi

hr "What the provider returned"
python3 - "$REPORT_FILE" <<'PY'
import json
import sys

report = json.load(open(sys.argv[1], encoding="utf-8"))
row = "   %-12s %-8s %-6s %-21s %-12s %s"
print(row % ("city", "result", "days", "new as-of (UTC)", "covers-to", "accepted ids"))
for c in report["cities"]:
    ids = c["message_ids"]
    shown = "-"
    if ids:
        shown = ids[0]
        if len(ids) > 1:
            shown += " (+%d more)" % (len(ids) - 1)
    stamp = (c["as_of"] or "-")[:19].replace("T", " ")
    print(row % (c["city"], "ok" if c["ok"] else "FAILED", c["accepted"] or "-",
                 stamp, c["last_date"] or "-", shown))
    if not c["ok"]:
        # One line. The provider's failure is a paragraph of URL-encoded query
        # string, and it is already in the fetch log directly above.
        reason = " ".join((c["error"] or "").split())
        print("   %-12s   %s" % ("", reason[:150] + ("..." if len(reason) > 150 else "")))
print()
print("   accepted %d message(s); ingestor outbox total=%d pending=%d"
      % (report["accepted"], report["outbox"]["total"], report["outbox"]["pending"]))
PY

IDS="$(python3 -c \
  'import json,sys;print(",".join(json.load(open(sys.argv[1],encoding="utf-8"))["message_ids"]))' \
  "$REPORT_FILE")"
TOTAL_IDS="$(python3 -c 'import sys;print(len([i for i in sys.argv[1].split(",") if i]))' "$IDS")"
# Recorded here rather than at the verdict below, because the exit trap reads it
# and an interrupt during the wait-for-storage loop must still record which
# cities the provider refused.
FAILED_CITIES="$(python3 -c \
  'import json,sys;print(" ".join(json.load(open(sys.argv[1],encoding="utf-8"))["failed_cities"]))' \
  "$REPORT_FILE")"
printf '%s' "$FAILED_CITIES" >"$STATE_DIR/failed_cities"

# ------------------------------------------------- follow them to the DB --

# The ingestor keeps its own outbox on its own volume and the API cannot read
# it, so a record accepted there is traced by asking that service -- the same
# thing demos/lib.sh does for the failure drills.
published_count() {
  dc exec -T -e AOW_IDS="$IDS" ingestor python -c '
import json, os
from services.common import config
from services.common.outbox import Outbox

ids = [i for i in os.environ["AOW_IDS"].split(",") if i]
rows = [Outbox(config.OUTBOX_PATH).status_of(i) for i in ids]
print(json.dumps({"published": sum(1 for r in rows if r and r["published_at"])}))
' | tr -d '\r'
}

stored_count() {
  psql_q "SELECT count(*) FROM ingest_log WHERE message_id = ANY(string_to_array('$IDS', ','))" \
    | tr -d '\r '
}

STORED=0
PUBLISHED=0
if [ "$TOTAL_IDS" -gt 0 ]; then
  hr "Where those messages got to"
  note "waiting up to ${WAIT_S}s for the consumer to store them"
  elapsed=0
  while :; do
    STORED="$(stored_count || echo 0)"
    [ -z "$STORED" ] && STORED=0
    [ "$STORED" -ge "$TOTAL_IDS" ] && break
    [ "$elapsed" -ge "$WAIT_S" ] && break
    sleep 3
    elapsed=$((elapsed + 3))
  done
  # "unknown" rather than 0 when the query itself fails: a number that is
  # quietly wrong is worse here than an admission.
  PUBLISHED="$(published_count 2>/dev/null | python3 -c \
    'import json,sys;print(json.load(sys.stdin)["published"])' 2>/dev/null || echo unknown)"
  printf '   %-36s %s\n' "accepted into the ingestor outbox" "$TOTAL_IDS"
  printf '   %-36s %s\n' "published to the broker" "$PUBLISHED / $TOTAL_IDS"
  printf '   %-36s %s\n' "stored in Postgres (ingest_log)" "$STORED / $TOTAL_IDS"
  printf '   %-36s %s\n' "still in flight" "$((TOTAL_IDS - STORED))"
  if [ "$STORED" -lt "$TOTAL_IDS" ]; then
    note "in-flight rows are owed, not lost: they are fsynced in the ingestor's"
    note "outbox and replay on their own. Re-run this command, or watch"
    note "GET /coverage, until the count catches up."
  fi
fi

hr "Stored after the refresh"
AFTER_COV="$(curl -s "$API/coverage")"
printf '%s' "$AFTER_COV" >"$STATE_DIR/cov_after.json"
note "weather as-of $(printf '%s' "$AFTER_COV" | json_field weather_as_of), covering $(printf '%s' "$AFTER_COV" | json_field weather_first_date) .. $(printf '%s' "$AFTER_COV" | json_field weather_last_date)"
printf '   %-12s %-21s %-12s %-21s %s\n' "city" "as-of before" "covers-to" "as-of after" "covers-to"
for c in $CITIES; do
  IFS='|' read -r b_asof b_last _ <<<"${BEFORE[$c]}"
  IFS='|' read -r a_asof a_last a_rows <<<"$(city_state "$c")"
  printf '   %-12s %-21s %-12s %-21s %s\n' "$c" "$b_asof" "$b_last" "$a_asof" "$a_last"
  printf '%s\t%s\t%s\t%s\n' "$c" "$a_asof" "$a_last" "$a_rows" >>"$STATE_DIR/after.tsv"
done

# ---------------------------------------------------------------- verdict --

hr "Result"
if [ -n "$FAILED_CITIES" ]; then
  fail "the provider did not answer for: $FAILED_CITIES"
  note "their stored forecast is unchanged and still carries its older as-of"
  exit 2
fi
if [ "$FETCH_RC" -ne 0 ]; then
  fail "the fetch exited $FETCH_RC"
  exit 2
fi
if [ "$TOTAL_IDS" -gt 0 ] && [ "$STORED" -eq 0 ]; then
  fail "accepted and queued, but nothing reached the database within ${WAIT_S}s -- check the consumer"
  exit 4
fi
pass "refreshed $TOTAL_IDS forecast day(s); $STORED stored; egress window closed and verified"
exit 0
