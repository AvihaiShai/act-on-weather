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
#   * and finally, that the same wording can be asked for on demand through
#     `POST /reenrich` -- M12's third update path, and the only one that needs
#     no connectivity at all
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

# ---------------------------------------------------------------------------
# Everything above happened by itself. M12 also requires a way to update stored
# information, and re-wording is the update path that needs no connectivity at
# all: the model, the queue and the database are all local. The request travels
# the same route as a fetched record -- API outbox, broker, consumer -- so it
# is traced here exactly like the M11 drills trace theirs.
# ---------------------------------------------------------------------------

hr "M12: re-wording a stored row on demand"

# A 'deferred' row is the interesting target: scored, charted and answerable,
# it was simply never sent to the model because it fell outside the top
# ENRICH_TOP_N activities for its day. Re-wording one shows both that
# include_deferred reaches those rows and that the row was complete without a
# sentence all along.
PICK="SELECT city_id || '|' || forecast_date || '|' || activity FROM recommendations"
ROW=$(psql_q "$PICK WHERE status = 'deferred' ORDER BY forecast_date, city_id, activity LIMIT 1")
INCLUDE_DEFERRED=true
if [ -z "$ROW" ]; then
  ROW=$(psql_q "$PICK WHERE status = 'ready' ORDER BY forecast_date, city_id, activity LIMIT 1")
  INCLUDE_DEFERRED=false
  note "nothing is deferred right now -- re-wording an already-worded row instead"
fi

if [ -z "$ROW" ]; then
  fail "there are no stored recommendations to re-word"
else
  R_REST=${ROW#*|}
  R_CITY=${ROW%%|*}
  R_DATE=${R_REST%%|*}
  R_ACT=${R_REST##*|}
  WHERE="city_id='$R_CITY' AND forecast_date='$R_DATE' AND activity='$R_ACT'"
  note "target: $R_CITY · $R_DATE · $R_ACT"

  # A second row on the same day in the same city. The request below names one
  # activity, so this one must not move. The assertion matters: an unscoped
  # re-enrichment would reset every deferred row in the system and re-create
  # the whole wording backlog.
  CONTROL=$(psql_q "SELECT activity FROM recommendations WHERE city_id='$R_CITY'
                      AND forecast_date='$R_DATE' AND activity <> '$R_ACT'
                      AND status = 'deferred' LIMIT 1")

  psql_q "SELECT '   before: status=' || status || '  score=' || coalesce(score::text,'-') ||
                 '  text=' || coalesce(left(text,44),'(none)') FROM recommendations WHERE $WHERE"
  SCORE_BEFORE=$(psql_q "SELECT coalesce(score::text,'') FROM recommendations WHERE $WHERE")

  MID2=$(curl -s -X POST "$API/reenrich" -H 'Content-Type: application/json' \
    -d '{"city":"'"$R_CITY"'","forecast_date":"'"$R_DATE"'","activity":"'"$R_ACT"'","include_deferred":'"$INCLUDE_DEFERRED"'}' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["message_id"])')
  note "accepted: $MID2"

  if wait_stored "$MID2" 60; then
    pass "the request was delivered and applied (traced $MID2)"
  else
    fail "the re-enrichment request never reached the database"
  fi

  STATE=$(psql_q "SELECT status FROM recommendations WHERE $WHERE")
  case "$STATE" in
    pending|ready) pass "the row is '$STATE' -- queued for the model, nothing re-scored" ;;
    *)             fail "the row is still '$STATE'" ;;
  esac

  if [ -n "$CONTROL" ]; then
    CONTROL_STATE=$(psql_q "SELECT status FROM recommendations WHERE city_id='$R_CITY'
                              AND forecast_date='$R_DATE' AND activity='$CONTROL'")
    if [ "$CONTROL_STATE" = "deferred" ]; then
      pass "the neighbouring row ($CONTROL) is untouched -- the request was scoped to one activity"
    else
      fail "a row nobody asked about moved to '$CONTROL_STATE'"
    fi
  fi

  note "waiting for the enricher to pick it up (it polls every ${ENRICH_POLL_SECONDS:-60}s,"
  note "then the model words it and the sentence travels back through the queue)"
  RE_TEXT=""
  for _ in $(seq 1 60); do
    RE_TEXT=$(psql_q "SELECT coalesce(text,'') FROM recommendations WHERE $WHERE")
    [ -n "$RE_TEXT" ] && break
    sleep 5
  done

  psql_q "SELECT '   after:  status=' || status || '  score=' || coalesce(score::text,'-') ||
                 '  model=' || coalesce(model,'-') FROM recommendations WHERE $WHERE"
  if [ -n "$RE_TEXT" ]; then
    pass "re-worded on request: $(printf '%.72s' "$RE_TEXT")..."
  else
    fail "the row was queued for wording but never worded"
  fi

  SCORE_AFTER=$(psql_q "SELECT coalesce(score::text,'') FROM recommendations WHERE $WHERE")
  if [ "$SCORE_AFTER" = "$SCORE_BEFORE" ]; then
    pass "the score is unchanged ($SCORE_AFTER/100) -- re-enrichment re-words, it does not re-score"
  else
    fail "the score moved from $SCORE_BEFORE to $SCORE_AFTER"
  fi
fi

hr "Result"
if [ "$FAILED" -eq 0 ]; then
  printf '\033[32mThe model is a presentation layer, not a dependency --\033[0m\n'
  printf '\033[32mand its wording can be asked for again at any time, offline.\033[0m\n'
else
  printf '\033[31mSomething failed -- see above.\033[0m\n'
fi
exit "$FAILED"
