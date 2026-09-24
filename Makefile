# Convenience only. Every target is a docker compose line you can type by hand,
# and the README shows the raw commands too -- nothing here is required to run
# the project, and there is no host-side Python anywhere.

COMPOSE        ?= docker compose
CONNECTED      := -f compose.yml -f compose.connected.yml
DEMO           := -f compose.yml -f compose.demo.yml
TOOLS          := -f compose.tools.yml

.PHONY: help stage stage-fetch stage-build up up-demo down logs ps test demo \
        offline no-data-loss update reenrich questions refresh refresh-check \
        snapshot samples redrive dlq clean

help:
	@echo "Staging (needs the internet, once):"
	@echo "  make stage          pull the pinned images, stage the model, build the services"
	@echo ""
	@echo "Running (no internet needed):"
	@echo "  make up             start everything (7 verified events, no generated rows)"
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
	@echo "  make demo           all of the above, in order"
	@echo ""
	@echo "Connected maintenance:"
	@echo "  make refresh        re-fetch the forecast through a temporary egress window"
	@echo "  make refresh-check  prove that window opens and closes (no internet needed)"
	@echo "  make snapshot       rebuild data/snapshot/ from source"
	@echo "  make samples        regenerate the labelled sample events (no network)"
	@echo ""
	@echo "Operations:"
	@echo "  make dlq            list what is quarantined"
	@echo "  make redrive        move dead letters back onto the exchange"

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

# ---------------------------------------------------------------- running --
up:
	$(COMPOSE) up -d
	@echo ""
	@echo "UI  http://localhost:8080"
	@echo "API http://localhost:8000/docs"
	@echo "The llm container stays unhealthy for ~3 minutes while it loads the model."

# Demo mode. Adds data/snapshot/events.samples.jsonl -- 45 generated rows,
# every one is_sample and titled "Sample: ..." -- so the planner and the agent
# can be shown outside London, where the only seven verified events are.
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

# ------------------------------------------------------------------ demos --
# A convenience for hosts that have bash. The portable form -- what the README
# documents, and what a Windows reviewer runs -- is
#   docker compose -f compose.tools.yml run --rm demos <name>
# and it executes these same scripts from this same working tree.
offline:       ; bash demos/01_offline.sh
no-data-loss:  ; bash demos/02_no_data_loss.sh
update:        ; bash demos/03_update.sh
reenrich:      ; bash demos/04_reenrich.sh
questions:     ; bash demos/05_questions.sh

demo: offline questions no-data-loss update reenrich

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

# -------------------------------------------------------------- operations --
dlq:
	$(COMPOSE) exec consumer python -m services.tools.redrive --list

redrive:
	$(COMPOSE) exec consumer python -m services.tools.redrive
