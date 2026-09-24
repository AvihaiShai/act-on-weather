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

# images.lock is a file someone downloaded, so treat it as a claim rather than
# as proof. Two things make the claim hard to write by hand. First, the
# references have to live in this repository's own GHCR namespace, taken from
# the git remote and not from the lock. Second, the registry itself still has
# to agree that the `sha-<commit>` tag points at exactly that digest, and only
# the publish step of a green `main` run ever creates that tag: it runs after
# the scans and the integration test, pull requests never get the credentials
# to push, and `main` is branch-protected with lint, unit, guard and
# build-and-scan all required, admins included. So a `sha-<commit>` tag that
# resolves to this digest means those four jobs passed on this exact commit.
origin="$(git config --get remote.origin.url)"
slug="${origin#*github.com}"
slug="${slug#[:/]}"
slug="${slug%.git}"
image_root="ghcr.io/$(printf '%s' "$slug" | tr '[:upper:]' '[:lower:]')"
if ! [[ "$image_root" =~ ^ghcr\.io/[a-z0-9._-]+/[a-z0-9._-]+$ ]]; then
  echo "cannot derive a GHCR namespace from the git remote: $origin" >&2
  exit 1
fi

# Pull by digest first: that reference cannot be moved under us. Then resolve
# the CI tag and require it to land on the same digest.
for component in services ui; do
  case "$component" in
    services) ref="$services_ref" ;;
    ui) ref="$ui_ref" ;;
  esac
  if [ "$ref" != "$image_root/$component@${ref##*@}" ]; then
    echo "$component: $ref is not an image of $image_root" >&2
    exit 1
  fi
  docker pull "$ref"
  tag="$image_root/$component:sha-$commit"
  docker pull "$tag"
  published="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$tag" \
    | grep -F -m1 "$image_root/$component@sha256:")"
  if [ "$published" != "$ref" ]; then
    echo "$component: the registry resolves $tag to $published, images.lock claims $ref" >&2
    exit 1
  fi
done

out="dist/aow-$commit"
test ! -e "$out" || { echo "$out already exists" >&2; exit 1; }
mkdir -p "$out/models"
git archive --format=tar HEAD | tar -xf - -C "$out"
# `git archive` ships what is committed, and nothing else. An installer script
# that is still only in the working tree would leave a bundle that cannot
# verify or install itself, and `git diff HEAD` above cannot see that because
# an untracked file is not a difference.
for needed in install-offline.sh verify-bundle.sh verify-bundle-images.sh bundle-image-manifests.sh; do
  test -f "$out/scripts/$needed" || { echo "scripts/$needed is not in the bundle: commit it first" >&2; exit 1; }
done
cp models/Qwen3-1.7B-Q4_K_M.gguf "$out/models/"
# Not "images.lock": the repository already tracks IMAGES.lock, and a
# staging machine with a case-insensitive filesystem (Windows, macOS by
# default) would silently overwrite one with the other.
cp "$lock" "$out/ci-images.lock"
printf '%s\n' "$commit" > "$out/release-version.txt"

docker tag "$services_ref" "aow-bundle/services:$commit"
docker tag "$ui_ref" "aow-bundle/ui:$commit"

# .env.example supplies only placeholders for Compose interpolation here.
docker compose --env-file .env.example pull postgres rabbitmq llm edge
# The observability overlay is opt-in at run time but not at packaging time:
# an air-gapped host cannot pull prometheus and grafana later, so a bundle that
# leaves them out is a bundle where the monitoring overlay simply cannot be
# started. They are ~313 MB of the archive and are loaded, not run, unless the
# operator passes `-f compose.observability.yml`.
obs="-f compose.yml -f compose.observability.yml"
# shellcheck disable=SC2086  # deliberate word splitting: two -f flags
docker compose $obs --env-file .env.example pull prometheus grafana edge-observability
# shellcheck disable=SC2086
images="$(docker compose $obs --env-file .env.example config --images)"

