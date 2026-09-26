"""Every read the system performs, in one module.

The API routers and the agent's router call the *same* functions. That is the
point: an answer the agent gives and a number the UI shows cannot drift apart,
because there is only one SQL statement behind each of them.

All of these run as `aow_reader`, which holds SELECT and nothing else.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

import psycopg

# ------------------------------------------------------- event local day ----

# An event's calendar day is the day it falls on *in the city*, not in UTC and
# not in whatever timezone the database session happens to run in. The Laver
# Cup starts at 2026-09-25T00:00+01:00, which is 2026-09-24T23:00Z; a traveller
# in London asking about "tomorrow" on the 24th means the 25th, and a filter
# that compared the raw timestamptz against a date answered "nothing on" (F2).
#
# So every read of `events` joins `cities` and derives two local dates:
#
#   starts_on  the local date the event begins
#   ends_on    the local date it is still running on
#
# The active window is half-open, [starts_at, ends_at): an event billed as
# ending at local midnight ends on the previous day rather than opening the
# next one. Subtracting a microsecond before the cast is what makes that true,
# and GREATEST stops a row whose `ends_at` is null, equal to or earlier than
# its start from ending before it began.
#
# These expressions are not sargable -- `AT TIME ZONE <column>` is STABLE, not
# IMMUTABLE, so no index can cover them -- and `events_city_start_idx` is left
# serving the ordering alone. A deliberate trade: this table holds tens of
# rows, and the right day matters more here than the scan does.
#
# Every read also derives `is_current` from `valid_until` (migration 006). A
# stored listing is a reading of a web page taken at `checked_at`, and nothing
# in an air-gapped run can notice that the venue cancelled the show afterwards.
# So the row keeps its provenance and its place in the coverage counts, but
# once its reading has expired it stops being returned as something that is
# scheduled.
#
# `events()` filters on it by default, and so does the events row of
# COVERAGE_SQL, because an expired listing three weeks out would otherwise
# stretch the advertised window past the last date anything is returned for.
# The three places that deliberately look at expired rows are `BY_CITY_SQL`'s
# `verified_events_expired`, `EVENT_FRESHNESS_SQL`, and the API's
# `include_expired` -- because "we hold four London listings that nobody has
# re-checked since 24 September" is a more useful thing to show a reader than
# an empty list.
EVENTS_LOCALISED_SQL = """
WITH localised AS (
  SELECT e.id, e.city_id, e.title, e.category, e.venue, e.starts_at, e.ends_at,
         e.source, e.source_url, e.is_sample, e.as_of, e.revision,
         e.checked_at, e.valid_until, (e.valid_until > now()) AS is_current,
         c.timezone,
         (e.starts_at AT TIME ZONE c.timezone)::date AS starts_on,
         GREATEST(
           (e.starts_at AT TIME ZONE c.timezone)::date,
           ((COALESCE(e.ends_at, e.starts_at) - INTERVAL '1 microsecond')
              AT TIME ZONE c.timezone)::date
         ) AS ends_on
    FROM events e JOIN cities c ON c.id = e.city_id
)
"""


def event_days(row: Mapping[str, Any]) -> list[date]:
    """Every local date a stored event row is active on.

    A multi-day event appears on *each* of its days -- the Laver Cup runs the
    25th to the 27th, and a traveller planning the 26th has to see it. That is
    this system's definition of "on that day", and the API filter, the
    itinerary grouping and the agent's rendering all take it from here so they
    cannot drift apart.
    """
    start = row["starts_on"]
    end = row.get("ends_on") or start
    if end < start:
        end = start
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


# ------------------------------------------------------------- coverage ----

# `kind` says what a date range means for that entity, which is the difference
# between "this table has no coverage" and "this table is not date-scoped".
# Places and facts genuinely have no window -- a museum is not valid between
# two dates -- and the UI used to render that as a bare "-", which reads as
# missing data. They report their as-of and their city spread instead.
COVERAGE_SQL = """
SELECT 'weather' AS entity, 'forecast window' AS kind,
       MAX(as_of) AS as_of, COUNT(*) AS rows,
       COUNT(DISTINCT city_id) AS cities, 0 AS samples,
       MIN(forecast_date)::text AS first_date, MAX(forecast_date)::text AS last_date
  FROM weather_daily
