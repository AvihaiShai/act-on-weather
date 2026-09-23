#!/usr/bin/env bash
#
# The local model is not on the critical path.
#
# What this shows, in order:
#   * with `llm` stopped, weather is still accepted, still stored, and still
#     scored -- because the score comes from the rule engine, not the model
#   * the rows simply sit `status='pending'`; nothing fails and nothing is lost
#   * a temporary outage consumes no retry attempt, so however long it lasts,
#     no row is ever marked failed because of it
#   * when `llm` comes back, the rows are worded with no manual step
#
# This is the concrete form of "LLM failure never blocks or loses weather
# data": the deterministic score is the product, and the model's sentence is a
# presentation layer over it.

source "$(dirname "$0")/lib.sh"
FAILED=0

hr "Before"
psql_q "SELECT '   ' || status || ': ' || count(*) FROM recommendations GROUP BY status ORDER BY status"

hr "Stopping the local model"
dc stop llm >/dev/null 2>&1
note "llm stopped"

hr "Asking for an activity while the model is down"
DAY=$(psql_q "SELECT min(forecast_date) FROM weather_daily")
MID=$(curl -s -X POST "$API/recommendations" -H 'Content-Type: application/json' \
  -d '{"city":"rome","forecast_date":"'"$DAY"'","activity":"an outage-time picnic"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["message_id"])')
note "accepted: $MID"

if wait_stored "$MID" 60; then
  pass "stored while the model was down"
else
  fail "the request never reached the database"
fi

psql_q "SELECT '   activity=' || activity || '  score=' || coalesce(score::text,'-') ||
               '  band=' || coalesce(band,'-') || '  status=' || status
          FROM recommendations WHERE activity = 'an_outage_time_picnic'"
SCORE=$(psql_q "SELECT coalesce(score::text,'') FROM recommendations WHERE activity='an_outage_time_picnic'")
[ -n "$SCORE" ] && pass "it already has its score ($SCORE/100) -- the rule engine produced that, not the model" \
                || fail "no score was computed"

hr "The agent, with no model to phrase with"
curl -s -X POST "$API/agent/ask" -H 'Content-Type: application/json' \
  -d '{"question":"What is the weather tomorrow in Rome?"}' \
  | python3 -c '
import json, sys, textwrap
d = json.load(sys.stdin)
print("   A:", "\n      ".join(textwrap.wrap(d["answer"], 92)))
print("   note:", d.get("note", "(none)"))
print("   local model called:", d["llm_called"])
'
note "It still answers, from the same rows, rendered by code instead of prose."

hr "What the enricher is doing meanwhile"
dc logs enricher --tail 4 2>&1 | sed 's/^/   /'
note "It retries and waits. A connection failure is not counted as an attempt,"
note "so no row can be marked failed by an outage, however long it lasts."

PENDING_BEFORE=$(psql_q "SELECT count(*) FROM recommendations WHERE status='pending'")
FAILED_ROWS=$(psql_q "SELECT count(*) FROM recommendations WHERE status='failed' AND last_error ILIKE '%connect%'")
note "pending: $PENDING_BEFORE · rows failed by the outage: $FAILED_ROWS (must be 0)"
[ "$FAILED_ROWS" = "0" ] && pass "the outage stranded nothing" || fail "rows were failed by a temporary outage"

hr "Restarting the local model"
dc start llm >/dev/null 2>&1
note "waiting for it to load the model (up to ~3 minutes on CPU)"
for _ in $(seq 1 60); do
  dc exec -T llm sh -c 'curl -sf http://127.0.0.1:8080/health >/dev/null' 2>/dev/null && break
  sleep 5
done
note "model healthy"

note "waiting for the row to be worded -- no manual step is taken here"
for _ in $(seq 1 60); do
  TEXT=$(psql_q "SELECT coalesce(text,'') FROM recommendations WHERE activity='an_outage_time_picnic'")
  [ -n "$TEXT" ] && break
  sleep 5
done

hr "After"
psql_q "SELECT '   status=' || status || E'\n   text=' || coalesce(text,'(none)')
          FROM recommendations WHERE activity = 'an_outage_time_picnic'"
if [ -n "$TEXT" ]; then
  pass "reworded automatically once the model returned"
else
  fail "the row is still unworded"
fi
psql_q "SELECT '   ' || status || ': ' || count(*) FROM recommendations GROUP BY status ORDER BY status"

hr "Result"
if [ "$FAILED" -eq 0 ]; then
  printf '\033[32mThe model is a presentation layer, not a dependency.\033[0m\n'
else
  printf '\033[31mSomething failed -- see above.\033[0m\n'
fi
exit "$FAILED"