# edge and edge-observability are both nginx and share one bundle alias. That
# is only sound while they pin the same reference, and nothing else would
# notice if a later edit gave them different bytes -- the bundle would simply
# run the observability edge on the main edge's image. Fail here instead.
# Same two reference shapes as the loop below: `nginx:alpine@sha256:...` today,
# `nginx@sha256:...` if a future pin drops the tag.
nginx_refs="$(printf '%s\n' "$images" | awk '/^nginx[:@]/ {print}' | LC_ALL=C sort -u)"
[ "$(printf '%s\n' "$nginx_refs" | grep -c .)" -eq 1 ] \
  || { echo "edge and edge-observability use different nginx images" >&2; exit 1; }
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
# bundle alias -> the repository it comes from. Matched against Compose's
# rendered reference, which takes one of two shapes depending on whether the
# pin kept a tag: `postgres:17-alpine@sha256:...` but `prom/prometheus@sha256:...`.
# An earlier version matched a literal `<repo>:` prefix and so could not see a
# digest-only pin at all -- packaging failed with "no image for prometheus",
# which is the right way round, but the matcher has to handle both forms.
for pair in 'postgres=postgres' 'rabbitmq=rabbitmq' 'llm=ghcr.io/ggml-org/llama.cpp' \
            'edge=nginx' 'prometheus=prom/prometheus' 'grafana=grafana/grafana'; do
  name="${pair%%=*}"
  repo="${pair#*=}"
  ref=""
  while IFS= read -r candidate; do
    # A prefix test, not a substring one: `nginx` must not match `nginx-extras`,
    # and the delimiter is what proves the repository name ended there.
    case "$candidate" in
      "$repo:"* | "$repo@"*) ref="$candidate"; break ;;
    esac
  done <<< "$images"
  test -n "$ref" || { echo "no image for $name in the rendered compose files" >&2; exit 1; }
  [[ "$ref" == *@sha256:* ]] || { echo "$name is not pinned by digest in compose.yml: $ref" >&2; exit 1; }
  docker tag "$ref" "aow-bundle/$name:$commit"
  printf '%s %s\n' "$name" "$ref" >> "$out/images.bundle.lock"
done
printf 'stage %s\n' "$stage_ref" >> "$out/images.bundle.lock"

docker save -o "$out/images.tar" \
  "aow-bundle/services:$commit" "aow-bundle/ui:$commit" \
  "aow-bundle/postgres:$commit" "aow-bundle/rabbitmq:$commit" \
  "aow-bundle/llm:$commit" "aow-bundle/edge:$commit" \
  "aow-bundle/stage:$commit" "aow-bundle/demos:$commit" \
  "aow-bundle/prometheus:$commit" "aow-bundle/grafana:$commit"

# The proof runner is built from this release's pinned Dockerfile on the
# connected machine. Record its manifest digest from the archive we ship.
demos_digest="$(bash scripts/bundle-image-manifests.sh "$out/images.tar" | awk '$1 == "demos" {print $2}')"
[[ "$demos_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "missing demos manifest digest" >&2; exit 1; }
printf 'demos aow-bundle/demos@%s\n' "$demos_digest" >> "$out/images.bundle.lock"

(
  cd "$out"
  # Everything, not only the two large binaries. The code, the Compose files
  # and the migrations travel by plain file copy like anything else, so they
  # get the same one-command check on the far side.
  find . -type f ! -name SHA256SUMS ! -name .env -print0 \
    | sort -z | xargs -0 sha256sum > SHA256SUMS
)

# Fail here, on the connected machine, rather than ship a bundle that does not
# match what it claims to contain. It is the same gate the offline installer
# runs, so a folder that passes here is a folder that installs.
bash scripts/verify-bundle.sh "$out"
echo "Offline release ready: $out"
echo "Carry the SHA256SUMS digest printed above out of band: the installer"
echo "checks it when it is passed as AOW_SHA256SUMS."
