"""The UI (M9, M10).

Nine pages in the navigation, one rule: nothing is rendered without the as-of
stamp of the data behind it. The coverage strip is drawn in the header, from
`GET /coverage`, and every chart and answer sits under it.

The UI holds no business logic and no database credentials. It calls the API,
which is the same API the demo scripts call, which reads through the same
`common.queries` functions the agent uses. So what a reviewer sees on screen
and what the agent says in chat come from one place.

Writes are accepted, not applied: a save or an edit returns 202 and a
message_id, and the page says so rather than pretending the row is already
stored.

Only the selected page is rendered. Navigation is stored in the URL so a
reload or bookmark returns to the same page.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from html import escape
from pathlib import Path

import forecast
import pandas as pd
import places_map
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
import theme
import yaml

API = os.environ.get("API_BASE", "http://api:8000")
TIMEOUT = float(os.environ.get("API_TIMEOUT_S", "180"))
# Set by compose.demo.yml. Demo mode adds 45 generated sample events so the
# planner and the agent can be shown outside London; it must never be possible
# to be in it without seeing that you are, hence the banner below.
DEMO_EVENTS = os.environ.get("AOW_DEMO_EVENTS", "0").strip().lower() in {"1", "true", "yes", "on"}

st.set_page_config(
    page_title="act-on-weather",
    layout="wide",
    page_icon="\N{SUN BEHIND CLOUD}",
    initial_sidebar_state="collapsed",
)
theme.apply()


# ------------------------------------------------------------------ http ----


def api_get(path: str, **params):
    try:
        response = requests.get(f"{API}{path}", params=params, timeout=TIMEOUT)
    except requests.RequestException:
        st.error("The weather service is unreachable. Please try again shortly.")
        st.stop()
    if response.status_code == 404:
        return None
    if not response.ok:
        st.error("The weather service could not load this information. Please try again.")
        st.stop()
    return response.json()


def api_send(method: str, path: str, payload):
    try:
        response = requests.request(method, f"{API}{path}", json=payload, timeout=TIMEOUT)
    except requests.RequestException:
        st.error("The weather service is unreachable. Please try again shortly.")
        return None
    if not response.ok:
        st.error("Your change could not be saved. Please try again.")
        return None
    return response.json()


# Keep short-lived API reads current during routine interactions.
cached_get = st.cache_data(ttl=20, show_spinner=False)(api_get)


def coverage():
    return cached_get("/coverage")


# ---------------------------------------------------------------- header ----


def fmt_ts(value) -> str:
    if not value:
        return "never"
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except ValueError:
        return str(value)


def header(cov) -> None:
    """The single most important element in the UI: it is what stops a stale
    snapshot from looking like live data."""
    first, last = cov.get("weather_first_date"), cov.get("weather_last_date")
    entities = {e["entity"]: e for e in cov["entities"]}

    def rows_of(name: str) -> str:
        return f"{(entities.get(name) or {}).get('rows', 0):,}"

    def events_chip() -> str:
        """Never a single number. "52 events" reads as coverage; "39 verified +
        45 samples" reads as what it is.

        The counts behind it are current rows only, because that is what the
        agent will answer from: an event row is a reading of a listing page
        taken on a particular day, and once it is past its recheck date it
        stops being offered as a schedule (migration 006). Rows that have
        fallen out of the window are named separately rather than dropped from
        the chip, so a reviewer looking at a thin number can tell a feed that
        has gone stale apart from a feed that was never there.
        """
        row = entities.get("events") or {}
        samples = int(row.get("samples") or 0)
        verified = int(row.get("rows") or 0) - samples
        chip = f"{verified} verified + {samples} samples" if samples else f"{verified} verified"
        freshness = cov.get("event_freshness") or {}
        # Generated rows age out on the same rule, so an expired sample is
        # counted here too. Without it a demo run past its recheck window would
        # show a chip that simply stopped mentioning the samples at all.
        expired = int(freshness.get("expired") or 0) + int(freshness.get("samples_expired") or 0)
        if expired:
            chip += f", {expired} expired"
        return chip

    st.markdown(
        f"""
        <div class="aow-hero">
          <h1>act-on-weather</h1>
          <p>Explore the forecast, find a good day to go out, and plan around
             the weather. The dates below show when this information was last updated.</p>
          {theme.chips([
              ("Forecast covers", f"{first} → {last}"),
              ("Weather as of", fmt_ts(cov.get("weather_as_of"))),
              ("Cities", str(len(cov["cities"]))),
              ("Places", rows_of("places")),
              ("City facts", rows_of("facts")),
              ("Events", events_chip()),
          ])}
        </div>
        """,
        unsafe_allow_html=True,
    )
    if DEMO_EVENTS:
        # Counted from the same coverage row the chip above uses. This wording
        # used to carry the numbers as literals, and went stale the first time
        # the verified feed grew.
        _events = entities.get("events") or {}
        _samples = int(_events.get("samples") or 0)
        _verified = int(_events.get("rows") or 0) - _samples
        st.warning(
            f"**Demo mode.** This run also stores {_samples} **generated sample events** "
            "so the trip planner and the agent can be exercised with a denser calendar. "
            "They are titled *Sample: …*, marked `is_sample` in the database, and "
            f"labelled wherever they appear. Only {_verified} events in this system are "
            "real listings. A default run (`docker compose up -d`, without "
            f"`compose.demo.yml`) stores those {_verified} and nothing else, and deletes "
            "any sample row left over from a demo run.",
            icon="⚠",
        )


def city_picker(cov, key: str, label: str = "City") -> str:
    options = {c["name"]: c["id"] for c in cov["cities"]}
    return options[st.selectbox(label, list(options), key=key)]


def date_bounds(cov) -> tuple[date, date]:
    return (
        date.fromisoformat(cov["weather_first_date"]),
        date.fromisoformat(cov["weather_last_date"]),
    )


# ----------------------------------------------------------- 0. dashboard ----


def page_dashboard(cov) -> None:
    """A one-day view of the best stored activity in each city."""
    first, last = date_bounds(cov)
    today = forecast.local_today(cov["cities"][0]["timezone"])
    selected_day = st.date_input(
        "Forecast day",
        value=min(max(today, first), last),
        min_value=first,
        max_value=last,
        key="dashboard_day",
    )
    st.caption(
        "The highest weather suitability score in each city for this day. "
        "Scores describe weather comfort; they do not guarantee an activity or venue is available."
    )
    rows = cached_get("/scores", start=str(selected_day), end=str(selected_day)) or []
    best_by_city = {}
    for row in rows:
        if row["forecast_date"] != str(selected_day):
            continue
        city = row["city_id"]
        if city not in best_by_city or row["score"] > best_by_city[city]["score"]:
            best_by_city[city] = row
    if not best_by_city:
        st.warning("No activity scores are stored for this day.")
        return

    names = {city["id"]: city["name"] for city in cov["cities"]}
    summary = pd.DataFrame(
        [
            {
                "City": names.get(city, city),
                "Activity": row["activity_label"],
                "Score": row["score"],
                "Rating": row["band"],
                "LLM note": row.get("text")
                or (
                    "Deferred — request a note in Suitability"
                    if row.get("status") == "deferred"
                    else "Pending"
                ),
                "Weather as of": fmt_ts(row.get("weather_as_of")),
            }
            for city, row in best_by_city.items()
        ]
    ).sort_values("Score", ascending=False)
    figure = px.bar(
        summary,
        x="City",
        y="Score",
        color="Rating",
        text="Activity",
        color_discrete_map={
            "great": "#1E874B",
            "good": "#5DAE72",
            "fair": "#E2B23C",
            "poor": "#C0362C",
        },
        category_orders={"City": summary["City"].tolist()},
    )
    figure.update_yaxes(range=[0, 100])
    st.plotly_chart(theme.transparent(figure, 420), width="stretch")
    st.dataframe(summary, hide_index=True, width="stretch")
    st.caption(
        "Explore the full weather trend in Forecast, compare every activity in "
        "Suitability, or build a route in Trip planner."
    )


# ----------------------------------------------------------- 1. forecast ----


def page_forecast(cov) -> None:
    city = city_picker(cov, "forecast_city")
    rows = cached_get(f"/weather/{city}")
    if not rows:
        st.warning("No stored weather for that city.")
        return

    city_info = next(item for item in cov["cities"] if item["id"] == city)
    today = forecast.local_today(city_info["timezone"])
    window = forecast.upcoming_rows(rows, city_info["timezone"])
    if not window:
        last_day = max(row["forecast_date"] for row in rows)
        st.warning(
            f"No current forecast for {city_info['name']}. The stored forecast ended "
            f"{last_day}; refresh while connected."
        )
        return

    frame = pd.DataFrame(rows).sort_values("forecast_date")
    frame["forecast_date"] = pd.to_datetime(frame["forecast_date"])

    columns = st.columns(4)
    for column, card in zip(columns, forecast.highlights(window, today), strict=True):
        column.metric(card["label"], card["value"], help=card["help"])
    st.caption(
        f"Showing {len(window)} forecast days, {window[0]['forecast_date']} "
        f"to {window[-1]['forecast_date']}."
    )

    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=frame["forecast_date"],
            y=frame["temp_max_c"],
            name="High (C)",
            mode="lines+markers",
            line={"color": "#C0362C", "width": 3},
        )
    )
    figure.add_trace(
        go.Scatter(
            x=frame["forecast_date"],
            y=frame["temp_min_c"],
            name="Low (C)",
            mode="lines+markers",
            line={"color": "#3B6FE0", "width": 3},
            fill="tonexty",
            fillcolor="rgba(59,111,224,0.10)",
        )
    )
    figure.add_trace(
        go.Bar(
            x=frame["forecast_date"],
            y=frame["precip_mm"],
            name="Rain (mm)",
            marker_color="#6E8CA8",
            opacity=0.45,
            yaxis="y2",
        )
    )
    figure.update_layout(
        yaxis={"title": "Temperature (C)"},
        yaxis2={
            "title": "Rain (mm)",
            "overlaying": "y",
            "side": "right",
            "showgrid": False,
            "rangemode": "tozero",
        },
    )
    st.plotly_chart(theme.transparent(figure, 420), width="stretch")

    st.caption(f"Source: {rows[0]['provider']} · Updated {fmt_ts(rows[0]['as_of'])}")
    with st.expander("The stored rows"):
        st.dataframe(
            frame[
                [
                    "forecast_date",
                    "temp_min_c",
                    "temp_max_c",
                    "precip_mm",
                    "precip_prob",
                    "wind_kmh",
                    "uv_index",
                    "sunshine_hours",
                    "as_of",
                    "revision",
                ]
            ].rename(
                columns={
                    "forecast_date": "Date",
                    "temp_min_c": "Low (°C)",
                    "temp_max_c": "High (°C)",
                    "precip_mm": "Rain (mm)",
                    "precip_prob": "Rain chance",
                    "wind_kmh": "Wind (km/h)",
                    "uv_index": "UV index",
                    "sunshine_hours": "Sunshine (hours)",
                    "as_of": "Updated",
                    "revision": "Version",
                }
            ),
            hide_index=True,
            width="stretch",
        )


# -------------------------------------------------------- 2. suitability ----

# The sea-state caveat, as the UI's own copy.
#
# The UI deliberately shares no code with the services -- it talks to the API
# and nothing else, and its image carries `services/ui/` and `data/` and not
# `services/common/`. So the sentence below is written twice, here and in
# `services/common/coast.sea_state_caveat`, and
# `tests/unit/test_coastal_evidence.py` asserts the two produce the same string
# for the same city row. A caveat that drifts between the chat tab and the
# heatmap would be worse than one written once badly.


def _activities_file() -> Path:
    # The source tree and the UI image put data in different relative places,
    # the same split `places_map._map_file` deals with.
    here = Path(__file__).resolve()
    candidates = (
        here.parent / "data" / "activities.yml",
        here.parent.parent.parent / "data" / "activities.yml",
    )
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[0])


@st.cache_data(ttl=600, show_spinner=False)
def sea_state_activities() -> set[str]:
    """The activities whose quality depends on water nothing here measures.

    Read from the same data/activities.yml the rule engine scores from, rather
    than listed again in this file: the flag and the score ceiling it goes with
    are one decision, and a second hand-maintained list of surfing, swimming,
    fishing and boat rides would be the thing that falls out of date.
    """
    with open(_activities_file(), encoding="utf-8") as handle:
        catalogue = yaml.safe_load(handle)["activities"]
    return {key for key, cfg in catalogue.items() if cfg.get("sea_state_unmeasured")}


def format_km(distance: float) -> str:
    """Matches `common.coast.format_km`: a decimal under ten kilometres, where
    it is the difference between "on the beach" and "a bus ride", and none
    above, where it would only pretend to a precision nobody has."""
    return f"{distance:.1f} km" if distance < 10 else f"{distance:.0f} km"


def sea_state_caption(city: dict) -> str:
    """The UI's copy of `common.coast.sea_state_caveat`; see the note above."""
    name = city.get("name") or city.get("id") or "this city"
    coast_name = city.get("coast_name")
    distance = city.get("coast_distance_km")
    if coast_name and distance is not None:
        where = f"the {name} forecast point, {format_km(float(distance))} from {coast_name}"
    else:
        where = f"the {name} forecast point"
    return (
        f"These scores rate the stored forecast for {where} -- nothing in the data "
        "measures the waves, the swell or the water temperature."
    )


