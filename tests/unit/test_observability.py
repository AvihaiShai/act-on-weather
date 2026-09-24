"""The monitoring stack, checked as shipped files rather than as a running system.

Everything here is offline and reads what is committed. No Docker, no network,
no Prometheus process. That is a deliberate limit and it is worth being clear
about what it buys, because monitoring config fails in a peculiarly quiet way:
a PromQL expression naming a metric that does not exist is perfectly valid
PromQL. It parses, it evaluates, it returns an empty vector, and the alert
built on it never fires. Nobody is paged about an alert that cannot fire. The
first time anyone finds out is during the incident it was written for.

So the expensive assertion in this file is the first one: every `aow_*` metric
name that appears in an alert expression or a dashboard query must be on a
literal list of the metrics the services actually export. A typo fails here,
in a second, instead of silently never firing. The list is written out in this
file rather than derived from the source, which makes renaming a metric a
deliberate two-place edit -- that is the point, not an oversight.

The rest:

  * every alert rule has a `for:`, a `severity` and an annotation that tells an
    operator what to DO;
  * the scrape config still lists every job the dashboards query;
  * Grafana's datasource points into the internal network and its phone-home
    switches are off in the overlay;
  * no dashboard uses a panel type that is not built into core Grafana, since
    a plugin panel is something Grafana would have to download;
  * and the isolation property itself -- prometheus and grafana on `backend`
    only, edge-observability on both -- asserted over the overlay file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
OBS = ROOT / "observability"
PROM_CONFIG = OBS / "prometheus" / "prometheus.yml"
ALERTS = OBS / "prometheus" / "alerts.yml"
DATASOURCE = OBS / "grafana" / "provisioning" / "datasources" / "prometheus.yml"
DASHBOARD_PROVIDER = OBS / "grafana" / "provisioning" / "dashboards" / "dashboards.yml"
DASHBOARD_DIR = OBS / "grafana" / "dashboards"
OVERLAY = ROOT / "compose.observability.yml"

# ---------------------------------------------------------------------------
# The metric contract.
#
# Written out by hand, on purpose. Deriving it from services/ would make this
# test agree with the code by construction, which is the one thing it must not
# do: the failure it exists to catch is a PromQL expression and an exporter
# that have drifted apart, and a test that reads both from the same place
# cannot see that. Renaming a metric therefore costs two edits -- the service
# and this list -- and the second one is the review.
APPLICATION_METRICS = {
    # HTTP surface (api, agent)
    "aow_http_requests_total",
    "aow_http_request_duration_seconds",
    "aow_http_exceptions_total",
    # Producer outbox -- where the no-data-loss guarantee begins
    "aow_outbox_entries",
    "aow_outbox_oldest_pending_age_seconds",
    "aow_outbox_publish_failures_total",
    # Broker traffic, as the application sees it
    "aow_messages_published_total",
    "aow_messages_consumed_total",
    "aow_message_processing_duration_seconds",
    "aow_consumer_last_message_timestamp_seconds",
    # Ingestion
    "aow_ingestion_runs_total",
    "aow_ingestion_last_success_timestamp_seconds",
    # Enrichment (the LLM path, deliberately off the critical path)
    "aow_enrichment_requests_total",
    "aow_enrichment_duration_seconds",
    "aow_enrichment_backlog",
}

# Prometheus expands a histogram into <name>_bucket, <name>_sum and
# <name>_count, and every p95 expression in this repository queries the
# _bucket series. Stripping the suffix is how those map back onto the contract
# above; without it every histogram query would look like an unknown metric.
HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")

# Every scrape job in prometheus.yml. Named here so that deleting a job -- and
# thereby silently blinding a dashboard -- is a test failure rather than a
# smaller config file.
EXPECTED_JOBS = {
    "aow-api",
    "aow-agent",
    "aow-ingestor",
    "aow-consumer",
    "aow-enricher",
    "rabbitmq",
    "rabbitmq-queues",
    "llm",
    "prometheus",
}

# Panel types built into core Grafana. Anything outside this set is a plugin,
# and a plugin is a download from grafana.com -- which is exactly the runtime
# network call the whole air-gap design forbids. A dashboard referencing one
# would render as "panel plugin not found" on the reviewer's machine.
CORE_PANEL_TYPES = {"timeseries", "stat", "table", "gauge", "bargauge", "row", "text"}

# The phone-home settings, each of which is a real outbound call Grafana makes
# by default. `backend` being internal already makes all of them impossible;
# these are defence in depth, and their absence would be a claim in the README
# that the configuration does not back up.
GRAFANA_MUST_BE_DISABLED = {
    "GF_ANALYTICS_REPORTING_ENABLED": "false",
    "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
    "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES": "false",
    "GF_ANALYTICS_FEEDBACK_LINKS_ENABLED": "false",
    "GF_NEWS_NEWS_FEED_ENABLED": "false",
    "GF_PLUGINS_PREINSTALL_DISABLED": "true",
    # The one that is not in any air-gap guide: Grafana fetches plugin
    # signing keys from grafana.com on start and retries every 60s forever,
    # even with every setting above disabled. Found in the container's logs.
    "GF_PLUGINS_PUBLIC_KEY_RETRIEVAL_DISABLED": "true",
    "GF_SECURITY_DISABLE_GRAVATAR": "true",
    "GF_AUTH_ANONYMOUS_ENABLED": "false",
    "GF_USERS_ALLOW_SIGN_UP": "false",
}

# A bare metric name at a token boundary. `{` and `(` are excluded after the
# name so that `sum(` and friends are not mistaken for metrics; the leading
# lookbehind keeps `rate(aow_x[5m])` from matching `ate` and keeps label
# values ("aow.ingest") out of it.
METRIC_TOKEN = re.compile(r"(?<![\w.])(aow_[a-z0-9_]+)")


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _canonical(name: str) -> str:
    for suffix in HISTOGRAM_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def alert_rules() -> list[tuple[str, dict]]:
    """(group name, rule) for every alerting rule there is."""
    doc = _load(ALERTS)
    rules = []
    for group in doc["groups"]:
        for rule in group["rules"]:
            if "alert" in rule:
                rules.append((group["name"], rule))
    return rules


def dashboard_files() -> list[Path]:
    return sorted(DASHBOARD_DIR.glob("*.json"))


def dashboard_panels() -> list[tuple[str, dict]]:
    """(dashboard file name, panel) for every panel in every dashboard."""
    panels = []
    for path in dashboard_files():
        doc = json.loads(path.read_text(encoding="utf-8"))
        for panel in doc.get("panels", []):
            panels.append((path.name, panel))
            # Collapsed rows nest their children; none today, but a dashboard
            # that grows one should not quietly stop being checked.
            for nested in panel.get("panels", []) or []:
                panels.append((path.name, nested))
    return panels


def promql_expressions() -> list[tuple[str, str]]:
    """(where it came from, expression) for every PromQL string we ship."""
    found = [(f"alerts.yml:{rule['alert']}", rule["expr"]) for _, rule in alert_rules()]
    for filename, panel in dashboard_panels():
        for target in panel.get("targets", []) or []:
            if "expr" in target:
                found.append((f"{filename}:{panel.get('title', panel.get('id'))}", target["expr"]))
    return found


# ---------------------------------------------------------------------------
# Guards. Every parametrised test below is generated from a glob or a parsed
# file, and a collection that silently came back empty would pass all of them
# without checking anything.


def test_the_observability_files_are_all_present():
    missing = [
        str(path.relative_to(ROOT))
        for path in (PROM_CONFIG, ALERTS, DATASOURCE, DASHBOARD_PROVIDER, OVERLAY)
        if not path.is_file()
    ]
    assert not missing, f"missing shipped files: {missing}"
    assert dashboard_files(), "no dashboards found -- has the directory moved?"
    assert len(alert_rules()) >= 10, f"only {len(alert_rules())} alert rules"
    assert promql_expressions(), "no PromQL found to check"


# ---------------------------------------------------------------------------
# The expensive one: names.


@pytest.mark.parametrize(("where", "expr"), promql_expressions(), ids=lambda v: str(v)[:60])
def test_every_aow_metric_queried_is_one_the_services_export(where, expr):
    """A PromQL expression naming a metric that does not exist is valid PromQL.
    It evaluates to an empty vector, the alert built on it never fires, and
    nobody is paged about an alert that cannot fire. This is the check that
    turns that silence into a test failure."""
    unknown = sorted(
        {name for name in METRIC_TOKEN.findall(expr) if _canonical(name) not in APPLICATION_METRICS}
    )
    assert not unknown, (
        f"{where} queries {unknown}, which no service exports. Either the metric "
        f"was renamed and this expression was not, or it is a typo -- in both "
        f"cases the query returns nothing forever. The contract is "
        f"APPLICATION_METRICS at the top of this file; changing a name is meant "
        f"to be a two-place edit."
    )


def test_the_contract_is_actually_exercised():
    """The test above only fails on names it sees. If no expression queried any
    aow_ metric at all it would pass vacuously, so: the dashboards and alerts
    together must cover a decent share of the contract."""
    queried = {
        _canonical(name) for _, expr in promql_expressions() for name in METRIC_TOKEN.findall(expr)
    }
    assert queried, "nothing queries an aow_ metric -- the check above proves nothing"
    uncovered = APPLICATION_METRICS - queried
    assert len(queried) >= 12, (
        f"only {len(queried)} of {len(APPLICATION_METRICS)} contract metrics are "
        f"used anywhere; not shown at all: {sorted(uncovered)}"
    )


def test_queue_depth_comes_from_the_detailed_exporter():
    """RabbitMQ 4.x publishes `rabbitmq_queue_messages` on its default /metrics
    endpoint with NO `queue` label -- the value is the sum over every queue.
    Verified against the running exporter: with one message in aow.ingest and
    one in aow.dlq it reads 2. An alert written against that would fire on a
    healthy backlog and stay silent for a single poisoned message, which is
    precisely backwards. The per-queue series lives on /metrics/detailed and
    is named `rabbitmq_detailed_queue_messages`."""
    depth_queries = [
        (where, expr) for where, expr in promql_expressions() if "queue_messages" in expr
    ]
    assert depth_queries, "nothing queries queue depth at all"
    for where, expr in depth_queries:
        assert "rabbitmq_detailed_queue_messages" in expr, (
            f"{where} uses an aggregated queue metric: {expr!r}. Use "
            f"rabbitmq_detailed_queue_messages{{queue=...}}, which is the only "
            f"series carrying a per-queue label."
        )


# ---------------------------------------------------------------------------
# Alert rules.


@pytest.mark.parametrize(
    ("group", "rule"),
    alert_rules(),
    ids=lambda v: v.get("alert", "") if isinstance(v, dict) else str(v),
)
def test_every_alert_waits_before_it_fires(group, rule):
    """Without a `for:`, one failed scrape, one restart or one slow request
    fires the alert. An operator learns to ignore it, and then ignores it on
    the day it is real."""
    assert rule.get("for"), f"{rule['alert']} (group {group}) has no `for:`"


@pytest.mark.parametrize(
    ("group", "rule"),
    alert_rules(),
    ids=lambda v: v.get("alert", "") if isinstance(v, dict) else str(v),
)
def test_every_alert_states_a_severity(group, rule):
    """`critical` means data is at risk or the system is not answering;
    `warning` means degraded while the guarantees still hold. Anything else is
    a label nobody has agreed the meaning of."""
    severity = (rule.get("labels") or {}).get("severity")
    assert severity in {
        "critical",
        "warning",
    }, f"{rule['alert']} has severity {severity!r}; expected 'critical' or 'warning'"


@pytest.mark.parametrize(
    ("group", "rule"),
    alert_rules(),
    ids=lambda v: v.get("alert", "") if isinstance(v, dict) else str(v),
)
def test_every_alert_tells_the_operator_what_to_do(group, rule):
    """The whole justification for shipping alerts without an Alertmanager is
    that each one carries its own remediation. A description that restates the
    expression is noise with extra steps, so the bar here is length: enough
    text that somebody wrote an instruction rather than a label."""
    annotations = rule.get("annotations") or {}
    assert annotations.get("summary"), f"{rule['alert']} has no summary"
    description = (annotations.get("description") or "").strip()
    assert len(description) >= 120, (
        f"{rule['alert']} has a {len(description)}-character description. It should say "
        f"what an operator ought to DO -- which command to run, what it means for "
        f"stored data -- not restate the expression."
    )


def test_the_required_alerts_all_exist():
    """Named individually. Losing one of these in a refactor would leave the
    stack quietly unmonitored for exactly the failure it was written for."""
    required = {
        "AowServiceDown",
        "AowModelUnavailable",
        "AowQueueBacklog",
        "AowDeadLetters",
        "AowOutboxStuck",
        "AowIngestionStale",
        "AowServerErrors",
        "AowHighLatency",
        "AowConsumerRejecting",
        "AowEnrichmentFailing",
    }
    present = {rule["alert"] for _, rule in alert_rules()}
    assert required <= present, f"missing alert rules: {sorted(required - present)}"


def test_the_model_alert_says_weather_is_unaffected():
    """The design decision this alert exists to communicate: the enricher polls
    pending recommendations instead of sitting in the ingest path, so a dead
    model cannot block or lose a weather record. An operator reading the alert
    at 2am must not conclude the pipeline is down."""
    rule = next(rule for _, rule in alert_rules() if rule["alert"] == "AowModelUnavailable")
    text = " ".join(rule["annotations"].values()).lower()
    assert "weather data is unaffected" in text
    assert "pending" in text


def test_the_dead_letter_alert_names_the_commands_that_fix_it():
    rule = next(rule for _, rule in alert_rules() if rule["alert"] == "AowDeadLetters")
    text = " ".join(rule["annotations"].values())
    assert "make dlq" in text and "make redrive" in text


def test_there_is_no_alertmanager_and_the_file_says_why():
    """Not shipping one is a decision, and a decision that is not written down
    reads as an omission. The argument: an air-gapped single host has no mail
    relay and no webhook target, so a routing component would have nowhere to
    route; the rules surface through ALERTS and the dashboard, and
    Alertmanager belongs on the production (Kubernetes) path."""
    assert "alerting" not in _load(PROM_CONFIG), "prometheus.yml configures an Alertmanager"
    text = ALERTS.read_text(encoding="utf-8").lower()
    assert "alertmanager" in text, "alerts.yml never explains the missing Alertmanager"
    assert (
        "kubernetes" in text or "production path" in text
    ), "alerts.yml does not say where Alertmanager does belong"

    firing_panels = [
        panel
        for _, panel in dashboard_panels()
        if any(
            'ALERTS{alertstate="firing"}' in (t.get("expr") or "")
            for t in panel.get("targets", []) or []
        )
    ]
    assert firing_panels, (
        'no dashboard panel renders ALERTS{alertstate="firing"}. Without an '
        "Alertmanager that panel IS the delivery path, so it is not optional."
    )


# ---------------------------------------------------------------------------
# Scrape configuration.


def test_prometheus_scrapes_every_expected_job():
    config = _load(PROM_CONFIG)
    jobs = {entry["job_name"] for entry in config["scrape_configs"]}
    assert jobs == EXPECTED_JOBS, (
        f"scrape jobs drifted: missing {sorted(EXPECTED_JOBS - jobs)}, "
        f"unexpected {sorted(jobs - EXPECTED_JOBS)}"
    )


@pytest.mark.parametrize(
    ("job", "target"),
    [
        ("aow-api", "api:8000"),
        ("aow-agent", "agent:8100"),
        ("aow-ingestor", "ingestor:9100"),
        ("aow-consumer", "consumer:9100"),
        ("aow-enricher", "enricher:9100"),
        ("rabbitmq", "rabbitmq:15692"),
        ("rabbitmq-queues", "rabbitmq:15692"),
        ("llm", "llm:8080"),
        ("prometheus", "localhost:9090"),
    ],
)
def test_each_job_points_at_the_right_container_and_port(job, target):
    """Ports are spelled out because a metrics endpoint on the wrong port is a
    target that is permanently `up == 0`, which then fires AowServiceDown
    forever and teaches everyone to ignore it."""
    config = _load(PROM_CONFIG)
    entry = next(e for e in config["scrape_configs"] if e["job_name"] == job)
    targets = [t for static in entry["static_configs"] for t in static["targets"]]
    assert targets == [target]


def test_the_rules_file_is_actually_loaded():
    """An alerts file Prometheus does not read is a file nobody notices is
    broken."""
    config = _load(PROM_CONFIG)
    assert config["rule_files"] == ["/etc/prometheus/alerts.yml"]


def test_the_scrape_interval_matches_what_grafana_is_told():
    """Grafana's `timeInterval` is how it picks a default step for rate()
    windows. Set below the real scrape interval it generates queries that
    return gaps between scrapes, and every graph grows holes nobody can
    explain."""
    scrape = _load(PROM_CONFIG)["global"]["scrape_interval"]
    grafana = _load(DATASOURCE)["datasources"][0]["jsonData"]["timeInterval"]
    assert scrape == grafana == "15s"


def test_the_per_queue_scrape_asks_for_the_right_family():
    """The detailed endpoint returns everything by default -- 460 KB a scrape.
    One family is 2.3 KB and holds the only per-queue depth series there is."""
    config = _load(PROM_CONFIG)
    entry = next(e for e in config["scrape_configs"] if e["job_name"] == "rabbitmq-queues")
    assert entry["metrics_path"] == "/metrics/detailed"
    assert entry["params"]["family"] == ["queue_coarse_metrics"]


# ---------------------------------------------------------------------------
# Grafana provisioning.


def test_the_datasource_points_into_the_internal_network():
    """http://prometheus:9090 is a hostname on `backend`, which is
    `internal: true`. This is not merely where Prometheus happens to be -- it
    is the only place Grafana can reach at all."""
    doc = _load(DATASOURCE)
    (datasource,) = doc["datasources"]
    assert datasource["url"] == "http://prometheus:9090"
    assert datasource["type"] == "prometheus"
    assert datasource["uid"] == "aow-prometheus"
    assert datasource["access"] == "proxy"
    assert datasource["editable"] is False


def test_the_dashboards_are_provisioned_from_the_mounted_directory():
    doc = _load(DASHBOARD_PROVIDER)
    (provider,) = doc["providers"]
    assert provider["type"] == "file"
    assert provider["options"]["path"] == "/var/lib/grafana/dashboards"
    assert provider["allowUiUpdates"] is False, (
        "a dashboard edited in the browser would win over the file until the next "
        "restart silently reverted it"
    )


@pytest.mark.parametrize("path", dashboard_files(), ids=lambda p: p.name)
def test_every_dashboard_is_valid_json_with_a_stable_uid(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc.get("uid"), f"{path.name} has no uid, so its URL changes on every reload"
    assert doc.get("title")
    assert doc.get("panels")


@pytest.mark.parametrize(("filename", "panel"), dashboard_panels(), ids=lambda v: str(v)[:40])
def test_every_panel_uses_a_core_grafana_visualisation(filename, panel):
    """A plugin panel is a download from grafana.com. On an air-gapped host it
    never arrives and the panel renders as 'plugin not found' -- which looks
    like a broken dashboard rather than a broken premise."""
    assert panel["type"] in CORE_PANEL_TYPES, (
        f"{filename}: panel {panel.get('title')!r} uses '{panel['type']}', which is not "
        f"a core Grafana panel and would have to be downloaded at runtime"
    )


@pytest.mark.parametrize(("filename", "panel"), dashboard_panels(), ids=lambda v: str(v)[:40])
def test_every_panel_says_what_it_means_and_in_what_unit(filename, panel):
    """A number with no unit is a number nobody can act on: 4 could be seconds,
    messages or per-second. The title is the claim and the description is the
    reasoning; a reviewer reads both."""
    if panel["type"] in {"row", "text"}:
        pytest.skip("layout panel, no data")
    title = panel.get("title", "")
    assert len(title) > 10, f"{filename}: panel {panel.get('id')} has title {title!r}"
    assert (
        len(panel.get("description", "")) >= 60
    ), f"{filename}: panel {title!r} has no description explaining what it means"
    unit = (panel.get("fieldConfig", {}).get("defaults", {})).get("unit")
    assert unit, f"{filename}: panel {title!r} sets no unit"


@pytest.mark.parametrize(("filename", "panel"), dashboard_panels(), ids=lambda v: str(v)[:40])
def test_every_panel_names_the_provisioned_datasource(filename, panel):
    """By uid, not by Grafana's auto-generated id. A dashboard that relies on
    'whatever the default datasource is' breaks the moment a second one is
    added, and breaks silently."""
    if panel["type"] in {"row", "text"}:
        pytest.skip("layout panel, no data")
    assert panel.get("datasource", {}).get("uid") == "aow-prometheus"


# ---------------------------------------------------------------------------
# The overlay itself: the isolation property, asserted over the shipped file.


def overlay() -> dict:
    return _load(OVERLAY)


def test_prometheus_and_grafana_are_on_the_internal_network_only():
    """The design point a reviewer should test. Putting Grafana on `frontend`
    to publish 3000 directly would be one line shorter and would hand Grafana
    a working route to the internet -- and Grafana, left alone, checks
    grafana.com for updates, plugin updates and a news feed on every start.
    The GF_* switches turn that off, but 'we asked it not to' is a weaker
    claim than 'it cannot', and the README's air-gap argument rests on the
    second one."""
    services = overlay()["services"]
    assert services["prometheus"]["networks"] == ["backend"]
    assert services["grafana"]["networks"] == ["backend"]


def test_the_observability_edge_is_the_only_thing_that_straddles():
    """Same pattern as `edge` in compose.yml, same single reason: Docker
    cannot publish a port from an internal network, so one stateless proxy
    holds both sides."""
    edge = overlay()["services"]["edge-observability"]
    assert sorted(edge["networks"]) == ["backend", "frontend"]
    assert edge["ports"] == ["${AOW_BIND_ADDR:-127.0.0.1}:3000:3000"]


def test_prometheus_is_not_published_and_grafana_is_not_published_directly():
    """Only edge-observability may publish. test_compose_ports.py enforces
    this across every compose file; it is restated here because it is the
    property this overlay exists to preserve, and a reader of this file should
    not have to go and find the other one."""
    services = overlay()["services"]
    assert "ports" not in services["prometheus"]
    assert "ports" not in services["grafana"]


@pytest.mark.parametrize(("variable", "value"), sorted(GRAFANA_MUST_BE_DISABLED.items()))
def test_grafana_phones_home_for_nothing(variable, value):
    """Each of these is a real outbound call Grafana makes by default. The
    network already makes them impossible; turning them off as well means
    Grafana is not spending its start-up timing out on DNS that will never
    resolve, and the container's logs stay readable."""
    environment = overlay()["services"]["grafana"]["environment"]
    assert (
        environment.get(variable) == value
    ), f"{variable} is {environment.get(variable)!r}, expected {value!r}"


