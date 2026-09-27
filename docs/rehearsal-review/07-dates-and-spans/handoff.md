# F-8 / F-9 lane — handoff

| | |
|---|---|
| Lane | `docs/rehearsal-review/07-dates-and-spans/` |
| Date | 2026-09-27 |
| Branch | `fix/f8-f9-dates-and-event-spans`, worktree `D:\aow-f8-f9-dates` |
| Base | `a12ae378df45e288c85ab0b710cb0e1e6d55861f` (= `origin/main`) |
| Scope owned | `data/events.seed.jsonl`, `services/agent/dates.py`, their focused tests |
| Not touched | `router.py`, `grounding.py`, consumer/API/ingestor, README, the rehearsal report, the main checkout |

This file supplies replacement wording for the rehearsal report. Applying it is
the report owner's step, not this lane's.

---

## 1. F-8 is a false finding. Withdraw it.

The report's F-8 (`:1294`) says four one-night concert rows spread a single
night over extra days, and asks for their `ends_at` to be set to the show's
real end or dropped. **Acting on it would delete five real concert nights from
the feed** — the precise failure the brief is absolute about.

Every one of the five spanning rows is a genuine multi-day run. Checked
2026-09-27 against each venue's own listing — the same page and the same
JSON-LD block `services/tools/event_recheck.py` reads:

| row | page `startDate` | page `endDate` | stored days |
|---|---|---|---|
| `theo2:laver-cup-2026` | `2026-09-25T11:30:00+01:00` | `2026-09-27T12:30:00+01:00` | 09-25 → 09-27 |
| `theo2:niall-horan-2026` | `2026-10-02T18:30:00+01:00` | `2026-10-03T23:00:00+01:00` | 10-02 → 10-03 |
| `theo2:the-strokes` | `2026-10-06T18:30:00+01:00` | `2026-10-07T23:00:00+01:00` | 10-06 → 10-07 |
| `theo2:westlife-2026` | `2026-10-09T18:30:00+01:00` | `2026-10-11T22:30:00+01:00` | 10-09 → 10-11 |

The three O2 concerts each publish a **door time per night** — two nights for
Niall Horan (both 18:30) and The Strokes (both 18:30), three for Westlife
(18:30, 18:30, 18:00). The date ranges are printed on the pages as
`2 Oct - 3 Oct 2026`, `6 Oct - 7 Oct 2026` and `9 Oct - 11 Oct 2026`.

The Laver Cup values read off the live page are **identical to the fixture
already committed at `tests/unit/test_recheck.py:57-58`**, which is what
confirms these readings came off the real pages and not a lookalike.

The fifth row, `coliseulisboa:radio-macau-2026-09-30`, was never in doubt: the
seed's own `source` field already records *"run of 30 September - 2 October
2026, 21:30 each night"*, and the page still reads
`30 setembro, 2026 a 2 outubro, 2026` at 21:30 with no cancellation marker. It
carries no structured data, which is why `tests/integration/event_recheck.py`
uses it as the row that cannot be auto-renewed.

**No seed row was changed.** `python scripts/snapshot_manifest.py --check`
→ exit 0, 55 events, unchanged.

### Suggested replacement for the F-8 row (`:1294`)

```
| ~~F-8~~ | — | **WITHDRAWN 2026-09-27.** The four concert rows are not
one-night listings. All five spanning rows are genuine multi-day runs,
confirmed against each venue's own JSON-LD (`startDate`/`endDate`) and printed
date range; the three O2 concerts publish a door time for every night. The
Laver Cup values match the fixture at `tests/unit/test_recheck.py:57-58`, and
Rádio Macau's run is already quoted in the seed's own `source` field. Acting on
this finding would have removed five real concert nights. The day spans are now
pinned to their sources by `tests/unit/test_event_spans.py`. |
```

### The one related thing that *is* true, and is not F-8

Seven `theo2:` rows store a local-midnight `starts_at` and a `23:59` end. Those
**instants** are placeholders; the venue pages give real door times. This is a
deliberate, documented decision — a multi-day run is recorded as whole days so
the recheck tool's day-granularity comparison does not flag 00:00 against the
page's 11:30 (`services/tools/event_recheck.py:324-330`). The amendment already
records it correctly as a presentation gap, §5 item 9. **It moves no day**, and
replacing the placeholders would be a separate change with its own evidence.

Note also that changing any `ends_at` in the seed alone would not reach a
running database: `services/consumer/main.py:302` guards the upsert with
`EXCLUDED.as_of > events.as_of`, so a value-only edit is a no-op until the
volume is rebuilt or `as_of` is bumped. Bumping `as_of` on these four rows
was deliberately **not** done — it would assert a 2026-09-27 re-read for 4 of
55 rows and extend their `valid_until`, which is the "extending validity to
make coverage look better" the amendment forbids. The verification is recorded
as test evidence instead.

---

## 2. F-9 is real, and is fixed

Both gaps at `:1295` are confirmed and repaired in `services/agent/dates.py`.

