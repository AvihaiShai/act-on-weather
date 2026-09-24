# Evidence: event validity and sea-state claims

**Dated 2026-09-24 (UTC; 2026-09-25 local).** Second pass on review finding
**F9** ("event and activity data limits weaken reviewer examples").

The first pass built the *mechanism* — `checked_at`, `valid_until`, the expiry
filter, `include_expired`, the coast reference points and the sea-activity
score ceiling. This pass re-audited the data that mechanism carries, added the
re-check the first pass left open, and removed one sea-state claim that had
survived the ceiling.

**It does not claim a full event feed or marine suitability.** Both remain open
and are stated as such at the end of each section. Every command below was run;
where something was not run, this file says so.

---

### 1. Every stored listing was re-opened

All 39 rows on the previous tree were re-fetched at their own `source_url`.
All 36 distinct URLs resolved — no timeout, no 403, no bot-gate — so nothing
was unverifiable for transport reasons.

| verdict | count |
|---|---|
| current, matching the stored row | 35 |
| title differs from the listing headline | 2 |
| still listed, date now behind today | 1 |
| **gone — HTTP 404 and absent from the venue index** | **1** |

`suzannedellal:altar-for-the-beast-2026-09-24` was **removed**. Its page is a
hard 404 (confirmed with `curl -L`, against a control URL on the same host that
returns 200) and the show is absent from the Suzanne Dellal Centre's own index.
That venue deletes a show's page once its run ends, so a re-check there cannot
distinguish *cancelled* from *archived*; removal is the only honest response.
Tel Aviv 9 → 8.

**A correction to the record above.** The first pass reported that three titles
were "corrected to what the listing calls the event". For two of them that is
not what happened: The O2's pages carry `<title>`, `og:title` and `<h1>` of
exactly `Niall Horan` and `Westlife`, while the stored titles are the tour
names (*DINNER PARTY Live on Tour*, *Westlife 25: The Anniversary World Tour*),
which appear only in body copy. **The data was left as it is** — both titles
are sourced from those pages and are more informative than the headline — but
the sentence describing how they were derived was wrong, and this corrects it.
(The third, *The Phantoms starring Lee Mead*, is `<h1>` + `<h2>` and is fine.)

### 2. Coverage: 39 → 57 → 55, and Lisbon is no longer the thin city

Nineteen listings were added by this audit, every one read off a first-party
page, taking the feed from 39 to 57. The re-check in section 7 then removed two
that the venue had cancelled, so the **final feed is 55 rows: Lisbon 14,
Reykjavík 13, London 10, Rome 10, Tel Aviv 8.**

- **Lisbon 4 → 16.** The previous pass stopped at the Coliseu dos Recreios and
  the MEO Arena. The **Fundação Calouste Gulbenkian** publishes a per-concert
  agenda that meets the bar in full (8 rows, Grande Auditório), plus 3 more
  Coliseu rows and 1 MEO Arena row.
- **Reykjavík 6 → 13.** Seven further Harpa listings, each hall read off its
  own page.

**This is the finding that matters more than the count.** Lisbon tripled in one
audit purely by reading one more venue's site. That is direct evidence that a
per-city number measures *how many venues were mined*, not what is happening in
the city — Hot Clube, Capitólio, LAV, São Carlos and the fado houses are still
unread. The README now says this in place of treating 16 as coverage.

The **category** spread is the sharper limit: 47 of the 55 rows are `concert`,
`sport` exists only in London, and `market`, `festival` and `exhibition` have
no verified row anywhere, in any city. The router can reach those categories;
the data cannot answer them.

Rejected rather than filed under a category that would misdescribe them:
a Gulbenkian broadcast the venue itself labels `Transmissão`; two conference
talks; film screenings and an open rehearsal; CCB workshops and guided visits
on date-parameterised templates; a São Luiz run outside the window; and
Culturgest entirely, because no listing page with a date was ever fetched for
it — only a search snippet.

