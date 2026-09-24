#!/usr/bin/env bash
# The whole pre-install gate for an offline release folder, in one command.
# scripts/install-offline.sh runs it before it touches the host, and
# scripts/package-offline.sh runs it before it calls a folder shippable, so
# both sides check the same things in the same order.
#
# It answers four questions, cheapest failure first:
#
#   1. did every file arrive intact, and did anything arrive that the release
#      does not list?                            -- SHA256SUMS
#   2. is the model the one the repository pins?  -- models.lock
#   3. does images.bundle.lock agree with the two locks that were fixed before
#      this folder existed: the CI run artifact (ci-images.lock) and the
#      committed IMAGES.lock?
#   4. does images.tar actually hold those images, by verified manifest digest?
#      -- scripts/verify-bundle-images.sh
#
# What it cannot answer, and the README says so: SHA256SUMS is self-attesting.
# Whoever can rewrite the folder can rewrite the checksums with it, and this
# script is itself one of the files being checked. Step 3 is what narrows that
# down -- an attacker also has to match a digest recorded in a CI run and one
# committed to git. The only anchor outside the folder entirely is the digest
# of SHA256SUMS itself: it is printed here, and if you were handed it out of
# band, pass it as AOW_SHA256SUMS and this script checks it.
set -euo pipefail

cd "${1:-.}"
for required in SHA256SUMS models.lock IMAGES.lock ci-images.lock images.bundle.lock release-version.txt; do
  test -f "$required" || { echo "not an offline release folder: no $required" >&2; exit 1; }
done

# ----------------------------------------------------------------- 1. files --
own="$(sha256sum SHA256SUMS | awk '{print $1}')"
echo "SHA256SUMS sha256:$own"
if [ -n "${AOW_SHA256SUMS:-}" ] && [ "$own" != "${AOW_SHA256SUMS#sha256:}" ]; then
  echo "SHA256SUMS does not match the digest supplied out of band" >&2
  exit 1
fi

sha256sum -c SHA256SUMS

# `sha256sum -c` verifies the files that are listed. It is silent about a file
# that was added, so list those too: the same `find` that wrote SHA256SUMS,
# minus the paths an install legitimately creates afterwards (the pre-upgrade
# dumps in backup/, the operator's .env, a half-written model download).
#
# promotion-record.json is excluded for a different reason. When release.yml
# builds the bundle it writes the record in and reseals SHA256SUMS over it, so
# there it IS listed and the `sha256sum -c` above checks it like any other
# file. But the bundle an operator actually ships is rebuilt by
# package-offline.sh, which does not write the record -- it is downloaded
# separately, as the aow-promotion-<sha> workflow artifact. docs/RELEASE.md
# tells the operator to compare it against the folder, so the folder has to
# tolerate it being dropped in. Excluding it here does not weaken the check:
# listed, it is still verified; unlisted, it is the one file the documented
# procedure legitimately adds.
unlisted="$(
  comm -23 \
    <(find . -type f ! -name SHA256SUMS ! -name .env ! -name '*.part' \
        ! -path './promotion-record.json' \
        ! -path './backup/*' ! -path '*/__pycache__/*' -print | LC_ALL=C sort) \
    <(awk '{print substr($0, 67)}' SHA256SUMS | LC_ALL=C sort)  # 64 hex + 2 separators, then the name
)"
if [ -n "$unlisted" ]; then
  echo "files present that SHA256SUMS does not list:" >&2
  printf '%s\n' "$unlisted" | sed 's/^/  /' >&2
  # A release folder copied to removable media by a file manager rather than
  # by tar or rsync arrives with the file manager's own metadata in it, and
  # that is by far the most likely way an operator meets this error. Name it,
  # because "files present that SHA256SUMS does not list" on a .DS_Store reads
  # like a tampered bundle and is not one. These are still refused rather than
  # ignored: a release folder holds what the release put in it, and nothing a
  # reviewer has to take on trust.
  if printf '%s\n' "$unlisted" | grep -qE '(^|/)(\.DS_Store|\._[^/]*|desktop\.ini|Thumbs\.db|\.Spotlight-V100|\.Trashes|\.fseventsd)$'; then
    echo >&2
    echo "Some of those are file-manager metadata, not release content. They are" >&2
    echo "added by copying the folder with Finder or Explorer, typically via" >&2
    echo "removable media. Delete them and re-run, or re-copy the folder with" >&2
    echo "'tar -cf - dist/aow-<sha> | ...' or rsync, which do not create them." >&2
  fi
  exit 1
fi

# ----------------------------------------------------------------- 2. model --
sha256sum -c models.lock

# ------------------------------------------------- 3. what the images claim --
# images.bundle.lock says which image each bundle alias is supposed to be. On
# its own it is just another file in the folder. These checks tie every one of
# its lines to a record made before the folder existed.
commit="$(awk '$1 == "commit" {print $2}' ci-images.lock)"
version="$(cat release-version.txt)"
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || { echo "ci-images.lock has no release commit" >&2; exit 1; }
if [ "$commit" != "$version" ]; then
  echo "release-version.txt says $version, the CI manifest was built for $commit" >&2
  exit 1
fi

status=0
seen_services=0
seen_ui=0
while read -r alias ref; do
  test -n "$alias" || continue
  # IMAGES.lock records the untagged form, so drop a :tag if there is one.
  # Only the last path segment is examined, or a registry:port would lose its
  # port instead.
  repo="${ref%@*}"
  case "${repo##*/}" in *:*) repo="${repo%:*}" ;; esac
  case "$alias" in
    services|ui)
      # Published by CI from the release commit, and recorded in the run
      # artifact that packaging was given.
      want="$(awk -v a="$alias" '$1 == a {print $2; exit}' ci-images.lock)"
      if [ "$ref" != "$want" ]; then
        echo "$alias: images.bundle.lock has $ref, the CI manifest has ${want:-nothing}" >&2
        status=1
      fi
      if [ "$alias" = services ]; then seen_services=1; else seen_ui=1; fi
      ;;
    demos)
      # The proof runner is built from this release's pinned Dockerfile while
      # packaging, so there is no earlier record to check it against: its
      # digest is read out of the archive at that moment. SHA256SUMS is all
      # that protects it in transit, which is stated in the README rather than
      # dressed up as something stronger.
      if [ "$repo" != "aow-bundle/demos" ]; then
        echo "demos: expected a locally built aow-bundle/demos image, got $ref" >&2
        status=1
      fi
      ;;
    *)
      # Upstream images. IMAGES.lock is committed, and CI fails the build if it
      # ever stops matching what the Compose files and Dockerfiles pin.
      want="$(awk -v r="$repo" 'index($0, r "@") == 1 {print; exit}' IMAGES.lock)"
      if [ -z "$want" ]; then
        echo "$alias: $repo is not in IMAGES.lock" >&2
        status=1
      elif [ "${ref##*@}" != "${want##*@}" ]; then
        echo "$alias: images.bundle.lock has ${ref##*@}, IMAGES.lock has ${want##*@}" >&2
        status=1
      fi
      ;;
  esac
done < images.bundle.lock

if [ "$seen_services" -eq 0 ] || [ "$seen_ui" -eq 0 ]; then
  echo "images.bundle.lock does not carry both application images" >&2
  status=1
fi
[ "$status" -eq 0 ] || exit 1
echo "images.bundle.lock matches the CI manifest for $commit and the committed IMAGES.lock"

# ----------------------------------------------------------------- 4. images --
bash scripts/verify-bundle-images.sh .

echo "Bundle verified: $(pwd)"
