"""The UI (M9, M10).

Seven tabs across the top, one rule: nothing is rendered without the as-of stamp
of the data behind it. The coverage strip is drawn in the header, from
`GET /coverage`, and every chart and answer sits under it.

The UI holds no business logic and no database credentials. It calls the API,
which is the same API the demo scripts call, which reads through the same
`common.queries` functions the agent uses. So what a reviewer sees on screen
and what the agent says in chat come from one place.

Writes are accepted, not applied: a save or an edit returns 202 and a
message_id, and the page says so rather than pretending the row is already
stored.

Layout note: `st.tabs` executes every tab body on every rerun, not just the
visible one. That is why the read helpers below are cached -- without it, one
click would fire every query in the app.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

import forecast
import pandas as pd
import places_map
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
import theme

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
    except requests.RequestException as exc:
        st.error(f"The API is unreachable: {exc}")
        st.stop()
    if response.status_code == 404:
        return None
    if not response.ok:
        st.error(f"{path} returned {response.status_code}: {response.text[:300]}")
        st.stop()
    return response.json()


def api_send(method: str, path: str, payload):
    try:
        response = requests.request(method, f"{API}{path}", json=payload, timeout=TIMEOUT)
    except requests.RequestException as exc:
        st.error(f"The API is unreachable: {exc}")
        return None
    if not response.ok:
        st.error(f"{path} returned {response.status_code}: {response.text[:300]}")
        return None
    return response.json()


# Cached because every tab body runs on every rerun (see the module docstring).
# The TTLs are short: this is a live view of a pipeline, and a stale number
# here would undercut the whole point of the as-of stamps.
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


def header(cov, health) -> None:
    """The single most important element in the UI: it is what stops a stale
    snapshot from looking like live data."""
    first, last = cov.get("weather_first_date"), cov.get("weather_last_date")
    entities = {e["entity"]: e for e in cov["entities"]}

    def rows_of(name: str) -> str:
        return f"{(entities.get(name) or {}).get('rows', 0):,}"

    def events_chip() -> str:
        """Never a single number. "52 events" reads as coverage; "7 verified +
        45 samples" reads as what it is."""
        row = entities.get("events") or {}
        samples = int(row.get("samples") or 0)
        verified = int(row.get("rows") or 0) - samples
        if samples:
            return f"{verified} verified + {samples} samples"
        return f"{verified} verified"

    st.markdown(
        f"""
        <div class="aow-hero">
          <h1>act-on-weather</h1>
          <p>Five cities, one stored forecast, and a local model that words
             recommendations it is not allowed to decide. Everything on this
             page comes from stored data &mdash; nothing is fetched from the
             internet at runtime.</p>
          {theme.chips([
              ("Forecast covers", f"{first} → {last}"),
              ("Weather as of", fmt_ts(cov.get("weather_as_of"))),
              ("Cities", str(len(cov["cities"]))),
              ("Places", rows_of("places")),
              ("Background facts", rows_of("facts")),
              ("Events", events_chip()),
              ("API", health["status"]),
              ("Database", "up" if health["database"] else "down"),
          ])}
        </div>
        """,
        unsafe_allow_html=True,
    )
    if DEMO_EVENTS:
        st.warning(
            "**Demo mode.** This run also stores 45 **generated sample events** so the "
            "trip planner and the agent can be exercised in all five cities. They are "
            "titled *Sample: …*, marked `is_sample` in the database, and labelled "
            "wherever they appear. Only seven events in this system are real listings, "
            "and all seven are in London. A default run (`docker compose up -d`, "
            "without `compose.demo.yml`) stores those seven and nothing else, and "
            "deletes any sample row left over from a demo run.",
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


# ----------------------------------------------------------- 1. forecast ----


def page_forecast(cov) -> None:
    city = city_picker(cov, "forecast_city")
    rows = cached_get(f"/weather/{city}")
    if not rows:
        st.warning("No stored weather for that city.")
        return

    city_info = next(item for item in cov["cities"] if item["id"] == city)
    latest = forecast.next_row(rows, city_info["timezone"])
    if latest is None:
        last_day = max(row["forecast_date"] for row in rows)
        st.warning(
            f"No current forecast for {city_info['name']}. The stored forecast ended "
            f"{last_day}; refresh while connected."
        )
        return

    frame = pd.DataFrame(rows).sort_values("forecast_date")
    frame["forecast_date"] = pd.to_datetime(frame["forecast_date"])

    columns = st.columns(4)
    columns[0].metric(f"Next high · {latest['forecast_date']}", f"{latest['temp_max_c']:.0f}°C")
    columns[1].metric("Next low", f"{latest['temp_min_c']:.0f}°C")
    columns[2].metric("Rain", f"{latest['precip_mm']:.1f} mm")
    columns[3].metric("Wind", f"{latest['wind_kmh']:.0f} km/h")

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

    st.caption(
        f"Provider: {rows[0]['provider']} · row as of {fmt_ts(rows[0]['as_of'])} "
        f"· revision {rows[0]['revision']}"
    )
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
            ],
            hide_index=True,
            width="stretch",
        )