### 3. A trap worth recording: a status template that reads as a cancellation

gulbenkian.pt renders **all three** session-status variants into the HTML and
hides the inactive ones with Alpine's `x-cloak`. Flattened to text, every
listing appears to say *Cancelado* and *Esgotado*. A summarising fetch of the
Brahms listing reported "both performances show Cancelado"; the markup shows
otherwise:

```
DAY "15 out 2026": isCancelled() x-cloak(hidden)  isSoldOut() x-cloak(hidden)  !isSoldOut() NO x-cloak -> AVAILABLE
DAY "16 out 2026": isCancelled() x-cloak(hidden)  isSoldOut() NO x-cloak -> SOLD OUT
```

The span **without** `x-cloak` is the server-rendered active state. Anyone
re-checking this venue with a text summariser will get false cancellations —
and false in the dangerous direction, dropping live events. Every Gulbenkian
date stored here was read from that structured markup, not from rendered text.

### 4. Sea state: the ceiling was holding the number, not the claim

`min_wind_kmh: 12` on surfing is **removed**, and `rule_version` goes 3 → 4.

It was justified in-file as "a flat, windless day has no swell" and made a calm
day emit the user-visible reason *"only 8km/h of wind, too flat for this"* — a
verdict on the waves assembled from a land wind reading. The ceiling was
capping the score at 69 while that reason string walked the claim straight
past it.

Checked against Open-Meteo's marine model, using the wind the rule actually
reads (the **stored city forecast**, not the coast point) against the swell at
that city's own coast reference:

| city / coast point | stored land wind | rule's verdict | actual swell |
|---|---|---|---|
| Lisbon / Praia de Carcavelos, 2026-10-03 | 10.9 km/h | penalised, "too flat" | **1.58 m at 8.8 s** — a clean groundswell |
| Rome / Lido di Ostia, 2026-09-25 | 16.9 km/h | unpenalised | **0.40 m at 3.6 s** — dead windslop |

It fails open on a flat sea and closed on a rideable one. Removed rather than
retuned: no threshold on land wind is a measurement of swell. The rule is now
stated in `services/common/rules.py` and `coast.py` as the general one — no
rule may turn a land measurement into a statement about the water, in a score,
a label or a reason.

### 5. A marine provider was probed, not assumed — and still declined

`https://marine-api.open-meteo.com/v1/marine`, keyless, probed against the four
coast reference points:

| point | HTTP | wave/swell/SST | horizon | answered from |
|---|---|---|---|---|
| Lido di Ostia | 200 | real values | **10 of 16 days** | a grid cell **2.9 km** away |
| Praia de Carcavelos | 200 | real values | **10 of 16 days** | **4.6 km** away |
| Gordon Beach | 200 | real values | **10 of 16 days** | **7.2 km** away |
| Nautholsvík | 200 | real values | **10 of 16 days** | **13.2 km** away |

The expected blocker — a wave grid that does not resolve the Tagus estuary or
Faxaflói — **did not occur**; all four return plausible, physically distinct
values. The decision rests on two other facts. The model runs **10 days against
the 16 this system stores**, so six days in sixteen would still need the cap;
and it answers from its own cell up to 13 km from the named point, which stacks
a second "not measured where you think" caveat on the forecast-point distance.

There is also a structural blocker: `scripts/snapshot_manifest.py` requires
every snapshot entity to cover **every** configured city, and marine data for a
five-city set including inland London cannot. Ingesting it means deliberately
weakening that guard, plus a table, a migration, a consumer write path, the
refresh path, scoring and tests. A half-integrated wave feed — stored but
unscored, or scored but unrefreshable — would be worse than the honest cap.

The numbers are recorded in `services/ingestor/providers.py` so the next person
weighs them instead of re-deriving them.

### Gates and drills run on this tree

Each gate run on its own, exit code checked, via `scripts/local-gates.sh`
(Docker, `--network none`):

