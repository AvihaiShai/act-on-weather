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

cd "$(dirname "$0")/.."
dump="${1:?usage: bash scripts/restore-offline.sh backup/<project>-<timestamp>.sql}"
test -s "$dump" || { echo "$dump is missing or empty" >&2; exit 1; }
test -f .env || { echo "this release folder has no .env" >&2; exit 1; }
export AOW_IMAGE_VERSION="$(cat release-version.txt)"
# Same check the installer makes: this value picks the image tags Compose
# resolves, so a folder whose version file is not a commit would silently start
# nothing at all.
[[ "$AOW_IMAGE_VERSION" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid release version" >&2; exit 1; }

dc() { docker compose -f compose.yml -f compose.bundle.yml --env-file .env "$@"; }

# Nothing may write while the database is being replaced. The consumer is the
# only writer, but the API and the enricher hold connections, and the ingestor
# would keep publishing into a queue whose consumer is gone.
echo "stopping writers"
dc stop ingestor consumer enricher api ui

echo "restoring $dump"
dc exec -T postgres sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 --quiet' \
  < "$dump"

echo "restarting"
dc up -d --no-build --pull never
dc exec -T api python - < scripts/release-smoke.py
echo "Restored $dump into release $AOW_IMAGE_VERSION"
