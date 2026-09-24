"""The bundle's image table has to agree with the Compose files it reads.

`scripts/package-offline.sh` carries a table of bundle alias -> upstream
repository, and resolves each one against `docker compose config --images`. If
the table and the Compose files disagree, packaging fails -- loudly, which is
the right way round, but only on a staging machine with the registry reachable
and roughly ten minutes into a run.

These tests are the cheap version of that, with no Docker and no network. They
read the same table out of the script and check it against the same Compose
files, so a repository renamed, an image added to the observability overlay
without being added to the bundle, or a pin that changes shape is caught in the
unit job instead.

The shape matters and is the reason this file exists. Compose renders a pin
that kept its tag as `postgres:17-alpine@sha256:...` and a digest-only pin as
`prom/prometheus@sha256:...`. A matcher that assumes the first form cannot see
the second at all.
"""

from __future__ import annotations

import re
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "scripts" / "package-offline.sh"
# The files package-offline.sh renders to build its image list.
COMPOSE = (ROOT / "compose.yml", ROOT / "compose.observability.yml")


def alias_table() -> dict[str, str]:
    """The alias -> repository pairs the packaging script iterates over."""
    text = SCRIPT.read_text(encoding="utf-8")
    block = re.search(r"for pair in (.*?); do", text, re.S)
    assert block, "package-offline.sh no longer has a `for pair in ...` table"
    pairs = re.findall(r"'([^']+)'", block.group(1))
    assert pairs, "the alias table is empty"
    table = {}
    for pair in pairs:
        alias, _, repo = pair.partition("=")
        assert repo, f"{pair!r} is not alias=repository"
        table[alias] = repo
    return table


def pinned_images() -> list[str]:
    """Every `image:` reference in the Compose files, as written."""
    found = []
    for path in COMPOSE:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s+image:\s*(\S+)", line)
            if match:
                found.append(match.group(1))
    return found


def resolves(repo: str, images: list[str]) -> list[str]:
    """The same rule the script uses: a prefix ending at `:` or `@`.

    A prefix test rather than a substring one, so `nginx` does not match
    `nginx-extras`; the delimiter is what proves the repository name ended.
    """
    return [i for i in images if i.startswith(f"{repo}:") or i.startswith(f"{repo}@")]


def test_every_bundle_alias_resolves_to_a_pinned_image() -> None:
    images = pinned_images()
    for alias, repo in alias_table().items():
        matches = resolves(repo, images)
        assert matches, f"{alias}: nothing in the Compose files starts with {repo}: or {repo}@"
        for reference in matches:
            assert "@sha256:" in reference, f"{alias}: {reference} is not pinned by digest"


def test_both_reference_shapes_are_actually_present() -> None:
    """Guards the bug this file was written for.

    If every pin in the repository grew a tag, a matcher that only handled
    `repo:` would pass these tests while still being wrong the next time
    someone pins by digest alone. So assert the repository really does contain
    both shapes, and that the table copes with each.
    """
    images = pinned_images()
    table = alias_table()
    tagged = [i for i in images if re.match(r"[^@]+:[^@]+@sha256:", i)]
    digest_only = [i for i in images if re.match(r"[^:@]+(/[^:@]+)*@sha256:", i)]
    assert tagged, "expected at least one `repo:tag@sha256:` pin"
    assert digest_only, "expected at least one digest-only `repo@sha256:` pin"

    for group, label in ((tagged, "tagged"), (digest_only, "digest-only")):
        covered = [
            alias
            for alias, repo in table.items()
            if any(r in resolves(repo, images) for r in group)
        ]
        assert covered, f"no bundle alias resolves a {label} pin; the matcher is untested for it"


def test_the_two_edges_share_one_nginx_reference() -> None:
    """`edge` and `edge-observability` share a bundle alias, so they must share
    an image. package-offline.sh fails packaging if they stop; this says so
    earlier and without Docker."""
    nginx = {i for i in pinned_images() if i.startswith(("nginx:", "nginx@"))}
    assert len(nginx) == 1, f"edge and edge-observability disagree on nginx: {sorted(nginx)}"


def test_promotion_record_covers_every_bundle_alias() -> None:
    """Every image the package script saves must have a documented proof tier."""
    promotion = runpy.run_path(str(ROOT / "scripts" / "cd-promotion-record.py"))
    saved_aliases = set(alias_table()) | {"services", "ui", "stage", "demos"}
    assert set(promotion["PROVENANCE_BY_ALIAS"]) == saved_aliases
