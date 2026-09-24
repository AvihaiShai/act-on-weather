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
# So the refresh starts this, detached, while it is opening the window -- after
# the network exists and before the ingestor is on it. Its whole job is to make
# sure the window cannot outlive a deadline:
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
# It is started after the network is created and before the ingestor is attached
# to it, so the window is never unwatched while anything can route through it,
# and the guard's first look already finds the network. The short wait below is
# only a belt: two earlier orderings each failed a drill -- started before the
# network, the guard checked once, saw nothing, and exited a second before the
# window it existed to bound was created; given a long wait instead, it lingered
# after fast runs and adopted a later run's window under the wrong deadline.
#
# Usage (the refresh does this; it is not typed by hand):
#   bash refresh_window_guard.sh NETWORK CONTAINER DEADLINE_S [POLL_S] [APPEAR_S]
set -uo pipefail

EGRESS="${1:?network name}"
CID="${2:?ingestor container}"
DEADLINE="${3:?deadline in seconds}"
POLL="${4:-5}"
# How long to wait for the window to exist. The refresh creates it BEFORE
# starting this, so the normal answer is "already there" and this wait covers
# only a daemon slow to answer. Short on purpose: a long wait here is how a
# guard outlives its own run and starts watching the next one's window.
APPEAR="${5:-10}"

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
