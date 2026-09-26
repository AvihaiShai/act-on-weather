#!/usr/bin/env bash
#
# First run, in one command: check that this host can run the stack, create
# .env with real passwords, stage everything, start it, and wait until it is
# actually serving.
#
#   bash scripts/bootstrap.sh
#
# It is a convenience, exactly like the Makefile. Every step below is a docker
# command the README already documents, in the same order and with the same
# flags; nothing here is required to run the project, and there is no step you
# could not type by hand. `bash` is the only thing it adds to the
# prerequisites -- on Windows that is the Git Bash that came with the `git`
# which fetched this folder.
#
# There is deliberately no PowerShell twin. A second implementation is a
# second thing to keep in step with the README, which is the mistake
# compose.tools.yml's `stage` service already removed once (it replaced the
# per-OS curl/Invoke-WebRequest + sha256sum/Get-FileHash recipes with one
# implementation). Nothing below needs a Windows-specific path: there are no
# bind mounts, no absolute container paths, no tty and no host tooling beyond
# docker itself, so MSYS path translation has nothing to translate.
#
# RERUNNING IT IS SAFE, and that is a requirement rather than a happy
# accident:
#   * an existing .env is never read for its values, never rewritten and never
#     replaced -- it is only checked for leftover `change-me` placeholders;
#   * the model is re-hashed, never re-downloaded (scripts/stage_model.py);
#   * the pull is skipped when the four pinned images are already here;
#   * no volume is ever removed. This script never runs `down`, with or
#     without -v.
#
# Exit codes:
#   0  the stack is up and healthy (or --no-start finished its work)
#   1  a step failed; the message says which one and what to run next
#   2  bad usage
set -euo pipefail

cd "$(dirname "$0")/.."

# Where the passwords live. Same knob as demos/lib.sh, so a reviewer can point
# every script in this repository at a different env file without editing one.
ENV_FILE="${AOW_ENV_FILE:-.env}"
TEMPLATE=".env.example"
TIMEOUT=900
SKIP_STAGE=0
START=1
WAIT_ONLY=0
REFRESH=0
# Set once the refresh has run, and read by the final report so it can never
# describe snapshot data as freshly fetched: not-attempted | ok | failed.
REFRESH_RESULT=not-attempted

# README: 8 GB for Docker (the caps in compose.yml total 6.7 GB) and about
# 6 GB of disk. Both are warnings, not gates: they are measured through
# Docker, and Docker Desktop's numbers are the VM's, not the host's.
MIN_MEM_BYTES=$((8 * 1024 * 1024 * 1024))
MIN_DISK_BYTES=$((6 * 1024 * 1024 * 1024))

