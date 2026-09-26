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
#   3  --refresh did not complete (the stack is up either way)
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
# `|| true` so a missing or unreadable Makefile reaches the named check in
# step 1 rather than killing the script here, before `die` is even defined,
# with awk's own error and no "next:" line.
PYIMAGE="$(awk '$1 == "PYIMAGE" { print $3 }' Makefile 2>/dev/null || true)"

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
                  files, that every image the stack needs is already local,
                  and that the staged model matches models.lock. A missing
                  proof-runner image is a warning, not a failure: the app does
                  not need it to run. It does not use a network.
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
# --no-start's early exit lives inside the same block --wait-only skips, so
# given both flags the script silently ignored --no-start and polled for the
# full timeout. The pair is contradictory; say so rather than picking one.
if [ "$WAIT_ONLY" -eq 1 ] && [ "$START" -eq 0 ]; then
  echo "--wait-only polls a stack that is already up and --no-start stops before starting one; pick one" >&2
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

# Whether a service that has no healthcheck can yet be called up.
#
# These two functions set globals rather than echoing, because they memoise and
# a `$(...)` call would run them in a subshell and throw every update away --
# which is exactly the bug the first version of this had: the baseline was
# never retained, so every poll looked like the first one and a healthy stack
# waited out the full timeout.
#
# RESTART_COUNT: how many times Docker has restarted a service's container.
# Anything unreadable answers 0, so a service is never held back by a failure
# to measure it. The two `|| true`s are what make that true and are not
# decoration: `set -e` is in force, this function is called as a plain
# statement inside the poll loop rather than in a condition, and `pipefail`
# makes the whole substitution non-zero when docker exits non-zero. Without
# them the script died at the assignment, before the fallback below could run
# -- with no "bootstrap failed:" line, no "next:" line and a bare exit 1,
# which is exactly the failure this file exists to prevent. `2>/dev/null`
# hides the message but not the status. The case that matters is a cached
# container id that Docker has since replaced, because the id is looked up
# once and kept for the whole wait.
# The container id is looked up once per service and kept: the
# loop polls every 5s for up to 15 minutes and `compose ps -q` is not free. The
# memo is a flat string rather than an associative array, because `declare -A`
# needs bash 4 and macOS still ships bash 3.2.
_CID_CACHE=' '
RESTART_COUNT=0
read_restart_count() {
  _rc_svc="$1"
  _rc_cid="${_CID_CACHE##* $_rc_svc=}"
  _rc_cid="${_rc_cid%% *}"
  if [ -z "$_rc_cid" ]; then
    _rc_cid="$(dc ps -q "$_rc_svc" 2>/dev/null | tr -d '\r' | head -n1 || true)"
    if [ -z "$_rc_cid" ]; then RESTART_COUNT=0; return; fi
    _CID_CACHE="$_CID_CACHE$_rc_svc=$_rc_cid "
  fi
  _rc_count="$(docker inspect -f '{{.RestartCount}}' "$_rc_cid" 2>/dev/null | tr -d '\r' || true)"
  case "$_rc_count" in '' | *[!0-9]*) RESTART_COUNT=0 ;; *) RESTART_COUNT="$_rc_count" ;; esac
}

