"""The grounding layer (F3): typed facts, deterministic rendering, validation.

Every test here is a question the live agent got wrong before this layer
existed. The observed answers are quoted in the test that closes them, so a
regression is recognisable rather than just red.

The clock is frozen and every date is written out. These tests have to keep
meaning something after the staged forecast window has expired.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from services.agent import grounding, router

# The three days these tests talk about. Fixed, not derived from `today`.
DAY1 = date(2026, 9, 24)
DAY2 = date(2026, 9, 25)
DAY3 = date(2026, 9, 26)

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


def place(name, category, is_sample=False):
    return {
        "id": f"osm:{name.lower().replace(' ', '-')}",
        "name": name,
        "category": category,
        "is_sample": is_sample,
        "source": "OpenStreetMap (ODbL)",
    }


def event(event_id, title, category, day, venue="The O2 arena", is_sample=False, last_day=None):
    """A row shaped the way `queries.events` returns one.

    `starts_on`/`ends_on` are the city-local dates the query derives (F2), so
    the fixture cannot drift into a shape the database never produces. Pass
    `last_day` for an event that runs across several days.
    """
    return {
        "id": event_id,
        "title": title,
        "category": category,
        "venue": venue,
        "starts_at": datetime(day.year, day.month, day.day, 19, tzinfo=UTC),
        "timezone": "Europe/London",
        "starts_on": day,
        "ends_on": last_day or day,
        "is_sample": is_sample,
        "source": "The O2 arena official event listing",
    }


def forecast_row(day, high=21.0, low=13.0):
    return {
        "forecast_date": day,
        "provider": "open-meteo",
        "temp_max_c": high,
        "temp_min_c": low,
        "precip_mm": 0.4,
        "precip_prob": 15,
        "wind_kmh": 14.0,
        "sunshine_hours": 6.5,
    }


def verdict_row(day, activity, label, score, band="good"):
    return {
        "forecast_date": day,
        "activity": activity,
        "activity_label": label,
        "score": score,
        "band": band,
        "text": None,
    }


def retrieval(question, city=LONDON, **rows):
    """A Retrieval assembled by hand, with the question resolved for real.

    The resolution is the actual router code -- only the SQL is replaced -- so
    intent, interest and event-category detection are under test here too.
    """
    resolution = router.Resolution(
        question=question,
        city=city,
        window=router.dates.parse(question, city["timezone"]),
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


# ------------------------------------------------------ event categories ----


@pytest.mark.parametrize(
    "question, expected",
    [
        ("Which concerts are scheduled in London this week?", ["concert"]),
        ("Any gigs in London?", ["concert"]),
        ("Are there any sports events tomorrow in London?", ["sport"]),
        ("Is there a football match in London on Friday?", ["sport"]),
        ("What is the weather tomorrow in Rome?", []),
        ("What can I do in London this week?", []),
    ],
)
def test_a_question_resolves_to_the_event_kind_it_asked_about(question, expected):
    """Empty means "no particular kind", and the router must not read it as
    "no events": an open question still has to see everything that is on."""
    assert grounding.requested_event_categories(question.lower()) == expected


def test_the_second_example_question_asks_for_concerts_only():
    text = (
        "what activities can i do with my wife this week in london? "
        "we like concerts, shopping and fine dining."
    )
    assert grounding.requested_event_categories(text) == ["concert"]


@pytest.mark.parametrize(
    "question, category",
    [
        ("Any comedy on in London this week?", "comedy"),
        ("What markets are on in London this week?", "market"),
    ],
)
def test_scheduled_category_does_not_turn_into_an_activity_verdict(monkeypatch, question, category):
    monkeypatch.setattr(router.queries, "cities", lambda _conn: [LONDON])
    monkeypatch.setattr(router.dates, "today_in", lambda _tz: DAY1)

    resolution = router.Router(object()).resolve(question)

    assert "events" in resolution.intents
    assert "activities" not in resolution.intents
    assert resolution.event_categories == [category]
    assert resolution.activities == []


def test_comedy_suitability_still_asks_for_an_activity_verdict(monkeypatch):
    monkeypatch.setattr(router.queries, "cities", lambda _conn: [LONDON])
    monkeypatch.setattr(router.dates, "today_in", lambda _tz: DAY1)

    resolution = router.Router(object()).resolve("Is comedy suitable in London tomorrow?")

    assert "activities" in resolution.intents
    assert "events" not in resolution.intents
    assert resolution.activities == ["standup_comedy"]


def test_the_router_filters_events_by_the_requested_kind(monkeypatch):
    """The reproduced failure: 'which concerts are scheduled in London this
    week?' returned the Laver Cup, a tennis tournament stored as `sport`,
    because the query asked for every category and the model took what it was
    given."""
    stored = [
        event("theo2:laver-cup-2026", "Laver Cup 2026", "sport", DAY2),
        event("theo2:the-strokes", "The Strokes", "concert", date(2026, 10, 6)),
    ]
    asked = {}

    def events(_conn, _city, *, start=None, end=None, categories=None, **_kwargs):
        asked["categories"] = categories
        # Overlap on local dates, the way the real query filters (F2).
        rows = [r for r in stored if r["ends_on"] >= start and r["starts_on"] <= end]
        if categories:
            rows = [r for r in rows if r["category"] in categories]
        return rows

    monkeypatch.setattr(router.queries, "cities", lambda _conn: [LONDON])
    monkeypatch.setattr(router.queries, "coverage", lambda _conn: COVERAGE)
    monkeypatch.setattr(router.queries, "events", events)
    # Asked only when the retrieval above comes back empty, so a gap can
    # say whether the feed went stale or was never there.
    monkeypatch.setattr(
        router.queries, "expired_events", lambda *_a, **_k: {"expired": 0, "last_checked": None}
    )
    monkeypatch.setattr(router.queries, "places", lambda *_a, **_k: [])
    monkeypatch.setattr(router.dates, "today_in", lambda _tz: DAY1)

    result = router.Router(object()).retrieve("Which concerts are scheduled in London this week?")

    assert asked["categories"] == ["concert"]
    assert result.events == []
    brief = grounding.build(result)
    assert "Laver Cup" not in grounding.render(brief)
    assert any(g.subject == "events:concert" for g in brief.gaps)


# ------------------------------------------------ places are not events ----


def test_a_concert_hall_is_typed_as_a_place_not_a_concert():
    result = retrieval(
        "Which concerts are scheduled in London this week?",
        places=[place("Wigmore Hall", "concert_hall"), place("Cadogan Hall", "concert_hall")],
    )
    brief = grounding.build(result)

    assert [p.name for p in brief.places] == ["Wigmore Hall", "Cadogan Hall"]
    assert brief.events == []
    block = grounding.prompt_block(brief)
    assert "NOT scheduled events" in block
    assert "concert hall" in block
    answer = grounding.render(brief)
    assert "Wigmore Hall" in answer
    # The venue may be named. What it may not do is acquire a programme.
    assert "concert at" not in answer.lower()


def test_a_venue_offered_as_a_concert_is_rejected():
    """The observed E2 answer, word for word."""
    result = retrieval(
        "What activities can I do with my wife this week in London? "
        "We like concerts, shopping and fine dining.",
        forecast=[forecast_row(DAY1)],
        recommendations=[verdict_row(DAY1, "museums", "A museum day", 80)],
        places=[place("Wigmore Hall", "concert_hall"), place("Elystan Street", "restaurant")],
    )
    brief = grounding.build(result)
    answer = (
        "You can enjoy concerts, shopping, and fine dining in London this week. "
        "Concerts can be enjoyed at Cadogan Hall or Wigmore Hall."
    )

    broken = grounding.violations(answer, brief)
    assert any("no stored event row" in v for v in broken)
    assert any("Wigmore Hall" in v and "scheduled event" in v for v in broken)


def test_naming_a_venue_without_claiming_an_event_is_allowed():
    result = retrieval(
        "What activities can I do with my wife this week in London? "
        "We like concerts, shopping and fine dining.",
        forecast=[forecast_row(DAY1)],
        recommendations=[verdict_row(DAY1, "museums", "A museum day", 80)],
        places=[place("Wigmore Hall", "concert_hall"), place("Elystan Street", "restaurant")],
    )
    brief = grounding.build(result)
    answer = (
        "Wigmore Hall is a concert hall on record, and Elystan Street is a restaurant. "
        "A museum day scores 80/100 on 2026-09-24."
    )
    assert grounding.violations(answer, brief) == []


def test_the_gap_sentence_is_written_by_code_and_appended():
    result = retrieval(
        "What activities can I do with my wife this week in London? "
        "We like concerts, shopping and fine dining.",
        forecast=[forecast_row(DAY1)],
        recommendations=[verdict_row(DAY1, "museums", "A museum day", 80)],
        places=[place("Wigmore Hall", "concert_hall")],
        events=[event("theo2:laver-cup-2026", "Laver Cup 2026", "sport", DAY2)],
    )
    brief = grounding.build(result)

    # The sport row was not asked for, so it is not retrieved in practice; what
    # matters here is that a missing concert is stated even when other event
    # rows exist.
    block = grounding.gap_block(brief)
    assert "No concert is on record in London" in block
    assert "not evidence that none is scheduled" in block
    # The same sentence reaches the traveller on either path: appended after
    # the model, or as one more line of the rendered answer.
    sentence = next(g.text for g in brief.gaps if g.subject == "events:concert")
    assert sentence in block
    assert sentence in grounding.render(brief)


# --------------------------------------------- an event keeps its category ----


def test_relabelling_a_stored_event_is_rejected():
    """'A focused "Which concerts are scheduled in London this week?" reply
    cited the Laver Cup (category sport) as a concert.'"""
    result = retrieval(
        "What is on in London this week?",
        events=[event("theo2:laver-cup-2026", "Laver Cup 2026", "sport", DAY2)],
    )
    brief = grounding.build(result)

    broken = grounding.violations(
        "The following concerts are scheduled: Laver Cup 2026 at The O2 arena.", brief
    )
    assert any("Laver Cup 2026" in v for v in broken)


