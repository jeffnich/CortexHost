#!/usr/bin/env python3
"""
Backfill the Cortex brain with every Claude Code session on this Mac.

Walks ~/.claude/projects/*/*.jsonl, distills each session into personal-layer
memory sparks via an LLM, and pushes them to the cloud corpus. Resumable via
a state file; idempotent (already-processed sessions are skipped, and the
cloud dedup guard catches re-runs).

Two-zone privacy filter is applied to EVERY session: durable personal-layer
items cross (decisions, frameworks, facts, lessons, career signals); employer
/ proprietary content (code, customer data, internal product specifics,
colleague names) is dropped. Personal projects keep nearly everything; work
projects keep only the personal layer.

  python3 scripts/backfill_claude_sessions.py --dry-run --limit 3
  python3 scripts/backfill_claude_sessions.py --execute
"""

import argparse
import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

PROJECTS_DIR = Path.home() / ".claude" / "projects"
COWORK_DIR = Path.home() / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions"
STATE_FILE = ROOT / ".cortex_session_backfill_state.json"

QDRANT_URL = os.getenv("DEDUP_QDRANT_URL", "https://qdrant-production-8b89.up.railway.app").rstrip("/")
QDRANT_KEY = os.getenv("QDRANT_CLOUD_API_KEY", "")
COLLECTION = os.getenv("QDRANT_COLLECTION", "memories")
TENANT = os.getenv("CORTEX_TENANT_ID", "")
USER_RAW = os.getenv("CORTEX_USER_ID", "")
SCOPED_USER = f"{TENANT}:{USER_RAW}"

DISTILL_MODEL = os.getenv("BACKFILL_MODEL", "gpt-5-mini")
EMBED_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
MIN_TURNS = 4
CHUNK_CHARS = 48000
MAX_CHUNKS = 4          # cap very long sessions (newest content first)
WORKERS = 6

_openai = OpenAI()
USER_NAME = os.getenv("CORTEX_USER_NAME", "the user")
EXCLUDE_EXTRA = os.getenv("CORTEX_EXCLUDE", "")

DISTILLER_SYSTEM = (
    "You are a privacy filter and distiller for __CORTEX_USER__'s personal memory "
    "system. From a Claude Code work-session transcript, extract ONLY durable, "
    "reusable personal-layer items about __CORTEX_USER__: decisions and their reasoning, "
    "frameworks / mental models, durable facts and preferences, lessons learned, "
    "career signals, and project state worth remembering later.\n"
    "EXCLUDE entirely: source code, secrets, file paths, command output, customer "
    "data, employer-proprietary or internal product specifics, and anything that "
    "only matters inside that one session. Replace colleague names with roles.\n"
    "Each spark must be one or two self-contained sentences that make sense with no "
    "other context. Prefer few high-signal sparks over many shallow ones; return an "
    "empty list if nothing durable qualifies.\n"
    'Return STRICT JSON only: {"sparks":[{"type":"decision|framework|fact|'
    'preference|lesson|career_signal|project_state","text":"..."}]}'
)
DISTILLER_SYSTEM = DISTILLER_SYSTEM.replace("__CORTEX_USER__", USER_NAME)
if EXCLUDE_EXTRA:
    DISTILLER_SYSTEM += "\nAlso EXCLUDE entirely: " + EXCLUDE_EXTRA


def iter_sessions(cowork: bool = False):
    out = []
    if cowork:
        # Cowork / local-agent-mode store: main session transcripts only,
        # excluding subagent execution detail and per-tool audit trails.
        for f in COWORK_DIR.rglob("*.jsonl"):
            s = str(f)
            if "/subagents/" in s or f.name == "audit.jsonl" or "/-sessions-" not in s:
                continue
            proj = next(
                (p.replace("-sessions-", "cowork:") for p in f.parts if p.startswith("-sessions-")),
                "cowork",
            )
            out.append((proj, f))
        return out
    for proj_dir in sorted(PROJECTS_DIR.glob("*")):
        if not proj_dir.is_dir():
            continue
        project = proj_dir.name.replace(str(Path.home()).replace("/", "-") + "-", "")
        for f in proj_dir.glob("*.jsonl"):
            out.append((project, f))
    return out


def parse_transcript(path: Path) -> tuple[str, int]:
    turns = []
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") not in ("user", "assistant"):
                    continue
                msg = o.get("message")
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                content = msg.get("content")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                    text = "\n".join(p for p in parts if p)
                text = text.strip()
                if not text or len(text) < 2:
                    continue
                turns.append(f"{role.upper()}: {text}")
    except Exception:
        return "", 0
    return "\n\n".join(turns), len(turns)