def coastal_points_caption(cities: list[dict]) -> str:
    """The same point made about several cities at once.

    One activity across five cities cannot use the per-city sentence without
    saying "these scores rate" five times over, so the distances are listed
    instead. The claim is identical: the forecast point is not the water, and
    the water is not measured.
    """
    located = [c for c in cities if c.get("coast_name") and c.get("coast_distance_km") is not None]
    if not located:
        return (
            "Nothing in the data measures the waves, the swell or the water "
            "temperature, so these scores rate the land forecast alone."
        )
    distances = "; ".join(
        f"{c['name']} is {format_km(float(c['coast_distance_km']))} from {c['coast_name']}"
        for c in sorted(located, key=lambda c: -float(c["coast_distance_km"]))
    )
    return (
        "Each city is scored at its own forecast point, which is not the water: "
        f"{distances}. Nothing in the data measures the waves, the swell or the "
        "water temperature."
    )


def page_heatmap(cov) -> None:
    st.caption(
        "Compare how the forecast suits each activity. Higher scores mean better "
        "conditions; the notes below explain each recommendation."
    )
    scope = st.radio(
        "Show", ["One city, every activity", "One activity, every city"], horizontal=True
    )

    if scope == "One city, every activity":
        city = city_picker(cov, "heat_city")
        rows = cached_get("/scores", city=city)
        index, columns = "activity_label", "forecast_date"
        city_row = next(c for c in cov["cities"] if c["id"] == city)
        if not city_row["coastal"]:
            st.caption("Water activities are unavailable here because this city is inland.")
        elif any(row["activity"] in sea_state_activities() for row in rows or []):
            # Having a coast is not knowing what the sea is doing, and the
            # table below is where a reader would otherwise assume it is.
            st.caption(sea_state_caption(city_row))
    else:
        activities = cached_get("/activities") or []
        labels = {a["label"]: a["activity"] for a in activities}
        chosen = st.selectbox("Activity", list(labels))
        rows = cached_get("/scores", activity=labels[chosen])
        index, columns = "city_id", "forecast_date"
        if labels[chosen] in sea_state_activities():
            shown = {row["city_id"] for row in rows or []}
            st.caption(coastal_points_caption([c for c in cov["cities"] if c["id"] in shown]))

    if not rows:
        st.warning("No scores on record for that selection.")
        return

    frame = pd.DataFrame(rows)
    pivot = frame.pivot_table(index=index, columns=columns, values="score", aggfunc="max")
    # Best rows at the top; alphabetical order buries the answer.
    pivot = pivot.loc[pivot.mean(axis=1).sort_values(ascending=False).index]
    figure = px.imshow(
        pivot,
        color_continuous_scale=[(0, "#C0362C"), (0.45, "#E2B23C"), (1, "#1E874B")],
        zmin=0,
        zmax=100,
        aspect="auto",
        text_auto=True,
        labels={"color": "score"},
    )
    st.plotly_chart(theme.transparent(figure, 90 + 34 * len(pivot)), width="stretch")

    counts = {"pending": 0, "deferred": 0, "failed": 0}
    for row in rows:
        if row["status"] in counts:
            counts[row["status"]] += 1
    if counts["pending"]:
        st.info(
            f"Notes are still being prepared for {counts['pending']} of {len(rows)} "
            "scores. The scores are ready to use."
        )
    if counts["deferred"]:
        st.caption(
            f"{counts['deferred']} activities have scores without written notes. "
            "You can request a note for a specific activity below."
        )
    if counts["failed"]:
        st.warning(f"{counts['failed']} rows are marked failed; the reason is in the table.")

    st.markdown("**Recommendation notes**")
    ready = [r for r in rows if r.get("text")]
    if ready:
        st.dataframe(
            pd.DataFrame(ready)[
                ["forecast_date", "city_id", "activity_label", "score", "band", "text", "model"]
            ].rename(
                columns={
                    "forecast_date": "Date",
                    "city_id": "City",
                    "activity_label": "Activity",
                    "score": "Score",
                    "band": "Rating",
                    "text": "Recommendation",
                    "model": "Written by",
                }
            ),
            hide_index=True,
            width="stretch",
            height=260,
        )
    else:
        st.caption("No recommendation notes yet.")

    ask_for_activity(cov)