def test_a_stored_event_reported_as_itself_passes():
    result = retrieval(
        "What is on in London this week?",
        events=[event("theo2:laver-cup-2026", "Laver Cup 2026", "sport", DAY2)],
    )
    brief = grounding.build(result)
    answer = "Laver Cup 2026 is on at The O2 arena on 2026-09-25."
    assert grounding.violations(answer, brief) == []


def test_stating_the_absence_of_an_event_is_not_a_claim_that_one_exists():
    """The negation guard. Without it the honest answer fails its own check."""
    result = retrieval("Which concerts are scheduled in London this week?")
    brief = grounding.build(result)
    assert grounding.violations("There is no concert on record in London this week.", brief) == []


# ------------------------------------------------------ verdicts and gaps ----


def test_a_verdict_with_no_stored_score_is_rejected():
    """The Lisbon history answer acquired 'a suitability verdict for the
    history of Lisbon' from a question that retrieved no scores at all."""
    result = retrieval("Tell me about the history of Lisbon", city=LISBON)
    result.facts = [{"title": "Lisbon", "summary": "Capital of Portugal on the river Tagus."}]
    brief = grounding.build(result)

    assert brief.verdicts == []
    broken = grounding.violations(
        "Lisbon is ideal for sightseeing and its old town is perfect for walking.", brief
    )
    assert "gives a suitability verdict with no stored score" in broken


