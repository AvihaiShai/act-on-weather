"""The visual system for the Streamlit interface.

Everything here is a stylesheet written into the page with one `st.markdown`
call. There is no CSS file to serve, no webfont, no icon set and no CDN --
which matters more than it sounds, because the runtime rule for this project
is zero network calls, and a Google Fonts `@import` would quietly break it.
Icons are unicode glyphs from data/activities.yml, rendered by whatever the
operating system already has.

Two practical notes on Streamlit, because they explain the odd selectors:

  * Streamlit's DOM is generated, so the stable hooks are its `data-testid`
    attributes, not class names. They are pinned to streamlit==1.64.0; the
    version is pinned in services/ui/requirements.txt for exactly this reason,
    and a version bump means re-checking this file.

Nothing here carries information. If the stylesheet fails to apply, every
number, timestamp and label on the page is still there in Streamlit's default
skin -- the design is a layer over the data, never a substitute for it.
"""

from __future__ import annotations

import streamlit as st

# One place for every colour in the app. The band colours are shared with the
# heatmap and the day cards so a "good" day is the same green everywhere.
BAND_COLOURS = {"good": "#1E874B", "fair": "#B77900", "poor": "#C0362C"}
BAND_TINTS = {
    "good": "rgba(30, 135, 75, 0.14)",
    "fair": "rgba(183, 121, 0, 0.14)",
    "poor": "rgba(192, 54, 44, 0.14)",
}

CSS = """
<style>
:root {
  --glass-bg: #FFFFFF;
  --glass-bg-strong: #FFFFFF;
  --glass-border: #D9E0DF;
  --glass-shadow: 0 3px 14px rgba(24, 45, 49, 0.045);
  --glass-shadow-lg: 0 8px 28px rgba(24, 45, 49, 0.075);
  --ink: #183136;
  --ink-soft: #52696B;
  --accent: #126B70;
  --radius: 12px;
}

[data-testid="stAppViewContainer"] {
  background: #F5F7F4;
  color: var(--ink);
}

[data-testid="stHeader"] { background: transparent; }
[data-testid="stToolbar"] { right: 0.75rem; }

/* Streamlit keeps a wide gutter that wastes a laptop screen on a heatmap. */
.block-container { padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1320px; }
p, li { line-height: 1.55; }
[data-testid="stCaptionContainer"] { color: var(--ink-soft); }
h1, h2, h3 { color: var(--ink); letter-spacing: -0.025em; }

/* ---------------------------------------------------------------- panels -- */
/* st.container(border=True) and st.expander both land on this wrapper. */
[data-testid="stVerticalBlockBorderWrapper"] {
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  box-shadow: var(--glass-shadow);
}

[data-testid="stExpander"] details {
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  border-radius: 14px;
  box-shadow: var(--glass-shadow);
  overflow: hidden;
}
[data-testid="stExpander"] summary { font-weight: 600; color: var(--ink); }

/* Main navigation is a keyed Streamlit container, so nested radios keep their
   native styling. The selected page is also reflected in the URL. */
.st-key-main_nav {
  position: sticky;
  top: 2.4rem;
  z-index: 50;
  background: #FFFFFF;
  border: 1px solid var(--glass-border);
  border-radius: 12px;
  box-shadow: var(--glass-shadow);
  padding: 0.45rem 0.65rem;
  margin-bottom: 1.25rem;
}
.st-key-main_nav [role="radiogroup"] { gap: 0.25rem; flex-wrap: wrap; }
.st-key-main_nav [role="radiogroup"] label {
  border-radius: 8px;
  padding: 0.5rem 0.7rem;
  color: var(--ink-soft);
  font-weight: 650;
  transition: background 140ms ease, color 140ms ease;
}
.st-key-main_nav [role="radiogroup"] label:hover { background: #EDF4F2; color: var(--ink); }
.st-key-main_nav [role="radiogroup"] label:has(input:checked) {
  background: #E5F0EE;
  color: var(--accent);
}
.st-key-main_nav [role="radiogroup"] label p {
  font-size: 1rem;
  line-height: 1.45;
  white-space: nowrap;
}
.st-key-main_nav [role="radiogroup"] label > div > div:first-child { display: none; }
.st-key-main_nav [role="radiogroup"] label p::first-letter { font-size: 1.15rem; }
.stTabs [data-baseweb="tab-list"] { gap: 0.75rem; border-bottom: 1px solid var(--glass-border); }
.stTabs [data-baseweb="tab-list"] button { font-size: 0.95rem; font-weight: 600; color: var(--ink-soft); }
.stTabs [data-baseweb="tab-list"] button[aria-selected="true"] { color: var(--accent); }
.stTabs [data-baseweb="tab-panel"] { padding-top: 1.2rem; }

/* --------------------------------------------------------------- widgets -- */
[data-testid="stTextInput"] input,
[data-testid="stTextArea"] textarea,
[data-testid="stDateInput"] input,
[data-baseweb="select"] > div {
  background: var(--glass-bg-strong) !important;
  border: 1px solid var(--glass-border) !important;
  border-radius: 8px !important;
  box-shadow: none;
}

.stButton > button, .stDownloadButton > button, [data-testid="stFormSubmitButton"] > button {
  border-radius: 8px;
  border: 1px solid var(--glass-border);
  background: var(--glass-bg-strong);
  color: var(--ink);
  font-weight: 600;
  box-shadow: var(--glass-shadow);
  transition: transform 120ms ease, box-shadow 120ms ease;
}
.stButton > button:hover, [data-testid="stFormSubmitButton"] > button:hover {
  box-shadow: var(--glass-shadow-lg);
  border-color: var(--accent);
  color: var(--accent);
}
.stButton > button[kind="primary"], [data-testid="stFormSubmitButton"] > button[kind="primary"] {
  background: var(--accent);
  color: #fff;
  border: none;
}
.stButton > button[kind="primary"]:hover { color: #fff; }

[data-testid="stMetric"] {
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  border-radius: 10px;
  padding: 1rem 1.1rem;
  box-shadow: var(--glass-shadow);
}
[data-testid="stMetricLabel"] { color: var(--ink-soft); }

[data-testid="stDataFrame"], [data-testid="stTable"] {
  border-radius: 14px;
  overflow: hidden;
  border: 1px solid var(--glass-border);
  box-shadow: var(--glass-shadow);
}

[data-testid="stAlert"] {
  border-radius: 10px;
  border: 1px solid var(--glass-border);
}

/* Plotly draws its own white card; transparent backgrounds are set on the
   figures too, this kills the wrapper. */
[data-testid="stPlotlyChart"] {
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  box-shadow: var(--glass-shadow);
  padding: 0.6rem;
}

/* -------------------------------------------------------- app furniture -- */
.aow-hero {
  background: #E8F1ED;
  border: 1px solid var(--glass-border);
  border-radius: 12px;
  padding: 1.6rem 1.8rem;
  margin-bottom: 1.45rem;
}
.aow-hero h1 {
  margin: 0;
  font-size: clamp(2rem, 3vw, 2.6rem);
  letter-spacing: -0.045em;
  color: var(--ink);
}
.aow-hero p { margin: 0.45rem 0 0; color: var(--ink-soft); font-size: 1rem; max-width: 56rem; }

.aow-chips { display: flex; flex-wrap: wrap; gap: 0.45rem; margin-top: 0.85rem; }
.aow-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  background: #FFFFFF;
  border: 1px solid #CFDBD6;
  border-radius: 6px;
  padding: 0.35rem 0.65rem;
  font-size: 0.83rem;
  font-weight: 600;
  color: var(--ink-soft);
  white-space: nowrap;
}
.aow-chip b { color: var(--ink); font-weight: 700; }
.aow-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }

.aow-day-head {
  display: flex;
  align-items: baseline;
  gap: 0.6rem;
  flex-wrap: wrap;
}
.aow-day-date { font-weight: 700; color: var(--ink-soft); font-size: 0.85rem;
                text-transform: uppercase; letter-spacing: 0.06em; }
.aow-day-act { font-size: 1.22rem; font-weight: 700; color: var(--ink); }
.aow-score {
  border-radius: 999px;
  padding: 0.14rem 0.6rem;
  font-size: 0.78rem;
  font-weight: 700;
}
.aow-alt {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  background: rgba(255,255,255,0.6);
  border: 1px solid rgba(255,255,255,0.85);
  border-radius: 999px;
  padding: 0.2rem 0.65rem;
  margin: 0.15rem 0.25rem 0.15rem 0;
  font-size: 0.82rem;
  color: var(--ink-soft);
}
.aow-sample {
  background: rgba(183,121,0,0.16);
  color: #8A5B00;
  border-radius: 6px;
  padding: 0.05rem 0.4rem;
  font-size: 0.72rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
</style>
"""


