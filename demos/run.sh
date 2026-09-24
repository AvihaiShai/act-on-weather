#!/usr/bin/env bash
#
# Dispatcher for the demo scripts, by name rather than by filename.
#
# Used as the entrypoint of the `demos` service in compose.tools.yml:
#
#   docker compose -f compose.tools.yml run --rm demos offline
#
# It runs on a host with bash too -- `bash demos/run.sh offline` is the same
# thing -- so there is one list of proofs, not one per platform.
set -euo pipefail

cd "$(dirname "$0")/.."

usage() {
  cat <<'EOF'
Proofs. Each one runs against the stack that is already up.

  offline        M6  -- air-gapped operation, and the no-guessing rule
  questions      M7/M8 -- agent breadth, including what it refuses
  no-data-loss   M11 -- consumer, database, broker and poison-message drills
  update         M12 -- an edit through the queue, with its history
  reenrich       the local model is a presentation layer, not a dependency
  all            every one of the above, in order

Run one with:
  docker compose -f compose.tools.yml run --rm demos <name>
EOF
}

run_one() {
  case "$1" in
    offline)      bash demos/01_offline.sh ;;
    no-data-loss) bash demos/02_no_data_loss.sh ;;
    update)       bash demos/03_update.sh ;;
    reenrich)     bash demos/04_reenrich.sh ;;
    questions)    bash demos/05_questions.sh ;;
    *)
      echo "unknown proof: $1" >&2
      echo >&2
      usage >&2
      return 2
      ;;
  esac
}

case "${1:-help}" in
  help | -h | --help)
    usage
    ;;
  all)
    # The same order as `make demo`: prove the system works and answers before
    # breaking it, so a failure in a drill is unambiguous.
    for name in offline questions no-data-loss update reenrich; do
      run_one "$name"
    done
    ;;
  *)
    run_one "$1"
    ;;
esac
