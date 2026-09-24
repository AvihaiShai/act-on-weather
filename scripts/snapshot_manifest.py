"""Derive the snapshot's headline numbers from the snapshot itself.

The README and the Makefile tell a reviewer how much data ships in
``data/snapshot/``. Those numbers were written by hand once and went stale:
a README saying "289 places" shipped against a snapshot holding 620, which is
the kind of error that costs more trust than the data is worth.

So the numbers get a source. ``python scripts/snapshot_manifest.py`` writes
``data/snapshot/MANIFEST.json`` from the files; ``--check`` rebuilds it, fails
if the committed manifest has drifted from the files, and then fails if any
count quoted in the README or the Makefile disagrees with it. CI runs the
``--check`` form, so a snapshot change that leaves the prose behind cannot
merge.

Counts are reported as **unique ids**, not lines. The consumer upserts on the
record id, so a file with a duplicated id stores fewer rows than it has lines,
and the number a reviewer can verify against ``/coverage`` is the unique one.
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
# listed.
PROSE = [
    (r"\b(\d+)\s+places\b", ("places",)),
    (r"\b(\d+)\s+background\s+articles\b", ("facts",)),
    (r"\b(\d+)\s+verified\s+events\b", ("events",)),
    (r"\b(\d+)\s+labelled\s+sample\s+events\b", ("events.samples",)),
    (r"\b(\d+)\s+labelled\s+samples\b", ("events.samples",)),
    (r"\b(\d+)\s+verified\s+\+\s+(\d+)\s+samples\b", ("events", "events.samples")),
    (r"\b(\d+)-day\s+daily\s+forecasts\b", ("weather.days",)),
]

DOCUMENTS = ("README.md", "Makefile")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build() -> dict:
    entities = {}
    for name, filename in ENTITIES.items():
        path = SNAPSHOT / filename
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
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


def counts(manifest: dict) -> dict[str, int]:
    """The numbers the prose is allowed to quote, keyed as the patterns name them."""
    values = {name: entry["stored"] for name, entry in manifest["entities"].items()}
    values["weather.days"] = manifest["entities"]["weather"]["days"]
    return values


def check() -> int:
    built = build()
    errors = []

    if not MANIFEST.exists():
        print(f"{MANIFEST} is missing; run: python scripts/snapshot_manifest.py", file=sys.stderr)
        return 1
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

    for source, mirror in MIRRORED.items():
        if digest(ROOT / source) != digest(ROOT / mirror):
            errors.append(
                f"{source} and {mirror} differ; the snapshot was rebuilt from a different seed"
            )

    expected = counts(built)
    for document in DOCUMENTS:
        text = (ROOT / document).read_text(encoding="utf-8")
        for pattern, names in PROSE:
            for match in re.finditer(pattern, text):
                for quoted, name in zip(match.groups(), names, strict=True):
                    if int(quoted) != expected[name]:
                        line = text[: match.start()].count("\n") + 1
                        errors.append(
                            f"{document}:{line}: says {quoted} for {name}, "
                            f"snapshot has {expected[name]} -- {match.group(0)!r}"
                        )

    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(
        "snapshot manifest matches the files, and every count in "
        + " and ".join(DOCUMENTS)
        + " matches it:"
    )
    for name, value in sorted(expected.items()):
        print(f"  {name} = {value}")
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        raise SystemExit(check())
    MANIFEST.write_text(json.dumps(build(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {MANIFEST.relative_to(ROOT)}")