UNION ALL
SELECT 'recommendations', 'forecast window', MAX(updated_at), COUNT(*),
       COUNT(DISTINCT city_id), 0,
       MIN(forecast_date)::text, MAX(forecast_date)::text
  FROM recommendations
UNION ALL
-- Local dates, and the window closes on the last day an event is still
-- running rather than on the last day one starts (see EVENTS_LOCALISED_SQL).
-- Counted over the CURRENT rows only, so the window this reports is the window
-- the agent will actually answer from. An expired listing still exists and is
-- still counted separately by `EVENT_FRESHNESS_SQL`; what it must not do is
-- stretch the advertised coverage window past the last date anything can
-- actually be answered for.
SELECT 'events', 'event window', MAX(e.as_of), COUNT(*),
       COUNT(DISTINCT e.city_id), COUNT(*) FILTER (WHERE e.is_sample),
       MIN((e.starts_at AT TIME ZONE c.timezone)::date)::text,
       MAX(GREATEST(
             (e.starts_at AT TIME ZONE c.timezone)::date,
             ((COALESCE(e.ends_at, e.starts_at) - INTERVAL '1 microsecond')
                AT TIME ZONE c.timezone)::date
           ))::text
  FROM events e JOIN cities c ON c.id = e.city_id
 WHERE e.valid_until > now()
UNION ALL
SELECT 'places', 'not date-scoped', MAX(as_of), COUNT(*),
       COUNT(DISTINCT city_id), COUNT(*) FILTER (WHERE is_sample), NULL, NULL
  FROM places
UNION ALL
SELECT 'facts', 'not date-scoped', MAX(as_of), COUNT(*),
       COUNT(DISTINCT city_id), COUNT(*) FILTER (WHERE is_sample), NULL, NULL
  FROM facts
UNION ALL
SELECT 'itineraries', 'saved by users', MAX(updated_at), COUNT(*),
       COUNT(DISTINCT city_id), 0,
       MIN(start_date)::text, MAX(end_date)::text
  FROM itineraries
"""

# Which cities actually hold what. Without this the row counts above hide the
# shape of the data -- "7 events" looks like coverage until you see they are
# all in one city.
BY_CITY_SQL = """
SELECT c.id AS city_id, c.name, c.coastal,
       -- The coast reference point, so a coastal city's row reads as a claim
       -- somebody can check rather than a boolean somebody asserted. Null for
       -- an inland city; see migration 007.
       c.coast_name, c.coast_distance_km,
       (SELECT COUNT(*) FROM weather_daily w WHERE w.city_id = c.id)   AS weather,
       (SELECT COUNT(*) FROM recommendations r WHERE r.city_id = c.id) AS recommendations,
       (SELECT COUNT(*) FROM places p WHERE p.city_id = c.id)          AS places,
       (SELECT COUNT(*) FROM facts f WHERE f.city_id = c.id)           AS facts,
       (SELECT COUNT(*) FROM events e WHERE e.city_id = c.id)          AS events,
       (SELECT COUNT(*) FROM events e WHERE e.city_id = c.id AND e.is_sample) AS sample_events,
       -- Split out per city because F9 was about exactly this shape: "26
       -- events" read as coverage right up until you saw that eleven were in
       -- London and one was in Tel Aviv. A per-city current count is what lets
       -- the UI and the closure record say where the feed is thin without
       -- anybody having to count rows by hand.
       (SELECT COUNT(*) FROM events e
         WHERE e.city_id = c.id AND NOT e.is_sample AND e.valid_until > now())
                                                                       AS verified_events_current,
       (SELECT COUNT(*) FROM events e
         WHERE e.city_id = c.id AND NOT e.is_sample AND e.valid_until <= now())
                                                                       AS verified_events_expired
  FROM cities c ORDER BY c.name
