-- act-on-weather schema.
--
-- Two application roles are created here:
--   aow_writer  -- the consumer, the only role with write grants (M4)
--   aow_reader  -- the api and the agent, SELECT only
-- The owner role (POSTGRES_USER) runs migrations and nothing else.
--
-- Passwords arrive as psql variables, never as literals in this file:
--   psql -v writer_password=... -v reader_password=... -f 001_init.sql

\set ON_ERROR_STOP on

BEGIN;

-- ---------------------------------------------------------------- roles ----
DO $roles$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aow_writer') THEN
    CREATE ROLE aow_writer LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aow_reader') THEN
    CREATE ROLE aow_reader LOGIN;
  END IF;
END
$roles$;

ALTER ROLE aow_writer WITH PASSWORD :'writer_password';
ALTER ROLE aow_reader WITH PASSWORD :'reader_password';

-- ------------------------------------------------------------- 1 cities ----
CREATE TABLE IF NOT EXISTS cities (
  id          TEXT PRIMARY KEY,              -- slug, e.g. rome
  name        TEXT NOT NULL,
  country     TEXT NOT NULL,
  lat         DOUBLE PRECISION NOT NULL,
  lon         DOUBLE PRECISION NOT NULL,
  timezone    TEXT NOT NULL,
  aliases     TEXT[] NOT NULL DEFAULT '{}'
);

-- ------------------------------------------------------ 2 weather_daily ----
-- PK is (city_id, forecast_date). `provider` is recorded but deliberately NOT
-- part of the key: a second provider must replace the day, not shadow it, so
-- that the version-matched join to `recommendations` stays unique.
CREATE TABLE IF NOT EXISTS weather_daily (
  city_id         TEXT NOT NULL REFERENCES cities(id),
  forecast_date   DATE NOT NULL,
  provider        TEXT NOT NULL,
  temp_min_c      DOUBLE PRECISION,
  temp_max_c      DOUBLE PRECISION,
  precip_mm       DOUBLE PRECISION,
  precip_prob     INTEGER,
  wind_kmh        DOUBLE PRECISION,
  uv_index        DOUBLE PRECISION,
  sunshine_hours  DOUBLE PRECISION,
  sunrise         TIMESTAMPTZ,
  sunset          TIMESTAMPTZ,
  weather_code    INTEGER,
  source_url      TEXT,
  as_of           TIMESTAMPTZ NOT NULL,       -- when the provider produced it
  ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  revision        INTEGER NOT NULL DEFAULT 1,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (city_id, forecast_date)
);
CREATE INDEX IF NOT EXISTS weather_daily_date_idx ON weather_daily (forecast_date);

-- ----------------------------------------------------- 3 recommendations ----
-- Keyed by activity, so an activity the user typed is a row like any other
-- (M2). `score` is the deterministic rule output and is the source of truth;
-- `text` is what the LLM wrote about that score and may be NULL.
CREATE TABLE IF NOT EXISTS recommendations (
  city_id          TEXT NOT NULL REFERENCES cities(id),
  forecast_date    DATE NOT NULL,
  activity         TEXT NOT NULL,             -- slug; free text is slugified
  activity_label   TEXT NOT NULL,
  requested        BOOLEAN NOT NULL DEFAULT false,  -- true = a user asked for it
  score            INTEGER,
  band             TEXT,                      -- good | fair | poor
  reasons          JSONB NOT NULL DEFAULT '[]'::jsonb,
  rule_version     INTEGER,
  status           TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'ready', 'failed')),
  text             TEXT,
  model            TEXT,
  invalid_attempts INTEGER NOT NULL DEFAULT 0,  -- only schema-invalid output counts
  last_error       TEXT,
  weather_as_of    TIMESTAMPTZ,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (city_id, forecast_date, activity)
);
CREATE INDEX IF NOT EXISTS recommendations_pending_idx
  ON recommendations (created_at) WHERE status = 'pending';

