# Ongoing capture

One-time backfills live in the README quickstart. This doc wires the
*ongoing* rails so new thinking flows in without you doing anything.

All scripts read `.env` at the repo root (`CORTEX_MCP_URL` must point at your
MCP service, secret included).

## Claude Code sessions (auto-capture on session end)

Add a SessionEnd hook to `~/.claude/settings.json` so every session is
distilled into sparks and pushed:

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/cortex/capture/cortex_capture_hook.py"
          }
        ]
      }
    ]
  }
}
```

Notes:
- The hook is gated by an allowlist at `~/.cortex/capture/allowlist.txt`
  (one project-directory name per line). Sessions outside it are ignored, so
  work repos stay out by default. Two-zone by choice.
- The worker runs detached (never blocks Claude), scrubs obvious secrets,
  distills with the same privacy prompt as the backfills, and dedupes by
  content hash, so re-runs are safe.

## ChatGPT (periodic re-import)

ChatGPT has no push API, so re-export every few weeks:
Settings → Data controls → Export data, drop `conversations.json` into
`uploads/`, then:

```bash
python3 capture/distill_chatgpt_export.py --execute
```

State lives in `.cortex_chatgpt_distill_state.json`; only new conversations
are processed.

## Obsidian (scheduled)

Set in `.env`:

```
OBSIDIAN_VAULT=/path/to/your/vault
OBSIDIAN_INCLUDE=Journal,Ideas,How I work
```

Only the listed folders are ever read. Then schedule (cron example, hourly):

```
0 * * * * cd /path/to/cortex && python3 capture/ingest_obsidian.py --execute
```

macOS launchd works the same way; give the interpreter Full Disk Access if
your vault lives somewhere protected.

## Anything else

`capture/cortex_ingest.py` is the library: `push(records, source=...)` and
`distill(text, kind=...)`. Any new source is ~30 lines: read, distill, push
with a stable `dedupe_key`. See `ingest_obsidian.py` for the pattern.
