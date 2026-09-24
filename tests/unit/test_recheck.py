"""The operator-assisted event re-check (services/tools/event_recheck.py).

F9 left one thing open: *"There is no automated re-check. Extending a listing's
life means opening its page and patching `checked_at`, one row at a time."*

The tempting close is a job that re-fetches every `source_url` and bumps
`checked_at` on HTTP 200. These tests exist mostly to make that impossible.

The fixtures below are cut down from pages actually fetched on 2026-09-25 from
the ten venue sites this feed is built on, and they encode what was found there:

  * harpa.is publishes a full `schema.org/Event` with `startDate` and
    `eventStatus`;
  * theo2.co.uk publishes `Event`/`SportsEvent`/`MusicEvent` with `startDate`
    but no `eventStatus`, and one of its pages embeds a raw tab inside a JSON
    string, which is invalid JSON;
  * the other eight publish no event structured data at all;
  * auditorium.com serves a *scheduled* concert's page containing the words
    "EVENTO ANNULLATO" -- about a different show, in the sidebar;
  * coliseulisboa.com says "esgotado" (sold out) on a show that is going ahead;
  * one stored listing's page is now a 404.

Each of those is a way to get this wrong, and each has a test.
"""

from __future__ import annotations

import datetime
import json

import pytest

from services.tools import event_recheck as rc

# --------------------------------------------------------------- fixtures ----

# harpa.is: the best case. Everything needed is published.
HARPA = """
<html><head><title>Deep Purple | Harpa</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Event",
 "name":"Deep Purple - heidurstonleikar",
 "startDate":"2026-09-26T20:00:00+00:00",
 "endDate":"2026-09-26T22:00:00.000Z",
 "eventStatus":"https://schema.org/EventScheduled",
 "location":{"@type":"Place","name":"Harpa"}}
</script></head><body>Uppselt / sold out</body></html>
"""

# theo2.co.uk: a SportsEvent whose JSON-LD contains a literal tab inside a
# string, exactly as the live page does. json.loads() rejects it; json.loads(
# ..., strict=False) accepts it. This one page is the difference between 7 and
# 8 verifiable rows on that site.
THEO2_TAB = (
    "<html><head><title>Laver Cup 2026 | The O2</title>"
    '<script type="application/ld+json">'
    '{"@context":"http://schema.org","@type":"SportsEvent","name":"Laver Cup 2026",'
    '"startDate":"2026-09-25T11:30:00+01:00","endDate":"2026-09-27T12:30:00+01:00",'
    '"location":{"@type":"Place","name":"The O2 arena "},'
    '"description":"Team Europe\tCarlos"}'
    "</script></head><body></body></html>"
)

# auditorium.com: the trap. This page is for a concert that IS going ahead. The
# word ANNULLATO belongs to a different show in the related-events rail.
AUDITORIUM_SIDEBAR = """
<html><head><title>Battisti in Classica | Auditorium</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"WebPage","name":"Battisti in Classica"}
</script></head>
<body><h1>Roberto Fabbri presenta: Battisti in...Classica!</h1>
<aside>EVENTI CORRELATI <span>Annullato</span> EVENTO ANNULLATO - Ryan Adams</aside>
</body></html>
"""

