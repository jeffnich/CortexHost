#!/usr/bin/env python3
"""
Cortex MCP server, cloud variant (Railway).

Standalone: no CortexHost repo imports. Same four tools as cortex_mcp/server.py,
talking to Qdrant over Railway's private network with API key auth.

Env (Railway service variables):
  PORT                     injected by Railway
  QDRANT_URL               http://qdrant.railway.internal:6333
  QDRANT_API_KEY           Qdrant service key
  OPENAI_API_KEY           embeddings (text-embedding-3-small, locked to corpus)
  CORTEX_MCP_PATH_SECRET   capability URL path segment
  CORTEX_TENANT_ID / CORTEX_USER_ID   identity scope

Client attribution: every write is stamped server-side with `client` (which
app made the call) alongside the model-supplied `source`. The label comes from,
in order: a `?client=<name>` query param on the connector URL, an
`X-Cortex-Client` header, then the MCP clientInfo / User-Agent the client sent.
Give each app its own URL (e.g. .../mcp?client=cursor) to make the tag reliable.

Access log: every tool call, read or write, appends one hash-chained row to a
small SQLite file (CORTEX_ACCESS_DB, default /data/access.db when /data exists,
else ./access.db): when, which app, which tool, read or write, which memory ids.
The owner dashboard reads it from GET /<CORTEX_MCP_PATH_SECRET>/access.json in
the browser, so the public Gateway never sees this service's URL. Without a
volume the log resets on redeploy. CORTEX_ACCESS_CORS_ORIGIN (default "*",
since the secret path is the boundary) sets which page origin may read it.
"""

import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone

import requests
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from openai import OpenAI

QDRANT_URL = os.environ["QDRANT_URL"].rstrip("/")
QDRANT_KEY = os.environ.get("QDRANT_API_KEY", "")
COLLECTION = os.environ.get("QDRANT_COLLECTION", "memories")
TENANT = os.environ["CORTEX_TENANT_ID"]
USER_RAW = os.environ["CORTEX_USER_ID"]
SCOPED_USER = f"{TENANT}:{USER_RAW}"
EMBED_MODEL = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

_openai = OpenAI()

# The capability-URL secret path is the security boundary here, so the
# default localhost-only DNS-rebinding guard would just reject our own
# public domain. Trust the Railway host (and any host, since remote
# connectors arrive with varied Host/Origin headers behind the secret path).
_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False,
    allowed_hosts=["*"],
    allowed_origins=["*"],
)
mcp = FastMCP("cortex", transport_security=_security)


# ----------------------------------------------------------------------------
# Client attribution
# ----------------------------------------------------------------------------

# Substring of clientInfo.name or User-Agent (lowercased) -> canonical label.
# First match wins, so more specific patterns go first.
_CLIENT_PATTERNS = (
    ("claude-desktop", "claude-desktop"),  # Claude Code inside the desktop app
    ("claude-code", "claude-code"),
    ("claude-user", "claude-ai"),  # claude.ai connector proxy (every claude.ai surface)
    ("claude-ai", "claude-ai"),
    ("openai", "chatgpt"),
    ("chatgpt", "chatgpt"),
    ("codex", "codex"),
    ("cursor", "cursor"),
)
_LABEL_RE = re.compile(r"[^a-z0-9._-]+")


def _clean_label(value: str | None) -> str | None:
    label = _LABEL_RE.sub("-", (value or "").strip().lower()).strip("-")[:40]
    return label or None