# The pinned helper image, read out of the Makefile so this file does not
# become a third place the pin has to be updated. It is the same digest
# compose.tools.yml gives the `stage` service, so pulling it here is staging
# work rather than an extra download.
PYIMAGE="$(awk '$1 == "PYIMAGE" { print $3 }' Makefile)"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_BOLD=$'\033[1m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_RED=$'\033[31m'; C_OFF=$'\033[0m'
else
  C_BOLD=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_OFF=''
fi

step() { printf '\n%s== %s%s\n' "$C_BOLD" "$*" "$C_OFF"; }
note() { printf '   %s\n' "$*"; }
pass() { printf '%s   PASS%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn() { printf '%s   WARN%s %s\n' "$C_YELLOW" "$C_OFF" "$*"; }

# Every failure exits here, so every failure names the step, the cause and the
# next command. A bootstrap that prints a stack trace and stops has failed the
# person it exists for.
die() {
  printf '\n%sbootstrap failed: %s%s\n' "$C_RED" "$1" "$C_OFF" >&2
  if [ $# -gt 1 ]; then
    printf 'next: %s\n' "$2" >&2
  fi
  exit 1
}

usage() {
  cat <<'EOF'
First run, in one command.

  bash scripts/bootstrap.sh [options]
  make bootstrap [ARGS="--offline"]

What it does, in order:
  1. prerequisites -- docker on PATH, the daemon reachable, Compose v2
  2. resources     -- Docker's memory and free disk (warnings, not gates)
  3. configuration -- create .env from .env.example, with generated passwords
  4. staging       -- the README's step-2 commands: pull, stage the model, build
  5. start         -- docker compose up -d
  6. health        -- poll until every service is healthy, then print the URLs

With --refresh, one more:
  7. refresh       -- open the egress window and fetch a fresh forecast

What --refresh does and does not update:
  refreshed   the weather forecast, per city, through the outbox and the queue
  NOT         places, city facts and events. Those are the committed snapshot
              in data/snapshot/; the event set is a manually verified sample
              with a validity timer. Rebuilding them is `make snapshot`, a
              maintainer step that rewrites files in the repository and expects
              the diff to be reviewed. There is no marine data at all.
  An incomplete refresh is reported as a failure. Some cities may already
  have advanced; check the per-city result and each stored as-of stamp.

Options:
  --refresh       after the stack is healthy, fetch a fresh forecast. Needs a
                  network. Cannot be combined with --offline.
  --offline, --skip-stage
                  skip connected staging. Checks that .env renders the Compose
                  files, all images are local, and the staged model matches
                  models.lock. It does not use a network.
  --no-start      stop after step 4. Does not start anything and does not wait.
  --wait-only     skip to step 6 and poll a stack that is already up. Use it
                  when --timeout ran out and you want to keep waiting.
  --timeout N     seconds to wait in step 6 (default 900).
  -h, --help      this text.

Environment:
  AOW_ENV_FILE    env file to create and check (default .env). The same
                  variable demos/lib.sh reads.
  NO_COLOR        set it to anything to turn off the colour.

Safe to run twice: it never rewrites an existing .env, never re-downloads the
model and never removes a volume.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --refresh) REFRESH=1; shift ;;
    --skip-stage | --offline) SKIP_STAGE=1; shift ;;
    --no-start) START=0; shift ;;
    --wait-only) WAIT_ONLY=1; shift ;;
    --timeout)
      [ $# -ge 2 ] || { echo "--timeout takes a number of seconds" >&2; exit 2; }
      TIMEOUT="$2"; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; echo >&2; usage >&2; exit 2 ;;
  esac
done

case "$TIMEOUT" in
  '' | *[!0-9]*) echo "--timeout takes a number of seconds" >&2; exit 2 ;;
esac

# Caught here rather than fifteen minutes later, at the point where the fetch
# would fail for a reason the flags already made inevitable.
if [ "$REFRESH" -eq 1 ] && [ "$SKIP_STAGE" -eq 1 ]; then
  echo "--refresh needs a network and --offline promises none; pick one" >&2
  exit 2
fi
if [ "$REFRESH" -eq 1 ] && [ "$START" -eq 0 ]; then
  echo "--refresh acts on a running stack, so it cannot be used with --no-start" >&2
  exit 2
fi

# `docker compose` the way demos/lib.sh does it: with --env-file while the file
# exists, plain before it does, so step 1 and step 2 work on a fresh clone.
dc() {
  if [ -f "$ENV_FILE" ]; then
    docker compose --env-file "$ENV_FILE" "$@"
  else
    docker compose "$@"
  fi
}

have_image() { docker image inspect "$1" >/dev/null 2>&1; }

# Used for the disk measurement and for generating the passwords. Pulls only
# if the image is not already here and staging is allowed. Offline mode never
# touches a registry, even for an optional resource measurement.
pyimage_ready() {
  if have_image "$PYIMAGE"; then
    return 0
  fi
  if [ "$SKIP_STAGE" -eq 1 ]; then
    return 1
  fi
  note "fetching the pinned helper image (python:3.12-slim, ~130 MB)..."
  docker pull --quiet "$PYIMAGE" >/dev/null 2>&1
}

# Integer arithmetic only: no bc, no python on the host.
gib() { printf '%s.%s GiB' "$(( $1 / 1073741824 ))" "$(( $1 % 1073741824 * 10 / 1073741824 ))"; }

# ------------------------------------------------------------------------
# 1. Prerequisites
# ------------------------------------------------------------------------
step "1/6  Prerequisites"

command -v docker >/dev/null 2>&1 \
  || die "docker is not on PATH." \
         "install Docker Desktop (Windows/macOS) or Docker Engine with the Compose plugin (Linux), then rerun this script"