def ask_for_activity(cov) -> None:
    """M2: the catalogue is a default, not the menu."""
    st.divider()
    st.markdown("**Ask about a different activity**")
    st.caption(
        "Try an activity that is not listed above. We will check it against the "
        "forecast and add a short explanation."
    )
    first, last = date_bounds(cov)
    left, middle, right = st.columns([2, 2, 3])
    with left:
        city = city_picker(cov, "req_city")
    with middle:
        day = st.date_input("Date", value=first, min_value=first, max_value=last)
    with right:
        activity = st.text_input("Activity", placeholder="kite surfing, rock climbing, a picnic...")

    if st.button("Ask", type="primary", disabled=not activity):
        result = api_send(
            "POST",
            "/recommendations",
            {"city": city, "forecast_date": str(day), "activity": activity},
        )
        if result:
            st.success("Request received. Your result will appear above when it is ready.")


# --------------------------------------------------------- 3. the agent ----


def page_chat(cov) -> None:
    st.caption(
        "Ask about weather, activities, or places in the five covered cities. "
        "Answers use the stored information and show when it was updated."
    )
    examples = (
        "What is the weather tomorrow in Rome?",
        "What activities can I do with my wife this week in London? "
        "We like concerts, shopping and fine dining.",
        "Tell me something about Tel Aviv.",
    )
    for column, example in zip(st.columns(len(examples)), examples, strict=True):
        if column.button(example, key=f"eg_{hash(example)}", width="stretch"):
            st.session_state["question"] = example

    question = st.text_input(
        "Question", key="question", placeholder="What is the weather tomorrow in Rome?"
    )
    if st.button("Ask the agent", type="primary", disabled=not question):
        with st.spinner("Reading stored data and phrasing an answer..."):
            st.session_state["answer"] = api_send("POST", "/agent/ask", {"question": question})

    answer = st.session_state.get("answer")
    if not answer:
        return
    with st.container(border=True):
        st.markdown(answer["answer"])
        if answer.get("note"):
            st.warning(answer["note"])
        st.caption(answer["as_of"] or "no data behind this answer")
    with st.expander("Answer details"):
        st.write(f"City: {answer['city'] or 'not specified'}")
        st.write(f"Dates: {', '.join(map(str, answer['dates'])) or 'not specified'}")
        st.write(f"Records consulted: {answer['rows_used']}")


# ------------------------------------------------------- 4. trip planner ----

INTERESTS = [
    "concerts",
    "shopping",
    "fine_dining",
    "dining",
    "history",
    "museums",
    "outdoors",
    "sports",
]


def page_planner(cov) -> None:
    city = city_picker(cov, "plan_city")
    first, last = date_bounds(cov)

    left, right = st.columns(2)
    with left:
        start = st.date_input("From", value=first, min_value=first, max_value=last)
    with right:
        end = st.date_input(
            "To", value=min(first + timedelta(days=4), last), min_value=first, max_value=last
        )

    interests = st.multiselect(
        "What are you interested in?",
        INTERESTS,
        help=(
            "Used twice: it filters the places on each day, and it nudges the day's "
            "activity towards something that matches. It is a nudge, not an override -- "
            "the weather score still decides."
        ),
    )

    catalogue = cached_get("/activities", city=city) or []
    labels = {a["label"]: a["activity"] for a in catalogue if a["in_catalogue"]}
    picked = st.multiselect(
        "Only these activities (optional)",
        list(labels),
        help="Leave empty to consider everything scored for this city.",
    )

    varied = st.toggle(
        "Vary the plan across days",
        value=True,
        help=(
            "On. In a warm, dry city one activity scores highest every single day, so "
            "without this a seven-day trip is the same suggestion seven times. Turning "
            "it off shows the raw best-scoring activity per day, repeats and all."
        ),
    )

    if st.button("Build the itinerary", type="primary"):
        plan = api_send(
            "POST",
            "/agent/itinerary",
            {
                "city": city,
                "start_date": str(start),
                "end_date": str(end),
                "interests": interests,
                "activities": [labels[p] for p in picked],
                "pace": "varied" if varied else "best",
            },
        )
        if plan:
            st.session_state["plan"] = plan

    plan = st.session_state.get("plan")
    if plan:
        render_plan(plan, cov)

    st.divider()
    render_saved_itineraries(cov)


def render_plan(plan: dict, cov) -> None:
    """One renderer for a freshly built plan and for a reopened stored one.

    They differ only in what may honestly be said above the days. A built plan
    was scored against the coverage window it names. A stored one carries the
    as-of it was built from, which the forecast may since have moved past, so
    it gets `render_plan_staleness` in place of a coverage line.
    """
    saved = plan.get("saved")
    st.markdown(f"### {plan['title']}")
    if saved:
        provenance = (
            scored_from(plan.get("as_of"))
            if all(day.get("weather_as_of") for day in plan["days"])
            else (
                f"recorded weather timestamp {fmt_ts(plan['as_of'])}; "
                "per-day provenance unavailable"
                if plan.get("as_of")
                else scored_from(None)
            )
        )
        st.caption(f"Saved itinerary · Updated {fmt_ts(saved['updated_at'])} · " f"{provenance}")
        render_plan_staleness(plan)
    else:
        built = (
            f"Built from data as of {fmt_ts(plan['as_of'])}"
            if plan.get("as_of")
            else "Built from stored data with no recorded as-of"
        )
        st.caption(f"{built} · coverage {plan['coverage']['first']} to {plan['coverage']['last']}")
    if plan.get("requested_days_outside_coverage"):
        st.warning(
            "No stored weather for: "
            + ", ".join(plan["requested_days_outside_coverage"])
            + ". Those days are left out rather than guessed."
        )

    for day in plan["days"]:
        render_day(day)

    st.divider()
    if saved:
        render_rename(plan, saved)
    else:
        render_save(plan)


def scored_from(as_of) -> str:
    """How a plan says which snapshot it was scored against.

    A stored plan may have no scoring as-of: the caller that saved it did not
    send one, and nothing invents one on its behalf. `fmt_ts` renders a missing
    value as "never", which beside "scored from weather as of" reads as a
    statement about the weather rather than about the record, so the absent
    case gets its own sentence.
    """
    if not as_of:
        return "scoring timestamp not recorded, so it cannot be compared with what is stored now"
    return f"scored from weather as of {fmt_ts(as_of)}"


def render_plan_staleness(plan: dict) -> None:
    """A stored itinerary is a snapshot of a snapshot.

    Its scores are the rule engine's output against the forecast of the day it
    was built, and nothing re-scores them in place. Redrawing them under the
    header's current as-of stamp would let stale numbers read as live, which is
    the one thing this UI is not allowed to do.
    """
    saved_days = {str(day["date"]): day for day in plan["days"]}
    current_rows = (
        api_get(f"/weather/{plan['city']}", start=plan["start_date"], end=plan["end_date"]) or []
    )
    current_days = {
        str(row["forecast_date"]): row
        for row in current_rows
        if str(row["forecast_date"]) in saved_days
    }

    def stamp(value):
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    stamped_days = [day for day in saved_days.values() if day.get("weather_as_of")]
    if len(stamped_days) == len(saved_days):
        changed = any(
            date_key in current_days
            and stamp(current_days[date_key]["as_of"]) != stamp(day["weather_as_of"])
            for date_key, day in saved_days.items()
            if day.get("weather_as_of")
        )
    else:
        changed = False  # Older plans stored no per-day provenance to compare.
    if changed:
        current = max((stamp(row["as_of"]) for row in current_days.values()), default=None)
        st.info(
            "The stored forecast has been refreshed since this was saved (it is now "
            f"as of {fmt_ts(current)}). The days below are the ones that were saved, "
            "scores and wording included. Build the itinerary again to re-score it "
            "against what is stored now."
        )

    outside = [day for day in saved_days if day not in current_days]
    if outside:
        st.warning(
            "The stored forecast no longer covers: "
            + ", ".join(outside)
            + ". Those days are shown exactly as they were saved and cannot be re-scored."
        )