**Gap 1 — a stated range resolved only its first date.** `ISO_DATE.search`
took the first match, so `from 2026-10-05 to 2026-10-14` was answered as the
single day 2026-10-05. Now every ISO date in the question is collected and the
range spans `min`…`max`.

**Gap 2 — no weeks pattern.** `next three weeks` fell through to the assumed
seven days. There is now a `WEEKS_AHEAD` pattern, and a `NUMBER_WORDS` table so
spelled-out counts are read — which also fixes `next three days`, silently
seven days before for the same reason.

Two further defects were found in the lines being rewritten and fixed with them:

- **A 500.** `date(2026, 99, 99)` raises, and the old code let that escape
  `Router.resolve`. `what about 2026-99-99` was enough to return a 500 from
  `/agent/ask`. Unusable dates are now dropped and the question falls through
  to the default, which the footer discloses.
- **A false refusal.** A range written backwards produced an inverted
  `DateRange`, whose `days()` is empty, which the router reports as *"I have no
  weather data for …"* — a refusal covering dates it holds. Ends are now sorted.

### The one behaviour change worth a reviewer's attention

The relative-range bound moved from a bare `16` to `MAX_RELATIVE_DAYS = 31`.
16 was an unlinked duplicate of the ingestor's `FORECAST_DAYS`
(`services/ingestor/main.py:285`) and no test pinned it. It had to move,
because **events are queried over the un-clamped window**
(`services/agent/router.py:552-553`) and the feed holds a London fixture on
2026-10-17 — twenty days out. At 16, `any sports in the next three weeks?`
would have missed `theo2:dubois-vs-wardley-2`, the one London sports row §4.3
warns a reviewer will find. `next 30 days` now resolves to 30 days rather than
16.

Coverage refusal is unchanged and still explicit: `router.py:470` refuses only
when **no** day in the range is covered, and a longer range is therefore
strictly *less* likely to refuse and more likely to emit the
`coverage:partial` gap sentence. A range wholly outside stored data is still
refused by template before the model is reached. Tested.

### Suggested replacement for the F-9 row (`:1295`)

```
| F-9 | **DONE 2026-09-27** | Date parsing: both gaps closed in
`services/agent/dates.py`, plus a 500 on a date-shaped non-date and a false
"no weather data" refusal on a backwards range. Relative ranges are bounded at
31 days rather than 16 so a three-week question can still reach the
2026-10-17 London fixture. 28 focused tests in `tests/unit/test_dates.py`. |
```

---

## 3. Also confirmed, for §4.3 and the amendment

Amendment §5 items 3–7 can be closed as **verified 2026-09-27**: items 3
(Rádio Macau), 4 (Westlife), 5 (Niall Horan), 6 (The Strokes) and 7 (Laver Cup
span) are all confirmed correct as stored. Item 9 (theo2 rows carry no real
showtime) is confirmed **true** and remains an open presentation gap.

Items **1** (Gulbenkian Prokofiev cancellation status), **2** (why `d3eba86`
dropped the Suzanne Dellal row), **8** (the real Dubois vs Wardley 2 local
date) and **10** (the 28 machine-unverifiable rows) were **not** checked here —
they belong to the content-trust lane, not this one, and item 1 is the one that
matters most.

---

## 4. Two things this lane found and did not fix

**A time-rotted test, failing on `main` today, unrelated to this change.**
`tests/unit/test_grounding.py::test_unambiguous_yearless_or_weekday_event_claim_is_checked`
fails for both parameters (`September 26`, `Saturday`) on the unmodified
baseline. `retrieval()` resolves `"…this week"` against the **real** clock
(`tests/unit/test_grounding.py:110`), and its `DAY3` is `2026-09-26` — now in
the past, so the year-less date no longer resolves into the window. It has
failed since 2026-09-27 and will keep failing. The fix is to freeze the clock
the way `test_event_days.py:149` does. **Owner: whoever owns `grounding.py`;
this lane must not edit it.** It does not fail in CI's container only because
CI has not run since the window rolled past.

**`main.py:104` + `services/ui/app.py:609`.** `answer["dates"]` is a string and
the UI renders `", ".join(map(str, answer["dates"]))`, so it iterates
characters: `Dates: 2, 0, 2, 6, -, 0, 9, -, 2, 7`. Pre-existing, outside this
lane, and it gets more visible the longer a resolved range is.

---

## 5. Test results

Run on the Windows host, no Docker, no shared stack.

```
pytest tests/unit/test_dates.py                       28 passed
pytest tests/unit/test_event_spans.py                 58 passed
ruff check services tests scripts                     All checks passed!
ruff format --check services tests scripts            118 files already formatted
python scripts/snapshot_manifest.py --check           exit 0, events = 55
```

The three failures in the wider suite — the two grounding ones above and
`test_coastal_evidence.py::test_the_ui_caption_is_word_for_word_the_agent_caveat`
(needs `streamlit`, absent on the host) — reproduce identically on the
unmodified baseline and are not caused by this change.
