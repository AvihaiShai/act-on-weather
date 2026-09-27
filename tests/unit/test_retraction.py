"""R9(c): withdrawing a collected record from the published output.

The defect these guard. Records here are revised, never deleted, and the
writer holds no DELETE grant reaching an event, a place or a fact. That left
no way out for a record which turned out to be wrong after it was collected:
removing its line from the snapshot produces no message, so an install that
already stored the row kept serving it. A dress rehearsal found two listings
the project's own recheck had verified as cancelled still being served.

The end-to-end proof is `tests/integration/retraction.py`, which needs a real
database. What is checkable here is everything that does not: the payload
contract, the routing, the SQL the consumer builds, the wipe's behaviour, and
-- the one most likely to rot -- that every published read filters retracted
rows. That last one is a source assertion rather than a database one on
purpose: a new read added without the filter is exactly how this defect would
come back, and no unit test with a mocked cursor would notice.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.common import config, schemas
from services.consumer import main as consumer

REPO = Path(__file__).resolve().parents[2]
QUERIES = (REPO / "services" / "common" / "queries.py").read_text(encoding="utf-8")
MIGRATION = REPO / "db" / "migrations" / "009_record_retraction.sql"


class Cursor:
    """Records the SQL the handler builds, and what it claims to have hit."""

    def __init__(self, rowcount: int = 1, rows: list | None = None):
        self.rowcount = rowcount
        self.writes: list[tuple[str, object]] = []
        self._rows = rows or []

    def execute(self, sql, params=None):
        self.writes.append((" ".join(sql.split()), params))
        return self

    def fetchall(self):
        return self._rows


def retraction(entity: str = "events", entity_id: str = "theo2:x") -> schemas.RecordRetraction:
    return schemas.RecordRetraction(
        entity=entity,
        entity_id=entity_id,
        reason="the venue cancelled it",
        retracted_at=datetime(2026, 9, 27, 12, 0, tzinfo=UTC),
    )


# ------------------------------------------------------------- the payload ----


def test_a_withdrawal_must_say_why():
    """The one change nobody can reconstruct from the data afterwards.

    The row a retraction refers to still reads exactly as it did, so if the
    reason is not on the message it exists nowhere.
    """
    with pytest.raises(ValidationError):
        schemas.RecordRetraction(
            entity="events", entity_id="x", reason="", retracted_at=datetime.now(UTC)
        )
    with pytest.raises(ValidationError):
        schemas.RecordRetraction(entity="events", entity_id="x", retracted_at=datetime.now(UTC))


def test_only_collected_entities_can_be_retracted():
    """Weather is replaced day by day, a recommendation is derived rather than
    collected, and an itinerary is the user's own and has its own delete."""
    for entity in ("events", "places", "facts"):
        assert retraction(entity).entity == entity
    for entity in ("weather_daily", "recommendations", "itineraries", "cities"):
        with pytest.raises(ValidationError):
            retraction(entity)


def test_the_decision_date_travels_on_the_message():
    """Never `now()` at the consumer: replaying the queue must not restamp a
    withdrawal with the clock of the replay."""
    assert "retracted_at" in schemas.RecordRetraction.model_fields
    assert schemas.RecordRetraction.model_fields["retracted_at"].is_required()


def test_the_routing_key_is_declared_and_validates():
    assert config.RK_RETRACT in config.ROUTING_KEYS
    parsed = schemas.validate(config.RK_RETRACT, json.loads(retraction().model_dump_json()))
    assert isinstance(parsed, schemas.RecordRetraction)


# ------------------------------------------------------------- the handler ----


def test_the_handler_marks_rather_than_deletes():
    """An UPDATE, so it needs no DELETE grant, and the row keeps its source,
    its as-of and its history."""
    cursor = Cursor()

    consumer.apply_retraction(cursor, retraction())

    [(sql, params)] = cursor.writes
    assert sql.startswith("UPDATE events SET retracted_at")
    assert "DELETE" not in sql.upper()
    assert params["id"] == "theo2:x"
    assert params["at"] == datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    assert params["reason"] == "the venue cancelled it"