def render_save(plan: dict) -> None:
    title = st.text_input("Save as", value=plan["title"])
    if st.button("Save this itinerary"):
        saved = api_send(
            "POST",
            "/itineraries",
            {
                "city": plan["city"],
                "title": title,
                "start_date": plan["start_date"],
                "end_date": plan["end_date"],
                "days": plan["days"],
                # The snapshot these scores were computed from, carried from
                # the plan the agent built. It travels with the record because
                # the API must not read the database to find it: a write that
                # depends on Postgres is a write that is lost when Postgres is
                # down, and a clock substituted for it is a provenance nothing
                # scored the plan against.
                "as_of": plan.get("as_of"),
            },
        )
        if saved:
            st.success("Save requested. Your itinerary will appear below shortly.")


def render_rename(plan: dict, saved: dict) -> None:
    """A reopened plan is already a row, so the write here is an edit of it.

    Offering "save" again would post a second row with a new id on every click,
    which is how a list of saved trips turns into a list of duplicates. A
    rename is the M12 patch path: same queue, same revision bump, same history.
    """
    title = st.text_input("Rename", value=plan["title"], key=f"rename_{saved['id']}")
    if st.button("Rename this itinerary", disabled=title.strip() == plan["title"]):
        result = api_send("PATCH", f"/records/itineraries/{saved['id']}", {"title": title.strip()})
        if result:
            st.success("Rename requested. The new title will appear shortly.")


def render_saved_itineraries(cov) -> None:
    """The way back into a stored trip.

    This list used to render only underneath a freshly built plan, so a saved
    itinerary could be seen and never reopened. It is its own section now, it
    does not wait for a plan to exist, and the days come from
    `GET /itineraries/{id}` -- the list endpoint carries titles and dates only.
    """
    st.markdown("**Saved itineraries**")
    rows = cached_get("/itineraries") or []
    if not rows:
        st.caption("No saved itineraries yet. Build one above to get started.")
        return

    by_id = {row["id"]: row for row in rows}

    def label(itinerary_id: str) -> str:
        row = by_id[itinerary_id]
        return f"{row['title']} · {row['city_id']} · {row['start_date']} → {row['end_date']}"

    left, right = st.columns([5, 1], vertical_alignment="bottom")
    chosen = left.selectbox(
        "Open a saved itinerary", list(by_id), format_func=label, key="saved_itinerary"
    )
    if right.button("Open", width="stretch"):
        # Uncached on purpose: this is a click on one named row, and a 20s-old
        # copy of a plan someone just corrected is worth less than one API call.
        row = api_get(f"/itineraries/{chosen}")
        if row:
            st.session_state["plan"] = plan_from_saved(row, cov)
            st.rerun()
        else:
            st.error("That itinerary is no longer available.")

    confirmed = st.checkbox(
        "Confirm removal of this saved itinerary and its edit history",
        key=f"remove_confirm_{chosen}",
    )
    if st.button("Delete selected saved itinerary", disabled=not confirmed):
        result = api_send("DELETE", f"/itineraries/{chosen}", None)
        if result:
            plan = st.session_state.get("plan") or {}
            if (plan.get("saved") or {}).get("id") == chosen:
                st.session_state.pop("plan", None)
            cached_get.clear()
            st.success("Removal requested. The saved itinerary will disappear shortly.")

    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def plan_from_saved(row: dict, cov) -> dict:
    """Shape a stored row like a built plan, so that one renderer -- and the
    map tab's highlight -- serve both. `saved` is what marks it as stored."""
    return {
        "city": row["city_id"],
        "title": row["title"],
        "start_date": str(row["start_date"]),
        "end_date": str(row["end_date"]),
        "days": row.get("days") or [],
        "as_of": row.get("as_of"),
        "coverage": {
            "first": cov.get("weather_first_date"),
            "last": cov.get("weather_last_date"),
        },
        "saved": {
            "id": row["id"],
            "revision": row["revision"],
            "updated_at": row["updated_at"],
        },
    }


def render_day(day: dict) -> None:
    band = day.get("activity_band") or "poor"
    icon = day.get("activity_icon") or ""
    with st.container(border=True):
        st.markdown(
            f'<div class="aow-day-head">'
            f'<span class="aow-day-date">{day["date"]}</span>'
            f'<span class="aow-day-act">{icon} {day.get("activity") or "no scored activity"}</span>'
            f"{theme.score_pill(day.get('activity_score'), band)}"
            f"</div>",
            unsafe_allow_html=True,
        )
        if day.get("why"):
            st.caption(day["why"])
        # Written by the agent, next to the score it qualifies, and shown here
        # for the same reason it appears under the heatmap: a plan is where a
        # coastal score stops being data and starts being advice.
        if day.get("activity_caveat"):
            st.caption(day["activity_caveat"])

        alternatives = day.get("alternatives") or []
        if alternatives:
            pills = "".join(
                f'<span class="aow-alt">{alt.get("icon") or ""} {alt["label"]} '
                f'<b>{alt["score"]}</b></span>'
                for alt in alternatives
            )
            st.markdown(
                f'<div style="margin-top:.4rem"><span style="font-size:.8rem;'
                f'color:#55607A">Also good that day:</span> {pills}</div>',
                unsafe_allow_html=True,
            )

        # Where the day's activity actually happens, kept separate from the
        # interest-matched list so the two are never confused for each other.
        venues = day.get("activity_places") or []
        wanted = day.get("activity_place_categories") or []
        if venues:
            st.markdown(
                f"**For {day.get('activity') or 'this'}:** "
                + ", ".join(
                    f"[{v['name']}]({v['source_url']})" + (" *(sample)*" if v["is_sample"] else "")
                    for v in venues
                )
            )
        elif wanted:
            # The activity has a venue category and the city has no row for it.
            # Saying so beats letting the interest list below stand in for it.
            st.caption(
                f"No {', '.join(wanted)} on record for this city, so this day names "
                "no venue rather than suggesting one the data does not support."
            )

        if day["places"]:
            st.markdown(
                "Places on record: "
                + ", ".join(
                    f"[{p['name']}]({p['source_url']})" + (" *(sample)*" if p["is_sample"] else "")
                    for p in day["places"]
                )
            )
        else:
            st.caption("No places on record for those interests in this city.")

        for event in day["events"]:
            venue = f", {event['venue']}" if event["venue"] else ""
            # A run that spans days says so on each of them, so the same line
            # appearing three times reads as one tournament rather than three
            # separate fixtures.
            run = (
                f" \N{MIDDLE DOT} day {event['day_index']} of {event['day_count']}"
                f" ({event['starts_on']} to {event['ends_on']})"
                if event.get("day_count", 1) > 1
                else ""
            )
            if event["is_sample"]:
                # A sample has no listing to link to, because there is no
                # listing -- the URL is the Wikidata entry for the real venue
                # it was anchored to. Rendering it as the event's own source
                # would be the one misleading thing in an otherwise
                # thoroughly labelled row.
                st.markdown(
                    f"Event: {escape(event['title'])} "
                    f"({event['category']}{venue}){run} "
                    f'<span class="aow-sample">sample</span> '
                    f"\N{MIDDLE DOT} [venue reference]({event['source_url']})",
                    unsafe_allow_html=True,
                )
            else:
                # The check date travels with the line, not just with the page.
                # A traveller reading "Laver Cup, 25 to 27 September" is reading
                # somebody's note of a web page, and the date that note was
                # taken is the difference between a schedule and a recollection.
                checked = str(event.get("checked_at") or "")[:10]
                stamp = f" \N{MIDDLE DOT} listing checked {checked}" if checked else ""
                st.markdown(
                    f"Event: [{event['title']}]({event['source_url']}) "
                    f"({event['category']}{venue}){run}{stamp}",
                    unsafe_allow_html=True,
                )


# ---------------------------------------------------------- places map ----