# -------------------------------------------------------- 2. suitability ----


def page_heatmap(cov) -> None:
    st.caption(
        "Every score here is computed by the rule engine in "
        "`services/common/rules.py` from the stored weather. The local model "
        "writes the sentence under each score; it never decides the score."
    )
    scope = st.radio(
        "Show", ["One city, every activity", "One activity, every city"], horizontal=True
    )

    if scope == "One city, every activity":
        city = city_picker(cov, "heat_city")
        rows = cached_get("/scores", city=city)
        index, columns = "activity_label", "forecast_date"
        coastal = next(c["coastal"] for c in cov["cities"] if c["id"] == city)
        if not coastal:
            st.caption(
                "This city is marked inland, so surfing, swimming, the beach, "
                "fishing and a boat ride are not scored for it at all. A score "
                "for surf, derived from an inland forecast, would be a number "
                "the system cannot stand behind."
            )
    else:
        activities = cached_get("/activities") or []
        labels = {a["label"]: a["activity"] for a in activities}
        chosen = st.selectbox("Activity", list(labels))
        rows = cached_get("/scores", activity=labels[chosen])
        index, columns = "city_id", "forecast_date"

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
            f"{counts['pending']} of {len(rows)} scores are still waiting for the local "
            "model to word them. The scores themselves are already final."
        )
    if counts["deferred"]:
        st.caption(
            f"{counts['deferred']} rows are **deferred**: the rule engine scored them and "
            "they are charted above, but the local model was not asked to word them. "
            "Each day's top-rated activities are worded first, because on CPU the model "
            "is the slow part and the score is the product. Ask about one below, or "
            "re-word a whole day from **Update data**, to pull it into the queue."
        )
    if counts["failed"]:
        st.warning(f"{counts['failed']} rows are marked failed; the reason is in the table.")

    st.markdown("**What the model wrote about these scores**")
    ready = [r for r in rows if r.get("text")]
    if ready:
        st.dataframe(
            pd.DataFrame(ready)[
                ["forecast_date", "city_id", "activity_label", "score", "band", "text", "model"]
            ],
            hide_index=True,
            width="stretch",
            height=260,
        )
    else:
        st.caption("Nothing worded yet.")

    ask_for_activity(cov)


