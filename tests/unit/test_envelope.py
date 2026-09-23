"""The envelope and the payload models.

A payload that fails validation must fail *before* the database is touched, so
that it becomes a dead letter and not a half-written row.
"""

import json

import pytest

from services.common import config, schemas
from services.common.envelope import Envelope


def make(routing_key=config.RK_WEATHER, **payload_overrides):
    payload = {
        "city_id": "rome",
        "forecast_date": "2026-09-24",
        "provider": "open-meteo",
        "temp_max_c": 26.0,
        "as_of": "2026-09-23T18:00:00+00:00",
    }
    payload.update(payload_overrides)
    return Envelope.create(
        routing_key,
        payload,
        source="ingestor",
        observed_at="2026-09-23T18:00:00+00:00",
        city="rome",
    )


def test_round_trip_preserves_the_message_id():
    original = make()
    restored = Envelope.from_bytes(original.to_bytes())
    assert restored.message_id == original.message_id
    assert restored.payload == original.payload
    assert restored.routing_key == config.RK_WEATHER


def test_message_ids_are_unique():
    assert make().message_id != make().message_id


@pytest.mark.parametrize(
    "raw",
    [
        b"not json at all",
        json.dumps({"message_id": "x"}).encode(),
        json.dumps(
            {
                "message_id": "x",
                "schema_version": 1,
                "source": "s",
                "routing_key": "weather.daily",
                "observed_at": "now",
                "payload": "a string",
            }
        ).encode(),
    ],
)
def test_malformed_envelopes_raise(raw):
    # ValueError covers both branches: json.JSONDecodeError subclasses it, and
    # the structural checks raise it directly.
    with pytest.raises(ValueError):
        Envelope.from_bytes(raw)


def test_valid_weather_payload_validates():
    model = schemas.validate(config.RK_WEATHER, make().payload)
    assert model.city_id == "rome"


def test_unknown_field_is_rejected():
    """extra='forbid': a producer that starts sending a field nobody stores is a bug."""
    with pytest.raises(schemas.ValidationError):
        schemas.validate(config.RK_WEATHER, make(surprise=1).payload)


def test_missing_required_field_is_rejected():
    payload = make().payload
    del payload["as_of"]
    with pytest.raises(schemas.ValidationError):
        schemas.validate(config.RK_WEATHER, payload)


def test_unknown_routing_key_is_rejected():
    with pytest.raises(ValueError):
        schemas.validate("weather.hourly", {})


def test_every_routing_key_has_a_model():
    assert set(schemas.PAYLOAD_MODELS) == set(config.ROUTING_KEYS)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Fine Dining!", "fine_dining"),
        ("  surfing ", "surfing"),
        ("Kite-flying", "kite_flying"),
        ("ROCK CLIMBING", "rock_climbing"),
    ],
)
def test_slugify(text, expected):
    assert schemas.slugify(text) == expected


def test_slugify_rejects_empty():
    with pytest.raises(ValueError):
        schemas.slugify("   !!!   ")
