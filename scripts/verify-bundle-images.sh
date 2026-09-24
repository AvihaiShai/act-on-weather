#!/usr/bin/env bash
# Prove that images.tar holds the images the release says it holds.
#
# SHA256SUMS answers "did these bytes arrive intact". It cannot answer "are
# these the bytes CI built", because whoever replaced the tar would replace
# SHA256SUMS with it. This script answers the second question: `docker save`
# keeps each image's upstream manifest digest in the archive's index.json, and
# images.bundle.lock records the digest each alias was tagged from -- for the
# two application images, straight out of the CI run artifact.
#
# It reads the archive, not the daemon, so it runs before `docker load` and
# needs nothing but tar and awk. Same script on both sides: the staging machine
# runs it before shipping, the offline host runs it before loading.
set -euo pipefail

dir="${1:-.}"
cd "$dir"
test -f images.tar || { echo "no images.tar in $dir" >&2; exit 1; }
test -f images.bundle.lock || { echo "no images.bundle.lock in $dir" >&2; exit 1; }

# alias -> manifest digest, as recorded inside the archive.
actual="$(bash scripts/bundle-image-manifests.sh images.tar)"

status=0
expected_count=0
while read -r alias ref; do
  test -n "$alias" || continue
  expected_count=$((expected_count + 1))
  want="${ref##*@}"
  case "$want" in
    sha256:*) ;;
    *) echo "images.bundle.lock: $alias is not pinned by digest: $ref" >&2; status=1; continue ;;
  esac
  got="$(printf '%s\n' "$actual" | awk -v a="$alias" '$1 == a { print $2; exit }')"
  if [ -z "$got" ]; then
    echo "images.tar does not contain $alias" >&2
    status=1
  elif [ "$got" != "$want" ]; then
    echo "$alias: images.tar has $got, release expects $want ($ref)" >&2
    status=1
  fi
done < images.bundle.lock

actual_count="$(printf '%s\n' "$actual" | grep -c . || true)"
if [ "$actual_count" != "$expected_count" ]; then
  echo "images.tar holds $actual_count images, images.bundle.lock lists $expected_count" >&2
  status=1
fi

if [ "$status" -eq 0 ]; then
  echo "images.tar matches images.bundle.lock ($expected_count images, by manifest digest)"
fi
exit "$status"
