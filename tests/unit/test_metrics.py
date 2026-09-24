"""The metric contract, and the cardinality rules that keep it affordable (B2).

Two different things are pinned here.

The first is *what a label may contain*. Prometheus keeps one time series per
distinct label combination, so an HTTP counter labelled with the request path
grows a series for every URL anyone tries -- one per city, one per itinerary id,
one per probe a scanner sends. These tests hold the opposite: the route label is
the template the router matched (`/weather/{city}`), anything unmatched collapses
into a single `<unmatched>` series, and a request method the code does not
recognise collapses the same way. The junk-path test asserts the series count
itself, because that is the failure it exists to catch.

The second is *the names*. The alert rules and the Grafana dashboard are written
against the exact strings below, and nothing in Prometheus complains when a
metric quietly stops existing -- the panel simply goes blank. So every name and
label set the dashboards use is asserted here, and a rename fails a test instead.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY, CollectorRegistry, generate_latest

from services.common import config, metrics
from services.common.envelope import Envelope
from services.common.outbox import Outbox

SERVICE = "unit-test"


def sample(name: str, **labels: str) -> float:
    """A series' current value, or 0.0 if it does not exist yet."""
    value = REGISTRY.get_sample_value(name, labels)
    return 0.0 if value is None else value


def series_count(name: str, registry=REGISTRY) -> int:
    """How many time series exist under one metric name, across all labels."""
    return sum(1 for family in registry.collect() for item in family.samples if item.name == name)


def build_app() -> TestClient:
    """A stand-in app with the same route shapes the API uses.

    Deliberately not `services.api.main.app` for these: its handlers read
    Postgres, and `db.Pool` dials with an unbounded retry loop, so a unit test
    that called one would hang rather than fail. The real app is exercised
    separately below, on the routes that answer without touching the database.
    """
    app = FastAPI()

    @app.get("/weather/{city}")
    def weather(city: str) -> dict[str, str]:
        return {"city": city}

    @app.get("/boom")
    def boom() -> dict[str, str]:
        raise RuntimeError("deliberate failure")

    metrics.mount_metrics(app, SERVICE)
    return TestClient(app)


# ------------------------------------------------------------ http labels ----


def test_route_label_is_the_template_not_the_path():
    """The cardinality guarantee: one series per endpoint, not per city."""
    client = build_app()
    before = sample(
        "aow_http_requests_total",
        service=SERVICE,
        method="GET",
        route="/weather/{city}",
        status="200",
    )

    assert client.get("/weather/rome").status_code == 200
    assert client.get("/weather/london").status_code == 200

    assert (
        sample(
            "aow_http_requests_total",
            service=SERVICE,
            method="GET",
            route="/weather/{city}",
            status="200",
        )
        == before + 2
    )
    # The concrete paths must not exist as series at all.
    for city in ("rome", "london"):
        assert (
            REGISTRY.get_sample_value(
                "aow_http_requests_total",
                {"service": SERVICE, "method": "GET", "route": f"/weather/{city}", "status": "200"},
            )
            is None
        )


def test_unmatched_paths_share_one_series():
    """A scanner walking random URLs must cost one series, not one each."""
    client = build_app()
    before_series = series_count("aow_http_requests_total")
    before_value = sample(
        "aow_http_requests_total",
        service=SERVICE,
        method="GET",
        route=metrics.UNMATCHED,
        status="404",
    )

    junk = ["/wp-login.php", "/.env", "/admin/1", "/admin/2", "/weather", "/../etc/passwd"]
    for path in junk:
        assert client.get(path).status_code == 404

    assert sample(
        "aow_http_requests_total",
        service=SERVICE,
        method="GET",
        route=metrics.UNMATCHED,
        status="404",
    ) == before_value + len(junk)
    # Six distinct paths, one new series.
    assert series_count("aow_http_requests_total") - before_series == 1


def test_unknown_method_shares_one_series():
    """The request line is the client's to write, so the method is bounded too."""
    client = build_app()
    before = series_count("aow_http_requests_total")
    for method in ("BREW", "WHAT", "PROPFIND"):
        client.request(method, "/weather/rome")
    assert (
        sample(
            "aow_http_requests_total",
            service=SERVICE,
            method=metrics.OTHER,
            route="/weather/{city}",
            status="405",
        )
        == 3
    )
    assert series_count("aow_http_requests_total") - before == 1


def test_duration_histogram_observes_the_request():
    client = build_app()
    labels = {"service": SERVICE, "method": "GET", "route": "/weather/{city}"}
    before = sample("aow_http_request_duration_seconds_count", **labels)
    client.get("/weather/rome")
    assert sample("aow_http_request_duration_seconds_count", **labels) == before + 1
    assert sample("aow_http_request_duration_seconds_sum", **labels) >= 0.0


