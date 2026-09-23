"""Exercise the running queue, database, API and correction path in CI.

Run via ``docker compose exec -T api python - < tests/integration/smoke.py``.
The API image already contains Python and its runtime dependencies, so this
does not install anything or reach an external service.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000"


def request(path: str, *, method: str = "GET", body: dict | None = None):
    payload = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(BASE + path, data=payload, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=5) as response:
        return response.status, json.load(response)


def wait_for(predicate, description: str, timeout: int = 180):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (OSError, ValueError, AssertionError) as exc:
            last_error = exc
        time.sleep(2)
    raise AssertionError(f"timed out waiting for {description}: {last_error}")


def stored_snapshot():
    _, health = request("/health")
    if health.get("status") != "ok":
        return None
    _, coverage = request("/coverage")
    weather = next(row for row in coverage["entities"] if row["entity"] == "weather")
    facts = next(row for row in coverage["entities"] if row["entity"] == "facts")
    if weather["rows"] > 0 and weather["cities"] == 5 and facts["rows"] > 0:
        return coverage
    return None


coverage = wait_for(stored_snapshot, "snapshot to pass through RabbitMQ into Postgres")
assert len(coverage["cities"]) == 5, coverage["cities"]

status, weather = request("/weather/rome")
assert status == 200 and weather and all(row["city_id"] == "rome" for row in weather)
status, scores = request("/scores?city=rome")
assert status == 200 and scores, "rules were not applied when weather was stored"

status, facts = request("/facts?city=rome")
assert status == 200 and facts, "snapshot facts are missing"
fact = facts[0]
new_title = fact["title"] + " [CI correction]"
path = "/records/facts/" + urllib.parse.quote(fact["id"], safe="")
status, accepted = request(path, method="PATCH", body={"title": new_title})
assert status == 202 and accepted["accepted"] and accepted["message_id"], accepted
message_id = urllib.parse.quote(accepted["message_id"], safe="")


def correction_stored():
    _, result = request("/outbox/" + message_id)
    return result if result.get("stored") else None


wait_for(correction_stored, "correction to pass through the outbox and queue", 120)
_, history = request(path + "/history")
assert any(row["new_row"]["title"] == new_title for row in history), history
print("PASS: snapshot -> queue -> database -> API, rules, correction -> queue -> history")
