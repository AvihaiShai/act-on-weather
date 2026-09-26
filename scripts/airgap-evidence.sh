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
#   bash scripts/airgap-evidence.sh [--out FILE] host   [label]
#   bash scripts/airgap-evidence.sh [--out FILE] bundle [dir]
#   bash scripts/airgap-evidence.sh [--out FILE] run    -- <command> [args...]
#
# Use --out, not a pipe. `cmd | tee file` returns tee's exit status, so a failed
# install reads as a pass -- and the obvious place to put that file, next to the
# bundle being verified, is inside the folder whose unlisted-file check then
# refuses it. --out writes the file itself, refuses a path inside a bundle, and
# leaves the measured exit code as this script's own.
#
# Nothing else here changes the host.
#
# What this cannot do: prove a negative about the network. `host` samples the
# link at one instant, and `run` counts pulls the daemon *completed*. Neither
# can see a pull that was attempted and failed, because Docker emits no event
# for one. A disconnected cable is the evidence; these are the corroboration.
set -euo pipefail

usage() {
  # Selected by content, not by line number. A hard-coded range silently starts
  # printing the wrong paragraph the first time the header above is edited,
  # which is exactly what happened when --out was added.
  sed -n '/^#   bash scripts/,/^# Nothing else here changes the host\./p' "$0" \
    | sed 's/^# \{0,1\}//' >&2
  exit 2
}

OUT=""
if [ "${1:-}" = "--out" ]; then
  OUT="${2:?--out needs a file path}"
  shift 2

  # Where the file must not go. docs/RELEASE-PROOF.md used to tell the operator
  # to `tee evidence/...` from inside the bundle, which creates a file
  # SHA256SUMS does not list -- so the very next verify refuses the folder, and
  # the evidence run destroys the thing it was measuring. Walk up from the
  # destination and refuse if any ancestor looks like a release folder.
  out_dir="$(cd "$(dirname "$OUT")" 2>/dev/null && pwd)" \
    || { echo "--out directory does not exist: $(dirname "$OUT")" >&2; exit 2; }
  probe="$out_dir"
  while [ -n "$probe" ]; do
    if [ -f "$probe/SHA256SUMS" ] && [ -f "$probe/release-version.txt" ]; then
      echo "refusing to write evidence inside a release bundle: $probe" >&2
      echo "  SHA256SUMS does not list it, so the next verify-bundle.sh would" >&2
      echo "  refuse the folder. Choose a path outside the bundle." >&2
      exit 2
    fi
    [ "$probe" = "/" ] && break
    probe="$(dirname "$probe")"
  done

  # Pin it absolute, now that the directory has been resolved and cleared. The
  # `bundle` subcommand cds into the folder it is inspecting, and emit() appends
  # to $OUT by the path it was given -- so a relative --out would be checked
  # here, outside the bundle, and then written there, inside it. That is the one
  # way the guard above could be satisfied and still produce the unlisted file
  # it exists to prevent.
  OUT="$out_dir/$(basename "$OUT")"
  : > "$OUT"
fi

# Written once, to both places. Not `| tee`: this script's exit status has to
# stay the measured command's.
emit() {
  printf '%s\n' "$*"
  [ -n "$OUT" ] && printf '%s\n' "$*" >> "$OUT"
  return 0
}

say() { emit "$*"; }
rule() { emit "--- $*"; }

# A command that may legitimately be absent on a minimal target: report the
# absence rather than aborting the capture. Evidence with a hole in it is worth
# more than no evidence, as long as the hole says so.
try() {
  if command -v "$1" >/dev/null 2>&1; then
    local captured status
    captured="$("$@" 2>&1)" && status=0 || status=$?
    [ -n "$captured" ] && emit "$captured"
    [ "$status" -eq 0 ] || emit "(exit $status)"
  else
    say "($1 not installed on this host)"
  fi
  return 0
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
    emit "$(free -h | awk 'NR<=2')"
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
    --format '{{.Actor.Attributes.name}}' > "$events" 2>/dev/null &
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
  # PIPESTATUS[0], not $?, so `tee` cannot report success for a failed command.
  [ -n "$OUT" ] && cat "$transcript" >> "$OUT"

  end_epoch="$(date +%s)"
  sleep 2
  kill "$watcher" 2>/dev/null || true
  wait "$watcher" 2>/dev/null || true

  rule "measured"
  say "exit_code: $exit_code"
  say "elapsed_seconds: $((end_epoch - start_epoch))"
  say "finished_at: $(date -u +%Y-%m-%dT%H:%M:%SZ) (UTC)"
  local pulls
  pulls="$(grep -c . "$events" || true)"
  say "completed_image_pulls: $pulls"
  if [ "$pulls" != "0" ]; then
    # Named, so the record can be audited rather than believed. A pull of an
    # aow-bundle/* image during an offline install is a failed proof; a pull of
    # something the platform does in the background is a dirty measurement on a
    # connected machine, and on the disconnected target neither can happen.
    say "pulled_images:"
    sort "$events" | uniq -c | while read -r count name; do
      say "  $count x $name"
    done
  fi
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
  say "Any pulled_images line naming an aow-bundle/* image is a failed proof."

  exit "$exit_code"
}

case "${1:-}" in
  host) shift; evidence_host "$@" ;;
  bundle) shift; evidence_bundle "$@" ;;
  run) shift; evidence_run "$@" ;;
  *) usage ;;
esac
