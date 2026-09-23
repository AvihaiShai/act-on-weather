# Convenience only. Every target is a docker compose line you can type by hand,
# and the README shows the raw commands too -- nothing here is required to run
# the project, and there is no host-side Python anywhere.

COMPOSE        ?= docker compose
CONNECTED      := -f compose.yml -f compose.connected.yml
MODEL_FILE     := models/Qwen3-1.7B-Q4_K_M.gguf
MODEL_URL      := https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q4_K_M.gguf

.PHONY: help stage stage-fetch stage-build up down logs ps test demo \
        offline no-data-loss update reenrich questions refresh snapshot \
        samples redrive dlq clean

help:
	@echo "Staging (needs the internet, once):"
	@echo "  make stage          pull the pinned images, download the model, build the services"
	@echo ""
	@echo "Running (no internet needed):"
	@echo "  make up             start everything"
	@echo "  make ps / logs      status / follow the logs"
	@echo "  make down           stop"
	@echo ""
	@echo "Proofs:"
	@echo "  make test           unit tests, in a container, no network"
	@echo "  make offline        M6  -- air-gapped operation"
	@echo "  make no-data-loss   M11 -- consumer, broker and poison-message drills"
	@echo "  make update         M12 -- an edit through the queue, with history"
	@echo "  make reenrich       the local model is not on the critical path"
	@echo "  make questions      M7/M8 -- agent breadth, including what it refuses"
	@echo "  make demo           all of the above, in order"
	@echo ""
	@echo "Connected maintenance:"
	@echo "  make refresh        re-fetch the forecast (extends the coverage window)"
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

stage-fetch:
	@echo "Pulling the pinned images (~2.2 GB)..."
	$(COMPOSE) pull --quiet postgres rabbitmq llm edge
	@echo "Fetching the model (~1.2 GB) if it is not already here..."
	@if [ -f $(MODEL_FILE) ]; then \
	  echo "  $(MODEL_FILE) present"; \
	else \
	  mkdir -p models && curl -fL --progress-bar -o $(MODEL_FILE) "$(MODEL_URL)"; \
	fi
	@echo "Verifying the model against models.lock..."
	@sha256sum -c models.lock

stage-build:
	$(COMPOSE) build

# ---------------------------------------------------------------- running --
up:
	$(COMPOSE) up -d
	@echo ""
	@echo "UI  http://localhost:8080"
	@echo "API http://localhost:8000/docs"
	@echo "The llm container stays unhealthy for ~3 minutes while it loads the model."

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
offline:       ; bash demos/01_offline.sh
no-data-loss:  ; bash demos/02_no_data_loss.sh
update:        ; bash demos/03_update.sh
reenrich:      ; bash demos/04_reenrich.sh
questions:     ; bash demos/05_questions.sh

demo: offline questions no-data-loss update reenrich

# ------------------------------------------------------ connected updates --
refresh:
	$(COMPOSE) $(CONNECTED) up -d ingestor
	$(COMPOSE) exec ingestor python -m services.ingestor.refresh
	@echo "Accepted. It publishes within a few seconds; check GET /coverage."

snapshot:
	$(COMPOSE) $(CONNECTED) run --rm --no-deps ingestor \
	  python -m services.ingestor.fetch_content
	@echo "data/snapshot/ rebuilt -- review the diff and commit it."

# The labelled sample events. Needs no network: it reads the places snapshot
# and writes data/events.samples.jsonl, which `make snapshot` then folds into
# data/snapshot/events.jsonl alongside the hand-verified rows. Every row it
# writes is is_sample=true and titled "Sample: ...". See
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