# gulbenkian.pt: the worst trap of the ten, and the one that caught a reviewer.
# Every session renders all three states into the HTML -- Cancelado, Esgotado
# and the available one -- and hides the two that do not apply with Alpine's
# `x-cloak`. This page is a SCHEDULED, AVAILABLE concert and it still contains
# the word "Cancelado" twice. A summariser asked what this page says answers
# "cancelled"; a grep answers "cancelled"; the JSON-LD says EventScheduled.
#
# Its dates are also written with a space instead of a `T` and no offset, which
# is not strict ISO-8601 and is exactly the sort of thing a date parser is
# quietly wrong about.
GULBENKIAN_XCLOAK = """
<html><head><title>Sinfonia n.o 2 de Brahms | Gulbenkian Musica</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"MusicEvent","name":"Sinfonia n.o 2 de Brahms",
 "startDate":"2026-10-15 20:00:00","endDate":"2026-10-15 21:00:00",
 "eventStatus":"https://schema.org/EventScheduled",
 "location":{"@type":"Place","name":"Grande Auditorio"}}
</script></head>
<body><ul class="fcg-event-schedule">
 <li><time x-bind="sessionDay">15 out 2026</time>
     <span x-cloak x-show="isCancelled()"><span>20:00</span> /
       <span class="tw-text-red-500">Cancelado</span></span>
     <span x-cloak x-show="isSoldOut()"><span>20:00</span> /
       <span class="tw-text-red-500">Esgotado</span></span>
     <span x-show="!isSoldOut()"><span>quinta, 20:00</span></span></li>
 <li><time x-bind="sessionDay">16 out 2026</time>
     <span x-cloak x-show="isCancelled()"><span>19:00</span> /
       <span class="tw-text-red-500">Cancelado</span></span>
     <span x-show="isSoldOut()"><span>19:00</span> /
       <span class="tw-text-red-500">Esgotado</span></span>
     <span x-cloak x-show="!isSoldOut()"><span>sexta, 19:00</span></span></li>
</ul></body></html>
"""

# coliseulisboa.com: sold out is not cancelled, and the date is prose.
COLISEU = """
<html><head><title>RADIO MACAU - Coliseu dos Recreios</title></head>
<body>O evento encontra-se esgotado. 30 setembro, 2026 a 2 outubro, 2026 as 21:30</body>
</html>
"""

SUZANNE_404 = (
    "<html><head><title>Page not found - Suzanne Dellal</title></head><body></body></html>"
)


def stored(**overrides):
    row = {
        "id": "harpa:deep-purple-2026-09-26",
        "city_id": "reykjavik",
        "title": "Deep Purple - heidurstonleikar",
        "venue": "Harpa",
        "starts_at": "2026-09-26T20:00:00+00:00",
        "ends_at": "2026-09-26T22:00:00+00:00",
        "checked_at": "2026-09-24T18:00:00+00:00",
        "valid_until": "2026-10-15T18:00:00+00:00",
        "source_url": "https://www.harpa.is/en/events/21533",
        "is_current": True,
        "is_sample": False,
    }
    row.update(overrides)
    return row


def result(html=None, *, status=200, error=None, content_type="text/html", url="https://x/y"):
    return rc.FetchResult(
        url=url,
        fetched_at="2026-09-25T09:00:00+00:00",
        ok=error is None and status < 400,
        status=None if error else status,
        final_url=None if error else url,
        content_type=None if error else content_type,
        bytes=None if error else len(html or ""),
        sha256=None if error else "0" * 64,
        error=error,
        html=html if (error is None and status < 400) else None,
    )


# -------------------------------------------------------------- extraction ----


def test_extracts_a_full_event_node():
    observed = rc.extract_observation(HARPA)
    assert observed is not None
    assert observed.schema_type == "Event"
    assert observed.starts_at == "2026-09-26T20:00:00+00:00"
    assert observed.status == "https://schema.org/EventScheduled"
    assert observed.venue == "Harpa"


def test_extracts_an_event_subtype():
    """SportsEvent, MusicEvent and TheaterEvent are all Events.

    All three appear in this feed's eight theo2.co.uk rows. Matching on the
    `Event` suffix rather than on a hand-kept list is why none of them is
    silently skipped.
    """
    for type_name in ("SportsEvent", "MusicEvent", "TheaterEvent", "Event"):
        html = (
            '<script type="application/ld+json">'
            f'{{"@type":"{type_name}","name":"x","startDate":"2026-10-02T18:30:00+01:00"}}'
            "</script>"
        )
        assert rc.extract_observation(html).schema_type == type_name


def test_malformed_json_ld_is_still_read():
    """The live theo2.co.uk page for the Laver Cup is invalid JSON.

    A strict parse drops the only node on that page worth reading, and the row
    silently becomes unverifiable. Leniency about control characters costs
    nothing -- the structure still has to parse -- and it is deliberate.
    """
    [block] = rc._LD_JSON.findall(THEO2_TAB)
    with pytest.raises(json.JSONDecodeError, match="[Ii]nvalid control character"):
        json.loads(block)

    observed = rc.extract_observation(THEO2_TAB)
    assert observed is not None
    assert observed.schema_type == "SportsEvent"
    assert observed.starts_at == "2026-09-25T11:30:00+01:00"


