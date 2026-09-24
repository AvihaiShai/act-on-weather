"""Operator-assisted re-check of stored event listings.

    # connected, inside the egress window (scripts/event-recheck.sh does this)
    docker compose exec -T ingestor python -m services.tools.event_recheck probe

    # air-gapped, no sockets at all
    python -m services.tools.event_recheck review --evidence recheck-<stamp>.jsonl

    # air-gapped apart from the stack's own API
    python -m services.tools.event_recheck apply  --evidence recheck-<stamp>.jsonl \
        --all-confirmed --yes

Why this is not a scraper
-------------------------
An event row is a note somebody took of a web page on a particular day
(migration 006). `checked_at` records when that happened and `valid_until`
derives from it, so a row stops being offered as a schedule once nobody has
looked at its page for AOW_EVENT_RECHECK_DAYS. Extending a row's life therefore
means re-opening its page -- and until now that meant doing it by hand, one row
at a time.

The obvious fix is a job that re-fetches every `source_url` and bumps
`checked_at` on HTTP 200. That fix is wrong, and the ten sites this feed is
built from are why. They were each probed on 2026-09-25:

  * www.harpa.is       -- schema.org/Event JSON-LD on all 6 listings, with
                          `startDate`, `endDate` AND `eventStatus`.
  * www.theo2.co.uk    -- schema.org Event/SportsEvent JSON-LD on all 8, with
                          `startDate` and `endDate` but NO `eventStatus`. One
                          of the eight embeds a raw tab inside a JSON string,
                          so it is only parseable with `strict=False`.
  * gulbenkian.pt      -- schema.org/MusicEvent JSON-LD with `startDate`,
                          `endDate`, `eventStatus` and `location`. Its dates are
                          written "2026-10-15 20:00:00": a space instead of the
                          `T`, and no offset. `local_day` reads it anyway.
  * the other eight    -- lso.co.uk, santacecilia.it, casadeljazz.com,
                          auditorium.com, coliseulisboa.com, arena.meo.pt,
                          suzannedellal.org.il, ipo.co.il: NO Event structured
                          data of any kind. No microdata, no `<time datetime>`,
                          no ISO date anywhere in the visible text. The date
                          exists only as prose in the site's own language --
                          "30 setembro, 2026 a 2 outubro, 2026 as 21:30".

So on most of this feed there is nothing to verify against, and a "did it 200?"
check would renew those rows on the strength of the venue's web server still
being up. Worse, the naive text checks are actively wrong:

  * www.auditorium.com serves a *scheduled* concert's page carrying the words
    "EVENTO ANNULLATO - Ryan Adams" -- in the related-events sidebar. Grepping
    for "annullato" cancels a live event.
  * coliseulisboa.com says "O evento encontra-se esgotado" (sold out) on a show
    that is still going ahead. Sold out is not cancelled.
  * gulbenkian.pt renders all three states of every session into the HTML --
    "Cancelado", "Esgotado" and the available one -- and hides the two that do
    not apply with Alpine's `x-cloak`. A scheduled, available concert therefore
    ships the word "Cancelado" twice. Anything that flattens this page to text,
    including an LLM summariser, reads those hidden spans and reports a
    cancellation that is not there.

Raw markup only, never rendered or flattened text
-------------------------------------------------
That last one generalises, so it is a rule rather than a special case. This
tool reads the bytes the server sent and the structured data inside them. It
never renders a page, never executes its JavaScript, never flattens it to text
and never asks a model to summarise it. A verdict of `cancelled` comes from one
place only: a `schema.org` `eventStatus` value. `<title>` is recorded in the
ledger as evidence and is compared against nothing.

The limitation that follows is worth stating plainly: a venue that publishes a
cancellation only in rendered prose, or only after JavaScript runs, is reported
here as `unverifiable`. That is the safe direction -- it sends a human to the
page -- and it is the reason this is an operator-assisted tool rather than a
job.

What this does instead
----------------------
It fetches what can be fetched, extracts only what the page states in
machine-readable form, diffs that against the stored row, and then STOPS. Nothing is
written by `probe`. A row's `checked_at` moves only when a human runs `apply`,
and only in one of two ways:

  * the page itself supported the stored row (`confirmed`), and the operator
    accepted that batch; or
  * the operator opened the page, read it, and said so with `--note`, which is
    filed in the evidence ledger next to what the fetch actually saw.

Everything else -- unreachable, bot-blocked, ambiguous, no structured data,
changed, cancelled, gone -- is reported as needing eyes and is never renewed.
That is the whole point: a tool that honestly says "6 of 39 could be verified,
33 need a human" is useful; one that renews all 39 on a 200 is a liability.

Where the write goes
--------------------
Through the existing path and no other: `apply` issues
`PATCH /records/events/<id>` with a new `checked_at`, which the API accepts into
its outbox, publishes to the broker, and the consumer applies -- re-deriving
`valid_until` from the configured window as it already does. There is no second
route into the database, and `valid_until` is still not patchable.

Evidence
--------
`probe` writes an append-only JSONL ledger. One line per row, carrying the
source URL, the instant of the fetch, the HTTP status, the SHA-256 of the bytes
that were read, what was extracted from them, and the verdict. `apply` appends
a decision line carrying the operator's note and the `message_id` the API
returned -- which is the join key into `outbox` and `ingest_log`, while
`record_history` holds the before/after of the row itself. So "what was observed
and when" survives outside the one timestamp that ends up on the row.

Air-gap
-------
`probe` is the only subcommand that opens a socket to anything outside the
stack, and it refuses to run unless AOW_RECHECK_ALLOW_EGRESS=1 is set. Only
scripts/event-recheck.sh sets it, and only while it holds the egress window
open. `review` makes no network calls at all. `apply` talks to the stack's own
API over the internal network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------- verdicts --
#
# Ordered worst-first, because that is the order an operator should read them
# in. Only CONFIRMED is ever renewable without somebody typing a note.

GONE = "gone"  # the page is a 404/410: the listing was withdrawn
CANCELLED = "cancelled"  # the page says so, in structured data
CHANGED = "changed"  # structured data found, and it disagrees with us
UNREACHABLE = "unreachable"  # timeout, DNS, 5xx, 403/429, not HTML
UNVERIFIABLE = "unverifiable"  # fetched fine, states nothing machine-readable
CONFIRMED = "confirmed"  # the page still supports the stored row

VERDICTS = (GONE, CANCELLED, CHANGED, UNREACHABLE, UNVERIFIABLE, CONFIRMED)

#: The only verdict `apply` will act on without an explicit per-row note.
RENEWABLE = {CONFIRMED}

VERDICT_HELP = {
    GONE: "the listing page is gone (404/410) -- decide whether to drop the row",
    CANCELLED: "the page's own structured data says cancelled or postponed",
    CHANGED: "the page states a different title, date or venue than we stored",
    UNREACHABLE: "could not be read (timeout, DNS, 5xx, 403/429, or not HTML)",
    UNVERIFIABLE: "page reachable, but it states no machine-readable event data",
    CONFIRMED: "the page's structured data still matches the stored row",
}

# schema.org types that mean "a scheduled happening". Matched on the suffix so
# that SportsEvent, MusicEvent, TheaterEvent and friends all land here without a
# list that goes stale. `Event` itself is included by the same rule.
_EVENT_TYPE = re.compile(r"(?:^|[^A-Za-z])[A-Za-z]*Event$")

# schema.org eventStatus values that mean the thing is not happening as listed.
_NOT_HAPPENING = {"eventcancelled", "eventpostponed", "eventrescheduled"}

_LD_JSON = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


# ------------------------------------------------------------- extraction --


@dataclass
class Observation:
    """What a page states about itself, in machine-readable form.

    Every field is either something the page published as structured data or
    None. Nothing here is inferred from prose, because inferring a date from
    prose in four languages is exactly the guessing this tool exists to avoid.
    """

    schema_type: str | None = None
    title: str | None = None
    starts_at: str | None = None
    ends_at: str | None = None
    status: str | None = None
    venue: str | None = None


def _walk(obj: Any):
    """Every dict anywhere in a decoded JSON document.

    Flat rather than schema-aware on purpose: real pages nest their Event under
    `@graph`, under a list, or at the top level, and three of the ten sites here
    do it differently from each other.
    """
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def _types(node: dict) -> list[str]:
    raw = node.get("@type")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, str)]
    return []


def _text(value: Any) -> str | None:
    """schema.org lets most of these be a string, an object or a list."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return _text(value.get("name"))
    if isinstance(value, list):
        for item in value:
            got = _text(item)
            if got:
                return got
    return None


