#!/usr/bin/env bash
# Run from the unpacked release on an offline Docker host. Existing named
# volumes are preserved, so the previous release folder can still serve after an
# image rollback -- but the folder is not the rollback unit for the database.
# The pre-upgrade dump this script takes below is: an image rollback cannot undo
# a migration, and only the dump can.
#
# Compose fixes the project name (`name: aow` in compose.yml), so every release
# folder installs over the same volumes -- that is what makes an upgrade an
# upgrade. Set COMPOSE_PROJECT_NAME to install a second, independent copy; the
# release test in the README does exactly that.
set -euo pipefail

cd "$(dirname "$0")/.."

# ---------------------------------------------------------- prerequisites --
# Everything that decides whether this host can run the release at all, before
# the multi-gigabyte `docker load` below rather than after it. The old order put
# the Compose render last, so a password missing from .env cost a full load
# first and reported nothing until then.
arch="$(docker info --format '{{.Architecture}}')"
[[ "$arch" == amd64 || "$arch" == x86_64 ]] || { echo "this release requires a Linux/amd64 Docker engine" >&2; exit 1; }
# The architecture is what the engine reports; these are what the scripts in
# this folder actually need, and they are not the same question. Docker Desktop
# on an Intel Mac reports x86_64 and passes the line above, then fails several
# steps later on a missing sha256sum -- so name the real requirement here.
# bash 4.4 is the floor because verify-bundle-images.sh uses `declare -A` and
# `mapfile`, and because an empty array under `set -u` is an error before 4.4.
command -v sha256sum >/dev/null || { echo "sha256sum is required (GNU coreutils); this release installs on a Linux host" >&2; exit 1; }
command -v tar >/dev/null || { echo "tar is required" >&2; exit 1; }
(( BASH_VERSINFO[0] > 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] >= 4) )) \
  || { echo "bash 4.4 or newer is required; this shell is ${BASH_VERSION}" >&2; exit 1; }
docker compose version >/dev/null 2>&1 \
  || { echo "the Docker Compose v2 plugin is required: 'docker compose version' does not work here" >&2; exit 1; }
test -f .env || { echo "copy .env.example to .env and set passwords first" >&2; exit 1; }
# The overlay is what points Compose at this bundle's images instead of the
# aow/*:dev tags compose.yml names. Without it every service resolves an image
# this host does not have, and Compose answers by pulling or building.
test -f compose.bundle.yml || { echo "compose.bundle.yml is missing: this is not a complete release folder" >&2; exit 1; }

# Everything the bundle claims about itself, checked before anything on this
# host changes: SHA256SUMS over every file, no unlisted file, models.lock,
# images.bundle.lock against the CI manifest and the committed IMAGES.lock,
# and images.tar against images.bundle.lock by verified manifest digest.
bash scripts/verify-bundle.sh .
if [ -f FAULT-INJECTION.json ]; then
  # Said again here, after verification and before anything on this host
  # changes. An operator who scrolled past the banner during verify is about to
  # migrate a database with a migration written to fail.
  echo "refusing to install a fault-injection test artifact without AOW_ALLOW_FAULT_INJECTION=1" >&2
  echo "  This folder is a deliberately broken TEST artifact (FAULT-INJECTION.json)." >&2
  echo "  Set that variable only on a disposable host used for the rollback drill." >&2
  test -n "${AOW_ALLOW_FAULT_INJECTION:-}" || exit 1
  echo "AOW_ALLOW_FAULT_INJECTION is set: installing a fault-injection artifact deliberately"