def _caller(ctx: Context | None) -> dict:
    """Best-effort identity of the app on the other end of this MCP session.

    Never raises: attribution must not be able to break a write.
    """
    info: dict = {}
    if ctx is None:
        return {"client": "unknown"}
    try:
        params = ctx.session.client_params
        if params and params.clientInfo:
            info["client_name"] = params.clientInfo.name
            info["client_version"] = params.clientInfo.version
    except Exception:
        pass
    try:
        req = ctx.request_context.request
        if req is not None:
            info["label"] = _clean_label(req.query_params.get("client")) or _clean_label(
                req.headers.get("x-cortex-client")
            )
            info["user_agent"] = (req.headers.get("user-agent") or "")[:200] or None
    except Exception:
        pass

    client = info.get("label")
    if not client and "claude-user" in (info.get("user_agent") or "").lower():
        # Arrived through the claude.ai connector proxy. clientInfo still says
        # which Claude surface: "claude-ai" for chat, "claude-code" for cloud
        # agent sessions (claude.ai/code, projects), which would otherwise be
        # confused with Claude Code running locally.
        name = (info.get("client_name") or "").lower()
        client = "claude-code-cloud" if "claude-code" in name else "claude-ai"
    if not client:
        haystack = f"{info.get('client_name') or ''} {info.get('user_agent') or ''}".lower()
        client = next((lbl for pat, lbl in _CLIENT_PATTERNS if pat in haystack), None)
    if not client:
        client = _clean_label(info.get("client_name")) or "unknown"
    info["client"] = client
    return {k: v for k, v in info.items() if v}


def _log_write(tool: str, caller: dict, memory_id: str) -> None:
    # One line per write so Railway logs show what each app actually sends.
    print(f"cortex-write tool={tool} id={memory_id} caller={json.dumps(caller)}", file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------
# Access log
# ----------------------------------------------------------------------------

_ACCESS_FIELDS = ("ts", "tool", "kind", "client", "memory_ids", "detail")
_ACCESS_GENESIS = "0" * 64


class AccessLog:
    """Append-only, hash-chained record of which app touched which memories."""

    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS access (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,"
                " tool TEXT NOT NULL, kind TEXT NOT NULL, client TEXT NOT NULL, memory_ids TEXT NOT NULL,"
                " detail TEXT, prev_hash TEXT NOT NULL, row_hash TEXT NOT NULL)"
            )

    @staticmethod
    def _hash(prev: str, fields: dict) -> str:
        blob = json.dumps({k: fields[k] for k in _ACCESS_FIELDS}, sort_keys=True)
        return hashlib.sha256(f"{prev}\n{blob}".encode()).hexdigest()

    def record(self, tool: str, kind: str, client: str, memory_ids: list, detail: str | None = None) -> None:
        fields = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tool": tool, "kind": kind,
            "client": client or "unknown", "memory_ids": json.dumps([str(i) for i in memory_ids][:50]),
            "detail": (detail or "")[:200] or None,
        }
        with self._lock:
            last = self._db.execute("SELECT row_hash FROM access ORDER BY id DESC LIMIT 1").fetchone()
            prev = last["row_hash"] if last else _ACCESS_GENESIS
            self._db.execute(
                "INSERT INTO access (ts, tool, kind, client, memory_ids, detail, prev_hash, row_hash)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (*[fields[k] for k in _ACCESS_FIELDS], prev, self._hash(prev, fields)),
            )

    def recent(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM access ORDER BY id DESC LIMIT ?",
                                    (max(1, min(int(limit), 2000)),)).fetchall()
        out = []
        for r in rows:
            d = {k: r[k] for k in ("id", *_ACCESS_FIELDS)}
            d["memory_ids"] = json.loads(d["memory_ids"])
            out.append(d)
        return out

    def verify(self) -> dict:
        with self._lock:
            rows = self._db.execute("SELECT * FROM access ORDER BY id").fetchall()
        prev = _ACCESS_GENESIS
        for r in rows:
            if r["prev_hash"] != prev or r["row_hash"] != self._hash(prev, dict(r)):
                return {"intact": False, "checked": len(rows), "broken_at": r["id"]}
            prev = r["row_hash"]
        return {"intact": True, "checked": len(rows)}


def _access_path() -> str:
    explicit = os.environ.get("CORTEX_ACCESS_DB", "").strip()
    if explicit:
        return explicit
    return "/data/access.db" if os.path.isdir("/data") else "access.db"