# STABILITY: '' when the service can be called up, otherwise the word to show.
#
# The absolute restart count is the wrong test, and it is an easy mistake to
# make: the counter is cumulative for the life of the container, so a container
# that ever restarted would be reported unhealthy by every later run of this
# script, forever, until something recreated it. That is not hypothetical --
# the no-data-loss drill stops and starts `consumer` four times by design and
# leaves it at RestartCount=6 while perfectly healthy. Gating on "count > 0"
# would make bootstrap hang on any machine where the proofs had been run, which
# is worse than the defect it fixes.
#
# What is meaningful is a restart *while we are watching*, so the first sample
# only records a baseline and reports `settling`. That costs a healthy stack
# one extra poll interval, and it is the whole reason this can tell a crash
# loop from a container that simply restarted an hour ago. The baseline is
# never moved afterwards, so a service that restarts once stays flagged for the
# rest of the wait instead of being declared settled between two restarts.
#
# Known limit, stated rather than hidden: Docker's restart backoff grows to
# 60s, so a loop slower than the remaining wait can still go unseen. This
# catches the fast loop a misconfigured broker or database produces, which is
# the case that was reporting success.
_RESTART_BASE=' '
STABILITY=''
read_stability() {
  _rg_svc="$1"
  read_restart_count "$_rg_svc"
  _rg_base="${_RESTART_BASE##* $_rg_svc=}"
  _rg_base="${_rg_base%% *}"
  if [ -z "$_rg_base" ]; then
    _RESTART_BASE="$_RESTART_BASE$_rg_svc=$RESTART_COUNT "
    STABILITY=settling
  elif [ "$RESTART_COUNT" -gt "$_rg_base" ]; then
    STABILITY=restarting
  else
    STABILITY=''
  fi
}

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
  # stdout is layer noise and is dropped; stderr is the diagnosis and is not.
  # It used to be discarded along with it, so a blocked registry, an expired
  # login and a rate limit all arrived as the same "not available" further down,
  # with nothing in the scrollback to act on.
  docker pull --quiet "$PYIMAGE" >/dev/null
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
pass "Compose plugin v${compose_version#v}"

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
  # for the same reason: the host's df is not the number that matters for the
  # images and the builds. It is not the whole requirement either -- the model
  # is a bind mount, so its 1.2 GB lands on the drive holding this checkout and
  # is measured by nothing here. The warning below says so rather than billing
  # it against a number that does not cover it.
  if pyimage_ready; then
    # `|| echo 0` for the same reason as the MemTotal line above: this step is
    # advisory, so a daemon that refuses `--network none`, or an image that
    # will not run here, must warn rather than abort the whole bootstrap with
    # a raw docker error and no "next:" line.
    disk_bytes="$(docker run --rm --network none "$PYIMAGE" \
      python -c 'import shutil; print(shutil.disk_usage(".").free)' 2>/dev/null | tr -d '\r' || echo 0)"
    case "$disk_bytes" in '' | *[!0-9]*) disk_bytes=0 ;; esac
    if [ "$disk_bytes" -eq 0 ]; then
      warn "could not read Docker's free disk; the README asks for about 6 GB."
    elif [ "$disk_bytes" -lt "$MIN_DISK_BYTES" ]; then
      warn "Docker has $(gib "$disk_bytes") free; staging needs about 6 GB."
      note "~2.2 GB of pulled images, ~1.7 GB built here and ~0.4 GB of tooling"
      note "bases, all on Docker's own filesystem, which is what the number above"
      note "measures. The 1.2 GB model is not: it is written to ./models on the"
      note "drive holding this checkout, and nothing here measures that one."
      note "Staging fails part-way through a pull or a build when it runs out."
      note "'docker system prune' reclaims space."
    else
      pass "Docker free disk: $(gib "$disk_bytes") on Docker's own filesystem"
    fi
  else
    # Not "and could not be pulled": in --offline mode no pull was attempted,
    # and the same sentence has to be true in both modes.
    warn "free disk not measured: the pinned helper image is not on this host."
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

  # `-f` below and not `-e`: a *directory* called .env passed the existence
  # test, made `grep` answer "Is a directory" on stderr, and the `|| true` that
  # tolerates grep's empty-match status turned that into a count of zero and a
  # PASS. The real failure then arrived much later as a raw Compose error.
  if [ -e "$ENV_FILE" ] && [ ! -f "$ENV_FILE" ]; then
    die "$ENV_FILE exists but is not a regular file, so it cannot be read as an env file." \
        "remove or rename it, then rerun this script"
  fi

  if [ -f "$ENV_FILE" ]; then
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
    # Two causes, two messages. Only one of them is --offline's doing, and
    # telling somebody who never passed --offline to "rerun without --offline"
    # sends them looking for a flag they did not use while the real cause --
    # a registry they cannot reach -- goes unnamed.
    if ! pyimage_ready; then
      if [ "$SKIP_STAGE" -eq 1 ]; then
        die "no $ENV_FILE, and --offline may not pull the pinned helper image that generates the passwords." \
            "rerun without --offline on a network, or copy $TEMPLATE to $ENV_FILE by hand and replace every change-me with a different password"
      fi
      die "no $ENV_FILE, and the pinned helper image that generates the passwords could not be pulled (the error above says why)." \
          "fix the registry access and rerun, or copy $TEMPLATE to $ENV_FILE by hand and replace every change-me with a different password"
    fi

    # Write beside the target and move it into place, so an interrupted run
    # cannot leave a half-written .env that `up` would read.
    #
    # Created 0600 before a single password is written into it. The redirect
    # below would otherwise create it under the ambient umask -- 0644 on most
    # hosts -- and it would stay that way for the whole `docker run`, which is
    # a second or more on a cold image. Tightening it afterwards closes the
    # window between the chmod and the move; it does not close that one, and
    # this does.
    #
    # On a filesystem that honours the mode. Where the bits are emulated they
    # are not honoured at all -- measured in Git Bash on NTFS, both the umask
    # here and the chmod below are no-ops and the file inherits the directory's
    # ACL. Nothing in a shell script changes that, so the mode is checked after
    # the move and reported rather than promised.
    tmp="$ENV_FILE.bootstrap.$$"
    trap 'rm -f "$tmp"' EXIT
    (umask 077; : >"$tmp") || die "could not create a temporary file beside $ENV_FILE." \
                                  "check that the directory holding $ENV_FILE is writable, then rerun this script"
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
    # chmod before the mv, not after: tightening the mode once the file is
    # already at its final name leaves the passwords group- and world-readable
    # for the width of the window between the two calls. The `umask 077` above
    # is what covers the longer window, the one the generator runs inside; this
    # is the belt to that pair of braces, and it also covers a host where the
    # subshell's umask did not take.
    chmod 600 "$tmp" 2>/dev/null || true
    mv "$tmp" "$ENV_FILE"
    trap - EXIT
    pass "created $ENV_FILE from $TEMPLATE with $generated generated password(s)"
    note "Each one is distinct and random, and none of them was printed. .env"
    note "is gitignored, so this file is the only copy of them."
    # `ls -l` and not `stat`, whose flags differ between GNU and BSD; the first
    # ten characters are the mode, and cutting there also drops the `+` or `.`
    # that an ACL or SELinux label adds after it.
    case "$(ls -l "$ENV_FILE" 2>/dev/null | cut -c1-10)" in
      -rw-------) note "Mode 0600: no other account on this host can read it." ;;
      *)
        warn "$ENV_FILE is not mode 0600 on this filesystem."
        note "Normal on Windows/NTFS, where the mode bits are emulated and"
        note "chmod does not apply -- the file inherits the folder's ACL. The"
        note "passwords are in it, so protect the folder if the host is shared."
        ;;
    esac
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
    # Two lists, not one union, because they have different consequences. The
    # compose.yml images are what `up` needs: without one of them there is no
    # stack, so a miss is fatal. The compose.tools.yml images only stage the
    # model and run the proofs; `aow/demos:dev` in particular is built by the
    # fourth line of README step 2, which reads optional. Treating it as fatal
    # refused to start a perfectly startable stack and told the reviewer to
    # "rerun on a machine with a network" -- advice that is both unnecessary
    # and, for someone who is already air-gapped, impossible to follow.
    # Each list is captured separately so that a failure of either is caught:
    # in a `{ a; b; }` group the exit status is b's alone, so a's failure was
    # swallowed and the check silently ran against half the images.
    #
    # Every `config --images` failure below names two causes, because it has
    # two: the flag is a later addition than the top-level `name:` key the
    # version gate enforces, so an older-but-still-v2 plugin fails here with
    # nothing whatever wrong in $ENV_FILE.
    runtime_images="$(dc config --images | tr -d '\r')" \
      || die "could not list the images the stack needs: either $ENV_FILE does not render the Compose files, or this Compose plugin is too old for 'config --images'." \
             "fix $ENV_FILE, or check that 'docker compose config --images' runs at all, and rerun"
    test -n "$runtime_images" || die "Compose listed no images to check." "check the Compose files"
    while IFS= read -r ref; do
      [ -n "$ref" ] || continue
      have_image "$ref" || die "$ref is not staged on this machine." \
                              "rerun without --offline on a machine with a network"
    done <<< "$runtime_images"
    pass "every image the stack needs is already local"

    # The stage service's own image is required after all: the model check
    # immediately below runs inside it, and without it that check would fail
    # with "the model is not staged", which would be the wrong diagnosis.
    stage_image="$(dc -f compose.tools.yml config --images stage | tr -d '\r' | head -n1)" \
      || die "could not read the stage image out of compose.tools.yml: either $ENV_FILE does not render it, or this Compose plugin is too old for 'config --images'." \
             "fix $ENV_FILE, or check that 'docker compose config --images' runs at all, and rerun"
    if [ -n "$stage_image" ] && ! have_image "$stage_image"; then
      die "$stage_image is not staged, so the model cannot be verified offline." \
          "rerun without --offline on a machine with a network"
    fi

    # Everything else in compose.tools.yml is the proof runner, which the app
    # does not need in order to run.
    # Captured into a variable with its own `|| die`, for the same reason the
    # two lists above are. A failing command substitution inside a `<<<`
    # redirection does not trip `set -e`: the loop simply reads nothing, runs
    # zero times, and the check below reports "the tooling images are here
    # too" on a machine where the list was never read. That is the one shape
    # of failure this whole step exists to catch, so it must not be the one
    # shape it cannot see.
    tools_images="$(dc -f compose.tools.yml config --images | tr -d '\r' | LC_ALL=C sort -u)" \
      || die "could not list the tooling images from compose.tools.yml: either $ENV_FILE does not render it, or this Compose plugin is too old for 'config --images'." \
             "fix $ENV_FILE, or check that 'docker compose config --images' runs at all, and rerun"
    missing_tools=''
    while IFS= read -r ref; do
      [ -n "$ref" ] || continue
      [ "$ref" = "$stage_image" ] && continue
      have_image "$ref" || missing_tools="$missing_tools $ref"
      # sort -u: two services share the demos image, and naming it twice in
      # the warning reads like two separate problems.
    done <<< "$tools_images"
    if [ -n "$missing_tools" ]; then
      warn "the stack can start, but these tooling images are missing:$missing_tools"
      note "They run the proofs; they are not needed to run the app. Build"
      note "them while connected with:"
      note "  docker compose -f compose.tools.yml build demos"
    else
      pass "the tooling images are here too"
    fi
    # The runtime override wins over compose.tools.yml's connected default.
    # If a model is absent, stage_model.py can only try a local file URL and
    # fail; it cannot fetch from the public registry or an internal mirror.
    #
    # Held rather than streamed, because the two ways this fails deserve
    # different treatment and only one of them is readable as it comes:
    #
    #   present but wrong -- stage_model.py prints the path, the expected hash
    #     and the actual one. That is a better message than anything this script
    #     could write, so it is passed straight through.
    #   absent -- the stager has nothing to verify and falls through to the
    #     fetch it was pinned away from, so it announces "fetching it from
    #     file:///dev/null/..." and ends in a NotADirectoryError traceback. Both
    #     are true; neither tells an air-gapped reader anything except that the
    #     model is not there. That is said here in one line instead.
    #
    # On success the output is printed unchanged: "already here, verifying it"
    # and the hash are the evidence an offline check exists to produce.
    if model_out="$(dc -f compose.tools.yml run --rm --pull never --no-deps \
        -e MODEL_BASE_URL=file:///dev/null stage 2>&1)"; then
      printf '%s\n' "$model_out"
      pass "the staged model matches models.lock"
    else
      case "$model_out" in
        *'does not match models.lock'*)
          printf '%s\n' "$model_out" >&2
          die "the staged model does not match models.lock." \
              "delete the file named above, then rerun without --offline, on a machine with a network" ;;
        *)
          die "the model is not staged on this machine." \
              "rerun without --offline, on a machine with a network" ;;
      esac
    fi
  else
    # The pull is the only step here that needs a network, so it is the only
    # one worth skipping when it has already happened. The model verify and
    # the builds are cheap no-ops on a staged machine.
    pinned="$(dc config --images postgres rabbitmq llm edge | tr -d '\r')" \
      || die "could not list the pinned images: either $ENV_FILE does not render the Compose files, or this Compose plugin is too old for 'config --images'." \
             "fix $ENV_FILE, or check that 'docker compose config --images' runs at all, and rerun"
    pulled=0
    for ref in $pinned; do
      have_image "$ref" || pulled=1
    done
    if [ "$pulled" -eq 0 ]; then
      note "the four pinned images are already on this host; skipping the pull."
    else
      note "pulling the four pinned images (~2.2 GB); this is the long one."
      note "Progress is printed per layer below. On a slow or throttled"
      note "registry this step can take tens of minutes; it was measured at"
      note "one point taking 53 kB/s from ghcr.io, so a stalled-looking pull"
      note "is usually just a slow one. Ctrl-C and rerun is safe -- finished"
      note "layers are cached and the pull resumes."
      # Deliberately not --quiet. This is the longest step in the script by a
      # wide margin and the only one that depends on somebody else's network.
      # With --quiet it printed nothing at all while it ran, so a reviewer had
      # no way to tell a slow pull from a hung one, and no reason to believe
      # waiting would help. Per-layer progress costs some scrollback and buys
      # the one thing this step needs: visible evidence that it is moving.
      dc pull postgres rabbitmq llm edge \
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

