#!/usr/bin/env bash
# Run from the unpacked release on an offline Docker host. Existing named
# volumes are preserved, so a prior release folder remains the rollback unit.
#
# Compose fixes the project name (`name: aow` in compose.yml), so every release
# folder installs over the same volumes -- that is what makes an upgrade an
# upgrade. Set COMPOSE_PROJECT_NAME to install a second, independent copy; the
# release test in the README does exactly that.
set -euo pipefail

cd "$(dirname "$0")/.."
arch="$(docker info --format '{{.Architecture}}')"
[[ "$arch" == amd64 || "$arch" == x86_64 ]] || { echo "this release requires a Linux/amd64 Docker engine" >&2; exit 1; }
test -f .env || { echo "copy .env.example to .env and set passwords first" >&2; exit 1; }
sha256sum -c SHA256SUMS
sha256sum -c models.lock
bash scripts/verify-bundle-images.sh .
export AOW_IMAGE_VERSION="$(cat release-version.txt)"
[[ "$AOW_IMAGE_VERSION" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid release version" >&2; exit 1; }

dc() { docker compose -f compose.yml -f compose.bundle.yml --env-file .env "$@"; }

# An upgrade, not a first install: the previous release is still serving. Take
# a dump before the new images touch the schema, because an image rollback
# alone cannot undo a migration. Restore it with scripts/restore-offline.sh.
project="${COMPOSE_PROJECT_NAME:-aow}"
running_pg="$(docker ps -q \
  --filter "label=com.docker.compose.project=$project" \
  --filter "label=com.docker.compose.service=postgres")"
if [ -n "$running_pg" ]; then
  mkdir -p backup
  dump="backup/$project-$(date -u +%Y%m%dT%H%M%SZ).sql"
  echo "upgrading a running installation: dumping its database to $dump"
  docker exec "$running_pg" sh -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' \
    > "$dump"
  test -s "$dump" || { echo "pre-upgrade dump is empty; refusing to continue" >&2; exit 1; }
fi

docker load -i images.tar
dc config --quiet
dc up -d --no-build --pull never
dc exec -T api python - < scripts/release-smoke.py
echo "Release $AOW_IMAGE_VERSION is serving on ports 8080 and 8000"
