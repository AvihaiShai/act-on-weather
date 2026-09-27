-- The retraction ledger: a withdrawal outlives the moment it was delivered.
--
-- The defect this closes. Migration 009 gave a collected record a way out of
-- the published output, and `services/consumer/main.py:apply_retraction`
-- applies it as an UPDATE of that row. The UPDATE is the whole of the
-- mechanism, so a withdrawal only works if the row is already there when the
-- message arrives. When it is not, the UPDATE matches nothing, the handler
-- logs a warning, and the transaction commits anyway -- carrying the
-- envelope's `message_id` into `ingest_log`. From that moment the withdrawal
-- is spent: the id is deterministic (`ingestor/main.py:natural_key`), the
-- outbox row is marked published and never re-drained, and `ingest_log` turns
-- every redelivery into a duplicate. If the record it named is ingested
-- afterwards, it is published with nothing left to withdraw it.
--
-- That is not a corner case. The curated `data/retractions.jsonl` list is
-- re-accepted on every boot precisely because an install only holds what it
-- has ingested, and `apply_retraction`'s own docstring treats "this install
-- never had that row" as normal rather than as an error. So both orderings --
-- record then withdrawal, withdrawal then record -- are ordinary, and only the
-- first one worked.
--
-- The shape of the fix. The decision is written down as a row of its own,
-- once, before it is applied to anything. `record_retractions` is the ledger
-- of every withdrawal this install has been told about, whether or not it
-- holds the record yet, and the consumer consults it whenever a collected
-- record comes into existence (`upsert_by_id`). Arrival order stops mattering
-- because the two halves no longer have to meet in time: the ledger is the
-- durable half, and the mark on the row is derived from it.
--
-- Why a table and not a retry. Requeuing or dead-lettering the message would
-- turn an expected condition -- a central list naming a row this deployment
-- does not carry -- into an operational alarm, and a redrive would still only
-- land if the record happened to have arrived by then. The ledger makes the
-- property true by construction instead of by timing.
--
-- Why this survives a rebuild. `wipe_business_rows()` (005) names the tables
-- it deletes one by one, and this is deliberately not one of them: a wipe
-- replays every collected record, so a ledger emptied by the wipe would
-- republish everything an operator had withdrawn -- the same F9 defect by a
-- third route. Because the ledger survives, the rebuild re-marks the rows as
-- it re-inserts them, and it stops depending on the wipe's two-pass order.
--
-- What it does not do. It is forward-looking. A withdrawal already consumed
-- against an absent row before this migration ran left no trace in the
-- database, and its `message_id` is in `ingest_log`, so it cannot be recovered
-- here. It has to be re-issued -- which works, because both the curated list
-- and `POST /records/.../retract` mint a new `message_id` once the reason, the
-- author or the decision date changes.
--
-- Idempotent, like every migration before it: CREATE TABLE IF NOT EXISTS is a
-- no-op the second time, the comments and grants are rewritten
-- unconditionally, and it takes no lock on a table anything is reading.

\set ON_ERROR_STOP on

BEGIN;

CREATE TABLE IF NOT EXISTS record_retractions (
  -- The target, as the `record.retract` payload names it. The entity is stored
  -- rather than derived, so the ledger can hold a withdrawal for a table this
  -- install has no matching row in at all -- which is the whole point.
  entity            TEXT        NOT NULL
                    CHECK (entity IN ('events', 'places', 'facts')),
  entity_id         TEXT        NOT NULL,

  -- The decision itself, exactly as it travels on the message. `retracted_at`
  -- is when the decision was taken, never `now()` at the consumer, and the
  -- ledger keeps the first value it was given for the same reason the row's
  -- `COALESCE(retracted_at, ...)` does: a replay must not restamp a withdrawal
  -- with the clock of the replay.
  retracted_at      TIMESTAMPTZ NOT NULL,
  retraction_reason TEXT        NOT NULL,
  retracted_by      TEXT        NOT NULL DEFAULT 'operator',

  -- When this install was told, as distinct from when the decision was taken.
  -- The gap between the two is the honest answer to "how long were we still
  -- serving it", and it is the one value here the consumer does not copy off
  -- the message.
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- One withdrawal per record. A second `record.retract` for the same target
  -- is a correction of the wording or the author, not a second decision, and
  -- the consumer's upsert treats it that way.
  PRIMARY KEY (entity, entity_id)
);