def test_handler_exception_is_counted_by_class_and_re_raised():
    """The class name is bounded by the code; the message never becomes a label."""
    client = build_app()
    labels = {
        "service": SERVICE,
        "method": "GET",
        "route": "/boom",
        "type": "RuntimeError",
    }
    before = sample("aow_http_exceptions_total", **labels)

    with pytest.raises(RuntimeError, match="deliberate failure"):
        client.get("/boom")

    assert sample("aow_http_exceptions_total", **labels) == before + 1
    # The failure is still a failure: instrumentation observes, it does not
    # swallow. And the exception message is nowhere in the exposition.
    assert b"deliberate failure" not in generate_latest()


def test_handled_http_errors_are_requests_not_exceptions():
    """A 404 raised by a route is a response, not a crash. Keeping the two apart
    is what makes `aow_http_exceptions_total` worth alerting on at all."""
    client = build_app()
    before = series_count("aow_http_exceptions_total")
    assert client.get("/nothing-here").status_code == 404
    assert series_count("aow_http_exceptions_total") == before


# ------------------------------------------------------- the real api app ----


def test_api_app_is_wired_and_serves_metrics():
    """The middleware and the endpoint are actually mounted on the shipped app.

    `/records/{entity}/{entity_id}/history` is used because an unknown entity is
    refused by the handler before it reaches Postgres, so the assertion needs no
    database. The TestClient is built without its context manager for the reason
    test_api.py gives: the startup event would dial RabbitMQ.
    """
    from services.api.main import app

    client = TestClient(app)
    labels = {
        "service": "api",
        "method": "GET",
        "route": "/records/{entity}/{entity_id}/history",
        "status": "404",
    }
    before = sample("aow_http_requests_total", **labels)
    assert client.get("/records/nope/whatever/history").status_code == 404
    assert sample("aow_http_requests_total", **labels) == before + 1

    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert b"aow_http_requests_total" in response.content
    # The API's own outbox gauges are collected by the same scrape.
    assert b"aow_outbox_entries" in response.content


# ---------------------------------------------------------------- outbox ----


def envelope(city: str = "rome") -> Envelope:
    return Envelope.create(
        config.RK_WEATHER,
        {
            "city_id": city,
            "forecast_date": "2026-09-24",
            "provider": "open-meteo",
            "as_of": "2026-09-23T18:00:00+00:00",
        },
        source="ingestor",
        observed_at="2026-09-23T18:00:00+00:00",
        city=city,
    )


def accepted_at(box: Outbox, message_id: str, when: str) -> None:
    """Force a row's acceptance time, so 'oldest' can be asserted rather than
    inferred from three timestamps a microsecond apart."""
    box.conn.execute("UPDATE outbox SET accepted_at = ? WHERE message_id = ?", (when, message_id))


def test_oldest_pending_is_none_when_nothing_is_owed(tmp_path):
    box = Outbox(tmp_path / "outbox.sqlite3")
    assert box.oldest_pending_accepted_at() is None


def test_oldest_pending_tracks_the_oldest_then_clears(tmp_path):
    box = Outbox(tmp_path / "outbox.sqlite3")
    first = box.accept(envelope("rome"))
    second = box.accept(envelope("london"))
    third = box.accept(envelope("lisbon"))
    accepted_at(box, first, "2026-09-20T08:00:00+00:00")
    accepted_at(box, second, "2026-09-21T08:00:00+00:00")
    accepted_at(box, third, "2026-09-22T08:00:00+00:00")

    # The oldest, not the newest: this is the difference between "the broker is
    # slow right now" and "a record accepted on Sunday is still not delivered".
    assert box.oldest_pending_accepted_at() == "2026-09-20T08:00:00+00:00"

    box.mark_published(next(box.unpublished())["seq"])
    assert box.oldest_pending_accepted_at() == "2026-09-21T08:00:00+00:00"

    for row in box.unpublished():
        box.mark_published(row["seq"])
    assert box.oldest_pending_accepted_at() is None


def test_outbox_gauges_report_the_real_file(tmp_path):
    registry = CollectorRegistry()
    box = Outbox(tmp_path / "outbox.sqlite3")
    metrics.register_outbox_metrics("ingestor", box, registry=registry)

    assert (
        registry.get_sample_value("aow_outbox_entries", {"service": "ingestor", "state": "pending"})
        == 0
    )
    assert (
        registry.get_sample_value("aow_outbox_oldest_pending_age_seconds", {"service": "ingestor"})
        == 0
    )

    first = box.accept(envelope("rome"))
    box.accept(envelope("london"))
    box.accept(envelope("lisbon"))
    box.mark_published(next(box.unpublished())["seq"])  # publishes the first

    assert (
        registry.get_sample_value("aow_outbox_entries", {"service": "ingestor", "state": "pending"})
        == 2
    )
    assert (
        registry.get_sample_value(
            "aow_outbox_entries", {"service": "ingestor", "state": "published"}
        )
        == 1
    )

    # Age is read at scrape time from the oldest row that is still owed, so
    # backdating one moves the gauge without anything else being touched.
    remaining = next(box.unpublished())["message_id"]
    assert remaining != first
    accepted_at(box, remaining, "2020-01-01T00:00:00+00:00")
    age = registry.get_sample_value(
        "aow_outbox_oldest_pending_age_seconds", {"service": "ingestor"}
    )
    assert age > 365 * 24 * 3600


