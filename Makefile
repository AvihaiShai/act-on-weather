# Convenience only. Every target is a docker command you can type by hand, and
# the README shows the raw commands too -- nothing here is required to run the
# project, and no target needs host-side Python, bash or curl. `make` itself is
# the only thing this file adds to the prerequisites.

COMPOSE        ?= docker compose
CONNECTED      := -f compose.yml -f compose.connected.yml
DEMO           := -f compose.yml -f compose.demo.yml
TOOLS          := -f compose.tools.yml
PROBE          := -p aow-f3 -f compose.yml -f compose.model-probe.yml
# Pinned in IMAGES.lock like every other image, and checked against it in CI.
PYIMAGE        := python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

.PHONY: help stage stage-fetch stage-build preflight up up-demo down logs ps test demo \
        grounding offline no-data-loss update reenrich questions refresh \
        refresh-check snapshot samples manifest redrive dlq clean \
        monitor monitor-down backup restore backup-restore

help:
	@echo "Staging (needs the internet, once):"
	@echo "  make stage          pull the pinned images, stage the model, build the services"
	@echo "  make preflight      check .env and the staged model, without starting anything"
	@echo ""
	@echo "Running (no internet needed):"
	@echo "  make up             start everything (39 verified events, no generated rows)"
	@echo "  make up-demo        same, plus 45 labelled sample events in all five cities"
	@echo "  make ps / logs      status / follow the logs"
	@echo "  make down           stop"
	@echo ""
	@echo "Proofs:"
	@echo "  make test           unit tests, in a container, no network"
	@echo "  make offline        M6  -- air-gapped operation"
	@echo "  make no-data-loss   M11 -- consumer, database, broker and poison-message drills"
	@echo "  make update         M12 -- an edit through the queue, with history"
	@echo "  make reenrich       the local model is not on the critical path"
	@echo "  make questions      M7/M8 -- agent breadth, including what it refuses"
	@echo "  make grounding      M7 -- adversarial grounding against the local model"
	@echo "  make backup-restore B3  -- back up, destroy the volumes, restore, verify"
	@echo "  make demo           all of the above, in order"
	@echo ""
	@echo "Connected maintenance:"
	@echo "  make refresh        re-fetch the forecast through a temporary egress window"
	@echo "                      (reports the run at GET /refresh/last)"
	@echo "  make refresh-check  prove that window opens and closes (no internet needed)"
	@echo "  make snapshot       rebuild data/snapshot/ from source"
	@echo "  make samples        regenerate the labelled sample events (no network)"
	@echo "  make manifest       re-derive the snapshot counts the docs quote"
	@echo ""
	@echo "Operations:"
	@echo "  make dlq            list what is quarantined"
	@echo "  make redrive        move dead letters back onto the exchange"
	@echo "  make monitor        start Prometheus and Grafana (optional overlay)"
	@echo "  make backup         back up Postgres, the outboxes and the broker topology"
	@echo "  make restore DIR=backups/<id>  restore one into an isolated project"

# ---------------------------------------------------------------- staging --
stage: stage-fetch stage-build
	@echo ""
	@echo "Staged. From here the stack needs no internet: make up"

# These are the README's four staging commands, verbatim, so `make stage` and
# the documented Docker-only path produce the same staged machine. The model
# download and its checksum live in the `stage` container (compose.tools.yml),
# not here: one implementation, and `make` itself needs no curl and no
# sha256sum.
stage-fetch:
	@echo "Pulling the pinned images (~2.2 GB)..."
	$(COMPOSE) pull --quiet postgres rabbitmq llm edge
	@echo "Staging the model (~1.2 GB) and verifying it against models.lock..."
	$(COMPOSE) $(TOOLS) run --rm stage

# The demos image is built here, while we are still connected, for the same
# reason the services are: the proofs have to run after the network goes away.
stage-build:
	$(COMPOSE) build
	$(COMPOSE) $(TOOLS) build demos