def test_event_nested_in_a_graph_is_found():
    html = (
        '<script type="application/ld+json">'
        '{"@graph":[{"@type":"WebSite"},'
        '{"@type":"Event","name":"n","startDate":"2026-10-01T19:30:00+00:00"}]}'
        "</script>"
    )
    assert rc.extract_observation(html).starts_at == "2026-10-01T19:30:00+00:00"


def test_a_list_valued_type_is_matched():
    html = (
        '<script type="application/ld+json">'
        '{"@type":["MusicEvent","Thing"],"name":"n","startDate":"2026-10-01T19:30:00+00:00"}'
        "</script>"
    )
    assert rc.extract_observation(html) is not None


def test_a_page_without_event_data_yields_nothing():
    """Eight of the ten sites land here on every row, and that is the answer.

    Returning None is what forces those rows to a human instead of renewing
    them because the venue's web server answered.
    """
    assert rc.extract_observation(AUDITORIUM_SIDEBAR) is None
    assert rc.extract_observation(COLISEU) is None
    assert rc.extract_observation("<html><body>nothing here</body></html>") is None


def test_an_event_node_without_a_date_is_not_an_observation():
    """Site-wide navigation boilerplate is sometimes typed as an Event."""
    html = '<script type="application/ld+json">{"@type":"Event","name":"Whats On"}</script>'
    assert rc.extract_observation(html) is None


# ------------------------------------------------------------ cancellation ----


@pytest.mark.parametrize(
    "status",
    [
        "https://schema.org/EventCancelled",
        "http://schema.org/EventPostponed",
        "EventRescheduled",
        "https://schema.org/EventCancelled/",
    ],
)
def test_structured_cancellation_is_recognised(status):
    assert rc.status_is_cancelled(status) is True


@pytest.mark.parametrize(
    "status", [None, "", "https://schema.org/EventScheduled", "EventMovedOnline"]
)
def test_anything_else_is_not_a_cancellation(status):
    assert rc.status_is_cancelled(status) is False


def test_the_word_annullato_in_a_sidebar_does_not_cancel_a_live_event():
    """The single most important test in this file.

    www.auditorium.com serves this concert's page with "EVENTO ANNULLATO - Ryan
    Adams" in its related-events rail. A text search for a cancellation word
    would cancel a show that is going ahead. Only `eventStatus` counts, so this
    page is reported as unverifiable -- honest -- and never as cancelled.
    """
    verdict, observed, _ = rc.classify(stored(), result(AUDITORIUM_SIDEBAR))
    assert verdict == rc.UNVERIFIABLE
    assert observed is None


def test_hidden_state_variants_do_not_cancel_an_available_concert():
    """gulbenkian.pt ships every session's three states and hides two of them.

    This fixture is an available concert whose HTML contains "Cancelado" twice
    and "Esgotado" once, inside `x-cloak` spans the browser never shows. A
    reviewer's summariser read this page and reported both performances
    cancelled; the page's own `eventStatus` says EventScheduled, and that is the
    only thing this tool reads.
    """
    assert GULBENKIAN_XCLOAK.count("Cancelado") == 2
    assert "Esgotado" in GULBENKIAN_XCLOAK

    row = stored(
        title="Sinfonia n.o 2 de Brahms",
        venue="Grande Auditorio",
        starts_at="2026-10-15T20:00:00+01:00",
        ends_at="2026-10-15T21:00:00+01:00",
    )
    verdict, observed, diffs = rc.classify(row, result(GULBENKIAN_XCLOAK))
    assert verdict == rc.CONFIRMED, (verdict, diffs)
    assert observed.status.endswith("EventScheduled")


def test_a_naive_space_separated_date_is_still_read():
    """gulbenkian.pt writes "2026-10-15 20:00:00" -- no `T`, no offset.

    A venue page's local time is the venue's local time, so the day it names is
    the day to compare. The point of asserting it is that a parser which
    quietly returned None here would turn every one of that venue's rows into
    an `unverifiable` and nobody would notice: the tool would still look like it
    was working.
    """
    assert rc.local_day("2026-10-15 20:00:00") == "2026-10-15"
    assert rc.extract_observation(GULBENKIAN_XCLOAK).starts_at == "2026-10-15 20:00:00"


