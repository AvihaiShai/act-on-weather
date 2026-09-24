"""The UI renders, offline, with no exception in any tab.

The UI is the largest single file in the repo and the one a reviewer looks at
first, yet until now nothing rendered it except a person clicking. `AppTest`
runs the real `services/ui/app.py` in-process, so a broken chart, a renamed
API field or a tab that raises is caught here rather than in a demo.

The API is stubbed from `tests/fixtures/ui_api.json`, captured from the running
stack and trimmed. The stub refuses any path the fixture does not cover, so an
endpoint added to the UI without a fixture fails loudly instead of rendering an
empty tab. Nothing here opens a socket: the test runs under `--network none`
in CI, which is the same rule the runtime follows.

What this does not do is check that the fixture still matches the API. That is
`tests/integration/smoke.py`'s job, against the real thing.
"""

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[2]
UI_DIR = ROOT / "services" / "ui"
APP = UI_DIR / "app.py"
FIXTURES = json.loads((ROOT / "tests" / "fixtures" / "ui_api.json").read_text(encoding="utf-8"))

# `app.py` imports its siblings as top-level modules, the way `streamlit run`
# from that directory does. AppTest does not set that up, so we do.
if str(UI_DIR) not in sys.path:
    sys.path.insert(0, str(UI_DIR))


# The seven tabs across the top of the app. `app.tabs` also returns the three
# nested ones inside "Update data", so these are matched by label rather than
# counted.
TOP_LEVEL_TABS = [
    "Forecast",
    "Suitability",
    "Trip planner",
    "Places map",
    "Ask the agent",
    "Update data",
    "Data coverage",
]


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _payload_for(url: str):
    """Fixtures are keyed by the first path segment.

    That is enough to serve `/weather/{city}` and `/records/{entity}/{id}` from
    one entry each, and it keeps the fixture a record of shapes rather than of
    a particular city's data.
    """
    path = urlparse(url).path
    key = path.strip("/").split("/")[0]
    if key not in FIXTURES:
        raise AssertionError(f"the UI called {path}, which the fixture does not cover")
    return FIXTURES[key]


def _run(monkeypatch, *, offline=False) -> AppTest:
    import requests

    def fake_get(url, params=None, timeout=None, **kwargs):
        if offline:
            raise requests.ConnectionError("no route to the API")
        return FakeResponse(_payload_for(url))

    def fake_request(method, url, json=None, timeout=None, **kwargs):
        if offline:
            raise requests.ConnectionError("no route to the API")
        return FakeResponse({"accepted": True, "message_id": "test-message-id"}, 202)

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "request", fake_request)
    # The app caches its reads, and the cache outlives a single AppTest run.
    st.cache_data.clear()

    # Generous: the places map parses a 3 MB coastline file on first render,
    # and CPU runners are slow. A timeout here would be a flaky failure, not a
    # useful one.
    return AppTest.from_file(str(APP), default_timeout=120).run()


@pytest.fixture
def app(monkeypatch) -> AppTest:
    return _run(monkeypatch)


@pytest.mark.parametrize("name", TOP_LEVEL_TABS)
def test_every_tab_renders_without_exception(app, name):
    assert not app.exception, [element.value for element in app.exception]
    matching = [tab for tab in app.tabs if name in tab.label]
    assert matching, f"no tab is labelled {name!r}"
    assert matching[0].children, f"the {name!r} tab rendered nothing"


def test_the_places_map_draws_from_the_bundled_coastline(app):
    """M10's map is a plotly figure over a coastline file shipped in the image,
    not a tile server. A tab that merely rendered its controls would still pass
    the tab test above, so the figure itself is asserted here."""
    map_tab = next(tab for tab in app.tabs if "Places map" in tab.label)
    assert map_tab.get("plotly_chart"), "the places map rendered no figure"


def test_the_as_of_stamp_is_in_the_header(app):
    """The one rule the UI has: nothing is shown without the stamp of the data
    behind it. If the header ever loses it, a stale snapshot starts looking
    like live data."""
    header = " ".join(element.value for element in app.markdown)
    assert "Weather as of" in header
    assert "Forecast covers" in header


def test_the_outbox_footer_renders_so_nothing_stopped_early(app):
    """`api_get` calls `st.stop()` on an API error, which ends the run quietly
    and leaves everything after it blank rather than raising. The footer is
    drawn after the last tab, so its presence is what proves the whole script
    ran."""
    captions = " ".join(element.value for element in app.caption)
    assert "Outbox:" in captions


def test_an_unreachable_api_is_reported_not_crashed(monkeypatch):
    """The reviewer's first run may well be against a stack that is still
    starting. That has to read as a message, not a stack trace."""
    at = _run(monkeypatch, offline=True)
    assert not at.exception, [e.value for e in at.exception]
    assert any("unreachable" in error.value for error in at.error)
