#!/usr/bin/env bash
# Run a packaged proof against this installed release, using only local images.
set -euo pipefail
cd "$(dirname "$0")/.."
test -f .env || { echo "this release folder has no .env" >&2; exit 1; }
export AOW_IMAGE_VERSION="$(cat release-version.txt)"
[[ "$AOW_IMAGE_VERSION" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid release version" >&2; exit 1; }
export AOW_PROJECT="${COMPOSE_PROJECT_NAME:-aow}"
# The tools Compose file has its own project. Pass the stack project through
# AOW_PROJECT instead of changing the tools project's name on the host.
# This lane and the operator refresh added the same knob under two names at
# once; AOW_PROJECT is the one that survived the merge, because it also names
# the tools project itself and the API address.
unset COMPOSE_PROJECT_NAME
# `run` does not support --no-build (Compose v5 rejects that flag). The bundle
# overlay removes the development build recipe with !reset, and --pull never
# makes a missing aow-bundle/demos:<commit> image fail without network access.
docker compose -f compose.tools.yml -f compose.tools.bundle.yml --env-file .env \
  run --rm --pull never demos "${@:-offline}"