# The backdrop palette. Everything here recedes: the markers carry Plotly's
# qualitative colours, so the streets, water and parks under them are low
# -contrast tints chosen to sit on the #F1F6FA plot background without
# competing with a marker for attention. Area layers get a fill, line layers
# only a stroke; which is which is `places_map.AREA_LAYERS`.
BASEMAP_STYLE = {
    "coastline": {"line": "#6EAFC4", "width": 1.6},
    "park": {"line": "rgba(126, 166, 120, 0.35)", "width": 0.6, "fill": "rgba(206, 228, 200, 0.6)"},
    "water": {"line": "rgba(110, 175, 196, 0.5)", "width": 0.6, "fill": "rgba(186, 219, 234, 0.8)"},
    "waterway": {"line": "rgba(110, 175, 196, 0.9)", "width": 1.4},
    "road_minor": {"line": "rgba(168, 178, 192, 0.55)", "width": 0.7},
    "road_major": {"line": "rgba(138, 150, 168, 0.85)", "width": 1.3},
}


def page_places_map(cov) -> None:
    city_id = city_picker(cov, "map_city")
    city = next(c for c in cov["cities"] if c["id"] == city_id)
    places = cached_get("/places", city=city_id, limit=500) or []
    places_as_of = next(
        (item.get("as_of") for item in cov["entities"] if item["entity"] == "places"),
        None,
    )
    # The basemap is a file in this image, so it carries its own as-of stamp
    # rather than the snapshot's. A city with no staged extract says so here
    # instead of silently drawing an empty panel.
    base = places_map.basemap(city_id)
    basemap_note = (
        f"OpenStreetMap extract as of {fmt_ts(base.properties.get('as_of'))}"
        if base
        else "no street layer staged for this city"
    )
    st.caption(
        f"Stored places as of {fmt_ts(places_as_of)} · "
        f"local {basemap_note} · no online map tiles"
    )
    if not places:
        st.info("No places are stored for this city.")
        return

    located = [p for p in places if p.get("lat") is not None and p.get("lon") is not None]
    if not located:
        st.info("The stored places for this city have no coordinates to plot.")
        return

    categories = sorted({p["category"] for p in located})
    left, right = st.columns([1, 2])
    with left:
        selected = st.selectbox(
            "Category", ["All categories", *categories], format_func=lambda s: s.replace("_", " ")
        )
    with right:
        search = st.text_input("Find a place", placeholder="Filter by name").strip().casefold()
    shown = [
        p
        for p in located
        if (selected == "All categories" or p["category"] == selected)
        and (not search or search in p["name"].casefold())
    ]
    if not shown:
        st.info("No stored places match those filters.")
        return

    center_lat, center_lon = float(city["lat"]), float(city["lon"])
    x_range, y_range = places_map.extent(center_lat, center_lon, located)
    figure = go.Figure()

    # The backdrop goes on first so it stays under the markers. One trace per
    # layer, not per street: the ways are already joined with `None` gaps.
    # Every vertex is read from the gzipped extract in this image -- there is
    # no tile request here, and there is nothing to request offline.
    projected = places_map.basemap_xy(city_id, center_lat, center_lon, x_range, y_range)
    for layer in places_map.BASEMAP_LAYERS:
        if layer not in projected:
            continue
        xs, ys = projected[layer]
        style = BASEMAP_STYLE[layer]
        figure.add_trace(
            go.Scatter(
                x=xs,
                y=ys,
                mode="lines",
                name=f"{places_map.TRACE_PREFIX}{layer}",
                line={"color": style["line"], "width": style["width"]},
                fill="toself" if layer in places_map.AREA_LAYERS else "none",
                fillcolor=style.get("fill"),
                hoverinfo="skip",
                showlegend=False,
            )
        )

    # Natural Earth is the fallback shoreline, and only the fallback. It is
    # generalized at 1:10m, which at this view puts Reykjavik's coast several
    # hundred metres inland of the streets beside it: drawn together the two
    # layers visibly disagree, and the accurate one is the OSM extract. So it
    # is drawn for a city that has no extract staged, and not otherwise.
    if not base:
        coast_x, coast_y = places_map.coastline_xy(center_lat, center_lon, x_range, y_range)
        if coast_x:
            figure.add_trace(
                go.Scatter(
                    x=coast_x,
                    y=coast_y,
                    mode="lines",
                    name=f"{places_map.TRACE_PREFIX}ne_coastline",
                    line={"color": "#6EAFC4", "width": 2},
                    hoverinfo="skip",
                    showlegend=False,
                )
            )

    palette = px.colors.qualitative.Plotly
    for index, category in enumerate(categories):
        rows = [p for p in shown if p["category"] == category]
        if not rows:
            continue
        xy = [
            places_map.local_xy(float(p["lat"]), float(p["lon"]), center_lat, center_lon)
            for p in rows
        ]
        figure.add_trace(
            go.Scatter(
                x=[x for x, _ in xy],
                y=[y for _, y in xy],
                mode="markers",
                name=category.replace("_", " "),
                text=[escape(p["name"]) for p in rows],
                customdata=[[p["lat"], p["lon"]] for p in rows],
                hovertemplate=(
                    "<b>%{text}</b><br>"
                    + category.replace("_", " ")
                    + "<br>%{customdata[0]:.5f}, %{customdata[1]:.5f}<extra></extra>"
                ),
                marker={
                    "size": 12,
                    "color": palette[index % len(palette)],
                    "line": {"color": "#FFFFFF", "width": 1.5},
                    "opacity": 0.9,
                },
            )
        )

    plan = st.session_state.get("plan")
    if (
        plan
        and plan.get("city") == city_id
        and st.checkbox("Highlight the current itinerary's stops", value=True)
    ):
        stops = [
            (day["date"], stop)
            for day in plan["days"]
            for stop in day["places"]
            if stop.get("lat") is not None and stop.get("lon") is not None
        ]
        if stops:
            xy = [
                places_map.local_xy(float(p["lat"]), float(p["lon"]), center_lat, center_lon)
                for _, p in stops
            ]
            figure.add_trace(
                go.Scatter(
                    x=[x for x, _ in xy],
                    y=[y for _, y in xy],
                    mode="markers",
                    name="Current itinerary",
                    text=[f"{day}: {escape(p['name'])}" for day, p in stops],
                    hovertemplate="<b>%{text}</b><extra></extra>",
                    marker={
                        "symbol": "circle-open",
                        "size": 22,
                        "color": "#E37A22",
                        "line": {"width": 3},
                    },
                )
            )

    figure.add_trace(
        go.Scatter(
            x=[0],
            y=[0],
            mode="markers",
            name="City centre",
            hovertemplate=f"{escape(city['name'])} centre<extra></extra>",
            marker={"symbol": "cross", "size": 13, "color": "#141A2A"},
        )
    )
    theme.transparent(figure, 610)
    figure.update_layout(
        plot_bgcolor="#F1F6FA",
        dragmode="pan",
        legend={"orientation": "h", "y": -0.18, "x": 0},
        margin={"l": 45, "r": 20, "t": 15, "b": 75},
    )
    figure.update_xaxes(title="East–west distance from city centre (km)", range=x_range)
    figure.update_yaxes(
        title="North–south distance from city centre (km)",
        range=y_range,
        scaleanchor="x",
        scaleratio=1,
    )
    st.plotly_chart(figure, width="stretch", config={"scrollZoom": True})
    st.caption(
        f"{len(shown)} of {len(places)} stored places shown. "
        "Pan or zoom to inspect markers. The map covers approximately 20 km "
        "around the city centre and does not provide directions. "
        "Map data © OpenStreetMap contributors, ODbL."
        + ("" if base else " A background map is unavailable for this city.")
    )
    st.dataframe(
        pd.DataFrame(
            {
                "Place": [p["name"] for p in shown],
                "Category": [p["category"].replace("_", " ") for p in shown],
                "Source": [p.get("source_url") for p in shown],
            }
        ),
        column_config={"Source": st.column_config.LinkColumn("Source")},
        hide_index=True,
        width="stretch",
    )


# --------------------------------------------------------- update data ----


