#!/usr/bin/env bash
# Probe every Cortex service and print PASS/WARN/FAIL. Exit 0 only if no FAIL.
set -uo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "FAIL: no .env (run ./setup.sh)"; exit 1; }
get() { grep "^$1=" .env | cut -d= -f2-; }
QKEY=$(get QDRANT_API_KEY); MCPS=$(get CORTEX_MCP_PATH_SECRET)
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

code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 -H "api-key: $QKEY" "http://localhost:6333/collections" 2>/dev/null)
if [ "$code" = "200" ]; then echo "PASS  qdrant ($code)"; else echo "FAIL  qdrant (HTTP ${code:-none})"; fail=1; fi

probe "mcp     " "http://localhost:8300/$MCPS/mcp"
probe "brief   " "http://localhost:8301/$BRS/brief.html" yes
probe "map     " "http://localhost:8302/$MAPS/map.html" yes
probe "demo    " "http://localhost:8302/$DEMOS/demo.html" yes

exit $fail
