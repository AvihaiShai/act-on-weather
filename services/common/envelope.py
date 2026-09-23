"""The message envelope every record travels in.

One shape for weather, places, facts, events, recommendations, itineraries and
user patches, so the consumer has exactly one thing to parse and one place to
find the idempotency key.

    message_id      uuid4, the idempotency key; survives redelivery
    schema_version  bumped when the envelope itself changes
    source          who produced it: 'ingestor', 'api', 'enricher'
    routing_key     what kind of record the payload is
    city            city slug, or None for records that have no city
    observed_at     when the upstream source produced the data ("as of")
    ingested_at     when this system accepted it
    payload         the record itself
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def now_utc() -> datetime:
    return datetime.now(UTC)


def new_message_id() -> str:
    return str(uuid.uuid4())


def _iso(value: datetime | str) -> str:
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


@dataclass
class Envelope:
    routing_key: str
    payload: dict[str, Any]
    source: str
    observed_at: str
    city: str | None = None
    message_id: str = field(default_factory=new_message_id)
    schema_version: int = 1
    ingested_at: str = field(default_factory=lambda: _iso(now_utc()))

    @classmethod
    def create(
        cls,
        routing_key: str,
        payload: dict[str, Any],
        *,
        source: str,
        observed_at: datetime | str,
        city: str | None = None,
        message_id: str | None = None,
    ) -> Envelope:
        return cls(
            routing_key=routing_key,
            payload=payload,
            source=source,
            observed_at=_iso(observed_at),
            city=city,
            message_id=message_id or new_message_id(),
        )

    def to_bytes(self) -> bytes:
        return json.dumps(self.as_dict(), separators=(",", ":"), sort_keys=True).encode()

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "schema_version": self.schema_version,
            "source": self.source,
            "routing_key": self.routing_key,
            "city": self.city,
            "observed_at": self.observed_at,
            "ingested_at": self.ingested_at,
            "payload": self.payload,
        }

    @classmethod
    def from_bytes(cls, raw: bytes) -> Envelope:
        """Parse, or raise. A raise here is a poison message and goes to the DLQ."""
        data = json.loads(raw.decode())
        if not isinstance(data, dict):
            raise ValueError("envelope is not an object")
        missing = [
            k
            for k in (
                "message_id",
                "schema_version",
                "source",
                "routing_key",
                "observed_at",
                "payload",
            )
            if k not in data
        ]
        if missing:
            raise ValueError(f"envelope missing fields: {', '.join(missing)}")
        if not isinstance(data["payload"], dict):
            raise ValueError("envelope payload is not an object")
        return cls(
            routing_key=data["routing_key"],
            payload=data["payload"],
            source=data["source"],
            observed_at=data["observed_at"],
            city=data.get("city"),
            message_id=data["message_id"],
            schema_version=int(data["schema_version"]),
            ingested_at=data.get("ingested_at") or _iso(now_utc()),
        )