services="$(dc config --services | tr -d '\r')" \
  || die "cannot list the services; $ENV_FILE does not render the Compose files." "fix $ENV_FILE and rerun"

# Probe `ps --format` once, before the loop, instead of letting it fail
# silently inside it. A Compose that cannot render this Go template returns
# nothing, every service then reads "(no container)", and the script waits out
# the full timeout on a stack that is actually healthy -- a 15-minute silent
# hang whose failure message points at the wrong thing. The version gate above
# only enforces the major, which is the floor for the top-level `name:` key,
# not for `ps --format`, `config --images` or `up --pull`; a capability probe
# is the honest check because it tests what this script actually uses.
if ! dc ps --all --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}' >/dev/null 2>&1; then
  die "this Compose plugin (v${compose_version#v}) cannot render 'docker compose ps --format', which this script needs to tell a healthy service from a starting one." \
      "install a Compose plugin that supports it (v2.21 or newer), then check it with 'docker compose ps --format \"{{.Service}}|{{.State}}\"'"
fi

started_at="$(date +%s)"
deadline=$((started_at + TIMEOUT))
last_line=''
last_print=0
first_poll=1

while :; do
  # --all, because `migrate` runs once and exits 0 and would otherwise vanish
  # from the list the moment it succeeds.
  ps_out="$(dc ps --all --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}' 2>/dev/null | tr -d '\r' || true)"

  pending=''
  broken=''
  absent=0
  counted=0
  for svc in $services; do
    counted=$((counted + 1))
    line="$(printf '%s\n' "$ps_out" | grep -m1 "^$svc|" || true)"
    if [ -z "$line" ]; then
      pending="$pending $svc(no container)"
      absent=$((absent + 1))
      continue
    fi
    IFS='|' read -r _ state health code <<<"$line"
    case "$state" in
      running)
        case "$health" in
          healthy) ;;
          '')
            # No healthcheck: `running` is all Compose can tell us, and it is
            # not enough. ingestor, consumer, enricher and ui have none --
            # services/Dockerfile and services/ui/Dockerfile both declare no
            # HEALTHCHECK -- and all four carry `restart: unless-stopped`. `ui`
            # belongs on that list even though `edge` in front of it is probed,
            # because edge's /healthz is nginx answering for itself and says
            # nothing about whether Streamlit behind it is serving yet.
            #
            # A consumer that cannot reach the broker crash-loops with a backoff
            # that starts at 100ms, while this loop samples every 5s -- so one
            # sample landing between two restarts would report the whole stack
            # healthy and exit 0 on a database that nothing is writing to. Restarts happening *while we
            # wait* are the signal Compose does not surface in `ps`.
            read_stability "$svc"
            if [ -n "$STABILITY" ]; then
              pending="$pending $svc($STABILITY)"
            fi
            ;;
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

  # `--wait-only` on a stack that was never started is the one case where
  # waiting cannot possibly help. Every service reads "(no container)" and the
  # loop would sit out the whole deadline -- fifteen minutes by default --
  # before failing with a `next:` line that tells the reader to run
  # `docker compose logs` against containers that do not exist. The capability
  # probe above does not catch this: it checks that `ps --format` runs, and on
  # an empty project it runs perfectly and prints nothing.
  #
  # The confirming `ps --all -q` separates "nothing is running" from "that one
  # `ps` call hiccuped", because only the first is worth dying on. It costs one
  # extra call, once, on the poll that was going to fail anyway.
  if [ "$WAIT_ONLY" -eq 1 ] && [ "$first_poll" -eq 1 ] \
     && [ "$counted" -gt 0 ] && [ "$absent" -eq "$counted" ] \
     && [ -z "$(dc ps --all -q 2>/dev/null | tr -d '\r' || true)" ]; then
    if [ "$SKIP_STAGE" -eq 1 ]; then
      start_cmd="bash scripts/bootstrap.sh --offline"
    else
      start_cmd="bash scripts/bootstrap.sh"
    fi
    die "no container exists for any service in this Compose project, so there is nothing to wait for. --wait-only polls a stack that is already up; it does not start one." \
        "$start_cmd"
  fi
  first_poll=0

  if [ -z "$pending" ]; then
    elapsed=$(( $(date +%s) - started_at ))
    pass "every service is healthy (${elapsed}s)"
    break
  fi

  now="$(date +%s)"
  if [ "$now" -ge "$deadline" ]; then
    printf '\n'
    dc ps --all || true
    # The hint goes on its own line, because `next:` is an invitation to
    # copy-paste and must therefore be a command and nothing else. Welding
    # " -- or keep waiting with ..." onto the end produced a `next:` line that
    # fails with "no such service: or" when a reviewer does exactly that.
    printf 'nothing has failed yet; to keep waiting instead, run:\n  bash scripts/bootstrap.sh --wait-only --timeout 600\n' >&2
    die "still waiting after ${TIMEOUT}s for:$pending" \
        "docker compose logs --tail 50$(printf '%s' "$pending" | sed 's/([^)]*)//g')"
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
# It also has to read it from the same two places Compose does, in the same
# order Compose uses them: the shell environment first, then the env file.
# .env.example ships AOW_BIND_ADDR as a line to edit, so the file is the
# documented way to move the boundary -- and reading only the shell meant a
# stack that .env had put on 0.0.0.0 was reported as "published on 127.0.0.1
# only". That sentence is the one line in this report that describes a security
# property, over an API with write routes and no authentication, so it is the
# one line that must not be able to be wrong.
bind="${AOW_BIND_ADDR:-}"
if [ -z "$bind" ] && [ -f "$ENV_FILE" ]; then
  bind="$(sed -n 's/^[[:space:]]*AOW_BIND_ADDR=\([^[:space:]#]*\).*/\1/p' "$ENV_FILE" | tail -n1)"
fi
bind="${bind:-127.0.0.1}"

# A wildcard is what a socket binds, not somewhere a browser can go, so the URL
# says localhost while the warning below keeps the real value.
case "$bind" in
  127.0.0.1 | 0.0.0.0 | '::' | '::0' | '[::]') host=localhost ;;
  *) host="$bind" ;;
esac
note "UI   http://$host:8080"
# /openapi.json and not /docs: the schema is served by the stack and needs
# nothing else, while /docs is the one page here that is not self-contained --
# Swagger UI's bundle comes from a CDN, so on the air-gapped host this whole
# project is built for, that page renders empty.
note "API  http://$host:8000/openapi.json"
note "     /docs shows the same schema in a browser, but its Swagger UI bundle"
note "     is fetched from a CDN, so that page alone needs a network."
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
  exit 3
fi
