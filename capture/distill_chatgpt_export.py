#!/usr/bin/env python3
"""
Distill the raw ChatGPT export into a high-signal layer.

The chatgpt-backup source is ~42k raw message-level points (one chat turn each),
which dilutes retrieval. This re-distills the original export (uploads/
conversations.json) at the CONVERSATION level into durable two-zone sparks and
pushes them via the cloud MCP as source=chatgpt. Resumable (state file) and
idempotent (content-hash dedupe_key). After this is verified, the raw
chatgpt-backup layer can be retired.

  python3 scripts/distill_chatgpt_export.py --sample 3    # distill 3, print, no push
  python3 scripts/distill_chatgpt_export.py --dry-run
  python3 scripts/distill_chatgpt_export.py --execute
"""

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import cortex_ingest as ci

ROOT = Path(__file__).resolve().parent.parent
EXPORT = ROOT / "uploads" / "conversations.json"
STATE_FILE = ROOT / ".cortex_chatgpt_distill_state.json"
CAP = 48000
WORKERS = 6
USER_NAME = os.getenv("CORTEX_USER_NAME", "the user")
EXCLUDE_EXTRA = os.getenv("CORTEX_EXCLUDE", "")

SYS = (
    "You are a privacy filter and distiller for __CORTEX_USER__'s personal memory "
    "system. From one of __CORTEX_USER__'s past ChatGPT conversations, extract ONLY durable, "
    "reusable personal-layer items about __CORTEX_USER__: decisions and their reasoning, "
    "frameworks and mental models, durable facts and preferences, goals and life "
    "events, lessons learned, career signals, and project state worth remembering "
    "later.\n"
    "EXCLUDE entirely: generic question-and-answer with no personal signal, source "
    "code, secrets, one-off factual lookups, anything ephemeral, and employer-"
    "proprietary specifics. Replace colleague names with roles. Each spark is one or "
    "two self-contained sentences that stand on their own. Prefer few high-signal "
    "sparks; return an empty list if nothing durable qualifies.\n"
    'Return STRICT JSON only: {"sparks":[{"type":"decision|framework|fact|preference|'
    'goal|life_event|lesson|career_signal|project_state","text":"..."}]}'
)
SYS = SYS.replace("__CORTEX_USER__", USER_NAME)
if EXCLUDE_EXTRA:
    SYS += "\nAlso EXCLUDE entirely: " + EXCLUDE_EXTRA


def conv_text(conv):
    msgs = []
    for node in (conv.get("mapping") or {}).values():
        m = node.get("message")
        if not isinstance(m, dict):
            continue
        role = (m.get("author") or {}).get("role")
        if role not in ("user", "assistant"):
            continue
        parts = (m.get("content") or {}).get("parts") or []
        text = "\n".join(p for p in parts if isinstance(p, str) and p.strip())
        if not text.strip():
            continue
        msgs.append((m.get("create_time") or 0, role, text.strip()))
    msgs.sort(key=lambda x: x[0] or 0)
    body = "\n\n".join(f"{r.upper()}: {t}" for _, r, t in msgs)
    return body, len(msgs), (msgs[0][0] if msgs else None)


def distill_conv(conv):
    cid = conv.get("conversation_id") or conv.get("id") or hashlib.sha1((conv.get("title", "") + str(conv.get("create_time", ""))).encode()).hexdigest()
    title = conv.get("title") or ""
    text, n, first_ts = conv_text(conv)
    if n < 2 or len(text) < 200:
        return cid, title, None, []
    if len(text) > CAP:
        text = text[:CAP]
    try:
        sparks = ci.distill(text, context=f"Past ChatGPT conversation: {title}", system=SYS)
    except Exception as e:
        print(f"  distill error ({title[:40]}): {type(e).__name__}", flush=True)
        sparks = []
    when = datetime.fromtimestamp(first_ts, timezone.utc).isoformat() if first_ts else None
    return cid, title, when, sparks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    a = ap.parse_args()

    convs = json.load(open(EXPORT))
    state = {} if a.fresh or not STATE_FILE.exists() else json.loads(STATE_FILE.read_text())

    def key(c):
        return c.get("conversation_id") or c.get("id") or hashlib.sha1((c.get("title", "") + str(c.get("create_time", ""))).encode()).hexdigest()

    todo = [c for c in convs if key(c) not in state]
    if a.limit:
        todo = todo[: a.limit]
    print(f"conversations: {len(convs)} total, {len(todo)} to process", flush=True)

    if a.sample:
        for c in todo[: a.sample]:
            cid, title, when, sparks = distill_conv(c)
            print(f"\n[{title[:55]}] -> {len(sparks)} sparks", flush=True)
            for s in sparks[:6]:
                print("   ", s.get("type", "note"), "::", (s.get("text") or "")[:130], flush=True)
        return

    # process in batches so it's resumable and the push isn't one giant call
    BATCH = 80
    total_sparks = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        records, processed = [], []
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for cid, title, when, sparks in pool.map(distill_conv, chunk):
                for s in sparks:
                    t = (s.get("text") or "").strip()
                    if not t:
                        continue
                    records.append({
                        "text": t, "type": s.get("type", "note"), "when": when,
                        "dedupe_key": f"cgpt:{cid}:{hashlib.sha1(t.encode()).hexdigest()[:12]}",
                        "tags": ["chatgpt", s.get("type", "note")],
                    })
                processed.append(cid)
        if not (a.dry_run or not a.execute):
            res = ci.push(records, source="chatgpt", default_tags=["chatgpt"])
            for cid in processed:
                state[cid] = 1
            STATE_FILE.write_text(json.dumps(state))
            total_sparks += (res.get("stored", 0) if isinstance(res, dict) else 0)
            print(f"  [{min(i+BATCH,len(todo))}/{len(todo)}] pushed {res} | tracked {len(state)}", flush=True)
        else:
            total_sparks += len(records)
            print(f"  [{min(i+BATCH,len(todo))}/{len(todo)}] would push {len(records)} sparks", flush=True)
    print(f"DONE: {total_sparks} sparks across {len(todo)} conversations", flush=True)


if __name__ == "__main__":
    main()