def ld_json_blocks(html: str) -> list[Any]:
    """Every parseable ld+json document in a page.

    `strict=False` is not laziness. www.theo2.co.uk embeds a literal tab inside
    a JSON string in its SportsEvent block, which is invalid JSON; a strict
    parse drops the one node on that page worth reading. Being lenient about
    control characters costs nothing -- the structure still has to be valid --
    and it is the difference between 7 and 8 verifiable rows on that site.
    """
    out = []
    for raw in _LD_JSON.findall(html):
        try:
            out.append(json.loads(raw.strip(), strict=False))
        except (ValueError, TypeError):
            continue
    return out


def extract_observation(html: str) -> Observation | None:
    """The page's own Event node, or None if it does not publish one.

    None is the answer for eight of this feed's ten sites, and returning it
    honestly is the most important thing this function does.
    """
    for document in ld_json_blocks(html):
        for node in _walk(document):
            if not isinstance(node, dict):
                continue
            types = [t for t in _types(node) if _EVENT_TYPE.search(t)]
            if not types:
                continue
            # A node typed as an Event but carrying no date is a navigation
            # stub, not a listing. Requiring startDate is what keeps site-wide
            # boilerplate out of the evidence.
            start = _text(node.get("startDate"))
            if not start:
                continue
            return Observation(
                schema_type=types[0],
                title=_text(node.get("name")),
                starts_at=start,
                ends_at=_text(node.get("endDate")),
                status=_text(node.get("eventStatus")),
                venue=_text(node.get("location")),
            )
    return None


