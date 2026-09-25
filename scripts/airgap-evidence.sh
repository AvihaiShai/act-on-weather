#!/usr/bin/env bash
# Capture the evidence an air-gap proof is made of, as files rather than as
# things an operator retyped from a checklist.
#
# docs/RELEASE-PROOF.md asks for nine artifacts and names a command for three
# of them. The rest -- engine identity, the store census before the load, the
# link state, the transferred archive's digest, timings, and a pull count --
# had no producing command at all, which is how "0 pull attempts" came to be a
# sentence in a document rather than a number from a run. This script produces
# them.
#
#   bash scripts/airgap-evidence.sh host   [label]
#   bash scripts/airgap-evidence.sh bundle [dir]
#   bash scripts/airgap-evidence.sh run    -- <command> [args...]
#
# Everything goes to stdout so the operator can tee it; nothing is written
# except the transcript `run` needs, and nothing here changes the host.
#
# What this cannot do: prove a negative about the network. `host` samples the
# link at one instant, and `run` counts pulls the daemon *completed*. Neither
# can see a pull that was attempted and failed, because Docker emits no event
# for one. A disconnected cable is the evidence; these are the corroboration.
set -euo pipefail

usage() {
  sed -n '3,20p' "$0" >&2
  exit 2
}

say() { printf '%s\n' "$*"; }
rule() { printf -- '--- %s\n' "$*"; }

# A command that may legitimately be absent on a minimal target: report the
# absence rather than aborting the capture. Evidence with a hole in it is worth
# more than no evidence, as long as the hole says so.
try() {
  if command -v "$1" >/dev/null 2>&1; then
    "$@" 2>&1 || say "(exit $?)"
  else
    say "($1 not installed on this host)"
  fi
}

evidence_host() {
  local label="${1:-unlabelled}"
  say "airgap-evidence host: $label"
  say "captured_at: $(date -u +%Y-%m-%dT%H:%M:%SZ) (UTC)"
  say "hostname: $(hostname)"
  say "uname: $(uname -a)"

  rule "docker engine"
  # The engine ID is the whole point of this block. Two hosts that report the
  # same ID are one host, whatever the case badges say.
  say "engine_id: $(docker info --format '{{.ID}}' 2>&1 || say unavailable)"
  say "server_version: $(docker version --format '{{.Server.Version}}' 2>&1 || say unavailable)"
  say "operating_system: $(docker info --format '{{.OperatingSystem}}' 2>&1 || say unavailable)"
  say "architecture: $(docker info --format '{{.Architecture}}' 2>&1 || say unavailable)"
  say "storage_driver: $(docker info --format '{{.Driver}}' 2>&1 || say unavailable)"
  say "compose_version: $(docker compose version --short 2>&1 || say 'unavailable -- Compose v2 is required')"

  rule "store census"
  # Counted, not listed: a fresh target should report zeroes, and a wall of
  # image names would bury that. `image ls -qa` includes untagged layers, which
  # the release installer's own census cannot see.
  say "images: $(docker image ls -qa 2>/dev/null | grep -c . || true)"
  say "volumes: $(docker volume ls -q 2>/dev/null | grep -c . || true)"
  say "containers: $(docker ps -aq 2>/dev/null | grep -c . || true)"
  say "networks: $(docker network ls -q 2>/dev/null | grep -c . || true)"

  rule "network links (point-in-time sample)"
  try ip -br link
  say ""
  try ip -br addr

  rule "routes and resolution"
  try ip route
  # Counted separately because this is the line a reader will quote. It must
  # never report 0 for a host that simply has no `ip` command: an absent tool
  # is not an absent route, and this capture exists to support exactly that
  # claim, so it is the one place a silent default would be a lie.
  if command -v ip >/dev/null 2>&1; then
    say "default_routes: $(ip route 2>/dev/null | grep -c '^default' || true) (0 is what a disconnected host reports)"
  else
    say 'default_routes: UNKNOWN -- ip is not installed, so this host cannot be checked this way'
  fi

  rule "resources"
  try df -h .
  say ""
  if command -v free >/dev/null 2>&1; then
    free -h | awk 'NR<=2'
  else
    say '(free not installed on this host)'
  fi

  rule "published ports in use"
  # The install publishes 8080 and 8000; a collision there fails after
  # `docker load` has already replaced images, which is the worst moment.
  try ss -ltnp
}

