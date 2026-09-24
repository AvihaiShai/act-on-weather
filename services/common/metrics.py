"""Everything the stack exports to Prometheus, in one file (B2).

The metrics are defined here rather than beside the code that moves them for
one reason: the alert rules and the Grafana dashboard are written against these
exact names and label sets, and a name that lives in five modules gets renamed
in four of them.

Two rules govern everything below.

*Labels come from bounded sets.* Prometheus keeps one time series per distinct
label combination, so a label whose values are not fixed by the code is a
memory leak with a scanner for a trigger. A URL path, a city, a message id or
an exception message are therefore never labels here; a service name, a matched
route template, a routing key we declared and an exception class name are.
Where a value arrives from outside -- the request method, the routing key on a
delivery -- it is checked against the allowed set and folded into a placeholder
if it is not in it.

*Instrumentation may degrade, never block.* A port already in use, a scrape
that raises, a database that is down: each of those is allowed to cost a
number, and none of them is allowed to stop a service from starting or a
message from being stored. Every callback below is wrapped accordingly, and a
collector that cannot read its source yields nothing rather than a zero -- a
gap on a graph is honest, a zero is a lie that clears an alert.

Both HTTP services run uvicorn single-process: the `command:` lines in
compose.yml carry no `--workers`. That is what makes prometheus_client's
default process-wide registry the right one -- the process that answers a
scrape of `/metrics` is the same process that counted the requests. Adding
`--workers` would silently break every counter here, because each worker would
answer some scrapes with its own partial numbers; it would require the
multiprocess collector and a shared directory instead.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    start_http_server,
)
from prometheus_client.core import GaugeMetricFamily

from . import config

log = logging.getLogger(__name__)

# The value every label that could otherwise be unbounded falls back to. One
# extra series in total, whatever an unrouted request or a stray routing key
# happens to say.
UNMATCHED = "<unmatched>"
OTHER = "<other>"

# The only methods that get their own series. h11 will happily hand us any
# token the client put on the request line, so this list -- not the request --
# decides how many series the counter can have.
HTTP_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
)

# Seconds. The low end is where a stored-data read belongs (every API read is a
# single indexed SELECT); the tail runs to a minute because an agent answer
# waits on a CPU llama.cpp call and a 30s p99 there is a fact worth seeing
# rather than an outlier to clip.
HTTP_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)

# Consumer work is a single Postgres transaction, so the interesting range is
# milliseconds; the long buckets exist to make a database that has gone slow
# visible as latency before it becomes a redelivery loop.
CONSUME_BUCKETS = (0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 10, 30)

# One enrichment is one CPU LLM call through a single llama.cpp slot, which is
# seconds to tens of seconds; the last bucket is the default LLM_TIMEOUT_S, so a
# call that ran into the timeout lands in it rather than beyond the graph.
ENRICH_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120, 180)


# ------------------------------------------------------------------- http ----

HTTP_REQUESTS = Counter(
    "aow_http_requests_total",
    "HTTP requests that produced a response, by matched route template.",
    ["service", "method", "route", "status"],
)
HTTP_DURATION = Histogram(
    "aow_http_request_duration_seconds",
    "Wall time from entering the middleware to the handler returning.",
    ["service", "method", "route"],
    buckets=HTTP_BUCKETS,
)
HTTP_EXCEPTIONS = Counter(
    "aow_http_exceptions_total",
    "Requests whose handler raised. `type` is the exception class name.",
    ["service", "method", "route", "type"],
)


def route_template(scope: dict[str, Any]) -> str:
    """The route's template, `/weather/{city}`, never the concrete path.

    This is the whole cardinality guarantee for the HTTP metrics. `scope["path"]`
    would give one series per city, per itinerary id and per junk URL a scanner
    tries; the matched route is one series per endpoint the code declares.
    Starlette puts the route object into the scope when it matches, and because
    it updates the same dict the middleware is holding, it is there by the time
    the call returns. A request that matched nothing has no route at all, which
    is exactly the case that must not be allowed to name its own series.
    """
    route = scope.get("route")
    # FastAPI's APIRoute carries `path_format`; a plain Starlette Mount or Route
    # only has `path`. Both are templates, neither contains user input.
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    return template or UNMATCHED


def _method(scope: dict[str, Any]) -> str:
    method = str(scope.get("method", "")).upper()
    return method if method in HTTP_METHODS else OTHER


class HttpMetrics:
    """Pure-ASGI middleware, so it can read the route the router matched.

    It has to run outside the router to time the whole request, and it has to
    read the scope *after* the call to learn which route was chosen -- which is
    why this is an ASGI class and not a `BaseHTTPMiddleware` subclass or a
    FastAPI dependency.
    """

    def __init__(self, app, service: str):
        self.app = app
        self.service = service

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # Lifespan and websocket traffic: nothing to count, and the scope
            # has no route.
            await self.app(scope, receive, send)
            return

        method = _method(scope)
        status: str | None = None
        started = time.perf_counter()

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = str(message["status"])
            await send(message)

        raised: str | None = None
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            # Only the class name. The message is attacker-influenced free text
            # and would be an unbounded label; the message belongs in the log,
            # which is where it already goes.
            raised = type(exc).__name__
            raise
        finally:
            route = route_template(scope)
            HTTP_DURATION.labels(service=self.service, method=method, route=route).observe(
                time.perf_counter() - started
            )
            if raised is not None:
                HTTP_EXCEPTIONS.labels(
                    service=self.service, method=method, route=route, type=raised
                ).inc()
            # An unhandled exception before response.start becomes a 500 in
            # Starlette's outer error middleware. Count that response here too:
            # otherwise the 5xx ratio omits exactly the failures it should show.
            # If streaming failed after response.start, keep the status that
            # was actually sent instead of inventing a second 500 response.
            observed_status = status or ("500" if raised is not None else None)
            if observed_status is not None:
                HTTP_REQUESTS.labels(
                    service=self.service, method=method, route=route, status=observed_status
                ).inc()
            # Neither branch: the client went away before a response started.
            # Counted as nothing rather than invented as a status, because a
            # placeholder status on a real endpoint reads like a real outcome.


def mount_metrics(app, service: str) -> None:
    """Give a FastAPI app the middleware and a `/metrics` endpoint.

    `/metrics` is deliberately an internal endpoint. The only published port is
    the edge proxy's, and the proxy refuses `/metrics`, so this is reachable
    from the `backend` network -- that is, from Prometheus -- and from nowhere
    else. Nothing in it is secret, but a request counter is a traffic log, and
    the rule for this stack is that nothing is exposed unless it has to be.
    """
    # Imported here rather than at module scope: the consumer, the ingestor and
    # the enricher import this module too, and the shared layer's
    # common/requirements.txt deliberately does not carry the web framework.
    from starlette.responses import Response

    app.add_middleware(HttpMetrics, service=service)

    # No return annotation on purpose: this module uses postponed annotations,
    # and FastAPI evaluates a route's annotations against the module globals,
    # where `Response` -- imported just above, inside this function -- does not
    # exist. Annotating it would fail at import, which is a strange enough trap
    # to be worth a line.
    @app.get("/metrics", include_in_schema=False)
    def metrics_endpoint():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ----------------------------------------------------------------- outbox ----

OUTBOX_PUBLISH_FAILURES = Counter(
    "aow_outbox_publish_failures_total",
    "Attempts to publish an accepted record that the broker did not confirm.",
    ["service"],
)
MESSAGES_PUBLISHED = Counter(
    "aow_messages_published_total",
    "Records confirmed by the broker. Counted after the confirm, never before.",
    ["service", "routing_key"],
)


def safe_routing_key(routing_key: str | None) -> str:
    """Routing keys are a closed set (config.ROUTING_KEYS) and the consumer
    dead-letters anything else. Folding an unknown one into a placeholder keeps
    that true for the metrics as well, so a misconfigured publisher cannot mint
    series."""
    return routing_key if routing_key in config.ROUTING_KEYS else UNMATCHED


def _age_seconds(accepted_at: str | None) -> float:
    if not accepted_at:
        return 0.0
    try:
        accepted = datetime.fromisoformat(accepted_at)
    except (TypeError, ValueError):
        return 0.0
    if accepted.tzinfo is None:
        accepted = accepted.replace(tzinfo=UTC)
    # Clamped at zero: a container whose clock stepped back must not report a
    # negative age, which would read as "nothing is pending".
    return max(0.0, (datetime.now(UTC) - accepted).total_seconds())


class OutboxCollector:
    """The producer-side outbox, read while Prometheus waits.

    A collector rather than gauges something sets on a timer, because a timer is
    one more thing that can stop: a wedged publisher loop would leave the last
    cheerful value frozen on the dashboard indefinitely. Here the numbers come
    out of SQLite at scrape time, so they are either current or absent.

    `aow_outbox_oldest_pending_age_seconds` is the one to alert on. Depth alone
    cannot tell a broker that is briefly slow from records that are stranded:
    two hundred pending rows that drain in seconds is a healthy ingest, while
    one pending row that has sat there for an hour is the M11 guarantee being
    tested for real.
    """

    def __init__(self, service: str, box, lock: threading.Lock | None = None):
        self.service = service
        self.box = box
        # The API accepts on request threads and publishes on a background one,
        # so its handle is already guarded and we take that same lock rather
        # than adding a second one. The ingestor and the enricher are single
        # loops with no lock, and are given their own read-only handle instead
        # (see register_outbox_file), so the scrape thread never touches the
        # connection the loop is writing on.
        self.lock: Any = lock if lock is not None else contextlib.nullcontext()

    def collect(self):
        entries = GaugeMetricFamily(
            "aow_outbox_entries",
            "Records in this producer's outbox, by whether the broker confirmed them.",
            labels=["service", "state"],
        )
        oldest = GaugeMetricFamily(
            "aow_outbox_oldest_pending_age_seconds",
            "Seconds since the oldest still-unpublished record was accepted; 0 when none is.",
            labels=["service"],
        )
        try:
            with self.lock:
                counts = self.box.counts()
                accepted_at = self.box.oldest_pending_accepted_at()
        except Exception as exc:  # noqa: BLE001 - a scrape must never raise
            # Yield nothing. A missing sample breaks the line on the graph and
            # fires the "no data" arm of the alert; a zero would say the outbox
            # is empty, which is the opposite of what a failing read means.
            log.warning("outbox metrics unavailable for %s: %s", self.service, exc)
            return
        pending = int(counts.get("pending") or 0)
        total = int(counts.get("total") or 0)
        entries.add_metric([self.service, "pending"], pending)
        entries.add_metric([self.service, "published"], max(0, total - pending))
        oldest.add_metric([self.service], _age_seconds(accepted_at))
        yield entries
        yield oldest


def register_outbox_metrics(
    service: str, box, *, lock: threading.Lock | None = None, registry=REGISTRY
) -> OutboxCollector:
    collector = OutboxCollector(service, box, lock)
    registry.register(collector)
    return collector


def register_outbox_file(service: str, path, *, registry=REGISTRY) -> OutboxCollector | None:
    """Export an outbox through a second, read-only handle of our own.

    For the ingestor and the enricher, which are single loops that hold their
    writable handle without a lock because nothing else touches it. A scrape
    arrives on the metrics server's thread, so it gets its own connection rather
    than a share of theirs; SQLite's WAL mode lets it read committed rows while
    the loop keeps writing.

    Failing to open it is logged and swallowed. Opening a read-only handle can
    fail -- a missing file, a volume permission -- and a service that refused to
    start because it could not measure itself would be instrumentation causing
    the outage it exists to reveal.
    """
    from .outbox import Outbox

    try:
        box = Outbox(path, readonly=True)
    except Exception as exc:  # noqa: BLE001 - never fatal
        log.warning("outbox metrics disabled for %s: cannot open %s (%s)", service, path, exc)
        return None
    return register_outbox_metrics(service, box, registry=registry)


# --------------------------------------------------------------- consumer ----

MESSAGES_CONSUMED = Counter(
    "aow_messages_consumed_total",
    "Deliveries that reached a terminal outcome: stored, duplicate or dead-lettered.",
    ["routing_key", "result"],
)
MESSAGE_PROCESSING = Histogram(
    "aow_message_processing_duration_seconds",
    "Time to handle one delivery, including the database transaction.",
    ["routing_key"],
    buckets=CONSUME_BUCKETS,
)
CONSUMER_LAST_MESSAGE = Gauge(
    "aow_consumer_last_message_timestamp_seconds",
    "Unix time of the last delivery the consumer handled.",
)


# --------------------------------------------------------------- ingestor ----

INGESTION_RUNS = Counter(
    "aow_ingestion_runs_total",
    "Ingestion attempts by source. One per snapshot replay, one per city fetched live.",
    ["source", "result"],
)
INGESTION_LAST_SUCCESS = Gauge(
    "aow_ingestion_last_success_timestamp_seconds",
    "Unix time of the last successful ingestion from this source.",
    ["source"],
)


# --------------------------------------------------------------- enricher ----

ENRICHMENT_REQUESTS = Counter(
    "aow_enrichment_requests_total",
    "Wording attempts: ready, invalid (the model was wrong) or unavailable (it was not there).",
    ["result"],
)
ENRICHMENT_DURATION = Histogram(
    "aow_enrichment_duration_seconds",
    "Wall time of one local model call made by the enricher, successful or not.",
    buckets=ENRICH_BUCKETS,
)

# Counted per status so the two are never added together: 'pending' rows are
# queued for the model, 'deferred' rows are scored and charted and were never
# queued at all (see consumer.score_defaults). An alert on the first would be
# permanently firing if it included the second.
BACKLOG_SQL = """
SELECT status, COUNT(*) AS rows
  FROM recommendations
 WHERE status IN ('pending', 'deferred')
 GROUP BY status