def test_describing_a_place_beyond_its_category_is_rejected():
    """Also from the Lisbon answer: 'The Atelier-Museu Julio Pomar preserves
    the work of Julio Pomar' and 'the Portuguese Riviera ... is a popular
    tourist destination'. Both came from the names alone."""
    result = retrieval("Tell me about the history of Lisbon", city=LISBON)
    result.places = [place("Museu Maynense", "museum"), place("Carmo Archaeological", "museum")]
    result.facts = [{"title": "Lisbon", "summary": "Capital of Portugal on the river Tagus."}]
    brief = grounding.build(result)

    broken = grounding.violations(
        "Museu Maynense houses significant cultural collections and is popular.", brief
    )
    assert any("Museu Maynense" in v and "beyond its stored category" in v for v in broken)


def test_a_description_that_the_stored_background_supports_is_allowed():
    result = retrieval("Tell me about the history of Lisbon", city=LISBON)
    result.places = [place("Museu Maynense", "museum")]
    result.facts = [
        {
            "title": "Museu Maynense",
            "summary": "The Museu Maynense is known for its natural history collection.",
        }
    ]
    brief = grounding.build(result)
    assert grounding.violations("Museu Maynense is known for its collection.", brief) == []


def test_a_verdict_on_an_unscored_activity_is_rejected():
    """'London surfing answer first said no record and then gave a
    weather-based unsuitability verdict.'"""
    result = retrieval(
        "Is it a good day for surfing in London tomorrow?",
        forecast=[forecast_row(DAY2)],
        recommendations=[verdict_row(DAY2, "running", "Running", 61, "fair")],
    )
    result.unscored_activities = ["surfing"]
    brief = grounding.build(result)

    assert any(g.subject == "activity:surfing" for g in brief.gaps)
    assert "no coast on record" in grounding.render(brief)
    broken = grounding.violations(
        "There is no surfing score on record. The wind is light, so surfing is "
        "good for tomorrow anyway.",
        brief,
    )
    assert any("surfing" in v for v in broken)


def test_an_unscored_activity_is_stated_plainly_in_the_prompt():
    result = retrieval("Is it a good day for surfing in London tomorrow?")
    result.unscored_activities = ["surfing"]
    brief = grounding.build(result)
    block = grounding.prompt_block(brief)
    assert "ASKED ABOUT BUT NOT ON RECORD" in block
    assert "no coast on record" in block


# ------------------------------------------------------------- rendering ----


def test_the_rendered_answer_carries_every_retrieved_row():
    result = retrieval(
        "What can I do in London this week? We like shopping.",
        forecast=[forecast_row(DAY1), forecast_row(DAY2, high=19.0)],
        recommendations=[
            verdict_row(DAY1, "museums", "A museum day", 80),
            verdict_row(DAY1, "running", "Running", 55, "fair"),
            verdict_row(DAY2, "museums", "A museum day", 72),
        ],
        places=[place("Piccadilly Market", "market")],
        events=[event("theo2:laver-cup-2026", "Laver Cup 2026", "sport", DAY2)],
    )
    answer = grounding.render(grounding.build(result))

    assert "2026-09-24: high 21C, low 13C" in answer
    assert "2026-09-24: best rated activity is A museum day (80/100)" in answer
    assert "2026-09-25: best rated activity is A museum day (72/100)" in answer
    assert "Piccadilly Market" in answer
    assert "2026-09-25 Laver Cup 2026 (sport) at The O2 arena" in answer
    # The runner-up did not win the day and must not be reported as if it had.
    assert "Running" not in answer


def test_sample_rows_stay_labelled_as_samples():
    result = retrieval(
        "What is on in London this week?",
        events=[
            event("sample:concert", "Sample: evening concert", "concert", DAY2, is_sample=True)
        ],
    )
    brief = grounding.build(result)
    assert "[sample data]" in grounding.render(brief)
    assert "[sample data]" in grounding.prompt_block(brief)


def test_a_place_only_question_still_renders_something_useful():
    result = retrieval(
        "Where can we go shopping in London?",
        places=[place("Piccadilly Market", "market"), place("East Street Market", "market")],
    )
    answer = grounding.render(grounding.build(result))
    assert "Piccadilly Market" in answer and "East Street Market" in answer
    assert "not a programme" in answer


