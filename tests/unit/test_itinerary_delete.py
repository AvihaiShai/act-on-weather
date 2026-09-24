"""A saved trip's removal follows the queue and clears its revision history."""

import pytest
from fastapi.testclient import TestClient

from services.api import main as api
from services.common import config, schemas
from services.consumer import main as consumer


def test_delete_accepts_a_queued_request_without_a_database_read(monkeypatch):
    def no_database_read(*args):
        raise AssertionError("DELETE must be accepted while the database is down")

    monkeypatch.setattr(api.queries, "itinerary", no_database_read)
    monkeypatch.setattr(api, "accept", lambda key, payload: (key, payload))

    response = TestClient(api.app).delete("/itineraries/trip-1")

    assert response.status_code == 202
    assert response.json()["message_id"] == [config.RK_ITINERARY_DELETE, {"id": "trip-1"}]


def test_consumer_removes_only_named_trip_and_its_history():
    statements = []

    class Cursor:
        def execute(self, sql, params):
            statements.append((" ".join(sql.split()), params))

    payload = schemas.validate(config.RK_ITINERARY_DELETE, {"id": "trip-1"})
    consumer.delete_itinerary(Cursor(), payload)

    assert statements == [
        ("DELETE FROM itineraries WHERE id = %s", ("trip-1",)),
        (
            "DELETE FROM record_history WHERE entity = 'itineraries' AND entity_id = %s",
            ("trip-1",),
        ),
    ]


def test_delete_payload_rejects_unexpected_fields():
    with pytest.raises(schemas.ValidationError):
        schemas.validate(config.RK_ITINERARY_DELETE, {"id": "trip-1", "all": True})
