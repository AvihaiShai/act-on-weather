"""Everything the services read from the environment, in one place.

No credential has a usable default: if it is missing the service fails at
import time with a clear message rather than half-starting.
"""

from __future__ import annotations

import os
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
    }
)

SCHEMA_VERSION = 1


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
# Generous on purpose: llama.cpp runs with --parallel 1, so an interactive
# request can queue behind an enrichment batch. Timing out at that point would
# report a model outage that is really just a busy single slot.
LLM_TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "180"))

# ----------------------------------------------------------------- outbox --
OUTBOX_PATH = Path(os.environ.get("AOW_OUTBOX_PATH", "/outbox/outbox.sqlite3"))

# ------------------------------------------------------------- enrichment --
ENRICH_POLL_SECONDS = float(os.environ.get("ENRICH_POLL_SECONDS", "60"))
ENRICH_BATCH = int(os.environ.get("ENRICH_BATCH", "8"))
# Applies ONLY to output the model got wrong (schema-invalid or empty). A
# connection error or a timeout is a temporary outage and consumes no attempt,
# so a long `llm` outage can never strand a row permanently.
ENRICH_MAX_INVALID_ATTEMPTS = int(os.environ.get("ENRICH_MAX_INVALID_ATTEMPTS", "5"))
