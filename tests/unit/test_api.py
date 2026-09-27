"""The API's request contract.

A reviewer probing an API sends a field that looks plausible and watches what
happens. Pydantic's default is to drop an unrecognised key, so the request
succeeds having quietly ignored it -- `{"days": 2}` on an itinerary request
returned a one-day plan with a 200. These tests pin the opposite: an unknown
field is a 422 that names the field.

No database and no broker: validation runs before the handler body, and the
one endpoint tested past validation writes only to the local outbox. The
TestClient is built without its context manager on purpose, so the startup
event -- which would launch the publisher thread and dial RabbitMQ -- never
fires.
"""

import pytest
from fastapi.testclient import TestClient

from services.api.main import app
from services.common import refresh_state

client = TestClient(app)


# Every write endpoint, with a body that is valid except for one invented
# field. `days` is the real case from the itinerary form; the others are the
# same mistake spelled differently.
UNKNOWN_FIELD_CASES = [
    ("POST", "/agent/itinerary", {"city": "rome", "days": 2}, "days"),
    ("POST", "/agent/ask", {"question": "is it raining?", "city": "rome"}, "city"),
    (
        "POST",
        "/recommendations",
        {"city": "rome", "forecast_date": "2026-09-24", "activity": "a picnic", "score": 90},
        "score",
    ),
    ("POST", "/reenrich", {"city": "rome", "include_pending": True}, "include_pending"),
    (
        "POST",
        "/itineraries",
        {
            "city": "rome",
            "title": "Three days",
            "start_date": "2026-09-24",
            "end_date": "2026-09-26",
            "pace": "varied",
        },
        "pace",
    ),
]


@pytest.mark.parametrize(("method", "path", "body", "field"), UNKNOWN_FIELD_CASES)
def test_unknown_field_is_refused_not_ignored(method, path, body, field):
    response = client.request(method, path, json=body)
    assert response.status_code == 422, response.text
    assert field in response.text


def test_valid_body_is_still_accepted():
    """The guard refuses extras; it must not refuse the real shape."""
    response = client.post(
        "/recommendations",
        json={"city": "rome", "forecast_date": "2026-09-24", "activity": "a rooftop picnic"},
    )
    assert response.status_code == 202, response.text
    assert response.json()["message_id"]


# ------------------------------- the activity slug floor, on the write path ----
#
# `POST /recommendations` gained a second 422 when the read path's floor was made
# a shared constant, and the route is public, so the contract change belongs in
# this file rather than only in the slug round-trip tests. There are three
# distinct refusals on one field and they are told apart here, because the
# interesting one is reachable only through a gap between the other two:
# `RecommendationRequestIn.activity` carries `min_length=2` on the text the
# traveller TYPED, and the floor below applies to the slug that text normalises
# to. Any name that is filler plus one character satisfies the first and fails the
# second.


@pytest.mark.parametrize("activity", ["a x", "go x", "the 7", "  an  q  "])
def test_a_name_that_normalises_to_one_character_is_refused(activity):
    """The new 422. `slugify` trims leading and trailing filler, so "a x" is
    stored as `x` -- a key the agent's own floor then discards as too short, which
    made the row unreachable by the very question that asked for it. Refusing it
    at the door is what keeps the two floors from disagreeing."""
    response = client.post(
        "/recommendations",
        json={"city": "rome", "forecast_date": "2026-09-24", "activity": activity},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    # An HTTPException, so `detail` is a flat string -- not pydantic's list. The
    # message names the slug and the floor, because "invalid activity" sends the
    # caller back to the field they typed rather than to what became of it.
    assert "after normalisation" in body["detail"], body
    assert "at least 2 are needed" in body["detail"], body


def test_a_name_that_is_nothing_but_filler_is_refused_differently():
    """The bound on the case above, and the reason both messages exist. Nothing
    survives the trim at all, so there is no slug to report a length for, and
    `slugify` raises before the floor is reached."""
    response = client.post(
        "/recommendations",
        json={"city": "rome", "forecast_date": "2026-09-24", "activity": "a go"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "activity is empty after normalisation"


def test_a_two_character_name_is_still_accepted():
    """The floor is two, not three, and it is measured on the slug. A reviewer
    probing the edge gets a 202 here -- a write path stricter than the read path
    would refuse an activity the system is perfectly able to score."""
    response = client.post(
        "/recommendations",
        json={"city": "rome", "forecast_date": "2026-09-24", "activity": "a 5k"},
    )
    assert response.status_code == 202, response.text
    assert response.json()["activity"] == "5k"


def test_record_patch_body_stays_open():
    """M12's edit path is the deliberate exception.

    `fields` is a column map over five entities, so the allowed keys live with
    the consumer's `PATCHABLE` allow-list -- an unlisted column becomes a
    poison message there, which is visible in the DLQ, rather than a silent
    no-op here.
    """
    response = client.patch("/records/places/rome-colosseum", json={"summary": "corrected"})
    assert response.status_code == 202, response.text
    assert response.json()["message_id"]


# ------------------------------------------------- the last refresh run (F4) --
# `GET /refresh/last` is the UI's only source for what the operator refresh
# actually did. It reads one JSON file from a read-only volume, so it needs no
# database and belongs here with the rest of the request contract.


def test_no_recorded_refresh_is_a_200_not_a_404(monkeypatch, tmp_path):
    """A fresh install has never run a refresh. That is a normal state the UI has
    something to say about, so it is a body and not an error."""
    monkeypatch.setattr(refresh_state.config, "REFRESH_STATE_PATH", tmp_path / "absent.json")
    response = client.get("/refresh/last")
    assert response.status_code == 200, response.text
    assert response.json() == {"recorded": False}


def test_a_recorded_refresh_is_served_back(monkeypatch, tmp_path):
    report = {"outcome": "cities-failed", "exit_code": 2, "failed_cities": ["reykjavik"]}
    target = tmp_path / "last-run.json"
    refresh_state.write(report, target)
    monkeypatch.setattr(refresh_state.config, "REFRESH_STATE_PATH", target)

    body = client.get("/refresh/last").json()
    assert body["recorded"] is True
    assert body["report"]["outcome"] == "cities-failed"


def test_a_corrupt_report_reads_as_nothing_recorded(monkeypatch, tmp_path):
    """The route is called on every UI rerun. A hand-edited or truncated file must
    not turn the Update tab into a 500."""
    target = tmp_path / "last-run.json"
    target.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(refresh_state.config, "REFRESH_STATE_PATH", target)
    assert client.get("/refresh/last").json() == {"recorded": False}


def test_there_is_no_route_that_starts_a_refresh():
    """The whole design of F4: the only way to open an egress window is to run a
    command on the Docker host. Nothing reachable over HTTP may start one, so the
    only route with `refresh` in its path is this read-only GET."""
    refresh_routes = {
        (route.path, tuple(sorted(route.methods)))
        for route in app.routes
        if getattr(route, "methods", None) and "refresh" in route.path
    }
    assert refresh_routes == {("/refresh/last", ("GET",))}