_access: AccessLog | None = None


def _record_access(tool: str, kind: str, client: str, memory_ids=(), detail: str | None = None) -> None:
    """Never lets logging break the tool call it describes."""
    global _access
    try:
        if _access is None:
            _access = AccessLog(_access_path())
        _access.record(tool, kind, client, [i for i in memory_ids if i is not None], detail)
    except Exception as exc:
        print(f"access log failed tool={tool}: {exc!r}", file=sys.stderr, flush=True)


def _log_access(tool: str, kind: str, ctx: Context | None, memory_ids=(), detail: str | None = None) -> None:
    _record_access(tool, kind, _caller(ctx)["client"], memory_ids, detail)


def _embed(text: str) -> list[float]:
    resp = _openai.embeddings.create(model=EMBED_MODEL, input=" ".join(text.split())[:4000])
    return resp.data[0].embedding


def _q(method: str, path: str, body: dict | None = None):
    r = requests.request(
        method,
        f"{QDRANT_URL}{path}",
        json=body,
        headers={"api-key": QDRANT_KEY},
        timeout=25,
    )
    r.raise_for_status()
    return r.json()


def _identity_filter() -> dict:
    return {"must": [{"key": "user_id", "match": {"value": SCOPED_USER}}]}


def _search(vector, limit: int, extra_must=None, score_threshold: float = 0.25):
    flt = _identity_filter()
    if extra_must:
        flt["must"].extend(extra_must)
    return _q(
        "POST",
        f"/collections/{COLLECTION}/points/search",
        {
            "vector": vector,
            "limit": limit,
            "with_payload": True,
            "filter": flt,
            "score_threshold": score_threshold,
        },
    )["result"]


def _fmt(hit: dict) -> dict:
    p = hit.get("payload") or {}
    return {
        "score": round(hit.get("score"), 3) if isinstance(hit.get("score"), (int, float)) else None,
        "text": p.get("text", ""),
        "timestamp": p.get("created_at") or p.get("timestamp"),
        "source": p.get("source"),
        "client": p.get("client"),
        "tags": p.get("tags") or [],
    }


@mcp.tool()
def search_memory(
    query: str,
    limit: int = 8,
    year_start: int | None = None,
    year_end: int | None = None,
    ctx: Context | None = None,
) -> str:
    """Semantic search over the user's full memory corpus (their entire history).

    Use this to recall the user's past thinking, decisions, projects, preferences,
    relationships, and history before answering questions about them or their work.
    Optionally constrain results to a year range (inclusive).
    """
    vec = _embed(query)
    extra = []
    if year_start:
        extra.append(
            {"key": "created_at_ts", "range": {"gte": datetime(year_start, 1, 1, tzinfo=timezone.utc).timestamp()}}
        )
    if year_end:
        extra.append(
            {"key": "created_at_ts", "range": {"lt": datetime(year_end + 1, 1, 1, tzinfo=timezone.utc).timestamp()}}
        )
    hits = _search(vec, max(1, min(int(limit), 50)), extra)
    _log_access("search_memory", "read", ctx, [h.get("id") for h in hits], f"query: {query}")
    return json.dumps({"count": len(hits), "memories": [_fmt(h) for h in hits]}, default=str)