| gate | result |
|---|---|
| `ruff check services tests scripts` | **pass** |
| `ruff format --check services tests scripts` | **fails on `services/ui/app.py` only** — a concurrent session's uncommitted file, not part of this work |
| unit suite | **1479 passed, 2 skipped** (re-run after the re-check tool and its fix landed) |
| `scripts/snapshot_manifest.py --check` | **pass** — events 55, and every count in README.md, Makefile, ASSIGNMENT.md, TECHNICAL_DECISIONS.md and docs/ARCHITECTURE.md matches it |

Integration, against real Postgres and RabbitMQ on fresh volumes in an isolated
Compose project:

| drill | result |
|---|---|
| `tests/integration/smoke.py` | **PASS** — snapshot → queue → database → API, rules, correction → queue → history |
| `tests/integration/event_local_days.py` (F2 regression) | **PASS** |
| `tests/integration/event_freshness.py` (F9 regression) | **PASS** — a listing left the default read when its check date aged out, stayed counted under `include_expired`, and returned with its derived expiry when re-checked |

Measured on that stack rather than asserted: `GET /events` returned **55 rows,
every one `is_current: true`** — lisbon 14, london 10, reykjavik 13, rome 10,
tel-aviv 8; categories concert 49, dance 4, sport 2, theatre 1, comedy 1.

### What this still does not close

- **"Full event feed" is not claimed and should not be.** 55 rows over a
  handful of venues per city for about three weeks is a hand-checked sample.
  The Lisbon 4 → 16 jump is the evidence for why the number is not coverage.
- **`sport` is London-only**, and `market`, `festival` and `exhibition` have no
  verified row anywhere. Lisbon and Rome both have top-flight football in this
  window; no club fixture page was fetched, so nothing is claimed.
- **A listing page is evidence of a schedule, not of an occurrence.** Every
  verdict here means "the venue's own page said this at the recorded instant".
  Nothing detects a cancellation announced afterwards, and an air-gapped run
  cannot detect one at all. That is what `checked_at` / `valid_until` exist to
  surface.
- **Still no marine data**, and the cap therefore stands. It is now costed with
  measured numbers rather than asserted, and one inference that had leaked past
  it is gone. Even a complete marine integration would leave 6 days in 16
  capped.
- **`tests/fixtures/ui_api.json` still contains the removed reason string**
  ("only 9km/h of wind, too flat for this") on a row explicitly marked
  `rule_version: 2`. It is a captured API response, and the row is an honest
  record of what version 2 produced; editing it would falsify the capture.
- The pre-existing `PATCH`-validation gap recorded at the end of the previous
  section is unchanged and still outside F9.

### 6. The re-check itself: `scripts/event-recheck.sh`

The gap this closes is the one the previous section left open: *"There is no
automated re-check. Extending a listing's life means opening its page and
patching `checked_at`, one row at a time."*

All eleven venue sites were probed before anything was designed, because
whether reliable automation is possible here is a question about the sources,
not about effort:

| site | rows | machine-readable | verdict |
|---|---|---|---|
| `harpa.is` | 13 | `schema.org/Event` JSON-LD: name, startDate, endDate, **eventStatus** | auto-verifiable |
| `theo2.co.uk` | 8 | `Event`/`SportsEvent`/`MusicEvent` JSON-LD: name, dates; **no eventStatus** | auto-verifiable |
| `gulbenkian.pt` | 6 | `MusicEvent` in an `@graph`: name, dates, **eventStatus**, location | auto-verifiable |
| `coliseulisboa.com` | 6 | nothing; date is prose | manual only |
| `casadeljazz.com` | 5 | `ld+json` present but `WebPage` only | manual only |
| `suzannedellal.org.il` | 5 | nothing | manual only |
| `santacecilia.it` | 3 | nothing | manual only |
| `ipo.co.il` | 3 | `WebPage` only | manual only |
| `lso.co.uk` | 2 | nothing | manual only |
| `auditorium.com` | 2 | `WebPage` only | manual only |
| `arena.meo.pt` | 2 | nothing | manual only |

