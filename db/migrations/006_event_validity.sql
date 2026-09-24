-- Migration 006: when an event listing was last checked, and when that stops
-- counting.
--
-- Numbering note: 004 and 005 are reserved by other work in progress on this
-- repository, so this file takes 006. The migrations are applied in the order
-- compose.yml lists them, and they are each idempotent, so the gap is harmless.
--
-- Why this exists. Every row in data/events.seed.jsonl was produced by opening
-- a venue's or an organiser's own listing page and reading the title, the date
-- and the hall off it. `as_of` recorded when that happened, and for a while it
-- was doing two jobs at once: it was the provenance timestamp printed beside an
-- answer, and it was also the only hint that the reading might have gone stale.
--
-- Those are different facts and they need different columns, because an
-- air-gapped run cannot tell the difference between a listing that is still
-- accurate and one that was cancelled the day after somebody looked at it. So:
--
--   checked_at   the instant a human last read this row off its source_url.
--   valid_until  the instant after which that reading is no longer offered as
--                a current schedule. Derived once, from checked_at plus
--                AOW_EVENT_RECHECK_DAYS, by the ingestor -- see
--                services/ingestor/fetch_content.expiry_for -- and carried on
--                the row so the database, the API, the agent and the UI all
--                read the same instant.
--
-- Past valid_until the row is NOT deleted. Deleting would lose the provenance
-- and would make the coverage panel claim a gap where there is really a stale
-- reading, which is a different and more useful thing to tell a reviewer. It
-- simply stops being returned as a scheduled event; see queries.events, which
-- filters on it by default, and the `expired` count the coverage panel reports.
--
-- Backfill. An existing database has rows written before these columns existed.
-- They are backfilled from as_of, which is exactly what as_of used to mean for
-- an event row, and the expiry from as_of plus 21 days.
--
-- That 21 is a literal, and it is the one place in the system that does not
-- read AOW_EVENT_RECHECK_DAYS: psql has no access to the application's
-- environment, and passing it in would mean the migration container and the
-- ingestor container agreeing about a variable neither of them owns. So it is
-- the default, and it is deliberately only a starting value. A deployment
-- running a different window corrects itself on the next ingest, because
-- `consumer.upsert_event` widens its idempotency guard to let a changed
-- `valid_until` through even when nothing else about the row moved -- which is
-- also what makes lowering the window take effect at all.
--
-- The point of the backfill is therefore not to be exactly right; it is that
-- every pre-existing row ends up with an age rather than being silently
-- perpetual.
--
-- Idempotent, like the others: ADD COLUMN IF NOT EXISTS, and the backfill only
-- touches rows that are still NULL, so re-running on every boot is a no-op.

ALTER TABLE events ADD COLUMN IF NOT EXISTS checked_at  TIMESTAMPTZ;
ALTER TABLE events ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ;

UPDATE events SET checked_at = as_of WHERE checked_at IS NULL;
UPDATE events SET valid_until = as_of + INTERVAL '21 days' WHERE valid_until IS NULL;

-- Only once the backfill has run, so the constraint cannot fail on an existing
-- database. A fresh one goes through the same two no-op updates first.
ALTER TABLE events ALTER COLUMN checked_at  SET NOT NULL;
ALTER TABLE events ALTER COLUMN valid_until SET NOT NULL;

-- The default read filters on valid_until and orders by the local start day,
-- and this table is small enough that the planner will usually pick a scan
-- anyway. The index is here for the one query that is not small: the coverage
-- panel counting current against expired rows across every city.
CREATE INDEX IF NOT EXISTS events_valid_until_idx ON events (valid_until);

COMMENT ON COLUMN events.checked_at IS
  'When this listing was last read off its own source_url by hand. Provenance, '
  'not freshness policy.';
COMMENT ON COLUMN events.valid_until IS
  'After this instant the row is still stored and still counted, but it is no '
  'longer returned as a currently scheduled event. Derived from checked_at + '
  'AOW_EVENT_RECHECK_DAYS by the ingestor. See migration 006.';
