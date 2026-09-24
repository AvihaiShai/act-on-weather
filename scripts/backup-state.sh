#!/usr/bin/env bash
#
# Operational backup of the live state of a running act-on-weather stack.
#
#   bash scripts/backup-state.sh [--project NAME] [--dir DIR] [--env-file FILE]
#
# This is the *operational* backup: a copy of what the running system holds
# right now, taken without stopping it, so that a lost volume, a corrupted
# database or a bad day can be rolled back to a known point. It is NOT the
# pre-upgrade dump and it is NOT the installer rollback that the offline
# release path (scripts/package-offline.sh, scripts/install-offline.sh,
# scripts/restore-offline.sh) takes around a version change. Those protect a
# deployment; this protects the data.
#
# ---------------------------------------------------------------- what it takes
#
#   postgres.dump               pg_dump -Fc of the application database. A
#                               logical dump, taken through Postgres itself, not
#                               a copy of pgdata: a filesystem snapshot of a
#                               running Postgres is not a backup, because the
#                               heap, the WAL and the buffers it has not flushed
#                               yet are three different points in time.
#
#   outbox-<producer>.sqlite3   An online-consistent copy of each of the three
#                               producer outboxes (ingestor, api, enricher).
#                               See scripts/sqlite_backup.py for why copying the
#                               file is the wrong thing to do.
#
#   rabbitmq-definitions.json   Exchanges, queues, bindings, policies, users and
#                               permissions. NOT the messages.
#
#   manifest.json               Byte size and SHA-256 of every artefact, the
#                               migration files in the tree, the image digests
#                               in use, and the timestamps. `started_at` is the
#                               RPO reference point: anything the system
#                               accepted after it is outside this backup.
#
# ------------------------------------------------------- what it does NOT take
#
# Message bodies sitting in aow.ingest or aow.dlq are deliberately not backed
# up. RabbitMQ has no supported consistent export of queue contents, and the
# system does not need one: a message in flight was already fsynced into its
# producer's outbox before it was published (services/common/outbox.py), so the
# outbox copies above hold the bytes, and services/common/reconcile.py can
# republish any envelope the restored database cannot account for. That is what
# scripts/restore-state.sh does, and the manifest records how many messages
# were in each queue at backup time so the number is visible rather than
# implied.
#
# ------------------------------------------------------------------- how it runs
#
# Docker only. No host psql, no host sqlite3, no host Python: every command
# runs inside a container, artefacts arrive over the container's stdout and
# checksums are computed by piping the file back into a container's stdin. That
# is also why nothing here bind-mounts the working tree -- a bind mount is the
# one thing in this repository that behaves differently on Windows, on macOS
# and inside the tools container.
#
# Exit codes:
#   0  a complete backup whose manifest validates
#   1  the stack is not in a state where a backup can be taken
#   2  an artefact could not be taken, or the manifest did not validate
set -euo pipefail

cd "$(dirname "$0")/.."

# Git Bash (MSYS) rewrites arguments that look like absolute POSIX paths into
# Windows paths before handing them to docker.exe. Almost every absolute path
# in this script is a path INSIDE a container -- /outbox/outbox.sqlite3,
# /tmp/aow-definitions.json -- and that rewrite would send `docker exec` looking
# at C:\Users\...\Temp instead. So the translation is switched off here, and the
# one place that still needs it (a host path handed to docker as --env-file) is
# translated explicitly with cygpath below. Both variables are inert on Linux
# and macOS; every host file this script reads or writes is opened by bash
# itself, which is unaffected either way.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

# The stack to back up. `aow` is what `docker compose up -d` creates and what
# every command in the README means, so an operator never sets this; the drill
# sets it to run against an isolated project.
PROJECT="${AOW_PROJECT:-aow}"
ENV_FILE="${AOW_ENV_FILE:-.env}"
BACKUP_DIR=""
# An optional extra Compose file, layered on compose.yml. CI builds the service
# image under its own tag and selects it with compose.ci.yml, so a command that
# can only address the developer's `aow/services:dev` is a command CI cannot
# run. Everything else about the stack is unchanged.
OVERLAY="${AOW_COMPOSE_OVERLAY:-}"

usage() {
  cat <<'EOF'
Back up the live state of a running act-on-weather stack.

  bash scripts/backup-state.sh [options]

  --project NAME    the Compose project to back up (default: aow)
  --dir DIR         where to write the backup
                    (default: ./backups/<UTC timestamp>)
  --env-file FILE   the env file the Compose project was started with
                    (default: .env)
  --overlay FILE    an extra Compose file layered on compose.yml, for a stack
                    started with one (e.g. compose.ci.yml). Also AOW_COMPOSE_OVERLAY.
  -h, --help        this text

The last line of output is BACKUP_DIR=<path>, for scripting.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --dir)     BACKUP_DIR="$2"; shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --overlay) OVERLAY="$2"; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