def page_update(cov) -> None:
    """M12. The brief requires a way to update stored information and prescribes
    no method, so there are three, and this tab is where they live. Two of them
    work with no connectivity at all; the third is the one thing in the system
    that cannot."""
    st.caption("Check the latest forecast, correct a record, or refresh a recommendation note.")
    render_user_data_wipe()
    st.divider()

    refresh_tab, correct_tab, reword_tab = st.tabs(
        [
            "1 · Operator refresh (connected)",
            "2 · Correct a record",
            "3 · Refresh recommendation notes",
        ]
    )

    with refresh_tab:
        render_refresh(cov)
    with correct_tab:
        render_correction(cov)
    with reword_tab:
        render_reenrich(cov)


def render_refresh(cov) -> None:
    """The one update path that needs a route out -- and the only tab in this UI
    that describes an operator action instead of performing one.

    There is deliberately no button here. Pressing it would have to reach
    something that can attach the ingestor to a network with a route out, which
    means either the Docker socket inside this container or an unauthenticated write
    endpoint that runs host commands. Both are a worse problem than the one they
    solve, in a container whose entire job is rendering read-only views.

    So this tab shows three things, and keeps them apart, because conflating the
    first two is the mistake this page used to make:

      1. how fresh the STORED forecast is, city by city, read from the database;
      2. what the LAST RUN of the command did, read from the report it files at
         `GET /refresh/last` -- which cities the provider answered for, the
         as-of before and after, the ids it accepted, how many were stored, and
         whether the egress window closed;
      3. the command itself.

    (1) and (2) answer different questions and can disagree in the way that
    matters most: stored data can look fresh because an *earlier* run worked
    while the most recent one failed outright. A single freshness stamp cannot
    show that, and the previous version of this tab showed only the stamp.

    Nothing on this page fetches anything -- and it says so, because a page that
    prints a command next to a freshness stamp reads as if it had just run it.
    """
    st.markdown(
        "**Operator refresh.** Run from a shell on the Docker host. Needs "
        "connectivity — this is the one path that cannot work air-gapped, by design."
    )
    st.warning(
        "This page does not fetch. It shows what is **stored**, what the **last "
        "run of the command did**, and the command itself. Nothing here reaches "
        "the provider.",
        icon="ℹ",
    )

    st.markdown("#### 1. What is stored right now")
    st.caption(
        "From the database, through the API. This is the state of the data, not "
        "the outcome of any particular refresh."
    )
    columns = st.columns(4)
    columns[0].metric("Newest weather as-of", fmt_ts(cov.get("weather_as_of")).replace(" UTC", ""))
    columns[1].metric("Age", age_of(cov.get("weather_as_of")))
    columns[2].metric("Covers from", cov.get("weather_first_date") or "-")
    columns[3].metric("Covers to", cov.get("weather_last_date") or "-")

    st.dataframe(freshness_table(cov), hide_index=True, width="stretch")
    st.caption(
        "Per city, because a refresh that only half worked looks exactly like "
        "this: four cities stamped minutes ago and one still carrying last "
        "week's as-of."
    )
    if st.button("Re-read stored data", key="refresh_reread"):
        st.cache_data.clear()
        st.rerun()
    st.caption(
        "Re-reads the database and the last-run report through the API. It does "
        "not contact the weather provider — only the command below does that."
    )

    render_last_run()

    st.markdown("#### 3. The operator command")
    st.code("docker compose -f compose.tools.yml run --rm refresh", language="bash")
    st.caption(
        "It attaches **only the ingestor** to a network it creates for the "
        "occasion, fetches the forecast into the same outbox every other record "
        "uses, then detaches the ingestor and deletes that network again from a "
        "trap — on success, on a failed fetch and on Ctrl-C alike — and asserts "
        "both before reporting. It prints per-city "
        "success or failure, the as-of before and after, the accepted message "
        "ids, and how many of them are stored versus still in flight. "
        "`--check` opens and closes the window without fetching, and needs no "
        "connectivity."
    )

    with st.expander("The manual sequence, and why the wrapper exists"):
        st.markdown(
            "Typed by hand it is three commands, and the **third** is the one "
            "that matters: it is what puts the ingestor back on the internal "
            "network. Until it runs, one service in this stack still has a route "
            "out."
        )
        st.code(
            "docker compose -f compose.yml -f compose.connected.yml up -d ingestor\n"
            "docker compose exec ingestor python -m services.ingestor.refresh\n"
            "docker compose up -d ingestor",
            language="bash",
        )
        st.markdown(
            "The wrapper exists because a step an operator has to remember is a "
            "step that gets skipped — and because a failed fetch, or a closed "
            "terminal, skips it too."
        )


def render_user_data_wipe() -> None:
    st.markdown("### Wipe all user data")
    st.caption(
        "Remove every saved itinerary, visitor requested activity, manual correction "
        "and edit history. Collected weather, places, facts and events are restored "
        "from their source records, including connected updates."
    )
    counts = api_get("/user-data") or {}
    st.write(
        f"Currently: {counts.get('saved_itineraries', 0)} saved itineraries, "
        f"{counts.get('requested_activities', 0)} requested activities, "
        f"{counts.get('manual_corrections', 0)} manual corrections."
    )
    confirmation = st.text_input("Type WIPE to confirm", key="wipe_user_data_confirm")
    if st.button("Wipe all user data", disabled=confirmation != "WIPE"):
        result = api_send("POST", "/user-data/wipe", {"confirm": "WIPE"})
        if result:
            st.session_state["wipe_message_id"] = result["message_id"]
            st.info("Wipe accepted. It will finish after earlier queued writes are applied.")

    message_id = st.session_state.get("wipe_message_id")
    if message_id:
        status = api_get(f"/user-data/wipe/{message_id}") or {}
        if status.get("status") == "complete":
            for key in ("plan", "question", "answer", "last_patch"):
                st.session_state.pop(key, None)
            cached_get.clear()
            st.session_state.pop("wipe_message_id", None)
            st.success("User data wiped. Collected records remain available.")
        else:
            st.info(f"Wipe status: {status.get('status', 'pending')}.")
            if st.button("Check wipe status"):
                st.rerun()


OUTCOMES = {
    "ok": ("Succeeded", "success"),
    "ok-with-rows-in-flight": ("Succeeded, rows still in flight", "info"),
    "cities-failed": ("The provider refused at least one city", "error"),
    "accepted-not-stored": ("Accepted, but nothing reached the database", "error"),
    "no-fetch-report": ("The fetch produced no report", "error"),
    "window-not-closed": ("THE EGRESS WINDOW DID NOT CLOSE", "error"),
}


def render_last_run() -> None:
    """Section 2: what the last run of the command actually did (M12, F4).

    Read from `GET /refresh/last`, which serves one JSON file that
    `scripts/refresh.sh` files on its way out. This is the part a reviewer cannot
    get from the freshness table: a refresh that failed leaves the stored data
    exactly as fresh as it was, so the only place the failure shows is here.

    Nothing on this page can create that report. There is no write route for it;
    the only way to record a run is to run the command on the host.
    """
    st.markdown("#### 2. What the last run of that command did")
    payload = cached_get("/refresh/last") or {}
    if not payload.get("recorded"):
        st.info(
            "**No refresh run has been recorded on this stack.** That is the "
            "normal state of a fresh install: the bundled snapshot was loaded at "
            "startup, which is not a refresh. It also means nobody has run the "
            "command below since this stack's volumes were created — not that a "
            "refresh failed.",
            icon="ℹ",
        )
        return

    run = payload["report"]
    window = run.get("egress_window") or {}
    label, kind = OUTCOMES.get(run.get("outcome"), (run.get("outcome") or "Unknown", "warning"))
    # st.success / st.error / st.info / st.warning -- the banner colour is the
    # first thing read, so the outcome picks it rather than a fixed style.
    getattr(st, kind)(
        f"**{label}** · exit code `{run.get('exit_code')}` · recorded "
        f"{fmt_ts(run.get('recorded_at'))}{ago(run.get('recorded_at'))}"
    )

    columns = st.columns(4)
    columns[0].metric("Accepted", run.get("accepted", 0))
    columns[1].metric("Published", run.get("published", "-"))
    columns[2].metric("Stored", run.get("stored", 0))
    # The claim the whole command exists to make, so it gets a card of its own
    # rather than a line in a caption.
    columns[3].metric(
        "Egress window",
        "closed" if window.get("closed_and_verified") else "NOT CLOSED",
        help=(
            "Whether the one container given a route out was put back on the "
            "internal network, and the temporary network deleted, and both "
            "checked afterwards."
        ),
    )

    st.dataframe(last_run_table(run), hide_index=True, width="stretch")

    caption = (
        f"Run against project `{run.get('project') or '?'}` via "
        f"`{window.get('network') or 'no window'}`"
    )
    if window.get("held_seconds") is not None:
        caption += f", open for {window['held_seconds']}s"
    if window.get("deadline_seconds"):
        caption += (
            f" of a {window['deadline_seconds']}s hard limit that a separate guard "
            "enforced, so the window could not have outlived the command even if "
            "it had been killed outright"
        )
    else:
        caption += (
            " with **no deadline guard on this run** — the window was closed only by "
            "the command's own trap, which a `kill -9` would have beaten"
        )
    if window.get("inherited_open_window"):
        caption += ". It also found a window left open by an earlier run and closed that"
    st.caption(caption + ".")
    if run.get("stored", 0) < run.get("accepted", 0):
        st.caption(
            "Accepted but not yet stored is owed, not lost: those rows are "
            "fsynced in the ingestor's outbox and replay on their own."
        )

    with st.expander("The accepted message ids, and the raw report"):
        st.caption(
            "Each id can be followed at `GET /outbox/{message_id}`, which says "
            "whether it is stored."
        )
        st.json(run)