# ------------------------------------------------------- the whole handler ----


def _stub_agent(monkeypatch, result):
    from services.agent import main

    monkeypatch.setattr(main, "pool", type("StubPool", (), {"conn": object()})())
    monkeypatch.setattr(
        main,
        "Router",
        lambda _conn: type("StubRouter", (), {"retrieve": lambda self, _q: result})(),
    )
    return main


def test_an_ungrounded_answer_is_replaced_by_the_rendered_one(monkeypatch):
    result = retrieval(
        "What activities can I do with my wife this week in London? "
        "We like concerts, shopping and fine dining.",
        forecast=[forecast_row(DAY1)],
        recommendations=[verdict_row(DAY1, "museums", "A museum day", 80)],
        places=[place("Wigmore Hall", "concert_hall"), place("Elystan Street", "restaurant")],
    )
    main = _stub_agent(monkeypatch, result)
    monkeypatch.setattr(
        main.client,
        "chat_json",
        lambda *_a, **_k: {
            "answer": "Concerts can be enjoyed at Wigmore Hall this week, and Elystan "
            "Street is famous for its fine dining."
        },
    )

    response = main.ask(main.AskIn(question=result.resolution.question))

    assert response["llm_called"] is True
    assert "note" in response and "do not support" in response["note"]
    assert response["answer"] == grounding.render(grounding.build(result))
    assert "No concert is on record in London" in response["answer"]


def test_a_grounded_answer_survives_and_gains_the_gap_sentence(monkeypatch):
    result = retrieval(
        "What activities can I do with my wife this week in London? "
        "We like concerts, shopping and fine dining.",
        forecast=[forecast_row(DAY1)],
        recommendations=[verdict_row(DAY1, "museums", "A museum day", 80)],
        places=[place("Wigmore Hall", "concert_hall"), place("Elystan Street", "restaurant")],
    )
    main = _stub_agent(monkeypatch, result)
    grounded = (
        "On 2026-09-24 a museum day scores 80/100 in London. Wigmore Hall is a concert "
        "hall on record and Elystan Street is a restaurant on record."
    )
    monkeypatch.setattr(main.client, "chat_json", lambda *_a, **_k: {"answer": grounded})

    response = main.ask(main.AskIn(question=result.resolution.question))

    assert "note" not in response
    assert response["answer"].startswith(grounded)
    assert "No concert is on record in London" in response["answer"]


def test_a_named_activity_never_reaches_the_model(monkeypatch):
    """A specific activity is answered from its stored rows, so there is no
    wording in which a verdict can be invented."""
    result = retrieval(
        "Is it a good day for surfing in London tomorrow?",
        forecast=[forecast_row(DAY2)],
    )
    result.resolution.activities = ["surfing"]
    result.unscored_activities = ["surfing"]
    main = _stub_agent(monkeypatch, result)

    def fail(*_a, **_k):
        raise AssertionError("the model must not be called for a named activity")

    monkeypatch.setattr(main.client, "chat_json", fail)
    response = main.ask(main.AskIn(question=result.resolution.question))

    assert response["llm_called"] is False
    assert "no suitability score on record" in response["answer"]
    assert "London has no coast on record" in response["answer"]


def test_an_empty_retrieval_names_what_was_missing(monkeypatch):
    result = retrieval("Which concerts are scheduled in London this week?")
    main = _stub_agent(monkeypatch, result)

    def fail(*_a, **_k):
        raise AssertionError("nothing was retrieved; there is nothing to phrase")

    monkeypatch.setattr(main.client, "chat_json", fail)
    response = main.ask(main.AskIn(question=result.resolution.question))

    assert response["llm_called"] is False
    assert "No concert is on record in London" in response["answer"]


def test_the_model_being_down_still_gives_the_grounded_answer(monkeypatch):
    result = retrieval(
        "What can I do in London this week?",
        forecast=[forecast_row(DAY1)],
        recommendations=[verdict_row(DAY1, "museums", "A museum day", 80)],
    )
    main = _stub_agent(monkeypatch, result)

    def unavailable(*_a, **_k):
        raise main.LlmUnavailable("model stopped")

    monkeypatch.setattr(main.client, "chat_json", unavailable)
    response = main.ask(main.AskIn(question=result.resolution.question))

    assert response["llm_called"] is False
    assert response["answer"] == grounding.render(grounding.build(result))


# ----------------------------------------------------------- the vocabulary ----


def test_claim_words_are_a_subset_of_routing_words_or_phrases():
    """A claim word that the router does not recognise would mean the
    validator rejecting a category the retrieval never even tried to fetch."""
    for category, cfg in grounding.event_types().items():
        words = {w.lower() for w in cfg["words"]}
        for claim in cfg["claims"]:
            assert (
                claim.lower() in words or " " in claim
            ), f"{claim!r} in {category} is neither a routing word nor a phrase"


def test_every_event_category_has_a_label():
    for category, cfg in grounding.event_types().items():
        assert cfg.get("label"), f"{category} has no label for the gap sentence"


