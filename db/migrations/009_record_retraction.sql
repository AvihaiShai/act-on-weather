-- Retraction: withdrawing a collected record from the published output.
--
-- The gap this closes. Records here are revised, never deleted (see
-- TECHNICAL_DECISIONS.md and 003_demo_events.sql). The sentence that used to
-- stand here -- "the writer holds no DELETE grant that reaches an event, a
-- place or a fact" -- was false for events and misleading for the other two,
-- and it was checked against the live grants rather than argued about:
--
--   * `events`: the writer holds a direct, table-wide DELETE. `003:27` grants
--     it and `005:4` revokes DELETE on `weather_daily`, `recommendations`,
--     `places` and `facts` WITHOUT naming `events`, which is what leaves the
--     003 grant live. It is deliberate and it is load-bearing:
--     `enforce_event_mode()` in `services/consumer/main.py` runs
--     `DELETE FROM events WHERE is_sample AND retracted_at IS NULL` as the
--     writer, and adding `events` to the 005 revoke would break the
--     demo-sample purge. What restricts that delete to sample rows is the
--     predicate in application code, not the grant.
--   * `places` and `facts`: no table grant, but the writer holds EXECUTE on
--     `wipe_business_rows()`, which is `SECURITY DEFINER` and deletes them as
--     the owner. The protection is the indirection, which `005`'s own header
--     states was the intent -- not the absence of a route.
--
-- What actually keeps `record_history` honest is that no supported path
-- deletes a collected record: the one DELETE the writer can aim is confined to
-- sample rows by its own WHERE clause, and the wipe deletes everything and
-- replays it rather than removing one row.
--
-- The no-delete rule has one cost, and it is the reason for this file: until
-- now there was no way out for a record that turned out to be wrong *after* it
-- was collected. A
-- venue cancels a concert; a recheck cannot confirm a listing; a place closes.
-- Removing the row from `data/events.seed.jsonl` stopped it reaching a new
-- install and did nothing at all to an install that already had it, because a
-- record that is absent from a snapshot simply produces no message. The only
-- remedy was to destroy the volume and re-ingest.
--
-- The shape of the fix. A retraction is a soft mark, not a delete:
-- `retracted_at` says when the record was withdrawn and `retraction_reason`
-- says why, in one line, for a person reading the row later. Every published
-- read filters `retracted_at IS NULL` (services/common/queries.py), so the
-- record leaves the API, the agent, the planner, the UI and every count in
-- /coverage. The row itself stays, with its source, its as-of and its full
-- history, which is the point: "we withdrew this on the 27th because the
-- venue cancelled it" is a stronger statement than a row that silently
-- vanished, and it is the difference between a retraction and a cover-up.
--
-- Why this needs no new grant. Marking is an UPDATE, and the writer already
-- holds UPDATE on every table (001_init.sql). Nothing here widens what the
-- one writer can do, and no role gains DELETE.
--
-- Why the existing triggers are left to do the bookkeeping. `events`,
-- `places` and `facts` all carry the `_bump`/`_hist` trigger pair from
-- 001_init.sql, so marking a row bumps `revision` and files the before/after
-- into `record_history` for free. A retraction is an ordinary revision of the
-- record, and it reads as one.
--
-- Idempotent, like every migration before it: ADD COLUMN IF NOT EXISTS is a
-- no-op the second time, the partial indexes are IF NOT EXISTS, and the
-- comments are rewritten unconditionally. Adding a nullable column with no
-- default does not rewrite the table.

\set ON_ERROR_STOP on

BEGIN;

ALTER TABLE events ADD COLUMN IF NOT EXISTS retracted_at      TIMESTAMPTZ;
ALTER TABLE events ADD COLUMN IF NOT EXISTS retraction_reason TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS retracted_by      TEXT;

ALTER TABLE places ADD COLUMN IF NOT EXISTS retracted_at      TIMESTAMPTZ;
ALTER TABLE places ADD COLUMN IF NOT EXISTS retraction_reason TEXT;
ALTER TABLE places ADD COLUMN IF NOT EXISTS retracted_by      TEXT;

ALTER TABLE facts  ADD COLUMN IF NOT EXISTS retracted_at      TIMESTAMPTZ;
ALTER TABLE facts  ADD COLUMN IF NOT EXISTS retraction_reason TEXT;
ALTER TABLE facts  ADD COLUMN IF NOT EXISTS retracted_by      TEXT;

-- Partial indexes on the published set. Every read adds `retracted_at IS
-- NULL`, and a partial index is the one that matches that predicate: it also
-- stays the size of the published data rather than the size of the table.
CREATE INDEX IF NOT EXISTS events_published_idx
  ON events (city_id, starts_at) WHERE retracted_at IS NULL;
CREATE INDEX IF NOT EXISTS places_published_idx
  ON places (city_id, category)  WHERE retracted_at IS NULL;
CREATE INDEX IF NOT EXISTS facts_published_idx
  ON facts  (city_id)            WHERE retracted_at IS NULL;

COMMENT ON COLUMN events.retracted_at IS
  'When this listing was withdrawn from the published output. NULL means '
  'published. A retracted row is still stored, still carries its source and '
  'as-of, and is still in record_history; it is filtered out of every read, '
  'every answer and every count in /coverage. Set only by a record.retract '
  'message through the queue -- see services/consumer/main.py.';
COMMENT ON COLUMN events.retraction_reason IS
  'One line saying why, for whoever reads the row later. Required by the '
  'record.retract payload: a withdrawal with no stated reason is not '
  'reviewable. The reason and the author may be corrected afterwards; '
  'retracted_at is kept at the first decision and never restamped.';
COMMENT ON COLUMN events.retracted_by IS
  'Who decided. "operator" for the curated data/retractions.jsonl list, '
  '"api" for a single-install withdrawal through POST /records/.../retract, '
  'or whatever the caller supplied.';

COMMENT ON COLUMN places.retracted_at IS
  'When this place was withdrawn from the published output. NULL means '
  'published. See events.retracted_at.';
COMMENT ON COLUMN places.retraction_reason IS 'Why it was withdrawn, in one line.';

COMMENT ON COLUMN facts.retracted_at IS
  'When this article was withdrawn from the published output. NULL means '
  'published. See events.retracted_at.';
COMMENT ON COLUMN facts.retraction_reason IS 'Why it was withdrawn, in one line.';

COMMIT;
