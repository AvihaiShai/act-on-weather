#!/usr/bin/env bash
#
# The bound on the egress window (F4).
#
# scripts/refresh.sh closes its window from a trap, which survives a failed
# fetch, a Ctrl-C, a SIGTERM and a crash. It cannot survive SIGKILL, because
# nothing can: the shell is gone before it can run anything. Without this, a
# `kill -9` during a refresh leaves the ingestor attached to the egress network
# until somebody runs the command again -- which could be never. "Someone will
# notice" is not a boundary.
#
# So the refresh starts this, detached, BEFORE it opens the window. Its whole
# job is to make sure the window cannot outlive a deadline:
#
#   * it polls; the moment the network no longer exists, it exits. A normal run
#     closes the window in seconds, so in the normal case this exits seconds
#     later having done nothing;
#   * at the deadline it closes the window itself -- detaches the ingestor,
#     removes the network -- and exits.
#
# The deadline is the guarantee: however the command that opened the window
# died, the window is gone by then. A fetch that is somehow still running when
# the deadline passes loses its route out and fails; that is the safe direction,
# and it is reported as a failed refresh rather than left as a silent hole.
#
# It is deliberately dumb: no owner tracking, no labels to match, one loop and
# two Docker calls. A watchdog that can be wrong about whose window it is
# closing would be worse than the problem it solves.
#
# This runs in its own container with the Docker socket, started by the refresh
# and never part of the stack -- the same trade scripts/refresh.sh and the demos
# runner already make, and for the same reason: driving Docker IS the job.
# Nothing in the running stack has the socket.
#
# It is started before the network exists, deliberately: a guard started after
# the window opens has a gap in which a kill leaves an unguarded window. So it
# waits for the window to appear first, and gives up if it never does -- a
# refresh that died before opening anything leaves nothing to guard. Getting
# this backwards is how the first version of this script failed its own drill:
# it checked once, saw no network, decided the window was already closed, and
# exited a second before the window it was meant to bound was created.
#
# Usage (the refresh does this; it is not typed by hand):
#   bash refresh_window_guard.sh NETWORK CONTAINER DEADLINE_S [POLL_S] [APPEAR_S]
set -uo pipefail

EGRESS="${1:?network name}"
CID="${2:?ingestor container}"
DEADLINE="${3:?deadline in seconds}"
POLL="${4:-5}"
# How long to wait for the window to be created at all. The refresh creates it
# within a second of starting this; a minute is slack, not a design parameter.
APPEAR="${5:-60}"

log() { printf '%s window-guard %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

network_exists() { docker network inspect "$EGRESS" >/dev/null 2>&1; }

attached() {
  docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}' "$CID" \
    2>/dev/null | tr -d '\r' | grep -qw "$EGRESS"
}

log "watching $EGRESS on $CID; deadline ${DEADLINE}s, polling every ${POLL}s"

# Phase 1: wait for the window to be opened. Polls faster than the watch loop,
# because this is a race against one `docker network create` a moment from now.
waited=0
while ! network_exists; do
  if [ "$waited" -ge "$APPEAR" ]; then
    log "$EGRESS never appeared in ${APPEAR}s -- the refresh opened no window. Nothing to guard."
    exit 0
  fi
  sleep 1
  waited=$((waited + 1))
done
log "$EGRESS is open; it will be closed by ${DEADLINE}s from now at the latest"

# Phase 2: watch it. The deadline is counted from here, which is when the
# ingestor actually gains a route out.
elapsed=0
while [ "$elapsed" -lt "$DEADLINE" ]; do
  if ! network_exists; then
    log "$EGRESS is gone -- the refresh closed its own window. Nothing to do."
    exit 0
  fi
  sleep "$POLL"
  elapsed=$((elapsed + POLL))
done

# The deadline. Either the refresh was SIGKILLed, or it is wedged. Either way
# the window closes now.
if ! network_exists; then
  log "$EGRESS is gone. Nothing to do."
  exit 0
fi

log "DEADLINE REACHED after ${DEADLINE}s and $EGRESS still exists."
log "the refresh that opened it did not close it -- closing it now"
if attached; then
  docker network disconnect -f "$EGRESS" "$CID" >/dev/null 2>&1 || true
fi
docker network rm "$EGRESS" >/dev/null 2>&1 || true

if attached || network_exists; then
  log "FAILED to close $EGRESS; a human has to act"
  log "  docker network disconnect -f $EGRESS $CID && docker network rm $EGRESS"
  exit 1
fi
log "closed: the ingestor is off $EGRESS and the network is removed"
exit 0