@pytest.mark.parametrize(
    "day_offset, expected",
    [(0, "2026-09-24"), (1, "2026-09-25")],
)
def test_the_rendered_event_date_is_the_stored_one(day_offset, expected):
    """This module reports the row's date and does not re-date it. Which
    calendar day a stored timestamp belongs to in the city's own time zone is
    F2's question, and this assertion has to keep holding after F2 lands."""
    day = DAY1 + timedelta(days=day_offset)
    result = retrieval(
        "What is on in London this week?",
        events=[event("theo2:x", "Some Event Name", "concert", day)],
    )
    brief = grounding.build(result)
    assert brief.events[0].day == expected


# ------------------------------------------------------- dates and names ----


def test_a_date_no_row_carries_is_rejected():
    """An event moved by a day reads as confirmed, which makes it worse than
    no answer at all."""
    result = retrieval(
        "What is on in London this week?",
        events=[event("theo2:the-strokes", "The Strokes", "concert", DAY2)],
    )
    brief = grounding.build(result)

    assert grounding.violations("The Strokes play on 2026-09-25.", brief) == []
    assert any(
        "2026-11-14" in v for v in grounding.violations("The Strokes play on 2026-11-14.", brief)
    )


def test_a_long_form_date_is_checked_too():
    result = retrieval(
        "What is on in London this week?",
        events=[event("theo2:the-strokes", "The Strokes", "concert", DAY2)],
    )
    brief = grounding.build(result)
    assert grounding.violations("The Strokes play on September 25, 2026.", brief) == []
    assert grounding.violations("The Strokes play on 14 November 2026.", brief)


def test_a_proper_noun_from_nowhere_is_rejected():
    """The Lisbon answer invented a Roman era and an Alfama district from a
    one-line summary that mentioned neither."""
    result = retrieval("Tell me about the history of Lisbon", city=LISBON)
    result.facts = [
        {
            "title": "Lisbon",
            "summary": "Lisbon is the capital of Portugal, on the river Tagus.",
        }
    ]
    brief = grounding.build(result)

    assert grounding.violations("Lisbon is the capital of Portugal, on the Tagus.", brief) == []
    broken = grounding.violations(
        "Lisbon has a history dating to the Roman era, and the Alfama district survives.", brief
    )
    assert any("Roman" in v for v in broken)
    assert any("Alfama" in v for v in broken)


def test_words_the_traveller_typed_are_not_inventions():
    """Echoing the question back is not a claim about the world."""
    result = retrieval("Tell me about the history of Lisbon", city=LISBON)
    result.facts = [{"title": "Lisbon", "summary": "Capital of Portugal."}]
    brief = grounding.build(result)
    assert grounding.violations("You asked about Lisbon; it is the capital.", brief) == []


def test_calendar_and_unit_words_are_not_inventions():
    result = retrieval(
        "What is the weather in London this week?",
        forecast=[forecast_row(DAY1)],
    )
    brief = grounding.build(result)
    answer = "On Thursday the high is 21C. September stays mild in London."
    assert grounding.violations(answer, brief) == []


def test_a_gap_the_model_already_stated_is_not_appended_twice():
    result = retrieval(
        "Which concerts are scheduled in London this week?",
        places=[place("Wigmore Hall", "concert_hall")],
    )
    brief = grounding.build(result)
    sentence = next(g.text for g in brief.gaps if g.subject == "events:concert")

    assert grounding.gap_block(brief, "")
    assert grounding.gap_block(brief, f"Sorry. {sentence}") == ""
    # A paraphrase is not the guaranteed wording, so the sentence still goes in.
    assert sentence in grounding.gap_block(brief, "There are no concerts, sorry.")


def test_the_footer_says_when_the_dates_were_assumed():
    """`dates.parse` documents that the assumed range is stated. It was not:
    str(window) drops the label, so 'sports events in London in October' was
    answered from the coming week with nothing saying so."""
    assumed = retrieval("Are there any sports events in London in October?")
    assert "assumed" in assumed.resolution.window.label
    assert "no dates in the question" in router.footer(assumed)

    explicit = retrieval("What is on in London tomorrow?")
    assert "no dates in the question" not in router.footer(explicit)


def test_a_background_answer_is_not_headed_by_a_forecast_window():
    result = retrieval("Tell me about the history of Lisbon", city=LISBON)
    result.facts = [{"title": "Lisbon", "summary": "Capital of Portugal."}]
    answer = grounding.render(grounding.build(result))
    assert answer.startswith("Lisbon:")
    assert "2026-09" not in answer.splitlines()[0]
    assert "Lisbon: Capital of Portugal." in answer


def test_place_names_containing_commas_are_still_readable():
    result = retrieval(
        "Which concerts are scheduled in London this week?",
        places=[
            place("St John's, Smith Square", "concert_hall"),
            place("Cadogan Hall", "concert_hall"),
        ],
    )
    line = next(
        row
        for row in grounding.render(grounding.build(result)).splitlines()
        if row.startswith("- concert hall:")
    )
    assert line == "- concert hall: St John's, Smith Square; Cadogan Hall"


# ------------------------------------------------------- clause scoping ----


