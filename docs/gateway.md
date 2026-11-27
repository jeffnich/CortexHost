# Agent gateway

A public front door that lets **other people's agents** interact with yours,
without ever touching your memory corpus.

```
visitor's agent ──MCP──► /mcp  (public tier)
                          ask_about_owner   ← answers only from your public profile
                          get_owner_take    ← answers only from your approved takes file
                          check_availability← free/busy blocks from your calendar feed
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

## Tools and permission levels

Every public response carries `permission`: `auto` (answered from material
you approved) or `queued` (filed for your decision).

| Tool | Permission | Source |
| --- | --- | --- |
| ask_about_owner | auto | `GATEWAY_PROFILE`, verbatim |
| get_owner_take | auto | `GATEWAY_TAKES`, verbatim; no match = "no approved take", never improvised |
| check_availability | auto | `GATEWAY_FREEBUSY_ICS_URL`, start/end times only |
| request_meeting | queued | visitor input |
| leave_message | queued | visitor input |
| check_request | auto | the requester's own request, token-gated |

### Availability feed

Use a feed that is free/busy only at the source. In Google Calendar: Settings
for your calendar → Access permissions → "Make available to public" with
"See only free/busy (hide details)", then copy the public iCal address into
`GATEWAY_FREEBUSY_ICS_URL`. The gateway also discards titles, attendees and
locations itself, but a free/busy-only feed means a leaked URL reveals nothing
either. Avoid the "secret address", which carries full event details.
`CORTEX_TZ`, `GATEWAY_WORK_HOURS` (default 09:00-17:00) and
`GATEWAY_WORK_DAYS` (default mon-fri) shape the answer.

## Instant pings

Every new meeting request or message pings the owner with
"name: subject · request id". Pick one channel:

- **Email (recommended).** Set `GATEWAY_NOTIFY_EMAIL` and
  `GATEWAY_RESEND_API_KEY`. One Resend API call sends a plain-text email
  (no HTML part) to your inbox, so the ping lands in the mail app you
  already have. The default sender `onboarding@resend.dev` needs no domain
  but only delivers to your Resend account's own address. Set
  `GATEWAY_NOTIFY_FROM` once you verify a domain. The subject is fixed text
  plus the request ID, never visitor text.
- **ntfy.** Set `GATEWAY_NTFY_URL` to `https://ntfy.sh/<long random topic>`
  and subscribe to it in the ntfy app. The topic name is the secret.

Email wins if both are set. Visitor text only goes in the body, as plain
text, with links neutered and length capped. Contact details and message
bodies are never sent. After `GATEWAY_PINGS_PER_HOUR` (default 10) pings in
an hour you get one "paused" notice, then nothing until the hour rolls over.
Requests still queue normally.

Why not push into the Claude app? It has no inbound notification API. A
Claude Code routine can be fired over HTTP, but every ping would start an
agent session (usage cost, and visitor text in an agent's context), which
is the wrong tool for a doorbell.

## Rate limits and hosted agents

Hosted agents (claude.ai's connector proxy and similar) rotate through a pool
of IPs, so limits key on the caller's network (/24 IPv4, /48 IPv6) rather
than the exact address. The catch: every visitor coming through the same
hosted proxy shares one budget. Per-visitor limits arrive with phase 2
OAuth grants, which give each caller a real identity.

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
