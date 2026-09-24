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

# Everything the bundle claims about itself, checked before anything on this
# host changes: SHA256SUMS over every file, no unlisted file, models.lock,
# images.bundle.lock against the CI manifest and the committed IMAGES.lock,
# and images.tar against images.bundle.lock by verified manifest digest.
bash scripts/verify-bundle.sh .
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

# Which of this release's images the engine already holds, recorded before
# anything is loaded. `docker load` cannot answer that afterwards: it prints
# "Loaded image" whether it unpacked the archive's bytes or found the content
# already in the store, which is exactly how the empty-archive bundle of
# docs/RELEASE-PROOF.md section 1 installed perfectly on the machine that
# packaged it and nowhere else. Every release drill before that one ran on that
# machine, so none of them could have caught it even in principle.
#
# This counts tags, not content: an engine that pulled the same layers but never
# tagged them reads as clean here. It is a floor under the claim, not a proof of
# it -- what proves the archive is self-contained is verify-bundle.sh above,
# which reads the archive's own bytes and never asks the daemon anything.
already=()
while read -r alias _; do
  test -n "$alias" || continue
  # </dev/null so nothing in the loop body can eat the lock file this loop is
  # reading; a half-consumed census would silently under-report.
  if docker image inspect "aow-bundle/$alias:$AOW_IMAGE_VERSION" >/dev/null 2>&1 </dev/null; then
    already+=("$alias")
  fi
done < images.bundle.lock

if [ -n "${AOW_REQUIRE_CLEAN_IMAGE_STORE:-}" ] && [ "${#already[@]}" -gt 0 ]; then
  echo "AOW_REQUIRE_CLEAN_IMAGE_STORE is set, and these release tags already exist: ${already[*]}" >&2
  exit 1
fi

# `docker load` exits 0 even when it could not unpack an image. A layer blob
# whose bytes do not match the digest its manifest names is refused by the
# content store, the tag is still created, and the only sign is a line in the
# output -- verified against Docker 29.8, which printed "Loaded image: ..."
# and "Error unpacking image ... content digest ... not found" and then exited
# 0. So the output is the check, not the exit status.
load_log="$(mktemp)"
trap 'rm -f "$load_log"' EXIT
docker load -i images.tar 2>&1 | tee "$load_log"
if grep -qi 'error' "$load_log"; then
  echo "docker load reported an error; the release has not been started" >&2
  exit 1
fi
expected_images="$(grep -c . images.bundle.lock)"
loaded_images="$(grep -c '^Loaded image' "$load_log" || true)"
if [ "$loaded_images" != "$expected_images" ]; then
  echo "docker load reported $loaded_images images, the release ships $expected_images" >&2
  exit 1
fi

# Say which install this was. An install onto an engine that already held these
# images is a legitimate re-install or upgrade, so it is reported rather than
# refused -- but it is not evidence that images.tar can install anywhere else,
# and the log has to stop reading as though it were.
if [ "${#already[@]}" -eq 0 ]; then
  echo "image store: this engine held none of the $expected_images release tags before the load; archive verification found their config and layers in images.tar"
else
  echo "image store: ${#already[@]} of $expected_images release tags were already in this engine before the load: ${already[*]}"
  echo "  A load that finds content already in the store succeeds even from an incomplete archive, so this install does not show that images.tar is self-contained. scripts/verify-bundle.sh checks that from the archive's bytes; only an install on an engine that has never held these images demonstrates it."
fi
dc config --quiet
dc up -d --no-build --pull never
dc exec -T api python - < scripts/release-smoke.py
echo "Release $AOW_IMAGE_VERSION is serving on ports 8080 and 8000"