def test_a_negation_at_the_end_does_not_cover_an_invitation_at_the_start():
    """Observed against the live model: 'You can also enjoy a game of football
    or a concert at the concert halls, though concerts are not on record for
    this period.' The trailing clause made the whole sentence read as negated,
    and the invitation went unchecked."""
    result = retrieval(
        "What activities can I do with my wife this week in London? "
        "We like concerts, shopping and fine dining.",
        forecast=[forecast_row(DAY1)],
        recommendations=[verdict_row(DAY1, "museums", "A museum day", 80)],
        places=[place("Wigmore Hall", "concert_hall")],
    )
    brief = grounding.build(result)
    broken = grounding.violations(
        "You can also enjoy a concert at the concert halls, though concerts are not "
        "on record for this period.",
        brief,
    )
    assert any("no stored event row" in v for v in broken)


def test_a_wholly_negative_sentence_is_still_allowed():
    result = retrieval("Which concerts are scheduled in London this week?")
    brief = grounding.build(result)
    assert (
        grounding.violations(
            "There is no concert on record this week, although the stored feed may "
            "simply not cover one.",
            brief,
        )
        == []
    )


def test_the_history_topic_is_retrieved_first_for_a_history_question(monkeypatch):
    """The Lisbon answer was handed three alphabetically-first museum
    descriptions and no row about the city, so the model wrote the history."""
    stored = [
        {"id": "wd:Q1", "title": "Atelier-Museu", "summary": "A museum.", "topic": "landmark"},
        {"id": "wd:Q2", "title": "Centro de Arte", "summary": "A gallery.", "topic": "landmark"},
        {"id": "wd:Q3", "title": "Convento", "summary": "A convent.", "topic": "landmark"},
        {"id": "wd:Q9", "title": "Lisbon", "summary": "Capital of Portugal.", "topic": "history"},
    ]

    def facts(_conn, _city, *, topic=None, limit=20):
        rows = [r for r in stored if topic is None or r["topic"] == topic]
        return rows[:limit]

    monkeypatch.setattr(router.queries, "cities", lambda _conn: [LISBON])
    monkeypatch.setattr(router.queries, "coverage", lambda _conn: COVERAGE)
    monkeypatch.setattr(router.queries, "facts", facts)
    monkeypatch.setattr(router.queries, "places", lambda *_a, **_k: [])
    monkeypatch.setattr(router.dates, "today_in", lambda _tz: DAY1)

    result = router.Router(object()).retrieve("Tell me about the history of Lisbon")

    assert [row["title"] for row in result.facts] == ["Lisbon", "Atelier-Museu", "Centro de Arte"]


def test_a_landmark_question_is_not_forced_onto_the_history_row(monkeypatch):
    stored = [
        {"id": "wd:Q1", "title": "Atelier-Museu", "summary": "A museum.", "topic": "landmark"},
        {"id": "wd:Q9", "title": "Lisbon", "summary": "Capital of Portugal.", "topic": "history"},
    ]

    def facts(_conn, _city, *, topic=None, limit=20):
        return [r for r in stored if topic is None or r["topic"] == topic][:limit]

    monkeypatch.setattr(router.queries, "cities", lambda _conn: [LISBON])
    monkeypatch.setattr(router.queries, "coverage", lambda _conn: COVERAGE)
    monkeypatch.setattr(router.queries, "facts", facts)
    monkeypatch.setattr(router.queries, "places", lambda *_a, **_k: [])
    monkeypatch.setattr(router.dates, "today_in", lambda _tz: DAY1)

    result = router.Router(object()).retrieve("Tell me about Lisbon")

    assert [row["title"] for row in result.facts] == ["Atelier-Museu", "Lisbon"]


# ------------------------------------------- absence: the record, not the city ----
#
# The system holds a hand-checked feed of a few venues per city over a few
# weeks. Its silence is a gap in that feed, and saying so is the whole of what
# it knows. The live model wrote "the stored data indicates that no scheduled
# events are taking place in Rome during this period", which names the store
# and still makes a claim about Rome.


@pytest.mark.parametrize(
    "answer",
    [
        "There are no concerts scheduled in London for 2026-09-24 to 2026-09-30.",
        "The stored data indicates that no scheduled events are taking place in London.",
        "No events are happening in London this week.",
        "Nothing is on in London during those dates.",
        "There are no concerts available in London this week.",
    ],
)
def test_an_absence_claimed_about_the_city_is_rejected(answer):
    result = retrieval("Which concerts are scheduled in London this week?")
    brief = grounding.build(result)
    broken = grounding.violations(answer, brief)
    assert any("rather than nothing being on record" in v for v in broken), broken


@pytest.mark.parametrize(
    "answer",
    [
        "No concert is on record in London for that week.",
        "I hold no scheduled concert for London in that week.",
        "I have no concert recorded for London between those dates.",
        "No sports event is on record for London, and none is listed in the feed.",
        "I do not have a concert on file for London this week.",
    ],
)
def test_an_absence_scoped_to_the_record_is_allowed(answer):
    """The other half, and the one that matters: a validator that rejected
    these would leave the agent unable to say the only true thing it knows."""
    result = retrieval("Which concerts are scheduled in London this week?")
    brief = grounding.build(result)
    assert grounding.violations(answer, brief) == []


