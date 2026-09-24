#!/usr/bin/env bash
# Bring up the real llama.cpp server with the staged Qwen3-1.7B-Q4_K_M model
# and run the adversarial grounding probe against it
# (tests/integration/model_grounding.py, F3). Release-candidate gate only --
# see the `model-grounding` job in .github/workflows/ci.yml for why this is
# not a per-PR gate, and for the actions/cache step that stages the model
# (scripts/stage_model.py) before this script runs.
#
# Mirrors the exact invocation documented in
# tests/integration/model_grounding.py's own module docstring: its own
# Compose project (`aow-f3`), the base compose.yml plus compose.model-probe.yml,
# `up -d --no-build --pull never llm` (the model server, never rebuilt here),
# then `run --rm --no-deps probe`.
set -euo pipefail

cd "$(dirname "$0")/.."
project="aow-f3-${GITHUB_RUN_ID:-$$}"
env_file=".env.example"

dc() { docker compose -p "$project" -f compose.yml -f compose.model-probe.yml --env-file "$env_file" "$@"; }

cleanup() {
  local status=$?
  if [ "$status" -ne 0 ]; then
    dc logs --tail=200 llm probe || true
  fi
  dc down --volumes --remove-orphans || true
}
trap cleanup EXIT

dc config --quiet
# Same reason the browser gate pulls `edge`: everything below runs with
# --pull never, so the llama.cpp server image has to be fetched first. On a
# developer machine it is already in the store because the stack has been run;
# on a clean runner it is not, and the first real run failed with
# `No such image: ghcr.io/ggml-org/llama.cpp:server@sha256:...` after having
# successfully staged the 1.28 GB model.
dc pull llm
dc up -d --no-build --pull never llm
dc run --rm --no-deps probe
echo "PASS: the real-model grounding probe held against llama.cpp + Qwen3-1.7B"
