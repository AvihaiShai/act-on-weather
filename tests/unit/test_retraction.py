"""R9(c): withdrawing a collected record from the published output.

The defect these guard. Records here are revised, never deleted, and the
writer holds no DELETE grant reaching an event, a place or a fact. That left
no way out for a record which turned out to be wrong after it was collected:
removing its line from the snapshot produces no message, so an install that
already stored the row kept serving it. A dress rehearsal found two listings
the project's own recheck had verified as cancelled still being served.

The end-to-end proof is `tests/integration/retraction.py` for the arrival order
where the record is already here, and the three `retraction_*` fixtures that
`scripts/retraction-drill.sh` drives for the order where it is not. Both need a
real database. What is checkable here is everything that does not: the payload
contract, the routing, the SQL the consumer builds, the ledger that makes a
withdrawal outlive its delivery, the wipe's behaviour, and -- the one most likely
to rot -- that every published read filters retracted rows. That last one is a
source assertion rather than a database one on purpose: a new read added without
the filter is exactly how this defect would come back, and no unit test with a
mocked cursor would notice.
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
LEDGER = REPO / "db" / "migrations" / "010_retraction_ledger.sql"


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

    _ledger, (sql, params) = cursor.writes
    assert sql.startswith("UPDATE events SET retracted_at")
    assert "DELETE" not in sql.upper()
    assert params["id"] == "theo2:x"
    assert params["at"] == datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    assert params["reason"] == "the venue cancelled it"


def test_an_already_retracted_row_is_not_restamped():
    """Without the guard every rebuild would move the date a withdrawal was
    decided on to the date of the rebuild.

    Two clauses do the work and the test used to name only the weaker one.
    `retracted_at IS NULL` is part of the WHERE and decides whether the row is
    touched at all; `COALESCE(retracted_at, %(at)s)` in the SET is what keeps
    the original date when the row IS touched -- which it is whenever the reason
    or the author is being corrected. Replacing the COALESCE with a bare
    `%(at)s` left this test green and restamped every correction.
    """
    cursor = Cursor()

    consumer.apply_retraction(cursor, retraction())

    _ledger, (sql, _params) = cursor.writes
    assert "retracted_at IS NULL" in sql
    assert "SET retracted_at = COALESCE(retracted_at, %(at)s)" in sql


def test_a_retraction_for_an_unknown_record_is_not_a_poison_message():
    """The list is curated centrally; an install only holds what it ingested."""
    cursor = Cursor(rowcount=0)

    consumer.apply_retraction(cursor, retraction())  # must not raise

    # The ledger write and the UPDATE that marked nothing. Two, not one: the
    # decision has to be written down even when there is nothing here to mark.
    assert len(cursor.writes) == 2


# ------------------------------------------------- the ledger (migration 010) ----

# The defect these guard. A `record.retract` can arrive before the record it
# names -- the curated list is applied against whatever this install happens to
# have ingested, `POST /records/.../retract` publishes from the API's own outbox
# with no ordering relationship to the ingestor's, and a record that
# dead-letters is stored only when an operator redrives it. The handler's UPDATE
# then matches nothing and the transaction commits anyway, carrying the
# envelope's message_id into `ingest_log`. That is terminal: the id is
# deterministic, the outbox row is marked published, and every redelivery is
# short-circuited as a duplicate before the handler is reached. The record then
# arrived later and was published with nothing left to withdraw it.


def test_the_decision_is_written_down_before_it_is_applied():
    """The ledger is the durable half. It is written first and unconditionally,
    so the withdrawal outlives the one delivery that carried it."""
    cursor = Cursor()

    consumer.apply_retraction(cursor, retraction())

    (ledger_sql, ledger_params), (row_sql, _) = cursor.writes
    assert ledger_sql.startswith("INSERT INTO record_retractions")
    assert row_sql.startswith("UPDATE events")
    assert ledger_params == {
        "entity": "events",
        "id": "theo2:x",
        "at": datetime(2026, 9, 27, 12, 0, tzinfo=UTC),
        "reason": "the venue cancelled it",
        "by": "operator",
    }


def test_the_ledger_records_the_entity_so_a_row_need_not_exist():
    """The whole point: the ledger can hold a withdrawal for a table this
    install has no matching row in at all, so `entity` is stored, not derived."""
    cursor = Cursor(rowcount=0)

    consumer.apply_retraction(cursor, retraction("places", "osm:closed-cafe"))

    (_sql, params), _row = cursor.writes
    assert params["entity"] == "places"
    assert params["id"] == "osm:closed-cafe"


def test_the_ledger_does_not_move_the_decision_date_either():
    """Same rule as the row's `COALESCE(retracted_at, ...)`: a replay must not
    restamp a withdrawal with the clock of the replay. Enforced by leaving
    `retracted_at` out of the conflict clause, and the reason and the author
    stay correctable."""
    cursor = Cursor()

    consumer.apply_retraction(cursor, retraction())

    (sql, _params), _row = cursor.writes
    conflict = sql.split("ON CONFLICT")[1]
    restamps = "retracted_at = EXCLUDED" in conflict
    assert not restamps, (
        "the ledger would restamp the decision date on every replay of the "
        "curated list, which is the one thing a withdrawal must not do"
    )
    assert "retraction_reason = EXCLUDED.retraction_reason" in conflict
    assert "retracted_by = EXCLUDED.retracted_by" in conflict


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("events", "EVENT_COLS"),
        ("places", "PLACE_COLS"),
        ("facts", "FACT_COLS"),
    ],
)
def test_a_record_arriving_after_its_withdrawal_is_marked_on_arrival(table, columns):
    """The other arrival order. `upsert_by_id` consults the ledger for every
    collected record it writes, so the two halves no longer have to meet in
    time."""
    cursor = Cursor()
    payload = dict.fromkeys(getattr(consumer, columns), None) | {"id": "row-1"}

    consumer.upsert_by_id(cursor, table, getattr(consumer, columns), payload)

    (insert_sql, _), (mark_sql, mark_params) = cursor.writes
    assert insert_sql.startswith(f"INSERT INTO {table}")
    assert mark_sql.startswith(f"UPDATE {table} SET retraction_reason")
    assert "FROM record_retractions r" in mark_sql
    assert mark_params == {"entity": table, "id": "row-1"}
    # The load-bearing clause, and the one this test used to leave out. Reading
    # `record_retractions` and writing the reason and the author withdraws
    # nothing: every published read filters on `retracted_at IS NULL`, so a row
    # that gains a reason without gaining a date is still served everywhere.
    # Deleting this one assignment left every assertion above green.
    assert "retracted_at = r.retracted_at" in mark_sql
    assert f"{table}.retracted_at IS NULL" in mark_sql


def test_the_arrival_mark_does_not_touch_a_row_already_withdrawn():
    """A correction of the wording can be in flight; the row's own handler is
    what settles that, not this."""
    cursor = Cursor()

    consumer.apply_recorded_retraction(cursor, "events", "row-1")

    [(sql, _params)] = cursor.writes
    assert "events.retracted_at IS NULL" in sql
    assert "DELETE" not in sql.upper()


def test_a_replay_the_as_of_guard_rejected_does_not_consult_the_ledger():
    """`rowcount == 0` means the upsert wrote nothing, so nothing was newly
    published and there is nothing to withdraw. Saves a statement on the
    common case and keeps the mark tied to an actual write."""
    cursor = Cursor(rowcount=0)
    payload = dict.fromkeys(consumer.FACT_COLS, None) | {"id": "row-1"}

    consumer.upsert_by_id(cursor, "facts", consumer.FACT_COLS, payload)

    assert len(cursor.writes) == 1


def test_a_table_with_no_retraction_column_is_left_alone():
    """`upsert_by_id` is only ever called for the three collected tables today.
    The guard is what keeps that from becoming a broken statement if it is not
    -- `weather_daily` has no `retracted_at` (migration 009)."""
    cursor = Cursor()

    consumer.apply_recorded_retraction(cursor, "weather_daily", "rome/2026-09-24")

    assert cursor.writes == []


def _upsert_insert_line() -> int:
    """The 1-based line of the one INSERT inside `upsert_by_id`.

    Located rather than hard-coded, so the test above pins "there is exactly one
    and it is this one" without also failing every time something above it
    moves.
    """
    source = (REPO / "services" / "consumer" / "main.py").read_text(encoding="utf-8")
    lines = source.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("def upsert_by_id"))
    for offset, line in enumerate(lines[start:], start):
        if "INSERT INTO {table}" in line:
            return offset + 1
    raise AssertionError("upsert_by_id no longer contains an INSERT")


def test_upsert_by_id_is_the_only_way_a_collected_row_comes_into_existence():
    """What makes the hook complete by construction rather than by memory.

    The mark is applied in `upsert_by_id` and nowhere else, which is only
    sufficient while that is the one INSERT into these three tables. The
    consumer is the single writer, so this is checkable here: a second INSERT
    added anywhere under `services/` has to apply the ledger too, and this is
    where it is said so.
    """
    # `INSERT INTO events` as a literal appears nowhere and never did: the one
    # real INSERT is the f-string `f"INSERT INTO {table} ..."` at the top of
    # `upsert_by_id`. Searching for the interpolated spelling found nothing and
    # would go on finding nothing however many statements were added, so the
    # search is for the SHAPE instead -- any `INSERT INTO` that names one of the
    # three collected tables, literally or through an f-string placeholder --
    # and the one known occurrence is asserted to be present, so the pattern
    # cannot silently stop matching.
    pattern = re.compile(
        r"INSERT\s+INTO\s+(?:\{table\}|\{entity\}|(?:" + "|".join(COLLECTED_TABLES) + r")\b)"
    )
    hits = []
    for path in sorted((REPO / "services").rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(REPO).as_posix()}:{number}")
    assert hits, "the search for an INSERT into a collected table matched nothing at all"
    assert hits == ["services/consumer/main.py:" + str(_upsert_insert_line())], (
        "a collected table is inserted into outside upsert_by_id, so a record "
        "withdrawn before it arrived would be published un-retracted: " + ", ".join(hits)
    )
    source = (REPO / "services" / "consumer" / "main.py").read_text(encoding="utf-8")
    body = source.split("def upsert_by_id")[1].split("\ndef ")[0]
    assert "apply_recorded_retraction" in body, (
        "upsert_by_id no longer applies the ledger, so a record that arrives "
        "after its withdrawal is published un-retracted"
    )


def test_the_ledger_is_not_emptied_by_a_rebuild():
    """A wipe replays every collected record. A ledger the wipe emptied would
    republish everything an operator had withdrawn -- the same defect by a
    third route -- so `wipe_business_rows()` must never name it."""
    seen = []
    for path in sorted((REPO / "db" / "migrations").glob("*.sql")):
        # Executable SQL only. 010 both names `wipe_business_rows()` in its
        # header and spells out the reinstatement procedure -- which contains a
        # `DELETE FROM record_retractions` -- in a comment. Reading the raw text
        # matched that comment and failed on the file that documents the rule.
        sql = _statements(path.read_text(encoding="utf-8"))
        if "wipe_business_rows" not in sql:
            continue
        seen.append(path.name)
        body = sql.split("wipe_business_rows", 1)[1]
        assert "DELETE FROM public.record_retractions" not in body, path.name
        assert "DELETE FROM record_retractions" not in body, path.name
    # Without this the loop body never ran if the function were renamed, and the
    # test passed by inspecting nothing.
    assert seen, "no migration defines wipe_business_rows -- this test read nothing"

    # And the same property in Python, because the wipe does not do all of its
    # deleting in SQL. `wipe_user_data` issues `DELETE FROM record_history`
    # itself, after the function call, so a `DELETE FROM record_retractions`
    # added beside it would be invisible to the loop above.
    source = (REPO / "services" / "consumer" / "main.py").read_text(encoding="utf-8")
    assert "DELETE FROM record_retractions" not in source
    assert "delete from record_retractions" not in source.lower()


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
    retractions_pass = body.index("in retractions:")
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


# ------------------------------------------------ the ledger migration (010) ----


def _statements(sql: str) -> str:
    """The file without its prose. Every migration here explains itself at
    length, so a substring search over the whole text matches its own
    rationale."""
    return "\n".join(
        line.strip()
        for line in sql.splitlines()
        if line.strip() and not line.strip().startswith("--")
    )


def test_the_ledger_migration_creates_the_table_one_row_per_record():
    sql = _statements(LEDGER.read_text(encoding="utf-8"))
    assert "CREATE TABLE IF NOT EXISTS record_retractions" in sql
    assert "PRIMARY KEY (entity, entity_id)" in sql, (
        "without this a second withdrawal of the same record is a second row "
        "rather than a correction of the first"
    )
    for column in ("retracted_at", "retraction_reason", "retracted_by", "recorded_at"):
        assert column in sql, column
    assert "CHECK (entity IN ('events', 'places', 'facts'))" in sql


def test_the_ledger_migration_is_idempotent_and_re_runnable():
    """There is no migrations table: every file in the list runs on every boot."""
    sql = LEDGER.read_text(encoding="utf-8")
    body = _statements(sql)
    assert "CREATE TABLE IF NOT EXISTS" in body
    backfill_is_reentrant = "ON CONFLICT (entity, entity_id) DO NOTHING" in body
    assert backfill_is_reentrant, "the backfill must be a no-op on the second boot"
    assert r"\set ON_ERROR_STOP on" in sql
    assert "BEGIN;" in sql and "COMMIT;" in sql


def test_the_ledger_migration_grants_no_delete_and_no_new_role():
    """A withdrawal is corrected, never removed, like every other collected
    fact here -- and the reader stays read-only."""
    body = _statements(LEDGER.read_text(encoding="utf-8"))
    grants = [
        " ".join(line.split()) for line in body.splitlines() if line.upper().startswith("GRANT")
    ]
    assert grants, "a table nobody can read fails silently"
    assert not [line for line in grants if "DELETE" in line.upper()]
    assert "GRANT SELECT ON record_retractions TO aow_reader;" in grants
    assert "GRANT SELECT, INSERT, UPDATE ON record_retractions TO aow_writer;" in grants
    assert "CREATE ROLE" not in body


def test_the_backfill_invents_no_reason():
    """An install upgrading to 010 already carries marks on rows, and every one
    is a decision with a date, a reason and an author. A row with no stated
    reason is skipped rather than given one."""
    body = _statements(LEDGER.read_text(encoding="utf-8"))
    backfill = body.split("INSERT INTO record_retractions", 1)[1]
    for table in COLLECTED_TABLES:
        assert f"FROM {table} WHERE retracted_at IS NOT NULL AND retraction_reason IS NOT NULL" in (
            " ".join(backfill.split())
        ), table


# ------------------------------- corrections, backfill, and reinstatement ----
#
# Three behaviours that had no coverage at all: a withdrawal corrected after the
# fact, a backfill with rows under it, and what it takes to put a record back.
# Each is asserted at the strongest level this file can reach without a
# database. What still needs one is stated in the README rather than implied
# here.


def test_a_corrected_withdrawal_rewrites_the_wording_and_not_the_date():
    """The second `record.retract` for one target is a correction, not a second
    decision. Never exercised before: every other test here sends one message."""
    cursor = Cursor()

    consumer.apply_retraction(cursor, retraction())
    (ledger_sql, ledger_params), (row_sql, row_params) = cursor.writes

    conflict = ledger_sql.split("ON CONFLICT", 1)[1]
    assert "retraction_reason = EXCLUDED.retraction_reason" in conflict
    assert "retracted_by = EXCLUDED.retracted_by" in conflict
    # The decision date is absent from the SET list, in the ledger exactly as in
    # the row. A correction that moved it would rewrite the history it records.
    assert "retracted_at = EXCLUDED.retracted_at" not in conflict
    assert ledger_params["at"] == row_params["at"]
    # And the row is reached even when it is already withdrawn, or a correction
    # would never land on it.
    assert "retraction_reason IS DISTINCT FROM %(reason)s" in row_sql
    assert "retracted_by IS DISTINCT FROM %(by)s" in row_sql


def test_a_rebuild_never_revises_a_withdrawal_it_replays():
    """The defect this closes. `wipe_user_data` replays every envelope the
    INGESTOR accepted and excludes the API's own, so an operator who corrected a
    withdrawal through `POST /records/.../retract` and then rebuilt had the
    older curated envelope written back over both the ledger and the row --
    reverting a deliberate decision to wording already withdrawn, silently.

    A rebuild restores state; it does not revise it.
    """
    cursor = Cursor()

    consumer.apply_retraction(cursor, retraction(), replay=True)

    (ledger_sql, _), (mark_sql, mark_params) = cursor.writes
    assert "ON CONFLICT (entity, entity_id) DO NOTHING" in ledger_sql, (
        "the replay overwrote a ledger entry it found, which is how a "
        "correction gets reverted by a rebuild"
    )
    assert "EXCLUDED" not in ledger_sql.split("ON CONFLICT", 1)[1]
    # And the row is marked FROM the ledger, not from this older envelope.
    assert mark_sql.startswith("UPDATE events SET retraction_reason")
    assert "FROM record_retractions r" in mark_sql
    assert "retracted_at = r.retracted_at" in mark_sql
    assert mark_params == {"entity": "events", "id": "theo2:x"}


def test_the_ordinary_delivery_is_unchanged_by_the_replay_flag():
    """The bound on it: `replay` defaults to False, and a real delivery still
    corrects the wording."""
    plain = Cursor()
    consumer.apply_retraction(plain, retraction())
    (ledger_sql, _), (row_sql, _) = plain.writes

    assert "DO UPDATE SET" in ledger_sql
    assert row_sql.startswith("UPDATE events SET retracted_at = COALESCE")


def test_the_wipe_replays_retractions_with_the_replay_flag_set():
    """The two halves above are worth nothing unless the wipe asks for them.
    Read from the source, because no unit test drives `wipe_user_data` end to
    end -- it opens a SQLite outbox and calls a SQL function."""
    source = (REPO / "services" / "consumer" / "main.py").read_text(encoding="utf-8")
    body = source.split("def wipe_user_data")[1].split("\ndef ")[0]
    pass_two = body.split("in retractions:", 1)[1]
    assert (
        "apply_retraction(cur, payload, replay=True)" in pass_two
    ), "the rebuild replays retractions as though they were fresh deliveries"
    # And it does NOT go through HANDLERS, which would lose the flag.
    assert "HANDLERS[routing_key](cur, payload)" not in pass_two


def test_the_backfill_records_no_arrival_time_it_cannot_know():
    """`recorded_at` is documented as when this install learned of a decision,
    and the gap to `retracted_at` as how long it was still being served. For a
    row already marked when 010 first ran, that moment was never recorded, and
    letting the column fall to DEFAULT now() made every backfilled row report
    the age of the upgrade as an exposure window."""
    body = _statements(LEDGER.read_text(encoding="utf-8"))
    backfill = " ".join(body.split("INSERT INTO record_retractions", 1)[1].split())

    assert "recorded_at" in backfill.split("SELECT", 1)[0], (
        "recorded_at is not in the backfill's column list, so it falls to "
        "DEFAULT now() and fabricates an arrival time"
    )
    # One explicit NULL per collected table.
    assert backfill.count("COALESCE(retracted_by, 'operator'), NULL") == len(COLLECTED_TABLES)
    # And the column has to permit it.
    create = body.split("CREATE TABLE IF NOT EXISTS record_retractions", 1)[1].split(";", 1)[0]
    recorded = next(line for line in create.splitlines() if line.strip().startswith("recorded_at"))
    assert "NOT NULL" not in recorded.upper(), recorded


def _top_level_commas(select_list: str) -> list[str]:
    """Split a SELECT list on commas that are not inside a function call.

    A plain `.split(",")` counts `COALESCE(retracted_by, 'operator')` as two
    expressions and makes the arity check below fail on correct SQL.
    """
    parts: list[str] = []
    depth = 0
    current = ""
    for char in select_list:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += char
    parts.append(current)
    return [part for part in parts if part.strip()]


def test_the_backfill_column_list_and_its_select_cannot_drift_apart():
    """A column list and a SELECT list of different lengths is a runtime error
    in a migration, which is the worst place to find one."""
    body = _statements(LEDGER.read_text(encoding="utf-8"))
    backfill = " ".join(body.split("INSERT INTO record_retractions", 1)[1].split())
    columns = backfill.split("(", 1)[1].split(")", 1)[0].split(",")
    first_select = backfill.split("SELECT", 1)[1].split("FROM", 1)[0]
    assert len(columns) == len(_top_level_commas(first_select)), (columns, first_select)


def test_repeating_the_migration_is_safe_and_is_not_claimed_to_be_a_no_op():
    """010 runs on every boot. `ON CONFLICT DO NOTHING` means it adds nothing
    where the ledger already covers the marked rows -- but it is NOT an
    unconditional no-op, because it re-derives the ledger FROM the rows. An
    entry deleted while its row stays marked comes back on the next boot. That
    is a constraint on how a record is reinstated, and the file used to claim
    the opposite."""
    text = LEDGER.read_text(encoding="utf-8")
    body = _statements(text)
    backfill = " ".join(body.split("INSERT INTO record_retractions", 1)[1].split())

    assert "ON CONFLICT (entity, entity_id) DO NOTHING" in backfill
    assert "CREATE TABLE IF NOT EXISTS record_retractions" in body
    # The file used to assert flatly that the backfill "is a no-op after the
    # first". It must now say what the exception is, in the same breath.
    assert "recreates an entry" in text
    assert "deleted while its row was still marked" in text


def test_reinstating_a_record_is_documented_as_both_halves_in_one_transaction():
    """Clearing the row's mark alone is not enough: the ledger re-marks it on
    the next collected message and on the next wipe. Deleting the ledger entry
    alone is not enough either: the backfill puts it back on the next boot. The
    procedure has to say both, in one transaction, in a migration numbered above
    this one -- and every document that carried the insufficient one-line
    version has to stop carrying it."""
    text = LEDGER.read_text(encoding="utf-8")

    assert (
        "DELETE FROM record_retractions WHERE entity" in text
    ), "010 does not spell out the reinstatement procedure"
    assert "retracted_at = NULL" in text
    lowered = text.lower()
    assert "one transaction" in lowered
    assert "above this file" in lowered

    # `services/api/main.py` only. The other three documents that carried the
    # insufficient one-line version -- README.md, TECHNICAL_DECISIONS.md and
    # docs/ARCHITECTURE.md -- are corrected in the same change, but the test
    # image deliberately does not COPY them (see tests/Dockerfile, which lists
    # what it ships and why), so asserting on them here fails in the container
    # and passes on a host checkout. A test that can only run in one of the two
    # places the suite runs is worse than no test.
    prose = (REPO / "services" / "api" / "main.py").read_text(encoding="utf-8")
    assert "reinstat" in prose.lower()
    assert "record_retractions" in prose, (
        "api/main.py describes reinstatement without naming the ledger entry " "that would undo it"
    )


def test_the_single_consumer_assumption_is_written_down():
    """The ledger write and the row mark are two statements, and they are
    correct together only because one process applies them. Nothing enforces it
    -- `docker compose up --scale consumer=2` is all it would take -- so it is
    stated where somebody scaling the consumer would look. Read at source level:
    no interleaving was reproduced."""
    text = LEDGER.read_text(encoding="utf-8").lower()
    assert "one consumer" in text
    assert "read committed" in text
    assert "advisory lock" in text