def apply() -> None:
    """Write the stylesheet into the page. Called once, at the top of app.py."""
    st.markdown(CSS, unsafe_allow_html=True)


def transparent(figure, height: int):
    """Match a Plotly figure to the page surface instead of its default card."""
    # Plotly widens a margin to fit tick labels on its own, but not to fit an
    # axis *title*, so with margins this tight the right-hand title on the
    # forecast chart's rain axis was clipped. automargin covers the titles;
    # the wider right margin is the fallback if it ever stops doing so.
    titled_right = any(axis.side == "right" and axis.title.text for axis in figure.select_yaxes())
    right = 60 if titled_right else 10
    figure.update_layout(
        height=height,
        margin={"l": 10, "r": right, "t": 30, "b": 10},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": "#183136"},
        legend={"orientation": "h", "y": 1.14, "bgcolor": "rgba(0,0,0,0)"},
    )
    figure.update_xaxes(gridcolor="rgba(20,26,42,0.08)", zerolinecolor="rgba(20,26,42,0.12)")
    figure.update_yaxes(
        gridcolor="rgba(20,26,42,0.08)",
        zerolinecolor="rgba(20,26,42,0.12)",
        automargin=True,
    )
    return figure


def chips(items: list[tuple[str, str]]) -> str:
    """A row of small glass pills: (label, value). Used for the coverage strip."""
    parts = "".join(
        f'<span class="aow-chip">{label} <b>{value}</b></span>' for label, value in items
    )
    return f'<div class="aow-chips">{parts}</div>'


def score_pill(score, band: str) -> str:
    if score is None:
        return ""
    colour = BAND_COLOURS.get(band, "#55607A")
    tint = BAND_TINTS.get(band, "rgba(85,96,122,0.14)")
    return (
        f'<span class="aow-score" style="background:{tint};color:{colour}">'
        f"{score}/100 · {band}</span>"
    )