# Everything a fresh clone gets wrong, checked before anything is started.
# Both steps are read-only: `config` renders the Compose files and fails by
# name on a password still missing from .env, and the `stage` container
# re-hashes a model that is already there rather than fetching it, so this
# needs no network.
preflight:
	$(COMPOSE) config --quiet
	@echo ".env is present and complete."
	$(COMPOSE) $(TOOLS) run --rm stage
	@echo ""
	@echo "Ready. Start it with: make up"

# ---------------------------------------------------------------- running --
up:
	$(COMPOSE) up -d
	@echo ""
	@echo "UI  http://localhost:8080"
	@echo "API http://localhost:8000/docs"
	@echo "The llm container stays unhealthy for ~3 minutes while it loads the model."

# Demo mode. Adds data/snapshot/events.samples.jsonl -- 45 generated rows,
# every one is_sample and titled "Sample: ..." -- so the planner and the agent
# can be shown on days the 39 verified events do not cover.
# Going back to "make up" restarts the consumer, which deletes them.
up-demo:
	$(COMPOSE) $(DEMO) up -d
	@echo ""
	@echo "UI  http://localhost:8080  -- the header carries a demo-mode banner."
	@echo "Back to verified-only data: make up"

down:
	$(COMPOSE) down

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f --tail 50

clean:
	$(COMPOSE) down -v
	@echo "Volumes removed. The next 'make up' starts from the committed snapshot."

# ------------------------------------------------------------------ tests --
test:
	docker build -q -f tests/Dockerfile -t aow/tests:dev .
	docker run --rm aow/tests:dev

# The grounding gate (F3). Its own Compose project, so it never touches a
# running stack: it starts a second llm on an isolated network, replays the
# questions that produced ungrounded answers, and fails if anything the agent
# would deliver is not supported by the rows it retrieved. Kept out of CI
# because CI has no model and no network to fetch one; run it before a release.
grounding:
	docker build -q -f tests/Dockerfile -t aow/tests:probe .
	$(COMPOSE) $(PROBE) up -d --no-build --pull never llm
	@$(COMPOSE) $(PROBE) run --rm --no-deps probe; status=$$?; \
	  $(COMPOSE) $(PROBE) down; exit $$status

# ------------------------------------------------------------------ demos --
# Each of these is the proof runner, spelled exactly as the README documents
# it. They used to be `bash demos/NN_*.sh`, which quietly added bash, curl and
# python3 to the host dependencies of a project whose stated prerequisite is
# Docker. The container bind-mounts this working tree, so it runs demos/*.sh
# from the checkout and not from an image layer -- and on a host that does have
# bash, curl and python3, `bash demos/01_offline.sh` is the same run without
# the wrapper.
offline:       ; $(COMPOSE) $(TOOLS) run --rm demos offline
no-data-loss:  ; $(COMPOSE) $(TOOLS) run --rm demos no-data-loss
update:        ; $(COMPOSE) $(TOOLS) run --rm demos update
reenrich:      ; $(COMPOSE) $(TOOLS) run --rm demos reenrich
questions:     ; $(COMPOSE) $(TOOLS) run --rm demos questions
# Deliberately not part of `make demo`. Every proof above runs against the
# stack that is already up; this one builds an isolated project of its own
# and destroys its volumes, which takes about two minutes and would be a
# surprising thing for `make demo` to do to a reviewer.
backup-restore: ; $(COMPOSE) $(TOOLS) run --rm demos backup-restore

# One container. demos/run.sh keeps the order -- prove the system works and
# answers before breaking it, so a failure in a drill is unambiguous.
demo: ; $(COMPOSE) $(TOOLS) run --rm demos all

# ------------------------------------------------------ connected updates --
# The operator refresh. One command, because the dangerous part of a refresh is
# not the fetch -- it is the step afterwards that puts the ingestor back on the
# internal network, and a step an operator has to remember is a step that gets
# skipped. scripts/refresh.sh closes that window from a trap and asserts it
# closed, so an interrupt or a failed fetch cannot leave a route out.
#
# The portable form -- what the README documents, and what a Windows reviewer
# runs -- is the compose line below, executing this same script from this same
# working tree.
#
# Both targets act on the `aow` project. Set AOW_PROJECT to point them at another
# stack; that is how the drills run the shipped command against an isolated
# project instead of editing compose.tools.yml.
refresh:
	$(COMPOSE) $(TOOLS) run --rm refresh