def test_the_code_written_gap_sentences_pass_their_own_check():
    """`render` and `gap_block` are the fallback. If the absence check rejected
    their wording, a rejected model answer would be replaced by another
    rejected answer."""
    result = retrieval("Which concerts or sports events are on in London this week?")
    brief = grounding.build(result)
    assert grounding.violations(grounding.render(brief), brief) == []
    assert grounding.violations(grounding.gap_block(brief), brief) == []


def test_the_absence_check_leaves_non_event_prose_alone():
    """Narrow on purpose: it reads clauses that are already about scheduled
    things, and nothing else."""
    result = retrieval("What is the weather in London this week?", forecast=[forecast_row(DAY1)])
    brief = grounding.build(result)
    assert grounding.violations("There is no rain on 2026-09-24.", brief) == []


def test_an_event_on_record_is_still_stated_as_scheduled():
    """The check is about absence. A row in hand may be described as on."""
    result = retrieval(
        "Which concerts are on in London this week?",
        events=[event("lso:free-friday", "Free Friday Lunchtime Concert", "concert", DAY2)],
    )
    brief = grounding.build(result)
    answer = "Free Friday Lunchtime Concert is scheduled at The O2 arena on 2026-09-25."
    assert grounding.violations(answer, brief) == []


# --------------------------------------------- the F3 x `where` integration ----
#
# `where_answer` and the grounding layer landed on separate branches and met
# here. Both render in code; the risk is the order they run in and the wiring
# between them, which is what these pin.


def _where_result(question, activities, venues=None, unlocated=None, **rows):
    result = retrieval(question, **rows)
    result.resolution.asks_where = True
    result.resolution.activities = list(activities)
    result.venues = venues or {}
    result.unlocated_activities = list(unlocated or [])
    return result


def test_a_where_question_is_answered_in_code_before_the_model(monkeypatch):
    """The `where` route returns above `grounding.build`. If the merge had put
    it below, a location question would reach the model with an empty brief."""
    result = _where_result(
        "Where can I see a museum in London?",
        ["museums"],
        venues={"museums": [place("British Museum", "museum")]},
    )
    main = _stub_agent(monkeypatch, result)
    monkeypatch.setattr(
        main.client,
        "chat_json",
        lambda *_a, **_k: pytest.fail("the where route called the model"),
    )

    out = main.ask(main.AskIn(question=result.resolution.question))

    assert out["llm_called"] is False
    assert "British Museum" in out["answer"]


def test_an_unlocatable_activity_is_refused_rather_than_substituted(monkeypatch):
    """Tel Aviv has beaches on record and no surf spot. The honest answer names
    neither a beach nor a verdict."""
    result = _where_result(
        "Where can I surf in Tel Aviv?",
        ["surfing"],
        unlocated=["surfing"],
        places=[place("Gordon Beach", "beach")],
    )
    main = _stub_agent(monkeypatch, result)
    monkeypatch.setattr(
        main.client,
        "chat_json",
        lambda *_a, **_k: pytest.fail("the where route called the model"),
    )

    answer = main.ask(main.AskIn(question=result.resolution.question))["answer"]

    assert "verified" in answer
    assert "Gordon Beach" not in answer


def test_a_where_answer_carries_no_forecast_window_it_never_read():
    """`footer` stamps the weather only when the answer used it, and the
    assumed-window note follows the same rule: a location question named no
    dates and assumed none."""
    result = _where_result("Where can I surf in Tel Aviv?", ["surfing"], unlocated=["surfing"])
    stamp = router.footer(result)
    assert "forecast covers" not in stamp
    assert "no dates in the question" not in stamp


def test_an_event_question_still_states_the_window_it_assumed():
    """The other side of that gate: an undated event question does get told
    which week it was answered for."""
    result = retrieval(
        "Which concerts are on in London?",
        events=[event("lso:free-friday", "Free Friday Lunchtime Concert", "concert", DAY2)],
    )
    assert "no dates in the question" in router.footer(result)


def test_context_block_is_the_grounded_prompt_after_the_merge():
    """Main rebuilt `context_block` by hand while F3 reduced it to a wrapper.
    The wrapper is what survived, so the typed sections are what the model
    sees -- a place labelled as a building, not as something that is on."""
    result = retrieval(
        "What is on in London this week?",
        places=[place("Wigmore Hall", "concert_hall")],
    )
    block = router.context_block(result)
    assert block == grounding.prompt_block(grounding.build(result))
    assert "NOT scheduled events" in block


# ---------------------------------------------- quantities from nowhere ----


def test_a_figure_no_row_carries_is_rejected():
    """Asked about the history of Lisbon the model wrote "a history dating back
    over 2,000 years" from a summary giving a population and a river."""
    result = retrieval("Tell me about the history of Lisbon", city=LISBON)
    result.facts = [
        {
            "title": "Lisbon",
            "summary": "Lisbon is the capital of Portugal, on the River Tagus, "
            "with a population of 658,236 as of 2025.",
        }
    ]
    brief = grounding.build(result)

    answer = "Lisbon has a history dating back over 2,000 years."
    assert any("2000" in v for v in grounding.violations(answer, brief))
    assert any("40" in v for v in grounding.violations("Lisbon has a 40-year tradition.", brief))

    # The figures the summary does carry are fine, however they are spelled.
    assert grounding.violations("Lisbon had 658236 people in 2025.", brief) == []
    assert grounding.violations("Lisbon had 658,236 people in 2025.", brief) == []


