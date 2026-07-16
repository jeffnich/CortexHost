# AGENTS.md — runbook for AI coding agents

You (Claude Code, Cursor, or any coding agent) are installing **Cortex**, a
self-hosted second brain: Qdrant + three small Python services (MCP memory
server, daily brief, galaxy map), plus local capture scripts. Everything runs
from docker compose. The human you're helping wants a working brain, not a
tour — execute, verify, and only ask when a step genuinely needs their input.

## Definition of done

1. `docker compose ps` shows qdrant, mcp, brief, map running.
2. `./verify.sh` prints no FAIL (WARN on brief/map is fine pre-data).
3. The user's AI client is connected to the MCP endpoint and a
   `search_memory` call answers (empty results are fine on a fresh corpus).
4. Ideally: their ChatGPT export is imported and `memory_stats` shows counts.

## Steps

1. **Prereqs.** `docker info` must succeed (else have the user start Docker
   Desktop). `python3 --version` >= 3.10.
2. **Run `./setup.sh`.** It copies `.env.example` -> `.env`, generates all
   secrets, asks for the OpenAI key (the ONE thing you must get from the
   user — never invent or reuse one from elsewhere), builds and boots the
   stack, waits, and runs verify. Non-interactive shells: set
   `OPENAI_API_KEY` in `.env` yourself first, then re-run.
3. **Verify.** `./verify.sh`. Debug FAILs before moving on (see below).
4. **Connect the user's client** (ask which they use):
   - **Claude Code**: `claude mcp add cortex --transport http "http://localhost:8300/<CORTEX_MCP_PATH_SECRET>/mcp"`
   - **Cursor**: write `.cursor/mcp.json`:
     `{"mcpServers": {"cortex": {"url": "http://localhost:8300/<secret>/mcp"}}}`
   - **claude.ai / Claude Desktop**: needs a public URL (deploy first, or
     tunnel); local-only installs should use Claude Code or Cursor.
   Then test: call `memory_stats` / `search_memory` through the client.
5. **Import history** (the payoff step):
   - `pip install -r capture/requirements.txt`
   - ChatGPT: user downloads their export (Settings -> Data controls ->
     Export), place `conversations.json` in `uploads/`, run
     `python3 capture/distill_chatgpt_export.py --execute`
     (resumable; state in `.cortex_chatgpt_distill_state.json`).
   - Claude Code history: `python3 capture/backfill_claude_sessions.py --execute`
   - Obsidian: set `OBSIDIAN_VAULT` + `OBSIDIAN_INCLUDE` in `.env` first
     (ONLY listed folders are read — ask the user which folders are
     personal), then `python3 capture/ingest_obsidian.py --execute`.
6. **Personalize.** Fill `CORTEX_USER_NAME`, `CORTEX_USER_CONTEXT`,
   `CORTEX_USER_GOALS`, `CORTEX_EXCLUDE`, `CORTEX_TZ` in `.env` (ask the
   user; 1-2 sentences is plenty), then `docker compose up -d` to reload.
7. **Ongoing capture** (optional now): see `docs/capture-setup.md` for the
   Claude Code SessionEnd hook and scheduled Obsidian sync.

## Known failure modes

| Symptom | Cause / fix |
|---|---|
| `docker compose` port conflict on 6333/8300-8302 | Set `QDRANT_PORT` / `MCP_PORT` / `BRIEF_PORT` / `MAP_PORT` in `.env` to free ports and re-run `./setup.sh`. |
| `ensurepip` fails creating a venv | Python 3.14 quirk on some Macs; use `python3.12 -m venv`. |
| brief/map return 503 | Normal while warming (brief ~1 min; map needs data + first UMAP). Re-check after import. |
| map stays "warming" forever with data present | Check `docker compose logs map` for `regen error`; usually a bad `OPENAI_API_KEY` (cluster labeling) or empty corpus. |
| capture scripts: `CORTEX_MCP_URL` errors | Set it in `.env` to `http://localhost:8300/<CORTEX_MCP_PATH_SECRET>/mcp`. |
| embeddings errors everywhere | Invalid/missing `OPENAI_API_KEY`, or no billing on the OpenAI account. |

## Security invariants (do not weaken)

- `.env` is gitignored; never commit it or print its values into chat.
- The URL path secrets ARE the credentials. Don't post full URLs anywhere
  public, don't shorten the secrets, don't disable them.
- `CORTEX_EXCLUDE` and `OBSIDIAN_INCLUDE` implement the user's privacy
  boundary (two-zone capture). Never widen them without being asked.
- `MAP_PASSWORD` gates the full map; leave the demo/full split intact.
