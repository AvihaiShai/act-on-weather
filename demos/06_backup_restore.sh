#!/usr/bin/env bash
#
# The backup/restore drill: total loss of live state, recovered from a backup.
#
#   bash demos/06_backup_restore.sh
#
# It needs no arguments, no running stack and no .env: it creates its own
# isolated Compose project with its own volumes and its own credentials, and
# tears the whole thing down again on the way out. A reviewer's `aow` stack is
# never touched, and neither are its ports -- this project never starts `edge`,
# so nothing is published and there is nothing to collide with.
#
# ------------------------------------------------------------ what it proves
#
# Three sets of records, each one traced by its own message_id, because a row
# count cannot tell a loss and a duplicate apart:
#
#   A  accepted and stored BEFORE the backup.
#      After the restore each one must be in ingest_log exactly once, with its
#      recommendations row beside it. This is the guarantee.
#
#   C  accepted with the consumer stopped, confirmed to the broker, and so
#      present in the API's outbox but NOT in the database when the dump was
#      taken. After the restore the dump alone cannot account for it: it comes
#      back only because scripts/restore-state.sh runs
#      services/common/reconcile.py, which republishes exactly the confirmed
#      envelopes the restored ingest_log cannot see. This is the part of the
#      story the pg_dump does not tell on its own.
#
#   B  accepted AFTER the backup started. These are lost, and that is the
#      documented boundary rather than a bug -- so they are asserted absent,
#      with numbers: how many were lost, and how far past the backup's start
#      timestamp the last one was accepted. Set A proves the guarantee; set B
#      proves the boundary is where the runbook says it is. A B id that turned
#      up PRESENT would mean the backup window is not what we claim, so it
#      fails the drill just as loudly as a missing A id.
#
# Between the backup and the restore the project is taken down with `-v`: the
# volumes are destroyed, which is the disruption. `pgdata`, `rabbitdata` and
# all three outbox volumes go with it.
#
# The verification deliberately does NOT go through the API that accepted the
# writes. It asks psql for `SELECT count(*) FROM ingest_log WHERE message_id =
# '<id>'` and requires exactly 1, per id, printed per id -- the same assertion
# scripts/ci-integration.sh gates on.
#
# Exit codes: 0 every assertion passed, 1 an assertion failed.
#
# shellcheck source=demos/lib.sh
source "$(dirname "$0")/lib.sh"

# lib.sh gives us hr/note/pass/fail and the repo root. It also defines dc() and
# psql_q() against the DEFAULT project, which this drill must never touch, so
# everything below talks to the stack through dcp()/psqlq() instead.
FAILED=0

# See the same block in scripts/backup-state.sh: MSYS would rewrite the
# container-side absolute paths this drill hands to `docker exec`.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

PROJECT="${AOW_DRILL_PROJECT:-aow-backup-drill}"
# Only what the drill needs. No llm (the enricher then simply has nothing to
# publish, which keeps the reconcile step below about set C and nothing else),
# no agent, no ui, and above all no edge -- edge publishes 8080 and 8000, and a
# proof that fights a reviewer's running stack for a port is not a proof.
SERVICES="postgres rabbitmq migrate consumer api ingestor enricher"
DRILL_START="$(date +%s)"

# ------------------------------------------------------------- credentials --
# AOW_ENV_FILE first (CI generates one), then a committed-workflow .env, then
# one made here. The generated file lives beside the backups rather than in
# /tmp: every host path in this drill is relative, because an absolute POSIX
# path handed to docker.exe on Git Bash is not the path docker.exe would read.
GENERATED_ENV=""
ENV_FILE="${AOW_ENV_FILE:-}"
if [ -z "$ENV_FILE" ] && [ -f .env ]; then
  ENV_FILE=".env"
fi
if [ -z "$ENV_FILE" ] || [ ! -f "$ENV_FILE" ]; then
  mkdir -p backups
  ENV_FILE="backups/.drill-$$.env"
  GENERATED_ENV="$ENV_FILE"
  : > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  # Generated in a container, because this repository has no host-side Python.
  for key in POSTGRES_PASSWORD POSTGRES_WRITER_PASSWORD POSTGRES_READER_PASSWORD RABBITMQ_PASSWORD; do
    printf '%s=%s\n' "$key" \
      "$(docker run --rm "${AOW_SERVICES_IMAGE:-aow/services:dev}" \
           python -c 'import secrets; print(secrets.token_hex(24))' | tr -d '\r')" \
      >> "$ENV_FILE"
  done
