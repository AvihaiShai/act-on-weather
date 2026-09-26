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
    if get("http://agent:8100/health").get("status") != "ok":
        return False
    if get("http://llm:8080/health").get("status") != "ok":
        return False
    if get("http://ui:8501/_stcore/health").strip() != b"ok":
        return False
    # /healthz is answered by nginx itself. These requests cross the actual
    # proxy route the operator's browser and API client use.
    if get("http://edge:8000/health").get("status") != "ok":
        return False
    if not get("http://edge:8000/weather/rome"):
        return False
    return b"streamlit" in get("http://edge:8080/").lower()


def main():
    deadline = time.monotonic() + 480  # first CPU model load can take ~3 minutes
    while time.monotonic() < deadline:
        try:
            if ready():
                print("PASS: stored forecast and scores; agent, model, UI and edge routes respond")
                return
        # Startup can temporarily return HTML, an empty response or no response.
        except (OSError, ValueError, KeyError, StopIteration, AttributeError, TypeError):
            pass
        time.sleep(5)
    raise SystemExit("release smoke test failed; inspect docker compose logs")


if __name__ == "__main__":
    main()
