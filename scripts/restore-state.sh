#!/usr/bin/env bash
#
# Restore the live state of an act-on-weather stack from a backup taken by
# scripts/backup-state.sh.
#
#   bash scripts/restore-state.sh backups/<id> [options]
#
# This is the *operational* restore: it rebuilds the data of a stack whose
# volumes were lost or corrupted. It is not the offline release path's
# installer rollback (scripts/restore-offline.sh), which puts a previous
# *version* of the deployment back. One protects the data, the other protects
# the deployment; they are separate commands on purpose, because on the day it
# matters an operator must not have to decide which half of a combined script
# they meant.
#
# ---------------------------------------------------------------- what it does
#
#   1. Verifies every artefact in the directory against the manifest's SHA-256
#      and byte count, and refuses to continue on a mismatch. A restore that
#      silently proceeds from a corrupt dump is worse than no restore: it
#      produces a database an operator will believe.
#   2. Refuses to touch the live Compose project unless told to, loudly. By
#      default it restores into a SEPARATE project (aow-restore) with fresh
#      volumes, so that a restore can be rehearsed, or a backup inspected,
#      without putting a running stack at risk.
#   3. Brings up postgres and rabbitmq on fresh volumes, runs the migrations
#      (which is where the aow_writer and aow_reader roles and their passwords
#      come from -- a database dump carries grants but not roles), then
#      pg_restore's the dump and imports the RabbitMQ definitions.
#   4. Puts the three producer outboxes back on their volumes, owned by uid
#      10001, which is the uid services/Dockerfile runs as.
#   5. Starts the stack and runs services/common/reconcile.py in each producer.
#      That is the step that recovers what the dump alone cannot: an envelope
#      the producer had confirmed to the broker but whose database write had
#      not happened when the dump was taken. reconcile republishes exactly the
#      IDs the restored `ingest_log` cannot account for, and the consumer's
#      idempotency key makes a repeat harmless.
#   6. Verifies, and prints the measured RTO and the RPO the manifest defines.
#
# Message bodies are NOT restored, because they were never backed up -- see the
# header of scripts/backup-state.sh. Step 5 is how the ones that still matter
# come back.
#
# Docker only: no host psql, no host sqlite3, no host Python.
#
# Exit codes:
#   0  restored and verified
#   1  bad arguments, or a missing prerequisite
#   2  the backup does not verify against its manifest -- nothing was touched
#   3  a restore step failed
#   4  restored, but the verification did not pass
set -euo pipefail

cd "$(dirname "$0")/.."

# Git Bash (MSYS) rewrites arguments that look like absolute POSIX paths into
# Windows paths before handing them to docker.exe. Almost every absolute path
# in this script is a path INSIDE a container -- /outbox/outbox.sqlite3,
# /tmp/aow-definitions.json -- and that rewrite would send `docker exec` looking
# at C:\Users\...\Temp instead. So the translation is switched off here, and the
# one place that still needs it (a host path handed to docker as --env-file) is
# translated explicitly with cygpath below. Both variables are inert on Linux
# and macOS; every host file this script reads is opened by bash itself.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

# Wall clock starts here: the RTO this prints at the end is measured from
# invocation to verification passing, not from some convenient later point.
START_EPOCH="$(date +%s)"

PROJECT="aow-restore"
LIVE_PROJECT="${AOW_PROJECT:-aow}"
ENV_FILE="${AOW_ENV_FILE:-.env}"
RESTORE_SERVICES="${AOW_RESTORE_SERVICES:-}"
DISRUPTION_AT=""
OVERWRITE_LIVE=0
BACKUP_DIR=""
# An optional extra Compose file, layered on compose.yml. CI builds the service
# image under its own tag and selects it with compose.ci.yml, so a command that
# can only address the developer's `aow/services:dev` is a command CI cannot
# run. Everything else about the stack is unchanged.
OVERLAY="${AOW_COMPOSE_OVERLAY:-}"