COMPOSE_FILES=(-f compose.yml)
if [ -n "$OVERLAY" ]; then
  [ -f "$OVERLAY" ] || { echo "no such Compose overlay: $OVERLAY" >&2; exit 1; }
  COMPOSE_FILES+=(-f "$OVERLAY")
fi

# The env file is a HOST path handed to docker.exe, so it is the one argument
# that still needs the MSYS translation switched off above. cygpath exists only
# on Git Bash; everywhere else this leaves the path exactly as given.
ENV_FILE_ARG="$ENV_FILE"
if command -v cygpath >/dev/null 2>&1; then
  ENV_FILE_ARG="$(cygpath -w "$ENV_FILE" 2>/dev/null || printf '%s' "$ENV_FILE")"
fi

# One spelling of "talk to that stack", so nothing below can drift onto a
# different project than the manifest records.
dc() {
  if [ -f "$ENV_FILE" ]; then
    docker compose --env-file "$ENV_FILE_ARG" -p "$PROJECT" "${COMPOSE_FILES[@]}" "$@"
  else
    docker compose -p "$PROJECT" "${COMPOSE_FILES[@]}" "$@"
  fi
}

say()  { printf '   %s\n' "$*"; }
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die()  { printf '\033[31mbackup failed: %s\033[0m\n' "$*" >&2; exit "${2:-2}"; }

# ---------------------------------------------------------------- preflight --

running() { [ -n "$(dc ps -q "$1" 2>/dev/null)" ]; }

for required in postgres rabbitmq; do
  running "$required" || die "the $required container of project '$PROJECT' is not running" 1
done

# The image that runs the helper containers: hashing and manifest validation.
# Taken from the stack itself rather than hardcoded, so the backup is measured
# with the same Python the system runs, and so this keeps working if the tag
# changes. AOW_SERVICES_IMAGE is the escape hatch when no producer is up.
HELPER_IMAGE="${AOW_SERVICES_IMAGE:-}"
if [ -z "$HELPER_IMAGE" ]; then
  for candidate in api ingestor consumer enricher; do
    cid="$(dc ps -q "$candidate" 2>/dev/null || true)"
    if [ -n "$cid" ]; then
      HELPER_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$cid")"
      break
    fi
  done
fi
[ -n "$HELPER_IMAGE" ] || HELPER_IMAGE="aow/services:dev"
docker image inspect "$HELPER_IMAGE" >/dev/null 2>&1 \
  || die "the helper image $HELPER_IMAGE is not present; build it or set AOW_SERVICES_IMAGE" 1

# The manifest's start timestamp, taken before a single byte is read. It is the
# conservative end of the window on purpose: a record accepted while the backup
# was running may or may not have made it into the dump, and an RPO an operator
# can rely on has to be the moment after which nothing is promised.
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BACKUP_ID="$(date -u +%Y%m%dT%H%M%SZ)"
[ -n "$BACKUP_DIR" ] || BACKUP_DIR="backups/$BACKUP_ID"
mkdir -p "$BACKUP_DIR"

PGUSER_NAME="${POSTGRES_USER:-aow}"
PGDB_NAME="${POSTGRES_DB:-aow}"

step "Backing up project '$PROJECT' into $BACKUP_DIR"
say "backup id:     $BACKUP_ID"
say "started at:    $STARTED_AT   <- the RPO reference point"
say "helper image:  $HELPER_IMAGE"

# ------------------------------------------------------------------ helpers --

# SHA-256 and byte count of a file, computed inside a container. The file goes
# in on stdin, so there is no bind mount and no host-path translation -- the
# one thing that differs between Git Bash, macOS and the tools container.
digest_of() {
  docker run --rm -i "$HELPER_IMAGE" python -c '
import hashlib, sys
h = hashlib.sha256()
n = 0
for chunk in iter(lambda: sys.stdin.buffer.read(1 << 20), b""):
    h.update(chunk)
    n += len(chunk)
print(h.hexdigest(), n)
' < "$1"
}

ARTEFACTS=()

