-- Migration 003: one narrow DELETE grant, so demo mode can be left.
--
-- 001 says, and means: "No DELETE grant for either role: nothing in this
-- system removes a record, it supersedes it." That rule is right for collected
-- data. A forecast is revised, a place is corrected, a recommendation is
-- re-worded -- every one of those is an upsert with a new revision, and the
-- history stays. Deleting has no place in it.
--
-- Generated sample events are the one thing in this system that is not
-- collected data. They are 45 rows written by services/ingestor/make_samples.py
-- so the trip planner and the agent can be exercised outside London, where the
-- only seven verified events are. They are opt-in (compose.demo.yml,
-- `make up-demo`) and labelled everywhere, and the README makes a stronger
-- claim than labelling: a default run's database does not contain a fabricated
-- row at all. Leaving demo mode therefore has to remove them, which needs a
-- DELETE, which 001 does not grant.
--
-- So: DELETE on `events` only, to the writer only. Not schema-wide, and not
-- through ALTER DEFAULT PRIVILEGES, so a table added later does not inherit
-- it. The consumer is still the only role that may change data, which is what
-- M4 actually rests on; what changes is that it may now remove a row from one
-- table, for one documented reason.
--
-- Idempotent, like the other two: GRANT on an already-granted privilege is a
-- no-op, so this file is safe to re-run on every boot.

GRANT DELETE ON events TO aow_writer;

COMMENT ON TABLE events IS
  'Verified event listings (is_sample = false) plus, in demo mode only, '
  'generated samples (is_sample = true). The writer holds DELETE on this table '
  'alone, so that a consumer starting with AOW_DEMO_EVENTS unset can remove '
  'every sample row left behind by a demo run. See migration 003.';
