"""Adversarial grounding probe against the real local model (F3).

The unit tests in tests/unit/test_grounding.py pin the checks with wording
chosen by hand. This one runs the questions that actually failed, through the
actual llama.cpp server, with the real system prompt, and asserts the property
the traveller cares about: whatever the model writes, what comes back is
supported by the rows.

Two outcomes count as a pass for each case:

  * the model stayed inside the facts, and `violations` is empty
  * it did not, and the handler replaced its wording with `grounding.render`

What does not pass is an answer that leaves the pipeline with a claim no row
carries -- and, separately, a case where the model is silently correct while
the validator would have rejected an honest answer. So the probe also asserts
that a hand-written, correct answer is never rejected: a validator that throws
everything away would otherwise look perfect here.

No database. The rows are fixed in this file, so the probe keeps meaning the
same thing after the staged forecast window expires.

Run it in an isolated Compose project, on a network that reaches the model:

  docker compose -p aow-f3 -f compose.yml -f compose.model-probe.yml \\
      --env-file .env.example up -d --no-build --pull never llm
  docker compose -p aow-f3 -f compose.yml -f compose.model-probe.yml \\
      --env-file .env.example run --rm --no-deps probe \\
      python -m tests.integration.model_grounding
"""

from __future__ import annotations

import os
import sys
import time
from datetime import UTC, date, datetime

from services.agent import grounding, router
from services.agent.main import SCHEMA, SYSTEM
from services.common.llm import LlmClient, LlmUnavailable

DAY1 = date(2026, 9, 24)
DAY2 = date(2026, 9, 25)
# The Laver Cup's real span in the snapshot: local midnight on the 25th to
# late on the 27th.
DAY4 = date(2026, 9, 27)

LONDON = {
    "id": "london",
    "name": "London",
    "country": "United Kingdom",
    "timezone": "Europe/London",
    "aliases": [],
    "coastal": False,
}
LISBON = {
    "id": "lisbon",
    "name": "Lisbon",
    "country": "Portugal",
    "timezone": "Europe/Lisbon",
    "aliases": [],
    "coastal": True,
}
COVERAGE = {
    "cities": [LONDON, LISBON],
    "weather_first_date": "2026-09-23",
    "weather_last_date": "2026-10-08",
    "weather_as_of": "2026-09-23",
}


def place(name, category):
    return {
        "id": f"osm:{name.lower().replace(' ', '-')}",
        "name": name,
        "category": category,
        "is_sample": False,
        "source": "OpenStreetMap (ODbL)",
    }


def event(event_id, title, category, day, venue="The O2 arena", last_day=None):
    """Shaped like a `queries.events` row, local dates included (F2)."""
    return {
        "id": event_id,
        "title": title,
        "category": category,
        "venue": venue,
        "starts_at": datetime(day.year, day.month, day.day, 19, tzinfo=UTC),
        "timezone": "Europe/London",
        "starts_on": day,
        "ends_on": last_day or day,
        "is_sample": False,
        "source": "The O2 arena official event listing",
    }


def forecast_row(day, high, low):
    return {
        "forecast_date": day,
        "provider": "open-meteo",
        "temp_max_c": high,
        "temp_min_c": low,
        "precip_mm": 0.4,
        "precip_prob": 15,
        "wind_kmh": 14.0,
        "sunshine_hours": 6.0,
    }


def verdict_row(day, activity, label, score, band):
    return {
        "forecast_date": day,
        "activity": activity,
        "activity_label": label,
        "score": score,
        "band": band,
        "text": None,
    }


def retrieval(question, city=LONDON, window=None, **rows):
    """`window` pins the date range a relative question would otherwise resolve
    against today's clock.

    "Are there any sports events tomorrow in London?" is the review's exact
    wording and has to stay that way, but a probe whose fixtures are dated
    2026-09-25 must keep asserting the same thing after 2026-09-25. Pinning the
    range is the only part that is faked; the question, the prompt and the
    validator are the real ones.
    """
    resolution = router.Resolution(
        question=question,
        city=city,
        window=window or router.dates.parse(question, city["timezone"]),
    )
    text = question.lower()
    for intent, words in router.INTENT_WORDS.items():
        if any(router._mentions(text, word) for word in words):
            resolution.intents.append(intent)
    if not resolution.intents:
        resolution.intents = ["weather", "activities"]
    interests = router.load_interests(router.config.DATA_DIR / "interests.yml")
    for interest, categories in interests.items():
        if router._mentions(text, interest.replace("_", " ")) or router._mentions(text, interest):
            resolution.interests.append(interest)
            resolution.categories.extend(categories)
    resolution.categories = sorted(set(resolution.categories))
    resolution.event_categories = grounding.requested_event_categories(text)
    return router.Retrieval(resolution, COVERAGE, True, **rows)


def surfing_retrieval():
    """London, asked about surfing: a forecast row and no surfing score.

    `unscored_activities` is what the real router sets when a named activity
    has no row for that city -- London is inland, so the rule engine never
    scores surfing there at all.
    """
    result = retrieval(
        "Is it a good day for surfing in London tomorrow?",
        window=router.dates.DateRange(DAY2, DAY2, "tomorrow"),
        forecast=[forecast_row(DAY2, 19.0, 12.0)],
    )
    result.resolution.activities = ["surfing"]
    result.unscored_activities = ["surfing"]
    return result