def test_grafanas_admin_password_comes_from_env_and_has_no_default():
    """`:?` rather than `:-`. A default would leave Grafana on a password
    committed to this repository, on a port published on the host -- loopback
    or not, that is a credential in Git."""
    environment = overlay()["services"]["grafana"]["environment"]
    assert environment["GF_SECURITY_ADMIN_PASSWORD"].startswith("${GRAFANA_ADMIN_PASSWORD:?")


@pytest.mark.parametrize("service", ["prometheus", "grafana", "edge-observability"])
def test_every_monitoring_container_has_a_memory_budget(service):
    """The rest of the stack sets one on every service, and for a reason: this
    is expected to run on a 16 GB machine beside a local model server, and an
    unbounded Prometheus on a long-lived demo host is the component that
    notices last."""
    assert overlay()["services"][service].get("mem_limit")


@pytest.mark.parametrize("service", ["prometheus", "grafana"])
def test_the_monitoring_images_are_pinned_by_digest(service):
    """Same rule as every other pulled image in this repository: a moving tag
    resolves differently on the reviewer's machine, which is the one place
    nothing can be re-pulled."""
    image = overlay()["services"][service]["image"]
    assert re.fullmatch(r".+@sha256:[0-9a-f]{64}", image), f"{service}: {image} is not pinned"


