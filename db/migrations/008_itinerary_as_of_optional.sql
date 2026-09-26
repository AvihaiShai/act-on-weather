-- The scoring as-of of a saved itinerary becomes optional.
--
-- It records which forecast snapshot the plan's scores were computed from, so
-- only whoever computed them can supply it. The API used to read it from the
-- database inside the write handler, which broke the rule that a write never
-- depends on the database: with Postgres down the save blocked, and the
-- fallback stamped the plan with the API's own clock -- a timestamp no
-- forecast ever had, which the UI then compared against the live window and
-- reported as "refreshed since this was saved".
--
-- The UI now sends the as-of of the plan it actually built. A caller that does
-- not send one stores NULL, and the UI says the scoring timestamp was not
-- recorded. A missing provenance can be read for what it is; an invented one
-- cannot.
--
-- Idempotent: dropping a NOT NULL that is already dropped is a no-op, and the
-- comment is rewritten unconditionally, so this is safe to re-run on every
-- boot like the migrations before it. It is also transactional and does not
-- rewrite the table -- `DROP NOT NULL` flips a catalog flag.

\set ON_ERROR_STOP on

BEGIN;

ALTER TABLE itineraries ALTER COLUMN as_of DROP NOT NULL;

COMMENT ON COLUMN itineraries.as_of IS
  'As-of of the forecast snapshot this plan''s scores were computed from, as '
  'reported by whoever computed them. NULL when the caller did not say: the '
  'UI renders that as "not recorded" and skips the staleness comparison, '
  'rather than substituting a timestamp nothing was scored against.';

COMMIT;