def test_an_already_retracted_row_is_not_restamped():
    """Without the guard every rebuild would move the date a withdrawal was
    decided on to the date of the rebuild."""
    cursor = Cursor()

    consumer.apply_retraction(cursor, retraction())

    [(sql, _params)] = cursor.writes
    assert "retracted_at IS NULL" in sql


def test_a_retraction_for_an_unknown_record_is_not_a_poison_message():
    """The list is curated centrally; an install only holds what it ingested."""
    cursor = Cursor(rowcount=0)

    consumer.apply_retraction(cursor, retraction())  # must not raise

    assert len(cursor.writes) == 1


def test_an_unretractable_entity_is_poison_not_a_silent_no_op(monkeypatch):
    """The pattern on the payload is the first guard; this is the second, at
    the point the table name would be interpolated into SQL."""
    payload = retraction()
    monkeypatch.setattr(payload, "entity", "itineraries")

    with pytest.raises(consumer.Poison):
        consumer.apply_retraction(Cursor(), payload)


def test_the_handler_is_wired_to_its_key():
    assert consumer.HANDLERS[config.RK_RETRACT] is consumer.apply_retraction


# ---------------------------------------------------------------- the wipe ----


def test_a_rebuild_replays_retractions_with_the_records():
    """`user_data.wipe` deletes every collected row and replays it from the
    ingestor's outbox. A retraction that is not replayed with them is silently
    undone, which is the original defect arriving by another route."""
    assert config.RK_RETRACT in consumer.SOURCE_KEYS


def test_a_rebuild_applies_records_before_retractions():
    """A retraction is an UPDATE of a row that must already exist, so replaying
    in outbox order would make correctness depend on the accept order of two
    different files."""
    source = (REPO / "services" / "consumer" / "main.py").read_text(encoding="utf-8")
    body = source.split("def wipe_user_data")[1].split("\ndef ")[0]
    records_pass = body.index("for routing_key, payload in records:")
    retractions_pass = body.index("for routing_key, payload in retractions:")
    assert records_pass < retractions_pass


def test_a_rebuild_carries_existing_retractions_across_the_delete():
    """Replaying the curated list restores the withdrawals that shipped with
    the repository. It does not restore one an operator made against this
    install alone, so the marks are captured before the delete and re-applied
    after the replay."""
    source = (REPO / "services" / "consumer" / "main.py").read_text(encoding="utf-8")
    body = source.split("def wipe_user_data")[1].split("\ndef ")[0]
    captured = body.index("retracted_at IS NOT NULL")
    deleted = body.index("wipe_business_rows")
    assert captured < deleted, "the marks must be read before the rows are deleted"
    assert "held.items()" in body, "the captured marks are never re-applied"


# ------------------------------------------------- every published read ----

# One entry per place a collected entity is read. A new read added without the
# filter is how this defect comes back, and a mocked cursor cannot see it.
COLLECTED_TABLES = ("events", "facts", "places")


def test_the_read_layer_is_the_only_place_these_tables_are_read():
    """If a second module starts reading them directly, this file stops being
    a sufficient guard and should be told to cover it too."""
    # The known-safe occurrences are stripped from the text first. Exempting
    # the whole FILE because it happens to contain one of them would let the
    # consumer -- or anything else quoting the same literal -- start reading a
    # collected table unguarded with this test still green.
    known_safe = (
        "DELETE FROM events WHERE is_sample AND retracted_at IS NULL",
        "IS DISTINCT FROM events.valid_until",
    )
    strays = []
    for path in (REPO / "services").rglob("*.py"):
        if path.name == "queries.py":
            continue
        text = path.read_text(encoding="utf-8")
        for safe in known_safe:
            text = text.replace(safe, "")
        for table in ("FROM events", "FROM places", "FROM facts"):
            if table in text:
                strays.append(f"{path.relative_to(REPO)}: {table}")
    assert not strays, (
        "these read a collected table outside services/common/queries.py, so the "
        "retraction filter has to be checked there as well: " + ", ".join(strays)
    )


