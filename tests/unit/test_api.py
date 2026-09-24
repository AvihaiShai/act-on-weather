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