fi
# `tr -d` rather than `cat`: a folder that reached this host through anything
# that rewrites line endings would otherwise fail the next line with "invalid
# release version" and give no hint why.
export AOW_IMAGE_VERSION="$(tr -d '\r\n' < release-version.txt)"
[[ "$AOW_IMAGE_VERSION" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid release version" >&2; exit 1; }

dc() { docker compose -f compose.yml -f compose.bundle.yml --env-file .env "$@"; }

# Renders the Compose files and fails by name on anything .env is still missing.
# It reads files and asks the daemon nothing, so running it here keeps the
# verify-before-anything-changes order intact while still failing on a bad .env
# before the dump and the load rather than after them.
dc config --quiet

# ------------------------------------------------- is this an upgrade at all --
# An upgrade, not a first install: this host already holds a database. Take a
# dump before the new images touch the schema, because an image rollback alone
# cannot undo a migration. Restore it with scripts/restore-offline.sh.
#
# The volume is the question, not the container. Asking `docker ps` alone meant
# that an operator who stopped the previous release before upgrading -- the
# natural thing to do, and what `make down` does -- got no dump at all, and the
# migration then became irreversible without anything saying so.
project="${COMPOSE_PROJECT_NAME:-aow}"
running_pg="$(docker ps -q \
  --filter "label=com.docker.compose.project=$project" \
  --filter "label=com.docker.compose.service=postgres")"
pgdata="$(docker volume ls -q --filter "name=^${project}_pgdata$")"

if [ -n "$running_pg" ]; then
  # Postgres fixes the superuser password inside pgdata when the volume is
  # initialised, and nothing here ever updates it: 001_init.sql re-applies the
  # writer and reader passwords on every boot, but not this one. So a new
  # release folder with freshly generated passwords authenticates as the wrong
  # user against the old volume, and `migrate` exits 2 with "password
  # authentication failed for user aow" -- after this script has loaded the
  # whole archive. Verified on Docker 29.8 against a stopped-and-restarted
  # stack whose .env had only POSTGRES_PASSWORD changed.
  #
  # Only the superuser triple matters, which is why it is the only one checked.
  for key in POSTGRES_USER POSTGRES_DB POSTGRES_PASSWORD; do
    live="$(docker exec "$running_pg" printenv "$key" 2>/dev/null || true)"
    # Last assignment wins, as Compose reads it. compose.yml defaults the user
    # and the database to `aow`; the password has no default and the render
    # above already refused an empty one.
    new="$(awk -F= -v key="$key" '$1 == key { sub(/^[^=]*=/, ""); value = $0 } END { print value }' .env)"
    case "$key" in POSTGRES_USER | POSTGRES_DB) new="${new:-aow}" ;; esac
    if [ -n "$live" ] && [ "$live" != "$new" ]; then
      echo "$key in .env does not match the database this host is already running" >&2
      echo "  Reuse the previous release's .env verbatim for an upgrade. Postgres keeps" >&2
      echo "  the superuser credentials inside the pgdata volume, and no migration" >&2
      echo "  changes them, so a freshly generated value cannot authenticate against an" >&2
      echo "  existing database -- migrate would fail after the images were loaded." >&2
      echo "  (The writer and reader passwords are re-applied on every boot; these three" >&2
      echo "  are not.)" >&2
      exit 1
    fi
  done

  mkdir -p backup
  dump="backup/$project-$(date -u +%Y%m%dT%H%M%SZ).sql"
  echo "upgrading a running installation: dumping its database to $dump"
  docker exec "$running_pg" sh -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' \
    > "$dump"
  # pg_dump streams, so "more than zero bytes" says nothing about whether it
  # finished. Its own closing marker does, and this file is the only thing
  # standing between a failed upgrade and a schema that cannot be rolled back.
  if ! tail -n5 "$dump" | grep -q 'PostgreSQL database dump complete'; then
    echo "the pre-upgrade dump is empty or did not finish (no pg_dump completion marker); refusing to continue" >&2
    exit 1
  fi
elif [ -n "$pgdata" ]; then
  if [ -z "${AOW_SKIP_PREUPGRADE_DUMP:-}" ]; then
    echo "this host already has the database volume $pgdata, but its postgres is not running" >&2
    echo "  A dump cannot be taken from a stopped database, and the migrations this" >&2
    echo "  install runs cannot be undone by rolling the images back. Start the" >&2
    echo "  previous release (docker compose -f compose.yml -f compose.bundle.yml up -d" >&2
    echo "  postgres, from that release folder) and run this again." >&2
    echo "  To install over it with no rollback point, set AOW_SKIP_PREUPGRADE_DUMP=1." >&2
    exit 1
  fi
  echo "AOW_SKIP_PREUPGRADE_DUMP is set: installing over the existing database $pgdata with NO pre-upgrade dump, deliberately"
fi

# Which of this release's images the engine already holds, recorded before
# anything is loaded. `docker load` cannot answer that afterwards: it prints
# "Loaded image" whether it unpacked the archive's bytes or found the content
# already in the store, which is exactly how the empty-archive bundle of
# docs/RELEASE-PROOF.md section 1 installed perfectly on the machine that
# packaged it and nowhere else. Every release drill before that one ran on that
# machine, so none of them could have caught it even in principle.
#
# This counts tags, not content: an engine that pulled the same layers but never
# tagged them reads as clean here. It is a floor under the claim, not a proof of
# it -- what proves the archive is self-contained is verify-bundle.sh above,
# which reads the archive's own bytes and never asks the daemon anything.

# Which engine this was, and what it held. These two lines record one thing:
# whether this engine had ever seen this release before it was loaded. That is
# the empty-image-store question, and it is the one no check inside the bundle
# can answer, because the bundle cannot see the machine. It was previously
# available only from the clean-engine job in CI, which left an operator on a
# real host copying it into a checklist by hand.
#
# It is not a network claim, and nothing in this script makes one. Three
# separate things get confused here, so keep them apart:
#   - the archive is self-contained        -- verify-bundle-images.sh, from bytes
#   - this engine had never held these images before the load -- the lines below
#   - the host has no route out            -- not established anywhere in this
#     repository; a separate engine, a VM or a CI runner is not a physical air
#     gap, and must never be recorded as one.
echo "engine: $(docker info --format '{{.ID}}') docker $(docker version --format '{{.Server.Version}}') on $(docker info --format '{{.OperatingSystem}}') $arch"
echo "store before load: $(docker image ls -qa | grep -c . || true) images, $(docker volume ls -q | grep -c . || true) volumes, $(docker ps -aq | grep -c . || true) containers"

already=()
while read -r alias _; do
  test -n "$alias" || continue
  # </dev/null so nothing in the loop body can eat the lock file this loop is
  # reading; a half-consumed census would silently under-report.
  if docker image inspect "aow-bundle/$alias:$AOW_IMAGE_VERSION" >/dev/null 2>&1 </dev/null; then
    already+=("$alias")
  fi
done < images.bundle.lock

if [ -n "${AOW_REQUIRE_CLEAN_IMAGE_STORE:-}" ] && [ "${#already[@]}" -gt 0 ]; then
  echo "AOW_REQUIRE_CLEAN_IMAGE_STORE is set, and these release tags already exist: ${already[*]}" >&2
  exit 1
fi

# Informational, with no threshold: how much room `docker load` needs depends on
# the engine's storage driver and on what it already holds, so a number here
# would be a guess. Printed so that a "no space left on device" below has the
# before picture next to it in the same transcript.
echo "disk: $(df -h . | awk 'NR == 2 {print $4}') free at $(pwd), docker root $(docker info --format '{{.DockerRootDir}}')"

# `docker load` exits 0 even when it could not unpack an image. A layer blob
# whose bytes do not match the digest its manifest names is refused by the
# content store, the tag is still created, and the only sign is a line in the
# output -- verified against Docker 29.8, which printed "Loaded image: ..."
# and "Error unpacking image ... content digest ... not found" and then exited
# 0. So the output is the check, not the exit status.
load_log="$(mktemp)"
trap 'rm -f "$load_log"' EXIT
docker load -i images.tar 2>&1 | tee "$load_log"
if grep -qi 'error' "$load_log"; then
  echo "docker load reported an error; the release has not been started" >&2
  exit 1
fi
expected_images="$(grep -c . images.bundle.lock)"
loaded_images="$(grep -c '^Loaded image' "$load_log" || true)"
if [ "$loaded_images" != "$expected_images" ]; then
  echo "docker load reported $loaded_images images, the release ships $expected_images" >&2
  exit 1
fi

# Say which install this was. An install onto an engine that already held these
# images is a legitimate re-install or upgrade, so it is reported rather than
# refused -- but it is not evidence that images.tar can install anywhere else,
# and the log has to stop reading as though it were.
if [ "${#already[@]}" -eq 0 ]; then
  echo "image store: this engine held none of the $expected_images release tags before the load; archive verification found their config and layers in images.tar"
else
  echo "image store: ${#already[@]} of $expected_images release tags were already in this engine before the load: ${already[*]}"
  echo "  A load that finds content already in the store succeeds even from an incomplete archive, so this install does not show that images.tar is self-contained. scripts/verify-bundle.sh checks that from the archive's bytes; only an install on an engine that has never held these images demonstrates it."
fi

if [ -n "$running_pg" ]; then
  # Stop the previous release's readers before migrate runs. Every boot
  # re-applies the migrations, and 006, 007 and 008 each take an ACCESS
  # EXCLUSIVE lock -- ALTER TABLE acquires it before discovering the change is
  # already made. A single in-flight SELECT from the old api, agent or enricher
  # makes migrate queue behind it, and a queued exclusive request blocks every
  # reader after it. Nothing sets lock_timeout, and `up -d` waits on migrate
  # completing, so that stalls the whole install. scripts/restore-offline.sh
  # stops the same set for the same reason.
  echo "stopping the previous release's readers before the migrations run"
  dc stop ingestor enricher api agent ui
fi
dc up -d --no-build --pull never

# `docker compose exec` fails outright if the container is still transitioning:
# `up -d` waits on the depends_on conditions, not on api's own process. Three
# cheap attempts to get an exec channel, then the smoke test runs once -- it has
# its own eight-minute retry loop for everything after that. Deliberately not
# `up -d --wait`, which treats the one-shot `migrate` service, which exits 0 by
# design, as a service that failed to come up.
for attempt in 1 2 3; do
  if dc exec -T api python -c '' >/dev/null 2>&1; then
    break
  fi
  test "$attempt" -lt 3 || { echo "the api container never became ready to run the smoke test" >&2; exit 1; }
  echo "api is not ready yet (attempt $attempt of 3); waiting"
  sleep 10
done
dc exec -T api python - < scripts/release-smoke.py
dc exec -T ui python - < scripts/release-ui-smoke.py

# The bind address, resolved from the same two places Compose reads it and in
# the same order: the shell environment first, then --env-file. compose.yml
# publishes on ${AOW_BIND_ADDR:-127.0.0.1}:8080 and :8000, and
# compose.bundle.yml overrides only the images, never edge's ports -- so this
# .env is what decides the boundary. Printing a constant 127.0.0.1 would be this
# script asserting a security property it never read, over an API whose write
# routes have no authentication. scripts/bootstrap.sh resolves it identically,
# for the same reason; the two must not disagree about this.
bind="${AOW_BIND_ADDR:-}"
if [ -z "$bind" ]; then
  bind="$(sed -n 's/^[[:space:]]*AOW_BIND_ADDR=\([^[:space:]#]*\).*/\1/p' .env | tail -n1)"
fi
bind="${bind:-127.0.0.1}"

# What was actually proved: the smoke test runs inside the api container, so it
# checked the API, the stored forecast and the scores from inside the stack,
# along with the agent, the model, the UI and edge answering their own health
# endpoints. It did not reach a published host port, so the addresses below are
# stated as configuration rather than as a result.
echo "Release $AOW_IMAGE_VERSION is up: the API is serving stored forecasts and scores from inside the stack"
echo "Published on $bind:8080 (UI) and $bind:8000 (API)"
case "$bind" in
  127.* | ::1) ;;
  *) echo "WARNING: $bind is not loopback, so the write API -- which has no authentication of its own -- is reachable from every machine that can route here. See the Security section of the README." ;;
esac
