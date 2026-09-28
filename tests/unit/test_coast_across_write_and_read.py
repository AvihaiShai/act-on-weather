"""One coast decision, driven through the write path and read back by the agent.

Three files already cover the coast rule and none of them covers the seam.
`test_requested_activity.py` imports the consumer and asserts what it stores.
`test_typed_activities.py` imports the agent and asserts what it says.
`test_inland_water.py` imports `services/common/rules.py` and asserts the two
predicates on their own. Between them every layer is pinned and the handoff
between the two outer ones is not: both sides read the same two-sided test --
`cfg.get("requires_coast")` when the catalogue holds the activity and
`rules.needs_coast` when it does not -- from their own copy of the branch
(`services/consumer/main.py:466` and `services/agent/main.py:348`). Nothing
failed if one copy changed and the other did not.

The two shapes that costs are worth naming, because they are not symmetric:

  * the writer stops refusing and the reader keeps explaining. A row appears
    for a city with no coast, and the answer that used to say "London has no
    coast on record" now reports a score instead. Wrong data, stated
    confidently.
  * the writer keeps refusing and the reader stops explaining. The row is
    correctly absent and the traveller is told only "no suitability score on
    record", with the reason -- the useful half -- silently dropped.

So each case here runs both halves against ONE store: the consumer's INSERT is
applied to an in-memory table, and the router reads that same table back.
Whatever the write path decided is exactly what the read path is given, which
is the property no single-layer test can assert.

Still no database and no model. The cursor is the fake the other consumer tests
use, extended to apply its INSERT; the queries are stubbed the way
`test_typed_activities.py` stubs them; `today_in` is frozen and every date is
written out, so these keep meaning something after the staged window expires.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from services.agent import dates, router
from services.common import schemas
from services.consumer import main as consumer

ROOT = Path(__file__).resolve().parents[2]

TODAY = date(2026, 9, 26)
TOMORROW = date(2026, 9, 27)

# The real city list, so a test cannot drift from the data the container ships.
# `COASTAL` is what `seed_cities` fills at startup; the consumer reads it as a
# module global, which is why it is patched rather than passed.
CITIES = yaml.safe_load((ROOT / "data" / "cities.yml").read_text(encoding="utf-8"))["cities"]
COASTAL = {city["slug"]: bool(city.get("coastal", False)) for city in CITIES}

LONDON = {
    "id": "london",
    "name": "London",
    "country": "United Kingdom",
    "timezone": "Europe/London",
    "aliases": [],
    "coastal": False,
}
ROME = {
    "id": "rome",
    "name": "Rome",
    "country": "Italy",
    "timezone": "Europe/Rome",
    "aliases": ["roma"],
    "coastal": True,
    # The shape `queries.cities` returns, so `coast.sea_state_caveat` renders
    # the sentence it renders in production rather than its degraded form.
    "coast_name": "Lido di Ostia",
    "coast_distance_km": 24.7,
}
COVERAGE = {
    "cities": [LONDON, ROME],
    "weather_first_date": "2026-09-24",
    "weather_last_date": "2026-10-09",
    "weather_as_of": "2026-09-24",
}

# Warm, dry, calm and sunny: no land rule is violated, so any score below 100
# here is a ceiling and any missing row is a refusal. Nothing in these tests can
# be explained by the weather.
PERFECT_DAY = {
    "temp_max_c": 24.0,
    "temp_min_c": 17.0,
    "precip_mm": 0.0,
    "precip_prob": 5.0,
    "wind_kmh": 9.0,
    "uv_index": 4.0,
    "sunshine_hours": 10.0,
    "as_of": "2026-09-24",
}


# ------------------------------------------------------------- the store ----


def stored_row(params: tuple[Any, ...]) -> dict[str, Any]:
    """The row `queries.recommendations` would read back for this INSERT.

    Built from the consumer's own bind parameters rather than written out, so a
    column the writer stops sending cannot be quietly supplied here. `requested`
    and `status` are literals in that statement rather than parameters, which is
    why they are the only two values this function knows by name.

    `reasons` goes through `json.loads` because the writer sends `json.dumps`
    into a JSONB column and psycopg hands the reader back the decoded list. That
    round trip is the one piece of the database this fake has to imitate.
    """
    city_id, forecast_date, activity, label, score, band, reasons, rule_version, as_of = params
    return {
        "city_id": city_id,
        "forecast_date": forecast_date,
        "activity": activity,
        "activity_label": label,
        "requested": True,
        "score": score,
        "band": band,
        "reasons": json.loads(reasons),
        "rule_version": rule_version,
        "status": "pending",
        "text": None,
        "model": None,
        "last_error": None,
        "weather_as_of": as_of,
    }


class WriteCursor:
    """The consumer's cursor, with its INSERT applied to `store`."""

    def __init__(self, store: list[dict[str, Any]], weather=PERFECT_DAY):
        self.store = store
        self.statements: list[str] = []
        self._weather = weather

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        self.statements.append(text)
        if text.startswith("INSERT INTO recommendations"):
            # The other INSERT in this handler is the "no stored weather for
            # that date" refusal, which takes four parameters and cannot be
            # reached here: every case below is given PERFECT_DAY. Asserting it
            # rather than branching keeps the fake honest -- if that path is
            # ever reached, the test should say so instead of storing a row
            # shaped from the wrong tuple.
            assert len(params) == 9, f"unexpected INSERT with {len(params)} parameters: {text}"
            self.store.append(stored_row(params))
        return self

    def fetchone(self):
        return self._weather


