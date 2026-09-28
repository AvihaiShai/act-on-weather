"""What the write path stores when a user asks for one activity by name.

`store_recommendation_request` is the only scoring path a user can aim, and it
had two defects that the default path did not share.

The first is a coast gate that leaked. `rules.activities_for_city` drops an
activity carrying `requires_coast` for an inland city, so the default path
never scores surfing in London, and the agent's answer says in so many words
that such an activity "is never scored there". The requested path reached the
same case through an `else` it shared with genuinely unknown activities, and so
scored it with `rules.GENERIC_CFG` -- which carries no `score_ceiling`. A
pleasant day on land was therefore enough to store London surfing as `good`,
uncapped, contradicting both the default path and the sentence the reader is
shown. Capping it would not have been enough: `beach_day` carries
`requires_coast` with no ceiling of its own, so there is no number to cap it
to, and inventing one is not open to us. The write path stores no row instead,
which is the absence the default path and the reader already agree on.

The second is narrower. `rule_version` was in the upsert's INSERT columns but
not in its `ON CONFLICT DO UPDATE SET`, so asking a second time for an activity
that already had a row rewrote the score, the band and the reasons while
leaving the previous version stamped on it. Nothing reads the column yet, which
is exactly why it was worth fixing now: a version-triggered rescore is the one
thing that would have to trust it.

These tests call the handler with the fake cursor the other consumer tests use
and read the statement it would run. They connect to nothing.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from services.common import schemas
from services.consumer import main as consumer

ROOT = Path(__file__).resolve().parents[2]

# The real catalogue and the real city list, so a test cannot drift from the
# data the container ships. `consumer.ACTIVITIES` is already loaded from
# data/activities.yml at import; only `COASTAL` is empty until `seed_cities`
# runs against the database, so each test fills it from the same file.
CITIES = yaml.safe_load((ROOT / "data" / "cities.yml").read_text(encoding="utf-8"))["cities"]
COASTAL = {city["slug"]: bool(city.get("coastal", False)) for city in CITIES}

INLAND = sorted(slug for slug, coastal in COASTAL.items() if not coastal)
COAST_ONLY = sorted(key for key, cfg in consumer.ACTIVITIES.items() if cfg.get("requires_coast"))

# Warm, dry, calm and sunny: every land rule in the catalogue is satisfied, so
# nothing here can hold a score down except a ceiling or a refusal.
PERFECT_DAY = {
    "temp_max_c": 24.0,
    "temp_min_c": 17.0,
    "precip_mm": 0.0,
    "precip_prob": 5.0,
    "wind_kmh": 9.0,
    "uv_index": 4.0,
    "sunshine_hours": 10.0,
    "as_of": "2026-09-27T00:00:00Z",
}


class Cursor:
    """The fake the other consumer tests use: record the SQL, answer the SELECT."""

    def __init__(self, weather=PERFECT_DAY):
        self.statements: list[str] = []
        self.params: list[tuple] = []
        self._weather = weather

    def execute(self, sql, params=None):
        self.statements.append(" ".join(sql.split()))
        self.params.append(params)
        return self

    def fetchone(self):
        return self._weather


def request(city_id, activity, label):
    return schemas.RecommendationRequest(
        city_id=city_id,
        forecast_date=date(2026, 9, 28),
        activity=activity,
        activity_label=label,
    )


@pytest.fixture(autouse=True)
def coastal(monkeypatch):
    monkeypatch.setattr(consumer, "COASTAL", COASTAL)


def test_the_city_list_still_has_an_inland_city_and_the_catalogue_a_coastal_activity():
    """Guard the premise: without both, every test below would pass vacuously."""
    assert INLAND == ["london"]
    assert COAST_ONLY == ["beach_day", "boat_ride", "fishing", "surfing", "swimming"]
    # beach_day is the reason the refusal cannot be a cap: it needs a coast and
    # carries no ceiling, so there is no number to hold it under `good`.
    assert consumer.ACTIVITIES["beach_day"].get("score_ceiling") is None


@pytest.mark.parametrize("activity", COAST_ONLY)
def test_a_coast_activity_asked_for_an_inland_city_stores_no_row(activity):
    """No row at all, which is the absence the reader is already built around.

    `router` collects an activity with no row into `unscored_activities` and the
    answer becomes "no suitability score on record; london has no coast on
    record". Storing a `failed` row instead would break that: nothing in
    `queries.recommendations` filters on status or a null score, so the row
    would come back as a scored one and the answer would read "Surfing is None
    (None/100)." in place of the coast sentence.
    """
    cursor = Cursor()
    label = consumer.ACTIVITIES[activity].get("label", activity)
    consumer.store_recommendation_request(cursor, request("london", activity, label))

    assert cursor.statements == []


@pytest.mark.parametrize("activity", COAST_ONLY)
def test_a_coast_activity_inland_never_reaches_the_uncapped_generic_scorer(activity, monkeypatch):
    """The defect was reaching `GENERIC_CFG` at all: it carries no ceiling."""
    monkeypatch.setattr(
        consumer.rules,
        "score_requested",
        lambda *_a, **_k: pytest.fail("a requires_coast activity was scored by the generic rule"),
    )
    label = consumer.ACTIVITIES[activity].get("label", activity)
    consumer.store_recommendation_request(Cursor(), request("london", activity, label))


@pytest.mark.parametrize("activity", COAST_ONLY)
def test_the_decision_does_not_depend_on_a_forecast_being_stored(activity):
    """The reason is structural: there is no coast whatever the weather says."""
    cursor = Cursor(weather=None)
    label = consumer.ACTIVITIES[activity].get("label", activity)
    consumer.store_recommendation_request(cursor, request("london", activity, label))

    # Not even the SELECT: it returns before reading the forecast, so it cannot
    # be mistaken for the "no stored weather for that date" refusal below it.
    assert cursor.statements == []


@pytest.mark.parametrize("activity", ["surfing", "swimming", "fishing", "boat_ride"])
def test_a_coastal_city_still_scores_the_activity_under_its_own_ceiling(activity):
    cursor = Cursor()
    label = consumer.ACTIVITIES[activity].get("label", activity)
    consumer.store_recommendation_request(cursor, request("rome", activity, label))

    assert "INSERT INTO recommendations" in cursor.statements[-1]
    score, band = cursor.params[-1][4], cursor.params[-1][5]
    assert score <= consumer.ACTIVITIES[activity]["score_ceiling"]
    assert band != "good"


def test_an_activity_the_catalogue_does_not_carry_still_gets_the_generic_score():
    """The fallback keeps its real job: an unknown activity, scored and labelled."""
    cursor = Cursor()
    consumer.store_recommendation_request(cursor, request("london", "kite_flying", "kite flying"))

    stored = cursor.statements[-1]
    assert "INSERT INTO recommendations" in stored
    assert "'pending'" in stored
    score, reasons = cursor.params[-1][4], cursor.params[-1][6]
    assert score is not None
    assert "general outdoor comfort" in reasons


def test_asking_again_restamps_the_version_of_the_engine_that_just_scored_it():
    cursor = Cursor()
    consumer.store_recommendation_request(cursor, request("london", "running", "running"))

    stored = cursor.statements[-1]
    set_clause = stored.split("DO UPDATE SET", 1)[1]
    # The score is being recomputed on this statement, so the version that
    # computed it has to travel with it. Without this the row kept whichever
    # version happened to score it first.
    assert "rule_version = EXCLUDED.rule_version" in set_clause
    assert consumer.RULE_VERSION in cursor.params[-1]


# ------------- the coast gate reached by a name the catalogue lacks ----------
#
# The third defect, and the same integrity breach as the first one reached by a
# different route. `requires_coast` is a property of a row in
# data/activities.yml, so the gate above could only ever ask the catalogue --
# and every water activity the catalogue does NOT list walked past it. "scuba
# diving" asked for London reached `rules.GENERIC_CFG`, which carries no
# ceiling, and a pleasant day on land stored scuba diving in London as `good`,
# 100/100, for a city with no coast on record.
#
# Found by reading the source, not by running it: this case was never POSTed to
# the live demo, and nothing in this file touches a database.

# Every name here has to answer YES to both questions the split in
# `rules.needs_coast` separated: does the water decide how good this is, and does
# it need the SEA. `wild_swimming` was on this list and answers only the first --
# swimming in a river, a lake or a Hampstead pond needs no coast, and refusing
# the row "because London has no coast on record" offered a true fact as the
# cause of something it did not cause. It moved to
# tests/unit/test_inland_water.py, which asserts the other side of the split.
TYPED_SEA = ["scuba_diving", "snorkelling", "kite_surfing", "sea_kayaking"]


def test_none_of_the_typed_sea_activities_is_in_the_catalogue():
    """Guard the premise. If any of these were ever added to
    data/activities.yml it would be handled by the catalogue branch instead, and
    every test below would be passing for the wrong reason."""
    for activity in TYPED_SEA:
        assert activity not in consumer.ACTIVITIES, activity
    # And both word tests really do fire on them, while a land activity the
    # catalogue also lacks is untouched by either. `needs_coast` is asserted as
    # well as `names_water` because the two stopped being one test: a name that
    # names water without needing the sea belongs in test_inland_water.py, and
    # leaving it here would assert a refusal this file cannot justify.
    for activity in TYPED_SEA:
        assert consumer.rules.names_water(activity), activity
        assert consumer.rules.needs_coast(activity), activity
    assert not consumer.rules.names_water("kite_flying")
    assert not consumer.rules.names_water("rock_climbing")


@pytest.mark.parametrize("activity", TYPED_SEA)
def test_a_typed_sea_activity_asked_for_an_inland_city_stores_no_row(activity):
    """The same absence the catalogue's own sea activities produce for London,
    and for the same reason: no coast, so nothing to score it from."""
    cursor = Cursor()
    consumer.store_recommendation_request(
        cursor, request("london", activity, activity.replace("_", " "))
    )

    assert cursor.statements == []


@pytest.mark.parametrize("activity", TYPED_SEA)
def test_a_typed_sea_activity_inland_never_reaches_the_uncapped_generic_scorer(
    activity, monkeypatch
):
    """The defect was reaching `GENERIC_CFG` at all."""
    monkeypatch.setattr(
        consumer.rules,
        "score_requested",
        lambda *_a, **_k: pytest.fail("a typed sea activity was scored by the generic rule"),
    )
    consumer.store_recommendation_request(
        Cursor(), request("london", activity, activity.replace("_", " "))
    )


@pytest.mark.parametrize("activity", TYPED_SEA)
def test_the_typed_decision_does_not_depend_on_a_forecast_being_stored(activity):
    cursor = Cursor(weather=None)
    consumer.store_recommendation_request(
        cursor, request("london", activity, activity.replace("_", " "))
    )

    assert cursor.statements == []


def test_a_typed_water_activity_that_needs_no_sea_is_stored_for_the_inland_city():
    """The other side of the split, asserted where the refusal is actually taken.

    tests/unit/test_inland_water.py holds `rules.needs_coast` to the catalogue's
    `distinct_names`, but the decision that costs a traveller a row is this
    handler's, and nothing asserted it there. So: London, no coast, and a row is
    stored anyway -- under the same 69 ceiling with the same caveat, because the
    ceiling follows `names_water` and the water here is no better measured than
    the sea is.
    """
    cursor = Cursor()
    consumer.store_recommendation_request(
        cursor, request("london", "wild_swimming", "wild swimming")
    )

    assert "INSERT INTO recommendations" in cursor.statements[-1]
    score, band, reasons = cursor.params[-1][4], cursor.params[-1][5], cursor.params[-1][6]
    assert score == 69, f"wild swimming in London scored {score}"
    assert band != "good"
    assert "waves" in reasons
    # The refusal sentence names the coast, so it must not appear for a name that
    # the coast has nothing to do with.
    assert "coast" not in reasons


@pytest.mark.parametrize("activity", TYPED_SEA)
def test_a_typed_sea_activity_in_a_coastal_city_is_capped_like_a_catalogue_one(activity):
    """A coast exists, so the row is stored -- under the ceiling the four
    catalogue sea activities carry, and never in the `good` band. PERFECT_DAY
    violates no land rule, so 100 is what an uncapped score would be."""
    cursor = Cursor()
    consumer.store_recommendation_request(
        cursor, request("rome", activity, activity.replace("_", " "))
    )

    assert "INSERT INTO recommendations" in cursor.statements[-1]
    score, band, reasons = cursor.params[-1][4], cursor.params[-1][5], cursor.params[-1][6]
    assert score == 69, f"{activity} scored {score}"
    assert band != "good"
    # The cap has to say what it is. A number quietly lowered behind the
    # reader's back is what `_apply_ceiling`'s reason exists to prevent, and the
    # reasons are what the model is handed to write from.
    assert "waves" in reasons
    assert "general outdoor comfort" in reasons


def test_the_ceiling_matches_the_catalogue_rather_than_being_a_second_number():
    """69 is the catalogue's own figure for this situation. Two numbers for one
    rule is how they drift apart."""
    ceilings = {
        cfg["score_ceiling"]
        for cfg in consumer.ACTIVITIES.values()
        if cfg.get("sea_state_unmeasured")
    }
    assert ceilings == {consumer.rules.GENERIC_SEA_CFG["score_ceiling"]}


def test_a_typed_land_activity_is_untouched_by_any_of_this():
    """The bound on the repair, asserted for the inland city that provoked it.
    `kite_flying` shares a word with `kite_surfing` and is not a water
    activity."""
    cursor = Cursor()
    consumer.store_recommendation_request(cursor, request("london", "kite_flying", "kite flying"))

    score, reasons = cursor.params[-1][4], cursor.params[-1][6]
    assert score == 100
    assert "waves" not in reasons
    assert "general outdoor comfort" in reasons
