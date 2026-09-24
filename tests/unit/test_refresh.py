"""The operator refresh reports per city, and says so when one city fails.

`scripts/refresh.sh` is the part that opens and closes the egress window; these
tests cover the part it runs inside the ingestor. The property that matters is
narrow but easy to lose: a refresh where the provider answered for four cities
and refused the fifth must not look like a success. The fifth city's stored
forecast is then quietly older than the rest, and only the per-city report and
the non-zero exit code make that visible.

Nothing here touches the network: the provider is a stub, and the outbox is a
SQLite file in tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.common import config
from services.common.outbox import Outbox
from services.ingestor import main as ingestor
from services.ingestor import refresh

ROOT = Path(__file__).resolve().parents[2]

CITIES = [
    {"slug": "rome", "name": "Rome", "lat": 41.9, "lon": 12.5, "timezone": "Europe/Rome"},
    {"slug": "london", "name": "London", "lat": 51.5, "lon": -0.1, "timezone": "Europe/London"},
]


class StubProvider:
    """Answers for every city except the ones named in `failing`."""

    name = "stub"

    def __init__(self, *, failing: set[str] | None = None, as_of="2026-09-24T06:00:00+00:00"):
        self.failing = failing or set()
        self.as_of = as_of
        self.calls: list[str] = []

    def daily_forecast(self, city, days):
        self.calls.append(city["slug"])
        if city["slug"] in self.failing:
            raise ConnectionError("no route to the provider")
        return [
            {
                "city_id": city["slug"],
                "forecast_date": f"2026-09-{24 + day:02d}",
                "provider": self.name,
                "temp_max_c": 20 + day,
                "as_of": self.as_of,
            }
            for day in range(days)
        ]


@pytest.fixture
def stack(monkeypatch, tmp_path):
    """A real outbox on disk, real city data, a stubbed provider."""
    monkeypatch.setattr(config, "OUTBOX_PATH", tmp_path / "outbox.sqlite3")
    monkeypatch.setattr(config, "DATA_DIR", ROOT / "data")

    def install(provider):
        monkeypatch.setattr(
            "services.ingestor.providers.get_provider", lambda name="stub": provider
        )
        return provider

    return install


# ------------------------------------------------------------- the report --


def test_a_clean_refresh_reports_every_city_and_exits_zero(stack, capsys, tmp_path):
    stack(StubProvider())

    code = refresh.main(["--json", "--days", "3", "--city", "rome", "--city", "london"])
    report = json.loads(capsys.readouterr().out)

    assert code == 0
    assert report["ok"] is True
    assert report["failed_cities"] == []
    assert {c["city"] for c in report["cities"]} == {"rome", "london"}
    for city in report["cities"]:
        assert city["ok"] is True
        assert city["accepted"] == 3
        assert city["as_of"] == "2026-09-24T06:00:00+00:00"
        assert city["first_date"] == "2026-09-24"
        assert city["last_date"] == "2026-09-26"
        assert len(city["message_ids"]) == 3

    # The ids are the operator's handle on the refresh: every one of them is in
    # the outbox, which is where the delivery guarantee starts.
    box = Outbox(config.OUTBOX_PATH)
    assert box.counts() == {"total": 6, "pending": 6}
    for message_id in report["message_ids"]:
        assert box.status_of(message_id) is not None


def test_one_failed_city_is_not_a_success(stack, capsys):
    """Four cities fetched and one refused is a failed refresh, and the exit
    code has to say so -- the wrapper turns it into a non-zero exit for the
    operator, and the other cities' days are still accepted."""
    provider = stack(StubProvider(failing={"london"}))

    code = refresh.main(["--json", "--days", "2", "--city", "rome", "--city", "london"])
    report = json.loads(capsys.readouterr().out)

    assert code == refresh.FETCH_FAILED
    assert report["ok"] is False
    assert report["failed_cities"] == ["london"]
    assert set(provider.calls) == {"rome", "london"}

    failed = next(c for c in report["cities"] if c["city"] == "london")
    assert failed["accepted"] == 0
    assert failed["message_ids"] == []
    assert "ConnectionError" in failed["error"]

    # Rome was still accepted. One city's outage must not throw away another's.
    ok = next(c for c in report["cities"] if c["city"] == "rome")
    assert ok["ok"] is True and ok["accepted"] == 2
    assert report["accepted"] == 2


def test_a_provider_that_returns_nothing_is_a_failure_not_an_empty_success(stack, capsys):
    class Empty(StubProvider):
        def daily_forecast(self, city, days):
            return []

    stack(Empty())
    code = refresh.main(["--json", "--city", "rome"])
    report = json.loads(capsys.readouterr().out)

    assert code == refresh.FETCH_FAILED
    assert report["cities"][0]["error"] == "provider returned no forecast days"


