#!/usr/bin/env python3
"""
Cortex SessionEnd capture hook (thin handler).

Reads the SessionEnd hook JSON on stdin, applies the capture gates, and if a
session passes, writes a job to ~/.cortex/capture/queue/ and spawns a DETACHED
worker (cortex_capture_worker.py) to distill it, then exits 0 immediately. No
distillation inline -- session exit is never delayed.

Gates (default-deny):
  1. off-record:  CORTEX_CAPTURE_OFF=1 in env, or a .cortex-offrecord file in cwd
  2. allowlist:   cwd must start with a prefix in ~/.cortex/capture/allowlist.txt
  3. transcript:  transcript_path must exist
Anything that fails a gate is a silent no-op (logged to capture.log, not captured).

  echo '<hook json>' | python3 cortex_capture_hook.py             # live
  echo '<hook json>' | python3 cortex_capture_hook.py --dry-run   # decision only
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path.home() / ".cortex" / "capture"
QUEUE = BASE / "queue"
ALLOWLIST = BASE / "allowlist.txt"
LOG = BASE / "capture.log"
WORKER = Path(__file__).resolve().parent / "cortex_capture_worker.py"


def log(session_id, project, gate, status, sparks="-"):
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"{ts} | {session_id} | {project} | gate={gate} | sparks={sparks} | status={status}\n"
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(line)
    except Exception:
        pass
    return line


def allowed(cwd):
    if not ALLOWLIST.exists():
        return False
    cwd = str(cwd or "")
    if not cwd:
        return False
    for raw in ALLOWLIST.read_text().splitlines():
        p = raw.strip()
        if not p or p.startswith("#"):
            continue
        if cwd == p or cwd.startswith(p.rstrip("/") + "/"):
            return True
    return False


def main():
    dry = "--dry-run" in sys.argv
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}
    session_id = str(data.get("session_id") or "unknown")
    transcript = data.get("transcript_path") or ""
    cwd = data.get("cwd") or ""
    reason = data.get("reason") or ""
    project = Path(cwd).name or "unknown"

    # gate 1: off-record escape hatch
    offrec = os.environ.get("CORTEX_CAPTURE_OFF") == "1" or (cwd and (Path(cwd) / ".cortex-offrecord").exists())
    if offrec:
        line = log(session_id, project, "offrecord", "skipped")
        if dry:
            print("DECISION: SKIP (off-record)\n" + line, end="")
        return

    # gate 2: allowlist (default deny)
    if not allowed(cwd):
        line = log(session_id, project, "deny", "skipped")
        if dry:
            print(f"DECISION: SKIP (cwd not on allowlist): {cwd}\n" + line, end="")
        return

    # gate 3: transcript must exist
    if not transcript or not Path(transcript).exists():
        line = log(session_id, project, "allow", "skipped-no-transcript")
        if dry:
            print(f"DECISION: SKIP (transcript missing): {transcript}\n" + line, end="")
        return

    job = {
        "session_id": session_id, "transcript_path": str(transcript), "cwd": str(cwd),
        "project": project, "reason": reason,
        "queued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if dry:
        line = log(session_id, project, "allow", "would-enqueue")
        print("DECISION: CAPTURE (allowlisted)\nwould enqueue job:")
        print(json.dumps(job, indent=2))
        print(line, end="")
        return

    QUEUE.mkdir(parents=True, exist_ok=True)
    job_path = QUEUE / f"{session_id}.json"
    job_path.write_text(json.dumps(job))
    log(session_id, project, "allow", "queued")
    try:
        subprocess.Popen(
            [sys.executable, str(WORKER), str(job_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, cwd=str(WORKER.parent),
        )
    except Exception as e:
        log(session_id, project, "allow", f"spawn-error:{type(e).__name__}")
    # return immediately; worker runs detached


if __name__ == "__main__":
    main()
