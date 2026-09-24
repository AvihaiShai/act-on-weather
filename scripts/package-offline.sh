#!/usr/bin/env bash
# On a connected staging machine, assemble the exact CI images and pinned
# upstream images into a folder that can be copied onto an offline host.
set -euo pipefail

cd "$(dirname "$0")/.."
arch="$(docker info --format '{{.Architecture}}')"
[[ "$arch" == amd64 || "$arch" == x86_64 ]] || { echo "CI images require a Linux/amd64 Docker engine" >&2; exit 1; }
lock="${1:?usage: bash scripts/package-offline.sh path/to/images.lock}"
test -f "$lock"

commit="$(awk '$1 == "commit" {print $2}' "$lock")"
services_ref="$(awk '$1 == "services" {print $2}' "$lock")"
ui_ref="$(awk '$1 == "ui" {print $2}' "$lock")"
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid commit in images.lock" >&2; exit 1; }
for ref in "$services_ref" "$ui_ref"; do
  [[ "$ref" =~ ^ghcr\.io/.+@sha256:[0-9a-f]{64}$ ]] || { echo "invalid image digest: $ref" >&2; exit 1; }
done
test "$(git rev-parse HEAD)" = "$commit" || { echo "check out commit $commit first" >&2; exit 1; }
git diff --quiet HEAD -- || { echo "tracked changes are not in the release commit" >&2; exit 1; }
sha256sum -c models.lock

out="dist/aow-$commit"
test ! -e "$out" || { echo "$out already exists" >&2; exit 1; }
mkdir -p "$out/models"
git archive --format=tar HEAD | tar -xf - -C "$out"
cp models/Qwen3-1.7B-Q4_K_M.gguf "$out/models/"
cp "$lock" "$out/images.lock"
printf '%s\n' "$commit" > "$out/release-version.txt"

docker pull "$services_ref"
docker pull "$ui_ref"
docker tag "$services_ref" "aow-bundle/services:$commit"
docker tag "$ui_ref" "aow-bundle/ui:$commit"

# .env.example supplies only placeholders for Compose interpolation here.
docker compose --env-file .env.example pull postgres rabbitmq llm edge
# The monitoring overlay is optional at runtime, but its images must travel
# with the bundle if an offline operator enables it later.
docker compose -f compose.yml -f compose.observability.yml --env-file .env.example \
  pull prometheus grafana edge-observability
images="$(docker compose -f compose.yml -f compose.observability.yml \
  --env-file .env.example config --images)"
docker compose -f compose.tools.yml --env-file .env.example pull stage
docker compose -f compose.tools.yml --env-file .env.example build demos
stage_ref="$(docker compose -f compose.tools.yml --env-file .env.example config --images | awk '/^python:.*@sha256:/ {print; exit}')"
test -n "$stage_ref" || { echo "no pinned stage image in compose.tools.yml" >&2; exit 1; }
docker tag "$stage_ref" "aow-bundle/stage:$commit"
docker tag aow/demos:dev "aow-bundle/demos:$commit"

# What each bundle alias is supposed to be: the alias, and the registry
# reference it was tagged from. The installer re-checks this against the tar on
# the offline host. SHA256SUMS proves images.tar arrived intact; this proves
# that the intact tar holds the images CI built, scanned and published.
{ printf 'services %s\n' "$services_ref"; printf 'ui %s\n' "$ui_ref"; } > "$out/images.bundle.lock"
for pair in 'postgres:postgres:' 'rabbitmq:rabbitmq:' 'llm:ghcr.io/ggml-org/llama.cpp:' \
            'edge:nginx:' 'prometheus:prom/prometheus:' 'grafana:grafana/grafana:' \
            'edge-observability:nginx:'; do
  name="${pair%%:*}"
  prefix="${pair#*:}"
  ref="$(printf '%s\n' "$images" | awk -v p="$prefix" 'index($0, p) == 1 {print; exit}')"
  test -n "$ref" || { echo "no image for $name in compose.yml" >&2; exit 1; }
  [[ "$ref" == *@sha256:* ]] || { echo "$name is not pinned by digest in compose.yml: $ref" >&2; exit 1; }
  docker tag "$ref" "aow-bundle/$name:$commit"
  printf '%s %s\n' "$name" "$ref" >> "$out/images.bundle.lock"
done
printf 'stage %s\n' "$stage_ref" >> "$out/images.bundle.lock"

docker save -o "$out/images.tar" \
  "aow-bundle/services:$commit" "aow-bundle/ui:$commit" \
  "aow-bundle/postgres:$commit" "aow-bundle/rabbitmq:$commit" \
  "aow-bundle/llm:$commit" "aow-bundle/edge:$commit" \
  "aow-bundle/prometheus:$commit" "aow-bundle/grafana:$commit" \
  "aow-bundle/edge-observability:$commit" \
  "aow-bundle/stage:$commit" "aow-bundle/demos:$commit"

# The proof runner is built from this release's pinned Dockerfile on the
# connected machine. Record its manifest digest from the archive we ship.
demos_digest="$(bash scripts/bundle-image-manifests.sh "$out/images.tar" | awk '$1 == "demos" {print $2}')"
[[ "$demos_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "missing demos manifest digest" >&2; exit 1; }
printf 'demos aow-bundle/demos@%s\n' "$demos_digest" >> "$out/images.bundle.lock"

# Fail here, on the connected machine, rather than ship a bundle whose contents
# do not match what it claims to contain.
bash scripts/verify-bundle-images.sh "$out"

(
  cd "$out"
  # Everything, not only the two large binaries. The code, the Compose files
  # and the migrations travel by plain file copy like anything else, so they
  # get the same one-command check on the far side.
  find . -type f ! -name SHA256SUMS ! -name .env -print0 \
    | sort -z | xargs -0 sha256sum > SHA256SUMS
)
echo "Offline release ready: $out"