usage() {
  cat <<'EOF'
Restore a stack's live state from a backup directory.

  bash scripts/restore-state.sh BACKUP_DIR [options]

  --project NAME             restore into this Compose project
                             (default: aow-restore -- deliberately NOT the
                             live stack)
  --live-project NAME        the project to protect (default: aow)
  --env-file FILE            env file for the target project (default: .env)
  --overlay FILE             an extra Compose file layered on compose.yml
                             (e.g. compose.ci.yml). Also AOW_COMPOSE_OVERLAY.
  --services "a b c"         bring up only these services afterwards
                             (default: every service in compose.yml; note that
                             `edge` publishes 8080/8000, so an isolated restore
                             alongside a running stack should exclude it)
  --disruption-at ISO        when the loss happened, so the RPO can be reported
                             as a duration rather than only a timestamp
  --overwrite-live-project   allow the target project to be the live one.
                             THIS DESTROYS THE LIVE STACK'S VOLUMES.
  -h, --help                 this text
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --live-project) LIVE_PROJECT="$2"; shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --overlay) OVERLAY="$2"; shift 2 ;;
    --services) RESTORE_SERVICES="$2"; shift 2 ;;
    --disruption-at) DISRUPTION_AT="$2"; shift 2 ;;
    --overwrite-live-project) OVERWRITE_LIVE=1; shift ;;
    -h | --help) usage; exit 0 ;;
    -*) echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
    *) BACKUP_DIR="$1"; shift ;;
  esac
done

say()  { printf '   %s\n' "$*"; }
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die()  { printf '\033[31mrestore failed: %s\033[0m\n' "$*" >&2; exit "${2:-3}"; }

[ -n "$BACKUP_DIR" ] || { usage >&2; exit 1; }
[ -d "$BACKUP_DIR" ] || die "no such backup directory: $BACKUP_DIR" 1
[ -f "$BACKUP_DIR/manifest.json" ] || die "$BACKUP_DIR has no manifest.json" 1

COMPOSE_FILES=(-f compose.yml)
if [ -n "$OVERLAY" ]; then
  [ -f "$OVERLAY" ] || die "no such Compose overlay: $OVERLAY" 1
  COMPOSE_FILES+=(-f "$OVERLAY")
fi

# The env file is a HOST path handed to docker.exe, so it is the one argument
# that still needs the MSYS translation switched off above. cygpath exists only
# on Git Bash; everywhere else this leaves the path exactly as given.
ENV_FILE_ARG="$ENV_FILE"
if command -v cygpath >/dev/null 2>&1; then
  ENV_FILE_ARG="$(cygpath -w "$ENV_FILE" 2>/dev/null || printf '%s' "$ENV_FILE")"
fi

# `--progress quiet` because this script narrates its own steps. Compose's
# per-container Creating/Starting/Waiting/Healthy chatter, on a stack this size,
# buries the two lines an operator is actually reading -- what was restored and
# whether it verified. Failures still print: the flag suppresses the progress
# UI, not errors. It is probed rather than assumed, because it arrived in a
# later Compose than the one some hosts have and a restore must not fail over
# the formatting of its own output.
PROGRESS=()
if docker compose --progress quiet version >/dev/null 2>&1; then
  PROGRESS=(--progress quiet)
fi

dc() {
  if [ -f "$ENV_FILE" ]; then
    docker compose "${PROGRESS[@]}" --env-file "$ENV_FILE_ARG" -p "$PROJECT" \
      "${COMPOSE_FILES[@]}" "$@"
  else
    docker compose "${PROGRESS[@]}" -p "$PROJECT" "${COMPOSE_FILES[@]}" "$@"
  fi
}

MANIFEST="$(cat "$BACKUP_DIR/manifest.json")"

HELPER_IMAGE="${AOW_SERVICES_IMAGE:-aow/services:dev}"
docker image inspect "$HELPER_IMAGE" >/dev/null 2>&1 \
  || die "the helper image $HELPER_IMAGE is not present; build it or set AOW_SERVICES_IMAGE" 1

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