-- No secondary index. The only read is by the full primary key, once per
-- collected record written. Said out loud because the absence is a choice.

COMMENT ON TABLE record_retractions IS
  'Every record withdrawal this install has been told about, whether or not it '
  'holds the record. Written by the consumer before the mark is applied to the '
  'row, and consulted whenever a collected record is inserted, so a withdrawal '
  'that arrives before its target still takes effect when the target appears. '
  'Deliberately not deleted by wipe_business_rows(): a rebuild replays every '
  'collected record, and an emptied ledger would republish withdrawn ones.';
COMMENT ON COLUMN record_retractions.entity IS
  'events, places or facts -- the three collected tables. Mirrors the '
  'record.retract payload and services/consumer/main.py:RETRACTABLE.';
COMMENT ON COLUMN record_retractions.entity_id IS
  'The id of the withdrawn record. There need be no row with this id: the '
  'ledger is what makes a withdrawal outlive the arrival of its target.';
COMMENT ON COLUMN record_retractions.retracted_at IS
  'When the decision was taken, supplied by whoever took it. Kept at the first '
  'value recorded and never moved, so replaying the queue cannot restamp it.';
COMMENT ON COLUMN record_retractions.retraction_reason IS
  'Why, in one line. Correctable: getting the wording of a withdrawal right '
  'afterwards is normal, and the alternative is editing the database by hand.';
COMMENT ON COLUMN record_retractions.retracted_by IS
  'Who decided. "operator" for the curated data/retractions.jsonl list, "api" '
  'for a single-install withdrawal through POST /records/.../retract.';
COMMENT ON COLUMN record_retractions.recorded_at IS
  'When this install learned of the decision. Set by the database, not the '
  'message -- the one value here that is local rather than collected.';

-- Backfill, so the ledger is not "every withdrawal since Tuesday". An install
-- upgrading to this migration already carries marks on rows, put there by
-- migration 009's handler, and every one of them is a decision with a date, a
-- reason and an author -- the row is the only place it was ever written down.
-- Copying them in makes the ledger the complete record, which is what lets a
-- rebuild re-mark a row from the ledger alone instead of depending on the
-- wipe's capture-and-restore.
--
-- Nothing is invented: a row with no stated reason is skipped rather than given
-- one, because the ledger requires a reason and a made-up one would be worse
-- than the row it came from. The supported path cannot produce such a row --
-- `reason` is required on the `record.retract` payload -- so this excludes only
-- a row edited into the database by hand.
--
-- Runs on every boot like the rest of the file, and is a no-op after the first:
-- ON CONFLICT DO NOTHING, and the ledger is never wider than the rows plus what
-- the consumer has since recorded.
INSERT INTO record_retractions
  (entity, entity_id, retracted_at, retraction_reason, retracted_by)
SELECT 'events', id, retracted_at, retraction_reason,
       COALESCE(retracted_by, 'operator')
  FROM events WHERE retracted_at IS NOT NULL AND retraction_reason IS NOT NULL
UNION ALL
SELECT 'places', id, retracted_at, retraction_reason,
       COALESCE(retracted_by, 'operator')
  FROM places WHERE retracted_at IS NOT NULL AND retraction_reason IS NOT NULL
UNION ALL
SELECT 'facts',  id, retracted_at, retraction_reason,
       COALESCE(retracted_by, 'operator')
  FROM facts  WHERE retracted_at IS NOT NULL AND retraction_reason IS NOT NULL
ON CONFLICT (entity, entity_id) DO NOTHING;

-- The same shape every other table has: the reader reads, the one writer
-- writes, and no role gains DELETE, because a withdrawal is corrected rather
-- than removed like every other collected fact here. 001_init.sql already sets
-- ALTER DEFAULT PRIVILEGES for both roles, so these are belt and braces --
-- stated anyway, because that default only covers tables created by the role
-- that set it, and a ledger nobody can read fails silently.
GRANT SELECT                 ON record_retractions TO aow_reader;
GRANT SELECT, INSERT, UPDATE ON record_retractions TO aow_writer;

COMMIT;