def test_scores_and_temperatures_are_not_read_as_invented_figures():
    """A false positive here costs every answer its wording, so the numbers the
    system states about itself have to pass."""
    result = retrieval(
        "What can I do in London this week?",
        forecast=[forecast_row(DAY1, high=22.0, low=14.0)],
        recommendations=[verdict_row(DAY1, "museums", "A museum day", 80)],
    )
    brief = grounding.build(result)
    answer = "On 2026-09-24 the high is 22C and the low is 14C, and a museum day " "scores 80/100."
    assert grounding.violations(answer, brief) == []


def test_a_multi_interest_question_spreads_its_place_budget(monkeypatch):
    """E2 names three interests. London holds more than eighteen concert halls,
    and `ORDER BY category, name LIMIT 18` gave the traveller all concert halls
    and no restaurant. The cap is per category now."""
    seen = {}

    def places(_conn, _city, *, categories=None, limit=50, per_category=None):
        seen.update(categories=categories, limit=limit, per_category=per_category)
        return []

    monkeypatch.setattr(router.queries, "cities", lambda _conn: [LONDON])
    monkeypatch.setattr(router.queries, "coverage", lambda _conn: COVERAGE)
    monkeypatch.setattr(router.queries, "places", places)
    monkeypatch.setattr(router.queries, "forecast", lambda *_a, **_k: [])
    monkeypatch.setattr(router.queries, "recommendations", lambda *_a, **_k: [])
    monkeypatch.setattr(router.queries, "events", lambda *_a, **_k: [])
    # Asked only when the retrieval above comes back empty, so a gap can
    # say whether the feed went stale or was never there.
    monkeypatch.setattr(
        router.queries, "expired_events", lambda *_a, **_k: {"expired": 0, "last_checked": None}
    )
    monkeypatch.setattr(router.queries, "facts", lambda *_a, **_k: [])
    monkeypatch.setattr(router.dates, "today_in", lambda _tz: DAY1)

    router.Router(object()).retrieve(
        "What activities can I do with my wife this week in London? "
        "We like concerts, shopping and fine dining."
    )

    assert len(seen["categories"]) > 1, seen["categories"]
    assert seen["per_category"] == 6
    assert seen["limit"] == 18


def test_an_open_places_question_keeps_the_flat_limit(monkeypatch):
    """No category was named, so there is nothing to spread a budget across
    and the extra SQL is not paid for."""
    seen = {}

    def places(_conn, _city, *, categories=None, limit=50, per_category=None):
        seen.update(categories=categories, per_category=per_category)
        return []

    monkeypatch.setattr(router.queries, "cities", lambda _conn: [LONDON])
    monkeypatch.setattr(router.queries, "coverage", lambda _conn: COVERAGE)
    monkeypatch.setattr(router.queries, "places", places)
    monkeypatch.setattr(router.queries, "forecast", lambda *_a, **_k: [])
    monkeypatch.setattr(router.queries, "recommendations", lambda *_a, **_k: [])
    monkeypatch.setattr(router.queries, "events", lambda *_a, **_k: [])
    # Asked only when the retrieval above comes back empty, so a gap can
    # say whether the feed went stale or was never there.
    monkeypatch.setattr(
        router.queries, "expired_events", lambda *_a, **_k: {"expired": 0, "last_checked": None}
    )
    monkeypatch.setattr(router.queries, "facts", lambda *_a, **_k: [])
    monkeypatch.setattr(router.dates, "today_in", lambda _tz: DAY1)

    router.Router(object()).retrieve("What is there to visit in London?")

    assert seen["categories"] is None, "no interest was named, so nothing is filtered"
    assert seen["per_category"] is None


# ------------------------------- an unscored activity gets one sentence ----


@pytest.mark.parametrize(
    "answer",
    [
        # The live model's wording, which the verdict-word list did not hold.
        "The weather forecast for 2026-09-25 shows a low of 12C and rain, "
        "which is not favorable for surfing.",
        "There is no surfing score on record. The wind is light, so surfing is fine.",
        "Surfing is not on record, but the mild temperatures would suit surfing.",
        "No surfing score is stored; conditions for surfing look dry.",
    ],
)
def test_reasoning_from_the_weather_about_an_unscored_activity_is_rejected(answer):
    result = retrieval(
        "Is it a good day for surfing in London tomorrow?",
        forecast=[forecast_row(DAY2, high=19.0, low=12.0)],
    )
    result.resolution.activities = ["surfing"]
    result.unscored_activities = ["surfing"]
    brief = grounding.build(result)
    assert any("surfing" in v for v in grounding.violations(answer, brief))


def test_stating_the_absence_and_the_forecast_separately_is_allowed():
    """The traveller still gets the forecast. It just does not get attached to
    an activity the system never scored."""
    result = retrieval(
        "Is it a good day for surfing in London tomorrow?",
        forecast=[forecast_row(DAY2, high=19.0, low=12.0)],
    )
    result.resolution.activities = ["surfing"]
    result.unscored_activities = ["surfing"]
    brief = grounding.build(result)
    answer = (
        "There is no surfing score on record for London. On 2026-09-25 the "
        "stored forecast is a high of 19C and a low of 12C."
    )
    assert grounding.violations(answer, brief) == []
