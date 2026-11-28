# Owner dashboard

A private web page for the person who owns this Cortex. It answers four
questions: what is waiting on me, what happened, what can strangers see, and
which apps touched my memory. It replaces the old galaxy map.

URL: `https://<gateway-host>/<GATEWAY_OWNER_SECRET>/` (trailing slash). The
owner secret in the path is the only lock, same as the owner MCP tier, so
treat the link like a password. The page loads nothing from third parties,
sends no Referer, and refuses to be framed.

## Screens

| Screen | Shows | Can change |
|---|---|---|
| Inbox | Pending meeting requests and messages, your triage agent's verdict, summary and draft reply. Visitor text is marked unverified and shown as plain text. | Approve, decline or mark done, with a reply the requester sees via `check_request`. |
| Activity | Everything that crossed the gateway: visitor calls, triage verdicts, your decisions and edits, outbound `ask_agent` calls. 14-day chart by who started it. Filter by lane or problems only. | Nothing. |
| Public view | Every public tool and whether it answers now or waits for you; what never crosses; "ask as a stranger" preview; the live profile and takes. | Edit and publish the profile and takes. Every save is kept as a version and wins over the env var. |
| Access log | From Cortex MCP: every read and write, by app, with memory ids, plus a per-app summary. | Nothing. |

Across the top: live integrity checks. The audit log and outbound log are
hash-chained in SQLite, so an edited, deleted or reordered row shows as a
broken chain. Audit rows written before chaining shipped are counted as
"older unchained" and skipped.

## Memory access log

Cortex MCP logs every tool call (search, recent, brief, store, to-dos) to a
hash-chained SQLite file and serves it at
`https://<mcp-host>/<CORTEX_MCP_PATH_SECRET>/access.json`. In the Access log
tab, paste that URL once. It is stored in your browser only: the browser
reads the feed directly, so the public gateway never learns where the memory
service lives and never holds its secret.

MCP env:

- `CORTEX_ACCESS_DB`: SQLite path. Default `/data/access.db` if `/data`
  exists, else `./access.db`. On Railway, attach a volume at `/data` or the
  log resets on each deploy.
- `CORTEX_ACCESS_CORS_ORIGIN`: which page origin may read the feed. Default
  `*`, since the secret path is the boundary; set it to your gateway's origin
  to narrow it.

## Build

The gateway Dockerfile builds the React + Tailwind app in a node stage and
copies only the static files into the Python image. Locally:

```bash
cd services/gateway/dashboard && npm ci && npm run build   # writes dist/
cd .. && python server.py                                   # serves it
```

For hot reload, run the gateway on :8080 and
`GATEWAY_DEV=http://localhost:8080/<secret> npm run dev`.