def write(store: list[dict[str, Any]], city_id: str, activity: str, label: str) -> WriteCursor:
    """`POST /recommendations`, as far as the consumer takes it."""
    cursor = WriteCursor(store)
    consumer.store_recommendation_request(
        cursor,
        schemas.RecommendationRequest(
            city_id=city_id,
            forecast_date=TOMORROW,
            activity=activity,
            activity_label=label,
        ),
    )
    return cursor


# -------------------------------------------------------------- the reader ---


def forecast_row(day: date) -> dict[str, Any]:
    return {
        "forecast_date": day,
        "provider": "open-meteo",
        "temp_max_c": 24.0,
        "temp_min_c": 17.0,
        "precip_mm": 0.0,
        "precip_prob": 5,
        "wind_kmh": 9.0,
        "sunshine_hours": 10.0,
        "as_of": "2026-09-24",
    }


@pytest.fixture
def seam(monkeypatch):
    """One store, written by the consumer and read by the agent.

    Returns `(write_one, ask)`. Nothing else is shared between the two halves,
    and in particular the reader is never told which decision the writer took --
    it sees only the rows, which is all it sees in production.
    """
    store: list[dict[str, Any]] = []

    monkeypatch.setattr(consumer, "COASTAL", COASTAL)
    monkeypatch.setattr(dates, "today_in", lambda _timezone: TODAY)
    monkeypatch.setattr(router.queries, "cities", lambda _conn: [LONDON, ROME])
    monkeypatch.setattr(router.queries, "coverage", lambda _conn: COVERAGE)
    monkeypatch.setattr(router.queries, "forecast", lambda *_a, **_k: [forecast_row(TOMORROW)])
    monkeypatch.setattr(router.queries, "events", lambda *_a, **_k: [])
    monkeypatch.setattr(
        router.queries, "expired_events", lambda *_a, **_k: {"expired": 0, "last_checked": None}
    )
    monkeypatch.setattr(router.queries, "facts", lambda *_a, **_k: [])
    monkeypatch.setattr(router.queries, "places", lambda *_a, **_k: [])

    def recommendations(_conn, city_id=None, *, start=None, end=None, activity=None):
        rows = [row for row in store if row["city_id"] == city_id]
        if start:
            rows = [row for row in rows if row["forecast_date"] >= start]
        if end:
            rows = [row for row in rows if row["forecast_date"] <= end]
        if activity:
            rows = [row for row in rows if row["activity"] == activity]
        return rows

    monkeypatch.setattr(router.queries, "recommendations", recommendations)

    monkeypatch.setenv("POSTGRES_READER_PASSWORD", "unit-test")
    from services.agent import main

    monkeypatch.setattr(main, "pool", type("StubPool", (), {"conn": object()})())
    monkeypatch.setattr(main, "Router", lambda _conn: router.Router(object()))
    # The model is not expected on any route here, and a stub that fails is the
    # assertion that says so for every case at once.
    monkeypatch.setattr(
        main.client,
        "chat_json",
        lambda *_a, **_k: pytest.fail("a named-activity question reached the model"),
    )

    def ask(question: str) -> dict[str, Any]:
        return main.ask(main.AskIn(question=question))

    def write_one(city_id: str, activity: str, label: str) -> WriteCursor:
        return write(store, city_id, activity, label)

    return write_one, ask, store


# --------------------------------------- a coast-only name, an inland city ---
#
# The case both integration handoffs flagged, and the one the two halves have to
# agree about: the writer refuses the row and the reader explains the refusal.

SCUBA_LONDON = "Is tomorrow a good day for scuba diving in London?"


def test_a_coast_only_typed_activity_inland_is_refused_and_then_explained(seam):
    write_one, ask, store = seam

    cursor = write_one("london", "scuba_diving", "scuba diving")
    # The writer's half: not even the forecast SELECT. The decision does not
    # depend on the weather, so it is taken before the weather is read.
    assert cursor.statements == []
    assert store == []

    response = ask(SCUBA_LONDON)

    # The reader's half, against that same empty store.
    assert response["llm_called"] is False
    assert "no suitability score on record" in response["answer"]
    assert "London has no coast on record" in response["answer"]
    # The specific fabrication this route exists to prevent: no number of any
    # kind on the line that names the activity.
    for line in response["answer"].splitlines():
        if "scuba diving" in line.lower():
            assert not any(char.isdigit() for char in line), line