def test_a_broken_outbox_yields_no_samples_rather_than_zero(tmp_path):
    """A scrape must never raise, and must never invent a reassuring number."""
    registry = CollectorRegistry()
    box = Outbox(tmp_path / "outbox.sqlite3")
    box.accept(envelope())
    metrics.register_outbox_metrics("api", box, registry=registry)
    box.close()

    assert generate_latest(registry) == b""
    assert (
        registry.get_sample_value("aow_outbox_entries", {"service": "api", "state": "pending"})
        is None
    )


# -------------------------------------------------------------- bounded ----


def test_routing_key_labels_are_folded_to_the_declared_set():
    assert metrics.safe_routing_key(config.RK_WEATHER) == config.RK_WEATHER
    assert metrics.safe_routing_key("weather.daily.v2.oops") == metrics.UNMATCHED
    assert metrics.safe_routing_key(None) == metrics.UNMATCHED


# ------------------------------------------------------------- contract ----

# (metric object, the exported series name a dashboard queries, its labels).
# The label values are dummies: what is under test is that these label *names*
# are accepted and that the series appears under exactly this name.
CONTRACT = [
    (
        metrics.HTTP_REQUESTS,
        "aow_http_requests_total",
        {"service": "contract", "method": "GET", "route": "/", "status": "200"},
    ),
    (
        metrics.HTTP_DURATION,
        "aow_http_request_duration_seconds_count",
        {"service": "contract", "method": "GET", "route": "/"},
    ),
    (
        metrics.HTTP_EXCEPTIONS,
        "aow_http_exceptions_total",
        {"service": "contract", "method": "GET", "route": "/", "type": "RuntimeError"},
    ),
    (metrics.OUTBOX_PUBLISH_FAILURES, "aow_outbox_publish_failures_total", {"service": "contract"}),
    (
        metrics.MESSAGES_PUBLISHED,
        "aow_messages_published_total",
        {"service": "contract", "routing_key": config.RK_WEATHER},
    ),
    (
        metrics.MESSAGES_CONSUMED,
        "aow_messages_consumed_total",
        {"routing_key": config.RK_WEATHER, "result": "stored"},
    ),
    (
        metrics.MESSAGE_PROCESSING,
        "aow_message_processing_duration_seconds_count",
        {"routing_key": config.RK_WEATHER},
    ),
    (metrics.CONSUMER_LAST_MESSAGE, "aow_consumer_last_message_timestamp_seconds", {}),
    (metrics.INGESTION_RUNS, "aow_ingestion_runs_total", {"source": "snapshot", "result": "ok"}),
    (
        metrics.INGESTION_LAST_SUCCESS,
        "aow_ingestion_last_success_timestamp_seconds",
        {"source": "snapshot"},
    ),
    (metrics.ENRICHMENT_REQUESTS, "aow_enrichment_requests_total", {"result": "ready"}),
    (metrics.ENRICHMENT_DURATION, "aow_enrichment_duration_seconds_count", {}),
]


@pytest.mark.parametrize(("metric", "series", "labels"), CONTRACT)
def test_metric_contract(metric, series, labels):
    """Exercise each metric with its documented labels, then find it by name.

    `labels()` refuses a label set that does not match the metric's own, so this
    pins the label names; `get_sample_value` pins the series name, including the
    `_total` and `_count` suffixes a PromQL query has to spell out.
    """
    child = metric.labels(**labels) if labels else metric
    if hasattr(child, "observe"):
        child.observe(0.0)
    elif hasattr(child, "inc"):
        child.inc(0)
    assert REGISTRY.get_sample_value(series, labels) is not None, f"{series} is missing"


class FakePool:
    """Stands in for common.db.Pool. `_conn` is what the collector reads, for
    the reason the collector documents: `pool.conn` would dial and retry."""

    class _Conn:
        closed = False

        def execute(self, sql):
            assert "recommendations" in sql
            return self

        def fetchall(self):
            return [{"status": "pending", "rows": 7}, {"status": "deferred", "rows": 41}]

    def __init__(self):
        self._conn = self._Conn()


def test_enrichment_backlog_contract_and_split():
    registry = CollectorRegistry()
    metrics.register_enrichment_backlog(FakePool(), registry=registry)
    assert registry.get_sample_value("aow_enrichment_backlog", {"status": "pending"}) == 7
    assert registry.get_sample_value("aow_enrichment_backlog", {"status": "deferred"}) == 41


def test_enrichment_backlog_emits_zero_rather_than_nothing():
    """An empty queue must still report a series: a gauge that disappears makes
    every alert written against it ambiguous."""
    registry = CollectorRegistry()

    class Empty(FakePool):
        class _Conn(FakePool._Conn):
            def fetchall(self):
                return []

        def __init__(self):
            self._conn = self._Conn()

    metrics.register_enrichment_backlog(Empty(), registry=registry)
    assert registry.get_sample_value("aow_enrichment_backlog", {"status": "pending"}) == 0
    assert registry.get_sample_value("aow_enrichment_backlog", {"status": "deferred"}) == 0