def last_run_table(run) -> pd.DataFrame:
    """One row per city the run asked for, with its as-of on both sides.

    `ok` is tri-state on purpose: `None` means the run never got as far as this
    city — an interrupt, or a stack that was not answering — which is a different
    thing from the provider refusing it.
    """
    rows = []
    for city in run.get("cities", []):
        ok = city.get("ok")
        rows.append(
            {
                "City": city.get("name") or city.get("city"),
                "Result": {True: "fetched", False: "FAILED", None: "not attempted"}[ok],
                "Days": city.get("accepted", 0),
                "As of before": fmt_ts(city.get("as_of_before")),
                "As of after": fmt_ts(city.get("as_of_after")),
                "Covers to": city.get("covers_to_after") or "-",
                "Why not": " ".join((city.get("error") or "").split())[:80] or "",
            }
        )
    return pd.DataFrame(rows)


def ago(stamp) -> str:
    """`", 3 h ago"`, or nothing when the word already says when.

    `age_of` answers "how old" in a metric card, where "just now" is the whole
    value. In a sentence that reads "recorded ... just now ago", so the phrase is
    built here instead of concatenated at the call site.
    """
    age = age_of(stamp)
    if age in {"just now", "never", "unknown"}:
        return f" ({age})" if age == "just now" else ""
    return f", {age} ago"


def age_of(as_of) -> str:
    """How old a stamp is, in words. An as-of is only meaningful next to how
    long ago it was."""
    if not as_of:
        return "never"
    try:
        stamp = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    minutes = int((datetime.now(UTC) - stamp).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // 1440} days"


def freshness_table(cov) -> pd.DataFrame:
    """One row per city: its own as-of, its own coverage end, and whether that
    still reaches today in that city.

    Built from the stored rows, and from nothing else. What the last *run* of the
    refresh did is a separate question with a separate source: `render_last_run`
    reads it from `GET /refresh/last`. Mixing the two here would let a stale-but-
    fresh-looking table stand in for a refresh that actually failed.

    "Days ahead" is counted against the city's local date, through the same
    `forecast.local_today` the forecast cards use, so the two cannot disagree.
    """
    rows = []
    for city in cov["cities"]:
        stored = cached_get(f"/weather/{city['id']}") or []
        as_of = max((str(row["as_of"]) for row in stored), default="")
        last = max((str(row["forecast_date"]) for row in stored), default="")
        today = forecast.local_today(city["timezone"])
        ahead = (date.fromisoformat(last) - today).days if last else None
        if not stored:
            state = "no stored forecast"
        elif ahead is None or ahead < 0:
            state = "expired — refresh needed"
        elif ahead < 2:
            state = "runs out within a day"
        else:
            state = "covers the week ahead"
        rows.append(
            {
                "City": city["name"],
                "As of": fmt_ts(as_of),
                "Age": age_of(as_of),
                "Covers to": last or "-",
                "Days ahead": "-" if ahead is None else ahead,
                "State": state,
                "Stored days": len(stored),
            }
        )
    return pd.DataFrame(rows)


def render_correction(cov) -> None:
    st.markdown("**Correct a place, fact, or event**")
    city = city_picker(cov, "rec_city")
    entity = st.selectbox("Record type", ["places", "facts", "events"])
    rows = cached_get(f"/{entity}", city=city, limit=200) or []
    if not rows:
        st.warning(f"No {entity} on record for that city.")
        return

    labels = {f"{r.get('name') or r.get('title')} (version {r['revision']})": r for r in rows}
    chosen = labels[st.selectbox("Record", list(labels))]
    field = st.selectbox(
        "Field",
        {
            "places": ["name", "category", "address"],
            "facts": ["title", "summary", "topic"],
            "events": ["title", "category", "venue"],
        }[entity],
    )
    value = st.text_area("New value", value=str(chosen.get(field) or ""))

    if st.button("Submit the correction", type="primary"):
        result = api_send("PATCH", f"/records/{entity}/{chosen['id']}", {field: value})
        if result:
            st.success("Correction received. The updated record will appear shortly.")
            st.session_state["last_patch"] = (entity, chosen["id"], result["message_id"])

    last = st.session_state.get("last_patch")
    if last:
        entity_name, row_id, message_id = last
        st.divider()
        st.markdown("**Correction status**")
        status = api_get(f"/outbox/{message_id}")
        if status:
            st.write(f"Status: {status.get('status', 'pending')}")
        history = api_get(f"/records/{entity_name}/{row_id}/history") or []
        if history:
            st.markdown(f"**Previous versions** — {len(history)}")
            for item in history:
                st.caption(f"Version {item['revision']} · {fmt_ts(item['changed_at'])}")


def render_reenrich(cov) -> None:
    st.markdown("**Refresh recommendation notes**")
    st.caption("Update the written explanation for an activity. The weather score stays the same.")

    city = city_picker(cov, "reenrich_city")
    status = cached_get("/enrichment", city=city) or {"counts": {}}
    counts = status["counts"]
    columns = st.columns(4)
    columns[0].metric("Worded", counts.get("ready", 0))
    columns[1].metric("Queued", counts.get("pending", 0))
    columns[2].metric("Deferred", counts.get("deferred", 0))
    columns[3].metric("Failed", counts.get("failed", 0))
    st.caption(
        "Some scored activities may not have a written note yet. You can include "
        "them in this update."
    )

    first, last = date_bounds(cov)
    left, right = st.columns([2, 3])
    with left:
        whole_city = st.toggle("The whole city", value=False)
        day = None
        if not whole_city:
            day = st.date_input(
                "Day", value=first, min_value=first, max_value=last, key="reenrich_day"
            )
    with right:
        include_deferred = st.checkbox(
            "Include deferred activities",
            value=True,
            help="Also prepare notes for activities that currently show a score only.",
        )

    if st.button("Queue the re-wording", type="primary"):
        result = api_send(
            "POST",
            "/reenrich",
            {
                "city": city,
                "forecast_date": None if whole_city else str(day),
                "include_deferred": include_deferred,
            },
        )
        if result:
            st.success("Update requested. New notes will appear when ready.")
            st.caption("A whole city may take a few minutes. Check the counts above for progress.")


# ------------------------------------------------------ 6. data coverage ----


def page_coverage(cov) -> None:
    """What the system holds, per entity and per city. This tab exists because
    row counts in a banner hide the shape of the data: a single events total
    reads as coverage until you see how the rows are spread and how many of
    them are generated samples."""
    st.caption(
        "Everything the system holds, and how old it is. A question about a date "
        "outside these windows is refused rather than guessed at."
    )

    st.markdown("**By record type**")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "record type": e["entity"],
                    "rows": e["rows"],
                    "cities": e["cities"],
                    "as of": fmt_ts(e["as_of"]),
                    "coverage": (
                        f"{e['first_date']} to {e['last_date']}"
                        if e.get("first_date")
                        else e["kind"]
                    ),
                    "what the range means": e["kind"],
                    "labelled samples": e["samples"],
                }
                for e in cov["entities"]
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "Places and background facts are updated as collections, so they do not "
        "have a start and end date."
    )

    freshness = cov.get("event_freshness") or {}
    current = int(freshness.get("current") or 0)
    expired = int(freshness.get("expired") or 0)
    covered = int(freshness.get("cities_covered") or 0)
    total_cities = len(cov["cities"])
    st.markdown("**Verified event listings**")
    st.caption(
        f"{current} checked listing(s) are still inside their recheck window, across "
        f"{covered} of {total_cities} cities; {expired} have fallen out of it. Each row "
        "records when somebody last opened its own listing page, and stops being "
        "offered as a scheduled event once that reading is older than the recheck "
        "window. Expired rows are kept and counted here rather than deleted: a feed "
        "that has gone out of date and a city nobody ever checked are different "
        "problems, and only a connected refresh fixes the first."
        + (
            f" {int(freshness.get('samples_expired') or 0)} generated sample row(s) have "
            "also aged out; they age on the same rule, and regenerating them with "
            "`make samples` is what moves them."
            if int(freshness.get("samples_expired") or 0)
            else ""
        )
        + (
            f" The oldest current reading was taken {fmt_ts(freshness.get('oldest_check'))}"
            f" and the first one expires {fmt_ts(freshness.get('next_expiry'))}."
            if current
            else ""
        )
    )

    st.markdown("**By city**")
    by_city = pd.DataFrame(cov["by_city"])
    st.dataframe(
        by_city.rename(
            columns={
                "name": "city",
                "sample_events": "of which samples",
                "coastal": "has a coast",
                "verified_events_current": "verified events, current",
                "verified_events_expired": "verified events, expired",
                "coast_name": "coast reference",
                "coast_distance_km": "km to the coast",
            }
        ).drop(columns=["city_id"]),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "`has a coast` gates surfing, swimming, the beach, fishing and a boat ride. "
        "An inland city gets no row for them, rather than a score derived from a "
        "forecast that says nothing about surf. The two columns beside it are what "
        "make that flag checkable: the named point the claim rests on, and how far "
        "the city's forecast point is from it. Rome's is about 25 km inland of the "
        "sea, so a score it carries for a sea activity is capped and says so."
    )

    st.markdown("**The activity catalogue**")
    activities = cached_get("/activities") or []
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "": a["icon"] or "",
                    "activity": a["label"],
                    "where": "indoor" if a["indoor"] else "outdoor",
                    "needs a coast": a["requires_coast"],
                    "scored days": a["scored_days"],
                    "worded": a["worded"],
                    "queued": a["pending"],
                    "deferred": a["deferred"],
                    "best score": a["best_score"],
                    "in the catalogue": a["in_catalogue"],
                }
                for a in activities
            ]
        ),
        hide_index=True,
        width="stretch",
        height=420,
    )
    st.caption("Activities added by visitors use a general outdoor comfort score.")

    st.markdown("**Sources and licences**")
    st.markdown(
        "- **Weather** — Open-Meteo forecast API, CC BY 4.0, no API key\n"
        "- **Places** — Wikidata SPARQL, CC0 (OpenStreetMap via Overpass, ODbL, "
        "when selected at staging time)\n"
        "- **Places map backdrop** — OpenStreetMap contributors, ODbL; "
        "Natural Earth coastline, public domain\n"
        "- **Background facts** — Wikipedia REST summaries, CC BY-SA 4.0: the city "
        "article plus one article per venue, resolved through its Wikidata sitelink\n"
        "- **Events (verified)** — `data/events.seed.jsonl`, hand-verified real "
        "listings, each row carrying its own source URL. 39 rows across all five "
        "cities, still thin and still uneven: London 10, Rome 10, Tel Aviv 9, "
        "Reykjavík 6, Lisbon 4. Each row records when its listing page was last "
        "opened and stops being reported as a current schedule once that reading "
        "is past its recheck window. "
        "These are the only events a default run stores.\n"
        "- **Events (generated samples)** — `data/events.samples.jsonl`, replayed "
        "only in demo mode (`compose.demo.yml`). Titled *Sample: …*, marked "
        "`is_sample` everywhere they appear including in the agent's prompt, and "
        "anchored to a real venue from the places snapshot — their link is a "
        "**venue reference**, not an event listing. Leaving demo mode deletes them "
        "from the database."
    )


