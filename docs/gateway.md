# Agent gateway

A public front door that lets **other people's agents** interact with yours,
without ever touching your memory corpus.

```
visitor's agent ──MCP──► /mcp  (public tier)
                          ask_about_owner   ← answers only from your public profile
                          request_meeting   ┐
                          leave_message     ├► request queue (SQLite) ──► you approve
                          check_request     ┘
you ──MCP──► /<GATEWAY_OWNER_SECRET>/mcp  (owner tier)
                          list_requests · resolve_request
                          gateway_activity · preview_public_profile
```

## Why it is safe to expose

The guardrail is **what doesn't exist**, not what a prompt says.

- The gateway has no Qdrant URL or key and no OpenAI key. No input, however
  adversarial, can reach `search_memory` through it, because there is no path.
  A test fails the build if those credentials ever appear in its code.
- Public answers are whole sections of a markdown file you wrote
  (`gateway-profile.md`). No model generates them, so nothing is inferred
  from private context. HTML comments in the file are stripped.
- Owner tools are a separate MCP server on a secret path. They never appear
  in a public session's tool list.
- Nothing commits you. Meetings and messages are queued; the requester gets a
  `request_id` + `status_token` and sees your reply only after you resolve it.
- Visitor text is normalized (invisible and bidi characters stripped, lengths
  capped) and shown to you under an `untrusted` key. Treat it as data.
- Every call lands in an audit log with a trace id, the calling app's label,
  and a salted hash of its IP (raw IPs are never stored).
- Rate limits: 60 calls per 10 min and 5 queue writes per hour per IP, 100
  queue writes per day overall, 200 pending requests max. All tunable via env.
- The public endpoint is stateless: no server-side sessions to exhaust.

## Run it

Locally it is part of `docker compose up` on port 8303. Write your profile:

```bash
cp services/gateway/profile.example.md gateway-profile.md   # setup.sh does this
$EDITOR gateway-profile.md
docker compose up -d gateway
```

On Railway: new service from `services/gateway`, attach a volume at `/data`,
and set `GATEWAY_OWNER_SECRET`, `CORTEX_USER_NAME`, and `GATEWAY_PROFILE`
(the profile markdown inline). Do **not** give it `QDRANT_*` or
`OPENAI_API_KEY`.

## Connect

- Visitors: `https://<gateway-host>/mcp`. Add `?client=<name>` so the audit
  log can tell apps apart (e.g. `/mcp?client=duet`).
- You: add `https://<gateway-host>/<GATEWAY_OWNER_SECRET>/mcp` as a connector
  and ask "what's in my gateway queue?"

## Tests

```bash
cd services/gateway && pip install -r requirements.txt pytest && python -m pytest -q
```

## Roadmap

Phase 2 adds OAuth grants and a per-project collaborator tier, so a
coworker's agent can read context you publish for one shared project.
Phase 3 adds A2A tasks for long-running work that waits on your approval.