if ! docker info >/dev/null 2>&1; then
  die "the Docker CLI is installed but the daemon is not reachable." \
      "start Docker Desktop, or 'sudo systemctl start docker' on Linux, then rerun this script"
fi
pass "Docker engine $(docker version --format '{{.Server.Version}}') is reachable"

# Compose v2 as a subcommand. The standalone docker-compose v1 cannot read the
# top-level `name:` key every Compose file here uses, so it is refused by name
# rather than left to fail later with a YAML error.
if ! compose_version="$(docker compose version --short 2>/dev/null)"; then
  if command -v docker-compose >/dev/null 2>&1; then
    die "only the standalone docker-compose (v1) is installed. These Compose files use the top-level 'name:' key, which v1 cannot read." \
        "install the Compose v2 plugin (it ships with Docker Desktop; on Linux: the docker-compose-plugin package) and check it with 'docker compose version'"
  fi
  die "the 'docker compose' subcommand is missing: the Compose v2 plugin is not installed." \
      "install the Compose v2 plugin, then check it with 'docker compose version'"
fi
compose_major="${compose_version#v}"
compose_major="${compose_major%%.*}"
case "$compose_major" in
  '' | *[!0-9]*) die "cannot read a version number from 'docker compose version --short' ($compose_version)." \
                     "run 'docker compose version' and check the plugin installation" ;;
esac
if [ "$compose_major" -lt 2 ]; then
  die "Compose $compose_version is too old; v2 or newer is required for the top-level 'name:' key." \
      "upgrade the Compose plugin, then check it with 'docker compose version'"
fi
pass "Compose plugin v$compose_version"

case "$PYIMAGE" in
  python:*@sha256:*) : ;;
  *) die "could not read the pinned PYIMAGE out of the Makefile." \
         "check the PYIMAGE line in the Makefile; it must be python:<tag>@sha256:<digest>" ;;
esac

if [ "$WAIT_ONLY" -eq 1 ]; then
  note "--wait-only: skipping steps 2 to 5."
fi

# ------------------------------------------------------------------------
# 2. Resources
# ------------------------------------------------------------------------
if [ "$WAIT_ONLY" -eq 0 ]; then
  step "2/6  Resources"

  # From the daemon, not from the host: on Docker Desktop the host has plenty
  # of RAM and the VM is the thing that has to fit the containers.
  mem_bytes="$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)"
  case "$mem_bytes" in '' | *[!0-9]*) mem_bytes=0 ;; esac
  if [ "$mem_bytes" -eq 0 ]; then
    warn "could not read Docker's memory limit; the README asks for 8 GB."
  elif [ "$mem_bytes" -lt "$MIN_MEM_BYTES" ]; then
    warn "Docker has $(gib "$mem_bytes"); the README asks for 8 GB."
    note "compose.yml caps every container and the caps total 6.7 GB, 3 GB of"
    note "it the model server. Below that the llm container is the one that"
    note "breaks: it is OOM-killed while loading the model and restarts, so"
    note "the stack never turns healthy. On Docker Desktop raise it under"
    note "Settings -> Resources."
  else
    pass "Docker memory: $(gib "$mem_bytes")"
  fi

  # Free space on Docker's own filesystem, measured from inside a container
  # for the same reason: the host's df is not the number that matters.
  if pyimage_ready; then
    disk_bytes="$(docker run --rm --network none "$PYIMAGE" \
      python -c 'import shutil; print(shutil.disk_usage(".").free)' | tr -d '\r')"
    case "$disk_bytes" in '' | *[!0-9]*) disk_bytes=0 ;; esac
    if [ "$disk_bytes" -eq 0 ]; then
      warn "could not read Docker's free disk; the README asks for about 6 GB."
    elif [ "$disk_bytes" -lt "$MIN_DISK_BYTES" ]; then
      warn "Docker has $(gib "$disk_bytes") free; staging needs about 6 GB."
      note "~2.2 GB of pulled images, the 1.2 GB model, ~1.7 GB built here and"
      note "~0.4 GB of tooling bases. Staging fails part-way through a pull or"
      note "a build when it runs out. 'docker system prune' reclaims space."
    else
      pass "Docker free disk: $(gib "$disk_bytes")"
    fi
  else
    warn "free disk not measured: the pinned helper image is not on this host and could not be pulled."
    note "The README asks for about 6 GB free."
  fi