"""

# The freshness of the verified feed as a whole: how many readings are still
# inside their recheck window, how many have fallen out of it, and when the
# oldest still-current reading was taken. This is the number an operator needs
# in order to decide whether to run a connected refresh, and it is deliberately
# reported next to the coverage window rather than buried in a log line.
EVENT_FRESHNESS_SQL = """
SELECT COUNT(*) FILTER (WHERE NOT is_sample AND valid_until > now())  AS current,
       COUNT(*) FILTER (WHERE NOT is_sample AND valid_until <= now()) AS expired,
       COUNT(*) FILTER (WHERE is_sample)                              AS samples,
       -- Split, because the same filter applies to a generated row and an
       -- expired sample is otherwise invisible: it would be counted in
       -- `samples`, excluded from every read, and named nowhere. Demo mode
       -- exists to exercise the planner in all five cities, and "the demo rows
       -- have aged out" has to be something the coverage panel can say rather
       -- than a stack that quietly stops showing them.
       COUNT(*) FILTER (WHERE is_sample AND valid_until > now())       AS samples_current,
       COUNT(*) FILTER (WHERE is_sample AND valid_until <= now())      AS samples_expired,
       MIN(checked_at) FILTER (WHERE NOT is_sample AND valid_until > now())
                                                                      AS oldest_check,
       MIN(valid_until) FILTER (WHERE NOT is_sample AND valid_until > now())
                                                                      AS next_expiry,
       COUNT(DISTINCT city_id) FILTER (WHERE NOT is_sample AND valid_until > now())
                                                                      AS cities_covered
  FROM events
"""


def coverage(conn: psycopg.Connection) -> dict[str, Any]:
    """What the system holds and how old it is.

    Every answer and every chart is stamped from this, and a question outside
    the window is refused rather than guessed (M7, M11 honesty).
    """
    rows = conn.execute(COVERAGE_SQL).fetchall()
    entities = {r["entity"]: r for r in rows}
    weather = entities.get("weather") or {}
    return {
        "entities": rows,
        "by_city": conn.execute(BY_CITY_SQL).fetchall(),
        "weather_first_date": weather.get("first_date"),
        "weather_last_date": weather.get("last_date"),
        "weather_as_of": weather.get("as_of"),
        # Reported beside the windows rather than inside the events row,
        # because it answers a different question: not "what does the system
        # hold" but "how much of it is still worth quoting".
        "event_freshness": conn.execute(EVENT_FRESHNESS_SQL).fetchone(),
        # `lat`/`lon` are the forecast point. `coast_*` is where the coast
        # actually is and how far the forecast point is from it, which is what
        # lets the UI caption a coastal score honestly rather than leaving the
        # reader to assume the forecast was taken on the beach.
        "cities": conn.execute(
            "SELECT id, name, country, lat, lon, timezone, coastal,"
            "       coast_name, coast_lat, coast_lon, coast_distance_km"
            "  FROM cities ORDER BY name"
        ).fetchall(),
    }


# ------------------------------------------------------------ activities ----

ACTIVITY_CATALOGUE_SQL = """
SELECT activity, MIN(activity_label) AS label,
       COUNT(*) AS scored_days,
       COUNT(*) FILTER (WHERE status = 'ready')    AS worded,
       COUNT(*) FILTER (WHERE status = 'pending')  AS pending,
       COUNT(*) FILTER (WHERE status = 'deferred') AS deferred,
       BOOL_OR(requested) AS user_requested,
       MAX(score) AS best_score, ROUND(AVG(score))::int AS avg_score
  FROM recommendations
 -- Cast, or Postgres cannot infer the parameter's type from a bare
 -- comparison against NULL and refuses the whole statement.
 WHERE (%(city)s::text IS NULL OR city_id = %(city)s::text)
 GROUP BY activity
 ORDER BY MIN(activity_label)
