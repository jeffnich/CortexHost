#!/usr/bin/env python3
"""
Cortex capture worker (detached distiller + auto-push). Friction-free: no manual promote.

Reuses the EXISTING condenser (scripts/backfill_claude_sessions.py:
parse_transcript + distill) -- distillation is not rebuilt here. Reads a job from
the queue, distills the transcript into sparks, writes them to the local outbox as
one JSON per session (durable audit record), and AUTO-PUSHES them into Cortex via
the cloud MCP store_memory tool.

Safety is the hook's gates (default-deny allowlist, off-record, transcript-exists)
plus the secret pre-scrub here and the condenser's own privacy prompt -- only
allowlisted personal projects ever reach this worker.

Idempotent two ways: by session_id (an existing outbox file = already processed)
and by content-hash dedupe_key on every spark (cc:<sid>:<sha1>), so resumed-session
re-fires and retries never duplicate.

  python3 cortex_capture_worker.py <job.json>   # process one job (detached in prod)
  python3 cortex_capture_worker.py --promote      # retry-push any outbox sparks not yet pushed
"""

import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parent))  # so we can reuse the condenser

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

BASE = Path.home() / ".cortex" / "capture"
OUTBOX = BASE / "outbox"
LOG = BASE / "capture.log"
MCP_URL = os.getenv("CORTEX_MCP_URL", "")
MIN_TURNS = 4

# Belt-and-suspenders secret scrub BEFORE anything reaches the distiller; the
# condenser's own prompt also excludes secrets -- this is a second, deterministic layer.
_SECRET_PATS = [
    re.compile(r"-----BEGIN[^-]+-----.*?-----END[^-]+-----", re.S),
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{6,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"(?im)^\s*(?:export\s+)?[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|APIKEY)[A-Za-z0-9_]*\s*[=:]\s*\S{6,}.*$"),
    re.compile(r"(?i)\b(?:authorization|bearer)\b[:=]?\s*[A-Za-z0-9._\-]{16,}"),
]


def scrub_secrets(text):
    for pat in _SECRET_PATS:
        text = pat.sub("[REDACTED]", text)
    return text


def log(session_id, project, gate, status, sparks="-"):
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(f"{ts} | {session_id} | {project} | gate={gate} | sparks={sparks} | status={status}\n")
    except Exception:
        pass


def to_records(sparks, sid, project):
    out = []
    for sp in sparks:
        t = (sp.get("text") or "").strip()
        if not t:
            continue
        typ = sp.get("type", "note")
        out.append({
            "text": t, "type": typ,
            "tags": ["claude-code", project, typ],
            "dedupe_key": "cc:" + sid + ":" + hashlib.sha1(t.encode()).hexdigest()[:12],
        })
    return out


async def _push(records):
    if not MCP_URL or not records:
        return 0
    pushed = 0
    async with streamablehttp_client(MCP_URL) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for rec in records:
                try:
                    res = await s.call_tool("store_memory", {
                        "text": rec["text"], "type": rec["type"],
                        "tags": rec["tags"], "source": "claude-code",
                        "dedupe_key": rec["dedupe_key"],
                    })
                    if json.loads(res.content[0].text).get("stored"):
                        pushed += 1
                except Exception:
                    pass
    return pushed


def push_records(records):
    try:
        return asyncio.run(_push(records))
    except Exception:
        return 0


def promote():
    """Retry-push any outbox sessions not fully pushed (e.g., the MCP was down at
    capture time). The normal path auto-pushes; this is just a catch-up for failures."""
    fixed = 0
    for f in sorted(OUTBOX.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if d.get("pushed_all"):
            continue
        recs = to_records(d.get("sparks", []), d.get("session_id", "unknown"), d.get("project", "unknown"))
        n = push_records(recs)
        d["pushed"], d["pushed_all"] = n, (n == len(recs))
        f.write_text(json.dumps(d, indent=2))
        fixed += 1
        print(f"{f.name}: pushed {n}/{len(recs)}")
    print(f"promote: retried {fixed} outbox file(s)")


def main():
    if "--promote" in sys.argv:
        promote()
        return
    if len(sys.argv) < 2:
        print("usage: cortex_capture_worker.py <job.json>")
        return

    job_path = Path(sys.argv[1])
    try:
        job = json.loads(job_path.read_text())
    except Exception:
        return
    sid = str(job.get("session_id", "unknown"))
    project = job.get("project", "unknown")
    OUTBOX.mkdir(parents=True, exist_ok=True)
    out_path = OUTBOX / f"{sid}.json"

    if out_path.exists():
        # A session can end more than once (long-lived sessions get resumed).
        # Re-capture when the transcript has grown since the last capture --
        # spark-level content-hash dedupe keys make overlap harmless. Only
        # skip when nothing new happened.
        try:
            prev = json.loads(out_path.read_text())
            prev_ts = datetime.fromisoformat(prev.get("captured_at", "1970-01-01T00:00:00+00:00")).timestamp()
            tr = Path(job["transcript_path"])
            if not tr.exists() or tr.stat().st_mtime <= prev_ts:
                log(sid, project, "allow", "dup-skip")
                job_path.unlink(missing_ok=True)
                return
            log(sid, project, "allow", "recapture-grown")
        except Exception:
            log(sid, project, "allow", "dup-skip")
            job_path.unlink(missing_ok=True)
            return

    try:
        from backfill_claude_sessions import parse_transcript, distill  # reuse condenser
    except Exception as e:
        log(sid, project, "allow", f"import-error:{type(e).__name__}")
        return

    text, n_turns = parse_transcript(Path(job["transcript_path"]))
    if n_turns < MIN_TURNS:
        log(sid, project, "allow", f"too-short:{n_turns}turns")
        job_path.unlink(missing_ok=True)
        return

    text = scrub_secrets(text)
    try:
        sparks = distill(text, project)
    except Exception as e:
        log(sid, project, "allow", f"distill-error:{type(e).__name__}")
        return

    records = to_records(sparks, sid, project)
    pushed = push_records(records)
    rec = {
        "session_id": sid, "project": project, "cwd": job.get("cwd"),
        "reason": job.get("reason"), "source": "claude-code",
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_turns": n_turns, "pushed": pushed, "pushed_all": pushed == len(records),
        "sparks": sparks,
    }
    out_path.write_text(json.dumps(rec, indent=2))
    status = "captured+pushed" if pushed == len(records) else ("captured+partial" if pushed else "captured+pushfail")
    log(sid, project, "allow", status, sparks=f"{pushed}/{len(records)}")
    job_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
