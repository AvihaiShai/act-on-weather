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
# index.json is only a label, so the label is checked against content: every
# digest it claims is also the name of a blob inside the archive, and that blob
# is extracted and hashed here. That is what makes the check worth anything. A
# blob that hashes to the digest CI published *is* the manifest CI published,
# and the config and layers under it are named by digest inside those verified
# bytes -- so an image cannot be swapped inside the archive without either
# breaking sha256 or changing a digest this script compares.
#
# It reads the archive, not the daemon, so it runs before `docker load` and
# needs nothing but tar, awk and sha256sum. Same script on both sides: the
# staging machine runs it before shipping, the offline host runs it before
# loading.
set -euo pipefail

dir="${1:-.}"
cd "$dir"
test -f images.tar || { echo "no images.tar in $dir" >&2; exit 1; }
test -f images.bundle.lock || { echo "no images.bundle.lock in $dir" >&2; exit 1; }

# alias -> manifest digest, as recorded inside the archive.
actual="$(bash scripts/bundle-image-manifests.sh images.tar)"

status=0
expected_count=0
blobs=()
declare -A alias_of=()
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
  else
    blobs+=("blobs/sha256/${want#sha256:}")
    alias_of["blobs/sha256/${want#sha256:}"]="$alias"
  fi
done < images.bundle.lock

actual_count="$(printf '%s\n' "$actual" | grep -c . || true)"
if [ "$actual_count" != "$expected_count" ]; then
  echo "images.tar holds $actual_count images, images.bundle.lock lists $expected_count" >&2
  status=1
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# One pass over the archive for every manifest blob at once. Each is a few
# kilobytes; the archive around them is gigabytes, so extracting them one at a
# time would mean re-reading the whole thing once per image.
if [ "${#blobs[@]}" -gt 0 ]; then
  tar -xf images.tar -C "$work" "${blobs[@]}" 2>/dev/null \
    || { echo "images.tar names a manifest blob it does not contain" >&2; status=1; }
  for blob in "${blobs[@]}"; do
    if [ ! -f "$work/$blob" ]; then
      echo "images.tar is missing $blob" >&2
      status=1
      continue
    fi
    got="$(sha256sum "$work/$blob" | awk '{print $1}')"
    if [ "$got" != "${blob##*/}" ]; then
      echo "images.tar: $blob holds different bytes (sha256:$got)" >&2
      status=1
    fi
  done
fi

# ------------------------------------------------- can it actually be loaded --
# The checks above prove the archive names the right manifests. They say
# nothing about whether the content those manifests point at is in the archive,
# because a manifest is a list of digests and nothing was comparing that list
# against what the tar holds.
#
# That is not a hypothetical gap. `docker save` on a containerd-image-store
# engine exports a registry image whose manifest carries no platform descriptor
# -- which is exactly how CI publishes `services` and `ui` -- as the manifest
# blob and nothing else: no config, no layers, exit status 0. A bundle built
# that way passes every other check in this folder and then dies on a clean
# host with `failed to read config content`. It only dies on a clean host: an
# engine that still holds the layers from its own `docker pull` loads the same
# broken archive without complaint, which is why this survived release drills
# run on the machine that packaged them.
#
# A multi-platform image must stay legal. `docker save` writes the upstream
# index listing every platform but stores only the one it exported, so absent
# children are normal and expected. The rule is therefore per image, not per
# blob: somewhere under each alias there must be one image manifest that is
# complete and declares this release's platform.
want_platform="${AOW_BUNDLE_PLATFORM:-linux/amd64}"

