"""Saving a trip is a write, and a write must not depend on the database.

`services/api/main.py`'s own docstring states the rule: "Writes do not touch
the database at all. A POST, PATCH or DELETE is fsynced into this service's
own outbox and answered `202 Accepted`". `DELETE /itineraries/{id}` is pinned
to that rule by `test_itinerary_delete.py`. `POST /itineraries` was not, and
it read `queries.coverage(pool.conn)` inside the handler to stamp the plan's
`as_of`.

Why that is worse than an unenforced docstring: `db.Pool.conn` reconnects
through `db.connect`, which retries *forever* with backoff. With Postgres
down and no live connection, the POST did not fail fast -- it blocked its
uvicorn worker for the length of the outage, while DELETE and PATCH on the
same service went on accepting. The first version of this file demonstrated
that by hanging rather than failing: 60 s in the test image with no result.

The handler now stamps through `scoring_as_of`, which makes one immediate
attempt via `pool.conn_if_up` and falls back to its own clock.
"""

import psycopg
import pytest
from fastapi.testclient import TestClient

from services.api import main as api
from services.common import config, schemas

BODY = {
    "city": "rome",
    "title": "Three days in Rome",
    "start_date": "2026-09-25",
    "end_date": "2026-09-27",
    "days": [{"date": "2026-09-25", "activity_slug": "museums", "activity_score": 71}],
}

STORED_AS_OF = "2026-09-23T18:16:44.438338+00:00"


class Pool:
    """A stand-in for `db.Pool` that cannot touch a real socket.

    `conn` raising rather than blocking is the point: a handler that reaches
    the retrying accessor fails this suite in milliseconds instead of hanging
    it, which is how the defect behaved before the fix.
    """

    def __init__(self, *, up: bool):
        self._up = up
        self.dropped = 0

    @property
    def conn(self):
        raise AssertionError("the write path must not wait on a reconnect")

    @property
    def conn_if_up(self):
        return object() if self._up else None

    def drop(self):
        self.dropped += 1


def _accepted(monkeypatch):
    """Capture what the handler hands the outbox, without writing one."""
    captured = {}

    def accept(key, payload, city=None):
        captured.update(key=key, payload=payload, city=city)
        return "message-1"

    monkeypatch.setattr(api, "accept", accept)
    return captured


def test_save_is_accepted_while_the_database_is_down(monkeypatch):
    """The regression: an unreachable database must not lose the save."""
    monkeypatch.setattr(api, "pool", Pool(up=False))
    monkeypatch.setattr(
        api.queries,
        "coverage",
        lambda conn: pytest.fail("coverage must not be read without a connection"),
    )
    captured = _accepted(monkeypatch)

    response = TestClient(api.app).post("/itineraries", json=BODY)

    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert captured["key"] == config.RK_ITINERARY
    assert captured["city"] == "rome"


def test_save_is_accepted_when_the_coverage_read_itself_fails(monkeypatch):
    """A connection that exists but breaks mid-read is the other half.

    Postgres going away does not mark the socket closed until something uses
    it, so the first write after an outage takes this branch rather than the
    one above.
    """
    pool = Pool(up=True)
    monkeypatch.setattr(api, "pool", pool)

    def unreachable(conn):
        raise psycopg.OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(api.queries, "coverage", unreachable)
    captured = _accepted(monkeypatch)

    response = TestClient(api.app).post("/itineraries", json=BODY)

    assert response.status_code == 202
    assert captured["payload"]["title"] == "Three days in Rome"
    assert pool.dropped == 1, "a broken connection must be dropped, not reused"


def test_save_never_waits_on_a_reconnect(monkeypatch):
    """The defect itself, not its symptom: `pool.conn` is never reached."""
    monkeypatch.setattr(api, "pool", Pool(up=False))
    captured = _accepted(monkeypatch)

    response = TestClient(api.app).post("/itineraries", json=BODY)

    assert response.status_code == 202
    assert captured["payload"]["id"]


def test_a_record_accepted_during_an_outage_still_validates(monkeypatch):
    """A degraded stamp must not manufacture a poison message.

    The payload travels the queue and is re-validated by the consumer. If the
    outage fallback produced something `schemas.validate` rejects, the outage
    would turn a good save into a dead letter -- a data loss the drills would
    not attribute to this handler.
    """
    monkeypatch.setattr(api, "pool", Pool(up=False))
    captured = _accepted(monkeypatch)

    TestClient(api.app).post("/itineraries", json=BODY)
    record = schemas.validate(config.RK_ITINERARY, captured["payload"])

    assert record.id
    assert record.city_id == "rome"
    assert record.as_of is not None
    assert record.days == BODY["days"]


def test_a_readable_database_still_supplies_the_scoring_as_of(monkeypatch):
    """The fallback must not quietly become the normal path.

    When coverage is readable the stamp has to be the forecast's own as-of.
    Otherwise every saved plan claims to have been scored against data as
    fresh as the moment it was saved, and the UI's staleness notice -- which
    compares this value against the current window -- can never fire.
    """
    monkeypatch.setattr(api, "pool", Pool(up=True))
    monkeypatch.setattr(api.queries, "coverage", lambda conn: {"weather_as_of": STORED_AS_OF})
    captured = _accepted(monkeypatch)

    TestClient(api.app).post("/itineraries", json=BODY)

    assert captured["payload"]["as_of"] == STORED_AS_OF


def test_a_datetime_as_of_is_serialised_not_stringified(monkeypatch):
    """`queries.coverage` returns whatever the driver gives it.

    Against real Postgres that is a `datetime`, and `str()` on one produces
    `'2026-09-23 18:16:44.438338+00:00'` -- a space where the ISO `T` belongs.
    The unit suite never connects, so only an explicit case pins this.
    """
    from datetime import UTC, datetime

    stamp = datetime(2026, 9, 23, 18, 16, 44, 438338, tzinfo=UTC)
    monkeypatch.setattr(api, "pool", Pool(up=True))
    monkeypatch.setattr(api.queries, "coverage", lambda conn: {"weather_as_of": stamp})
    captured = _accepted(monkeypatch)

    TestClient(api.app).post("/itineraries", json=BODY)

    assert captured["payload"]["as_of"] == stamp.isoformat()
    assert schemas.validate(config.RK_ITINERARY, captured["payload"]).as_of == stamp


def test_save_body_rejects_unexpected_fields():
    response = TestClient(api.app).post("/itineraries", json={**BODY, "as_of": "now"})
    assert response.status_code == 422


@pytest.mark.parametrize("missing", ["city", "title", "start_date", "end_date"])
def test_save_body_requires_the_fields_the_consumer_stores(missing):
    body = {k: v for k, v in BODY.items() if k != missing}
    response = TestClient(api.app).post("/itineraries", json=body)
    assert response.status_code == 422