# Opens and closes the egress window without fetching anything: the drill for
# "does this always put the ingestor back?". Needs no internet.
refresh-check:
	$(COMPOSE) $(TOOLS) run --rm refresh --check

snapshot:
	$(COMPOSE) $(CONNECTED) run --rm --no-deps ingestor \
	  python -m services.ingestor.fetch_content
	@echo "data/snapshot/ rebuilt -- review the diff and commit it."

# The labelled sample events. Needs no network: it reads the places snapshot
# and writes data/events.samples.jsonl, which `make snapshot` then copies to
# data/snapshot/events.samples.jsonl -- a file of its own, never merged with
# the hand-verified data/snapshot/events.jsonl, so that replaying it stays a
# decision (make up-demo) rather than a side effect of having a snapshot.
# Every row it writes is is_sample=true and titled "Sample: ...". See
# services/ingestor/make_samples.py for why they exist at all.
samples:
	$(COMPOSE) run --rm --no-deps ingestor \
	  python -m services.ingestor.make_samples
	@echo "data/events.samples.jsonl rebuilt -- run 'make snapshot' to fold it in."

# The numbers the README and this file quote about the snapshot, re-derived
# from the snapshot itself. Run it after `make snapshot`; CI fails the build if
# the committed manifest, or any count in the documentation, has drifted from
# the data. In the pinned Python image, so this stays a Docker-only repository.
manifest:
	docker run --rm -v "$(CURDIR):/work" -w /work $(PYIMAGE) \
	  python scripts/snapshot_manifest.py

# -------------------------------------------------------------- operations --
dlq:
	$(COMPOSE) exec consumer python -m services.tools.redrive --list

redrive:
	$(COMPOSE) exec consumer python -m services.tools.redrive

# ------------------------------------------------------------ monitoring --
# Opt-in, and that is the whole point of it being an overlay. Prometheus and
# Grafana are pinned in IMAGES.lock and travel in the offline bundle, but
# nothing starts them unless this file is passed, so a reviewer who wants only
# the application never pays for them.
#
# Grafana is the only thing published, on loopback like everything else.
# Prometheus stays on the internal network and is reached through Grafana.
#
# The installer exports AOW_IMAGE_VERSION only for its own process. A later
# `make monitor` reads the release's version file itself, so it still selects
# the bundle aliases from a fresh shell. A developer checkout has no version
# file and uses the registry-pinned observability overlay directly.
ifeq ($(strip $(AOW_IMAGE_VERSION)),)
ifneq ($(wildcard release-version.txt),)
AOW_IMAGE_VERSION := $(shell cat release-version.txt)
endif
endif
export AOW_IMAGE_VERSION
OBS_BUNDLE = $(if $(AOW_IMAGE_VERSION),-f compose.bundle.yml -f compose.observability.bundle.yml,)
OBS_OFFLINE_ARGS = $(if $(AOW_IMAGE_VERSION),--no-build --pull never,)

monitor:
	$(COMPOSE) -f compose.yml -f compose.observability.yml $(OBS_BUNDLE) up -d $(OBS_OFFLINE_ARGS)
	@echo ""
	@echo "Grafana http://127.0.0.1:3000 -- admin / GRAFANA_ADMIN_PASSWORD from .env"

monitor-down:
	$(COMPOSE) -f compose.yml -f compose.observability.yml $(OBS_BUNDLE) stop \
	  prometheus grafana edge-observability

# ------------------------------------------------------- backup / restore --
# Operational recovery of live state. Distinct from the release installer's
# rollback, which restores a previous *release*; this restores the data a
# running stack accepted. See docs/RUNBOOK-BACKUP-RESTORE.md.
backup:
	bash scripts/backup-state.sh

# make restore DIR=backups/2026-09-24T15-44-00Z
restore:
	bash scripts/restore-state.sh $(DIR)