def test_prometheus_keeps_its_database_on_a_named_volume():
    """AowIngestionStale asks whether anything succeeded in the last 24 hours.
    A TSDB that does not survive `docker compose down` cannot answer that, and
    the alert would reset to healthy every time the stack restarted."""
    doc = overlay()
    assert "promdata" in doc["volumes"]
    assert "grafanadata" in doc["volumes"]
    assert "promdata:/prometheus" in doc["services"]["prometheus"]["volumes"]
    assert "grafanadata:/var/lib/grafana" in doc["services"]["grafana"]["volumes"]


# ---------------------------------------------------------------------------
# The published API surface.


def test_the_api_metrics_endpoint_is_not_served_on_the_published_port():
    """edge/nginx.conf proxies `/` to api:8000, so without this the API's
    /metrics would be served on the host's port 8000 along with everything
    else -- a free map of every route, its error counts and its latency
    distribution, to anyone who can reach the port. Prometheus is unaffected:
    it scrapes api:8000/metrics directly over `backend`, which never passes
    through this proxy."""
    text = (ROOT / "edge" / "nginx.conf").read_text(encoding="utf-8")
    assert re.search(
        r"location\s*=\s*/metrics\s*\{[^}]*return\s+404", text
    ), "edge/nginx.conf does not block /metrics on the published API port"


def test_rabbitmq_is_told_which_plugins_it_must_run():
    """The exporter on 15692 is the only source of per-queue depth. The pinned
    image already enables both plugins, so this file does not switch anything
    on -- it pins the dependency, so a later image bump that drops one fails
    loudly instead of leaving every queue-depth alert silently unable to
    fire."""
    plugins = (OBS / "rabbitmq" / "enabled_plugins").read_text(encoding="utf-8")
    term = [line for line in plugins.splitlines() if line.strip() and not line.startswith("%")]
    assert term == ["[rabbitmq_management,rabbitmq_prometheus]."], (
        f"unexpected Erlang term {term!r}. It must be one list with a trailing "
        f"period, and it must re-declare rabbitmq_management: the mount replaces "
        f"the image's file rather than adding to it."
    )
    compose = (ROOT / "compose.yml").read_text(encoding="utf-8")
    assert "./observability/rabbitmq/enabled_plugins:/etc/rabbitmq/enabled_plugins:ro" in compose
