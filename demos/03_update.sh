#!/usr/bin/env bash
#
# M12: updating stored information.
#
# Three update paths exist; this exercises the one that is the headline, and
# names the other two:
#
#   1. a user correction  PATCH /records/{entity}/{id}  -- accepted, queued,
#      applied by the consumer, revision bumped, before-and-after filed.
#      Works offline. Demonstrated below.
#   2. a connected refresh  docker compose -f compose.tools.yml run --rm
#      refresh  -- opens a temporary egress window for the ingestor alone,
#      re-fetches the forecast, moves the coverage window forward and closes
#      the window again. Needs connectivity, by definition, which is why it is
#      named here rather than run: these proofs run air-gapped.
#   3. re-enrichment  when weather behind a recommendation changes, the
#      consumer resets that row to pending and the enricher rewords it.
#      Works offline. Demonstrated by demos/04_reenrich.sh.
#
# The point of (1) going through the queue: a user edit is delivered, retried
# and made idempotent by exactly the same machinery as a fetched record. There
# is one write path into this database, not two.

source "$(dirname "$0")/lib.sh"
FAILED=0

ENTITY=facts
ROW_ID=$(psql_q "SELECT id FROM $ENTITY ORDER BY id LIMIT 1")
[ -n "$ROW_ID" ] || { echo "no $ENTITY rows to edit"; exit 1; }
ENCODED=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$ROW_ID")

hr "The record before"
psql_q "SELECT 'revision ' || revision || '  |  ' || left(title, 60) FROM $ENTITY WHERE id = '$ROW_ID'" \
  | sed 's/^/   /'

hr "Submitting a correction"
NEW_TITLE="$(psql_q "SELECT title FROM $ENTITY WHERE id = '$ROW_ID'") [corrected $(date -u +%H:%M:%SZ)]"
RESPONSE=$(curl -s -X PATCH "$API/records/$ENTITY/$ENCODED" \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"title": sys.argv[1]}))' "$NEW_TITLE")")
echo "$RESPONSE" | sed 's/^/   /'
MID=$(echo "$RESPONSE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["message_id"])')

note ""
note "202 Accepted, not 200 OK: the row is not written yet. It is fsynced into"
note "the API outbox and owed. That is the honest status code, and the UI says"
note "the same thing."

hr "Following it through"
if wait_stored "$MID" 60; then
  pass "the consumer stored it"
else
  fail "it never reached the database"
fi
note "$(trace "$MID")"

hr "The record after"
psql_q "SELECT 'revision ' || revision || '  |  ' || left(title, 60) FROM $ENTITY WHERE id = '$ROW_ID'" \
  | sed 's/^/   /'
REV=$(psql_q "SELECT revision FROM $ENTITY WHERE id = '$ROW_ID'")
[ "$REV" -ge 2 ] && pass "revision is now $REV" || fail "revision did not advance"

hr "The audit trail the database trigger wrote"
psql_q "SELECT '   rev ' || revision || ' at ' || changed_at || E'\n     was: ' || (old_row->>'title') || E'\n     now: ' || (new_row->>'title')
          FROM record_history WHERE entity = '$ENTITY' AND entity_id = '$ROW_ID'
         ORDER BY revision DESC LIMIT 3"
COUNT=$(psql_q "SELECT count(*) FROM record_history WHERE entity='$ENTITY' AND entity_id='$ROW_ID'")
[ "$COUNT" -ge 1 ] && pass "$COUNT history row(s) on record" || fail "no history was written"

hr "Visible through the API the UI reads"
curl -s "$API/records/$ENTITY/$ENCODED/history" \
  | python3 -c 'import json,sys; [print("   rev", r["revision"], "at", r["changed_at"]) for r in json.load(sys.stdin)]'

hr "Result"
if [ "$FAILED" -eq 0 ]; then
  printf '\033[32mThe update path works end to end, through the queue.\033[0m\n'
else
  printf '\033[31mSomething failed -- see above.\033[0m\n'
fi
exit "$FAILED"
