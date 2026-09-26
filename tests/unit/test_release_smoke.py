"""The release gate must cross both edge proxy routes before it passes."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("release_smoke", ROOT / "scripts/release-smoke.py")
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def responses():
    return {
        "http://127.0.0.1:8000/health": {"status": "ok"},
        "http://127.0.0.1:8000/coverage": {
            "entities": [{"entity": "weather", "rows": 50, "cities": 5}]
        },
        "http://127.0.0.1:8000/weather/rome": [{"forecast_date": "2026-09-26"}],
        "http://127.0.0.1:8000/scores?city=rome": [{"score": 3}],
        "http://agent:8100/health": {"status": "ok"},
        "http://llm:8080/health": {"status": "ok"},
        "http://ui:8501/_stcore/health": b"ok",
        "http://edge:8000/health": {"status": "ok"},
        "http://edge:8000/weather/rome": [{"forecast_date": "2026-09-26"}],
        "http://edge:8080/": b"<html><title>Streamlit</title></html>",
    }


def test_release_smoke_reaches_both_proxy_routes(monkeypatch):
    pages = responses()
    seen = []

    def get(url):
        seen.append(url)
        return pages[url]

    monkeypatch.setattr(smoke, "get", get)
    assert smoke.ready()
    assert "http://edge:8000/weather/rome" in seen
    assert "http://edge:8080/" in seen


def test_direct_service_health_cannot_hide_a_broken_proxy(monkeypatch):
    pages = responses()
    pages["http://edge:8000/weather/rome"] = []
    monkeypatch.setattr(smoke, "get", pages.__getitem__)
    assert not smoke.ready()

    pages["http://edge:8000/weather/rome"] = [{"forecast_date": "2026-09-26"}]
    pages["http://edge:8080/"] = b"nginx default page"
    assert not smoke.ready()