evidence_bundle() {
  local dir="${1:-.}"
  cd "$dir"
  say "airgap-evidence bundle: $(pwd)"
  say "captured_at: $(date -u +%Y-%m-%dT%H:%M:%SZ) (UTC)"
  for required in SHA256SUMS release-version.txt images.tar; do
    test -f "$required" || { say "not an offline release folder: no $required"; exit 1; }
  done

  rule "identity"
  say "release_version: $(cat release-version.txt)"

  rule "digests"
  # `sha256sum -c` prints "images.tar: OK" and never the digest, so an operator
  # asked to compare the archive on the medium against the archive on the
  # target has nothing to compare. These are those two numbers.
  say "sha256(SHA256SUMS): $(sha256sum SHA256SUMS | awk '{print $1}')"
  say "sha256(images.tar): $(sha256sum images.tar | awk '{print $1}')"
  say "bytes(images.tar): $(wc -c < images.tar | tr -d ' ')"

  rule "size and shape"
  say "files: $(find . -type f | grep -c . || true)"
  say "bytes_total: $(du -sb . 2>/dev/null | awk '{print $1}' || say unavailable)"
  say "has_env: $(test -f .env && say yes || say 'no -- install-offline.sh will refuse until one exists')"
}

evidence_run() {
  [ "${1:-}" = "--" ] && shift
  [ "$#" -gt 0 ] || usage

  local transcript started_at start_epoch watcher events exit_code end_epoch
  transcript="$(mktemp)"
  events="$(mktemp)"
  trap 'rm -f "$transcript" "$events"' EXIT

  # Attach the watcher before the command starts, and give the daemon
  # connection a moment to establish. A pull the watcher missed because it was
  # still connecting would read as a clean zero, which is the one failure this
  # number must not have.
  docker events --filter 'type=image' --filter 'event=pull' \
    --format '{{.Actor.ID}}' > "$events" 2>/dev/null &
  watcher=$!
  sleep 2

  started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  start_epoch="$(date +%s)"
  say "airgap-evidence run: $*"
  say "started_at: $started_at (UTC)"
  rule "transcript"

  set +e
  "$@" 2>&1 | tee "$transcript"
  exit_code="${PIPESTATUS[0]}"
  set -e

  end_epoch="$(date +%s)"
  sleep 2
  kill "$watcher" 2>/dev/null || true
  wait "$watcher" 2>/dev/null || true

  rule "measured"
  say "exit_code: $exit_code"
  say "elapsed_seconds: $((end_epoch - start_epoch))"
  say "finished_at: $(date -u +%Y-%m-%dT%H:%M:%SZ) (UTC)"
  say "completed_image_pulls: $(grep -c . "$events" || true)"
  # A pull that failed because there is no network emits no event, so the
  # daemon's count cannot see it. What can is the text the tools print on the
  # way to trying: Compose announces "Pulling"/"Building" before it acts, and
  # a refused connection names the registry host.
  say "transcript_pull_or_build_markers: $(grep -Ec '^ *(Pulling|Building) |Pulling from |Error response from daemon: Get \"https' "$transcript" || true)"
  say "transcript_lines: $(grep -c . "$transcript" || true)"
  say ""
  say "A clean offline run reads: exit_code 0, completed_image_pulls 0,"
  say "transcript_pull_or_build_markers 0. A non-zero marker count with zero"
  say "completed pulls is the signature of a pull that was attempted and failed."

  exit "$exit_code"
}

case "${1:-}" in
  host) shift; evidence_host "$@" ;;
  bundle) shift; evidence_bundle "$@" ;;
  run) shift; evidence_run "$@" ;;
  *) usage ;;
esac