"""


def activity_catalogue(
    conn: psycopg.Connection, city_id: str | None = None
) -> list[dict[str, Any]]:
    """Every activity that has a stored score, with how far its wording got.

    Deliberately read from the recommendations table rather than from
    data/activities.yml: the UI must offer what the system actually scored,
    not what the catalogue file aspires to. It is also how the coastal gate
    becomes visible -- an inland city simply has no surfing row.
    """
    return conn.execute(ACTIVITY_CATALOGUE_SQL, {"city": city_id}).fetchall()


def in_coverage(cov: dict[str, Any], day: date) -> bool:
    first, last = cov.get("weather_first_date"), cov.get("weather_last_date")
    if not first or not last:
        return False
    return str(first) <= day.isoformat() <= str(last)


# -------------------------------------------------------------- weather ----


def forecast(
    conn: psycopg.Connection,
    city_id: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> list[dict[str, Any]]:
    sql = [
        "SELECT city_id, forecast_date, provider, temp_min_c, temp_max_c, precip_mm,",
        "       precip_prob, wind_kmh, uv_index, sunshine_hours, sunrise, sunset,",
        "       weather_code, source_url, as_of, revision, updated_at",
        "  FROM weather_daily WHERE city_id = %(city)s",
    ]
    params: dict[str, Any] = {"city": city_id}
    if start:
        sql.append("AND forecast_date >= %(start)s")
        params["start"] = start
    if end:
        sql.append("AND forecast_date <= %(end)s")
        params["end"] = end
    sql.append("ORDER BY forecast_date")
    return conn.execute("\n".join(sql), params).fetchall()


def stored_forecast_days(
    days: list[date], rows: list[dict[str, Any]]
) -> tuple[list[date], list[date]]:
    """Partition requested days using this city's actual forecast rows."""
    stored = {str(row["forecast_date"]) for row in rows}
    return (
        [day for day in days if day.isoformat() in stored],
        [day for day in days if day.isoformat() not in stored],
    )


# ------------------------------------------------------ recommendations ----


def recommendations(
    conn: psycopg.Connection,
    city_id: str | None = None,
    *,
    start: date | None = None,
    end: date | None = None,
    activity: str | None = None,
) -> list[dict[str, Any]]:
    sql = [
        "SELECT r.city_id, r.forecast_date, r.activity, r.activity_label, r.requested,",
        "       r.score, r.band, r.reasons, r.rule_version, r.status, r.text, r.model,",
        "       r.last_error, r.weather_as_of, r.updated_at",
        "  FROM recommendations r WHERE true",
    ]
    params: dict[str, Any] = {}
    if city_id:
        sql.append("AND r.city_id = %(city)s")
        params["city"] = city_id
    if start:
        sql.append("AND r.forecast_date >= %(start)s")
        params["start"] = start
    if end:
        sql.append("AND r.forecast_date <= %(end)s")
        params["end"] = end
    if activity:
        sql.append("AND r.activity = %(activity)s")
        params["activity"] = activity
    sql.append("ORDER BY r.forecast_date, r.activity")
    return conn.execute("\n".join(sql), params).fetchall()


# --------------------------------------------------------------- places ----


PLACE_COLUMNS = (
    "id, city_id, name, category, lat, lon, address, source, source_url, "
    "is_sample, as_of, revision"
)