def test_an_unknown_city_is_a_usage_error(stack, capsys):
    stack(StubProvider())
    assert refresh.main(["--json", "--city", "atlantis"]) == refresh.USAGE
    assert capsys.readouterr().out == ""


def test_re_running_the_same_fetch_does_not_duplicate(stack, capsys):
    """Same as-of, same natural key, same message id: a second run re-accepts
    nothing. That is what makes a retried refresh safe."""
    stack(StubProvider())
    refresh.main(["--json", "--days", "2", "--city", "rome"])
    first = json.loads(capsys.readouterr().out)
    refresh.main(["--json", "--days", "2", "--city", "rome"])
    second = json.loads(capsys.readouterr().out)

    assert first["message_ids"] == second["message_ids"]
    assert second["outbox"]["total"] == 2


def test_the_ingest_loop_still_gets_a_count(stack):
    """`accept_live_weather` returns rows now, and the long-running ingestor
    sums them. Pin that, because nothing else would notice it breaking."""
    stack(StubProvider(failing={"london"}))
    box = Outbox(config.OUTBOX_PATH)
    results = ingestor.accept_live_weather(box, CITIES, 2)
    assert sum(r["accepted"] for r in results) == 2


# ------------------------------------------------------- the tools service --


def test_the_refresh_container_has_no_route_out():
    """It drives the window; it never goes through it. The fetch happens in the
    ingestor, and the tools container stays on the internal network."""
    tools = (ROOT / "compose.tools.yml").read_text(encoding="utf-8")
    service = tools.split("  refresh:", 1)[1]
    assert "networks: [backend]" in service
    assert "egress" not in service

    # And it is not part of the stack: `docker compose up -d` cannot start it.
    # Comments are stripped first -- compose.yml mentions the script by name
    # where it explains the refresh_state volume, and that is documentation,
    # not a service that runs it.
    stack = "\n".join(
        line
        for line in (ROOT / "compose.yml").read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "refresh.sh" not in stack


def test_the_tools_target_the_aow_project_unless_told_otherwise():
    """AOW_PROJECT exists so a drill can run the shipped command against an
    isolated stack instead of editing this file. Its default has to stay `aow`,
    and the project and the network have to be driven by the same variable --
    otherwise these tools would drive one stack over another stack's network.
    """
    tools = (ROOT / "compose.tools.yml").read_text(encoding="utf-8")
    assert "name: ${AOW_PROJECT:-aow}-tools" in tools
    assert "name: ${AOW_PROJECT:-aow}_backend" in tools
    # Both socket-holding services, not just the refresh one.
    assert tools.count("COMPOSE_PROJECT_NAME: ${AOW_PROJECT:-aow}") == 2
    assert ": aow\n" not in tools, "a hardcoded project name came back"


def test_the_image_owns_the_directory_the_report_is_written_to():
    """Docker takes a fresh named volume's ownership from the image's directory.
    The services image runs as uid 10001, so a mount point that does not exist in
    the image comes out root-owned and the write fails with EACCES -- and only on
    a first run with a fresh volume, which is the run a reviewer does. This is
    that bug, found in the drill and pinned here.
    """
    dockerfile = (ROOT / "services" / "Dockerfile").read_text(encoding="utf-8")
    mount = str(config.REFRESH_STATE_PATH.parent)
    assert f"mkdir -p /outbox {mount}" in dockerfile
    assert f"chown -R appuser /app /outbox {mount}" in dockerfile


def test_only_the_ingestor_may_write_the_report():
    """The api serves the report and must not be able to change it: a status page
    that can rewrite the status it reports is not a status page. Read-only there,
    writable in the ingestor, which is where the refresh already runs."""
    stack = (ROOT / "compose.yml").read_text(encoding="utf-8")
    mount = str(config.REFRESH_STATE_PATH.parent)
    ingestor = stack.split("  ingestor:", 1)[1].split("\n  consumer:", 1)[0]
    api = stack.split("\n  api:", 1)[1].split("\n  ui:", 1)[0]
    assert f"- refresh_state:{mount}\n" in ingestor
    assert f"- refresh_state:{mount}:ro" in api


def test_the_run_report_is_filed_on_every_path_but_a_check():
    """The report is what the UI reads at GET /refresh/last. It is written from
    the exit trap, so an interrupted run still records how far it got -- and
    `--check` writes nothing, because overwriting the last real refresh with a
    window test would lose the more useful of the two."""
    script = (ROOT / "scripts" / "refresh.sh").read_text(encoding="utf-8")
    trap = script.split("on_exit() {", 1)[1].split("\ntrap ", 1)[0]
    assert "persist_report" in trap, "the exit trap does not file the run report"
    # After the window is dealt with, never before: the boundary outranks the
    # bookkeeping.
    assert trap.index("close_egress") < trap.index("persist_report")
    assert "PERSIST=0" in script.split('if [ "$CHECK" = 1 ]; then', 1)[1]
