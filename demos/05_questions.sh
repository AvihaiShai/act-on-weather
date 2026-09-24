#!/usr/bin/env bash
#
# M7, M8: the agent's breadth, and its limits.
#
# The last three questions matter more than the first four. Anything can answer
# a question it has data for; what a reviewer should check is what happens when
# it does not -- a date beyond the forecast, a city the system never collected,
# and a category with no rows. Each of those is refused in code, before the
# model is called, so there is no opportunity to invent an answer.

source "$(dirname "$0")/lib.sh"

ask() {
  printf '\n\033[1m%s\033[0m\n' "Q: $1"
  START=$(date +%s)
  curl -s -X POST "$API/agent/ask" -H 'Content-Type: application/json' \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"question": sys.argv[1]}))' "$1")" \
    | python3 -c '
import json, sys, textwrap
d = json.load(sys.stdin)
# Wrapped line by line, not as one paragraph: several answers are lists of
# dates or places, and collapsing them into prose is not what was sent.
wrapped = [w for line in d["answer"].splitlines() for w in (textwrap.wrap(line, 92) or [""])]
print("A:", "\n   ".join(wrapped))
if d.get("note"):
    print("!  ", d["note"])
print("   " + (d["as_of"] or "no dated data behind this"))
print("   city=%s dates=%s intents=%s model_called=%s rows=%s"
      % (d["city"], d["dates"], ",".join(d["intents"]), d["llm_called"], d["rows_used"]))
'
  printf '   took %ss\n' "$(( $(date +%s) - START ))"
}

hr "The brief's two example questions"
ask "What is the weather tomorrow in Rome?"
ask "What activities can I do with my wife this week in London? We like concerts, shopping and fine dining."

hr "Breadth"
ask "Tell me about the history of Lisbon."
ask "Are there any sports events in London this month?"
ask "Is this weekend good for running in Reykjavik?"

hr "Where, and what it will not locate"
# A place word routes to locations, not to the forecast. The second question is
# the one that matters: Tel Aviv has five beaches on record and none of them is
# recorded as having rideable surf, so none is offered as an answer.
ask "Where can I go to the beach in Tel Aviv?"
ask "Where can I surf in Tel Aviv?"
ask "Where and when can I surf in Tel Aviv this week?"

hr "The edges -- where it must say no"
ask "What is the weather in Rome on 2027-07-04?"
ask "What can I do in Buenos Aires tomorrow?"
ask "Which ski resorts are near Tel Aviv?"

printf '\n\033[1mNote\033[0m\n'
printf '   The three questions above were answered without calling the model at\n'
printf '   all where the coverage gate or the city lookup rejected them. That is\n'
printf '   the design: code decides whether there is an answer, and the model is\n'
printf '   only ever asked to phrase rows that were actually retrieved.\n'
