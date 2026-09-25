"""Saving a trip is a write, and a write must not depend on the database.

`services/api/main.py`'s module docstring states the rule plainly:

> Writes do not touch the database at all. A POST, PATCH or DELETE is fsynced
> into this service's own outbox and answered `202 Accepted`.

`DELETE /itineraries/{id}` was pinned to that rule by
`test_itinerary_delete.py`. `POST /itineraries` was not, and it read
`queries.coverage(pool.conn)` inside the handler to stamp the plan's `as_of`.
`db.Pool.conn` retries forever, so with Postgres down the POST blocked its
uvicorn worker for the length of the outage while DELETE and PATCH went on
accepting. The first version of this file demonstrated that by hanging rather
than failing: 60 s in the test image with no result.

The stamp now travels with the record. The plan the agent builds already
carries the as-of of the snapshot it scored against, the UI sends it, and the
handler reads nothing. A caller that omits it stores NULL -- see
`test_a_caller_that_omits_as_of_is_accepted_and_stores_nothing_invented`,
which is the compatibility decision and its reason.
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from services.api import main as api
from services.common import config, schemas

STORED_AS_OF = "2026-09-23T18:16:44.438338+00:00"

BODY = {
    "city": "rome",
    "title": "Three days in Rome",
    "start_date": "2026-09-25",
    "end_date": "2026-09-27",
    "days": [{"date": "2026-09-25", "activity_slug": "museums", "activity_score": 71}],
    "as_of": STORED_AS_OF,
}


class Pool:
    """A stand-in for `db.Pool` whose every accessor is a failure.

    Reaching any of them is the defect, so each one raises rather than
    blocking: the regression fails in milliseconds instead of hanging the
    suite, which is what the real `Pool.conn` does during an outage.
    """

    @property
    def conn(self):
        raise AssertionError("the write path must not touch the database")

    def drop(self):
        raise AssertionError("the write path must not touch the database")


@pytest.fixture
def accepted(monkeypatch):
    """Capture what the handler hands the outbox, without writing one."""
    captured = {}

    def accept(key, payload, city=None):
        captured.update(key=key, payload=payload, city=city)
        return "message-1"

    monkeypatch.setattr(api, "accept", accept)
    monkeypatch.setattr(api, "pool", Pool())

    def _post(body=BODY):
        response = TestClient(api.app).post("/itineraries", json=body)
        return response, captured

    return _post


def test_a_save_reaches_the_outbox_without_a_database(accepted):
    """The regression, stated as the rule it broke."""
    response, captured = accepted()

    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert captured["key"] == config.RK_ITINERARY
    assert captured["city"] == "rome"


def test_the_stamp_is_the_one_the_caller_scored_against(accepted):
    """Not the clock, and not whatever the database says now.

    The plan was scored against a snapshot; that snapshot's as-of is what makes
    the saved scores interpretable later. Substituting the time of the save
    would make every saved plan claim to be as fresh as the moment it was
    saved, and the UI's staleness notice could then never fire.
    """
    _response, captured = accepted()

    assert captured["payload"]["as_of"] == STORED_AS_OF


def test_a_caller_that_omits_as_of_is_accepted_and_stores_nothing_invented(accepted):
    """The compatibility decision, and the reason for it.

    Callers written before `as_of` existed keep working: the request is
    accepted and the record stores NULL. It is deliberately not filled in.
    Refusing would break those callers; substituting a clock would put a
    provenance on a stored record that nothing scored the plan against, and of
    the two only a missing timestamp can be read for what it is. The UI renders
    it as "not recorded" -- see `test_ui.py`.
    """
    body = {k: v for k, v in BODY.items() if k != "as_of"}
    response, captured = accepted(body)

    assert response.status_code == 202
    assert captured["payload"]["as_of"] is None

    record = schemas.validate(config.RK_ITINERARY, captured["payload"])
    assert record.as_of is None
    assert record.city_id == "rome"


def test_the_accepted_payload_survives_the_consumer(accepted):
    """A record accepted here is re-validated where it is written.

    If the API accepted a shape `schemas.validate` rejects, the save would be
    acknowledged and then dead-letter -- a data loss no drill would attribute
    to this handler.
    """
    _response, captured = accepted()
    record = schemas.validate(config.RK_ITINERARY, captured["payload"])

    assert record.id
    assert record.as_of == datetime.fromisoformat(STORED_AS_OF)
    assert record.days == BODY["days"]


def test_a_naive_as_of_is_not_silently_treated_as_utc(accepted):
    """A timestamp with no offset is ambiguous, and it is the caller's.

    Asserted so the behaviour is a decision on record rather than whatever
    pydantic happens to do: it is accepted and carried through unchanged, not
    rewritten. The UI always sends an offset.
    """
    _response, captured = accepted({**BODY, "as_of": "2026-09-23T18:16:44"})

    assert captured["payload"]["as_of"] == "2026-09-23T18:16:44"
    assert schemas.validate(config.RK_ITINERARY, captured["payload"]).as_of.tzinfo is None


def test_a_future_stamp_is_not_rejected_here(accepted):
    """Validation of the *value* is not this handler's job, and saying so.

    The handler's contract is that it does not read the database; it therefore
    has nothing to compare a timestamp against. A clock-skewed caller is a real
    possibility and this records that it is accepted rather than silently
    corrected -- correcting it would be inventing provenance again.
    """
    ahead = (datetime.now(UTC).replace(year=2030)).isoformat()
    response, captured = accepted({**BODY, "as_of": ahead})

    assert response.status_code == 202
    assert captured["payload"]["as_of"] == ahead


def test_save_body_rejects_unexpected_fields():
    response = TestClient(api.app).post("/itineraries", json={**BODY, "scored_at": "now"})
    assert response.status_code == 422


def test_save_body_rejects_an_unparseable_as_of():
    response = TestClient(api.app).post("/itineraries", json={**BODY, "as_of": "yesterday"})
    assert response.status_code == 422


@pytest.mark.parametrize("missing", ["city", "title", "start_date", "end_date"])
def test_save_body_requires_the_fields_the_consumer_stores(missing):
    body = {k: v for k, v in BODY.items() if k != missing}
    response = TestClient(api.app).post("/itineraries", json=body)
    assert response.status_code == 422


def test_the_consumer_stores_a_null_as_of_rather_than_refusing_it():
    """The other end of the compatibility decision.

    The column is nullable as of migration 008. If the consumer could not write
    NULL, a caller omitting `as_of` would be accepted at the API and then
    dead-letter -- the failure this decision exists to avoid.
    """
    statements = []

    class Cursor:
        def execute(self, sql, params):
            statements.append((sql, params))

    payload = schemas.validate(
        config.RK_ITINERARY,
        {
            "id": "trip-1",
            "city_id": "rome",
            "title": BODY["title"],
            "start_date": BODY["start_date"],
            "end_date": BODY["end_date"],
            "days": BODY["days"],
        },
    )
    from services.consumer import main as consumer

    consumer.upsert_itinerary(Cursor(), payload)

    assert len(statements) == 1
    assert statements[0][1]["as_of"] is None
