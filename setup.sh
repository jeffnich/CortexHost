#!/usr/bin/env bash
# Cortex one-shot setup: env scaffold -> secrets -> boot stack -> verify.
# Safe to re-run; it only fills blanks, never overwrites values you set.
set -euo pipefail
cd "$(dirname "$0")"

command -v docker >/dev/null || { echo "ERROR: docker not installed. Install Docker Desktop first."; exit 1; }
docker info >/dev/null 2>&1 || { echo "ERROR: docker daemon not running. Start Docker and re-run."; exit 1; }

[ -f .env ] || { cp .env.example .env; echo "created .env from .env.example"; }

gen() { python3 -c "import secrets; print(secrets.token_urlsafe(32))"; }
fill() { # fill KEY only if currently blank
  if grep -qE "^$1=$" .env; then
    v="$2"
    python3 - "$1" "$v" <<'PY'
import sys, pathlib
k, v = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env"); t = p.read_text()
p.write_text(t.replace(f"{k}=\n", f"{k}={v}\n", 1))
PY
    echo "  set $1"
  fi
}

echo "filling blank secrets..."
for k in QDRANT_API_KEY CORTEX_MCP_PATH_SECRET BRIEF_PATH_SECRET MAP_PATH_SECRET DEMO_PATH_SECRET; do
  fill "$k" "$(gen)"
done

if grep -qE "^OPENAI_API_KEY=$" .env; then
  if [ -t 0 ]; then
    read -rp "OpenAI API key (sk-...): " OKEY
    fill OPENAI_API_KEY "$OKEY"
  else
    echo "NOTE: OPENAI_API_KEY is blank in .env - set it, then re-run ./setup.sh"
    exit 1
  fi
fi

echo "booting the stack (first build takes a few minutes)..."
docker compose up -d --build

SECRET=$(grep '^CORTEX_MCP_PATH_SECRET=' .env | cut -d= -f2)
MPORT=$(grep '^MCP_PORT=' .env | cut -d= -f2); MPORT=${MPORT:-8300}
MAPPORT=$(grep '^MAP_PORT=' .env | cut -d= -f2); MAPPORT=${MAPPORT:-8302}
printf "waiting for the MCP server"
for i in $(seq 1 45); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://localhost:$MPORT/$SECRET/mcp" 2>/dev/null)
  case "$code" in 2*|4*) echo " - up (HTTP $code)."; break;; esac
  printf "."; sleep 2
done

./verify.sh || true

cat <<EOF

Connect your AI client to the memory server:

  Claude Code:
    claude mcp add cortex --transport http "http://localhost:$MPORT/$SECRET/mcp"

  Cursor (.cursor/mcp.json in any project):
    {"mcpServers": {"cortex": {"url": "http://localhost:$MPORT/$SECRET/mcp"}}}

Then import your history (makes it feel alive on day one):
  1. ChatGPT: Settings -> Data controls -> Export -> put conversations.json in uploads/
  2. pip install -r capture/requirements.txt
  3. python3 capture/distill_chatgpt_export.py --execute

The map at http://localhost:$MAPPORT/<MAP_PATH_SECRET>/map.html shows "warming up"
until memories exist and the first projection runs (a few minutes after import).
EOF