PLACES = [
    place("Wigmore Hall", "concert_hall"),
    place("Cadogan Hall", "concert_hall"),
    place("Piccadilly Market", "market"),
    place("Elystan Street", "restaurant"),
]
# The verified London concert in data/snapshot/events.jsonl, verbatim: a
# lunchtime recital on 2026-09-25 at LSO St Luke's. It is the row E2 now has to
# answer from.
FREE_FRIDAY = event(
    "lso:free-friday-lunchtime-2026-09-25",
    "Free Friday Lunchtime Concert",
    "concert",
    DAY2,
    venue="LSO St Luke's (Jerwood Hall)",
)
FORECAST = [forecast_row(DAY1, 21.0, 13.0), forecast_row(DAY2, 19.0, 12.0)]
VERDICTS = [
    verdict_row(DAY1, "museums", "A museum day", 80, "good"),
    verdict_row(DAY2, "museums", "A museum day", 74, "good"),
    verdict_row(DAY1, "running", "Running", 55, "fair"),
]

# The London example from ASSIGNMENT.md, plus every probe from the review that
# produced an ungrounded or wrongly dated answer. Each case is
# (name, retrieval, must_contain, must_not_contain); the phrases are matched
# case-insensitively against the answer the traveller actually receives.
CASES: list[tuple[str, object, list[str], list[str]]] = [
    (
        # The verified seed carries one London concert on 2026-09-25, so the
        # answer is derived from that row. It is deliberately NOT the "no
        # concert on record" expectation this case used to carry on the F3
        # branch: the fixtures follow the seed, or the probe stops testing the
        # system that ships.
        "E2 -- the London example question",
        retrieval(
            "What activities can I do with my wife this week in London? "
            "We like concerts, shopping and fine dining.",
            forecast=FORECAST,
            recommendations=VERDICTS,
            places=PLACES,
            events=[FREE_FRIDAY],
        ),
        ["Free Friday Lunchtime Concert"],
        # The nine concert halls are still places. Naming one as the concert is
        # the original E2 failure, and so is reporting the gap that no longer
        # exists.
        ["no concert is on record", "concert at wigmore hall", "concert at cadogan hall"],
    ),
    (
        "concerts only -- one on record",
        retrieval(
            "Which concerts are scheduled in London this week?",
            places=[p for p in PLACES if p["category"] == "concert_hall"],
            events=[FREE_FRIDAY],
        ),
        ["Free Friday Lunchtime Concert"],
        ["no concert is on record"],
    ),
    (
        # The honest-absence path, kept alive now that London has a concert:
        # Lisbon's window holds none. The answer must scope the absence to the
        # record and never to the city.
        "concerts only -- none on record",
        retrieval(
            "Which concerts are scheduled in Lisbon this week?",
            city=LISBON,
            places=[place("Coliseu dos Recreios", "concert_hall")],
        ),
        ["No concert is on record in Lisbon"],
        [
            "no concerts are scheduled in lisbon",
            "no concerts are taking place",
            "nothing is on in lisbon",
        ],
    ),
    (
        # A sports row must never answer a concert-only question. The router
        # filters by category, so this asserts the filter and the validator
        # together: the brief holds only the concert the question asked for.
        "concerts only -- a sports row must not answer it",
        retrieval(
            "Which concerts are scheduled in London this week?",
            events=[FREE_FRIDAY],
        ),
        ["Free Friday Lunchtime Concert"],
        ["laver cup"],
    ),
    (
        "a sports event is reported as itself",
        retrieval(
            "Are there any sports events in London this week?",
            events=[event("theo2:laver-cup-2026", "Laver Cup 2026", "sport", DAY2)],
        ),
        [],
        [],
    ),
    (
        # F2, through the model rather than through SQL: the review asked this
        # on 2026-09-24 and was told there was nothing on, because the row's
        # UTC instant is the 24th. The window is pinned so the question keeps
        # its exact wording without depending on today's date.
        "London sports on 2026-09-25 -- the review's exact question",
        retrieval(
            "Are there any sports events tomorrow in London?",
            window=router.dates.DateRange(DAY2, DAY2, "tomorrow"),
            events=[event("theo2:laver-cup-2026", "Laver Cup 2026", "sport", DAY2, last_day=DAY4)],
        ),
        ["Laver Cup 2026"],
        # The defect this closes was an answer that reported nothing on the
        # day the event actually opens.
        ["no sports event is on record", "no scheduled event"],
    ),
    (
        # The live agent never reaches the model with this one: "surfing" is a
        # named activity, so `ask` renders the stored verdicts in code and
        # makes no LLM call at all. The probe asks the model anyway, because
        # the guarantee worth proving is that a surf verdict cannot reach the
        # traveller even if it did.
        "London surfing -- no score on record",
        surfing_retrieval(),
        ["surf"],
        # The review saw "there is no record" followed by a weather-based
        # verdict in the same answer, and the first run of this probe let one
        # through in wording the verdict list did not hold: "which is not
        # favorable for surfing". So the assertion is now the weather itself --
        # no forecast word may appear in the same breath as surfing.
        # `violations` is what actually forbids the weather-beside-surfing
        # shape now; these are the words the delivered answer must never reach
        # for, kept as a second, blunter net.
        ["suitable", "unsuitable", "favorable", "favourable", "good for surfing"],
    ),
    (
        "the history of Lisbon",
        retrieval("Tell me about the history of Lisbon", city=LISBON),
        [],
        [],
    ),
]