def ask_for_activity(cov) -> None:
    """M2: the catalogue is a default, not the menu."""
    st.divider()
    st.markdown("**Ask about a different activity**")
    st.caption(
        "Anything you type is scored against the stored weather and worded by the "
        "local model. If it is already in the catalogue it is scored by its own "
        "rule and promoted to the front of the wording queue; if it is not, it is "
        "scored against general outdoor comfort and says so. Either way it goes "
        "through the queue like every other record."
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
            st.success(f"Accepted as `{result['message_id']}`. It is in the queue now.")
            st.caption("Refresh in a few seconds; it appears in the table above.")


# --------------------------------------------------------- 3. the agent ----


def page_chat(cov) -> None:
    st.caption(
        "The agent resolves the city, the dates and the coverage window in code, "
        "runs read-only queries, and makes one call to the local model to phrase "
        "the rows it retrieved. A question about a date outside the stored window "
        "is refused without calling the model at all."
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
    with st.expander("What it actually looked at"):
        st.json(
            {
                "city": answer["city"],
                "dates": answer["dates"],
                "intents": answer["intents"],
                "llm_called": answer["llm_called"],
                "rows_used": answer["rows_used"],
            }
        )


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
    if not plan:
        return

    st.markdown(f"### {plan['title']}")
    st.caption(
        f"Built from data as of {fmt_ts(plan.get('as_of'))} · "
        f"coverage {plan['coverage']['first']} to {plan['coverage']['last']}"
    )
    if plan.get("requested_days_outside_coverage"):
        st.warning(
            "No stored weather for: "
            + ", ".join(plan["requested_days_outside_coverage"])
            + ". Those days are left out rather than guessed."
        )

    for day in plan["days"]:
        render_day(day)

    st.divider()
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
            },
        )
        if saved:
            st.success(f"Accepted as `{saved['message_id']}` — id `{saved['id']}`.")
            st.caption(
                "It travels through the queue like every other record, so it "
                "appears below once the consumer has stored it."
            )

    saved_rows = cached_get("/itineraries", city=plan["city"])
    if saved_rows:
        st.markdown("**Saved itineraries**")
        st.dataframe(pd.DataFrame(saved_rows), hide_index=True, width="stretch")


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
            if event["is_sample"]:
                # A sample has no listing to link to, because there is no
                # listing -- the URL is the Wikidata entry for the real venue
                # it was anchored to. Rendering it as the event's own source
                # would be the one misleading thing in an otherwise
                # thoroughly labelled row.
                st.markdown(
                    f"Event: {escape(event['title'])} "
                    f"({event['category']}{venue}) "
                    f'<span class="aow-sample">sample</span> '
                    f"\N{MIDDLE DOT} [venue reference]({event['source_url']})",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f"Event: [{event['title']}]({event['source_url']}) "
                    f"({event['category']}{venue})",
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
        "Pan or zoom to inspect markers. The backdrop is a staged "
        "OpenStreetMap extract — © OpenStreetMap contributors, ODbL — covering "
        "20 km around the city centre: main and secondary streets, rivers, "
        "coastline, water and parks, simplified to about 12 m. Residential streets, "
        "buildings and labels are not in it, and it gives no route directions."
        + ("" if base else " No extract is staged for this city.")
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
    st.caption(
        "Three ways stored data changes. None of them writes to the database "
        "here: every one is accepted, published to the queue, and applied by the "
        "consumer — the same path a fetched record takes."
    )

    refresh_tab, correct_tab, reword_tab = st.tabs(
        [
            "1 · Operator refresh (connected)",
            "2 · Correct a record",
            "3 · Re-word with the model",
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
    something that can attach the ingestor to the egress network, which means
    either the Docker socket inside this container or an unauthenticated write
    endpoint that runs host commands. Both are a worse problem than the one they
    solve, in a container whose entire job is rendering read-only views.

    So this tab shows the two things it can show honestly: how fresh the stored
    forecast actually is, city by city, and the exact command an operator runs.
    Nothing on this page fetches anything -- and it says so, because a page that
    prints a command next to a freshness stamp reads as if it had just run it.
    """
    st.markdown(
        "**Operator refresh.** Run from a shell on the Docker host. Needs "
        "connectivity — this is the one path that cannot work air-gapped, by design."
    )
    st.warning(
        "This page does not fetch. It shows what is stored right now, and the "
        "command that changes it. Nothing here reaches the provider.",
        icon="ℹ",
    )

    st.markdown("#### What is stored right now")
    columns = st.columns(4)
    columns[0].metric("Newest weather as-of", fmt_ts(cov.get("weather_as_of")).replace(" UTC", ""))
    columns[1].metric("Age", age_of(cov.get("weather_as_of")))
    columns[2].metric("Covers from", cov.get("weather_first_date") or "-")
    columns[3].metric("Covers to", cov.get("weather_last_date") or "-")

    st.dataframe(freshness_table(cov), hide_index=True, width="stretch")
    st.caption(
        "Per city, because a refresh that only half worked looks exactly like "
        "this: four cities stamped minutes ago and one still carrying last "
        "week's as-of. The command below reports the same split at the time it "
        "runs, and exits non-zero when any city fails."
    )
    if st.button("Re-read stored data", key="refresh_reread"):
        st.cache_data.clear()
        st.rerun()
    st.caption(
        "Re-reads the database through the API. It does not contact the weather "
        "provider — only the command below does that."
    )

    st.markdown("#### The operator command")
    st.code("docker compose -f compose.tools.yml run --rm refresh", language="bash")
    st.caption(
        "It attaches **only the ingestor** to the egress network, fetches the "
        "forecast into the same outbox every other record uses, then closes that "
        "window again from a trap — on success, on a failed fetch and on Ctrl-C "
        "alike — and asserts it closed before reporting. It prints per-city "
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

    st.info(
        "Offline, this is deliberately the one thing that does not work. The system "
        "keeps answering from what it holds and refuses dates outside the stored "
        "window rather than guessing at them."
    )


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

    Built from the stored rows, not from a refresh log -- there is no such log,
    and inventing one would let this page claim a refresh that never landed.
    "Days ahead" is counted against the city's local date, the same date
    `forecast.next_row` uses for the forecast card, so the two cannot disagree.
    """
    now = forecast.utc_now()
    rows = []
    for city in cov["cities"]:
        stored = cached_get(f"/weather/{city['id']}") or []
        as_of = max((str(row["as_of"]) for row in stored), default="")
        last = max((str(row["forecast_date"]) for row in stored), default="")
        today = now.astimezone(ZoneInfo(city["timezone"])).date()
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
    st.markdown("**Fix a stored record.** Works air-gapped.")
    city = city_picker(cov, "rec_city")
    entity = st.selectbox("Record type", ["places", "facts", "events"])
    rows = cached_get(f"/{entity}", city=city, limit=200) or []
    if not rows:
        st.warning(f"No {entity} on record for that city.")
        return

    labels = {f"{r.get('name') or r.get('title')} (rev {r['revision']})": r for r in rows}
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
            st.success(f"Accepted as `{result['message_id']}`.")
            st.session_state["last_patch"] = (entity, chosen["id"], result["message_id"])

    last = st.session_state.get("last_patch")
    if last:
        entity_name, row_id, message_id = last
        st.divider()
        st.markdown("**Where that edit got to**")
        status = api_get(f"/outbox/{message_id}")
        if status:
            st.json(status)
        history = api_get(f"/records/{entity_name}/{row_id}/history") or []
        if history:
            st.markdown(f"**History for `{row_id}`** — {len(history)} revision(s)")
            for item in history:
                st.caption(f"revision {item['revision']} at {fmt_ts(item['changed_at'])}")


def render_reenrich(cov) -> None:
    st.markdown("**Ask the local model to re-word stored recommendations.** Works air-gapped.")
    st.caption(
        "Scores are untouched — they are the rule engine's output, and only a "
        "weather refresh changes them. This resets the wording, and the enricher "
        "picks the rows up on its next poll."
    )

    city = city_picker(cov, "reenrich_city")
    status = cached_get("/enrichment", city=city) or {"counts": {}}
    counts = status["counts"]
    columns = st.columns(4)
    columns[0].metric("Worded", counts.get("ready", 0))
    columns[1].metric("Queued", counts.get("pending", 0))
    columns[2].metric("Deferred", counts.get("deferred", 0))
    columns[3].metric("Failed", counts.get("failed", 0))
    st.caption(
        f"The consumer sends the top {status.get('top_n_worded_per_day', '?')} "
        "activities per day to the model and defers the rest. Deferred rows are "
        "scored and charted; they were simply never queued for prose."
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
            help="Pulls in the activities the consumer ranked out of the wording queue.",
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
            st.success(f"Accepted as `{result['message_id']}`. It is in the queue now.")
            st.caption(
                "On CPU the model takes a few seconds per row, so a whole city is a "
                "few minutes. Watch the counters above."
            )


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
        "`not date-scoped` is not a gap: a museum is not valid between two dates. "
        "Those rows are stamped with the as-of of the fetch that collected them."
    )

    st.markdown("**By city**")
    by_city = pd.DataFrame(cov["by_city"])
    st.dataframe(
        by_city.rename(
            columns={
                "name": "city",
                "sample_events": "of which samples",
                "coastal": "has a coast",
            }
        ).drop(columns=["city_id"]),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "`has a coast` gates surfing, swimming, the beach, fishing and a boat ride. "
        "An inland city gets no row for them, rather than a score derived from a "
        "forecast that says nothing about surf."
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
    st.caption(
        "Rows with `in the catalogue` false were typed in by a user on the "
        "Suitability tab and scored against general outdoor comfort, not against a "
        "rule tuned for them. The answer says so wherever they appear."
    )

    st.markdown("**Sources and licences**")
    st.markdown(
        "- **Weather** — Open-Meteo forecast API, CC BY 4.0, no API key\n"
        "- **Places** — Wikidata SPARQL, CC0 (OpenStreetMap via Overpass, ODbL, "
        "when selected at staging time)\n"
        "- **Places map backdrop** — a 20 km OpenStreetMap extract per city, "
        "© OpenStreetMap contributors, ODbL, staged into `data/map/` and read "
        "from this image; plus Natural Earth 1:10m coastline, public domain\n"
        "- **Background facts** — Wikipedia REST summaries, CC BY-SA 4.0: the city "
        "article plus one article per venue, resolved through its Wikidata sitelink\n"
        "- **Events (verified)** — `data/events.seed.jsonl`, hand-verified real "
        "listings, each row carrying its own source URL. Seven rows, all in London. "
        "These are the only events a default run stores.\n"
        "- **Events (generated samples)** — `data/events.samples.jsonl`, replayed "
        "only in demo mode (`compose.demo.yml`). Titled *Sample: …*, marked "
        "`is_sample` everywhere they appear including in the agent's prompt, and "
        "anchored to a real venue from the places snapshot — their link is a "
        "**venue reference**, not an event listing. Leaving demo mode deletes them "
        "from the database."
    )


# ---------------------------------------------------------------- layout ----

TABS = {
    "\N{SUN BEHIND CLOUD}️  Forecast": page_forecast,
    "\N{DIRECT HIT}  Suitability": page_heatmap,
    "\N{COMPASS}  Trip planner": page_planner,
    "\N{WORLD MAP}  Places map": page_places_map,
    "\N{SPEECH BALLOON}  Ask the agent": page_chat,
    "\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS}  Update data": page_update,
    "\N{BAR CHART}  Data coverage": page_coverage,
}

cov = coverage()
health = cached_get("/health")
header(cov, health)

for tab, render in zip(st.tabs(list(TABS)), TABS.values(), strict=True):
    with tab:
        render(cov)

st.divider()
left, right = st.columns([4, 1])
left.caption(
    f"Outbox: {health['outbox']['pending']} pending of {health['outbox']['total']} accepted "
    "· every write is accepted to a durable outbox before it is answered, "
    "and applied by the consumer after the queue delivers it."
)
if right.button("Reload from the API", width="stretch"):
    st.cache_data.clear()
    st.rerun()
