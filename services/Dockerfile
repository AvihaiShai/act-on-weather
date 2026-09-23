# One image for the five Python services. They differ only in their command, so
# one build, one dependency set and one Trivy scan covers all of them.
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
# Dependencies first, and alone, so a code change does not re-run pip.
COPY services/requirements.txt /tmp/req/requirements.txt
COPY services/common/requirements.txt /tmp/req/common/requirements.txt
RUN pip install --no-cache-dir -r /tmp/req/requirements.txt

COPY services/ /app/services/
COPY data/ /app/data/

# Non-root. /outbox is created here and owned by the same uid, so that Docker
# gives a fresh named volume mounted there the same ownership -- otherwise the
# outbox would be root-owned and the service could not accept anything.
RUN useradd -u 10001 -m appuser \
 && mkdir -p /outbox \
 && chown -R appuser /app /outbox
USER 10001

CMD ["python", "-m", "services.consumer.main"]
