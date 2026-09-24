"""The UI renders offline, with no exception on any page.

The UI is the largest single file in the repo and the one a reviewer looks at
first, yet until now nothing rendered it except a person clicking. `AppTest`
runs the real `services/ui/app.py` in-process, so a broken chart, a renamed
API field or a page that raises is caught here rather than in a demo.

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

PAGES = [
    "dashboard",
    "forecast",
    "suitability",
    "trip-planner",
    "places-map",
    "ask-the-agent",
    "update-data",
    "data-coverage",
    "monitoring",
]


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _payload_for(url: str, overrides: dict | None = None):
    """Fixtures are keyed by the whole path, then by its first segment.

    The first segment alone serves `/weather/{city}` and `/records/{entity}/{id}`
    from one entry each, and keeps the fixture a record of shapes rather than of
    a particular city's data. The full path is tried first because
    `/itineraries` and `/itineraries/{id}` return genuinely different shapes --
    a list of titles and dates, and one plan with its days.

    `overrides` replaces one path for one test. The fixture holds a single
    example per endpoint, and a couple of states are only interesting as the
    other case -- `/refresh/last` with nothing recorded, for instance.
    """
    path = urlparse(url).path.strip("/")
    if overrides and path in overrides:
        return overrides[path]
    for key in (path, path.split("/")[0]):
        if key in FIXTURES:
            return FIXTURES[key]
    raise AssertionError(f"the UI called /{path}, which the fixture does not cover")


def _run(monkeypatch, *, offline=False, overrides=None) -> AppTest:
    import requests

    def fake_get(url, params=None, timeout=None, **kwargs):
        if offline:
            raise requests.ConnectionError("no route to the API")
        return FakeResponse(_payload_for(url, overrides))

    def fake_request(method, url, json=None, timeout=None, **kwargs):
        if offline:
            raise requests.ConnectionError("no route to the API")
        return FakeResponse({"accepted": True, "message_id": "test-message-id"}, 202)

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "request", fake_request)
    monkeypatch.setattr(forecast, "utc_now", lambda: datetime(2026, 9, 24, 12, tzinfo=UTC))
    # The app caches its reads, and the cache outlives a single AppTest run.
    st.cache_data.clear()

    # The map can take longer to render on a CPU runner.
    return AppTest.from_file(str(APP), default_timeout=120).run()


@pytest.fixture
def app(monkeypatch) -> AppTest:
    return _run(monkeypatch)


def _open_page(app: AppTest, page: str) -> AppTest:
    return app.radio[0].set_value(page).run()


def _update_tab(app: AppTest):
    app = _open_page(app, "update-data")
    return next(tab for tab in app.tabs if "Operator refresh" in tab.label)


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders_without_exception(app, page):
    app = _open_page(app, page)
    assert not app.exception, [element.value for element in app.exception]
    assert app.radio[0].value == page
    assert app.markdown, f"the {page!r} page rendered nothing"


def test_page_selection_is_restored_from_the_url(app):
    app = _open_page(app, "ask-the-agent")
    assert "ask-the-agent" in str(app.query_params.get("page"))

    reloaded = AppTest.from_file(str(APP), default_timeout=120)
    reloaded.query_params["page"] = "ask-the-agent"
    reloaded.run()
    assert not reloaded.exception, [element.value for element in reloaded.exception]
    assert reloaded.radio[0].value == "ask-the-agent"


def test_dashboard_shows_activity_comparison(app):
    assert app.radio[0].value == "dashboard"
    assert app.get("plotly_chart"), "the dashboard rendered no activity chart"
    assert app.get("dataframe"), "the dashboard rendered no activity summary"


def test_monitoring_page_links_to_all_dashboards(app):
    app = _open_page(app, "monitoring")
    content = "\n".join(element.value for element in app.markdown)
    assert "http://127.0.0.1:3000/d/aow-service-health" in content
    assert "http://127.0.0.1:3000/d/aow-pipeline" in content
    assert "http://127.0.0.1:3000/d/aow-llm-observability" in content


def test_the_places_map_renders_a_figure(app):
    app = _open_page(app, "places-map")
    assert app.get("plotly_chart"), "the places map rendered no figure"


def test_each_forecast_card_names_the_day_it_came_from(app):
    """The four cards answer four different questions, so they land on four
    different days. Asserting them together is what catches a regression to
    reading all four off one row, which looked right only because the first
    card happened to be the day the other three were silently using."""
    app = _open_page(app, "forecast")
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
    app = _open_page(_run(monkeypatch), "forecast")
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


def test_the_status_footer_renders_so_nothing_stopped_early(app):
    """`api_get` calls `st.stop()` on an API error, which ends the run quietly
    and leaves everything after it blank rather than raising. The footer is
    drawn after the last tab, so its presence is what proves the whole script
    ran."""
    captions = " ".join(element.value for element in app.caption)
    assert "Updates in progress:" in captions


def test_an_unreachable_api_is_reported_not_crashed(monkeypatch):
    """The reviewer's first run may well be against a stack that is still
    starting. That has to read as a message, not a stack trace."""
    at = _run(monkeypatch, offline=True)
    assert not at.exception, [e.value for e in at.exception]
    assert any("unreachable" in error.value for error in at.error)


def test_a_saved_itinerary_can_be_reopened(app):
    """The planner saved trips and then offered no way back into one: the list
    rendered underneath a freshly built plan, so a stored itinerary was visible
    and unopenable. The "Open" button is the way back, and it has to put the
    stored days on screen, not just the title."""
    app = _open_page(app, "trip-planner")
    buttons = [button for button in app.button if button.label == "Open"]
    assert buttons, "the planner offers no way to reopen a saved itinerary"

    reopened = buttons[0].click().run()
    assert not reopened.exception, [element.value for element in reopened.exception]

    text = " ".join(element.value for element in reopened.markdown)
    assert "Two days in Lisbon" in text
    assert "Walking tour" in text, "the stored days did not render"

    captions = " ".join(element.value for element in reopened.caption)
    assert "Saved itinerary" in captions
    assert "Updated" in captions


def test_a_reopened_itinerary_is_renamed_rather_than_duplicated(app):
    """Re-saving a stored plan would post a new row with a new id on every
    click. The reopened plan offers the M12 patch path instead."""
    app = _open_page(app, "trip-planner")
    reopened = next(button for button in app.button if button.label == "Open").click().run()
    labels = [button.label for button in reopened.button]
    assert "Rename this itinerary" in labels
    assert "Save this itinerary" not in labels


def test_the_saved_list_is_there_before_anything_is_built(app):
    """It is the whole point of the section: a trip saved in an earlier session
    has to be findable on arrival, with no plan in session state."""
    app = _open_page(app, "trip-planner")
    assert any("Saved itineraries" in element.value for element in app.markdown)


def test_the_operator_refresh_tab_is_honest_about_what_it_does(app):
    """F4. The tab prints a command; it does not run one. A page that shows a
    freshness stamp next to a shell command reads as though it had just
    refreshed, so the disclaimer and the absence of a fetch button are the
    assertion."""
    update = _update_tab(app)
    code = " ".join(element.value for element in update.get("code"))
    assert "compose.tools.yml run --rm refresh" in code
    assert "docker compose up -d ingestor" in code, "the manual sequence lost its last command"
    assert any("does not fetch" in element.value for element in update.get("warning"))
    assert not [b for b in update.get("button") if "fetch" in b.label.lower()]


def test_the_refresh_tab_shows_freshness_per_city(app):
    """A refresh that only half worked shows up as one city with an older
    as-of. A single global stamp would hide it."""
    update = _update_tab(app)
    frame = _frames(update)[0].value
    assert {"City", "As of", "Covers to", "State"} <= set(frame.columns)


def _frames(tab):
    frames = list(tab.get("arrow_data_frame")) or list(tab.get("dataframe"))
    assert frames, "the tab rendered no dataframe"
    return frames


def test_the_refresh_tab_keeps_stored_freshness_and_the_last_run_apart(app):
    """F4. These answer different questions and the tab used to show only the
    first, which let a failed refresh hide behind data that still looked fresh.
    Two numbered sections, from two different sources, and the page says which is
    which."""
    update = _update_tab(app)
    text = " ".join(element.value for element in list(update.markdown) + list(update.caption))
    assert "1. What is stored right now" in text
    assert "2. What the last run of that command did" in text
    assert "3. The operator command" in text
    assert "state of the data, not the outcome of any particular refresh" in text
    # Two tables, in that order: stored state, then the run.
    assert len(_frames(update)) >= 2


def test_the_last_run_report_names_the_city_the_provider_refused(app):
    """The fixture is a run where four cities refreshed and Reykjavik did not.
    Its stored as-of is unchanged, so the only place that failure appears is the
    run report -- which is the whole reason the report exists."""
    update = _update_tab(app)
    run_table = _frames(update)[1].value
    assert {"City", "Result", "As of before", "As of after", "Why not"} <= set(run_table.columns)

    row = run_table[run_table["City"] == "Reykjavik"].iloc[0]
    assert row["Result"] == "FAILED"
    assert row["As of before"] == row["As of after"], "a refused city's as-of must not move"
    assert "ConnectionError" in row["Why not"]

    rome = run_table[run_table["City"] == "Rome"].iloc[0]
    assert rome["Result"] == "fetched"
    assert rome["As of before"] != rome["As of after"]


def test_the_last_run_report_states_whether_the_egress_window_closed(app):
    """The one claim the command exists to make gets a card of its own."""
    update = _update_tab(app)
    window = next(m for m in update.get("metric") if m.label == "Egress window")
    assert window.value == "closed"


def test_the_window_deadline_is_stated_either_way(monkeypatch, app):
    """A bounded window and an unbounded one must not read the same. The fixture
    run had a guard; a run whose guard could not start has to say so, because
    then only the trap closed the window and `kill -9` beats a trap."""
    update = _update_tab(app)
    captions = " ".join(element.value for element in update.caption)
    assert "600s hard limit" in captions
    assert "could not have outlived the command" in captions

    unguarded = json.loads(json.dumps(FIXTURES["refresh/last"]))
    unguarded["report"]["egress_window"]["deadline_seconds"] = None
    at = _run(monkeypatch, overrides={"refresh/last": unguarded})
    assert not at.exception, [e.value for e in at.exception]
    tab = _update_tab(at)
    assert any("no deadline guard on this run" in c.value for c in tab.caption)


def test_a_failed_run_is_not_reported_as_a_success(app):
    update = _update_tab(app)
    errors = " ".join(element.value for element in update.get("error"))
    assert "provider refused at least one city" in errors
    assert "exit code" in errors
    assert not update.get("success"), "a run that exited 2 must not render a success banner"


def test_nothing_recorded_reads_as_nothing_recorded(monkeypatch):
    """A fresh install has never refreshed. That must not look like a failure, and
    it must not look like a success either."""
    at = _run(monkeypatch, overrides={"refresh/last": {"recorded": False}})
    assert not at.exception, [e.value for e in at.exception]
    update = _update_tab(at)
    info = " ".join(element.value for element in update.get("info"))
    assert "No refresh run has been recorded on this stack" in info
    assert "not that a refresh failed" in info
    assert not update.get("error")


def test_saved_trip_delete_is_visible_without_opening_a_trip(app):
    app = _open_page(app, "trip-planner")
    delete = next(
        button for button in app.button if button.label == "Delete selected saved itinerary"
    )
    assert delete.disabled

    confirm = next(box for box in app.checkbox if box.label.startswith("Confirm removal"))
    app = confirm.set_value(True).run()
    delete = next(
        button for button in app.button if button.label == "Delete selected saved itinerary"
    )
    assert not delete.disabled

    app = delete.click().run()
    assert not app.exception, [element.value for element in app.exception]
    assert any("Removal requested" in element.value for element in app.success)


def test_wipe_all_user_data_requires_confirmation(app):
    app = _open_page(app, "update-data")
    wipe = next(button for button in app.button if button.label == "Wipe all user data")
    assert wipe.disabled

    confirmation = next(field for field in app.text_input if field.label == "Type WIPE to confirm")
    app = confirmation.set_value("WIPE").run()
    wipe = next(button for button in app.button if button.label == "Wipe all user data")
    assert not wipe.disabled

    app = wipe.click().run()
    assert not app.exception, [element.value for element in app.exception]
    assert any("User data wiped" in element.value for element in app.success)


def test_checking_for_updates_says_what_it_found(app):
    """The footer button clears the read cache and reruns. When nothing has
    changed -- the normal case -- the redrawn screen is identical, so without
    a word back the click looks like a dead button."""
    buttons = [button for button in app.button if button.label == "Check for updates"]
    assert buttons, "the footer offers no way to re-read the API"

    checked = buttons[0].click().run()
    assert not checked.exception, [element.value for element in checked.exception]
    assert [toast.value for toast in checked.toast] == ["Checked. Everything on screen is current."]
    captions = " ".join(element.value for element in checked.caption)
    assert "last checked 12:00 UTC" in captions
