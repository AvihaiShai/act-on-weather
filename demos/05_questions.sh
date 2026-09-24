#!/usr/bin/env bash
#
# M7, M8: the agent's breadth, and its limits.
#
# The last three questions matter more than the first four. Anything can answer
# a question it has data for; what a reviewer should check is what happens when
# it does not -- a date beyond the forecast, a city the system never collected,
# and a category with no rows.
#
# This script used to be narration: it printed questions and answers and ended
# with a prose note claiming the edge cases were refused without calling the
# model. Printing an answer is not checking it, and the claim was not quite
# true (see the closing note). So the narration stayed -- the answers are worth
# reading -- but every claim it makes is now an assertion that fails the script.

source "$(dirname "$0")/lib.sh"
FAILED=0

# Each `ask` leaves its parsed response here for the expectations that follow
# it. One file, overwritten per question: an expectation always refers to the
# question immediately above it, which is also how it reads.
LAST="$(mktemp)"
trap 'rm -f "$LAST"' EXIT

ask() {
  printf '\n\033[1m%s\033[0m\n' "Q: $1"
  START=$(date +%s)
  curl -s -X POST "$API/agent/ask" -H 'Content-Type: application/json' \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"question": sys.argv[1]}))' "$1")" \
    > "$LAST"
  python3 -c '
import json, sys, textwrap
d = json.load(open(sys.argv[1]))
# Wrapped line by line, not as one paragraph: several answers are lists of
# dates or places, and collapsing them into prose is not what was sent.
wrapped = [w for line in d["answer"].splitlines() for w in (textwrap.wrap(line, 92) or [""])]
print("A:", "\n   ".join(wrapped))
if d.get("note"):
    print("!  ", d["note"])
print("   " + (d["as_of"] or "no dated data behind this"))
print("   city=%s dates=%s intents=%s model_called=%s rows=%s"
      % (d["city"], d["dates"], ",".join(d["intents"]), d["llm_called"], d["rows_used"]))
' "$LAST"
  printf '   took %ss\n' "$(( $(date +%s) - START ))"

  # Every question, unconditionally: a reply at all, and a non-empty answer.
  # A curl that returned nothing, or an answer of "", would otherwise scroll
  # past as a blank line under a bold question.
  if python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
sys.exit(0 if isinstance(d.get("answer"), str) and d["answer"].strip() else 1)
' "$LAST" 2>/dev/null; then
    :
  else
    fail "no usable answer came back for: $1"
  fi
}

# The edge cases: refused in code, before the model is called. `llm_called` is
# the load-bearing field -- it is the difference between "the system knew it
# had nothing" and "the model was asked and happened to say so".
expect_refused_in_code() {
  if python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
rows = d["rows_used"]
used = sum(rows.values()) if isinstance(rows, dict) else rows
sys.exit(0 if d["llm_called"] is False and used == 0 else 1)
' "$LAST" 2>/dev/null; then
    pass "$1 -- refused with no model call and no rows"
  else
    fail "$1 -- expected a code-side refusal with no model call and no rows"
  fi
}

# The answerable ones: whatever the wording, the reply has to be standing on
# retrieved rows of the named kind, and has to carry an as-of stamp. This is
# the M7/M8 claim that an answer is grounded in stored, dated data.
expect_grounded_in() {
  local kind="$1" label="$2"
  if python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
rows = d["rows_used"]
used = rows.get(sys.argv[2], 0) if isinstance(rows, dict) else rows
sys.exit(0 if used > 0 and (d["as_of"] or "").strip() else 1)
' "$LAST" "$kind" 2>/dev/null; then
    pass "$label -- answered from stored $kind rows, with an as-of stamp"
  else
    fail "$label -- expected at least one stored $kind row and an as-of stamp"
  fi
}

hr "The brief's two example questions"
ask "What is the weather tomorrow in Rome?"
expect_grounded_in forecast "E1 Rome tomorrow"
ask "What activities can I do with my wife this week in London? We like concerts, shopping and fine dining."
expect_grounded_in recommendations "E2 London this week"

hr "Breadth"
ask "Tell me about the history of Lisbon."
expect_grounded_in facts "Lisbon history"
ask "Are there any sports events in London this month?"
ask "Is this weekend good for running in Reykjavik?"
expect_grounded_in recommendations "Reykjavik running"

hr "Where, and what it will not locate"
# A place word routes to locations, not to the forecast. The second question is
# the one that matters: Tel Aviv has beaches on record and none of them is
# recorded as having rideable surf, so none is offered as an answer.
ask "Where can I go to the beach in Tel Aviv?"
# `venues`, not `places`: a "where" question is answered by the venue route,
# which renders the rows in code and returns before the model is called. The
# two counters are separate on purpose, and asserting the wrong one here is
# how this expectation first failed.
expect_grounded_in venues "Tel Aviv beaches"
ask "Where can I surf in Tel Aviv?"
ask "Where and when can I surf in Tel Aviv this week?"

hr "The edges -- where it must say no"
ask "What is the weather in Rome on 2027-07-04?"
expect_refused_in_code "a date past the forecast window"
ask "What can I do in Buenos Aires tomorrow?"
expect_refused_in_code "a city the system never collected"
ask "Which ski resorts are near Tel Aviv?"
# Deliberately NOT expect_refused_in_code. This one does reach the model: the
# city is known and the place lookup returns rows, they are simply not ski
# resorts. The honest claim here is narrower -- the answer is built from rows
# that were actually retrieved, and the grounding check is what stops those
# rows being described as something they are not.
expect_grounded_in places "a category with no rows, in a city that has other rows"

printf '\n\033[1mNote\033[0m\n'
printf '   Two of the three edge questions were answered without calling the\n'
printf '   model at all: the coverage gate rejected the out-of-window date and\n'
printf '   the city lookup rejected the unknown city, both before any model\n'
printf '   call. The third reached the model, because Tel Aviv is a known city\n'
printf '   with places on record -- just no ski resorts. That is the design:\n'
printf '   code decides whether there is an answer, and the model is only ever\n'
printf '   asked to phrase rows that were actually retrieved.\n'

if [ "$FAILED" -eq 0 ]; then
  printf '\n\033[32mEvery expectation above held.\033[0m\n'
else
  printf '\n\033[31mSomething failed -- see above.\033[0m\n'
fi
exit "$FAILED"
