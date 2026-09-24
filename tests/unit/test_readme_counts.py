"""The documentation's snapshot counts must come from the snapshot.

`scripts/snapshot_manifest.py --check` already runs in CI's guard job. This is
the same check as a unit test, for two reasons: a reviewer running `make test`
sees it, and a failure arrives named -- "README.md:619 says 26, the snapshot has
7" -- rather than as a shell step that exited 1.

The tables live in the script, not here, so there is one list of "which
sentence quotes which number" rather than two that drift apart. What this file
adds is the pytest entry point and the checks on the tables themselves.

No network, no stack, no Docker: it reads `data/snapshot/`, `data/cities.yml`
and the tracked documents off disk.

`tests/Dockerfile` copies `data/` and `scripts/` but not the Markdown, so
inside the test image the document half skips and CI's guard job is what
enforces it. On a working tree -- which is where `pytest tests/unit` is run
during development -- everything is present and it all runs.
"""

from __future__ import annotations

import json

import pytest

from scripts import snapshot_manifest as sm

ABSENT = [name for name in sm.DOCUMENTS if not (sm.ROOT / name).is_file()]

needs_documents = pytest.mark.skipif(
    bool(ABSENT),
    reason=(
        f"not in this checkout: {', '.join(ABSENT)}. Add them to tests/Dockerfile "
        "to run the document half here as well as in CI's guard job."
    ),
)


@pytest.fixture(scope="module")
def manifest() -> dict:
    """The committed manifest, which is what the prose is measured against."""
    return json.loads(sm.MANIFEST.read_text(encoding="utf-8"))


def test_manifest_matches_the_snapshot_files():
    """sha256, row counts, city coverage, seed mirrors -- all of it, or say why not."""
    assert sm.snapshot_problems() == []


def test_manifest_covers_every_configured_city(manifest):
    configured = set(sm.city_slugs())
    assert configured, "data/cities.yml parsed to nothing"
    for name, split in sm.per_city().items():
        assert set(split) == configured, name
        assert manifest["entities"][name]["cities"] == len(configured)


def test_every_pattern_names_a_real_count(manifest):
    """A table row pointing at an entity that does not exist would never fire."""
    known = set(sm.counts(manifest))
    for pattern, names in sm.PROSE:
        for name in names:
            assert name in known, f"{pattern} refers to unknown count {name!r}"
    for pattern, name in sm.BREAKDOWNS:
        assert name in sm.ENTITIES, f"{pattern} refers to unknown entity {name!r}"


@needs_documents
def test_documents_quote_the_manifest(manifest):
    assert sm.prose_problems(manifest) == []


@needs_documents
def test_a_wrong_count_is_caught(manifest):
    """The guard is only worth having if it fails; prove it does."""
    wrong = json.loads(json.dumps(manifest))
    wrong["entities"]["events"]["stored"] += 1
    problems = sm.prose_problems(wrong)
    assert problems, "changing the event count did not fail any document"
    assert any("README.md" in problem for problem in problems)
