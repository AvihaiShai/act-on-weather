# Test runner. Dependencies are baked at build time so `make test` itself makes
# no network call -- the same rule the runtime follows.
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

WORKDIR /app
COPY services/requirements.txt /tmp/req/requirements.txt
COPY services/common/requirements.txt /tmp/req/common/requirements.txt
# The UI's own pinned set, because `test_ui.py` renders the real app through
# `streamlit.testing.v1.AppTest`. Same versions the UI image installs, so a
# render that passes here passes in the container that ships.
COPY services/ui/requirements.txt /tmp/req/ui.txt
# httpx is starlette's TestClient transport. It is a test-only dependency and
# is deliberately not in any service image.
RUN pip install --no-cache-dir -r /tmp/req/requirements.txt -r /tmp/req/ui.txt \
      pytest==8.3.3 ruff==0.8.4 httpx==0.27.2

COPY pytest.ini ruff.toml Makefile ./
# Bootstrap reads the pinned helper image from Makefile; its offline tests run
# the real script, so the test image needs that file too.
# Three tests read the deployment files themselves rather than the code.
# test_demo_events.py asserts that AOW_DEMO_EVENTS appears only in the demo
# overlay -- if demo mode ever leaked into the default compose file it would
# stop being opt-in. test_compose_ports.py asserts that every published port,
# on every overlay, binds loopback, so all of them are copied and not just the
# two that test happens to know about today. `.env.example` comes with them:
# it is the file an operator copies before their first run, and a wide address
# there would override every default in the Compose files. It holds
# placeholders only; the real `.env` is excluded by .dockerignore.
# test_refresh.py reads compose.tools.yml from the same set: the
# operator-refresh container must not be on the egress network.
COPY compose*.yml .env.example ./
# test_migrations_applied.py checks the migration files against the `-f` list
# in compose.yml's migrate command. Only the filenames are read, but the
# directory has to be here for the glob to see them at all -- and a glob that
# silently matches nothing is how the unwired migration this test exists for
# would have gone on being invisible.
COPY db/ ./db/
# test_observability.py reads the monitoring configuration as shipped: the
# Prometheus scrape config and alert rules, Grafana's provisioning and both
# dashboards. Its most valuable assertion is that every aow_ metric named in a
# PromQL expression is one the services actually export -- a typo there is
# valid PromQL that evaluates to nothing forever, so it has to fail in a test
# rather than during the incident the alert was written for. `edge/` comes
# with it for the one assertion that the API's /metrics is not served on the
# published port.
COPY observability/ ./observability/
COPY edge/ ./edge/
COPY data/ ./data/
COPY services/ ./services/
# scripts/refresh_report.py assembles the report the operator refresh files for
# the UI, so it is exercised like any other code rather than only by running the
# command. It is also what `ruff check scripts` lints in CI.
COPY scripts/ ./scripts/
# test_backup_restore.py reads demos/06_backup_restore.sh: the restore refuses a
# target project that still has containers running, and the one thing that must
# stay true of that guard is that the drill -- whose disruption is its own
# `down -v` -- is never caught by it. That is an assertion about the drill's
# order, so the drill has to be here to read.
COPY demos/ ./demos/
COPY tests/ ./tests/

RUN useradd -u 10001 -m appuser && chown -R appuser /app
USER 10001
CMD ["pytest"]