fi

# ------------------------------------------------------------------------
# 3. Configuration
# ------------------------------------------------------------------------
# The passwords are generated inside the pinned image and travel from its
# stdout straight into the file. They are never printed, never put in an
# argument (where `ps` would show them), never put in an environment variable
# and never held in a shell variable. The container also has no network.
GEN_ENV_PY='
import re, secrets, sys

generated = 0
out = []
for line in sys.stdin.read().splitlines():
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=change-me", line):
        out.append(line.split("=", 1)[0] + "=" + secrets.token_urlsafe(24))
        generated += 1
    else:
        out.append(line)
sys.stdout.write("\n".join(out) + "\n")
sys.stderr.write("%d\n" % generated)
'

if [ "$WAIT_ONLY" -eq 0 ]; then
  step "3/6  Configuration ($ENV_FILE)"

  if [ -e "$ENV_FILE" ]; then
    # Somebody's working configuration. Not read for its values, not rewritten,
    # not backed up -- only checked for the one thing that stops `up`.
    note "$ENV_FILE exists; keeping it exactly as it is. Nothing was generated."
    left="$(grep -c -E '^[A-Za-z_][A-Za-z0-9_]*=change-me[[:space:]]*$' "$ENV_FILE" || true)"
    case "$left" in '' | *[!0-9]*) left=0 ;; esac
    if [ "$left" -gt 0 ]; then
      die "$ENV_FILE still has $left placeholder password(s) left at 'change-me'." \
          "edit $ENV_FILE, give each of them a different value, and rerun this script -- or delete $ENV_FILE and let this script generate them"
    fi
    pass "$ENV_FILE has no change-me placeholders left"
  else
    test -f "$TEMPLATE" || die "$TEMPLATE is missing, so there is nothing to copy." \
                               "restore $TEMPLATE from git: 'git checkout -- $TEMPLATE'"
    pyimage_ready || die "no $ENV_FILE, and the pinned helper image that generates the passwords is not available in this mode." \
                         "rerun without --offline on a network, or copy $TEMPLATE to $ENV_FILE by hand and replace every change-me with a different password"

    # Write beside the target and move it into place, so an interrupted run
    # cannot leave a half-written .env that `up` would read.
    tmp="$ENV_FILE.bootstrap.$$"
    trap 'rm -f "$tmp"' EXIT
    if ! generated="$(docker run --rm -i --network none "$PYIMAGE" python -c "$GEN_ENV_PY" \
        < "$TEMPLATE" 2>&1 >"$tmp" | tail -n 1 | tr -d '\r')"; then
      die "generating $ENV_FILE failed." "run 'docker run --rm $PYIMAGE python -V' to check the helper image"
    fi
    case "$generated" in '' | *[!0-9]*) generated='some' ;; esac
    test -s "$tmp" || die "generating $ENV_FILE produced an empty file." "copy $TEMPLATE to $ENV_FILE by hand and set the passwords"
    if grep -qE '=change-me[[:space:]]*$' "$tmp"; then
      die "the generated file still contains a change-me placeholder." \
          "copy $TEMPLATE to $ENV_FILE by hand and set the passwords"
    fi
    # The template is settings as well as passwords; a renderer that dropped a
    # line would produce a file that fails much later and much less clearly.
    # `grep -c ''` and not `wc -l`, so a template without a trailing newline
    # does not read as one line short.
    if [ "$(grep -c '' "$tmp" || true)" -ne "$(grep -c '' "$TEMPLATE" || true)" ]; then
      die "the generated file has a different number of lines than $TEMPLATE." \
          "copy $TEMPLATE to $ENV_FILE by hand and set the passwords"
    fi
    if [ -e "$ENV_FILE" ]; then
      die "$ENV_FILE appeared while this script was generating one; refusing to overwrite it." \
          "check $ENV_FILE, then rerun this script"
    fi
    mv "$tmp" "$ENV_FILE"
    trap - EXIT
    chmod 600 "$ENV_FILE" 2>/dev/null || true
    pass "created $ENV_FILE from $TEMPLATE with $generated generated password(s)"
    note "Each one is distinct and random, and none of them was printed. .env"
    note "is gitignored, so this file is the only copy of them."
  fi
