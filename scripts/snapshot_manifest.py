"""Derive the snapshot's headline numbers from the snapshot itself.

The README and the Makefile tell a reviewer how much data ships in
``data/snapshot/``. Those numbers were written by hand once and went stale: a
README putting the place count at 289 shipped against a snapshot holding 620,
which is the kind of error that costs more trust than the data is worth.

So the numbers get a source. ``python scripts/snapshot_manifest.py`` writes
``data/snapshot/MANIFEST.json`` from the files; ``--check`` rebuilds it, fails
if the committed manifest has drifted from the files, and then fails if any
count quoted in the documentation disagrees with it. CI runs the ``--check``
form and ``tests/unit/test_readme_counts.py`` runs the same tables, so a
snapshot change that leaves the prose behind cannot merge.

What ``--check`` verifies, in order:

1. every file an entity names is present, and no unclaimed file is sitting in
   ``data/snapshot/`` pretending to be part of the reviewed data;
2. the committed manifest matches the files -- sha256, line count, unique-id
   count, city count, and the forecast window;
3. the manifest lists no entity that nothing produces any more;
4. every entity covers every city configured in ``data/cities.yml``, so a
   snapshot that silently lost a city fails here rather than in the UI;
5. the snapshot files still match the seeds they were built from;
6. every count and every per-city breakdown quoted in ``DOCUMENTS`` agrees
   with the data, and every pattern in the tables below still matches
   something, so a reworded sentence cannot quietly switch the guard off.

Counts are reported as **unique ids**, not lines. The consumer upserts on the
record id, so a file with a duplicated id stores fewer rows than it has lines,
and the number a reviewer can verify against ``/coverage`` is the unique one.

Deliberately stdlib-only: CI runs it on a bare runner with no ``pip install``,
which is also why ``data/cities.yml`` is read with a regex rather than PyYAML.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "data" / "snapshot"
MANIFEST = SNAPSHOT / "MANIFEST.json"
CITIES = ROOT / "data" / "cities.yml"

# Entity name -> the file it ships in. The name is what the prose checks below
# refer to, and what /coverage calls the same data.
ENTITIES = {
    "weather": "weather.jsonl",
    "places": "places.jsonl",
    "facts": "facts.jsonl",
    "events": "events.jsonl",
    "events.samples": "events.samples.jsonl",
}

# The snapshot is built from these; if the two ever diverge, a `make snapshot`
# was half-applied and the shipped data is not the data under review.
MIRRORED = {
    "data/events.seed.jsonl": "data/snapshot/events.jsonl",
    "data/events.samples.jsonl": "data/snapshot/events.samples.jsonl",
}

# Every way the documentation states one of these numbers. \s+ rather than a
# space because Markdown prose wraps, and a count split over two lines is still
# a count. Each pattern captures the numbers in the order its entities are
# listed, and each one must match somewhere or it is a guard that has stopped
# guarding -- see `unused` in prose_problems().
PROSE = [
    # Headline totals.
    (r"\b(\d+)\s+places\b", ("places",)),
    (r"\b(\d+)\s+background\s+articles\b", ("facts",)),
    (r"\b(\d+)-day\s+daily\s+forecasts\b", ("weather.days",)),
    # The verified events, whose count is the one most often repeated.
    (r"\b(\d+)\s+verified\s+events\b", ("events",)),
    (r"\b(\d+)\s+hand-verified\s+events\b", ("events",)),
    (r"\b(\d+)\s+(?:hand-)?verified\s+listings\b", ("events",)),
    (r"\b(\d+)\s+\*{0,2}real\*{0,2}\s+listings\b", ("events",)),
    (r"\b(\d+)\s+rows\s+across\s+all\s+five\s+cities\b", ("events",)),
    (r"\bverified\s+event\s+set\s+is\s+(\d+)\s+rows\b", ("events",)),
    (r"\bThere\s+are\s+(\d+),\s+covering\s+all\s+five\s+cities\b", ("events",)),
    # The generated samples, which must never be added to the verified rows.
    (r"\b(\d+)\s+labelled\s+sample\s+events\b", ("events.samples",)),
    (r"\b(\d+)\s+labelled\s+samples\b", ("events.samples",)),
    (r"\b(\d+)\s+\*{0,2}generated\s+sample\s+events\b", ("events.samples",)),
    (r"\b(\d+)\s+generated\s+events\b", ("events.samples",)),
    (r"\b(\d+)\s+generated\s+rows\b", ("events.samples",)),
    (r"\b(\d+)\s+rows\s+generated\s+by\b", ("events.samples",)),
    (r"\b(\d+)\s+rows,\s+every\s+one\s+\S*is_sample\b", ("events.samples",)),
    # Both numbers at once, which is the form the coverage tab reports.
    (r"\b(\d+)\s+verified\s+\+\s+(\d+)\s+samples\b", ("events", "events.samples")),
    # The files themselves, annotated with their row counts.
    (r"`data/snapshot/events\.jsonl`\s*\((\d+)\)", ("events",)),
    (r"`data/snapshot/events\.samples\.jsonl`\s*\((\d+)\)", ("events.samples",)),
]

# Prose that breaks one entity down per city. The totals above are covered;
# this is for the split drifting away from the total, which is easy to miss
# because the breakdown is written out four times. Group names are city slugs
# with `-` spelled `_`, since a regex group name cannot contain a hyphen.
BREAKDOWNS = [
    (
        r"\(london\s+(?P<london>\d+),\s+rome\s+(?P<rome>\d+),"
        r"\s+tel-aviv\s+(?P<tel_aviv>\d+),\s+reykjavik\s+(?P<reykjavik>\d+),"
        r"\s+lisbon\s+(?P<lisbon>\d+)\)",
        "events",
    ),
    (
        r"(?P<london>\d+)\s+London,\s+(?P<rome>\d+)\s+Rome,"
        r"\s+(?P<tel_aviv>\d+)\s+Tel\s+Aviv,\s+(?P<reykjavik>\d+)\s+Reykjavík,"
        r"\s+(?P<lisbon>\d+)\s+Lisbon",
        "events",
    ),
]

# Every tracked document that quotes one of these numbers. A document with no
# counts in it costs nothing to include and becomes a tripwire the day someone
# adds one.
DOCUMENTS = (
    "README.md",
    "Makefile",
    "ASSIGNMENT.md",
    "TECHNICAL_DECISIONS.md",
    "docs/ARCHITECTURE.md",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def line_of(text: str, at: int) -> int:
    """The 1-based line number of offset `at`, for an error a reader can jump to."""
    return text[:at].count("\n") + 1


def rows_of(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def city_slugs() -> list[str]:
    """The configured city slugs, read out of data/cities.yml without PyYAML."""
    return re.findall(r"^\s*-\s+slug:\s*(\S+)\s*$", CITIES.read_text(encoding="utf-8"), re.M)


def missing_files() -> list[str]:
    """Entity files the manifest describes that are not on disk."""
    return [f"data/snapshot/{name}" for name in ENTITIES.values() if not (SNAPSHOT / name).exists()]


def build() -> dict:
    entities = {}
    for name, filename in ENTITIES.items():
        path = SNAPSHOT / filename
        rows = rows_of(path)
        entry = {
            "file": f"data/snapshot/{filename}",
            "sha256": digest(path),
            "lines": len(rows),
            # What actually lands in the database: the consumer upserts on id.
            "stored": len({row["id"] for row in rows}) if "id" in rows[0] else len(rows),
            "cities": len({row["city_id"] for row in rows}),
        }
        if name == "weather":
            dates = sorted({row["forecast_date"] for row in rows})
            entry["days"] = len(dates)
            entry["first_date"] = dates[0]
            entry["last_date"] = dates[-1]
        if name.startswith("events"):
            # The distribution, not just the total. F9 was a finding about
            # shape: 26 verified events read as coverage of five cities until
            # you saw that eleven were in London and one was in Tel Aviv. A
            # derived per-city count means the README can state where the feed
            # is thin without anybody re-counting the file by hand, and means a
            # later edit that quietly concentrates the feed in one city shows
            # up in the manifest diff.
            by_city: dict[str, int] = {}
            for row in rows:
                by_city[row["city_id"]] = by_city.get(row["city_id"], 0) + 1
            entry["by_city"] = dict(sorted(by_city.items()))
            entry["checked_at"] = sorted({row["checked_at"] for row in rows})[0]
            entry["valid_until"] = sorted({row["valid_until"] for row in rows})[0]
        entities[name] = entry
    return {"entities": entities}


# The one column the ingestor is allowed to add on its way from a source file
# to the snapshot. `valid_until` is derived from the row's own `checked_at` by
# `config.event_valid_until`, deliberately rather than being written into the
# source file: the expiry belongs to the freshness policy, not to the listing,
# so changing the configured window has to move every row together. Everything
# else must match, because "the snapshot is the seed" is the property that
# stops a hand-edited snapshot shipping past review.
DERIVED_IN_SNAPSHOT = {"valid_until"}


def mirror_errors(source: Path, mirror: Path) -> list[str]:
    """Where a snapshot file has drifted from the file it is built out of.

    Compared row by row rather than byte for byte, so the derived column above
    is allowed through and nothing else is. A byte comparison was simpler and
    right until the ingestor started deriving an expiry; loosening it to "the
    files are roughly similar" would have given up the check, so instead it
    names exactly which key may appear and reports any other difference with
    the id of the row it is in.
    """
    rel = f"{source.relative_to(ROOT)} and {mirror.relative_to(ROOT)}".replace("\\", "/")
    seed = {row["id"]: row for row in rows_of(source)}
    shipped = {row["id"]: row for row in rows_of(mirror)}
    if set(seed) != set(shipped):
        missing = sorted(set(seed) - set(shipped))
        extra = sorted(set(shipped) - set(seed))
        return [f"{rel} hold different rows; missing {missing}, unexpected {extra}"]

    errors = []
    for row_id, want in seed.items():
        got = dict(shipped[row_id])
        for key in DERIVED_IN_SNAPSHOT:
            if key not in got:
                errors.append(f"{rel}: {row_id} is missing the derived {key}")
            elif key not in want:
                # Derived on the way through, so there is nothing to compare it
                # against. A source that already carries the column -- the
                # generated sample file does -- is compared on it like any
                # other key, because there the value is the producer's own and
                # a difference really would mean the two files were built
                # separately.
                got.pop(key)
        if got != want:
            differing = sorted(set(got) ^ set(want)) or sorted(
                k for k in want if got.get(k) != want.get(k)
            )
            errors.append(f"{rel}: {row_id} differs on {differing}")
    return errors


def counts(manifest: dict) -> dict[str, int]:
    """The numbers the prose is allowed to quote, keyed as the patterns name them."""
    values = {name: entry["stored"] for name, entry in manifest["entities"].items()}
    values["weather.days"] = manifest["entities"]["weather"]["days"]
    return values


def per_city() -> dict[str, dict[str, int]]:
    """Unique ids per city, per entity, straight from the files.

    Not from the manifest: the manifest records how many cities an entity
    covers, not the split, and its format is what the committed file already
    is. Step 2 of --check has pinned these files by sha256 before any caller
    gets here.
    """
    split: dict[str, dict[str, int]] = {}
    for name, filename in ENTITIES.items():
        rows = rows_of(SNAPSHOT / filename)
        # Same rule as build(): unique ids where the entity has them, rows
        # otherwise. Weather is keyed by (city, date) and carries no id, so
        # counting a missing field would collapse every day into one.
        keyed = "id" in rows[0]
        seen: dict[str, set[str]] = {}
        for index, row in enumerate(rows):
            seen.setdefault(row["city_id"], set()).add(row["id"] if keyed else str(index))
        split[name] = {city: len(ids) for city, ids in seen.items()}
    return split


def snapshot_problems() -> list[str]:
    """Everything that can be wrong with the files and the committed manifest."""
    errors: list[str] = []

    gone = missing_files()
    if gone:
        # Nothing below can run without them, and a traceback would be a worse
        # way to learn that the snapshot is incomplete.
        return [f"missing snapshot file: {name}" for name in gone]

    claimed = {MANIFEST.name} | set(ENTITIES.values())
    for stray in sorted(p.name for p in SNAPSHOT.iterdir() if p.name not in claimed):
        errors.append(
            f"data/snapshot/{stray} is not listed in ENTITIES; "
            "either add it or take it out of the snapshot"
        )

    built = build()
    if not MANIFEST.exists():
        errors.append(f"{MANIFEST} is missing; run: python scripts/snapshot_manifest.py")
        return errors

    committed = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if committed != built:
        errors.append(
            "data/snapshot/MANIFEST.json does not match the snapshot files; "
            "rerun: python scripts/snapshot_manifest.py"
        )
        for name, entry in built["entities"].items():
            was = committed.get("entities", {}).get(name)
            if was != entry:
                errors.append(f"  {name}: manifest {was} != files {entry}")
        for name in sorted(set(committed.get("entities", {})) - set(built["entities"])):
            errors.append(f"  {name}: in the manifest, but nothing produces it any more")

    configured = set(city_slugs())
    for name, split in per_city().items():
        absent = sorted(configured - set(split))
        extra = sorted(set(split) - configured)
        if absent:
            errors.append(
                f"{name}: no rows for {', '.join(absent)}, which data/cities.yml configures"
            )
        if extra:
            errors.append(
                f"{name}: rows for {', '.join(extra)}, which data/cities.yml does not list"
            )

    for source, mirror in MIRRORED.items():
        errors.extend(mirror_errors(ROOT / source, ROOT / mirror))

    return errors


def prose_problems(manifest: dict, documents: tuple[str, ...] = DOCUMENTS) -> list[str]:
    """Every count and breakdown in `documents` that disagrees with the data."""
    errors: list[str] = []
    expected = counts(manifest)
    split = per_city()
    unused = {pattern for pattern, _ in PROSE} | {pattern for pattern, _ in BREAKDOWNS}
    present = [name for name in documents if (ROOT / name).is_file()]

    for document in present:
        text = (ROOT / document).read_text(encoding="utf-8")

        def line_of(text: str, at: int) -> int:
            return text[:at].count("\n") + 1

        for pattern, names in PROSE:
            for match in re.finditer(pattern, text):
                unused.discard(pattern)
                for quoted, name in zip(match.groups(), names, strict=True):
                    if int(quoted) != expected[name]:
                        errors.append(
                            f"{document}:{line_of(text, match.start())}: says {quoted} for "
                            f"{name}, snapshot has {expected[name]} -- {match.group(0)!r}"
                        )

        for pattern, name in BREAKDOWNS:
            for match in re.finditer(pattern, text):
                unused.discard(pattern)
                for group, quoted in match.groupdict().items():
                    city = group.replace("_", "-")
                    actual = split[name].get(city, 0)
                    if int(quoted) != actual:
                        errors.append(
                            f"{document}:{line_of(text, match.start())}: says {quoted} "
                            f"{name} in {city}, snapshot has {actual}"
                        )

    for pattern in sorted(unused):
        errors.append(
            f"no document matches {pattern!r} any more; the wording changed and this "
            "guard went quiet -- update the pattern or delete the row"
        )
    return errors


def check() -> int:
    errors = snapshot_problems()
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    errors = prose_problems(manifest)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1

    print(
        "snapshot manifest matches the files, and every count in "
        + ", ".join(DOCUMENTS)
        + " matches it:"
    )
    for name, value in sorted(counts(manifest).items()):
        print(f"  {name} = {value}")
    for name, split in sorted(per_city().items()):
        print(f"  {name} by city = {', '.join(f'{c} {n}' for c, n in sorted(split.items()))}")
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        raise SystemExit(check())
    MANIFEST.write_text(json.dumps(build(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {MANIFEST.relative_to(ROOT)}")