def test_the_reason_travels_even_though_nothing_carries_it(seam):
    """The asymmetry worth pinning. There is no row, so there is no column, no
    reason string and no flag: the writer records its decision by storing
    nothing at all. The sentence that explains it is therefore re-derived by the
    reader from the same predicate, and the only thing keeping the two in step is
    that they ask the same question of the same module.
    """
    write_one, ask, store = seam

    write_one("london", "scuba_diving", "scuba diving")
    assert store == [], "this test is about an absence; a stored row voids it"

    answer = ask(SCUBA_LONDON)["answer"]

    assert "has no coast on record" in answer


# ----------------------------------------- the same name, a coastal city -----
#
# The control, and it is not decoration: without it every assertion above would
# still pass if the write path had simply stopped storing anything.


def test_the_same_name_in_a_coastal_city_is_stored_and_read_back_capped(seam):
    write_one, ask, store = seam

    write_one("rome", "scuba_diving", "scuba diving")

    assert len(store) == 1
    assert store[0]["score"] == 69
    assert store[0]["band"] != "good"

    answer = ask("Is tomorrow a good day for scuba diving in Rome?")["answer"]

    # The row the writer stored, reported by the reader as its own.
    assert "scuba diving is fair (69/100)" in answer.lower()
    # The cap says what it is, on the read side as well as on the row.
    assert "waves" in answer
    assert "Lido di Ostia" in answer
    # And no coast sentence, because Rome has one.
    assert "no coast on record" not in answer


# ------------------------------- water that needs no sea, an inland city -----


def test_an_inland_water_name_is_stored_inland_and_never_blamed_on_the_coast(seam):
    """The other side of the split, end to end. `wild swimming` is water and is
    not the sea, so the writer stores it under the same 69 ceiling and the reader
    reports that score without mentioning a coast -- which is the whole point:
    "London has no coast on record" would be a true sentence offered as the
    reason a Hampstead pond cannot be scored.
    """
    write_one, ask, store = seam

    write_one("london", "wild_swimming", "wild swimming")

    assert len(store) == 1
    assert store[0]["score"] == 69
    assert "waves" in " ".join(store[0]["reasons"])

    answer = ask("Is tomorrow a good day for wild swimming in London?")["answer"]

    assert "69/100" in answer
    assert "no coast on record" not in answer
    assert "no suitability score on record" not in answer


def test_an_explicit_sea_word_moves_the_same_inland_name_back_to_a_refusal(seam):
    """The token-precedence change, asserted where it is actually paid for.

    `wild sea swimming` carries a word from each list. Read inland-first, `wild`
    won and the writer stored a capped row for a city with no coast whose entire
    caveat was about water London does not have. `sea` decides it now, and both
    halves move together: no row, and the reader gives the coast as the reason.
    """
    write_one, ask, store = seam

    cursor = write_one("london", "wild_sea_swimming", "wild sea swimming")

    assert cursor.statements == []
    assert store == []

    answer = ask("Is tomorrow a good day for wild sea swimming in London?")["answer"]

    assert "no suitability score on record" in answer
    assert "London has no coast on record" in answer


# --------------------------------- the catalogue half of the same branch -----


def test_a_catalogue_coast_activity_inland_agrees_across_both_halves(seam):
    """The branch above the typed one. Both sides read `requires_coast` off
    data/activities.yml for a name the catalogue holds, and this is the only
    test that drives that read from both sides of one store.
    """
    write_one, ask, store = seam
    assert consumer.ACTIVITIES["surfing"].get("requires_coast"), "premise: surfing needs a coast"

    cursor = write_one("london", "surfing", consumer.ACTIVITIES["surfing"]["label"])

    assert cursor.statements == []
    assert store == []

    answer = ask("Is tomorrow a good day for surfing in London?")["answer"]

    assert "no suitability score on record" in answer
    assert "London has no coast on record" in answer


def test_a_land_activity_is_untouched_by_any_of_this(seam):
    """The bound on the whole file: a typed name with no water in it is stored
    uncapped and read back uncapped, with no caveat about a sea and no mention of
    a coast."""
    write_one, ask, store = seam

    write_one("london", "kite_flying", "kite flying")

    assert len(store) == 1
    assert store[0]["score"] == 100

    answer = ask("Is tomorrow a good day for kite flying in London?")["answer"]

    assert "100/100" in answer
    assert "waves" not in answer
    assert "coast" not in answer