# ---------------------------------------------------------- 7. monitoring ----


MONITORING_DASHBOARDS = (
    ("Service health", "http://127.0.0.1:3000/d/aow-service-health"),
    ("Pipeline", "http://127.0.0.1:3000/d/aow-pipeline"),
    ("LLM observability", "http://127.0.0.1:3000/d/aow-llm-observability"),
)


def page_monitoring(cov) -> None:
    st.caption(
        "System and LLM metrics are shown in Grafana. Weather and activity "
        "visualizations are in Dashboard, Forecast, and Suitability."
    )
    st.markdown(
        "Monitoring runs as an optional local service. Start it with "
        "`docker compose -f compose.yml -f compose.observability.yml up -d` "
        "after setting `GRAFANA_ADMIN_PASSWORD` in `.env`. "
        "Without an admin session, dashboards open as a read-only local viewer; "
        "the admin login is for settings."
    )
    st.markdown(
        "\n".join(f"- [Open {label} dashboard ↗]({url})" for label, url in MONITORING_DASHBOARDS)
    )
    st.caption("The monitoring service is available only on this machine by default.")


# ---------------------------------------------------------------- layout ----

PAGES = {
    "dashboard": ("▦  Dashboard", page_dashboard),
    "forecast": ("☀  Forecast", page_forecast),
    "suitability": ("◎  Suitability", page_heatmap),
    "trip-planner": ("◇  Trip planner", page_planner),
    "places-map": ("⌖  Places map", page_places_map),
    "ask-the-agent": ("✦  Ask the agent", page_chat),
    "update-data": ("↻  Update data", page_update),
    "data-coverage": ("▥  Data coverage", page_coverage),
    # Keep Monitoring last: the hover menu in theme.py follows the final nav label.
    "monitoring": ("◉  Monitoring", page_monitoring),
}


def remember_page() -> None:
    page = st.session_state["page"]
    st.query_params["page"] = page
    st.session_state["_last_query_page"] = page


requested_page = st.query_params.get("page", "dashboard")
if requested_page not in PAGES:
    requested_page = "dashboard"
if requested_page != st.session_state.get("_last_query_page"):
    st.session_state["page"] = requested_page
st.session_state["_last_query_page"] = requested_page

with st.container(key="main_nav"):
    selected_page = st.radio(
        "Main navigation",
        list(PAGES),
        format_func=lambda page: PAGES[page][0],
        horizontal=True,
        label_visibility="collapsed",
        key="page",
        on_change=remember_page,
    )
    st.markdown(
        '<div class="aow-monitor-dropdown" role="menu" aria-label="Monitoring dashboards">'
        + "".join(
            f'<a role="menuitem" href="{escape(url, quote=True)}">{escape(label)} ↗</a>'
            for label, url in MONITORING_DASHBOARDS
        )
        + "</div>",
        unsafe_allow_html=True,
    )

cov = coverage()
health = cached_get("/health")
header(cov)

PAGES[selected_page][1](cov)

st.divider()


def stored_state(cov, health) -> tuple:
    """What "anything new?" means here: how many rows of each kind are stored
    and how old each kind is, plus how many accepted writes are still in
    flight. Row counts alone would miss a correction, which replaces a value
    without adding a row, so the as-of stamp goes in too."""
    return (
        tuple(sorted((e["entity"], e["rows"], str(e["as_of"])) for e in cov["entities"])),
        health["outbox"]["pending"],
    )


pending = health["outbox"]["pending"]
state = stored_state(cov, health)

# The button clears a cache and reruns. Almost always the screen it redraws is
# identical, so without a word back the click reads as a dead button. This is
# the pass after that rerun: compare what was on screen when it was pressed
# with what was just read, and say which it was.
was = st.session_state.pop("checked_against", None)
if was is not None:
    if was != state:
        st.toast("New data loaded.")
    elif pending:
        st.toast(f"Nothing new yet. {pending} update(s) still in progress.")
    else:
        st.toast("Checked. Everything on screen is current.")

left, right = st.columns([4, 1])
checked_at = st.session_state.get("checked_at")
left.caption(
    f"Updates in progress: {pending}"
    + (f" · last checked {checked_at.strftime('%H:%M UTC')}" if checked_at else "")
)
if right.button("Check for updates", width="stretch"):
    st.session_state["checked_against"] = state
    st.session_state["checked_at"] = forecast.utc_now()
    st.cache_data.clear()
    st.rerun()