# Record one artefact in the manifest, after measuring what was actually
# written. Nothing is taken on trust: the size and checksum here are read back
# off the file, not predicted from what the producing command said it did.
record_artefact() { # name kind service container_path
  local name="$1" kind="$2" service="$3" container_path="$4" sha bytes
  read -r sha bytes <<EOF
$(digest_of "$BACKUP_DIR/$name")
EOF
  [ "${bytes:-0}" -gt 0 ] || die "$name came out empty"
  ARTEFACTS+=("{\"name\": \"$name\", \"kind\": \"$kind\", \"service\": \"$service\", \"present\": true, \"bytes\": $bytes, \"sha256\": \"$sha\", \"container_path\": \"$container_path\"}")
  say "$name  ${bytes} bytes  sha256:${sha}"
}

record_absent() { # name kind service reason
  ARTEFACTS+=("{\"name\": \"$1\", \"kind\": \"$2\", \"service\": \"$3\", \"present\": false, \"reason\": \"$4\"}")
  say "$1  ABSENT -- $4"
}

# ------------------------------------------------------------------ postgres --

step "Postgres"
say "pg_dump -Fc, through the database itself, into $BACKUP_DIR/postgres.dump"
# Custom format: compressed, and pg_restore can then be selective about what it
# puts back. Run inside the container over the unix socket, which is the one
# connection the image trusts without a password, so no credential crosses the
# host.
if ! dc exec -T postgres pg_dump -U "$PGUSER_NAME" -d "$PGDB_NAME" -Fc > "$BACKUP_DIR/postgres.dump"; then
  die "pg_dump failed"
fi
record_artefact postgres.dump postgres-custom-dump postgres "-"

# ------------------------------------------------------------------ outboxes --

step "Producer outboxes"
say "The delivery guarantee starts in these files, so they are the artefact"
say "that matters most -- see services/common/outbox.py."
for svc in ingestor api enricher; do
  name="outbox-$svc.sqlite3"
  if ! running "$svc"; then
    # Not fatal. A producer that is down still owns its volume, but nothing in
    # this script may write to a volume it cannot see a live owner for, and a
    # backup that quietly skipped a producer is exactly the kind of gap that is
    # only discovered during a restore.
    record_absent "$name" sqlite-outbox "$svc" "the $svc container is not running"
    continue
  fi
  # Ask the container where its own outbox is rather than assuming /outbox:
  # AOW_OUTBOX_PATH is configurable, and a backup that reads the wrong path
  # would produce a perfectly valid copy of nothing.
  outbox_path="$(dc exec -T "$svc" python -c \
    'from services.common import config; print(config.OUTBOX_PATH)' | tr -d '\r')"
  tmp_copy="/tmp/aow-backup-outbox.sqlite3"
  # The module travels on the container's stdin so that no image has to carry
  # it and no bind mount is needed; the paths travel as arguments.
  if ! report="$(dc exec -T "$svc" python - "$outbox_path" "$tmp_copy" \
      < scripts/sqlite_backup.py)"; then
    die "the online copy of the $svc outbox failed"
  fi
  say "$svc: $(printf '%s' "$report" | tr -d '\r')"
  dc exec -T "$svc" cat "$tmp_copy" > "$BACKUP_DIR/$name"
  dc exec -T "$svc" rm -f "$tmp_copy"
  record_artefact "$name" sqlite-outbox "$svc" "$outbox_path"
done

# ------------------------------------------------------------------ rabbitmq --

step "RabbitMQ topology"
say "Exchanges, queues, bindings, policies, users -- NOT message bodies."
dc exec -T rabbitmq rabbitmqctl -q export_definitions /tmp/aow-definitions.json \
  || die "rabbitmqctl export_definitions failed"
dc exec -T rabbitmq cat /tmp/aow-definitions.json > "$BACKUP_DIR/rabbitmq-definitions.json"
dc exec -T rabbitmq rm -f /tmp/aow-definitions.json
record_artefact rabbitmq-definitions.json rabbitmq-definitions rabbitmq "-"

# What is deliberately being left behind, counted rather than described. An
# operator reading the manifest afterwards can see exactly how many message
# bodies this backup did not capture, and restore-state.sh replays what the
# outboxes can account for.
QUEUES_JSON=""
queue_sep=""
while read -r qname qdepth; do
  [ -n "$qname" ] || continue
  # `rabbitmqctl --quiet` still prints a `name messages` header row on
  # RabbitMQ 4.x, so a row whose second column is not a number is that header
  # and not a queue. Letting it through put the literal word `messages` into
  # the manifest where an integer belongs, which the manifest check then
  # correctly refused as invalid JSON -- the round-trip check earning its keep.
  case "$qdepth" in
    '' | *[!0-9]*) continue ;;
  esac
  QUEUES_JSON="${QUEUES_JSON}${queue_sep}{\"queue\": \"$qname\", \"messages\": ${qdepth}}"
  queue_sep=", "
  say "not captured: $qdepth message(s) in $qname"
