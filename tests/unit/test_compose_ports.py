"""Published ports: every one of them, on every overlay, bound to loopback.

The API publishes write routes -- `POST /recommendations`, `POST /itineraries`,
`POST /reenrich`, `PATCH /records/{entity}/{id}` -- and authenticates none of
them. That is a defensible shape for what this actually is (one workstation,
one operator, the UI in the same stack) and an indefensible one the moment the
port is bound to 0.0.0.0 on a host with a LAN address, because then every
client that can route to the host can queue patches and enrichment work.

Docker's short port syntax makes the bad version the shorter one: `8000:8000`
binds every interface, `127.0.0.1:8000:8000` binds one. Nothing in a Compose
file makes that difference visible, and no other test in this suite would
notice a one-word regression in an overlay. So it is checked here, over the
shipped files rather than over a running stack:

  * every published port, in every `compose*.yml`, binds a loopback address,
  * the placeholder that carries the address defaults to loopback, so a run
    with no `.env` at all is still bound,
  * `.env.example` -- what an operator copies before their first run -- ships
    that same loopback value,
  * and the only services that publish anything are the boundary proxies --
    `edge` for the UI and the API, `edge-observability` for Grafana -- so the
    database, the broker, the model server and Prometheus stay off the host
    network entirely.

Widening the boundary is still possible (`AOW_BIND_ADDR`), and is still the
operator's deliberate act. What this test forbids is doing it by accident.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = sorted(ROOT.glob("compose*.yml"))

# Loopback, and nothing else. A hostname is not accepted: it would move the
# decision into the host's resolver, where this test cannot see it.
LOOPBACK = {"127.0.0.1", "::1", "[::1]"}

# ${VAR}, ${VAR:-default}, ${VAR-default}, ${VAR:?message}, ${VAR?message}
PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:?[-?])?([^}]*)\}")

UNRESOLVED = "<unresolved>"


def expand(spec: str) -> str:
    """Resolve a port spec the way Compose would with an empty environment.

    Only the default half of a placeholder is used, because the committed
    default is the thing under test. A placeholder with no default -- `${VAR}`
    or the `:?` error form -- resolves to a sentinel that cannot pass the
    loopback check: an unset variable in a port binding is exactly as
    unreviewable as a missing one.
    """

    def one(match: re.Match[str]) -> str:
        operator, default = match.group(2), match.group(3)
        return default if operator in ("-", ":-") and default else UNRESOLVED

    return PLACEHOLDER.sub(one, spec)


def host_ip(spec: str) -> str | None:
    """The host address a published-port entry binds, or None if it binds all
    of them.

    Compose short syntax, after placeholder expansion:

        "8000"                      -> None  (ephemeral host port, 0.0.0.0)
        "8000:8000"                 -> None  (0.0.0.0)
        "127.0.0.1:8000:8000"       -> "127.0.0.1"
        "[::1]:8000:8000"           -> "[::1]"
    """
    spec = expand(str(spec)).split("/")[0]  # drop any /tcp, /udp suffix

    if spec.startswith("["):  # bracketed IPv6 literal
        close = spec.index("]")
        return spec[: close + 1]

    parts = spec.split(":")
    return parts[0] if len(parts) >= 3 else None


def published_ports() -> list[tuple[str, str, str]]:
    """(compose file, service, port entry) for every published port there is."""
    found = []
    for path in COMPOSE_FILES:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for service, spec in (doc.get("services") or {}).items():
            for entry in (spec or {}).get("ports") or []:
                if isinstance(entry, dict):
                    # Long syntax. Absent host_ip means every interface, and
                    # is reported as the empty string so the failure names it.
                    entry = f"{entry.get('host_ip', '')}:{entry.get('published', '')}:{entry.get('target', '')}"
                found.append((path.name, service, str(entry)))
    return found


def test_there_are_compose_files_to_check():
    """Guards the two tests below: a glob that matched nothing would pass them
    both without checking anything."""
    assert {"compose.yml"} <= {p.name for p in COMPOSE_FILES}
    assert published_ports(), "no published ports found at all -- has the glob or the syntax moved?"


@pytest.mark.parametrize(
    ("filename", "service", "entry"),
    published_ports(),
    ids=lambda v: str(v).replace(":", "_"),
)
def test_every_published_port_binds_loopback(filename, service, entry):
    ip = host_ip(entry)
    assert ip in LOOPBACK, (
        f"{filename}: service '{service}' publishes {entry!r}, which binds "
        f"{'every interface' if ip is None else ip}. The API has write routes and no "
        f"authentication, so a published port must name a loopback address: "
        f'"${{AOW_BIND_ADDR:-127.0.0.1}}:HOST:CONTAINER".'
    )


def test_the_placeholder_defaults_to_loopback():
    """A run with no .env, or with AOW_BIND_ADDR simply unset, is still bound.
    `expand` resolving to the sentinel would fail the test above, so this is
    the narrower statement: the default is spelled, and it is loopback."""
    text = (ROOT / "compose.yml").read_text(encoding="utf-8")
    assert "${AOW_BIND_ADDR:-127.0.0.1}" in text
    assert expand("${AOW_BIND_ADDR:-127.0.0.1}:8000:8000") == "127.0.0.1:8000:8000"


def test_env_example_ships_a_loopback_address():
    """What the operator copies on their first run. A wide value here would
    override every default above without touching a Compose file."""
    example = ROOT / ".env.example"
    values = [
        line.split("=", 1)[1].strip()
        for line in example.read_text(encoding="utf-8").splitlines()
        if line.startswith("AOW_BIND_ADDR=")
    ]
    assert values == ["127.0.0.1"], f"expected one loopback AOW_BIND_ADDR, found {values}"


# The complete list of services allowed to publish a port, and why each one
# is on it. Both are nginx reverse proxies holding no state, no credentials
# and no outbound client, and both exist for the same single reason: Docker
# cannot publish a port from an `internal: true` network, so something has to
# straddle the boundary.
#
#   edge               -- the UI (8080) and the API (8000), from compose.yml.
#   edge-observability -- Grafana (3000), from compose.observability.yml.
#
# Adding a name here is a deliberate act with a paragraph attached. Anything
# else acquiring a `ports:` key is a regression, and the assertion below is an
# equality rather than a subset so that it catches one.
PROXIES_THAT_MAY_PUBLISH = {"edge", "edge-observability"}

# Services that must never publish, named individually rather than left to the
# equality above. The equality says "only the proxies"; this says *what it
# would mean* if one of these were the exception -- and a failure that names
# the database is more useful than one that names a set difference.
MUST_NEVER_PUBLISH = {
    "postgres": "the database, holding every stored record",
    "rabbitmq": "the broker, including its unauthenticated Prometheus exporter on 15692",
    "llm": "the model server, which answers generation requests with no auth at all",
    "prometheus": "the metrics store, whose expression browser can read every series",
    "grafana": "reachable through edge-observability; a second, direct binding would bypass it",
}


def test_only_the_boundary_proxies_publish_anything():
    """The topology the README states: reverse proxies straddle the boundary,
    and nothing else touches the host network.

    A `ports:` key appearing on postgres, rabbitmq or llm would put the
    database, the broker or the model server on the host network -- each of
    which is a larger hole than the one this file exists to close. The same
    now goes for prometheus and grafana: they sit on `backend`, which is
    `internal: true`, and that is what makes the air-gap claim about Grafana
    -- a program that phones home for update checks by default -- something
    Docker enforces rather than something we merely configured."""
    assert {service for _, service, _ in published_ports()} == PROXIES_THAT_MAY_PUBLISH


@pytest.mark.parametrize(("service", "why"), sorted(MUST_NEVER_PUBLISH.items()))
def test_the_stateful_services_publish_nothing(service, why):
    """Stated per service, over every compose file, so the failure message
    names the thing that would be exposed."""
    offenders = [
        f"{filename}: {entry}" for filename, name, entry in published_ports() if name == service
    ]
    assert not offenders, (
        f"'{service}' publishes {offenders}. It must not: it is {why}. "
        f"Reach it through a proxy on `frontend`, or from inside the stack with "
        f"`docker compose exec`."
    )


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("8000", None),
        ("8000:8000", None),
        ("0.0.0.0:8000:8000", "0.0.0.0"),
        ("127.0.0.1:8000:8000", "127.0.0.1"),
        ("127.0.0.1:8000:8000/tcp", "127.0.0.1"),
        ("[::1]:8000:8000", "[::1]"),
        ("${AOW_BIND_ADDR:-127.0.0.1}:8000:8000", "127.0.0.1"),
        ("${AOW_BIND_ADDR:-0.0.0.0}:8000:8000", "0.0.0.0"),
        ("${AOW_BIND_ADDR}:8000:8000", UNRESOLVED),
    ],
)
def test_the_parser_reads_docker_port_syntax(spec, expected):
    """The check above is only as good as this: every form Compose accepts has
    to be read the way Compose reads it, or a wide binding could pass by being
    spelled unusually."""
    assert host_ip(spec) == expected


@pytest.mark.parametrize("bad", ["0.0.0.0", "::", "", None, "localhost"])
def test_a_wide_binding_would_fail(bad):
    """The negative case, because a test that can only pass proves nothing.
    'localhost' is included deliberately: it usually resolves to loopback, but
    it is the host's resolver deciding, not this file."""
    assert bad not in LOOPBACK
