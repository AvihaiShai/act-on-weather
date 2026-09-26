"""Every migration in `db/migrations/` is one the `migrate` service runs.

This exists because it did not, and nothing noticed.
`008_itinerary_as_of_optional.sql` was written, committed, and cited in an
evidence document as having been applied against a real Postgres, while the
`migrate` command in `compose.yml` stopped at `007`. No database this
repository has ever started had the change in it.

Nothing else could have caught it. The command is an explicit `-f` list rather
than a glob, so adding a file is not enough; there is no migrations table, so
nothing at runtime knows a file exists and was skipped; and the unit suite
mocks the database, so a schema change that never happened looks exactly like
one that did. The failure mode is silent by construction: the stack starts
clean, every drill passes, and the defect only appears when a caller finally
exercises the column the migration was supposed to change -- at which point the
API accepts the write and the consumer dead-letters it.

So the list is checked against the directory, in both directions:

  * every numbered migration file is passed to psql, and
  * every file psql is told to run exists,

which also catches the rename and the typo. `900_fault_injection.sql` is
deliberately out of scope: it is generated into a bundle by
`scripts/make-fault-injection-bundle.sh`, which appends its own `-f` line to a
copy of `compose.yml`, and it must never be in the committed list.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "db" / "migrations"

# The numbered migrations the shipped stack applies. 9xx is reserved for files
# a tool injects into a bundle and never belongs here.
COMMITTED = sorted(p.name for p in MIGRATIONS.glob("*.sql") if not p.name.startswith("9"))

# `-f /db/migrations/<name>.sql`, as the command writes it.
APPLIED_RE = re.compile(r"-f\s+/db/migrations/(\S+\.sql)")


def _migrate_command() -> str:
    compose = yaml.safe_load((ROOT / "compose.yml").read_text(encoding="utf-8"))
    command = compose["services"]["migrate"]["command"]
    # The block scalar folds to a string; a list form would be equally valid
    # Compose, so both are accepted rather than assumed.
    return command if isinstance(command, str) else " ".join(command)


def _applied() -> list[str]:
    return APPLIED_RE.findall(_migrate_command())


def test_there_is_at_least_one_migration_to_check():
    """A guard on the guard: an empty glob would make everything below vacuous."""
    assert COMMITTED, "no migrations found -- this test is asserting nothing"


def test_every_migration_file_is_applied_by_the_migrate_service():
    """The regression. An unwired migration is a schema change that silently
    never happens, and there is no other place it would be noticed."""
    missing = [name for name in COMMITTED if name not in _applied()]

    assert not missing, (
        "these migrations exist but the migrate service never runs them: " + ", ".join(missing)
    )


def test_every_migration_the_service_runs_exists():
    """The other direction: a rename or a typo makes psql exit non-zero at
    boot with `ON_ERROR_STOP`, which fails the stack rather than the build."""
    absent = [name for name in _applied() if not (MIGRATIONS / name).is_file()]

    assert not absent, "the migrate command names files that do not exist: " + ", ".join(absent)


def test_the_migrations_are_applied_in_numeric_order():
    """They are not independent: 004 grants on a table 001 creates, and 008
    alters a column 001 declares. Applying them out of order is a boot failure
    that reads as a broken migration rather than a reordered list."""
    applied = _applied()

    assert applied == sorted(applied), f"out of order: {applied}"


def test_no_migration_is_applied_twice():
    """A duplicated `-f` is harmless for an idempotent file and destructive for
    one that is not, and it is invisible in a folded YAML block scalar."""
    applied = _applied()

    assert len(applied) == len(set(applied)), f"duplicated in the migrate command: {applied}"
