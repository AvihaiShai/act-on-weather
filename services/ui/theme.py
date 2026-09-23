"""The look: frosted-glass panels over a soft gradient.

Everything here is a stylesheet written into the page with one `st.markdown`
call. There is no CSS file to serve, no webfont, no icon set and no CDN --
which matters more than it sounds, because the runtime rule for this project
is zero network calls, and a Google Fonts `@import` would quietly break it.
Icons are unicode glyphs from data/activities.yml, rendered by whatever the
operating system already has.

Two practical notes on Streamlit, because they explain the odd selectors:

  * `backdrop-filter` only frosts what is *behind* an element, so the gradient
    has to sit on a fixed, painted layer (`stAppViewContainer`) rather than on
    `body`, which Streamlit covers with its own opaque background.
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
  --glass-bg: rgba(255, 255, 255, 0.58);
  --glass-bg-strong: rgba(255, 255, 255, 0.78);
  --glass-border: rgba(255, 255, 255, 0.85);
  --glass-shadow: 0 8px 32px rgba(18, 32, 68, 0.10);
  --glass-shadow-lg: 0 16px 48px rgba(18, 32, 68, 0.16);
  --ink: #141A2A;
  --ink-soft: #55607A;
  --accent: #3B6FE0;
  --radius: 18px;
}

/* The painted layer the frosting has something to blur. Fixed, so scrolling
   a long heatmap does not drag the gradient with it. */
[data-testid="stAppViewContainer"] {
  background:
    radial-gradient(1200px 620px at 12% -8%, #DCE7FF 0%, rgba(220,231,255,0) 60%),
    radial-gradient(1000px 560px at 92% 4%, #E4DCFF 0%, rgba(228,220,255,0) 58%),
    radial-gradient(900px 620px at 52% 108%, #D6F0EC 0%, rgba(214,240,236,0) 62%),
    linear-gradient(168deg, #EEF2FB 0%, #E8ECF8 55%, #EDEAF8 100%);
  background-attachment: fixed;
}

[data-testid="stHeader"] { background: transparent; }
[data-testid="stToolbar"] { right: 0.75rem; }

/* Streamlit keeps a wide gutter that wastes a laptop screen on a heatmap. */
.block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1400px; }

/* ---------------------------------------------------------------- panels -- */
/* st.container(border=True) and st.expander both land on this wrapper. */
[data-testid="stVerticalBlockBorderWrapper"] {
  background: var(--glass-bg);
  -webkit-backdrop-filter: blur(22px) saturate(175%);
  backdrop-filter: blur(22px) saturate(175%);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  box-shadow: var(--glass-shadow);
}

[data-testid="stExpander"] details {
  background: var(--glass-bg);
  -webkit-backdrop-filter: blur(18px) saturate(170%);
  backdrop-filter: blur(18px) saturate(170%);
  border: 1px solid var(--glass-border);
  border-radius: 14px;
  box-shadow: var(--glass-shadow);
  overflow: hidden;
}
[data-testid="stExpander"] summary { font-weight: 600; color: var(--ink); }

/* ------------------------------------------------------------------ tabs -- */
/* The top navigation. It replaced a sidebar radio, which hid most
   pages behind a control people did not look at. */
.stTabs [data-baseweb="tab-list"] {
  gap: 6px;
  background: var(--glass-bg);
  -webkit-backdrop-filter: blur(22px) saturate(175%);
  backdrop-filter: blur(22px) saturate(175%);
  border: 1px solid var(--glass-border);
  border-radius: 999px;
  padding: 6px;
  box-shadow: var(--glass-shadow);
  position: sticky;
  top: 2.6rem;
  z-index: 50;
}
.stTabs [data-baseweb="tab-list"] button {
  border-radius: 999px;
  padding: 0.5rem 1.05rem;
  color: var(--ink-soft);
  font-weight: 600;
  background: transparent;
  transition: background 140ms ease, color 140ms ease;
}
.stTabs [data-baseweb="tab-list"] button:hover {
  background: rgba(255,255,255,0.65);
  color: var(--ink);
}
.stTabs [data-baseweb="tab-list"] button[aria-selected="true"] {
  background: var(--glass-bg-strong);
  color: var(--accent);
  box-shadow: 0 2px 10px rgba(18,32,68,0.10);
}
/* Streamlit's sliding underline reads as a second, contradictory selection
   indicator once the selected tab is a filled pill. */
.stTabs [data-baseweb="tab-highlight"],
.stTabs [data-baseweb="tab-border"] { display: none; }
.stTabs [data-baseweb="tab-panel"] { padding-top: 1.4rem; }

/* --------------------------------------------------------------- widgets -- */
[data-testid="stTextInput"] input,
[data-testid="stTextArea"] textarea,
[data-testid="stDateInput"] input,
[data-baseweb="select"] > div {
  background: var(--glass-bg-strong) !important;
  border: 1px solid rgba(255,255,255,0.9) !important;
  border-radius: 12px !important;
  box-shadow: inset 0 1px 2px rgba(18,32,68,0.05);
}

.stButton > button, .stDownloadButton > button, [data-testid="stFormSubmitButton"] > button {
  border-radius: 999px;
  border: 1px solid rgba(255,255,255,0.9);
  background: var(--glass-bg-strong);
  color: var(--ink);
  font-weight: 600;
  box-shadow: var(--glass-shadow);
  transition: transform 120ms ease, box-shadow 120ms ease;
}
.stButton > button:hover, [data-testid="stFormSubmitButton"] > button:hover {
  transform: translateY(-1px);
  box-shadow: var(--glass-shadow-lg);
  border-color: var(--accent);
  color: var(--accent);
}
.stButton > button[kind="primary"], [data-testid="stFormSubmitButton"] > button[kind="primary"] {
  background: linear-gradient(135deg, #4C7DF0 0%, #6E5BE6 100%);
  color: #fff;
  border: none;
}
.stButton > button[kind="primary"]:hover { color: #fff; }

[data-testid="stMetric"] {
  background: var(--glass-bg);
  -webkit-backdrop-filter: blur(18px) saturate(170%);
  backdrop-filter: blur(18px) saturate(170%);
  border: 1px solid var(--glass-border);
  border-radius: 16px;
  padding: 0.85rem 1rem;
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
  border-radius: 14px;
  -webkit-backdrop-filter: blur(14px);
  backdrop-filter: blur(14px);
  border: 1px solid var(--glass-border);
}

/* Plotly draws its own white card; transparent backgrounds are set on the
   figures too, this kills the wrapper. */
[data-testid="stPlotlyChart"] {
  background: var(--glass-bg);
  -webkit-backdrop-filter: blur(20px) saturate(175%);
  backdrop-filter: blur(20px) saturate(175%);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  box-shadow: var(--glass-shadow);
  padding: 0.6rem;
}

/* -------------------------------------------------------- app furniture -- */
.aow-hero {
  background: var(--glass-bg);
  -webkit-backdrop-filter: blur(24px) saturate(180%);
  backdrop-filter: blur(24px) saturate(180%);
  border: 1px solid var(--glass-border);
  border-radius: 22px;
  box-shadow: var(--glass-shadow-lg);
  padding: 1.25rem 1.5rem;
  margin-bottom: 1rem;
}
.aow-hero h1 {
  margin: 0;
  font-size: 1.9rem;
  letter-spacing: -0.02em;
  color: var(--ink);
}
.aow-hero p { margin: 0.35rem 0 0; color: var(--ink-soft); font-size: 0.94rem; }

.aow-chips { display: flex; flex-wrap: wrap; gap: 0.45rem; margin-top: 0.85rem; }
.aow-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  background: rgba(255,255,255,0.72);
  border: 1px solid rgba(255,255,255,0.9);
  border-radius: 999px;
  padding: 0.28rem 0.75rem;
  font-size: 0.8rem;
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
    """Make a Plotly figure sit on the glass instead of on its own white card."""
    figure.update_layout(
        height=height,
        margin={"l": 10, "r": 10, "t": 30, "b": 10},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": "#141A2A"},
        legend={"orientation": "h", "y": 1.14, "bgcolor": "rgba(0,0,0,0)"},
    )
    figure.update_xaxes(gridcolor="rgba(20,26,42,0.08)", zerolinecolor="rgba(20,26,42,0.12)")
    figure.update_yaxes(gridcolor="rgba(20,26,42,0.08)", zerolinecolor="rgba(20,26,42,0.12)")
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
