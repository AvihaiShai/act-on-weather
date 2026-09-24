"""Headless-browser gate against the real running stack (F11).

Everything before this ran the UI's render functions in-process
(`tests/unit/test_ui.py`, `streamlit.testing.v1.AppTest`, against a stubbed
API). Nothing in the repo had ever loaded `app.py` in an actual browser
through the real `edge` proxy, against the real API, against a real Postgres
row -- the review's browser evidence was produced by hand. This script is
that check, made automatic and made to fail.

It asserts four things, each one a real failure mode, not a print:

1. The page loads and its seven tabs render (`renders_main_tabs`).
2. The page makes zero off-origin network requests (`no_external_requests`).
   `edge/nginx.conf` already sends a CSP that should make this true in any
   spec-compliant browser; this re-proves it from the outside, by recording
   every request Chromium actually issued rather than trusting the header.
3. An as-of / coverage timestamp is visible in the header chips
   (`as_of_is_visible`) -- the assignment requires every answer to carry its
   data's provenance, and the header chip is where that first appears.
4. The Forecast tab's four highlight cards never name a day before the
   viewed city's local "today" (`forecast_cards_are_not_stale`). This is F6:
   the cards used to read off the *first stored row* regardless of whether
   the snapshot's window had already passed, so a stale snapshot could label
   yesterday "today". The fix lives in `services/ui/forecast.py:upcoming_rows`
   -- this test is what would have caught the regression in a browser rather
   than trusting the fix will never move.

Run against the real stack, brought up by `scripts/ci-ui-gate.sh` the same
way `scripts/ci-integration.sh` brings up the ingestion path: its own Compose
project, fresh volumes, generated passwords, `--no-build --pull never`. This
script itself opens no database connection and holds no credential; it only
talks to the browser and, once, to the API `uitest` reaches through `edge` --
the same host a real reviewer's browser would reach.

Exit code is the result: 0 means every assertion held, non-zero means at
least one did not. Nothing here prints PASS without having checked something.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import Page, sync_playwright

UI_BASE_URL = os.environ.get("UI_BASE_URL", "http://edge:8080")
API_BASE_URL = os.environ.get("API_BASE_URL", "http://edge:8000")
NAV_TIMEOUT_MS = int(os.environ.get("UI_GATE_NAV_TIMEOUT_MS", "90000"))

TOP_LEVEL_TABS = [
    "Forecast",
    "Suitability",
    "Trip planner",
    "Places map",
    "Ask the agent",
    "Update data",
    "Data coverage",
]

MONTH_BY_ABBR = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}  # fmt: skip

CARD_LABEL = re.compile(r"^(?P<name>.+?)\s*\N{MIDDLE DOT}\s*(?P<day>.+)$")
CONCRETE_DAY = re.compile(r"^[A-Za-z]{3} (?P<day>\d{1,2}) (?P<mon>[A-Za-z]{3})$")


class Failure(Exception):
    """A gate assertion that did not hold. The caller collects these; it
    never prints a pass for a case that raised one."""


def wait_for_app(page: Page) -> None:
    page.goto(UI_BASE_URL, wait_until="load", timeout=NAV_TIMEOUT_MS)
    # Streamlit renders client-side after the initial HTML; wait for the app
    # shell rather than assuming `load` already painted the tabs.
    page.wait_for_selector("text=act-on-weather", timeout=NAV_TIMEOUT_MS)
    page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)


def renders_main_tabs(page: Page) -> None:
    tabs = page.get_by_role("tab")
    tabs.first.wait_for(timeout=NAV_TIMEOUT_MS)
    labels = tabs.all_inner_texts()
    missing = [name for name in TOP_LEVEL_TABS if not any(name in label for label in labels)]
    if missing:
        raise Failure(f"tabs missing from the rendered page: {missing} (saw: {labels})")
    if len(labels) < len(TOP_LEVEL_TABS):
        raise Failure(f"expected {len(TOP_LEVEL_TABS)} top-level tabs, rendered {len(labels)}")


def no_external_requests(requests_seen: list[str]) -> None:
    if not requests_seen:
        raise Failure("no requests were captured at all -- the listener is not wired up")
    origin = urlparse(UI_BASE_URL)
    allowed_hosts = {origin.hostname, "localhost", "127.0.0.1"}
    offenders = sorted(
        {url for url in requests_seen if urlparse(url).hostname not in allowed_hosts}
    )
    if offenders:
        raise Failure(f"page made {len(offenders)} off-origin request(s): {offenders[:10]}")


def as_of_is_visible(page: Page) -> None:
    chip = page.locator(".aow-chip", has_text="Weather as of")
    if chip.count() == 0:
        raise Failure("no 'Weather as of' chip is rendered in the header")
    text = chip.first.inner_text()
    if not re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC|never", text):
        raise Failure(f"'Weather as of' chip does not carry a timestamp: {text!r}")


def _resolve_card_day(suffix: str, today: date) -> date:
    """Turn a rendered day-label suffix ('today', 'tomorrow', 'Sat 3 Oct')
    back into a date, the same way `forecast.day_label` produced it, so it
    can be compared against the real city-local today.

    The label carries no year, so a concrete 'Sat 3 Oct' is resolved to
    whichever of this year or next year is nearest to today -- the only
    direction a genuinely upcoming day can roll. If that nearest reading is
    still before today, the label is stale, which is exactly the F6 defect.
    """
    if suffix == "today":
        return today
    if suffix == "tomorrow":
        return today + timedelta(days=1)
    match = CONCRETE_DAY.match(suffix)
    if not match:
        raise Failure(f"unrecognised forecast card day label: {suffix!r}")
    day = int(match.group("day"))
    month = MONTH_BY_ABBR[match.group("mon")]
    candidate = date(today.year, month, day)
    if (candidate - today).days < -20:
        candidate = date(today.year + 1, month, day)
    return candidate


def forecast_cards_are_not_stale(page: Page, cities: list[dict]) -> None:
    forecast_tab = page.get_by_role("tab", name=re.compile("Forecast"))
    forecast_tab.click()
    page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)

    warning = page.locator("text=No current forecast for")
    if warning.count() > 0:
        # The whole stored snapshot has fallen behind the real calendar. That
        # is a legitimate state -- a static demo snapshot ages -- and the UI
        # is required to say so rather than show a stale card, which is
        # exactly the property this branch checks instead.
        print(
            f"NOTE: forecast window has expired; UI shows the warning: {warning.first.inner_text()}"
        )
        return

    # `city_picker` (services/ui/app.py) calls `st.selectbox(label,
    # list(options), key=key)` with no `index=`, and `options` is built by
    # iterating `cov["cities"]` in order. Streamlit defaults an `index`-less
    # selectbox to option 0, so on a fresh page the selected city is always
    # `cities[0]` from the same /coverage response the header uses -- reading
    # that back from the browser's own BaseWeb combobox DOM would be reading
    # an implementation detail through a second, fragile route to learn a
    # fact this script already has.
    city = cities[0]

    labels = page.locator('[data-testid="stMetricLabel"]').all_inner_texts()
    card_labels = [label for label in labels if "\N{MIDDLE DOT}" in label]
    if len(card_labels) < 4:
        raise Failure(f"expected 4 highlight cards with a day label, saw: {labels}")

    # services/ui/forecast.py:local_today, re-derived here rather than
    # imported, so this check exercises what the browser actually renders
    # rather than importing the fix and testing itself.
    today = datetime.now(UTC).astimezone(ZoneInfo(city["timezone"])).date()

    stale = []
    for label in card_labels:
        match = CARD_LABEL.match(label)
        if not match:
            raise Failure(f"unparseable highlight card label: {label!r}")
        resolved = _resolve_card_day(match.group("day"), today)
        if resolved < today:
            stale.append((label, str(resolved), str(today)))
    if stale:
        raise Failure(
            f"{len(stale)} forecast card(s) name a day before {city['name']}'s "
            f"local today ({today}): {stale}"
        )
    print(
        f"forecast cards for {city['name']} (local today {today}): "
        f"{card_labels} -- none precede local today"
    )


def main() -> int:
    coverage = requests.get(f"{API_BASE_URL}/coverage", timeout=30).json()
    cities = coverage["cities"]
    if not cities:
        print("FAIL: /coverage reports no cities; nothing to check")
        return 1

    requests_seen: list[str] = []
    failures: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.on("request", lambda request: requests_seen.append(request.url))

        checks = [
            ("page loads", lambda: wait_for_app(page)),
            ("main tabs render", lambda: renders_main_tabs(page)),
            ("as-of timestamp visible", lambda: as_of_is_visible(page)),
            (
                "forecast cards are not stale (F6)",
                lambda: forecast_cards_are_not_stale(page, cities),
            ),
        ]
        for name, check in checks:
            try:
                check()
                print(f"PASS: {name}")
            except Failure as exc:
                failures.append(f"{name}: {exc}")
                print(f"FAIL: {name}: {exc}")

        browser.close()

    # Checked last and against everything the whole session issued, not just
    # what happened before the browser closed.
    try:
        no_external_requests(requests_seen)
        print(f"PASS: zero off-origin requests ({len(requests_seen)} same-origin requests seen)")
    except Failure as exc:
        failures.append(f"zero off-origin requests: {exc}")
        print(f"FAIL: zero off-origin requests: {exc}")

    print()
    if failures:
        for line in failures:
            print(f"FAIL: {line}")
        return 1
    print(f"PASS: all {len(checks) + 1} browser assertions held against the real stack")
    return 0


if __name__ == "__main__":
    sys.exit(main())