fi

# ------------------------------------------------------------------------
# 4. Staging
# ------------------------------------------------------------------------
if [ "$WAIT_ONLY" -eq 0 ]; then
  step "4/6  Staging"

  if [ "$SKIP_STAGE" -eq 1 ]; then
    # Confirm every required image is local before Compose has a chance to
    # pull or build. The stage image is included in the tools Compose file.
    note "--offline: not pulling, not building. Checking this machine is already staged."
    dc config --quiet || die "$ENV_FILE does not render the Compose files (the error above names the variable)." \
                             "fix $ENV_FILE and rerun"
    pass "$ENV_FILE renders every Compose file"
    staged_images="$(
      { dc config --images; dc -f compose.tools.yml config --images; } | LC_ALL=C sort -u
    )" || die "could not list the required images." "fix $ENV_FILE and rerun"
    test -n "$staged_images" || die "Compose listed no images to check." "check the Compose files"
    while IFS= read -r ref; do
      have_image "$ref" || die "$ref is not staged on this machine." \
                              "rerun without --offline on a machine with a network"
    done <<< "$staged_images"
    pass "every required image is already local"
    # The runtime override wins over compose.tools.yml's connected default.
    # If a model is absent, stage_model.py can only try a local file URL and
    # fail; it cannot fetch from the public registry or an internal mirror.
    dc -f compose.tools.yml run --rm --pull never --no-deps \
      -e MODEL_BASE_URL=file:///dev/null stage \
      || die "the model is not staged, or does not match models.lock." \
             "rerun without --offline, on a machine with a network"
    pass "the staged model matches models.lock"
  else
    # The pull is the only step here that needs a network, so it is the only
    # one worth skipping when it has already happened. The model verify and
    # the builds are cheap no-ops on a staged machine.
    pinned="$(dc config --images postgres rabbitmq llm edge)" \
      || die "$ENV_FILE does not render the Compose files (the error above names the variable)." \
             "fix $ENV_FILE and rerun"
    pulled=0
    for ref in $pinned; do
      have_image "$ref" || pulled=1
    done
    if [ "$pulled" -eq 0 ]; then
      note "the four pinned images are already on this host; skipping the pull."
    else
      note "pulling the four pinned images (~2.2 GB); this is the long one..."
      dc pull --quiet postgres rabbitmq llm edge \
        || die "could not pull the pinned images." \
               "check the network and rerun, or rerun with --offline on a machine that is already staged"
    fi
    pass "the four pinned upstream images are here"

    note "staging the model (~1.2 GB) and verifying it against models.lock..."
    dc -f compose.tools.yml run --rm stage \
      || die "staging the model failed." \
             "check the network, or set MODEL_BASE_URL in $ENV_FILE to an internal mirror, and rerun"
    pass "the model matches models.lock (an already-staged file is re-hashed, not re-fetched)"

    note "building the services, the UI and the proof runner..."
    dc build || die "building the service images failed." "read the build output above, fix it, and rerun"
    dc -f compose.tools.yml build demos \
      || die "building the demos image failed." "read the build output above, fix it, and rerun"
    pass "service, UI and demos images built"
  fi
fi

# ------------------------------------------------------------------------
# 5. Start
# ------------------------------------------------------------------------
if [ "$WAIT_ONLY" -eq 0 ]; then
  if [ "$START" -eq 0 ]; then
    step "Staged"
    note "--no-start: nothing was started."
    if [ "$SKIP_STAGE" -eq 1 ]; then
      note "Start it offline with: bash scripts/bootstrap.sh --offline"
    else
      note "Start it with: docker compose up -d"
    fi
    exit 0
  fi

  step "5/6  Start"
  if [ "$SKIP_STAGE" -eq 1 ]; then
    dc up -d --no-build --pull never \
      || die "starting the staged stack failed." "run 'docker compose logs --tail 50' to see why"
  else
    dc up -d || die "docker compose up -d failed." "run 'docker compose logs --tail 50' to see why"
  fi
  pass "Docker Compose started the stack"