def places(
    conn: psycopg.Connection,
    city_id: str | None = None,
    *,
    categories: list[str] | None = None,
    limit: int = 50,
    per_category: int | None = None,
) -> list[dict[str, Any]]:
    """Stored places, optionally narrowed to a set of categories.

    `per_category` caps how many rows any one category may take. Without it a
    flat `LIMIT` over `ORDER BY category, name` is decided by the alphabet: a
    traveller who asked about concerts, shopping and fine dining in London got
    eighteen concert halls and no restaurant, because `concert_hall` sorts
    first and London now holds more than eighteen of them. The ordering is the
    same either way, so the caller sees no difference except that every
    category it asked about is represented.
    """
    params: dict[str, Any] = {"limit": limit}
    where = ["true"]
    if city_id:
        where.append("AND city_id = %(city)s")
        params["city"] = city_id
    if categories:
        where.append("AND category = ANY(%(categories)s)")
        params["categories"] = categories

    if per_category:
        params["per_category"] = per_category
        sql = [
            f"WITH ranked AS (SELECT {PLACE_COLUMNS},",
            "       ROW_NUMBER() OVER (PARTITION BY category ORDER BY name) AS rank",
            "  FROM places WHERE",
            "\n".join(where),
            ")",
            f"SELECT {PLACE_COLUMNS} FROM ranked WHERE rank <= %(per_category)s",
            "ORDER BY category, name LIMIT %(limit)s",
        ]
    else:
        sql = [
            f"SELECT {PLACE_COLUMNS}",
            "  FROM places WHERE",
            "\n".join(where),
            "ORDER BY category, name LIMIT %(limit)s",
        ]
    return conn.execute("\n".join(sql), params).fetchall()


# --------------------------------------------------------------- events ----