-- ------------------------------------------------------------- 4 places ----
CREATE TABLE IF NOT EXISTS places (
  id          TEXT PRIMARY KEY,               -- stable: <source>:<source_id>
  city_id     TEXT NOT NULL REFERENCES cities(id),
  name        TEXT NOT NULL,
  category    TEXT NOT NULL,
  lat         DOUBLE PRECISION,
  lon         DOUBLE PRECISION,
  address     TEXT,
  source      TEXT NOT NULL,
  source_url  TEXT,
  is_sample   BOOLEAN NOT NULL DEFAULT false,
  as_of       TIMESTAMPTZ NOT NULL,
  ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  revision    INTEGER NOT NULL DEFAULT 1,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS places_city_category_idx ON places (city_id, category);

-- -------------------------------------------------------------- 5 facts ----
-- History and tourism background, one row per subject.
CREATE TABLE IF NOT EXISTS facts (
  id          TEXT PRIMARY KEY,
  city_id     TEXT NOT NULL REFERENCES cities(id),
  title       TEXT NOT NULL,
  summary     TEXT NOT NULL,
  topic       TEXT NOT NULL DEFAULT 'history',
  source      TEXT NOT NULL,
  source_url  TEXT,
  is_sample   BOOLEAN NOT NULL DEFAULT false,
  as_of       TIMESTAMPTZ NOT NULL,
  ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  revision    INTEGER NOT NULL DEFAULT 1,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS facts_city_idx ON facts (city_id);

-- ------------------------------------------------------------- 6 events ----
-- Sports fixtures, concerts and the like. Nothing is invented: every row
-- carries a source and an as-of, and is_sample is surfaced in the UI and in
-- the agent answers.
CREATE TABLE IF NOT EXISTS events (
  id          TEXT PRIMARY KEY,
  city_id     TEXT NOT NULL REFERENCES cities(id),
  title       TEXT NOT NULL,
  category    TEXT NOT NULL,                  -- sport | concert | festival ...
  venue       TEXT,
  starts_at   TIMESTAMPTZ NOT NULL,
  ends_at     TIMESTAMPTZ,
  source      TEXT NOT NULL,
  source_url  TEXT,
  is_sample   BOOLEAN NOT NULL DEFAULT false,
  as_of       TIMESTAMPTZ NOT NULL,
  ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  revision    INTEGER NOT NULL DEFAULT 1,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_city_start_idx ON events (city_id, starts_at);

-- -------------------------------------------------------- 7 itineraries ----
CREATE TABLE IF NOT EXISTS itineraries (
  id          TEXT PRIMARY KEY,
  city_id     TEXT NOT NULL REFERENCES cities(id),
  title       TEXT NOT NULL,
  start_date  DATE NOT NULL,
  end_date    DATE NOT NULL,
  days        JSONB NOT NULL DEFAULT '[]'::jsonb,
  as_of       TIMESTAMPTZ NOT NULL,           -- as-of of the data it was built from
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  revision    INTEGER NOT NULL DEFAULT 1,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --------------------------------------------------------- 8 ingest_log ----
-- The idempotency ledger. The consumer inserts the message_id in the SAME
-- transaction as the business write, and only then acks. A redelivery after a
-- crash between commit and ack hits this primary key and is dropped.
CREATE TABLE IF NOT EXISTS ingest_log (
  message_id   TEXT PRIMARY KEY,
  routing_key  TEXT NOT NULL,
  source       TEXT,
  processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------- 9 record_history ----
CREATE TABLE IF NOT EXISTS record_history (
  id          BIGSERIAL PRIMARY KEY,
  entity      TEXT NOT NULL,
  entity_id   TEXT NOT NULL,
  revision    INTEGER NOT NULL,
  changed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  old_row     JSONB,
  new_row     JSONB
);
CREATE INDEX IF NOT EXISTS record_history_entity_idx ON record_history (entity, entity_id);

-- ---------------------------------------------------------- revisioning ----
-- One trigger pair does the whole of M12 bookkeeping: bump the revision and
-- write the before/after into record_history, but only when the row really
-- changed, so a no-op re-ingest inflates neither.
CREATE OR REPLACE FUNCTION bump_revision() RETURNS TRIGGER AS $fn$
BEGIN
  NEW.revision   := OLD.revision + 1;
  NEW.updated_at := now();
  RETURN NEW;
END
$fn$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION write_history() RETURNS TRIGGER AS $fn$
DECLARE
  new_row JSONB := to_jsonb(NEW);
  key     TEXT;
BEGIN
  -- Addressed through the JSONB copy rather than as NEW.<column>: one trigger
  -- function serves five tables, and PL/pgSQL rejects a reference to a column
  -- that does not exist on the table it is currently firing for -- even in a
  -- CASE branch that would never be taken.
  key := CASE TG_TABLE_NAME
           WHEN 'weather_daily'
             THEN (new_row->>'city_id') || '/' || (new_row->>'forecast_date')
           ELSE new_row->>'id'
         END;
  INSERT INTO record_history (entity, entity_id, revision, old_row, new_row)
  VALUES (TG_TABLE_NAME, key, NEW.revision, to_jsonb(OLD), new_row);
  RETURN NULL;
END
$fn$ LANGUAGE plpgsql;

DO $triggers$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['weather_daily', 'places', 'facts', 'events', 'itineraries'] LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', t || '_bump', t);
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE ON %I FOR EACH ROW
         WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION bump_revision()',
      t || '_bump', t);
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', t || '_hist', t);
    EXECUTE format(
      'CREATE TRIGGER %I AFTER UPDATE ON %I FOR EACH ROW
         WHEN (OLD.revision IS DISTINCT FROM NEW.revision) EXECUTE FUNCTION write_history()',
      t || '_hist', t);
  END LOOP;
END
$triggers$;

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS TRIGGER AS $fn$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS recommendations_touch ON recommendations;
CREATE TRIGGER recommendations_touch BEFORE UPDATE ON recommendations
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- --------------------------------------------------------------- grants ----
GRANT USAGE ON SCHEMA public TO aow_reader, aow_writer;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO aow_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO aow_reader;

GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO aow_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO aow_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE ON TABLES TO aow_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO aow_writer;

-- Later migrations add narrow DELETE grants for demo-event and saved-trip
-- cleanup. Neither role gets schema-wide DELETE privileges.

COMMIT;
