#!/usr/bin/env bash
# Print bundle alias and OCI manifest digest from a docker-save archive.
set -euo pipefail
tar -xOf "${1:?usage: bundle-image-manifests.sh images.tar}" index.json \
  | tr ',{}' '\n\n\n' \
  | awk -F'"' '
      /"digest"/ { digest = $4 }
      /"io\.containerd\.image\.name"/ {
        count = split($4, parts, "/")
        alias = parts[count]
        sub(/:.*/, "", alias)
        print alias, digest
      }'