def page_title(html: str) -> str | None:
    """The <title>, recorded as evidence only.

    It is never compared against anything: it carries the venue's name, the
    site's tagline and sometimes a separator-mangled event name, so a mismatch
    would mean nothing. It is in the ledger so a human reading the report later
    can see which page was fetched.
    """
    match = _TITLE_TAG.search(html)
    if not match:
        return None
    text = re.sub(r"\s+", " ", match.group(1)).strip()
    return text[:200] or None


# -------------------------------------------------------------- comparison --


def normalise_title(value: str | None) -> str:
    """Casefolded, punctuation-flattened, whitespace-collapsed.

    This absorbs the differences that are not differences -- a curly apostrophe,
    an en dash, a doubled space, trailing punctuation -- and nothing else. It
    deliberately does NOT try to decide that "Niall Horan" and "Niall Horan:
    DINNER PARTY Live on Tour" are the same event. They might be; that is a
    judgement, and judgements belong to the operator.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("’", "'").replace("‘", "'")
    text = re.sub(r"[‐-―]", "-", text)
    text = re.sub(r"[^0-9a-zA-Z]+", " ", text)
    return " ".join(text.split()).casefold()


def local_day(value: str | None) -> str | None:
    """The calendar day an ISO-8601 instant falls on, in its own offset.

    Days, not instants, and that is a decision with a reason. The stored row for
    `theo2:laver-cup-2026` begins `2026-09-25T00:00:00+01:00` because it is a
    three-day tournament recorded as whole days; the page's SportsEvent node
    says `2026-09-25T11:30:00+01:00`, the first match's start time. Those are
    the same event on the same day and an instant comparison would call it a
    change. Both timestamps already carry the venue's own offset, so no
    timezone database is involved.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.date().isoformat()


