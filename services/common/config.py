"""Everything the services read from the environment, in one place.

No credential has a usable default: if it is missing the service fails at
import time with a clear message rather than half-starting.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path(os.environ.get("AOW_DATA_DIR", "/app/data"))
SNAPSHOT_DIR = DATA_DIR / "snapshot"

# ------------------------------------------------------------------ queue --
EXCHANGE = "aow.events"
QUEUE = "aow.ingest"
DLX = "aow.dlx"
DLQ = "aow.dlq"

# Every routing key the system uses. The consumer refuses anything else, so a
# typo becomes a dead letter rather than a silently dropped record.
RK_WEATHER = "weather.daily"
RK_PLACE = "place.record"
RK_FACT = "fact.record"
RK_EVENT = "event.record"
RK_RECOMMENDATION_REQUEST = "recommendation.request"
RK_LLM_RECOMMENDATION = "llm.recommendation"
RK_ITINERARY = "itinerary.record"
RK_PATCH = "record.patch"
# M12, the third update path: ask the local model to re-word stored
# recommendations. Like every other write it travels the queue, so the
# consumer stays the only role that touches the database.
RK_REENRICH = "recommendation.reenrich"

ROUTING_KEYS = frozenset(
    {
        RK_WEATHER,
        RK_PLACE,
        RK_FACT,
        RK_EVENT,
        RK_RECOMMENDATION_REQUEST,
        RK_LLM_RECOMMENDATION,
        RK_ITINERARY,
        RK_PATCH,
        RK_REENRICH,
    }
)

SCHEMA_VERSION = 1

# ------------------------------------------------------------ demo mode --
# Generated sample events (data/events.samples.jsonl) exist so the planner and
# the agent can be exercised on dates the verified seed does not reach: every
# real row in data/events.seed.jsonl was read off a venue's own listing page by
# hand, so it covers the days around the snapshot rather than a whole season.
# They are off by default: a default run stores the verified rows and nothing
# else.
#
# `docker compose -f compose.yml -f compose.demo.yml up -d` (make demo) turns
# them on. The flag is read by the ingestor, which decides whether the sample
# file is replayed at all, and by the consumer, which enforces it at the only
# place that writes to the database -- so turning demo mode back off removes
# the sample rows rather than leaving them behind.
DEMO_EVENTS = os.environ.get("AOW_DEMO_EVENTS", "0").strip().lower() in {"1", "true", "yes", "on"}

# --------------------------------------------------- event freshness --
# How long a checked event listing stays trustworthy.
#
# Every row in data/events.seed.jsonl was read off a venue's or an organiser's
# own listing page by hand, and it records the moment that happened in
# `checked_at`. What it cannot record is what the venue did afterwards: a
# concert is cancelled, a fixture is moved, a run adds a matinee. An air-gapped
# run has no way to find out, so the only honest thing it can do is say how old
# its reading is and stop presenting it as current once it is too old.
#
# `valid_until` is therefore `checked_at + AOW_EVENT_RECHECK_DAYS`, computed
# once by services.ingestor.fetch_content.load_events and carried on the row so
# that the database, the API, the agent and the UI all read the same instant
# rather than each recomputing it from a window that might differ between
# containers.
#
# 21 days is chosen to match the shape of the rest of the snapshot rather than
# to be generous: the weather snapshot is a 16-day forecast, and a system that
# already answers "no data for that date" outside that window should not be
# answering "the Laver Cup is on" from a listing nobody has looked at since.
EVENT_RECHECK_DAYS = int(os.environ.get("AOW_EVENT_RECHECK_DAYS", "21"))


def event_valid_until(checked_at: datetime) -> datetime:
    """When a reading of an event listing stops counting as current.

    The whole freshness policy, in one line, in the module that already owns
    the window. Both producers call it -- the ingestor when it loads the seed,
    and the consumer when an operator patches a row's `checked_at` after
    re-opening its listing page -- so a re-checked row and a freshly ingested
    one expire by the same rule rather than by two implementations of it.
    """
    return checked_at + timedelta(days=EVENT_RECHECK_DAYS)


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set; copy .env.example to .env and fill it in")
    return value


def rabbit_url() -> str:
    user = os.environ.get("RABBITMQ_USER", "aow")
    password = _require("RABBITMQ_PASSWORD")
    host = os.environ.get("RABBITMQ_HOST", "rabbitmq")
    port = os.environ.get("RABBITMQ_PORT", "5672")
    return f"amqp://{user}:{password}@{host}:{port}/%2F"


def _dsn(user_env: str, user_default: str, password_env: str) -> str:
    user = os.environ.get(user_env, user_default)
    password = _require(password_env)
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "aow")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def writer_dsn() -> str:
    """The consumer. The only role in the system with write grants."""
    return _dsn("POSTGRES_WRITER_USER", "aow_writer", "POSTGRES_WRITER_PASSWORD")


def reader_dsn() -> str:
    """The api, the agent and the enricher poll. SELECT only."""
    return _dsn("POSTGRES_READER_USER", "aow_reader", "POSTGRES_READER_PASSWORD")


def owner_dsn() -> str:
    """Migrations only."""
    return _dsn("POSTGRES_USER", "aow", "POSTGRES_PASSWORD")


# -------------------------------------------------------------------- llm --
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://llm:8080")
LLM_MODEL = os.environ.get("LLM_MODEL", "Qwen3-1.7B-Q4_K_M")
# Generous on purpose: llama.cpp runs with only --parallel 2 slots, so an interactive
# request can queue behind an enrichment batch. Timing out at that point would
# report a model outage that is really just a busy single slot.
LLM_TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "180"))

# ----------------------------------------------------------------- outbox --
OUTBOX_PATH = Path(os.environ.get("AOW_OUTBOX_PATH", "/outbox/outbox.sqlite3"))

# --------------------------------------------------- operator refresh state --
# Where scripts/refresh.sh files the report of its last run. One small JSON
# file on a named volume: written inside the ingestor, which is the container
# the refresh already runs commands in, and mounted read-only into the api so
# the UI can show what the last run actually did.
#
# Not the database and not the queue, and the reason is the case that matters:
# a refresh whose provider refused every city accepts nothing, so there is no
# message for the queue to carry and no row for the consumer to write. The one
# outcome an operator most needs to see is exactly the one the normal path
# cannot report. See services/common/refresh_state.py.
REFRESH_STATE_PATH = Path(os.environ.get("AOW_REFRESH_STATE_PATH", "/refresh/last-run.json"))

# ------------------------------------------------------------- enrichment --
ENRICH_POLL_SECONDS = float(os.environ.get("ENRICH_POLL_SECONDS", "60"))
ENRICH_BATCH = int(os.environ.get("ENRICH_BATCH", "8"))
# Applies ONLY to output the model got wrong (schema-invalid or empty). A
# connection error or a timeout is a temporary outage and consumes no attempt,
# so a long `llm` outage can never strand a row permanently.
ENRICH_MAX_INVALID_ATTEMPTS = int(os.environ.get("ENRICH_MAX_INVALID_ATTEMPTS", "5"))
# How many of a city-day's activities the local model is asked to word. The
# rule engine scores all 18 -- that is the product, and it is free. Wording all
# of them is ~1,300 CPU LLM calls per refresh through one llama.cpp slot, so
# the consumer ranks each day and defers the rest. Raise it if you have a GPU.
ENRICH_TOP_N = int(os.environ.get("ENRICH_TOP_N", "6"))
