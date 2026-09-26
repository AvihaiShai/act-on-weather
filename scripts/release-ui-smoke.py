"""Render every shipped Streamlit page against the installed release's API."""

from __future__ import annotations

from streamlit.testing.v1 import AppTest

PAGES = (
    "dashboard",
    "forecast",
    "suitability",
    "trip-planner",
    "places-map",
    "ask-the-agent",
    "update-data",
    "data-coverage",
    "monitoring",
)


def main() -> None:
    # This script runs inside the UI image, so AppTest uses the same app code
    # and dependencies that serve the browser. Its API_BASE points at the live
    # internal API. Navigation renders pages; no write button is pressed.
    at = AppTest.from_file("/app/app.py", default_timeout=120).run()
    for page in PAGES:
        at = at.radio[0].set_value(page).run()
        errors = [str(error.value) for error in at.exception]
        if errors:
            raise SystemExit(f"UI page {page} failed to render: {errors}")
        print(f"UI page {page}: ok", flush=True)
    print(f"PASS: {len(PAGES)} Streamlit pages rendered against the installed API")


if __name__ == "__main__":
    main()
