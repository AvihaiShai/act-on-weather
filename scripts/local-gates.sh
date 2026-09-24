#!/usr/bin/env bash
# Run the checks that gate a pull request, locally, before pushing.
#
# Why this exists rather than a line in the README telling you to run three
# commands. The F9 session pushed a branch that failed CI's `ruff format
# --check`, having run the same three gates locally and read them as green:
# the commands were chained and the combined output grepped for a success
# line, and the format check reports failure by printing something the grep
# did not match. A red gate read as green. That is the same defect this
# repository keeps finding in its own proofs -- output that is displayed
# rather than checked -- so the fix is the same: run each gate on its own and
# look at its exit code, never at its text.
#
# Each gate below runs separately, its status is captured, and the summary is
# computed from those statuses. There is no path through this script where a
# failing gate produces a zero exit.
#
# These are the four cheap PR gates. It does not run build-and-scan's Compose
# integration test, the browser gate or the model probe -- those need a built
# stack and are documented separately. Passing this does not promise CI is
# green; failing it promises CI is not.
set -uo pipefail          # deliberately NOT -e: a failing gate must be
                          # recorded and reported, not abort the run

cd "$(dirname "$0")/.."

IMAGE="${AOW_GATE_IMAGE:-aow/tests:local-gates}"

names=()
statuses=()

record() {
  names+=("$1")
  statuses+=("$2")
  if [ "$2" -eq 0 ]; then
    printf '\033[32mPASS\033[0m %s\n' "$1"
  else
    printf '\033[31mFAIL\033[0m %s (exit %s)\n' "$1" "$2"
  fi
}

# One image, built once, holding the same pinned ruff and pytest CI uses --
# so a version skew between a developer's machine and the runner cannot be
# what decides the result.
printf 'Building the gate image (%s)...\n' "$IMAGE"
if ! docker build -q -f tests/Dockerfile -t "$IMAGE" . >/dev/null; then
  echo "could not build the gate image; nothing was checked" >&2
  exit 1
fi

echo
docker run --rm --network none "$IMAGE" ruff check services tests scripts
record "ruff check" $?

docker run --rm --network none "$IMAGE" ruff format --check services tests scripts
record "ruff format --check" $?

# --network none for the same reason CI uses it: a unit test that quietly
# reaches the network is a test that will not survive the air-gapped runtime.
docker run --rm --network none "$IMAGE"
record "unit tests" $?

# Run on the host, not in the gate image, because CI runs it on the runner
# against the full checkout. The image carries the services and the tests, not
# README.md, so running it inside would fail on a missing file rather than on
# a stale count -- a gate that fails for the wrong reason teaches people to
# ignore it.
python3 scripts/snapshot_manifest.py --check
record "snapshot manifest" $?

echo
failed=0
for i in "${!names[@]}"; do
  [ "${statuses[$i]}" -eq 0 ] || failed=$((failed + 1))
done

if [ "$failed" -eq 0 ]; then
  printf '\033[32mAll %s gates passed.\033[0m Safe to push.\n' "${#names[@]}"
else
  printf '\033[31m%s of %s gates failed.\033[0m Fix them before pushing:\n' \
    "$failed" "${#names[@]}"
  for i in "${!names[@]}"; do
    [ "${statuses[$i]}" -eq 0 ] || printf '   - %s\n' "${names[$i]}"
  done
fi
exit "$failed"