@pytest.mark.parametrize(
    "prose",
    [
        "THIS EVENT HAS BEEN CANCELLED",
        "Evento annullato",
        "O concerto foi cancelado",
        "Sold out - no tickets remain",
    ],
)
def test_a_cancellation_stated_only_in_prose_is_unverifiable_not_cancelled(prose):
    """The rule, from the other side.

    `cancelled` is reachable from exactly one place -- a `schema.org`
    `eventStatus` -- so a page that announces a cancellation only in words is
    reported as something a human must read. That is the safe direction: it
    sends somebody to the page instead of guessing either way.
    """
    verdict, _, _ = rc.classify(stored(), result(f"<html><body>{prose}</body></html>"))
    assert verdict == rc.UNVERIFIABLE


def test_page_title_is_evidence_and_is_never_compared():
    """The only prose this tool reads, and it decides nothing.

    A title carries the venue's name and the site's tagline, so a mismatch
    would mean nothing. It is in the ledger so a human can see which page was
    fetched -- and a page whose title screams CANCELLED is still judged on its
    structured data alone.
    """
    html = HARPA.replace("Deep Purple | Harpa", "CANCELLED - Deep Purple | Harpa")
    line = rc.evidence_line(stored(), result(html))
    assert line["page_title"] == "CANCELLED - Deep Purple | Harpa"
    assert line["verdict"] == rc.CONFIRMED
    assert line["differences"] == []


def test_sold_out_is_not_cancelled():
    """coliseulisboa.com says "esgotado" on a show that is still scheduled."""
    verdict, _, _ = rc.classify(stored(), result(COLISEU))
    assert verdict == rc.UNVERIFIABLE

    verdict, _, _ = rc.classify(stored(), result(HARPA))
    assert verdict == rc.CONFIRMED  # and its body says "sold out" too


# ---------------------------------------------------------------- matching ----


def test_local_day_uses_the_offset_it_was_given():
    assert rc.local_day("2026-09-26T20:00:00+00:00") == "2026-09-26"
    assert rc.local_day("2026-09-26T22:00:00.000Z") == "2026-09-26"
    assert rc.local_day("2026-09-25T00:00:00+01:00") == "2026-09-25"
    assert rc.local_day("not a date") is None
    assert rc.local_day(None) is None


def test_the_stored_day_comes_from_sql_not_from_the_utc_instant():
    """The regression that eight false `changed` verdicts came from.

    `/events` serialises `starts_at` to UTC, so a listing recorded at local
    midnight -- which every theo2 row is -- comes back as 23:00 the previous
    day. Taking the date off that instant is a day early, and the first live
    run of this tool reported every one of the eight theo2 rows as `changed`
    with `starts_on` exactly one day out. The unit tests missed it because they
    passed timestamps that still carried the venue's offset.

    `queries.events` already derives the correct local day with `AT TIME ZONE`
    and returns it as `starts_on`, so the fix is to believe that column.
    """
    # What the API actually returns for theo2:karan-aujla-2026: local midnight
    # on 4 October, serialised to UTC, plus the day SQL derived.
    row = stored(starts_at="2026-10-03T23:00:00Z", starts_on="2026-10-04", ends_at=None)
    assert rc.stored_local_day(row, "starts_on", "starts_at") == "2026-10-04"

    # Without the derived column the instant is all there is, and it is wrong
    # by a day -- which is why the column is preferred rather than averaged in.
    bare = stored(starts_at="2026-10-03T23:00:00Z", ends_at=None)
    bare.pop("starts_on", None)
    assert rc.stored_local_day(bare, "starts_on", "starts_at") == "2026-10-03"

    # A date object from the driver rather than a string is still a date.
    assert rc.stored_local_day({"starts_on": datetime.date(2026, 10, 4)}, "starts_on", "x") == (
        "2026-10-04"
    )


