"""A wipe keeps collected messages and refuses to run without their source."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from services.api import main as api
from services.common import config, schemas
from services.common.envelope import Envelope
from services.common.outbox import Outbox
from services.consumer import main as consumer
from services.enricher import main as enricher


def test_wipe_requires_explicit_confirmation(monkeypatch):
    monkeypatch.setattr(api, "accept", lambda key, payload: (key, payload))
    client = TestClient(api.app)

    assert client.post("/user-data/wipe", json={"confirm": "yes"}).status_code == 422
    result = client.post("/user-data/wipe", json={"confirm": "WIPE"})
    assert result.status_code == 202
    assert result.json()["message_id"][0] == config.RK_USER_DATA_WIPE


def test_publisher_does_not_wait_for_wipe_cleanup(monkeypatch, tmp_path):
    box = Outbox(tmp_path / "api.sqlite3")
    envelope = Envelope.create(
        config.RK_USER_DATA_WIPE,
        {"requested_by": "api"},
        source="api",
        observed_at=datetime.now(UTC),
    )
    box.accept(envelope)
    seq = box.conn.execute(
        "SELECT seq FROM outbox WHERE message_id = ?", (envelope.message_id,)
    ).fetchone()[0]
    box.mark_published(seq)
    queued = Envelope.create(
        config.RK_ITINERARY,
        {"id": "later"},
        source="api",
        observed_at=datetime.now(UTC),
    )
    box.accept(queued)

    monkeypatch.setattr(api, "outbox", box)
    monkeypatch.setattr(
        api,
        "_purge_completed_wipes",
        lambda: pytest.fail("wipe cleanup must not run in the publisher thread"),
    )

    class Publisher:
        def __init__(self, **_kwargs):
            pass

        def publish(self, *_args):
            pass

    monkeypatch.setattr(api, "Publisher", Publisher)
    monkeypatch.setattr(api.time, "sleep", lambda _seconds: (_ for _ in ()).throw(StopIteration))
    with pytest.raises(StopIteration):
        api._publisher_loop()
    assert box.status_of(envelope.message_id) is not None
    assert box.published_id(queued.message_id) is not None
    box.close()


def test_wipe_status_waits_for_model_outbox_cleanup(monkeypatch, tmp_path):
    path = tmp_path / "model.sqlite3"
    with Outbox(path) as box:
        box.accept(
            Envelope.create(
                config.RK_LLM_RECOMMENDATION,
                {"activity": "visitor_activity"},
                source="enricher",
                observed_at=datetime.now(UTC),
            )
        )

    wiped_at = datetime.now(UTC) + timedelta(seconds=1)

    class Connection:
        def execute(self, sql, params):
            return self

        def fetchone(self):
            return {"processed_at": wiped_at}

    monkeypatch.setattr(api, "pool", SimpleNamespace(conn=Connection()))
    monkeypatch.setattr(api, "outbox", SimpleNamespace(status_of=lambda _id: None))
    monkeypatch.setattr(api, "Path", lambda _path: path)

    client = TestClient(api.app)
    assert client.get("/user-data/wipe/test-id").json() == {"status": "finishing"}
    with Outbox(path) as box:
        assert box.purge_accepted_through(wiped_at.isoformat()) == 1
    assert client.get("/user-data/wipe/test-id").json() == {"status": "complete"}


def test_wipe_replays_committed_source_and_clears_user_tables(monkeypatch, tmp_path):
    source_path = tmp_path / "source.sqlite3"
    envelope = Envelope.create(
        config.RK_WEATHER,
        {
            "city_id": "rome",
            "forecast_date": "2026-09-24",
            "provider": "open-meteo",
            "temp_max_c": 25.0,
            "as_of": datetime(2026, 9, 23, tzinfo=UTC).isoformat(),
        },
        source="snapshot",
        observed_at=datetime(2026, 9, 23, tzinfo=UTC),
    )
    with Outbox(source_path) as source:
        source.accept(envelope)

    real_outbox = consumer.Outbox
    monkeypatch.setattr(
        consumer, "Outbox", lambda _path, readonly: real_outbox(source_path, readonly=readonly)
    )
    restored = []
    monkeypatch.setattr(consumer, "upsert_weather", lambda cur, p: restored.append(p) or True)
    monkeypatch.setattr(consumer, "score_defaults", lambda cur, p: restored.append("scored"))

    class Cursor:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=None):
            self.statements.append(" ".join(sql.split()))
            return self

        def fetchall(self):
            return [{"message_id": envelope.message_id}]

    cursor = Cursor()
    consumer.wipe_user_data(cursor, schemas.UserDataWipe())

    assert [item for item in restored if item == "scored"] == ["scored"]
    assert restored[0].temp_max_c == 25.0
    assert "SELECT wipe_business_rows()" in cursor.statements
    assert cursor.statements[-1] == "DELETE FROM record_history"


def test_wipe_aborts_before_delete_if_a_collected_envelope_is_missing(monkeypatch, tmp_path):
    source_path = tmp_path / "empty.sqlite3"
    with Outbox(source_path):
        pass
    real_outbox = consumer.Outbox
    monkeypatch.setattr(
        consumer, "Outbox", lambda _path, readonly: real_outbox(source_path, readonly=readonly)
    )

    class Cursor:
        def execute(self, sql, params=None):
            if "wipe_business_rows" in sql or sql.startswith("DELETE"):
                raise AssertionError("no data may be removed when a source message is missing")
            return self

        def fetchall(self):
            return [{"message_id": "missing-source-id"}]

    with pytest.raises(RuntimeError, match="collected envelopes are missing"):
        consumer.wipe_user_data(Cursor(), schemas.UserDataWipe())


def test_outbox_purge_waits_for_publish(tmp_path):
    envelope = Envelope.create(
        config.RK_USER_DATA_WIPE, {}, source="api", observed_at=datetime.now(UTC)
    )
    with Outbox(tmp_path / "api.sqlite3") as box:
        box.accept(envelope)
        seq = next(box.unpublished())["seq"]
        with pytest.raises(RuntimeError, match="unpublished"):
            box.purge_published_through(seq)
        box.mark_published(seq)
        box.purge_published_through(seq)
        assert box.counts() == {"total": 0, "pending": 0}


def test_enricher_discards_pre_wipe_output(tmp_path):
    class Client:
        def chat_json(self, system, prompt, schema):
            return {"recommendation": "A pleasant day for a short walk outdoors."}

    row = {
        "city_id": "rome",
        "city_name": "Rome",
        "forecast_date": datetime(2026, 9, 24, tzinfo=UTC).date(),
        "activity": "walking",
        "activity_label": "walking",
        "score": 70,
        "band": "fair",
        "reasons": [],
        "weather_as_of": datetime(2026, 9, 23, tzinfo=UTC),
    }
    with Outbox(tmp_path / "enricher.sqlite3") as box:
        assert enricher.enrich_one(row, Client(), box, stale=lambda: True) == "skipped"
        assert box.counts() == {"total": 0, "pending": 0}

        assert enricher.enrich_one(row, Client(), box, stale=lambda: False) == "ready"
        assert box.purge_accepted_through(datetime.now(UTC).isoformat()) == 1
        assert box.counts() == {"total": 0, "pending": 0}