def stored_local_day(stored: dict, derived_key: str, instant_key: str) -> str | None:
    """The stored row's local calendar day, preferring the one SQL derived.

    `local_day` is only safe on a timestamp that still carries the venue's own
    offset, and the API's does not: `/events` serialises `starts_at` to UTC, so
    a listing recorded as `2026-10-04T00:00:00+01:00` comes back as
    `2026-10-03T23:00:00Z`. Taking the date off that is a day early, and every
    theo2 row is stored at local midnight -- which made the first run of this
    tool report eight false `changed` verdicts, all of them exactly one day out.

    `queries.events` already solves this: it derives `starts_on`/`ends_on` with
    `AT TIME ZONE` over the city's own zone (the F2 fix) and returns them
    alongside the instants, so the correct day is already in the response. Use
    it when it is there, and fall back to the instant only for a caller that
    did not come through that query.
    """
    derived = stored.get(derived_key)
    if derived:
        # Already a plain local date from SQL; a datetime would still be safe.
        return str(derived)[:10]
    return local_day(stored.get(instant_key))


@dataclass
class Difference:
    field: str
    stored: str | None
    observed: str | None


def compare(stored: dict, observed: Observation) -> list[Difference]:
    """Every field the page states AND we store, where the two disagree.

    A field the page does not publish is not a difference -- it is an absence,
    and reporting absences as changes would bury the real ones.
    """
    diffs: list[Difference] = []
    stored_day = stored_local_day(stored, "starts_on", "starts_at")
    observed_day = local_day(observed.starts_at)
    if observed_day and stored_day and observed_day != stored_day:
        diffs.append(Difference("starts_on", stored_day, observed_day))

    stored_end = stored_local_day(stored, "ends_on", "ends_at")
    observed_end = local_day(observed.ends_at)
    if observed_end and stored_end and observed_end != stored_end:
        diffs.append(Difference("ends_on", stored_end, observed_end))

    if observed.title and normalise_title(observed.title) != normalise_title(stored.get("title")):
        diffs.append(Difference("title", stored.get("title"), observed.title))

    # Venue is compared only when the page names one, and only loosely: a venue
    # is routinely written "The O2 arena" on the page and "The O2 arena
    # (indigo)" in the row, and a hall within a building is not a different
    # event. Containment either way counts as agreement; anything else is
    # reported for a human to judge.
    if observed.venue and stored.get("venue"):
        a, b = normalise_title(observed.venue), normalise_title(stored["venue"])
        if a and b and a not in b and b not in a:
            diffs.append(Difference("venue", stored["venue"], observed.venue))
    return diffs


def status_is_cancelled(status: str | None) -> bool:
    """True only for schema.org's own eventStatus values.

    Never a text search of the page, and never a summary of it. Three observed
    reasons, each from a real page in this feed:

      * auditorium.com serves a scheduled concert's page containing the words
        "EVENTO ANNULLATO - Ryan Adams" in its related-events sidebar;
      * coliseulisboa.com says "esgotado" (sold out) on a show going ahead;
      * gulbenkian.pt renders "Cancelado", "Esgotado" and the available state
        for every session and hides the inactive two with Alpine's `x-cloak`,
        so an available concert's HTML contains "Cancelado" twice.

    A grep cancels all three. So does anything that flattens the page to text,
    which is why nothing in this module does.
    """
    if not status:
        return False
    tail = status.rstrip("/").rsplit("/", 1)[-1].strip().casefold()
    return tail in _NOT_HAPPENING


# ------------------------------------------------------------------ fetch --


@dataclass
class FetchResult:
    """What one HTTP attempt produced. No judgement, just the record."""

    url: str
    fetched_at: str
    ok: bool
    status: int | None = None
    final_url: str | None = None
    content_type: str | None = None
    bytes: int | None = None
    sha256: str | None = None
    error: str | None = None
    html: str | None = field(default=None, repr=False)