def chunks_of(text: str):
    if len(text) <= CHUNK_CHARS:
        return [text]
    out = []
    i = 0
    while i < len(text) and len(out) < MAX_CHUNKS:
        out.append(text[i : i + CHUNK_CHARS])
        i += CHUNK_CHARS
    return out


def distill(text: str, project: str) -> list[dict]:
    sparks = []
    for chunk in chunks_of(text):
        try:
            resp = _openai.chat.completions.create(
                model=DISTILL_MODEL,
                messages=[
                    {"role": "system", "content": DISTILLER_SYSTEM},
                    {"role": "user", "content": f"Project: {project}\n\nTranscript:\n{chunk}"},
                ],
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content or "{}")
            for s in data.get("sparks", []):
                t = (s.get("text") or "").strip()
                if t:
                    sparks.append({"text": t, "type": s.get("type", "note")})
        except Exception as e:
            print(f"  distill error ({project}): {type(e).__name__}", flush=True)
    return sparks


def embed_batch(texts: list[str]) -> list[list[float]]:
    resp = _openai.embeddings.create(model=EMBED_MODEL, input=[t[:8000] for t in texts])
    return [d.embedding for d in resp.data]


def is_dup(vec) -> bool:
    r = requests.post(
        f"{QDRANT_URL}/collections/{COLLECTION}/points/search",
        json={
            "vector": vec,
            "limit": 1,
            "score_threshold": 0.97,
            "filter": {"must": [{"key": "user_id", "match": {"value": SCOPED_USER}}]},
        },
        headers={"api-key": QDRANT_KEY},
        timeout=30,
    )
    return bool(r.ok and r.json().get("result"))


def push(sparks: list[dict], project: str, session_id: str, source: str = "claude-code") -> int:
    if not sparks:
        return 0
    vecs = embed_batch([s["text"] for s in sparks])
    points = []
    for s, v in zip(sparks, vecs):
        if is_dup(v):
            continue
        now = datetime.now(timezone.utc)
        mid = str(uuid.uuid4())
        points.append(
            {
                "id": mid,
                "vector": v,
                "payload": {
                    "memory_id": mid,
                    "id": mid,
                    "text": s["text"],
                    "user_id": SCOPED_USER,
                    "tenant_id": TENANT,
                    "tenantId": TENANT,
                    "created_at": now.isoformat(),
                    "created_at_ts": now.timestamp(),
                    "updated_at": now.isoformat(),
                    "updated_at_ts": now.timestamp(),
                    "source": source,
                    "tags": [source, project, s["type"]],
                    "type_hint": s["type"],
                    "metadata": {"channel": f"{source}-backfill", "project": project, "session": session_id},
                },
            }
        )
    if not points:
        return 0
    requests.put(
        f"{QDRANT_URL}/collections/{COLLECTION}/points?wait=false",
        json={"points": points},
        headers={"api-key": QDRANT_KEY},
        timeout=60,
    ).raise_for_status()
    return len(points)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"done": [], "sparks": 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--cowork", action="store_true", help="walk the Cowork/local-agent-mode store instead of ~/.claude/projects")
    ap.add_argument("--source", default="claude-code", help="source tag for stored memories")
    args = ap.parse_args()
    execute = bool(args.execute) and not args.dry_run

    state = {"done": [], "sparks": 0} if args.fresh else load_state()
    done = set(state["done"])
    sessions = [s for s in iter_sessions(cowork=args.cowork) if str(s[1]) not in done]
    if args.limit:
        sessions = sessions[: args.limit]
    print(f"sessions to process: {len(sessions)} (already done: {len(done)})", flush=True)

    total_sparks = state.get("sparks", 0)
    processed = 0

    def work(item):
        project, path = item
        text, n = parse_transcript(path)
        if n < MIN_TURNS:
            return (item, [], n)
        sparks = distill(text, project)
        return (item, sparks, n)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(work, s): s for s in sessions}
        for fut in as_completed(futures):
            item, sparks, n = fut.result()
            project, path = item
            processed += 1
            if args.dry_run:
                print(f"[{processed}/{len(sessions)}] {project} ({n} turns) -> {len(sparks)} sparks", flush=True)
                for s in sparks[:4]:
                    print(f"    - [{s['type']}] {s['text'][:100]}", flush=True)
                continue
            pushed = push(sparks, project, path.stem, args.source) if sparks else 0
            total_sparks += pushed
            state["done"].append(str(path))
            state["sparks"] = total_sparks
            if processed % 10 == 0 or processed == len(sessions):
                STATE_FILE.write_text(json.dumps(state))
                print(f"[{processed}/{len(sessions)}] {project}: +{pushed} (total sparks: {total_sparks})", flush=True)

    if execute:
        STATE_FILE.write_text(json.dumps(state))
    print(json.dumps({"processed": processed, "total_sparks_pushed": total_sparks, "done": True}), flush=True)


if __name__ == "__main__":
    main()