def test_a_whole_day_row_matches_the_pages_start_time():
    """`theo2:laver-cup-2026` is stored as whole days; the page gives 11:30.

    Same event, same day. Comparing instants would report a change on every
    multi-day listing in the feed, and an operator who is shown three false
    changes stops reading the real one.
    """
    row = stored(starts_at="2026-09-25T00:00:00+01:00", ends_at="2026-09-27T23:59:00+01:00")
    row["title"] = "Laver Cup 2026"
    row["venue"] = "The O2 arena"
    verdict, _, diffs = rc.classify(row, result(THEO2_TAB))
    assert diffs == []
    assert verdict == rc.CONFIRMED


def test_cosmetic_title_differences_are_not_changes():
    assert rc.normalise_title("Schumann & Beethoven") == rc.normalise_title(
        "Schumann  &  beethoven"
    )
    assert rc.normalise_title("Yannets Levi’s Adventures") == rc.normalise_title(
        "Yannets Levi's Adventures"
    )
    assert rc.normalise_title("Deep Purple – tonleikar") == rc.normalise_title(
        "Deep Purple - tonleikar"
    )


def test_a_shorter_title_is_reported_not_absorbed():
    """theo2.co.uk names the artist; the stored row names the tour.

    "Niall Horan" and "Niall Horan: DINNER PARTY Live on Tour" are probably the
    same event. *Probably* is a judgement, and judgements belong to the
    operator, so this is a `changed` that a human resolves -- not a containment
    rule that quietly renews it.
    """
    row = stored(title="Niall Horan: DINNER PARTY Live on Tour")
    html = (
        '<script type="application/ld+json">'
        '{"@type":"MusicEvent","name":"Niall Horan",'
        '"startDate":"2026-09-26T18:30:00+00:00"}</script>'
    )
    verdict, _, diffs = rc.classify(row, result(html))
    assert verdict == rc.CHANGED
    assert [d.field for d in diffs] == ["title"]


def test_a_hall_inside_a_building_is_not_a_different_venue():
    """The page names the building, the row names the hall, or the reverse."""
    observed = rc.Observation(
        title="Deep Purple - heidurstonleikar",
        starts_at="2026-09-26T20:00:00+00:00",
        venue="Harpa",
    )
    row = stored(venue="Eldborg, Harpa Concert Hall")
    assert [d.field for d in rc.compare(row, observed)] == []


def test_a_genuinely_different_venue_is_reported():
    observed = rc.Observation(
        title="Deep Purple - heidurstonleikar",
        starts_at="2026-09-26T20:00:00+00:00",
        venue="The O2 arena",
    )
    assert [d.field for d in rc.compare(stored(), observed)] == ["venue"]


def test_a_field_the_page_omits_is_not_a_difference():
    """Absence is not disagreement, and reporting it as one buries the real ones."""
    observed = rc.Observation(starts_at="2026-09-26T20:00:00+00:00")
    assert rc.compare(stored(), observed) == []


def test_a_moved_date_is_reported():
    observed = rc.Observation(
        title="Deep Purple - heidurstonleikar",
        starts_at="2026-10-30T20:00:00+00:00",
        venue="Harpa",
    )
    diffs = rc.compare(stored(), observed)
    assert [d.field for d in diffs] == ["starts_on"]
    assert diffs[0].stored == "2026-09-26"
    assert diffs[0].observed == "2026-10-30"


# --------------------------------------------------------------- verdicts ----


@pytest.mark.parametrize("status", [404, 410])
def test_a_withdrawn_page_is_gone(status):
    """One stored row -- suzannedellal:altar-for-the-beast -- is a live example."""
    verdict, _, _ = rc.classify(stored(), result(SUZANNE_404, status=status))
    assert verdict == rc.GONE


@pytest.mark.parametrize("status", [403, 429, 500, 502, 503])
def test_bot_blocking_and_server_errors_are_unreachable_not_verified(status):
    """A 403 is the shape a bot block takes. It is not evidence of anything."""
    verdict, _, _ = rc.classify(stored(), result(HARPA, status=status))
    assert verdict == rc.UNREACHABLE


def test_a_transport_failure_is_unreachable():
    verdict, _, _ = rc.classify(stored(), result(error="ConnectTimeout: timed out"))
    assert verdict == rc.UNREACHABLE


