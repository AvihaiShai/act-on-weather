"""Read-only post-install checks for an offline release."""

from __future__ import annotations

import json
import time
import urllib.request


def get(url: str):
    with urllib.request.urlopen(url, timeout=5) as response:
        body = response.read()
        return json.loads(body) if body.startswith(b"{") or body.startswith(b"[") else body


def ready():
    health = get("http://127.0.0.1:8000/health")
    if health.get("status") != "ok":
        return False
    coverage = get("http://127.0.0.1:8000/coverage")
    weather = next(row for row in coverage["entities"] if row["entity"] == "weather")
    if weather["rows"] == 0 or weather["cities"] != 5:
        return False
    if not get("http://127.0.0.1:8000/weather/rome"):
        return False
    if not get("http://127.0.0.1:8000/scores?city=rome"):
        return False
    get("http://agent:8100/health")
    get("http://llm:8080/health")
    get("http://ui:8501/_stcore/health")
    get("http://edge:8080/healthz")
    return True


deadline = time.monotonic() + 480  # first CPU model load can take ~3 minutes
while time.monotonic() < deadline:
    try:
        if ready():
            print("PASS: API, stored forecast, scores, agent, model, UI and edge")
            break
    # AttributeError and TypeError are here because get() returns raw bytes for a
    # body that is not JSON: `health.get(...)` and `coverage["entities"]` then
    # raise, and an uncaught raise abandons the whole retry budget on the first
    # attempt -- the opposite of what a start-up probe should do. A service that
    # answers with an HTML error page while it is still coming up is exactly the
    # case this loop exists for.
    except (OSError, ValueError, KeyError, StopIteration, AttributeError, TypeError):
        pass
    time.sleep(5)
else:
    raise SystemExit("release smoke test failed; inspect docker compose logs")