fi

# ------------------------------------------------------------------------
# 6. Health
# ------------------------------------------------------------------------
step "6/6  Waiting for the stack (up to ${TIMEOUT}s)"
note "The llm container loads a 1.2 GB model on first start and reports"
note "unhealthy for about 3 minutes while it does. That is expected, and it is"
note "why the deadline here is minutes rather than seconds."

services="$(dc config --services)" \
  || die "cannot list the services; $ENV_FILE does not render the Compose files." "fix $ENV_FILE and rerun"

started_at="$(date +%s)"
deadline=$((started_at + TIMEOUT))
last_line=''
last_print=0

while :; do
  # --all, because `migrate` runs once and exits 0 and would otherwise vanish
  # from the list the moment it succeeds.
  ps_out="$(dc ps --all --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}' 2>/dev/null || true)"

  pending=''
  broken=''
  for svc in $services; do
    line="$(printf '%s\n' "$ps_out" | grep -m1 "^$svc|" || true)"
    if [ -z "$line" ]; then
      pending="$pending $svc(no container)"
      continue
    fi
    IFS='|' read -r _ state health code <<<"$line"
    case "$state" in
      running)
        # No healthcheck means "running is all we can know"; compose.yml gives
        # one to everything an operator would wait for.
        case "$health" in
          '' | healthy) ;;
          *) pending="$pending $svc($health)" ;;
        esac
        ;;
      exited)
        # Only migrate is a one-shot. A service that exits 0 is still gone and
        # must not make the stack appear healthy.
        if [ "$svc" != migrate ] || [ "$code" != "0" ]; then
          broken="$broken $svc(exit $code)"
        fi
        ;;
      *) pending="$pending $svc($state)" ;;
    esac
  done

  if [ -n "$broken" ]; then
    printf '\n'
    dc ps --all || true
    die "a container exited with a failure:$broken" \
        "docker compose logs --tail 50$(printf '%s' "$broken" | sed 's/([^)]*)//g')"
  fi

  if [ -z "$pending" ]; then
    elapsed=$(( $(date +%s) - started_at ))
    pass "every service is healthy (${elapsed}s)"
    break
  fi

  now="$(date +%s)"
  if [ "$now" -ge "$deadline" ]; then
    printf '\n'
    dc ps --all || true
    die "still waiting after ${TIMEOUT}s for:$pending" \
        "docker compose logs --tail 50$(printf '%s' "$pending" | sed 's/([^)]*)//g') -- or keep waiting with 'bash scripts/bootstrap.sh --wait-only --timeout 600'"
  fi

  # Print when something changes, and otherwise every 30s, so the output is a
  # progress log rather than either a wall of text or a frozen terminal.
  if [ "$pending" != "$last_line" ] || [ $((now - last_print)) -ge 30 ]; then
    note "$(( now - started_at ))s  waiting for:$pending"
    last_line="$pending"
    last_print="$now"
  fi
  sleep 5
done

