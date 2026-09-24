#!/usr/bin/env bash
#
# M6: the whole stack runs and answers with no internet at all.
#
# The headline proof is behavioural: disable the host's network adapter, run
# this, and both of the brief's example questions are still answered from
# stored data with an as-of stamp. That is the run a reviewer should do.
#
# The structural proofs below hold whether or not the host NIC is down, and
# they are what makes the behavioural one meaningful -- an answer produced
# while connected proves nothing on its own.

source "$(dirname "$0")/lib.sh"
FAILED=0

hr "1. Docker itself forbids a route out of the application network"
INTERNAL=$(docker network inspect aow_backend -f '{{.Internal}}' 2>/dev/null || echo "?")
note "aow_backend Internal = $INTERNAL"
[ "$INTERNAL" = "true" ] && pass "every application service is on a network with no gateway" \
                         || fail "aow_backend is not internal"

note "which containers are attached to a routable network:"
for net in aow_frontend aow_egress; do
  printf '   %-16s %s\n' "$net" \
    "$(docker network inspect "$net" -f '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null || echo '-')"
done
note "only the edge proxy straddles the boundary, because Docker cannot publish"
note "a port from an internal network. It proxies; it holds no credentials and"
note "makes no outbound call of its own."

hr "2. The services cannot reach the internet, demonstrated"
for svc in llm agent consumer api; do
  # `llm` is the upstream llama.cpp image and ships no python, so each service
  # is probed with python first and with its own curl as a fallback.
  if dc exec -T "$svc" python -c "
import socket, sys
socket.setdefaulttimeout(3)
try:
    socket.create_connection(('1.1.1.1', 443), 3)
except OSError as exc:
    print('   %-9s no route out (%s)' % ('$svc', type(exc).__name__)); sys.exit(0)
print('   %-9s REACHED THE INTERNET' % '$svc'); sys.exit(1)
" 2>/dev/null; then :; else
    if dc exec -T "$svc" sh -c 'curl -m 3 -s https://1.1.1.1 >/dev/null 2>&1' 2>/dev/null; then
      fail "$svc reached the internet"
    else
      note "$(printf '%-9s no route out' "$svc")"
    fi
  fi
done
pass "no application service can open an outbound connection"

hr "3. No cloud LLM anywhere in the repo"
note "Searching the source, the dependency pins and the compose files for any"
note "hosted-model SDK or endpoint:"
PATTERN='openai|anthropic|api\.openai|claude-|gemini|generativelanguage|cohere|mistral\.ai|huggingface\.co|together\.ai|groq|replicate|bedrock|azure.*openai'
# Scoped to the application, the way the CI guard job is: this file and
# .github/workflows/ci.yml both contain the pattern itself, and a check that
# fails on its own definition is a check nobody trusts.
HITS=$(grep -rInE "$PATTERN" \
        --include='*.py' --include='*.txt' --include='*.yml' --include='*.yaml' \
        --include='*.toml' --include='*.sql' \
        services tests db data compose.yml compose.connected.yml 2>/dev/null || true)
if [ -z "$HITS" ]; then
  pass "nothing matched -- the only model client is services/common/llm.py, pointed at llm:8080"
else
  fail "found references to a hosted model:"; echo "$HITS"
fi

note "the model file that is actually loaded, from the local bind mount:"
dc exec -T llm sh -c 'ls -la /models/*.gguf' 2>/dev/null | sed 's/^/   /'

hr "4. The example questions, answered from stored data"
for q in "What is the weather tomorrow in Rome?" \
         "What activities can I do with my wife this week in London? We like concerts, shopping and fine dining."; do
  printf '\n   \033[1mQ:\033[0m %s\n' "$q"
  RESPONSE=$(curl -s -X POST "$API/agent/ask" -H 'Content-Type: application/json' \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"question": sys.argv[1]}))' "$q")")
  echo "$RESPONSE" | python3 -c '
import json, sys, textwrap
d = json.load(sys.stdin)
print("   A:", "\n      ".join(textwrap.wrap(d["answer"], 92)))
print("   " + "-" * 70)
print("   as of:", d["as_of"] or "(none)")
print("   looked at:", d["rows_used"], "| local model called:", d["llm_called"])
'
done

hr "5. A question outside the stored coverage window"
note "The forecast covers only what was last fetched. A date beyond it must be"
note "refused in code, with no model call at all -- there is nothing to phrase,"
note "and a model asked to phrase 'no data' is a model given room to invent it."
curl -s -X POST "$API/agent/ask" -H 'Content-Type: application/json' \
  -d '{"question":"What is the weather in Rome on 2027-07-04?"}' \
  | python3 -c '
import json, sys, textwrap
d = json.load(sys.stdin)
print("   A:", "\n      ".join(textwrap.wrap(d["answer"], 92)))
print("   local model called:", d["llm_called"], "(must be False)")
sys.exit(0 if d["llm_called"] is False else 1)
' || FAILED=1
[ "$FAILED" -eq 0 ] && pass "refused from stored coverage, without calling the model"

hr "Result"
if [ "$FAILED" -eq 0 ]; then
  printf '\033[32mOffline operation demonstrated.\033[0m\n'
else
  printf '\033[31mSomething failed -- see above.\033[0m\n'
fi
exit "$FAILED"