@mcp.tool()
def store_memory(
    text: str,
    type: str = "note",
    tags: list[str] | None = None,
    source: str = "mcp",
    when: str | None = None,
    dedupe_key: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Store a durable fact, decision, framework, or commitment in the user's memory corpus.

    Use when the user states something worth remembering long-term. Near-duplicates
    (cosine >= 0.97 against an existing memory) are skipped automatically.
    `when` optionally back-dates the memory (ISO date/datetime, e.g. '2026-01-15')
    for imported or sensor data so it carries its real date, not capture time.
    `source` is your own label for where this came from; the server separately
    records which app made the call, so you don't need to guess it.
    `dedupe_key` gives the memory a deterministic id (re-storing the same key
    overwrites in place) and skips similarity dedup — use for keyed/templated
    records like one-per-day sensor data, e.g. 'oura:2026-05-04'.
    """
    text = (text or "").strip()
    if not text:
        return json.dumps({"stored": False, "reason": "empty text"})
    caller = _caller(ctx)
    vec = _embed(text)
    if dedupe_key:
        mid = str(uuid.uuid5(uuid.NAMESPACE_URL, "cortex:" + dedupe_key))
    else:
        dups = _search(vec, 1, score_threshold=0.97)
        if dups:
            return json.dumps({"stored": False, "reason": "near-duplicate", "existing": _fmt(dups[0])}, default=str)
        mid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    ts = now
    if when:
        try:
            ts = datetime.fromisoformat(when.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            ts = now
    payload = {
        "memory_id": mid,
        "id": mid,
        "text": text,
        "user_id": SCOPED_USER,
        "tenant_id": TENANT,
        "tenantId": TENANT,
        "created_at": ts.isoformat(),
        "created_at_ts": ts.timestamp(),
        "updated_at": now.isoformat(),
        "updated_at_ts": now.timestamp(),
        "source": source,
        "client": caller["client"],
        "tags": tags or [],
        "type_hint": type,
        "metadata": {"channel": "cortex-mcp-cloud", "caller": caller},
    }
    _q(
        "PUT",
        f"/collections/{COLLECTION}/points?wait=true",
        {"points": [{"id": mid, "vector": vec, "payload": payload}]},
    )
    _log_write("store_memory", caller, mid)
    _log_access("store_memory", "write", ctx, [mid])
    return json.dumps({"stored": True, "memory_id": mid, "client": caller["client"]})


@mcp.tool()
def recent_memories(
    days: int = 7, limit: int = 20, source: str | None = None, client: str | None = None,
    ctx: Context | None = None,
) -> str:
    """List the user's memories from the last N days, newest first.

    Optional source filter, e.g. 'chatgpt' (imported history), 'claude-code',
    'obsidian', 'mcp' (stored via this server).
    Optional client filter: the app that wrote it, e.g. 'claude-ai', 'cursor',
    'chatgpt', 'codex', 'claude-code' (only set on memories written after
    client tagging shipped).
    """
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).timestamp()
    flt = _identity_filter()
    flt["must"].append({"key": "created_at_ts", "range": {"gte": since}})
    if source:
        flt["must"].append({"key": "source", "match": {"value": source}})
    if client:
        flt["must"].append({"key": "client", "match": {"value": client}})
    res = _q(
        "POST",
        f"/collections/{COLLECTION}/points/scroll",
        {"limit": max(1, min(int(limit), 100)), "with_payload": True, "filter": flt},
    )
    pts = res["result"]["points"]
    _log_access("recent_memories", "read", ctx, [p.get("id") for p in pts], f"last {days} days")
    mems = sorted(
        (_fmt({"payload": p.get("payload") or {}}) for p in pts),
        key=lambda m: str(m["timestamp"] or ""),
        reverse=True,
    )
    return json.dumps({"count": len(mems), "memories": list(mems)}, default=str)


@mcp.tool()
def memory_stats(ctx: Context | None = None) -> str:
    """Corpus overview: total memory count and a full per-source breakdown."""
    _log_access("memory_stats", "read", ctx, [], "counts only")
    out: dict = {"collection": COLLECTION, "scoped_user": SCOPED_USER}
    out["total_points"] = _q("GET", f"/collections/{COLLECTION}")["result"]["points_count"]
    ident = {"must": [{"key": "user_id", "match": {"value": SCOPED_USER}}]}
    by_source: dict = {}
    # Prefer the facet API: enumerates every source value, including ones added
    # later (cortex-condenser, claude-code, cortex-os, future watchers).
    try:
        res = _q(
            "POST",
            f"/collections/{COLLECTION}/facet",
            {"key": "source", "filter": ident, "limit": 100, "exact": True},
        )
        by_source = {h["value"]: h["count"] for h in res["result"]["hits"]}
    except Exception:
        # Fallback: enumerate all known sources explicitly.
        for src in (
            "chatgpt-backup", "unknown", "claude-code", "cortex-condenser",
            "cortex-os", "eternal_gpt", "mcp",
        ):
            try:
                by_source[src] = _q(
                    "POST",
                    f"/collections/{COLLECTION}/points/count",
                    {
                        "filter": {"must": ident["must"] + [{"key": "source", "match": {"value": src}}]},
                        "exact": True,
                    },
                )["result"]["count"]
            except Exception:
                pass
    out["by_source"] = dict(sorted(by_source.items(), key=lambda kv: -kv[1]))
    out["scoped_total"] = sum(by_source.values())
    try:
        res = _q(
            "POST",
            f"/collections/{COLLECTION}/facet",
            {"key": "client", "filter": ident, "limit": 50, "exact": True},
        )
        out["by_client"] = {h["value"]: h["count"] for h in res["result"]["hits"]}
    except Exception:
        out["by_client"] = None  # needs the `client` payload index
    return json.dumps(out)


# ----------------------------------------------------------------------------
# Control-center layer: the user's daily operating surface, exposed as tools so it
# lives inside whatever AI he's already using (claude.ai, ChatGPT) instead of a
# separate dashboard. Items are stored in the same corpus with source
# "cortex-os" and structured os_* fields, so they're both list-filterable and
# semantically searchable.
# ----------------------------------------------------------------------------

try:
    from zoneinfo import ZoneInfo

    _TZ = ZoneInfo(os.environ.get("CORTEX_TZ", "UTC"))
except Exception:  # pragma: no cover - tzdata missing
    _TZ = timezone.utc


def _parse_due(due: str | None):
    if not due:
        return None, None
    s = due.strip()
    for cand in (s, s.replace("Z", "+00:00"), s + "T00:00:00+00:00" if len(s) == 10 else s):
        try:
            dt = datetime.fromisoformat(cand)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_TZ)
            return dt.isoformat(), dt.timestamp()
        except ValueError:
            continue
    return s, None  # keep raw string if unparseable


def _os_create(
    kind: str, text: str, due: str | None = None, to_whom: str | None = None, caller: dict | None = None
) -> str:
    text = (text or "").strip()
    if not text:
        return json.dumps({"created": False, "reason": "empty text"})
    now = datetime.now(timezone.utc)
    due_iso, due_ts = _parse_due(due)
    mid = str(uuid.uuid4())
    payload = {
        "memory_id": mid,
        "id": mid,
        "text": text,
        "user_id": SCOPED_USER,
        "tenant_id": TENANT,
        "tenantId": TENANT,
        "created_at": now.isoformat(),
        "created_at_ts": now.timestamp(),
        "updated_at": now.isoformat(),
        "updated_at_ts": now.timestamp(),
        "source": "cortex-os",
        "client": (caller or {}).get("client", "unknown"),
        "os_kind": kind,
        "os_status": "open",
        "os_due": due_iso,
        "os_due_ts": due_ts,
        "os_to": (to_whom or None),
        "os_done_at": None,
        "tags": ["cortex-os", kind],
        "type_hint": f"os_{kind}",
        "metadata": {"channel": "cortex-os", "caller": caller or {}},
    }
    _q(
        "PUT",
        f"/collections/{COLLECTION}/points?wait=true",
        {"points": [{"id": mid, "vector": _embed(text), "payload": payload}]},
    )
    _log_write(f"add_{kind}", caller or {}, mid)
    _record_access(f"add_{kind}", "write", (caller or {}).get("client", "unknown"), [mid])
    return json.dumps({"created": True, "id": mid, "kind": kind, "due": due_iso})


def _os_list(kind: str | None, status: str = "open", limit: int = 50) -> list[dict]:
    must = [
        {"key": "user_id", "match": {"value": SCOPED_USER}},
        {"key": "source", "match": {"value": "cortex-os"}},
        {"key": "os_status", "match": {"value": status}},
    ]
    if kind:
        must.append({"key": "os_kind", "match": {"value": kind}})
    res = _q(
        "POST",
        f"/collections/{COLLECTION}/points/scroll",
        {"limit": max(1, min(int(limit), 200)), "with_payload": True, "filter": {"must": must}},
    )["result"]["points"]
    items = []
    for p in res:
        pay = p.get("payload") or {}
        items.append(
            {
                "id": pay.get("memory_id") or p.get("id"),
                "kind": pay.get("os_kind"),
                "text": pay.get("text", ""),
                "due": pay.get("os_due"),
                "due_ts": pay.get("os_due_ts"),
                "to": pay.get("os_to"),
                "created": pay.get("created_at"),
            }
        )
    items.sort(key=lambda i: (i["due_ts"] is None, i["due_ts"] or 0, i["created"] or ""))
    return items


@mcp.tool()
def add_todo(text: str, due: str | None = None, ctx: Context | None = None) -> str:
    """Add a to-do to the user's task list. `due` is an optional date/time (ISO 8601,
    e.g. '2026-06-15' or '2026-06-15T14:00'); convert natural language like
    'tomorrow' or 'Friday' to an ISO date before calling.
    """
    return _os_create("todo", text, due=due, caller=_caller(ctx))


@mcp.tool()
def add_commitment(
    text: str, to_whom: str | None = None, due: str | None = None, ctx: Context | None = None
) -> str:
    """Record a commitment the user made (something they promised to do or deliver).
    Optionally capture who it's to (`to_whom`) and a due date (`due`, ISO 8601).
    Use this when the user says they'll do something for someone or by some time.
    """
    return _os_create("commitment", text, due=due, to_whom=to_whom, caller=_caller(ctx))


@mcp.tool()
def complete_item(item_id: str, ctx: Context | None = None) -> str:
    """Mark a to-do or commitment done by its id (ids come from whats_open / daily_brief)."""
    now = datetime.now(timezone.utc)
    _q(
        "POST",
        f"/collections/{COLLECTION}/points/payload?wait=true",
        {
            "payload": {
                "os_status": "done",
                "os_done_at": now.isoformat(),
                "os_done_by": _caller(ctx)["client"],
                "updated_at_ts": now.timestamp(),
            },
            "points": [item_id],
        },
    )
    _log_access("complete_item", "write", ctx, [item_id])
    return json.dumps({"completed": True, "id": item_id})


@mcp.tool()
def whats_open(kind: str | None = None, limit: int = 25, ctx: Context | None = None) -> str:
    """List the user's open to-dos and commitments, soonest-due first. Optionally
    filter by kind ('todo' or 'commitment'). Returns ids for completing items.
    """
    items = _os_list(kind, status="open", limit=limit)
    _log_access("whats_open", "read", ctx, [i["id"] for i in items])
    return json.dumps({"count": len(items), "items": items}, default=str)


@mcp.tool()
def daily_brief(ctx: Context | None = None) -> str:
    """The user's morning brief: today's date, open commitments and to-dos (soonest
    due first, overdue flagged), and how many memories were captured in the last
    day. Use this when the user asks what's on their plate, their agenda, or for a brief.
    """
    now_local = datetime.now(_TZ)
    today_ts = now_local.timestamp()
    commitments = _os_list("commitment", status="open", limit=50)
    todos = _os_list("todo", status="open", limit=50)
    _log_access("daily_brief", "read", ctx, [i["id"] for i in commitments + todos])

    def mark(items):
        for i in items:
            i["overdue"] = bool(i.get("due_ts") and i["due_ts"] < today_ts)
        return items

    since = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
    try:
        recent = _q(
            "POST",
            f"/collections/{COLLECTION}/points/count",
            {
                "filter": {
                    "must": [
                        {"key": "user_id", "match": {"value": SCOPED_USER}},
                        {"key": "created_at_ts", "range": {"gte": since}},
                    ]
                },
                "exact": True,
            },
        )["result"]["count"]
    except Exception:
        recent = None

    return json.dumps(
        {
            "date": now_local.strftime("%A, %B %d, %Y"),
            "open_commitments": mark(commitments),
            "open_todos": mark(todos),
            "captured_last_24h": recent,
        },
        default=str,
    )


def _ensure_collection():
    """Fresh installs have no collection; create it (plus payload indexes) so
    the first store_memory works. Waits for Qdrant to come up. Idempotent."""
    dim = int(os.environ.get("QDRANT_DIM", "1536"))
    for _ in range(45):
        try:
            r = requests.get(f"{QDRANT_URL}/collections/{COLLECTION}",
                             headers={"api-key": QDRANT_KEY}, timeout=5)
            if r.status_code == 200:
                return
            if r.status_code == 404:
                requests.put(f"{QDRANT_URL}/collections/{COLLECTION}",
                             json={"vectors": {"size": dim, "distance": "Cosine"}},
                             headers={"api-key": QDRANT_KEY}, timeout=20).raise_for_status()
                for field, schema in (("user_id", "keyword"), ("source", "keyword"),
                                      ("tags", "keyword"), ("os_status", "keyword"),
                                      ("os_kind", "keyword"), ("client", "keyword"),
                                      ("created_at_ts", "float")):
                    try:
                        requests.put(f"{QDRANT_URL}/collections/{COLLECTION}/index?wait=true",
                                     json={"field_name": field, "field_schema": schema},
                                     headers={"api-key": QDRANT_KEY}, timeout=20)
                    except Exception:
                        pass
                print(f"created collection '{COLLECTION}' ({dim}-dim cosine) + indexes", flush=True)
                return
        except Exception:
            pass
        import time
        time.sleep(2)
    print("warning: qdrant unreachable at boot; collection not ensured", file=sys.stderr)


def _ensure_client_index() -> None:
    """Keyword index on `client` so filtering/faceting by app is fast. Idempotent."""
    try:
        _q(
            "PUT",
            f"/collections/{COLLECTION}/index?wait=false",
            {"field_name": "client", "field_schema": "keyword"},
        )
    except Exception as exc:  # never block startup on this
        print(f"client index not ensured: {exc}", file=sys.stderr)


def register_access_route(secret: str) -> None:
    """GET /<secret>/access.json: the access log plus a chain check, for the owner dashboard."""
    from starlette.responses import JSONResponse, Response

    origin = os.environ.get("CORTEX_ACCESS_CORS_ORIGIN", "*").strip() or "*"
    cors = {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Methods": "GET",
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "Vary": "Origin"}

    @mcp.custom_route(f"/{secret}/access.json", methods=["GET", "OPTIONS"])
    async def access_json(request):
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=cors)
        global _access
        if _access is None:
            _access = AccessLog(_access_path())
        try:
            limit = int(request.query_params.get("limit", 300))
        except ValueError:
            limit = 300
        return JSONResponse({"chain": _access.verify(), "entries": _access.recent(limit)}, headers=cors)


if __name__ == "__main__":
    secret = os.environ.get("CORTEX_MCP_PATH_SECRET", "").strip()
    if not secret or len(secret) < 24:
        print("CORTEX_MCP_PATH_SECRET missing or too short", file=sys.stderr)
        sys.exit(1)
    _ensure_collection()
    _ensure_client_index()  # existing installs predate the `client` field
    # Railway's edge and private network are IPv6; bind dual-stack wildcard.
    mcp.settings.host = os.environ.get("MCP_BIND_HOST", "::")
    mcp.settings.port = int(os.environ.get("PORT", "8080"))
    mcp.settings.streamable_http_path = f"/{secret}/mcp"
    register_access_route(secret)
    mcp.run(transport="streamable-http")