# ------------------------------------------------------------------------
# 7. Refresh (only with --refresh)
# ------------------------------------------------------------------------
# Deliberately after the health gate: scripts/refresh.sh acts on a running
# stack, and a refresh against a half-started one fails in ways that look like
# a network fault.
#
# The exit codes are refresh.sh's own, and they are worth keeping apart --
# "the fetch failed" and "the egress window would not close" need different
# reactions from whoever is reading this.
if [ "$REFRESH" -eq 1 ]; then
  step "Refresh  Fetching a fresh forecast"
  note "This opens a bounded egress window, attaches only the ingestor to it,"
  note "fetches, and closes the window again. Places, facts and events are not"
  note "touched -- they are the committed snapshot."

  # AOW_PROJECT is what the refresh container uses to decide which stack to
  # act on, and it defaults to `aow` inside compose.tools.yml -- not to the
  # project this script is driving. So someone who isolates a second copy with
  # COMPOSE_PROJECT_NAME alone, which is the documented way to run one, would
  # have the fetch open an egress window on the *other* stack and refresh that
  # one instead. Default it here so the two cannot disagree.
  # Exported rather than prefixed onto the call, because `dc` is a shell
  # function and an assignment prefix on one of those does not behave the same
  # way it does on a command.
  export AOW_PROJECT="${AOW_PROJECT:-${COMPOSE_PROJECT_NAME:-aow}}"
  note "refreshing the '$AOW_PROJECT' project"

  refresh_rc=0
  dc -f compose.tools.yml run --rm refresh || refresh_rc=$?

  case "$refresh_rc" in
    0)
      REFRESH_RESULT=ok
      pass "forecast refreshed, egress window verified closed"
      ;;
    3)
      # The one outcome that is worse than a stale forecast.
      REFRESH_RESULT=failed
      printf '\n%s   EGRESS WINDOW STILL OPEN%s\n' "$C_RED" "$C_OFF"
      note "scripts/refresh.sh could not close the egress window (exit 3)."
      note "The ingestor may still reach the internet. Close it before using"
      note "this stack as an offline demonstration:"
      note "  docker network ls | grep aow-refresh"
      note "  docker network rm <that network>"
      ;;
    *)
      REFRESH_RESULT=failed
      warn "the refresh did not complete (exit $refresh_rc)"
      case "$refresh_rc" in
        1) note "the stack was not in a state where a refresh can run" ;;
        2) note "the fetch failed for at least one city; the egress window is closed" ;;
        4) note "accepted and queued, but nothing reached the database in time" ;;
        *) note "see the output above" ;;
      esac
      note "Check the per-city result above and GET /refresh/last. Some forecasts"
      note "may have advanced; each stored as-of stamp shows the current state."
      note "Retry with: docker compose -f compose.tools.yml run --rm refresh"
      ;;
  esac
fi

# ------------------------------------------------------------------------
# Report
# ------------------------------------------------------------------------
step "Ready"
# compose.yml publishes on ${AOW_BIND_ADDR:-127.0.0.1}, so the report has to
# read the same variable. It used to print localhost unconditionally, which is
# right on a default run and wrong on every isolated second copy -- and the
# isolated copy is exactly the case where someone is least able to guess the
# address they should be using.
bind="${AOW_BIND_ADDR:-127.0.0.1}"
if [ "$bind" = 127.0.0.1 ]; then
  host=localhost
else
  host="$bind"
fi
note "UI   http://$host:8080"
note "API  http://$host:8000/docs"
printf '\n'

# Said on every run, not only the interesting ones. A reviewer who cannot tell
# which rows were fetched today and which shipped with the clone cannot judge
# any answer the system gives, and the UI's as-of stamps are only meaningful
# next to a statement of what was supposed to have happened.
case "$REFRESH_RESULT" in
  ok)
    note "Data: the weather forecast was fetched just now. Places, city facts"
    note "and events are the committed snapshot -- they are never auto-fetched."
    ;;
  failed)
    printf '%s   Data: THE REFRESH DID NOT COMPLETE.%s Some cities may have\n' "$C_YELLOW" "$C_OFF"
    note "advanced while others retain older forecasts. Check /refresh/last;"
    note "each answer and chart carries the stored data's own as-of stamp."
    ;;
  *)
    note "Data: the committed snapshot, as cloned. Nothing was fetched -- rerun"
    note "with --refresh on a connected machine to update the forecast."
    ;;
esac
printf '\n'
case "$bind" in
  127.* | ::1) note "Both are published on $bind only." ;;
  *) note "Both are published on $bind; this can expose the unauthenticated API to other machines." ;;
esac
if [ "$bind" = 127.0.0.1 ]; then
  note "If your browser resolves localhost to ::1 and does not fall back, use"
  note "http://127.0.0.1:8080."
fi
note "Stop it with 'docker compose down'; that keeps the database and the queue."

# The stack is up either way, and the URLs above work either way -- but a run
# whose fetch failed did not do what it was asked to do, and a caller that only
# checks the exit status has to be able to tell. The report above says which.
if [ "$REFRESH_RESULT" = failed ]; then
  exit 2
fi
