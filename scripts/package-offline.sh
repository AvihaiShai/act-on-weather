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
images="$(docker compose --env-file .env.example config --images)"
for pair in 'postgres:postgres:' 'rabbitmq:rabbitmq:' 'llm:ghcr.io/ggml-org/llama.cpp:' 'edge:nginx:'; do
  name="${pair%%:*}"
  prefix="${pair#*:}"
  ref="$(printf '%s\n' "$images" | awk -v p="$prefix" 'index($0, p) == 1 {print; exit}')"
  test -n "$ref" || { echo "no image for $name in compose.yml" >&2; exit 1; }
  docker tag "$ref" "aow-bundle/$name:$commit"
done

docker save -o "$out/images.tar" \
  "aow-bundle/services:$commit" "aow-bundle/ui:$commit" \
  "aow-bundle/postgres:$commit" "aow-bundle/rabbitmq:$commit" \
  "aow-bundle/llm:$commit" "aow-bundle/edge:$commit"
(
  cd "$out"
  sha256sum images.tar models/Qwen3-1.7B-Q4_K_M.gguf > SHA256SUMS
)
echo "Offline release ready: $out"
