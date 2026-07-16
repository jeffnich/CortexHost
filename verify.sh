#!/usr/bin/env bash
# Probe every Cortex service and print PASS/WARN/FAIL. Exit 0 only if no FAIL.
set -uo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "FAIL: no .env (run ./setup.sh)"; exit 1; }
get() { grep "^$1=" .env | cut -d= -f2-; }
QKEY=$(get QDRANT_API_KEY); MCPS=$(get CORTEX_MCP_PATH_SECRET)
QP=$(get QDRANT_PORT); QP=${QP:-6333}; MP=$(get MCP_PORT); MP=${MP:-8300}
BP=$(get BRIEF_PORT); BP=${BP:-8301}; MAPP=$(get MAP_PORT); MAPP=${MAPP:-8302}
BRS=$(get BRIEF_PATH_SECRET); MAPS=$(get MAP_PATH_SECRET); DEMOS=$(get DEMO_PATH_SECRET)
fail=0

probe() { # name url expect_warming_ok
  local name=$1 url=$2 warmok=${3:-no}
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$url" 2>/dev/null)
  if [ "$code" = "200" ]; then echo "PASS  $name ($code)"
  elif [ "$code" = "503" ] && [ "$warmok" = "yes" ]; then echo "WARN  $name (503 warming - fine until first data/render)"
  else echo "FAIL  $name (HTTP ${code:-none})"; fail=1; fi
}

code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 -H "api-key: $QKEY" "http://localhost:$QP/collections" 2>/dev/null)
if [ "$code" = "200" ]; then echo "PASS  qdrant ($code)"; else echo "FAIL  qdrant (HTTP ${code:-none})"; fail=1; fi

# MCP is a protocol endpoint: 406/400 on a browser-style GET means "alive,
# speak MCP to me". Only 000/404/5xx are failures (404 = wrong secret).
mcode=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "http://localhost:$MP/$MCPS/mcp" 2>/dev/null)
case "$mcode" in
  2*|400|405|406) echo "PASS  mcp      ($mcode - MCP endpoint alive)";;
  404) echo "FAIL  mcp      (404 - secret path mismatch)"; fail=1;;
  *) echo "FAIL  mcp      (HTTP ${mcode:-none})"; fail=1;;
esac
probe "brief   " "http://localhost:$BP/$BRS/brief.html" yes
probe "map     " "http://localhost:$MAPP/$MAPS/map.html" yes
probe "demo    " "http://localhost:$MAPP/$DEMOS/demo.html" yes

exit $fail