def fetch(url: str, *, timeout: float = 20.0, session=None) -> FetchResult:
    """One GET, with every failure mode kept distinct.

    "Could not read the page" and "read the page, it says nothing useful" lead
    to different operator actions, so they are never collapsed into one flag.
    """
    import requests

    now = datetime.now(UTC).isoformat()
    session = session or requests.Session()
    try:
        response = session.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={
                "User-Agent": os.environ.get("AOW_RECHECK_UA", DEFAULT_UA),
                "Accept": "text/html,application/xhtml+xml",
                # These listings are read in English where the site offers it
                # and in the venue's own language where it does not. Only the
                # structured data is parsed either way.
                "Accept-Language": "en;q=1.0,he;q=0.8,is;q=0.8,it;q=0.8,pt;q=0.8",
            },
        )
    except Exception as exc:  # noqa: BLE001 - every transport failure is evidence
        return FetchResult(url=url, fetched_at=now, ok=False, error=f"{type(exc).__name__}: {exc}")

    body = response.content or b""
    result = FetchResult(
        url=url,
        fetched_at=now,
        ok=response.status_code < 400,
        status=response.status_code,
        final_url=str(response.url),
        content_type=(response.headers.get("Content-Type") or "").split(";")[0].strip() or None,
        bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
    )
    if result.ok:
        result.html = response.text
    return result


# --------------------------------------------------------------- classify --


def classify(stored: dict, result: FetchResult) -> tuple[str, Observation | None, list[Difference]]:
    """The whole decision, in one readable function.

    Read it as a list of the things that must be true before a row is allowed to
    be renewed, because that is what it is.
    """
    if result.error is not None:
        return UNREACHABLE, None, []
    if result.status in (404, 410):
        return GONE, None, []
    # 403 and 429 are the bot-block shapes, 5xx is the site being broken, and a
    # 3xx that never resolved is neither a page nor an answer. None of them is
    # evidence that a concert is still on.
    if not result.ok:
        return UNREACHABLE, None, []
    if result.content_type and not result.content_type.startswith(
        ("text/html", "application/xhtml")
    ):
        return UNREACHABLE, None, []
    if not result.html:
        return UNREACHABLE, None, []

    observed = extract_observation(result.html)
    if observed is None:
        # Eight of the ten sites land here on every row. This is the honest
        # answer, not a failure of this function.
        return UNVERIFIABLE, None, []
    if status_is_cancelled(observed.status):
        return CANCELLED, observed, compare(stored, observed)

    diffs = compare(stored, observed)
    if diffs:
        return CHANGED, observed, diffs
    return CONFIRMED, observed, []


# ------------------------------------------------------------------ ledger --


def evidence_line(stored: dict, result: FetchResult) -> dict[str, Any]:
    """One ledger row: what was asked, what came back, and what we made of it."""
    verdict, observed, diffs = classify(stored, result)
    return {
        "kind": "observation",
        "event_id": stored["id"],
        "city_id": stored.get("city_id"),
        "source_url": result.url,
        "fetched_at": result.fetched_at,
        "http_status": result.status,
        "final_url": result.final_url,
        "redirected": bool(
            result.final_url and result.final_url.rstrip("/") != result.url.rstrip("/")
        ),
        "content_type": result.content_type,
        "bytes": result.bytes,
        "sha256": result.sha256,
        "error": result.error,
        "page_title": page_title(result.html) if result.html else None,
        "stored": {
            "title": stored.get("title"),
            "venue": stored.get("venue"),
            "starts_at": stored.get("starts_at"),
            "ends_at": stored.get("ends_at"),
            # The local days actually compared, so the ledger shows the value
            # the verdict rests on rather than only the UTC instant it is
            # derived from. See stored_local_day.
            "starts_on": stored_local_day(stored, "starts_on", "starts_at"),
            "ends_on": stored_local_day(stored, "ends_on", "ends_at"),
            "checked_at": stored.get("checked_at"),
            "valid_until": stored.get("valid_until"),
            "is_current": stored.get("is_current"),
        },
        "observed": asdict(observed) if observed else None,
        "differences": [asdict(d) for d in diffs],
        "verdict": verdict,
        "renewable": verdict in RENEWABLE,
    }


def read_evidence(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def observations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("kind") == "observation"]


