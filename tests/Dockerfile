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

COPY pytest.ini ruff.toml ./
# test_demo_events.py asserts that AOW_DEMO_EVENTS appears only in the demo
# overlay. If demo mode ever leaked into the default compose file it would
# stop being opt-in, and that is worth catching in CI rather than by eye.
COPY compose.yml compose.demo.yml ./
COPY data/ ./data/
COPY services/ ./services/
COPY tests/ ./tests/

RUN useradd -u 10001 -m appuser && chown -R appuser /app
USER 10001
CMD ["pytest"]