# ------------------------------------------------- 1. verify before touching --

step "Verifying $BACKUP_DIR against its manifest"
say "Nothing below this point runs until every artefact matches."

OBSERVED=()
for file in "$BACKUP_DIR"/*; do
  base="$(basename "$file")"
  [ "$base" = "manifest.json" ] && continue
  read -r sha bytes <<EOF
$(digest_of "$file")
EOF
  say "$base  ${bytes} bytes  sha256:${sha}"
  OBSERVED+=("--observed" "$base=$sha:$bytes")
done

if ! docker run --rm -i -e AOW_MANIFEST="$MANIFEST" "$HELPER_IMAGE" \
      python - verify "${OBSERVED[@]}" < scripts/backup_manifest.py; then
  die "this backup does not match its manifest; nothing has been changed" 2
fi

# Read the manifest once, into shell variables. The values are emitted already
# shell-quoted by shlex.quote inside the container, which is what makes the
# eval safe -- and they come from a manifest this script has just checksum
# verified, not from anywhere a stranger can write.
eval "$(docker run --rm -e AOW_MANIFEST="$MANIFEST" "$HELPER_IMAGE" python -c '
import json, os, shlex
m = json.loads(os.environ["AOW_MANIFEST"])


def emit(key, value):
    print("%s=%s" % (key, shlex.quote(str(value))))


emit("M_BACKUP_ID", m["backup_id"])
emit("M_STARTED_AT", m["started_at"])
emit("M_FINISHED_AT", m["finished_at"])
emit("M_PROJECT", m["compose_project"])
emit("M_DATABASE", m.get("database", "aow"))
emit("M_MIGRATIONS", " ".join("%s:%s" % (x["file"], x["sha256"]) for x in m.get("migrations", [])))
for a in m["artefacts"]:
    key = a["name"].replace("-", "_").replace(".", "_")
    emit("A_%s_PRESENT" % key, "1" if a.get("present") else "0")
    emit("A_%s_PATH" % key, a.get("container_path", "-"))
')"

say "backup id:        $M_BACKUP_ID"
say "taken from:       project '$M_PROJECT', database '$M_DATABASE'"
say "RPO reference:    $M_STARTED_AT"

# ------------------------------------------------------- 2. the loud guard --

step "Target"
say "restoring into Compose project '$PROJECT'"
if [ "$PROJECT" != "$M_PROJECT" ]; then
  say "note: this backup was taken from project '$M_PROJECT'"
fi
# Only the live project is protected, and deliberately only that one: restoring
# a project from its own backup is the ordinary disaster-recovery case, and a
# guard that refused it would refuse the thing this script is for. What must
# never happen by accident is a restore over a stack that is still serving.
if [ "$PROJECT" = "$LIVE_PROJECT" ]; then
  if [ "$OVERWRITE_LIVE" -ne 1 ]; then
    printf '\033[31m'
    cat >&2 <<EOF
REFUSING: '$PROJECT' is the live project.

This restore destroys the target project's volumes. Restoring over a stack that
is still running, on the strength of a path typed on a command line, is how a
recovery turns into the outage. Restore into a separate project first, look at
it, and only then decide:

  bash scripts/restore-state.sh $BACKUP_DIR --project aow-restore

If you really do mean the live stack -- it is down, or you have decided to
discard what it now holds -- say so:

  bash scripts/restore-state.sh $BACKUP_DIR --project $PROJECT --overwrite-live-project
EOF
    printf '\033[0m'
    exit 2
  fi
  printf '\033[31m   --overwrite-live-project given: the volumes of the LIVE project\n'
  printf '   "%s" are about to be destroyed and replaced.\033[0m\n' "$PROJECT"
fi

# Schema drift. A restore runs the migrations in THIS tree, not the ones the
# dump was written against. Usually they are the same file; when they are not,
# the operator has to know before pg_restore starts putting rows into a schema
# that has moved on.
now_migrations=""
for sql in db/migrations/*.sql; do
  read -r sha _bytes <<EOF
$(digest_of "$sql")
EOF
  now_migrations="${now_migrations}$(basename "$sql"):${sha} "
done
if [ "$(echo "$now_migrations" | tr -s ' ' | sed 's/ $//')" != "$M_MIGRATIONS" ]; then
  printf '\033[33m   WARNING: the migrations in this tree differ from the ones recorded in\n'
  printf '   the backup. The restore will run the current ones. Check the diff before\n'
  printf '   you trust the result.\033[0m\n'
  say "in the backup: $M_MIGRATIONS"
  say "in this tree:  $now_migrations"
else
  say "migrations match the ones recorded in the backup"
fi

# --------------------------------------------- 3. fresh volumes and schema --

step "Fresh volumes for project '$PROJECT'"
dc down -v --remove-orphans >/dev/null 2>&1 || true
say "any previous volumes of '$PROJECT' removed"

step "Starting postgres and rabbitmq"
dc up -d --no-build --wait postgres rabbitmq || die "postgres/rabbitmq did not become healthy"
say "both healthy"

step "Running the migrations"
# The migrations are what create aow_writer and aow_reader and set their
# passwords from the env file. A database dump carries the GRANTs to those
# roles but not the roles themselves, so pg_restore would fail without this.
dc up -d --no-build migrate >/dev/null
migrate_cid="$(dc ps -aq migrate)"
migrate_rc="$(docker wait "$migrate_cid")"
[ "$migrate_rc" = "0" ] || {
  dc logs --no-color migrate | tail -20
  die "the migrate container exited $migrate_rc"
}
say "schema and roles created"

# ------------------------------------------------------------ 4. pg_restore --

step "pg_restore"
# --clean --if-exists: the dump is the source of truth for everything the
# migrations just created, so drop and recreate rather than restore data into a
# schema that might differ in a column.
# --no-owner: objects end up owned by the role running the restore, which is
# the same owner role the migrations use. The GRANTs in the dump still apply,
# so aow_writer keeps write and aow_reader keeps read.
# --exit-on-error: a partial restore that reports success is the failure mode
# this whole script exists to avoid.
if ! dc exec -T postgres pg_restore -U "${POSTGRES_USER:-aow}" -d "$M_DATABASE" \
      --clean --if-exists --no-owner --exit-on-error \
      < "$BACKUP_DIR/postgres.dump"; then
  die "pg_restore failed"
fi
say "database restored from postgres.dump"

# ------------------------------------------------- 5. rabbitmq definitions --

step "RabbitMQ definitions"
dc exec -T rabbitmq sh -c 'cat > /tmp/aow-definitions.json' \
  < "$BACKUP_DIR/rabbitmq-definitions.json"
dc exec -T rabbitmq rabbitmqctl -q import_definitions /tmp/aow-definitions.json \
  || die "rabbitmqctl import_definitions failed"
dc exec -T rabbitmq rm -f /tmp/aow-definitions.json
say "exchanges, queues, bindings, policies and users restored"
say "message bodies were never backed up -- reconcile below is how the ones"
say "that still matter come back"

# ---------------------------------------------------- 6. producer outboxes --

step "Producer outboxes"
# The containers are created but not started, so Compose makes the named
# volumes with its own labels (a volume this script created by hand would not
# carry them) and no producer is yet writing to the file being replaced.
dc create --no-build ingestor api enricher consumer >/dev/null 2>&1 \
  || die "could not create the producer containers"

for svc in ingestor api enricher; do
  key="outbox_${svc}_sqlite3"
  present_var="A_${key}_PRESENT"
  path_var="A_${key}_PATH"
  present="${!present_var:-0}"
  container_path="${!path_var:-/outbox/outbox.sqlite3}"
  if [ "$present" != "1" ]; then
    say "$svc: no outbox in this backup -- it starts empty"
    continue
  fi
  volume="${PROJECT}_${svc}_outbox"
  docker volume inspect "$volume" >/dev/null 2>&1 \
    || die "the volume $volume does not exist after 'compose create'"
  # As root, because Docker seeds a fresh volume with the image's ownership of
  # /outbox (uid 10001, see services/Dockerfile) and a file written by anyone
  # else lands root-owned -- the producer would then fail to open its own
  # outbox. The chown afterwards is the whole reason for --user 0.
  # The stale -wal/-shm removal matters because this file is a checkpointed
  # copy: a leftover write-ahead log beside it would belong to another
  # database entirely.
  docker run --rm -i --user 0 -v "$volume:/outbox" "$HELPER_IMAGE" \
    sh -c "rm -f '$container_path' '$container_path-wal' '$container_path-shm' \
           && cat > '$container_path' \
           && chown 10001:10001 '$container_path' \
           && chmod 600 '$container_path'" \
    < "$BACKUP_DIR/outbox-$svc.sqlite3" \
    || die "could not place the $svc outbox on $volume"
  say "$svc: outbox restored to $volume:$container_path"
done

# ------------------------------------------------------------ 7. start up --

step "Starting the stack"
if [ -n "$RESTORE_SERVICES" ]; then
  say "services: $RESTORE_SERVICES"
  # shellcheck disable=SC2086 -- a deliberately word-split service list
  dc up -d --no-build $RESTORE_SERVICES || die "the stack did not start"
else
  dc up -d --no-build || die "the stack did not start"
fi
say "up"

# ------------------------------------------------------------ 8. reconcile --

step "Reconciling the producer outboxes against the restored database"
say "This is the step the dump alone cannot do: an envelope that the producer"
say "had confirmed to the broker, but whose database write had not happened"
say "when the dump was taken, is in the outbox and not in ingest_log. It is"
say "republished here, and the consumer's idempotency key makes a repeat safe."

REPLAYED_TOTAL=0
RECONCILE_FAILED=0
for svc in ingestor api enricher; do
  key="outbox_${svc}_sqlite3"
  present_var="A_${key}_PRESENT"
  [ "${!present_var:-0}" = "1" ] || continue
  audit="$(dc exec -T "$svc" python -m services.common.reconcile 2>&1 | tr -d '\r' | tail -1)" || {
    say "$svc: the audit failed: $audit"
    RECONCILE_FAILED=1
    continue
  }
  say "$svc audit: $audit"
  # reconcile.py deliberately requires --id for a replay: an operator audits
  # first and then names what they are republishing. So the ids come out of the
  # audit and go back in one at a time.
  missing="$(printf '%s' "$audit" | docker run --rm -i "$HELPER_IMAGE" python -c '
import json, sys
try:
    report = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
for message_id in report.get("missing_ids", []):
    print(message_id)
')"
  [ -n "$missing" ] || { say "$svc: nothing to replay"; continue; }
  count="$(printf '%s\n' "$missing" | grep -c .)"
  say "$svc: $count envelope(s) to replay"
  # One `docker compose exec` for the whole list, not one per id. A backlog of
  # a few hundred is entirely normal after a real outage -- every record the
  # producers had confirmed but the consumer had not yet written -- and at the
  # better part of a second per exec that alone would dominate the RTO.
  if dc exec -T -e AOW_REPLAY_IDS="$(printf '%s' "$missing" | tr '\n' ',')" "$svc" sh -c '
      for mid in $(printf "%s" "$AOW_REPLAY_IDS" | tr "," " "); do
        python -m services.common.reconcile --replay --id "$mid" > /dev/null || {
          echo "could NOT replay $mid"
          exit 1
        }
        echo "replayed $mid"
      done' 2>&1 | tail -20 | sed 's/^/     /'; then
    REPLAYED_TOTAL=$((REPLAYED_TOTAL + count))
  else
    say "$svc: a replay failed -- see the ids above"
    RECONCILE_FAILED=1
  fi
done
say "replayed $REPLAYED_TOTAL envelope(s) the restored database could not account for"

# ------------------------------------------------------------ 9. verify --

step "Verification"
FAILED=0
check() { # description, expected, actual
  if [ "$2" = "$3" ]; then
    printf '\033[32m   PASS\033[0m %s\n' "$1"
  else
    printf '\033[31m   FAIL\033[0m %s (expected %s, got %s)\n' "$1" "$2" "$3"
    FAILED=1
  fi
}

psqlq() {
  dc exec -T postgres psql -U "${POSTGRES_USER:-aow}" -d "$M_DATABASE" -tAc "$1" | tr -d '\r'
}

rows="$(psqlq 'SELECT count(*) FROM ingest_log')"
say "ingest_log holds $rows row(s)"
if [ "${rows:-0}" -gt 0 ]; then
  printf '\033[32m   PASS\033[0m the restored database has an idempotency ledger\n'
else
  printf '\033[31m   FAIL\033[0m ingest_log is empty after the restore\n'
  FAILED=1
fi
dupes="$(psqlq 'SELECT count(*) - count(DISTINCT message_id) FROM ingest_log')"
check "no duplicate message_ids in the restored ledger" "0" "$dupes"

for queue in aow.ingest aow.dlq; do
  if dc exec -T rabbitmq rabbitmqctl -q list_queues name | tr -d '\r' | grep -qx "$queue"; then
    printf '\033[32m   PASS\033[0m %s exists after the definitions import\n' "$queue"
  else
    printf '\033[31m   FAIL\033[0m %s is missing after the definitions import\n' "$queue"
    FAILED=1
  fi
done

for svc in ingestor api enricher; do
  key="outbox_${svc}_sqlite3"
  present_var="A_${key}_PRESENT"
  [ "${!present_var:-0}" = "1" ] || continue
  counts="$(dc exec -T "$svc" python -c \
    'from services.common import config
from services.common.outbox import Outbox
print(Outbox(config.OUTBOX_PATH, readonly=True).counts())' | tr -d '\r')" || counts=""
  if [ -n "$counts" ]; then
    printf '\033[32m   PASS\033[0m the %s outbox opens after restore: %s\n' "$svc" "$counts"
  else
    printf '\033[31m   FAIL\033[0m the %s outbox does not open after restore\n' "$svc"
    FAILED=1
  fi
done

check "every confirmed envelope is accounted for or was replayed" "0" "$RECONCILE_FAILED"

# ------------------------------------------------------- 10. RPO and RTO --

END_EPOCH="$(date +%s)"
RTO_S=$((END_EPOCH - START_EPOCH))

step "Recovery objectives, as measured by this run"
say "RPO reference point: $M_STARTED_AT (the backup's started_at)"
if [ -n "$DISRUPTION_AT" ]; then
  # Computed in a container rather than with `date -d`, which GNU date has and
  # BSD/macOS date does not.
  RPO_S="$(docker run --rm -e A="$M_STARTED_AT" -e B="$DISRUPTION_AT" "$HELPER_IMAGE" python -c '
import os
from datetime import datetime
fmt = "%Y-%m-%dT%H:%M:%SZ"
a = datetime.strptime(os.environ["A"], fmt)
b = datetime.strptime(os.environ["B"], fmt)
print(int((b - a).total_seconds()))
')"
  say "disruption at:       $DISRUPTION_AT"
  say "measured RPO:        ${RPO_S}s of accepted work was lost (backup start -> disruption)"
else
  say "measured RPO:        not computed -- pass --disruption-at to report it as a"
  say "                     duration. Everything this stack accepted after"
  say "                     $M_STARTED_AT is not in this backup."
fi
say "measured RTO:        ${RTO_S}s from invocation to the verification above"

if [ "$FAILED" -ne 0 ]; then
  printf '\n\033[31mThe restore completed but the verification did not pass.\033[0m\n'
  exit 4
fi
printf '\n\033[32mRestored and verified into project "%s".\033[0m\n' "$PROJECT"
printf 'RTO_SECONDS=%s\n' "$RTO_S"
printf 'RPO_REFERENCE=%s\n' "$M_STARTED_AT"
