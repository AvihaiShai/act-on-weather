"""The UI (M9, M10).

Five pages, one rule: nothing is rendered without the as-of stamp of the data
behind it. The coverage banner is drawn on every page, from `GET /coverage`,
and every chart and answer sits under it.

The UI holds no business logic and no database credentials. It calls the API,
which is the same API the demo scripts call, which reads through the same
`common.queries` functions the agent uses. So what a reviewer sees on screen
and what the agent says in chat come from one place.

Writes are accepted, not applied: a save or an edit returns 202 and a
message_id, and the page says so rather than pretending the row is already
stored.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

API = os.environ.get("API_BASE", "http://api:8000")
TIMEOUT = float(os.environ.get("API_TIMEOUT_S", "180"))

BAND_COLOURS = {"good": "#1a7f37", "fair": "#bf8700", "poor": "#b42318"}

st.set_page_config(page_title="act-on-weather", layout="wide", page_icon="*")


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


@st.cache_data(ttl=20)
def coverage():
    return api_get("/coverage")


@st.cache_data(ttl=60)
def cities():
    return api_get("/cities")


# ---------------------------------------------------------------- banner ----


def fmt_ts(value) -> str:
    if not value:
        return "never"
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except ValueError:
        return str(value)


def banner(cov) -> None:
    """Drawn on every page. The single most important element in the UI: it is
    what stops a stale snapshot from looking like live data."""
    first, last = cov.get("weather_first_date"), cov.get("weather_last_date")
    st.caption(
        f"**Stored data** · weather as of {fmt_ts(cov.get('weather_as_of'))} · "
        f"forecast covers **{first} to {last}** · "
        "nothing outside that window is answered, and nothing here is fetched live."
    )
    with st.expander("What exactly is on record"):
        rows = []
        for entity in cov["entities"]:
            rows.append(
                {
                    "data": entity["entity"],
                    "rows": entity["rows"],
                    "as of": fmt_ts(entity["as_of"]),
                    "covers": (
                        f"{entity['first_date']} to {entity['last_date']}"
                        if entity.get("first_date")
                        else "-"
                    ),
                }
            )
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.caption(
            "Sources: Open-Meteo (weather, CC BY 4.0) · Wikidata (places, CC0; "
            "OpenStreetMap when selected, ODbL) · Wikipedia (background, CC BY-SA 4.0) · venue listings "
            "(events, each row carries its own source URL). Rows marked *sample* are "
            "labelled as such wherever they appear."
        )


def city_picker(cov, key: str) -> str:
    options = {c["name"]: c["id"] for c in cov["cities"]}
    name = st.selectbox("City", list(options), key=key)
    return options[name]


# ----------------------------------------------------------- 1. forecast ----


def page_forecast(cov) -> None:
    st.subheader("Forecast")
    city = city_picker(cov, "forecast_city")
    rows = api_get(f"/weather/{city}")
    if not rows:
        st.warning("No stored weather for that city.")
        return

    frame = pd.DataFrame(rows)
    frame["forecast_date"] = pd.to_datetime(frame["forecast_date"])

    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=frame["forecast_date"],
            y=frame["temp_max_c"],
            name="High (C)",
            mode="lines+markers",
            line={"color": "#b42318"},
        )
    )
    figure.add_trace(
        go.Scatter(
            x=frame["forecast_date"],
            y=frame["temp_min_c"],
            name="Low (C)",
            mode="lines+markers",
            line={"color": "#1f6feb"},
        )
    )
    figure.add_trace(
        go.Bar(
            x=frame["forecast_date"],
            y=frame["precip_mm"],
            name="Rain (mm)",
            marker_color="#7d8590",
            opacity=0.55,
            yaxis="y2",
        )
    )
    figure.update_layout(
        height=420,
        margin={"l": 10, "r": 10, "t": 30, "b": 10},
        yaxis={"title": "Temperature (C)"},
        yaxis2={
            "title": "Rain (mm)",
            "overlaying": "y",
            "side": "right",
            "showgrid": False,
            "rangemode": "tozero",
        },
        legend={"orientation": "h", "y": 1.12},
    )
    st.plotly_chart(figure, use_container_width=True)

    st.caption(
        f"Provider: {rows[0]['provider']} · row as of {fmt_ts(rows[0]['as_of'])} · "
        f"revision {rows[0]['revision']}"
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
            use_container_width=True,
        )


# ------------------------------------------------------------ 2. heatmap ----


def page_heatmap(cov) -> None:
    st.subheader("Suitability")
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
        rows = api_get("/scores", city=city)
        index, columns = "activity_label", "forecast_date"
    else:
        rows = api_get("/scores")
        activities = sorted({r["activity_label"] for r in rows})
        label = st.selectbox("Activity", activities)
        rows = [r for r in rows if r["activity_label"] == label]
        index, columns = "city_id", "forecast_date"

    if not rows:
        st.warning("No scores on record for that selection.")
        return

    frame = pd.DataFrame(rows)
    pivot = frame.pivot_table(index=index, columns=columns, values="score", aggfunc="max")
    figure = px.imshow(
        pivot,
        color_continuous_scale=[(0, "#b42318"), (0.45, "#e8b931"), (1, "#1a7f37")],
        zmin=0,
        zmax=100,
        aspect="auto",
        text_auto=True,
        labels={"color": "score"},
    )
    figure.update_layout(height=60 + 36 * len(pivot), margin={"l": 10, "r": 10, "t": 20, "b": 10})
    st.plotly_chart(figure, use_container_width=True)

    pending = sum(1 for r in rows if r["status"] == "pending")
    failed = sum(1 for r in rows if r["status"] == "failed")
    if pending:
        st.info(
            f"{pending} of {len(rows)} scores are still waiting for the local model to "
            "word them. The scores themselves are already final."
        )
    if failed:
        st.warning(f"{failed} rows are marked failed; hover the table below for the reason.")

    st.markdown("**What the model wrote about these scores**")
    ready = [r for r in rows if r.get("text")]
    if ready:
        st.dataframe(
            pd.DataFrame(ready)[
                ["forecast_date", "city_id", "activity_label", "score", "band", "text", "model"]
            ],
            hide_index=True,
            use_container_width=True,
            height=260,
        )
    else:
        st.caption("Nothing worded yet.")

    ask_for_activity(cov)


def ask_for_activity(cov) -> None:
    """M2: the five scored activities are defaults, not the menu."""
    st.divider()
    st.markdown("**Ask about a different activity**")
    st.caption(
        "Scored against the stored weather and worded by the local model. "
        "It goes through the queue like every other record, so it is accepted "
        "now and appears once the consumer and the enricher have processed it."
    )
    left, middle, right = st.columns([2, 2, 3])
    with left:
        city = city_picker(cov, "req_city")
    with middle:
        first = date.fromisoformat(cov["weather_first_date"])
        last = date.fromisoformat(cov["weather_last_date"])
        day = st.date_input("Date", value=first, min_value=first, max_value=last)
    with right:
        activity = st.text_input("Activity", placeholder="surfing, rock climbing, a picnic...")

    if st.button("Ask", type="primary", disabled=not activity):
        result = api_send(
            "POST",
            "/recommendations",
            {
                "city": city,
                "forecast_date": str(day),
                "activity": activity,
            },
        )
        if result:
            st.success(f"Accepted as `{result['message_id']}`. It is in the queue now.")
            st.caption("Refresh in a few seconds; it appears in the table above.")


# --------------------------------------------------------------- 3. chat ----


def page_chat(cov) -> None:
    st.subheader("Ask the agent")
    st.caption(
        "The agent resolves the city, the dates and the coverage window in code, "
        "runs read-only queries, and makes one call to the local model to phrase "
        "the rows it retrieved. A question about a date outside the stored window "
        "is refused without calling the model at all."
    )
    for example in (
        "What is the weather tomorrow in Rome?",
        "What activities can I do with my wife this week in London? "
        "We like concerts, shopping and fine dining.",
    ):
        if st.button(example, key=f"eg_{hash(example)}"):
            st.session_state["question"] = example

    question = st.text_input(
        "Question", key="question", placeholder="What is the weather tomorrow in Rome?"
    )
    if st.button("Ask the agent", type="primary", disabled=not question):
        with st.spinner("Reading stored data and phrasing an answer..."):
            answer = api_send("POST", "/agent/ask", {"question": question})
        if answer:
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


# ------------------------------------------------------------ 4. planner ----


def page_planner(cov) -> None:
    st.subheader("Trip planner")
    city = city_picker(cov, "plan_city")
    first = date.fromisoformat(cov["weather_first_date"])
    last = date.fromisoformat(cov["weather_last_date"])

    left, right = st.columns(2)
    with left:
        start = st.date_input("From", value=first, min_value=first, max_value=last)
    with right:
        end = st.date_input(
            "To", value=min(first + timedelta(days=2), last), min_value=first, max_value=last
        )
    interests = st.multiselect(
        "Interests",
        [
            "concerts",
            "shopping",
            "fine_dining",
            "dining",
            "history",
            "museums",
            "outdoors",
            "sports",
        ],
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
        band = day.get("activity_band") or "poor"
        with st.container(border=True):
            st.markdown(
                f"**{day['date']}** — :{'green' if band == 'good' else 'orange' if band == 'fair' else 'red'}"
                f"[{day.get('activity') or 'no scored activity'}]"
                + (
                    f" ({day['activity_score']}/100)"
                    if day.get("activity_score") is not None
                    else ""
                )
            )
            if day.get("why"):
                st.caption(day["why"])
            if day["places"]:
                st.markdown(
                    "Places on record: "
                    + ", ".join(
                        f"[{p['name']}]({p['source_url']})"
                        + (" *(sample)*" if p["is_sample"] else "")
                        for p in day["places"]
                    )
                )
            else:
                st.caption("No places on record for those interests in this city.")
            if day["events"]:
                for event in day["events"]:
                    sample = " *(sample data)*" if event["is_sample"] else ""
                    st.markdown(
                        f"Event: [{event['title']}]({event['source_url']}) "
                        f"({event['category']}{', ' + event['venue'] if event['venue'] else ''})"
                        + sample
                    )

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

    saved_rows = api_get("/itineraries", city=plan["city"])
    if saved_rows:
        st.markdown("**Saved itineraries**")
        st.dataframe(pd.DataFrame(saved_rows), hide_index=True, use_container_width=True)


# --------------------------------------------------------------- 5. edit ----


def page_records(cov) -> None:
    st.subheader("Correct a stored record")
    st.caption(
        "M12. An edit is not written here: it is accepted, published to the queue, "
        "and applied by the consumer -- the same path a fetched record takes. The "
        "consumer bumps the revision and files the before-and-after into "
        "`record_history`."
    )
    city = city_picker(cov, "rec_city")
    entity = st.selectbox("Record type", ["places", "facts", "events"])
    rows = api_get(f"/{entity}", city=city, limit=100) or []
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


# ---------------------------------------------------------------- layout ----

PAGES = {
    "Forecast": page_forecast,
    "Suitability": page_heatmap,
    "Ask the agent": page_chat,
    "Trip planner": page_planner,
    "Correct a record": page_records,
}

st.title("act-on-weather")
cov = coverage()
banner(cov)

choice = st.sidebar.radio("Pages", list(PAGES))
st.sidebar.divider()
health = api_get("/health")
st.sidebar.caption(
    f"API {health['status']} · database {'up' if health['database'] else 'down'} · "
    f"outbox {health['outbox']['pending']} pending of {health['outbox']['total']}"
)
st.sidebar.caption(
    "Everything on screen comes from stored data. Nothing is fetched "
    "from the internet at runtime."
)
if st.sidebar.button("Refresh from the API"):
    st.cache_data.clear()
    st.rerun()

PAGES[choice](cov)
