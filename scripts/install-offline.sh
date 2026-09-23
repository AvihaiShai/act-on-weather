#!/usr/bin/env bash
# Run from the unpacked release on an offline Docker host. Existing named
# volumes are preserved, so a prior release folder remains the rollback unit.
set -euo pipefail

cd "$(dirname "$0")/.."
arch="$(docker info --format '{{.Architecture}}')"
[[ "$arch" == amd64 || "$arch" == x86_64 ]] || { echo "this release requires a Linux/amd64 Docker engine" >&2; exit 1; }
test -f .env || { echo "copy .env.example to .env and set passwords first" >&2; exit 1; }
sha256sum -c SHA256SUMS
sha256sum -c models.lock
export AOW_IMAGE_VERSION="$(cat release-version.txt)"
[[ "$AOW_IMAGE_VERSION" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid release version" >&2; exit 1; }

docker load -i images.tar
dc() { docker compose -f compose.yml -f compose.bundle.yml --env-file .env "$@"; }
dc config --quiet
dc up -d --no-build --pull never
dc exec -T api python - < scripts/release-smoke.py
echo "Release $AOW_IMAGE_VERSION is serving on ports 8080 and 8000"