def test_a_non_html_response_is_unreachable():
    """A captive portal or a CDN error page that serves JSON is not a listing."""
    verdict, _, _ = rc.classify(stored(), result("{}", content_type="application/json"))
    assert verdict == rc.UNREACHABLE


def test_a_cancelled_event_is_reported_as_cancelled():
    html = HARPA.replace("EventScheduled", "EventCancelled")
    verdict, observed, _ = rc.classify(stored(), result(html))
    assert verdict == rc.CANCELLED
    assert observed.status.endswith("EventCancelled")


def test_only_confirmed_is_renewable():
    """The rule the whole design rests on, asserted directly."""
    assert {rc.CONFIRMED} == rc.RENEWABLE
    for verdict in rc.VERDICTS:
        assert (verdict in rc.RENEWABLE) == (verdict == rc.CONFIRMED)


@pytest.mark.parametrize(
    ("html", "status", "error", "expected"),
    [
        (HARPA, 200, None, rc.CONFIRMED),
        (AUDITORIUM_SIDEBAR, 200, None, rc.UNVERIFIABLE),
        (COLISEU, 200, None, rc.UNVERIFIABLE),
        (SUZANNE_404, 404, None, rc.GONE),
        (None, None, "DNSError", rc.UNREACHABLE),
    ],
)
def test_renewable_is_set_only_where_a_page_verified_the_row(html, status, error, expected):
    line = rc.evidence_line(stored(), result(html, status=status or 200, error=error))
    assert line["verdict"] == expected
    assert line["renewable"] is (expected == rc.CONFIRMED)


# ----------------------------------------------------------------- ledger ----


def test_the_ledger_records_what_was_observed_and_when():
    """Requirement: a re-check records evidence, not just a new timestamp."""
    line = rc.evidence_line(stored(), result(HARPA))
    assert line["kind"] == "observation"
    assert line["event_id"] == "harpa:deep-purple-2026-09-26"
    assert line["fetched_at"] == "2026-09-25T09:00:00+00:00"
    assert line["http_status"] == 200
    assert line["sha256"] == "0" * 64
    assert line["page_title"] == "Deep Purple | Harpa"
    assert line["observed"]["starts_at"] == "2026-09-26T20:00:00+00:00"
    # The row as it stood before anything was decided, so the ledger is
    # readable on its own long after the row has moved on.
    assert line["stored"]["checked_at"] == "2026-09-24T18:00:00+00:00"
    assert line["stored"]["valid_until"] == "2026-10-15T18:00:00+00:00"


def test_an_unreadable_page_still_produces_evidence():
    line = rc.evidence_line(stored(), result(error="ReadTimeout: 20s"))
    assert line["verdict"] == rc.UNREACHABLE
    assert line["error"] == "ReadTimeout: 20s"
    assert line["renewable"] is False


# ------------------------------------------------------------------ probe ----


def test_probe_refuses_to_run_without_the_egress_window(monkeypatch, capsys):
    """The air-gap guard.

    `probe` is the only code in this repository that fetches from the public
    internet. It runs from scripts/event-recheck.sh, which sets this variable
    only while it is holding the egress window open.
    """
    monkeypatch.delenv("AOW_RECHECK_ALLOW_EGRESS", raising=False)
    called = []
    monkeypatch.setattr(rc, "fetch", lambda *a, **k: called.append(a))
    assert rc.main(["probe"]) == 2
    assert called == []
    assert "AOW_RECHECK_ALLOW_EGRESS" in capsys.readouterr().err


def test_probe_never_fetches_a_generated_sample(monkeypatch, capsys):
    """A sample cites a venue reference, not a listing page.

    There is nothing to re-check on one, and pretending otherwise would put a
    verdict on a row whose URL was never a listing in the first place.
    """
    monkeypatch.setenv("AOW_RECHECK_ALLOW_EGRESS", "1")
    monkeypatch.setattr(
        rc,
        "_api_get",
        lambda base, path, **kw: [
            stored(id="sample:x", is_sample=True, source_url="https://example.org/venue")
        ],
    )
    monkeypatch.setattr(rc, "fetch", lambda *a, **k: pytest.fail("a sample was fetched"))
    assert rc.main(["probe"]) == 1
    assert "no verified event rows" in capsys.readouterr().err