done <<EOF
$(dc exec -T rabbitmq rabbitmqctl list_queues name messages --quiet | tr -d '\r')
EOF

# ---------------------------------------------------------------- provenance --

step "Provenance"

# The migration files as they stand in this tree. A restore runs them before
# pg_restore, so if they have moved on since the backup was taken the restored
# schema is not the schema the dump was written against -- restore-state.sh
# compares these and says so.
MIGRATIONS_JSON=""
mig_sep=""
for sql in db/migrations/*.sql; do
  read -r sha _bytes <<EOF
$(digest_of "$sql")
EOF
  MIGRATIONS_JSON="${MIGRATIONS_JSON}${mig_sep}{\"file\": \"$(basename "$sql")\", \"sha256\": \"$sha\"}"
  mig_sep=", "
done
say "migrations: $(ls db/migrations/*.sql | wc -l | tr -d ' ') file(s)"

# The images actually running, by the reference Compose resolved and by the
# image ID the daemon is using. A restore onto different images is allowed --
# that is a normal upgrade -- but it should never be a surprise.
IMAGES_JSON=""
img_sep=""
for svc in $(dc ps --services 2>/dev/null); do
  cid="$(dc ps -q "$svc" 2>/dev/null || true)"
  [ -n "$cid" ] || continue
  ref="$(docker inspect -f '{{.Config.Image}}' "$cid")"
  iid="$(docker inspect -f '{{.Image}}' "$cid")"
  IMAGES_JSON="${IMAGES_JSON}${img_sep}{\"service\": \"$svc\", \"image\": \"$ref\", \"image_id\": \"$iid\"}"
  img_sep=", "
done
say "images: recorded for every running service"

# ------------------------------------------------------------------ manifest --

FINISHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

{
  printf '{\n'
  printf '  "manifest_version": 1,\n'
  printf '  "backup_id": "%s",\n' "$BACKUP_ID"
  printf '  "started_at": "%s",\n' "$STARTED_AT"
  printf '  "finished_at": "%s",\n' "$FINISHED_AT"
  printf '  "rpo_reference": "%s",\n' "$STARTED_AT"
  printf '  "rpo_note": "Everything this stack accepted after started_at is outside this backup.",\n'
  printf '  "compose_project": "%s",\n' "$PROJECT"
  printf '  "database": "%s",\n' "$PGDB_NAME"
  printf '  "artefacts": [\n'
  i=0
  for artefact in "${ARTEFACTS[@]}"; do
    [ "$i" -gt 0 ] && printf ',\n'
    printf '    %s' "$artefact"
    i=$((i + 1))
  done
  printf '\n  ],\n'
  printf '  "not_backed_up": {\n'
  printf '    "what": "RabbitMQ message bodies in flight",\n'
  printf '    "why": "there is no consistent export of queue contents; the bytes are already in the producer outboxes, and services/common/reconcile.py republishes what the restored database cannot account for",\n'
  printf '    "queues_at_backup_time": [%s]\n' "$QUEUES_JSON"
  printf '  },\n'
  printf '  "migrations": [%s],\n' "$MIGRATIONS_JSON"
  printf '  "images": [%s]\n' "$IMAGES_JSON"
  printf '}\n'
} > "$BACKUP_DIR/manifest.json"

step "Checking the manifest we just wrote"
# Round-trip: validate the manifest, and re-measure every artefact against it,
# from the files as they now sit on disk. A backup that cannot verify itself
# the moment it is taken will not verify six weeks later either.
OBSERVED=()
for file in "$BACKUP_DIR"/*; do
  base="$(basename "$file")"
  [ "$base" = "manifest.json" ] && continue
  read -r sha bytes <<EOF
$(digest_of "$file")
EOF
  OBSERVED+=("--observed" "$base=$sha:$bytes")
done

if ! docker run --rm -i -e AOW_MANIFEST="$(cat "$BACKUP_DIR/manifest.json")" \
      "$HELPER_IMAGE" python - verify "${OBSERVED[@]}" < scripts/backup_manifest.py; then
  die "the manifest does not describe the files that were written"
fi

step "Done"
say "artefacts:  $(ls "$BACKUP_DIR" | wc -l | tr -d ' ') file(s) in $BACKUP_DIR"
say "RPO point:  $STARTED_AT"
say "restore it: bash scripts/restore-state.sh $BACKUP_DIR"
printf 'BACKUP_DIR=%s\n' "$BACKUP_DIR"
