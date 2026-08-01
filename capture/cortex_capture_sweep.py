#!/usr/bin/env python3
"""
Nightly catch-up for missed session captures.

The SessionEnd hook is the realtime path, but it can miss (host never fires
SessionEnd, payload transcript path invalid, machine asleep). This sweep walks
~/.claude/projects/*, applies the SAME allowlist gate as the hook, and runs the
worker for any transcript modified in the last N days. Spark-level content-hash
dedupe keys make re-processing idempotent, so overlap with the hook is safe.

  python3 cortex_capture_sweep.py            # last 3 days
  python3 cortex_capture_sweep.py --days 30  # deeper catch-up
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cortex_capture_hook import allowed, log  # same gate, same audit log

PROJECTS = Path.home() / ".claude" / "projects"
QUEUE = Path.home() / ".cortex" / "capture" / "queue"
WORKER = Path(__file__).resolve().parent / "cortex_capture_worker.py"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=3.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cutoff = time.time() - args.days * 86400
    ran = 0
    for proj_dir in sorted(PROJECTS.glob("*")):
        if not proj_dir.is_dir():
            continue
        cwd = proj_dir.name.replace("-", "/")  # slug -> path (best effort)
        if not allowed(cwd):
            continue
        for f in proj_dir.glob("*.jsonl"):
            if f.stat().st_mtime < cutoff:
                continue
            sid = f.stem
            job = {"session_id": sid, "transcript_path": str(f), "cwd": cwd,
                   "project": Path(cwd).name, "reason": "sweep",
                   "queued_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            if args.dry_run:
                print(f"would sweep: {Path(cwd).name}/{sid} ({f.stat().st_size//1024}KB)")
                continue
            QUEUE.mkdir(parents=True, exist_ok=True)
            jp = QUEUE / f"{sid}.json"
            jp.write_text(json.dumps(job))
            log(sid, Path(cwd).name, "allow", "sweep-run")
            subprocess.run([sys.executable, str(WORKER), str(jp)], timeout=1800)
            ran += 1
    print(f"sweep complete: {ran} session(s) processed")


if __name__ == "__main__":
    main()
