"""Every read the system performs, in one module.

The API routers and the agent's router call the *same* functions. That is the
point: an answer the agent gives and a number the UI shows cannot drift apart,
because there is only one SQL statement behind each of them.

All of these run as `aow_reader`, which holds SELECT and nothing else.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import psycopg

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
SELECT 'events', 'event window', MAX(as_of), COUNT(*),
       COUNT(DISTINCT city_id), COUNT(*) FILTER (WHERE is_sample),
       MIN(starts_at)::date::text, MAX(starts_at)::date::text
  FROM events
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
       (SELECT COUNT(*) FROM weather_daily w WHERE w.city_id = c.id)   AS weather,
       (SELECT COUNT(*) FROM recommendations r WHERE r.city_id = c.id) AS recommendations,
       (SELECT COUNT(*) FROM places p WHERE p.city_id = c.id)          AS places,
       (SELECT COUNT(*) FROM facts f WHERE f.city_id = c.id)           AS facts,
       (SELECT COUNT(*) FROM events e WHERE e.city_id = c.id)          AS events,
       (SELECT COUNT(*) FROM events e WHERE e.city_id = c.id AND e.is_sample) AS sample_events
  FROM cities c ORDER BY c.name
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
        "cities": conn.execute(
            "SELECT id, name, country, lat, lon, timezone, coastal FROM cities ORDER BY name"
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


def places(
    conn: psycopg.Connection,
    city_id: str | None = None,
    *,
    categories: list[str] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    sql = [
        "SELECT id, city_id, name, category, lat, lon, address, source, source_url,",
        "       is_sample, as_of, revision",
        "  FROM places WHERE true",
    ]
    params: dict[str, Any] = {"limit": limit}
    if city_id:
        sql.append("AND city_id = %(city)s")
        params["city"] = city_id
    if categories:
        sql.append("AND category = ANY(%(categories)s)")
        params["categories"] = categories
    sql.append("ORDER BY category, name LIMIT %(limit)s")
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
    limit: int = 50,
) -> list[dict[str, Any]]:
    """`category` is the API's single-value filter; `categories` is the agent's.

    They exist side by side because a question can ask about more than one kind
    at once ("concerts or theatre this weekend"), and because answering a
    question about concerts with a tennis tournament is the exact defect this
    argument was added to close.
    """
    sql = [
        "SELECT id, city_id, title, category, venue, starts_at, ends_at, source,",
        "       source_url, is_sample, as_of, revision",
        "  FROM events WHERE true",
    ]
    params: dict[str, Any] = {"limit": limit}
    if city_id:
        sql.append("AND city_id = %(city)s")
        params["city"] = city_id
    if start:
        sql.append("AND starts_at >= %(start)s::date")
        params["start"] = start
    if end:
        sql.append("AND starts_at < (%(end)s::date + 1)")
        params["end"] = end
    if category:
        sql.append("AND category = %(category)s")
        params["category"] = category
    if categories:
        sql.append("AND category = ANY(%(categories)s)")
        params["categories"] = list(categories)
    sql.append("ORDER BY starts_at LIMIT %(limit)s")
    return conn.execute("\n".join(sql), params).fetchall()


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
    return conn.execute(
        "SELECT id, name, country, lat, lon, timezone, aliases, coastal"
        "  FROM cities ORDER BY name"
    ).fetchall()
