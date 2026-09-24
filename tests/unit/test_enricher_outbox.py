"""Enrichment results remain owed after a broker failure."""

from pika.exceptions import AMQPConnectionError

from services.common.outbox import Outbox
from services.enricher.main import accept_result, drain


class Publisher:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    def publish(self, routing_key, body, message_id):
        if self.fail:
            raise AMQPConnectionError("broker down")
        self.sent.append((routing_key, body, message_id))

    def close(self):
        pass


def test_enricher_result_survives_publish_failure(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    box = Outbox(path)
    accept_result(
        box,
        {
            "city_id": "rome",
            "forecast_date": "2026-09-24",
            "activity": "walking",
            "weather_as_of": "2026-09-23T18:00:00+00:00",
            "status": "ready",
            "text": "A suitable day for walking.",
            "model": "local",
        },
        "rome",
    )
    message_id = next(box.unpublished())["message_id"]
    assert drain(box, Publisher(fail=True)) is False
    box.close()

    reopened = Outbox(path)
    publisher = Publisher()
    assert drain(reopened, publisher) is True
    assert len(publisher.sent) == 1
    assert publisher.sent[0][2] == message_id
    assert reopened.status_of(message_id)["published_at"] is not None
