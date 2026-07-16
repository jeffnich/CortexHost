#!/usr/bin/env python3
"""
Shared Cortex ingest helpers: two-zone distill + push to the cloud brain.

Every Mac-side source backfill (obsidian, apple notes, calendar, imessage) uses
this. Writes go through the cloud MCP `store_memory` tool, NOT direct to Qdrant
(which is private on Railway's network) -- same path as
scripts/session_end_capture.py.

`dedupe_key` gives each record a deterministic id server-side, so re-runs are
idempotent and templated/structured records don't collapse under the 0.97
similarity guard (the bug that ate the first Oura import).
"""

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

MCP_URL = os.getenv("CORTEX_MCP_URL", "")
USER_NAME = os.getenv("CORTEX_USER_NAME", "the user")
EXCLUDE_EXTRA = os.getenv("CORTEX_EXCLUDE", "")
DISTILL_MODEL = os.getenv("BACKFILL_MODEL", "gpt-5-mini")
CHUNK_CHARS = 48000
MAX_CHUNKS = 6

_openai = OpenAI()

DISTILLER_SYSTEM = (
    "You are a privacy filter and distiller for __CORTEX_USER__'s personal memory "
    "system. From the provided {kind}, extract ONLY durable, reusable personal-layer "
    "items about __CORTEX_USER__: decisions and their reasoning, frameworks / mental models, "
    "durable facts and preferences, goals, lessons learned, career signals, "
    "relationships, health/mood signals, and project state worth remembering later.\n"
    "EXCLUDE entirely: secrets; employer-proprietary or internal work product, "
    "customer, or strategy specifics; raw code and command output; and anything that "
    "only matters in the moment. Replace colleague names with roles.\n"
    "Each spark is one or two self-contained sentences that stand alone with no other "
    "context. Prefer few high-signal sparks over many shallow ones; return an empty "
    "list if nothing durable qualifies.\n"
    'Return STRICT JSON only: {{"sparks":[{{"type":"decision|framework|fact|'
    'preference|goal|lesson|career_signal|relationship|project_state","text":"..."}}]}}'
)
DISTILLER_SYSTEM = DISTILLER_SYSTEM.replace("__CORTEX_USER__", USER_NAME)
if EXCLUDE_EXTRA:
    DISTILLER_SYSTEM += "\nAlso EXCLUDE entirely: " + EXCLUDE_EXTRA


def _chunks(text: str):
    if len(text) <= CHUNK_CHARS:
        return [text]
    out, i = [], 0
    while i < len(text) and len(out) < MAX_CHUNKS:
        out.append(text[i : i + CHUNK_CHARS])
        i += CHUNK_CHARS
    return out


def distill(text: str, kind: str = "note", context: str = "", system: str | None = None) -> list[dict]:
    """Two-zone distill of free-form text -> [{text, type}]. Pass `system` to
    override the default distiller prompt (e.g. iMessage keeps personal names)."""
    sys = system or DISTILLER_SYSTEM.format(kind=kind)
    sparks = []
    for chunk in _chunks(text):
        try:
            resp = _openai.chat.completions.create(
                model=DISTILL_MODEL,
                messages=[
                    {"role": "system", "content": sys},
                    {"role": "user", "content": (context + "\n\n" + chunk).strip()},
                ],
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content or "{}")
            for s in data.get("sparks", []):
                t = (s.get("text") or "").strip()
                if t:
                    sparks.append({"text": t, "type": s.get("type", "note")})
        except Exception as e:
            print(f"  distill error: {type(e).__name__}: {e}", flush=True)
    return sparks


async def _push_async(records, source, default_tags, log):
    stored = skipped = errors = 0
    async with streamablehttp_client(MCP_URL) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for i, rec in enumerate(records):
                args = {
                    "text": rec["text"],
                    "type": rec.get("type", "note"),
                    "source": source,
                    "tags": rec.get("tags") or (default_tags + [rec.get("type", "note")]),
                }
                if rec.get("when"):
                    args["when"] = rec["when"]
                if rec.get("dedupe_key"):
                    args["dedupe_key"] = rec["dedupe_key"]
                try:
                    res = await s.call_tool("store_memory", args)
                    out = json.loads(res.content[0].text)
                    if out.get("stored"):
                        stored += 1
                    else:
                        skipped += 1
                except Exception as e:
                    errors += 1
                    log(f"  push error: {type(e).__name__}: {e}")
                if (i + 1) % 25 == 0:
                    log(f"  ...{i + 1}/{len(records)} (stored {stored}, skipped {skipped}, err {errors})")
    return {"stored": stored, "skipped": skipped, "errors": errors, "total": len(records)}


def push(records, source, default_tags=None, log=print):
    """Push records [{text, type?, when?, dedupe_key?, tags?}] via the cloud MCP."""
    if not MCP_URL:
        raise SystemExit("CORTEX_MCP_URL not set in .env")
    if not records:
        return {"stored": 0, "skipped": 0, "errors": 0, "total": 0}
    return asyncio.run(_push_async(records, source, default_tags or [source], log))
