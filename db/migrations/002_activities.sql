-- Migration 002: room for a wider activity catalogue.
--
-- Two changes, both driven by growing data/activities.yml from 5 activities
-- to 18:
--
--   1. cities.coastal -- surfing, swimming, the beach, fishing and a boat ride
--      carry `requires_coast` and are scored only where there is a coast. The
--      flag is configuration, seeded by the consumer from data/cities.yml
--      alongside the rest of the city row.
--
--   2. recommendations.status gains 'deferred'. The rule engine scores every
--      activity for every city and day -- that is cheap, deterministic and it
--      is the source of truth. Wording them all is not: 18 activities x 5
--      cities x 16 days is ~1,300 LLM calls per refresh, and on the CPU
--      default that is hours of a single llama.cpp slot the agent also shares.
--      So the consumer ranks a day's scores and leaves only the top
--      ENRICH_TOP_N 'pending'; the rest are 'deferred' -- scored, stored and
--      charted, but never queued for wording unless a user asks for that
--      activity by name, which flips the row back to 'pending'.
--
-- Idempotent: safe to re-run, and safe against a database created by 001 alone.

\set ON_ERROR_STOP on

BEGIN;

ALTER TABLE cities ADD COLUMN IF NOT EXISTS coastal BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE recommendations DROP CONSTRAINT IF EXISTS recommendations_status_check;
ALTER TABLE recommendations ADD CONSTRAINT recommendations_status_check
  CHECK (status IN ('pending', 'ready', 'failed', 'deferred'));

-- The enricher's hot query is "pending rows, best first". The partial index
-- from 001 was ordered by created_at, which no longer matches how rows are
-- drained now that a day's activities are ranked.
DROP INDEX IF EXISTS recommendations_pending_idx;
CREATE INDEX IF NOT EXISTS recommendations_pending_idx
  ON recommendations (forecast_date, score DESC) WHERE status = 'pending';

COMMIT;