@pytest.mark.parametrize("table", COLLECTED_TABLES)
def test_every_read_of_a_collected_table_filters_retracted_rows(table):
    """Counted rather than pattern-matched per site: the point is that the
    number of reads and the number of filters move together, so adding a read
    without a filter fails here."""
    reads = len(re.findall(rf"FROM {table}\b", QUERIES))
    assert reads, f"no reads of {table} found -- this test would be vacuous"

    # Each read is followed, within its own statement, by the filter. Split on
    # the read itself and require the guard before the next one begins.
    #
    # `places()` and `facts()` assemble their WHERE from a Python list whose
    # first element is the filter, so the guard is not textually adjacent to
    # the FROM. Those two sites read `FROM <table> WHERE",` as a bare string
    # fragment and are covered by the test below instead.
    builder = 'WHERE",'
    unguarded = []
    for chunk in QUERIES.split(f"FROM {table}")[1:]:
        if chunk.lstrip().startswith(builder):
            continue
        # The window ends where this statement does, not after a fixed number
        # of characters: the four BY_CITY_SQL event subqueries sit within ~90
        # characters of each other, so a fixed window let them borrow each
        # other's filter and deleting one was undetectable.
        closes = re.search(r"\)\s+AS ", chunk)
        ends = [
            pos
            for pos in (
                chunk.find("(SELECT COUNT(*)"),
                chunk.find("UNION ALL"),
                chunk.find("\n  FROM "),
                # The end of a scalar subquery is its closing paren followed by
                # its alias -- NOT the first `)\n`, which matches the `now()`
                # at the end of a line and cut two windows short of the filter
                # on the line below.
                closes.start() if closes else -1,
            )
            if pos != -1
        ]
        window = chunk[: min(ends)] if ends else chunk[:400]
        if "retracted_at IS NULL" not in window:
            unguarded.append(window.strip().splitlines()[0] if window.strip() else "<eof>")
    assert not unguarded, (
        f"a read of {table} does not exclude withdrawn rows: {unguarded}. "
        "Every published read must carry `retracted_at IS NULL` (migration 009)."
    )


def test_places_and_facts_reads_start_from_the_filter():
    """These two build their WHERE as a list, so the filter is the first
    element rather than an appended clause."""
    assert 'where = ["retracted_at IS NULL"]' in QUERIES
    assert '"  FROM facts WHERE retracted_at IS NULL"' in QUERIES


# ----------------------------------------------------------- the migration ----


def test_the_migration_adds_the_columns_to_all_three_tables():
    sql = MIGRATION.read_text(encoding="utf-8")
    for table in ("events", "places", "facts"):
        assert re.search(
            rf"ALTER TABLE {table}\s+ADD COLUMN IF NOT EXISTS retracted_at", sql
        ), table
        assert re.search(
            rf"ALTER TABLE {table}\s+ADD COLUMN IF NOT EXISTS retraction_reason", sql
        ), table


def test_the_migration_is_idempotent_and_grants_nothing():
    """It is re-run on every boot like the eight before it, and it must not
    widen what the one writer can do."""
    sql = MIGRATION.read_text(encoding="utf-8")
    # Statements only: the prose above them explains what the file does and
    # why it needs no grant, so a substring search over the whole file matches
    # its own rationale.
    statements = [
        line.strip()
        for line in sql.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    body = "\n".join(statements)

    assert body.count("ADD COLUMN IF NOT EXISTS") == 9  # 3 tables x 3 columns
    assert "CREATE INDEX IF NOT EXISTS" in body
    assert not [line for line in statements if line.upper().startswith("GRANT")], (
        "retraction is an UPDATE, and the writer already holds UPDATE; "
        "this migration must not widen any role's privileges"
    )
    assert "BEGIN;" in sql and "COMMIT;" in sql
