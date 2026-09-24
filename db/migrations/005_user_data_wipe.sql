-- The consumer can rebuild collected rows from the ingestor's durable outbox.
-- Keep the destructive first step in one owner-run function rather than
-- granting direct DELETE on every collected table to the application role.
REVOKE DELETE ON weather_daily, recommendations, places, facts FROM aow_writer;

CREATE OR REPLACE FUNCTION wipe_business_rows() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
BEGIN
  DELETE FROM public.itineraries;
  DELETE FROM public.recommendations;
  DELETE FROM public.weather_daily;
  DELETE FROM public.places;
  DELETE FROM public.facts;
  DELETE FROM public.events;
END;
$fn$;

REVOKE ALL ON FUNCTION wipe_business_rows() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION wipe_business_rows() TO aow_writer;
