# Test runner. Dependencies are baked at build time so `make test` itself makes
# no network call -- the same rule the runtime follows.
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

WORKDIR /app
COPY services/requirements.txt /tmp/req/requirements.txt
COPY services/common/requirements.txt /tmp/req/common/requirements.txt
RUN pip install --no-cache-dir -r /tmp/req/requirements.txt pytest==8.3.3 ruff==0.8.4

COPY pytest.ini ruff.toml ./
COPY data/ ./data/
COPY services/ ./services/
COPY tests/ ./tests/

RUN useradd -u 10001 -m appuser && chown -R appuser /app
USER 10001
CMD ["pytest"]