if [ "$status" -eq 0 ]; then
  # One listing answers every presence question below without extracting
  # anything; extraction is reserved for the small JSON blobs to be read.
  tar -tf images.tar | sed -n 's|^\./||; s|^blobs/sha256/||p' | LC_ALL=C sort -u > "$work/present"

  # Children of every index, and the indexless manifests, in one extra pass.
  : > "$work/candidates"
  for blob in "${blobs[@]}"; do
    if grep -q '"manifests"' "$work/$blob"; then
      for kid in $(tr ',{}[]' '\n\n\n\n\n' < "$work/$blob" | awk -F'"' '/"digest"/ {print $4}'); do
        LC_ALL=C grep -qxF "${kid#sha256:}" "$work/present" \
          && printf '%s %s\n' "${alias_of[$blob]}" "blobs/sha256/${kid#sha256:}" >> "$work/candidates"
      done
    else
      printf '%s %s\n' "${alias_of[$blob]}" "$blob" >> "$work/candidates"
    fi
  done
  mapfile -t kid_blobs < <(awk '{print $2}' "$work/candidates" | LC_ALL=C sort -u)
  [ "${#kid_blobs[@]}" -eq 0 ] || tar -xf images.tar -C "$work" "${kid_blobs[@]}" 2>/dev/null || true

  # The config blob of each candidate, so its platform can be read. An index's
  # own platform field would do for multi-platform images, but the indexless
  # manifests that make this check necessary declare it nowhere else.
  # The config is the first digest after the "config" key. Split on JSON
  # punctuation first: a manifest is written on one line, so awk over the raw
  # bytes would field-split the whole document rather than one key at a time.
  config_of() {
    tr ',{}[]' '\n\n\n\n\n' < "$1" \
      | awk -F'"' '/"config"/ {found = 1} found && /"digest"/ {print substr($4, 8); exit}'
  }
  : > "$work/configs"
  while read -r _ blob; do
    grep -q '"manifests"' "$work/$blob" && continue
    printf 'blobs/sha256/%s\n' "$(config_of "$work/$blob")" >> "$work/configs"
  done < "$work/candidates"
  mapfile -t config_blobs < <(LC_ALL=C sort -u "$work/configs")
  [ "${#config_blobs[@]}" -eq 0 ] || tar -xf images.tar -C "$work" "${config_blobs[@]}" 2>/dev/null || true

  for alias in $(awk '{print $1}' "$work/candidates" | LC_ALL=C sort -u); do
    loadable=0
    reasons=""
    while read -r candidate blob; do
      [ "$candidate" = "$alias" ] || continue
      grep -q '"manifests"' "$work/$blob" && continue
      short="${blob##*/}"
      short="${short:0:12}"

      # Every digest in an image manifest is its config or one of its layers.
      total=0
      missing=0
      for digest in $(tr ',{}[]' '\n\n\n\n\n' < "$work/$blob" | awk -F'"' '/"digest"/ {print $4}'); do
        total=$((total + 1))
        LC_ALL=C grep -qxF "${digest#sha256:}" "$work/present" || missing=$((missing + 1))
      done
      if [ "$missing" -gt 0 ]; then
        reasons="$reasons
    $short: $missing of $total blobs (its config and layers) are not in the archive"
        continue
      fi

      config="$work/blobs/sha256/$(config_of "$work/$blob")"
      platform="$(tr ',{}' '\n\n\n' < "$config" 2>/dev/null \
        | awk -F'"' '$2 == "os" {os = $4} $2 == "architecture" {arch = $4} END {print os "/" arch}')"
      case "$platform" in
        "$want_platform") loadable=1 ;;
        # An attestation manifest rides alongside a buildx image and declares
        # no platform of its own. It is not something to load, so not a fault.
        unknown/unknown | /) ;;
        *) reasons="$reasons
    $short: complete, but it is $platform" ;;
      esac
    done < "$work/candidates"

    if [ "$loadable" -eq 0 ]; then
      echo "images.tar cannot load $alias as $want_platform:$reasons" >&2
      status=1
    fi
  done
fi

if [ "$status" -eq 0 ]; then
  echo "images.tar matches images.bundle.lock ($expected_count images, by verified manifest digest)"
  echo "images.tar is complete: every image has its config and layers, as $want_platform"
fi
exit "$status"