fi

ENV_FILE_ARG="$ENV_FILE"
if command -v cygpath >/dev/null 2>&1; then
  ENV_FILE_ARG="$(cygpath -w "$ENV_FILE" 2>/dev/null || printf '%s' "$ENV_FILE")"
fi

# An optional extra Compose file, layered on compose.yml. CI builds the service
# image under its own tag and selects it with compose.ci.yml; without this the
# drill could only ever run against the developer's `aow/services:dev`, which is
# not the image a CI gate is supposed to be proving anything about. The two
# scripts under test read the same variable from the environment, so setting it
# once covers all three.
COMPOSE_FILES=(-f compose.yml)
if [ -n "${AOW_COMPOSE_OVERLAY:-}" ]; then
  [ -f "$AOW_COMPOSE_OVERLAY" ] || { fail "no such Compose overlay: $AOW_COMPOSE_OVERLAY"; exit 1; }
  COMPOSE_FILES+=(-f "$AOW_COMPOSE_OVERLAY")
fi

dcp() { docker compose --env-file "$ENV_FILE_ARG" -p "$PROJECT" "${COMPOSE_FILES[@]}" "$@"; }

# The separate reader. POSTGRES_USER/POSTGRES_DB come from the shell, not from
# the env file, so they are the compose defaults -- which is exactly what the
# generated env file above leaves them as.
psqlq() { dcp exec -T postgres psql -U "${POSTGRES_USER:-aow}" -d "${POSTGRES_DB:-aow}" -tAc "$1" | tr -d '\r'; }

cleanup() {
  local status=$?
  hr "Tearing the drill project down"
  if [ "$status" -ne 0 ]; then
    dcp logs --no-color --tail 40 consumer api 2>&1 | sed 's/^/   /' || true
  fi
  dcp down -v --remove-orphans >/dev/null 2>&1 || true
  note "project '$PROJECT' and all of its volumes removed"
  [ -n "$GENERATED_ENV" ] && rm -f "$GENERATED_ENV"
  return 0
}
trap cleanup EXIT

# --------------------------------------------------------------- helpers --

# Accept one record through the API, reusing the CI fixture rather than a second
# copy of the same request. It prints the message_id and asserts the record
# reached the fsynced outbox, which is where the guarantee starts.
accept_one() { # label -> message_id
  dcp exec -T -e AOW_TEST_DATE="$DAY" -e AOW_TEST_LABEL="$1" \
    api python - < tests/integration/accept_request.py | tr -d '\r'
}

# The activity slug the consumer will key the recommendations row on. Every
# label this drill uses is lowercase words separated by single spaces, which is
# exactly what schemas.slugify turns into underscores -- so this stays a shell
# substitution rather than another container round trip.
slug_of() { printf '%s' "$1" | tr ' ' '_'; }

outbox_field() { # message_id field -> value, or the string 'error'
  dcp exec -T api python -c "
import json, urllib.request
try:
    with urllib.request.urlopen('http://127.0.0.1:8000/outbox/$1', timeout=15) as r:
        print(json.load(r).get('$2'))
except Exception as exc:
    print('error:%s' % exc)
" | tr -d '\r'
}

wait_for() { # description, seconds, command...
  local what="$1" limit="$2"; shift 2
  local waited=0
  while [ "$waited" -lt "$limit" ]; do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 3; waited=$((waited + 3))
  done
  note "timed out after ${limit}s waiting for $what"
  return 1
}

stored_is_true() { [ "$(outbox_field "$1" stored)" = "True" ]; }
published_is_set() { [ "$(outbox_field "$1" published_at)" != "None" ]; }
weather_present() { [ "$(psqlq 'SELECT count(*) FROM weather_daily')" -gt 0 ]; }

