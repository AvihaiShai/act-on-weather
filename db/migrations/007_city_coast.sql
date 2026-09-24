-- Migration 007: where a coastal city's coast actually is.
--
-- Numbered 007 although 003 is the highest migration on this branch: 004, 005
-- and 006 are reserved by work in flight elsewhere, and two files claiming one
-- number is the one mistake a numbered migration list cannot survive.
--
-- Migration 002 added `cities.coastal`, which decides whether surfing,
-- swimming, the beach, fishing and a boat ride are scored for a city. It is a
-- boolean and nothing more, so it can be wrong in a way nobody can check: the
-- forecast point for Rome sits about 25 km from the sea and Lisbon's about
-- 18 km, and the suitability score they carried was computed from weather
-- measured well inland of any water.
--
-- These four columns make that claim inspectable. `coast_name` is a real,
-- named reference point on the city's coast, `coast_lat`/`coast_lon` are its
-- coordinates, and `coast_distance_km` is the great-circle distance from the
-- city's forecast point to it. All four are seeded by the consumer from
-- data/cities.yml, like `coastal` itself, so the file and the table cannot
-- disagree. The distance is *derived* at seed time by
-- services/common/coast.haversine_km and is deliberately not written down in
-- the YAML, so moving a coordinate cannot leave a stale number behind it.
--
-- All four are nullable, because an inland city genuinely has none. Reading
-- code treats a null coast the same way it treats an inland city: it still
-- says that nothing in the data measures the sea, it simply cannot say how
-- far away that sea is.
--
-- Idempotent, like 002 and 003: safe to re-run on every boot.

\set ON_ERROR_STOP on

BEGIN;

ALTER TABLE cities ADD COLUMN IF NOT EXISTS coast_name        TEXT;
ALTER TABLE cities ADD COLUMN IF NOT EXISTS coast_lat         DOUBLE PRECISION;
ALTER TABLE cities ADD COLUMN IF NOT EXISTS coast_lon         DOUBLE PRECISION;
ALTER TABLE cities ADD COLUMN IF NOT EXISTS coast_distance_km DOUBLE PRECISION;

COMMENT ON COLUMN cities.lat IS
  'Latitude of the FORECAST POINT -- the coordinate the weather provider is '
  'asked about. It is the city centre, not the shoreline. See coast_lat.';
COMMENT ON COLUMN cities.coast_name IS
  'A named reference point on this city''s coast, or NULL for an inland city. '
  'What turns cities.coastal from an assertion into a checkable claim.';
COMMENT ON COLUMN cities.coast_distance_km IS
  'Great-circle distance from the forecast point to coast_lat/coast_lon, '
  'derived by services/common/coast.haversine_km when the consumer seeds the '
  'city. It says where the forecast was taken; it says nothing about the sea '
  'state, which this system does not measure at all.';

COMMIT;