"""


class EnrichmentBacklogCollector:
    """How much wording is outstanding, from the database the enricher polls.

    Cached for `ttl` seconds because a scrape costs a round trip to Postgres.
    At the 15s scrape interval this stack uses that changes almost nothing; what
    it buys is a ceiling, so a second Prometheus, a reload loop or a human
    curling `/metrics` cannot turn the enricher's one read connection into a
    query generator. The same query already backs `GET /enrichment`.

    A failed read serves the last value it had rather than zero. A database blip
    must not look like "the backlog cleared", which is the one reading that
    would silently resolve the alert this gauge exists for.
    """

    def __init__(self, pool, *, ttl: float = 15.0):
        self.pool = pool
        self.ttl = ttl
        self._cached: dict[str, int] = {}
        self._fetched_at = 0.0

    def _counts(self) -> dict[str, int]:
        # Keyed off the fetch time, not off the cache being non-empty: an empty
        # result is a real answer ("nothing is waiting"), and treating it as a
        # cache miss would mean a round trip per scrape in exactly the steady
        # state the cache exists for.
        if self._fetched_at and time.monotonic() - self._fetched_at < self.ttl:
            return self._cached
        # The pool's existing handle, not `pool.conn`. `Pool.conn` dials with an
        # unbounded retry loop, so during a database outage a scrape would block
        # in it for as long as the outage lasts, and every scrape would park
        # another thread there. Reconnecting is the enricher loop's job; a
        # scrape only reports what it can already see.
        conn = getattr(self.pool, "_conn", None)
        if conn is None or conn.closed:
            return self._cached
        try:
            rows = conn.execute(BACKLOG_SQL).fetchall()
        except Exception as exc:  # noqa: BLE001 - a scrape must never raise
            log.warning("enrichment backlog unavailable: %s", exc)
            # Deliberately not dropping the connection here: the enricher's own
            # loop owns that decision, and a scrape reaching in to reset it
            # would be instrumentation changing the behaviour it measures.
            return self._cached
        self._cached = {str(row["status"]): int(row["rows"]) for row in rows}
        self._fetched_at = time.monotonic()
        return self._cached

    def collect(self):
        counts = self._counts()
        backlog = GaugeMetricFamily(
            "aow_enrichment_backlog",
            "Recommendations waiting on the local model, by stored status.",
            labels=["status"],
        )
        # Both statuses are always emitted, including as zero: a 'pending'
        # series that vanishes when the queue empties makes every rate() and
        # every alert on it ambiguous.
        for status in ("pending", "deferred"):
            backlog.add_metric([status], counts.get(status, 0))
        yield backlog


def register_enrichment_backlog(pool, *, registry=REGISTRY) -> EnrichmentBacklogCollector:
    collector = EnrichmentBacklogCollector(pool)
    registry.register(collector)
    return collector


# ------------------------------------------------------------ http server ----

METRICS_PORT = 9100


def start_metrics_server(port: int = METRICS_PORT) -> bool:
    """Expose `/metrics` from a service that has no HTTP server of its own.

    Used by the consumer, the ingestor and the enricher. The containers are
    attached only to the internal `backend` network and publish no ports, so
    binding every interface inside the container means the backend network and
    nothing beyond it.

    A failure here is logged and swallowed on purpose: the port being taken is
    an instrumentation problem, and instrumentation is never allowed to stop the
    pipeline. A consumer that will not start because its metrics port is busy
    would be a monitoring feature causing the outage it was added to detect.
    """
    try:
        start_http_server(port)
    except OSError as exc:
        log.warning("metrics server not started on port %d (%s); continuing", port, exc)
        return False
    log.info("metrics server listening on :%d/metrics", port)
    return True
