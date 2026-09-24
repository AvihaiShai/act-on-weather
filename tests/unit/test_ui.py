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
from datetime import UTC, datetime
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

import forecast  # noqa: E402

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
    """Fixtures are keyed by the whole path, then by its first segment.

    The first segment alone serves `/weather/{city}` and `/records/{entity}/{id}`
    from one entry each, and keeps the fixture a record of shapes rather than of
    a particular city's data. The full path is tried first because
    `/itineraries` and `/itineraries/{id}` return genuinely different shapes --
    a list of titles and dates, and one plan with its days.
    """
    path = urlparse(url).path.strip("/")
    for key in (path, path.split("/")[0]):
        if key in FIXTURES:
            return FIXTURES[key]
    raise AssertionError(f"the UI called /{path}, which the fixture does not cover")


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
    monkeypatch.setattr(forecast, "utc_now", lambda: datetime(2026, 9, 24, 12, tzinfo=UTC))
    # The app caches its reads, and the cache outlives a single AppTest run.
    st.cache_data.clear()

    # Generous: the places map parses a staged city basemap on first render,
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


def test_the_places_map_renders_a_figure(app):
    """A tab that merely rendered its controls would pass the tab test above.

    Assert the figure itself here; `test_places_map.py` verifies that the staged
    basemap geometry reaches it for Rome and London.
    """
    map_tab = next(tab for tab in app.tabs if "Places map" in tab.label)
    assert map_tab.get("plotly_chart"), "the places map rendered no figure"


def test_each_forecast_card_names_the_day_it_came_from(app):
    """The four cards answer four different questions, so they land on four
    different days. Asserting them together is what catches a regression to
    reading all four off one row, which looked right only because the first
    card happened to be the day the other three were silently using."""
    cards = [
        metric
        for metric in app.metric
        if metric.label.split(" · ")[0] in {"Warmest", "Coldest", "Next rain", "Windiest"}
    ]
    assert [(card.label, card.value) for card in cards] == [
        ("Warmest · today", "33°C"),  # 33.3 on the 24th
        ("Coldest · tomorrow", "17°C"),  # 17.2 on the 25th
        ("Next rain", "None"),  # the fixture week is dry
        ("Windiest · Mon 28 Sep", "20 km/h"),  # 19.6 on the 28th
    ]


def test_expired_forecast_has_no_highlight_cards(monkeypatch):
    app = _run(monkeypatch)
    monkeypatch.setattr(forecast, "utc_now", lambda: datetime(2026, 9, 30, 12, tzinfo=UTC))
    app.run()
    assert not any(metric.label.startswith("Warmest") for metric in app.metric)
    assert any("No current forecast" in warning.value for warning in app.warning)


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


def test_the_operator_refresh_tab_is_honest_about_what_it_does(app):
    """F4. The tab prints a command; it does not run one. A page that shows a
    freshness stamp next to a shell command reads as though it had just
    refreshed, so the disclaimer and the absence of a fetch button are the
    assertion."""
    update = next(tab for tab in app.tabs if "Update data" in tab.label)
    code = " ".join(element.value for element in update.get("code"))
    assert "compose.tools.yml run --rm refresh" in code
    assert "docker compose up -d ingestor" in code, "the manual sequence lost its last command"
    assert any("does not fetch" in element.value for element in update.get("warning"))
    assert not [b for b in update.get("button") if "fetch" in b.label.lower()]


def test_the_refresh_tab_shows_freshness_per_city(app):
    """A refresh that only half worked shows up as one city with an older
    as-of. A single global stamp would hide it."""
    update = next(tab for tab in app.tabs if "Update data" in tab.label)
    frames = update.get("arrow_data_frame") or update.get("dataframe")
    assert frames, "the per-city freshness table did not render"
    columns = list(frames[0].value.columns)
    assert {"City", "As of", "Covers to", "State"} <= set(columns)


def test_a_saved_itinerary_can_be_reopened(app):
    """The planner saved trips and then offered no way back into one: the list
    rendered underneath a freshly built plan, so a stored itinerary was visible
    and unopenable. The "Open" button is the way back, and it has to put the
    stored days on screen, not just the title."""
    buttons = [button for button in app.button if button.label == "Open"]
    assert buttons, "the planner offers no way to reopen a saved itinerary"

    reopened = buttons[0].click().run()
    assert not reopened.exception, [element.value for element in reopened.exception]

    text = " ".join(element.value for element in reopened.markdown)
    assert "Two days in Lisbon" in text
    assert "Walking tour" in text, "the stored days did not render"

    captions = " ".join(element.value for element in reopened.caption)
    assert "Saved itinerary" in captions
    assert "revision 1" in captions


def test_a_reopened_itinerary_is_renamed_rather_than_duplicated(app):
    """Re-saving a stored plan would post a new row with a new id on every
    click. The reopened plan offers the M12 patch path instead."""
    reopened = next(button for button in app.button if button.label == "Open").click().run()
    labels = [button.label for button in reopened.button]
    assert "Rename this itinerary" in labels
    assert "Save this itinerary" not in labels


def test_the_saved_list_is_there_before_anything_is_built(app):
    """It is the whole point of the section: a trip saved in an earlier session
    has to be findable on arrival, with no plan in session state."""
    assert any("Saved itineraries" in element.value for element in app.markdown)