No site bot-blocked the probe; every URL resolved. So the limit is not access,
it is that eight of eleven venues publish no structured data at all — no
microdata, no `<time datetime>`, not even an ISO date in visible text.

**Three traps that would break a naive scraper**, all observed:

1. `auditorium.com` serves a *scheduled* concert's page containing "EVENTO
   ANNULLATO - Ryan Adams" — in the related-events sidebar.
2. `coliseulisboa.com` prints *esgotado* (sold out) on a show going ahead.
3. `gulbenkian.pt` renders all three session states into the HTML and hides two
   with Alpine `x-cloak`, so an available concert's markup contains "Cancelado"
   twice. A summarising fetch of the Brahms listing reported both performances
   as cancelled; the markup and the `eventStatus` both say otherwise.

Hence the design: **raw markup only, never flattened text, never a summariser**,
and `cancelled` reachable from exactly one place — a `schema.org` `eventStatus`.
`RENEWABLE = {confirmed}`; every other verdict needs `--id ID --note "what you
saw"`, filed in the ledger beside the fetch that failed. The check date written
is the instant the page was read, never `now()`. The probe refuses to run
unless `AOW_RECHECK_ALLOW_EGRESS=1`, which only the wrapper sets, and only
while it holds a bounded egress window open.

### 7. What the first live run found, including in our own data

Run against the 57-row feed, connected, egress window opened and verified
closed afterwards:

- **2 rows came back `schema.org/EventCancelled`** — both dates of Gulbenkian's
  Prokofiev Piano Concerto No. 2, hand-verified as scheduled the previous day.
  Verified independently against the venue's JSON-LD and **removed**. Lisbon
  16 → 14, feed 57 → 55. This is the case the whole mechanism exists for: a
  venue cancels after a person checked the page, and an air-gapped run has no
  way to learn that.
- **A bug in the new tool.** Seven of nine `changed` verdicts were false, every
  one exactly one day early on the stored side. `/events` serialises
  `starts_at` to UTC, so a listing recorded at local midnight
  (`2026-10-04T00:00:00+01:00`, which every theo2 row is) comes back as
  `2026-10-03T23:00:00Z`, and taking the date off that instant loses a day.
  `queries.events` already derives the correct local day with `AT TIME ZONE`
  and returns it as `starts_on` — the F2 fix — and the tool was not using it.
  Fixed, with a regression test; the unit tests had missed it because they fed
  in timestamps that still carried the venue's offset.

**Re-measured after both corrections, on the 55-row feed:**

| verdict | rows |
|---|---|
| `confirmed` (renewable) | 21 |
| `unverifiable` | 28 |
| `changed` | 6 |
| `cancelled` / `gone` / `unreachable` | 0 |

The 6 remaining `changed` are reported rather than absorbed, deliberately:
3 are titles or venues we record differently from the page headline (the O2
stores tour names, which appear only in body copy), and 3 are Gulbenkian
multi-session runs where one JSON-LD node spans the whole run while we store
one row per session. None is a silent renewal, which is the property that
matters.

### What section 6–7 does not close

- **28 of 55 rows can only ever be renewed by a person opening the page.**
  That is a property of the sources and will not improve until those venues
  publish structured data.
- **Even `confirmed` means "the page still states this date", not "the show
  will happen."** `theo2.co.uk` publishes no `eventStatus` at all, so a
  cancellation there would surface only as `changed` or `gone`.
- **The tool never deletes.** The two cancelled rows were removed by hand after
  it reported them; there is no delete path in this change.
- **The wrapper's egress-window logic mirrors `scripts/refresh.sh`** and was
  exercised live three times (each time the window was verified closed
  afterwards), but it has no unit tests of its own — only `bash -n` and those
  runs.