def events(
    conn: psycopg.Connection,
    city_id: str | None = None,
    *,
    start: date | None = None,
    end: date | None = None,
    category: str | None = None,
    categories: list[str] | None = None,
    include_expired: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """`category` is the API's single-value filter; `categories` is the agent's.

    They exist side by side because a question can ask about more than one kind
    at once ("concerts or theatre this weekend"), and because answering a
    question about concerts with a tennis tournament is the exact defect this
    argument was added to close.

    `start`/`end` are local calendar dates in the city's own timezone, and they
    match on overlap: a row comes back if any day it is active on falls inside
    the range, not only if it *begins* inside it. Every row carries `timezone`,
    `starts_on` and `ends_on` so no caller has to re-derive the day.

    `include_expired` defaults to False, which is the important default in this
    function: by default this returns only listings whose last check is still
    inside the recheck window, because everything that consumes it -- the
    agent's event answers, the itinerary, the API -- presents what it gets back
    as a schedule. An operator who wants to see what has gone stale asks for it
    explicitly, and gets `is_current` on every row to tell the two apart.
    """
    sql = [
        EVENTS_LOCALISED_SQL,
        "SELECT * FROM localised WHERE true",
    ]
    params: dict[str, Any] = {"limit": limit}
    if not include_expired:
        sql.append("AND is_current")
    if city_id:
        sql.append("AND city_id = %(city)s")
        params["city"] = city_id
    if start:
        sql.append("AND ends_on >= %(start)s::date")
        params["start"] = start
    if end:
        sql.append("AND starts_on <= %(end)s::date")
        params["end"] = end
    if category:
        sql.append("AND category = %(category)s")
        params["category"] = category
    if categories:
        sql.append("AND category = ANY(%(categories)s)")
        params["categories"] = list(categories)
    # By local day first: two events on the same local calendar day in
    # different cities belong together, whatever their UTC instants are.
    sql.append("ORDER BY starts_on, starts_at LIMIT %(limit)s")
    return conn.execute("\n".join(sql), params).fetchall()


def expired_events(
    conn: psycopg.Connection,
    city_id: str | None = None,
    *,
    start: date | None = None,
    end: date | None = None,
    categories: list[str] | None = None,
) -> dict[str, Any]:
    """How many listings the default read just filtered out, and when they
    were checked.

    "No concert is on record in Tel Aviv this week" and "the four concert
    listings on record for Tel Aviv this week were last checked on 24 September
    and are past their recheck date" are different answers, and only the second
    one tells the reader what to do about it. The agent asks for this only when
    it is about to report a gap, so the ordinary path still runs one query.

    Grouped by category, and that is not a convenience. A question can ask
    about two kinds at once ("any concerts or dance this week?"), and it gets a
    gap sentence per kind. A single total attached to both would quote the
    concert count in the dance sentence -- and, worse, would claim a stale
    listing for a category that has never had one, which is the precise
    confusion between "the feed is out of date" and "the city is quiet" that
    this whole feature exists to prevent. So the caller gets `by_category` and
    a `total` for the question as a whole, and uses whichever matches the gap
    it is writing.
    """
    sql = [
        EVENTS_LOCALISED_SQL,
        "SELECT category, COUNT(*) AS expired, MAX(checked_at) AS last_checked",
        "  FROM localised WHERE NOT is_current",
    ]
    params: dict[str, Any] = {}
    if city_id:
        sql.append("AND city_id = %(city)s")
        params["city"] = city_id
    if start:
        sql.append("AND ends_on >= %(start)s::date")
        params["start"] = start
    if end:
        sql.append("AND starts_on <= %(end)s::date")
        params["end"] = end
    if categories:
        sql.append("AND category = ANY(%(categories)s)")
        params["categories"] = list(categories)
    sql.append("GROUP BY category")

    rows = conn.execute("\n".join(sql), params).fetchall()
    by_category = {
        str(row["category"]): {
            "expired": int(row["expired"]),
            "last_checked": row["last_checked"],
        }
        for row in rows
    }
    checks = [row["last_checked"] for row in rows if row["last_checked"]]
    return {
        "expired": sum(entry["expired"] for entry in by_category.values()),
        "last_checked": max(checks) if checks else None,
        "by_category": by_category,
    }


# ---------------------------------------------------------------- facts ----


def facts(
    conn: psycopg.Connection,
    city_id: str | None = None,
    *,
    topic: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    sql = [
        "SELECT id, city_id, title, summary, topic, source, source_url, is_sample,",
        "       as_of, revision",
        "  FROM facts WHERE true",
    ]
    params: dict[str, Any] = {"limit": limit}
    if city_id:
        sql.append("AND city_id = %(city)s")
        params["city"] = city_id
    if topic:
        sql.append("AND topic = %(topic)s")
        params["topic"] = topic
    sql.append("ORDER BY title LIMIT %(limit)s")
    return conn.execute("\n".join(sql), params).fetchall()


# ----------------------------------------------------------- itineraries ----


def itinerary(conn: psycopg.Connection, itinerary_id: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT id, city_id, title, start_date, end_date, days, as_of, revision,"
        "       created_at, updated_at"
        "  FROM itineraries WHERE id = %s",
        (itinerary_id,),
    ).fetchone()


def itineraries(conn: psycopg.Connection, city_id: str | None = None) -> list[dict[str, Any]]:
    if city_id:
        return conn.execute(
            "SELECT id, city_id, title, start_date, end_date, revision, updated_at"
            "  FROM itineraries WHERE city_id = %s ORDER BY updated_at DESC",
            (city_id,),
        ).fetchall()
    return conn.execute(
        "SELECT id, city_id, title, start_date, end_date, revision, updated_at"
        "  FROM itineraries ORDER BY updated_at DESC"
    ).fetchall()


# -------------------------------------------------------------- history ----


def history(conn: psycopg.Connection, entity: str, entity_id: str) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT revision, changed_at, old_row, new_row FROM record_history"
        "  WHERE entity = %s AND entity_id = %s ORDER BY revision DESC",
        (entity, entity_id),
    ).fetchall()


def cities(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Every city, with the coast reference that makes `coastal` checkable.

    `lat`/`lon` are the forecast point -- the coordinate the weather provider
    was asked about -- and `coast_*` is the named point on that city's coast
    together with the distance derived between the two at seed time. The agent
    reads both, because a coastal suitability score has to be able to say what
    it is a score of: the weather at a point 25 km from the sea, in Rome's
    case, and never the sea itself.
    """
    return conn.execute(
        "SELECT id, name, country, lat, lon, timezone, aliases, coastal,"
        "       coast_name, coast_lat, coast_lon, coast_distance_km"
        "  FROM cities ORDER BY name"
    ).fetchall()
