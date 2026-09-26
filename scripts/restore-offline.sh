#!/usr/bin/env bash
# Restore a pre-upgrade dump taken by scripts/install-offline.sh.
#
# Rolling an image back is not rolling a schema back: `docker load` of the
# previous release replaces code, and leaves whatever the newer migration did
# to the database in place. This is the other half of that rollback. Run it
# from the release folder you rolled back to, against a dump that release
# produced.
#
# The outbox volumes are deliberately untouched. They hold records that were
# accepted but not yet published, and those are not part of the schema the
# failed upgrade changed -- replaying them after the restore is the point.
set -euo pipefail

dump="${1:?usage: bash scripts/restore-offline.sh backup/<project>-<timestamp>.sql}"
# Resolved against the operator's directory, before the `cd` below moves us. The
# installer writes its dump into the NEW release folder's backup/, and this
# script runs from the OLD one, so a relative path on the command line almost
# never means this release folder. Resolving it after the `cd` reported "missing
# or empty" for a file that plainly existed -- or, when the old folder happened
# to hold a same-named dump from an earlier upgrade, silently restored that one
# instead.
case "$dump" in
  /* | ?:[/\\]*) ;;             # already absolute; the second form is Git Bash on Windows
  *) dump="$PWD/$dump" ;;
esac

cd "$(dirname "$0")/.."
test -s "$dump" || { echo "$dump is missing or empty" >&2; exit 1; }
test -f .env || { echo "this release folder has no .env" >&2; exit 1; }
export AOW_IMAGE_VERSION="$(tr -d '\r\n' < release-version.txt)"
# Same check the installer makes: this value picks the image tags Compose
# resolves, so a folder whose version file is not a commit would silently start
# nothing at all.
[[ "$AOW_IMAGE_VERSION" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid release version" >&2; exit 1; }

dc() { docker compose -f compose.yml -f compose.bundle.yml --env-file .env "$@"; }

# Nothing may write while the database is being replaced. The consumer is the
# only writer, but the api, the agent and the enricher all hold reader
# connections, and the ingestor would keep publishing into a queue whose
# consumer is gone. The agent matters as much as the others here: `--clean`
# leads with DROP statements, and an in-flight SELECT of its own blocks one.
echo "stopping writers"
dc stop ingestor consumer enricher api agent ui

# --single-transaction, because the dump starts by dropping everything it is
# about to recreate. Without it a dump that is truncated, or from the wrong
# release, drops the schema, stops on the first error and leaves a half-dropped
# database with the writers already stopped. With it the restore is all or
# nothing, which is the only useful behaviour for a rollback.
echo "restoring $dump"
dc exec -T postgres sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" --single-transaction -v ON_ERROR_STOP=1 --quiet' \
  < "$dump"

echo "restarting"
dc up -d --no-build --pull never
dc exec -T api python - < scripts/release-smoke.py
echo "Restored $dump into release $AOW_IMAGE_VERSION"