# "The snapshot has finished loading" is two conditions, not one, and the queue
# on its own is the misleading half: the ingestor drains its outbox 200 rows at
# a time every two seconds, so aow.ingest is repeatedly empty long before the
# ingestor has published everything it accepted at startup. Waiting only on the
# queue left several hundred records in flight when the backup was taken --
# which is legitimate for the backup, but it would make the restore's reconcile
# step replay hundreds of records that have nothing to do with what this drill
# is proving. So: nothing left unpublished in the ingestor's outbox, AND
# nothing left in the queue.
ingestor_outbox_drained() {
  [ "$(dcp exec -T ingestor python -c '
from services.common import config
from services.common.outbox import Outbox
print(Outbox(config.OUTBOX_PATH, readonly=True).counts()["pending"])' | tr -d '\r')" = "0" ]
}
queue_empty() {
  [ "$(dcp exec -T rabbitmq rabbitmqctl list_queues name messages --quiet | tr -d '\r' \
      | awk '$1 == "aow.ingest" { print $2 }')" = "0" ]
}
pipeline_settled() { ingestor_outbox_drained && queue_empty; }

# ------------------------------------------------- 1. an isolated stack --

hr "An isolated stack with fresh volumes"
note "project:   $PROJECT"
note "services:  $SERVICES"
note "env file:  $ENV_FILE${GENERATED_ENV:+  (generated for this run)}"
note "compose:   ${COMPOSE_FILES[*]}"
note "image:     ${AOW_SERVICES_IMAGE:-aow/services:dev}  (the helper containers)"
dcp down -v --remove-orphans >/dev/null 2>&1 || true
# shellcheck disable=SC2086 -- a deliberately word-split service list
dcp up -d --no-build $SERVICES >/dev/null 2>&1 || { fail "the drill stack did not start"; exit 1; }
note "up; waiting for the snapshot to finish loading"

wait_for "the ingestor's snapshot to reach the database" 300 weather_present \
  || { fail "no weather ever reached the drill database"; exit 1; }
wait_for "the ingestor's snapshot to be fully published and consumed" 300 pipeline_settled \
  || note "the pipeline has not settled; the reconcile step will have more to replay"
DAY="$(psqlq 'SELECT min(forecast_date) FROM weather_daily')"
note "stored rows:     $(psqlq 'SELECT count(*) FROM ingest_log')"
note "forecast day in use for the drill records: $DAY"

# --------------------------------------- 2. set A: accepted and stored --

hr "Set A -- accepted and stored BEFORE the backup"
A_LABELS=("drill alpha one" "drill alpha two" "drill alpha three")
A_IDS=()
for label in "${A_LABELS[@]}"; do
  mid="$(accept_one "$label")"
  A_IDS+=("$mid")
  note "accepted  $mid  ($label -> $(slug_of "$label"))"
done
for mid in "${A_IDS[@]}"; do
  if wait_for "$mid to be stored" 120 stored_is_true "$mid"; then
    pass "stored before the backup: $mid"
  else
    fail "set A record never stored before the backup: $mid"
    exit 1
  fi
done

# ------------------------- 3. set C: confirmed to the broker, not stored --

hr "Set C -- confirmed to the broker, NOT yet in the database"
note "The consumer is stopped, so this record is published and confirmed but"
note "never written. The dump about to be taken cannot contain it; the API's"
note "outbox can, and does. This is what reconcile.py is for."
dcp stop consumer >/dev/null 2>&1
C_LABEL="drill confirmed unstored"
C_ID="$(accept_one "$C_LABEL")"
note "accepted  $C_ID  ($C_LABEL -> $(slug_of "$C_LABEL"))"
if wait_for "$C_ID to be confirmed by the broker" 60 published_is_set "$C_ID"; then
  pass "confirmed to the broker while the consumer was down: $C_ID"
else
  fail "the set C record was never confirmed to the broker: $C_ID"
  exit 1
fi
if [ "$(psqlq "SELECT count(*) FROM ingest_log WHERE message_id = '$C_ID'")" = "0" ]; then
  pass "and it is NOT in ingest_log, which is the state the backup will capture"
else
  fail "the set C record was stored before the backup; the drill cannot prove its point"
  exit 1
fi

# ------------------------------------------------------------ 4. backup --

hr "Taking the backup"
BACKUP_OUT="$(AOW_ENV_FILE="$ENV_FILE" bash scripts/backup-state.sh --project "$PROJECT")" || {
  printf '%s\n' "$BACKUP_OUT" | sed 's/^/   /'
  fail "scripts/backup-state.sh failed"
  exit 1
}
printf '%s\n' "$BACKUP_OUT" | sed 's/^/   /'
BACKUP_DIR="$(printf '%s' "$BACKUP_OUT" | sed -n 's/^BACKUP_DIR=//p' | tail -1)"
[ -n "$BACKUP_DIR" ] || { fail "backup-state.sh did not report a backup directory"; exit 1; }
# The manifest's start timestamp IS the RPO reference point, so it is read back
# out of the artefact rather than remembered from a shell variable.
BACKUP_STARTED_AT="$(sed -n 's/.*"started_at": "\([^"]*\)".*/\1/p' "$BACKUP_DIR/manifest.json" | head -1)"
pass "backup written to $BACKUP_DIR"
note "manifest started_at (the RPO reference point): $BACKUP_STARTED_AT"

dcp start consumer >/dev/null 2>&1
note "consumer restarted -- the live stack now converges, which is what makes"
note "the loss below a property of the BACKUP and not of the running system"
wait_for "$C_ID to be stored on the live stack" 120 stored_is_true "$C_ID" \
  || note "the set C record had not stored live before set B was accepted"

# ------------------------------------------- 5. set B: after the backup --

hr "Set B -- accepted AFTER the backup started"
note "These define the RPO window. They are outside the backup by construction."
B_LABELS=("drill beta one" "drill beta two")
B_IDS=()
B_LAST_ACCEPTED_AT=""
for label in "${B_LABELS[@]}"; do
  mid="$(accept_one "$label")"
  B_LAST_ACCEPTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  B_IDS+=("$mid")
  note "accepted  $mid  ($label -> $(slug_of "$label"))  at $B_LAST_ACCEPTED_AT"
done

# Set B has to be real before its later absence means anything. If the API ever
# stopped accepting writes, every B id would be trivially absent after the
# restore and the boundary assertions would pass while proving nothing. So each
# one is required to have been genuinely accepted, confirmed AND committed on
# the live stack first.
[ "${#B_IDS[@]}" -ge 2 ] || { fail "set B is empty; the RPO boundary cannot be measured"; exit 1; }
for mid in "${B_IDS[@]}"; do
  if wait_for "$mid to be stored on the live stack" 120 stored_is_true "$mid"; then
    pass "accepted and stored on the live stack after the backup: $mid"
  else
    fail "a set B record never stored on the live stack: $mid"
    exit 1
  fi
  if [ "$(psqlq "SELECT count(*) FROM ingest_log WHERE message_id = '$mid'")" = "1" ]; then
    pass "and a separate reader sees it exactly once BEFORE the disruption: $mid"
  else
    fail "set B record $mid was not committed before the disruption"
    exit 1
  fi
done

# ------------------------------------------------------- 6. the disruption --

hr "The disruption -- total loss of live state"
DISRUPTION_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
note "at $DISRUPTION_AT: docker compose down -v"
note "pgdata, rabbitdata and all three outbox volumes are destroyed"
dcp down -v --remove-orphans >/dev/null 2>&1
if [ -z "$(docker volume ls -q --filter "name=^${PROJECT}_pgdata$")" ]; then
  pass "the database volume is gone"
else
  fail "the database volume survived; this is not a disruption"
fi

# ---------------------------------------------------------- 7. the restore --

hr "The restore"
RESTORE_START="$(date +%s)"
if AOW_ENV_FILE="$ENV_FILE" bash scripts/restore-state.sh "$BACKUP_DIR" \
     --project "$PROJECT" --live-project "${AOW_PROJECT:-aow}" \
     --services "$SERVICES" --disruption-at "$DISRUPTION_AT" 2>&1 | sed 's/^/   /'; then
  pass "scripts/restore-state.sh completed and verified itself"
else
  fail "scripts/restore-state.sh failed"
fi
RESTORE_END="$(date +%s)"

# ------------------------------------------------------- 8. verification --

hr "Verification, from a separate reader"
note "psql against the restored database -- not the API that accepted the"
note "writes, and not a row count that a loss and a duplicate could cancel out."
VERIFY_START="$(date +%s)"

# Set C comes back through the replay, and a replay is a publish: the consumer
# still has to pick it up. So the one id that travels the slow path is given
# time before the per-id assertions run.
c_committed() { [ "$(psqlq "SELECT count(*) FROM ingest_log WHERE message_id = '$C_ID'")" = "1" ]; }
wait_for "the replayed set C record to commit" 120 c_committed \
  || note "the replayed record had not committed within 120s"

assert_stored_once() { # message_id label
  local mid="$1" label="$2" count rows
  count="$(psqlq "SELECT count(*) FROM ingest_log WHERE message_id = '$mid'")"
  rows="$(psqlq "SELECT count(*) FROM recommendations WHERE city_id = 'rome' AND forecast_date = '$DAY' AND activity = '$(slug_of "$label")'")"
  if [ "$count" = "1" ]; then
    pass "ingest_log count = 1 for $mid"
  else
    fail "ingest_log count = ${count:-<none>} for $mid (must be exactly 1)"
  fi
  if [ "$rows" = "1" ]; then
    pass "its recommendations row is present: rome/$DAY/$(slug_of "$label")"
  else
    fail "the recommendations row for $mid is missing (rome/$DAY/$(slug_of "$label"))"
  fi
}

note ""
note "Set A -- must be present exactly once:"
for index in "${!A_IDS[@]}"; do
  assert_stored_once "${A_IDS[$index]}" "${A_LABELS[$index]}"
done

note ""
note "Set C -- confirmed to the broker before the backup, absent from the dump,"
note "recovered by reconcile.py during the restore:"
assert_stored_once "$C_ID" "$C_LABEL"

note ""
note "Set B -- accepted after the backup started. ABSENT is the expected and"
note "correct result: it is the documented RPO boundary, not a bug. A set B id"
note "that came back PRESENT would mean the backup window is not where the"
note "runbook says it is, so that -- and only that -- fails the drill here."
B_LOST=0
for mid in "${B_IDS[@]}"; do
  count="$(psqlq "SELECT count(*) FROM ingest_log WHERE message_id = '$mid'")"
  if [ "$count" = "0" ]; then
    pass "absent as expected, outside the backup window: $mid"
    B_LOST=$((B_LOST + 1))
  else
    fail "set B record $mid is PRESENT (count = $count) -- the backup boundary is not where we claim"
  fi
done
VERIFY_END="$(date +%s)"

# ---------------------------------------------------- 9. the measurements --

hr "Measured recovery objectives"
RTO_S=$(( (RESTORE_END - RESTORE_START) + (VERIFY_END - VERIFY_START) ))
# The RPO window as numbers rather than a sentence: how far past the backup's
# start timestamp the last lost write was accepted, and how long the stack went
# on accepting work before its volumes died. Both change if the behaviour
# changes, which is the point of reporting them instead of describing them.
# Computed in a container because `date -d` is GNU-only and macOS does not have
# it; DISRUPTION_EPOCH is kept as a plain epoch for the same reason.
read -r RPO_SPAN_S RPO_WINDOW_S <<EOF
$(docker run --rm -e A="$BACKUP_STARTED_AT" -e B="$B_LAST_ACCEPTED_AT" -e D="$DISRUPTION_AT" \
  "${AOW_SERVICES_IMAGE:-aow/services:dev}" python -c '
import os
from datetime import datetime
fmt = "%Y-%m-%dT%H:%M:%SZ"
start = datetime.strptime(os.environ["A"], fmt)
last_b = datetime.strptime(os.environ["B"], fmt)
disruption = datetime.strptime(os.environ["D"], fmt)
print(int((last_b - start).total_seconds()), int((disruption - start).total_seconds()))
' | tr -d '\r')
EOF

note "RPO reference point (manifest started_at):  $BACKUP_STARTED_AT"
note "last set B id accepted at:                  $B_LAST_ACCEPTED_AT"
note "disruption at:                              $DISRUPTION_AT"
note "records lost (set B):                       $B_LOST of ${#B_IDS[@]}"
note "RPO span (backup start -> last lost write): ${RPO_SPAN_S}s"
note "at-risk window (backup start -> disruption): ${RPO_WINDOW_S}s"
note "RTO (restore + verification):               ${RTO_S}s"
note "whole drill, wall clock:                    $(( $(date +%s) - DRILL_START ))s"

hr "Every message id this drill used"
for index in "${!A_IDS[@]}"; do
  printf '   A  %s  %s\n' "${A_IDS[$index]}" "${A_LABELS[$index]}"
done
printf '   C  %s  %s\n' "$C_ID" "$C_LABEL"
for index in "${!B_IDS[@]}"; do
  printf '   B  %s  %s\n' "${B_IDS[$index]}" "${B_LABELS[$index]}"
done
note ""
note "backup kept at: $BACKUP_DIR"

if [ "$FAILED" -eq 0 ]; then
  printf '\n\033[32mBackup and restore drill passed.\033[0m\n'
else
  printf '\n\033[31mThe backup/restore drill failed -- see above.\033[0m\n'
fi
exit "$FAILED"