def test_probe_fetches_a_shared_source_url_once(monkeypatch, capsys):
    """Three of the 39 rows are performances on one show page."""
    monkeypatch.setenv("AOW_RECHECK_ALLOW_EGRESS", "1")
    rows = [stored(id="a"), stored(id="b"), stored(id="c", source_url="https://other/")]
    monkeypatch.setattr(rc, "_api_get", lambda base, path, **kw: rows)
    fetched: list[str] = []

    def one(url, **kw):
        fetched.append(url)
        return result(HARPA, url=url)

    monkeypatch.setattr(rc, "fetch", one)
    assert rc.main(["probe"]) == 0
    assert fetched == ["https://www.harpa.is/en/events/21533", "https://other/"]
    assert len(capsys.readouterr().out.strip().splitlines()) == 3


# ------------------------------------------------------------------ apply ----


class FakeApi:
    """Records the PATCHes `apply` would send, and never touches a network."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, base, event_id, fields, timeout=20.0):
        self.calls.append((event_id, fields))
        return {"accepted": True, "message_id": f"msg-{len(self.calls)}"}


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "recheck.jsonl"
    lines = [
        rc.evidence_line(stored(id="ok"), result(HARPA)),
        rc.evidence_line(stored(id="dark"), result(error="ConnectTimeout")),
        rc.evidence_line(stored(id="prose"), result(COLISEU)),
        rc.evidence_line(stored(id="dead"), result(SUZANNE_404, status=404)),
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def test_apply_is_a_dry_run_without_yes(ledger, monkeypatch, capsys):
    api = FakeApi()
    monkeypatch.setattr(rc, "_api_patch", api)
    assert rc.main(["apply", "--evidence", str(ledger), "--all-confirmed"]) == 0
    assert api.calls == []
    assert "dry run" in capsys.readouterr().out


def test_all_confirmed_renews_only_what_a_page_verified(ledger, monkeypatch):
    """The headline behaviour: 1 of these 4 rows is renewable, and only it moves."""
    api = FakeApi()
    monkeypatch.setattr(rc, "_api_patch", api)
    assert rc.main(["apply", "--evidence", str(ledger), "--all-confirmed", "--yes"]) == 0
    assert [event_id for event_id, _ in api.calls] == ["ok"]


@pytest.mark.parametrize("event_id", ["dark", "prose", "dead"])
def test_apply_refuses_an_unverified_row_without_a_note(event_id, ledger, monkeypatch, capsys):
    """A fetch that failed, timed out or was ambiguous is not a re-check.

    This is requirement 3 of the brief, asserted as a refusal rather than as a
    comment: no `checked_at` moves on the strength of a page nobody verified
    and nobody read.
    """
    api = FakeApi()
    monkeypatch.setattr(rc, "_api_patch", api)
    assert rc.main(["apply", "--evidence", str(ledger), "--id", event_id, "--yes"]) == 1
    assert api.calls == []
    assert "refusing" in capsys.readouterr().err


def test_a_human_who_read_the_page_may_renew_it_with_a_note(ledger, monkeypatch):
    """The other half: automation cannot verify it, but a person can.

    The note is required, and it is filed in the ledger beside what the fetch
    actually saw -- so "renewed because a human looked" is distinguishable from
    "renewed because a parser agreed" for ever after.
    """
    api = FakeApi()
    monkeypatch.setattr(rc, "_api_patch", api)
    code = rc.main(
        [
            "apply",
            "--evidence",
            str(ledger),
            "--id",
            "prose",
            "--note",
            "opened the page, 30 setembro 2026 still listed",
            "--yes",
        ]
    )
    assert code == 0
    assert [event_id for event_id, _ in api.calls] == ["prose"]
    decisions = [r for r in rc.read_evidence(str(ledger)) if r["kind"] == "decision"]
    assert len(decisions) == 1
    assert decisions[0]["verdict"] == rc.UNVERIFIABLE
    assert decisions[0]["note"].startswith("opened the page")
    assert decisions[0]["message_id"] == "msg-1"


def test_the_patched_check_date_is_when_the_page_was_read(ledger, monkeypatch):
    """Never `now()`.

    `valid_until` is derived from `checked_at`, so a check date later than the
    observation behind it would buy the row time nothing paid for.
    """
    api = FakeApi()
    monkeypatch.setattr(rc, "_api_patch", api)
    rc.main(["apply", "--evidence", str(ledger), "--all-confirmed", "--yes"])
    (_, fields) = api.calls[0]
    assert fields == {"checked_at": "2026-09-25T09:00:00+00:00"}


def test_apply_patches_only_checked_at(ledger, monkeypatch):
    """`valid_until` stays underivable by hand.

    The consumer recomputes it from `checked_at` and rejects it as poison when
    it is patched directly (F9). Sending only `checked_at` keeps that intact.
    """
    api = FakeApi()
    monkeypatch.setattr(rc, "_api_patch", api)
    rc.main(["apply", "--evidence", str(ledger), "--all-confirmed", "--yes"])
    for _, fields in api.calls:
        assert set(fields) == {"checked_at"}


def test_a_failed_patch_is_recorded_and_does_not_stop_the_batch(ledger, monkeypatch):
    def boom(base, event_id, fields, timeout=20.0):
        raise OSError("api unreachable")

    monkeypatch.setattr(rc, "_api_patch", boom)
    assert rc.main(["apply", "--evidence", str(ledger), "--all-confirmed", "--yes"]) == 1
    decisions = [r for r in rc.read_evidence(str(ledger)) if r["kind"] == "decision"]
    assert decisions[0]["action"] == "patch-failed"
    assert "api unreachable" in decisions[0]["error"]


def test_apply_selects_nothing_by_default(ledger, monkeypatch, capsys):
    monkeypatch.setattr(rc, "_api_patch", FakeApi())
    assert rc.main(["apply", "--evidence", str(ledger), "--yes"]) == 1
    assert "nothing selected" in capsys.readouterr().err


# ----------------------------------------------------------------- review ----


def test_review_reports_the_split_rather_than_a_total(ledger, capsys):
    """ "39 re-checked" would be a lie by omission. The split is the finding."""
    assert rc.main(["review", "--evidence", str(ledger)]) == 0
    out = capsys.readouterr().out
    assert "1 of 4 can be renewed from the page itself; 3 need a human" in out
    # Every row that needs eyes is named, with its URL, so the operator has
    # somewhere to go.
    for event_id in ("dark", "prose", "dead"):
        assert event_id in out


def test_review_makes_no_network_calls(ledger, monkeypatch):
    monkeypatch.setattr(rc, "fetch", lambda *a, **k: pytest.fail("review fetched a page"))
    monkeypatch.setattr(rc, "_api_call", lambda *a, **k: pytest.fail("review called the API"))
    assert rc.main(["review", "--evidence", str(ledger)]) == 0


def test_summary_counts_every_verdict():
    rows = [
        rc.evidence_line(stored(), result(HARPA)),
        rc.evidence_line(stored(), result(COLISEU)),
        rc.evidence_line(stored(), result(SUZANNE_404, status=404)),
    ]
    assert rc.summarise(rows) == {
        rc.GONE: 1,
        rc.CANCELLED: 0,
        rc.CHANGED: 0,
        rc.UNREACHABLE: 0,
        rc.UNVERIFIABLE: 1,
        rc.CONFIRMED: 1,
    }


# -------------------------------------------------------------- structure ----


def test_the_only_outbound_fetch_lives_in_probe():
    """Structural, and worth asserting.

    The air-gap claim is that exactly one subcommand can reach the public
    internet. That is easy to break by accident later -- a "just re-fetch it
    here" in `apply` would do it -- and hard to notice, because the tests all
    run with no network anyway.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(rc))
    callers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(call.func, ast.Name) and call.func.id == "fetch"
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
        )
    }
    assert callers == {"cmd_probe"}


def test_the_write_path_is_the_existing_patch_endpoint():
    """No second route into the database, asserted on the source.

    Everything this tool writes goes through PATCH /records/events/<id>, which
    is accepted into the API's outbox, published to the broker and applied by
    the consumer. There is no psycopg import here and no SQL.
    """
    import inspect

    source = inspect.getsource(rc)
    assert "/records/events/" in source
    assert "psycopg" not in source
    assert "INSERT" not in source and "UPDATE " not in source
