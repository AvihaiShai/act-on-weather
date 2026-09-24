"""Prove the shipped enricher process reads pending work with its reader role.

CI deliberately does not start a model here. Pending recommendations must remain
pending, while the real enricher container starts, connects to Postgres, and
reports the same backlog that a separate reader sees.
"""

import re
import time
import urllib.request

import psycopg

from services.common import config

with psycopg.connect(config.reader_dsn()) as conn:
    pending = conn.execute(
        "SELECT count(*) FROM recommendations WHERE status = 'pending'"
    ).fetchone()[0]
assert pending > 0, "smoke test did not create pending recommendations"

deadline = time.monotonic() + 30
observed = None
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen("http://127.0.0.1:9100/metrics", timeout=3) as response:
            metrics = response.read().decode()
        match = re.search(
            r'^aow_enrichment_backlog\{status="pending"\} (\d+(?:\.\d+)?)$', metrics, re.M
        )
        if match:
            observed = int(float(match.group(1)))
            if observed == pending:
                break
    except OSError:
        pass
    time.sleep(1)
else:
    raise AssertionError(f"enricher backlog metric {observed!r} != reader count {pending}")

print(f"PASS: running enricher reads and reports {pending} pending recommendations")