# The control answer for each case: correct, grounded, and written by hand. A
# validator that rejects one of these is too strict to ship.
CONTROLS = {
    "E2 -- the London example question": (
        "On 2026-09-24 a museum day scores 80/100 in London, and on 2026-09-25 it "
        "scores 74/100. Free Friday Lunchtime Concert is at LSO St Luke's (Jerwood "
        "Hall) on 2026-09-25. Wigmore Hall and Cadogan Hall are concert halls on "
        "record, Piccadilly Market is a market, and Elystan Street is a restaurant."
    ),
    "concerts only -- one on record": (
        "Free Friday Lunchtime Concert is at LSO St Luke's (Jerwood Hall) on 2026-09-25."
    ),
    "concerts only -- none on record": "I hold no concert on record for Lisbon in that week.",
    "concerts only -- a sports row must not answer it": (
        "Free Friday Lunchtime Concert is at LSO St Luke's (Jerwood Hall) on 2026-09-25."
    ),
    "a sports event is reported as itself": "Laver Cup 2026 is on at The O2 arena on 2026-09-25.",
    "London sports on 2026-09-25 -- the review's exact question": (
        "Laver Cup 2026 runs at The O2 arena from 2026-09-25 to 2026-09-27, so it is "
        "on tomorrow."
    ),
    "London surfing -- no score on record": (
        "There is no surfing score on record for London. On 2026-09-25 the stored "
        "forecast is a high of 19C and a low of 12C."
    ),
    "the history of Lisbon": "Lisbon is the capital of Portugal, on the river Tagus.",
}


def lisbon_facts():
    return [
        {
            "title": "Lisbon",
            "summary": (
                "Lisbon is the capital and largest city of Portugal, on the Atlantic "
                "coast where the river Tagus meets the sea. It was rebuilt on a grid "
                "plan after the 1755 earthquake."
            ),
        }
    ]


# By name, not by position: the list has grown once and will grow again.
next(c for c in CASES if c[0] == "the history of Lisbon")[1].facts = lisbon_facts()


def wait_for_model(client: LlmClient, timeout: int = 300) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.healthy():
            return
        time.sleep(5)
    raise SystemExit(f"model at {client.base_url} never became healthy")


def main() -> int:
    client = LlmClient(timeout=float(os.environ.get("PROBE_TIMEOUT_S", "180")))
    wait_for_model(client)

    failures: list[str] = []
    for name, result, must_contain, must_not_contain in CASES:
        brief = grounding.build(result)

        # 1. The control: a correct answer must survive.
        control = CONTROLS[name]
        rejected = grounding.violations(control, brief)
        if rejected:
            failures.append(f"{name}: a correct hand-written answer was rejected: {rejected}")

        # 2. The model, on the real prompt.
        started = time.monotonic()
        try:
            parsed = client.chat_json(
                SYSTEM,
                f"Question: {result.resolution.question}\n\n"
                f"Stored data:\n{grounding.prompt_block(brief)}",
                SCHEMA,
                max_tokens=700,
            )
        except LlmUnavailable as exc:
            return int(bool(print(f"SKIP: model unavailable ({exc})")))
        elapsed = time.monotonic() - started
        answer = str(parsed.get("answer", "")).strip()
        broken = grounding.violations(answer, brief)

        if broken:
            delivered = grounding.render(brief)
            verdict = f"model wording rejected ({broken[0]})"
        else:
            gaps = grounding.gap_block(brief, answer)
            delivered = f"{answer}\n\n{gaps}" if gaps else answer
            verdict = "model wording accepted"

        # 3. Whatever path it took, the delivered answer is grounded and states
        #    the gaps. This is the property the traveller actually gets.
        still_broken = grounding.violations(delivered, brief)
        if still_broken:
            failures.append(f"{name}: delivered answer is still ungrounded: {still_broken}")
        lowered = delivered.lower()
        for phrase in must_contain:
            if phrase.lower() not in lowered:
                failures.append(f"{name}: delivered answer never says {phrase!r}")
        for phrase in must_not_contain:
            if phrase.lower() in lowered:
                failures.append(f"{name}: delivered answer says {phrase!r}")

        print(f"\n=== {name} ({elapsed:.1f}s, {verdict}) ===")
        print(f"model: {answer}")
        print(f"delivered: {delivered}")

    print()
    if failures:
        for line in failures:
            print(f"FAIL: {line}")
        return 1
    print(f"PASS: {len(CASES)} adversarial cases stayed grounded against the local model")
    return 0


if __name__ == "__main__":
    sys.exit(main())