def append_evidence(path: str, row: dict[str, Any]) -> None:
    """Append-only. A decision never overwrites the observation it acted on."""
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def summarise(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = dict.fromkeys(VERDICTS, 0)
    for row in observations(rows):
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    return counts


# -------------------------------------------------------------------- HTTP --


def _api_call(url: str, *, method: str = "GET", body: Any = None, timeout: float = 20.0) -> Any:
    """stdlib only, on purpose.

    `probe` runs inside the ingestor, which has `requests`. `review` and `apply`
    have to run wherever the operator is reading the report -- the tooling image
    (docker:28-cli plus bash, curl and a bare python3), or a host with nothing
    but Python. Keeping the two calls that talk to our own API on urllib means
    the offline half of this tool has no dependencies at all.
    """
    import urllib.request

    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _api_get(base: str, path: str, timeout: float = 20.0) -> Any:
    return _api_call(base.rstrip("/") + path, timeout=timeout)


def _api_patch(base: str, event_id: str, fields: dict[str, Any], timeout: float = 20.0) -> dict:
    """The only write in this module, and it is the existing queue path.

    PATCH is accepted into the API's outbox, published to the broker and applied
    by the consumer, which re-derives `valid_until` from `checked_at`. Nothing
    here touches Postgres, and there is no second route that does.
    """
    return _api_call(
        f"{base.rstrip('/')}/records/events/{event_id}",
        method="PATCH",
        body=fields,
        timeout=timeout,
    )


# ---------------------------------------------------------------- probe ----


def cmd_probe(args: argparse.Namespace) -> int:
    if os.environ.get("AOW_RECHECK_ALLOW_EGRESS") != "1":
        print(
            "refusing to probe: AOW_RECHECK_ALLOW_EGRESS is not 1.\n"
            "This subcommand is the only part of the system that fetches from the public\n"
            "internet, so it runs only from scripts/event-recheck.sh, which sets that\n"
            "variable while it holds the egress window open. The air-gapped runtime must\n"
            "never reach this code path.",
            file=sys.stderr,
        )
        return 2

    rows = _api_get(args.api, f"/events?include_expired=true&limit={args.limit}")
    if args.city:
        rows = [r for r in rows if r.get("city_id") in args.city]
    if args.id:
        rows = [r for r in rows if r["id"] in args.id]
    # Generated demo samples cite a venue reference, not a listing page, so
    # there is nothing to re-check on them. They age out on the same rule and
    # are regenerated, which is the right fix for a stale sample.
    rows = [r for r in rows if not r.get("is_sample")]
    if not rows:
        print("no verified event rows matched", file=sys.stderr)
        return 1

    seen_urls: dict[str, FetchResult] = {}
    for row in rows:
        url = row.get("source_url") or ""
        if not url.startswith("http"):
            result = FetchResult(
                url=url, fetched_at=datetime.now(UTC).isoformat(), ok=False, error="no source_url"
            )
        elif url in seen_urls:
            # Three of the 39 rows share a source_url with another row (a run of
            # performances on one show page). Fetching it once is politeness to
            # the venue, and it keeps the two rows' evidence identical, which is
            # what it should be.
            result = seen_urls[url]
        else:
            result = fetch(url, timeout=args.timeout)
            seen_urls[url] = result
        line = evidence_line(row, result)
        print(json.dumps(line, ensure_ascii=False, sort_keys=True), flush=True)
        if args.progress:
            print(
                f"  {line['verdict']:<13} {row['id']}  http={line['http_status']}",
                file=sys.stderr,
                flush=True,
            )
    return 0


# --------------------------------------------------------------- review ----


def _render(rows: list[dict[str, Any]], *, verbose: bool) -> str:
    out: list[str] = []
    counts = summarise(rows)
    total = sum(counts.values())
    out.append(f"{total} stored listing(s) re-checked\n")
    for verdict in VERDICTS:
        if counts.get(verdict):
            out.append(f"  {counts[verdict]:>3}  {verdict:<13} {VERDICT_HELP[verdict]}")
    renewable = sum(1 for r in observations(rows) if r["renewable"])
    out.append("")
    out.append(
        f"{renewable} of {total} can be renewed from the page itself; "
        f"{total - renewable} need a human to open the page."
    )
    out.append("")
    for row in sorted(
        observations(rows), key=lambda r: (VERDICTS.index(r["verdict"]), r["event_id"])
    ):
        if row["verdict"] == CONFIRMED and not verbose:
            continue
        out.append(f"--- {row['event_id']}  [{row['verdict']}]")
        out.append(f"    {row['source_url']}")
        detail = f"    http={row['http_status']} bytes={row['bytes']} sha256={(row['sha256'] or '')[:12]}"
        if row.get("error"):
            detail += f" error={row['error']}"
        if row.get("redirected"):
            detail += f" redirected-to={row['final_url']}"
        out.append(detail)
        if row.get("page_title"):
            out.append(f"    page title: {row['page_title']}")
        if row.get("observed"):
            obs = row["observed"]
            out.append(
                f"    page states: {obs.get('schema_type')} "
                f"{obs.get('title')!r} start={obs.get('starts_at')} "
                f"status={obs.get('status')}"
            )
        for diff in row.get("differences", []):
            out.append(
                f"    CHANGED {diff['field']}: stored {diff['stored']!r} -> page {diff['observed']!r}"
            )
        if row["verdict"] == UNVERIFIABLE:
            out.append("    this site publishes no event structured data; the date is only prose.")
        out.append(
            f"    stored: {row['stored']['title']!r} @ {row['stored']['venue']!r} "
            f"starts {row['stored']['starts_at']} checked {row['stored']['checked_at']}"
        )
        out.append("")
    return "\n".join(out)


def cmd_review(args: argparse.Namespace) -> int:
    rows = read_evidence(args.evidence)
    if args.verdict:
        rows = [
            r for r in rows if r.get("verdict") in args.verdict or r.get("kind") != "observation"
        ]
    print(_render(rows, verbose=args.verbose))
    decisions = [r for r in rows if r.get("kind") == "decision"]
    if decisions:
        print(f"{len(decisions)} decision(s) already recorded in this ledger:")
        for row in decisions:
            print(
                f"  {row['event_id']}  {row['action']}  message_id={row.get('message_id')}  "
                f"note={row.get('note')!r}"
            )
    return 0


# ---------------------------------------------------------------- apply ----


def cmd_apply(args: argparse.Namespace) -> int:
    rows = read_evidence(args.evidence)
    by_id = {r["event_id"]: r for r in observations(rows)}
    if not by_id:
        print("this ledger holds no observations", file=sys.stderr)
        return 1

    # What the operator is asking to renew, and on what grounds.
    chosen: list[tuple[dict[str, Any], str | None]] = []
    if args.all_confirmed:
        chosen += [(r, None) for r in by_id.values() if r["renewable"]]
    for spec in args.id or []:
        row = by_id.get(spec)
        if row is None:
            print(f"no observation for {spec} in this ledger", file=sys.stderr)
            return 1
        if row["renewable"]:
            chosen.append((row, None))
        elif args.note:
            chosen.append((row, args.note))
        else:
            # The refusal that makes this tool defensible. A row nothing
            # verified can still be renewed -- an operator who opened the page
            # is better evidence than any parser -- but it cannot be renewed
            # silently, and what they saw is recorded.
            print(
                f"refusing {spec}: verdict is {row['verdict']!r}, not {CONFIRMED!r}.\n"
                f"  {VERDICT_HELP[row['verdict']]}\n"
                f"  Open {row['source_url']} yourself. If the listing still stands, re-run with\n"
                f"  --note \"what you saw\" to record that a human checked it.",
                file=sys.stderr,
            )
            return 1

    if not chosen:
        print("nothing selected: pass --all-confirmed and/or --id ID", file=sys.stderr)
        return 1

    # Deduplicate, keeping the explicit --id note if one was given.
    merged: dict[str, tuple[dict[str, Any], str | None]] = {}
    for row, note in chosen:
        if row["event_id"] not in merged or note:
            merged[row["event_id"]] = (row, note)

    print(f"about to re-check {len(merged)} row(s) through PATCH /records/events/<id>:")
    for row, note in merged.values():
        how = "page confirmed it" if note is None else f"operator note: {note}"
        print(f"  {row['event_id']:<52} {row['verdict']:<13} {how}")
    if not args.yes:
        print("\ndry run. Re-run with --yes to send these through the queue.")
        return 0

    failures = 0
    for row, note in merged.values():
        # The instant the page was actually read, not "now". A row must never
        # carry a check date later than the observation that justifies it.
        checked_at = row["fetched_at"]
        try:
            accepted = _api_patch(args.api, row["event_id"], {"checked_at": checked_at})
        except Exception as exc:  # noqa: BLE001 - reported per row, never fatal to the batch
            print(f"  FAILED {row['event_id']}: {exc}", file=sys.stderr)
            failures += 1
            append_evidence(
                args.evidence,
                {
                    "kind": "decision",
                    "event_id": row["event_id"],
                    "action": "patch-failed",
                    "decided_at": datetime.now(UTC).isoformat(),
                    "verdict": row["verdict"],
                    "checked_at": checked_at,
                    "note": note,
                    "error": str(exc),
                },
            )
            continue
        append_evidence(
            args.evidence,
            {
                "kind": "decision",
                "event_id": row["event_id"],
                "action": "recheck-accepted",
                "decided_at": datetime.now(UTC).isoformat(),
                "verdict": row["verdict"],
                "evidence_sha256": row["sha256"],
                "observed_at": row["fetched_at"],
                "checked_at": checked_at,
                "note": note,
                "message_id": accepted.get("message_id"),
            },
        )
        print(f"  accepted {row['event_id']} -> message_id={accepted.get('message_id')}")

    print(
        f"\n{len(merged) - failures} re-check(s) accepted into the queue. "
        "The consumer re-derives valid_until from checked_at; follow GET /outbox/<message_id>."
    )
    return 1 if failures else 0


# ----------------------------------------------------------------- main ----


def default_api_base() -> str:
    """Where our own API is, from whichever variable the caller already sets.

    `API` is what demos/lib.sh and scripts/*.sh export (http://edge:8000 inside
    the tooling container, http://localhost:8000 on a host), so honouring it
    means `apply` needs no extra flag wherever the rest of the tooling runs.
    `AOW_API_BASE` wins when both are set, and the compose service name is the
    fallback for a bare `docker compose exec`.
    """
    return os.environ.get("AOW_API_BASE") or os.environ.get("API") or "http://api:8000"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="event_recheck",
        description="Operator-assisted re-check of stored event listings.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="fetch listing pages and write an evidence ledger")
    probe.add_argument("--api", default=default_api_base())
    probe.add_argument("--city", action="append", help="limit to a city; repeatable")
    probe.add_argument("--id", action="append", help="limit to one event id; repeatable")
    probe.add_argument("--limit", type=int, default=500)
    probe.add_argument("--timeout", type=float, default=20.0)
    probe.add_argument("--progress", action="store_true", help="verdicts to stderr as they land")
    probe.set_defaults(func=cmd_probe)

    review = sub.add_parser("review", help="diff the ledger against what is stored (no network)")
    review.add_argument("--evidence", required=True)
    review.add_argument("--verdict", action="append", choices=list(VERDICTS))
    review.add_argument("--verbose", action="store_true", help="show confirmed rows too")
    review.set_defaults(func=cmd_review)

    apply_ = sub.add_parser("apply", help="patch checked_at for chosen rows, through the queue")
    apply_.add_argument("--evidence", required=True)
    apply_.add_argument("--api", default=default_api_base())
    apply_.add_argument("--id", action="append", help="renew one row; repeatable")
    apply_.add_argument(
        "--all-confirmed", action="store_true", help=f"renew every row whose verdict is {CONFIRMED}"
    )
    apply_.add_argument(
        "--note", help="what you saw on the page; required to renew anything not confirmed"
    )
    apply_.add_argument("--yes", action="store_true", help="actually send; otherwise a dry run")
    apply_.set_defaults(func=cmd_apply)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
