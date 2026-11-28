"""Owner dashboard: a private web UI over the gateway's own data.

Everything lives under /<GATEWAY_OWNER_SECRET>/, the same boundary as the owner
MCP tier: the page at /<secret>/, its built assets at /<secret>/assets/, and a
small JSON API at /<secret>/api/. It reads and writes only what the gateway
already holds (queue, audit log, outbound log, public documents). The memory
access feed comes from the Cortex MCP service and is fetched by the browser
directly, so the gateway never learns that URL or touches the corpus.
"""

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from guard import clean_text
from store import DOCUMENT_NAMES, REQUEST_STATUSES

DIST = Path(__file__).resolve().parent / "dashboard" / "dist"

# Strict by default: the page holds the owner secret in its URL, so nothing
# third-party loads and nothing leaks it through a Referer header. connect-src
# allows https so the browser can read the memory access feed from Cortex MCP.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self' https:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}

# Owner-tier tools that only read. Hidden from the activity feed by default so
# the feed shows things that happened, not the owner looking at them.
OWNER_READS = {"list_requests", "gateway_activity", "preview_public_profile", "outbound_activity"}

LANES = ("inbound", "triage", "owner", "outbound")


def _json(payload, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=SECURITY_HEADERS)


def _err(msg: str, status: int = 400) -> JSONResponse:
    return _json({"ok": False, "error": msg}, status)


def _summarize(tool: str, args: dict, detail: str | None) -> str:
    """One readable line for an audit row. Args are already cleaned at the door."""
    a = args or {}
    if tool == "ask_about_owner":
        return f"asked: {a.get('question') or ''}"
    if tool == "get_owner_take":
        return f"asked for a take on: {a.get('topic') or ''}"
    if tool == "check_availability":
        return "checked availability"
    if tool in ("request_meeting", "leave_message"):
        what = "requested a meeting" if tool == "request_meeting" else "left a message"
        subj = a.get("topic") or a.get("subject") or ""
        return f"{what}: {subj}".rstrip(": ")
    if tool == "check_request":
        return f"checked status of {a.get('request_id') or 'a request'}"
    if tool == "record_triage":
        return f"triaged {a.get('request_id')}: {a.get('verdict')}"
    if tool == "resolve_request":
        return f"{a.get('decision')} {a.get('request_id')}" + (" with a reply" if a.get("reply") else "")
    if tool == "save_document":
        return f"edited public {a.get('name')} (v{a.get('version')})"
    if tool in ("list_untriaged", "list_pending"):
        return "read the queue"
    return detail or tool


def activity(gw, limit: int = 150, include_reads: bool = False) -> list[dict]:
    events = []
    for r in gw.store.recent_audit(limit * 2):
        if r["tool"] == "ask_agent":
            continue  # the outbound log has the full record of these
        if r["tier"] == "owner" and r["tool"] in OWNER_READS and not include_reads:
            continue
        if r["tier"] == "triage" and r["tool"] != "record_triage" and not include_reads:
            continue
        lane = {"public": "inbound", "triage": "triage", "owner": "owner"}.get(r["tier"], r["tier"])
        events.append({
            "ts": r["ts"], "lane": lane, "tool": r["tool"], "client": r["client"], "outcome": r["outcome"],
            "summary": _summarize(r["tool"], r["args"], r["detail"]), "trace_id": r["trace_id"],
            "request_id": (r["args"] or {}).get("request_id"), "chained": r["chained"],
            "detail": r["detail"] if r["outcome"] != "ok" else None,
        })
    for c in gw.store.recent_outbound(limit):
        q = (c.get("request") or {}).get("question") or (c.get("request") or {}).get("tool") or ""
        events.append({
            "ts": c["ts"], "lane": "outbound", "tool": c["tool"], "client": c["triggered_by"],
            "outcome": c["status"], "summary": f"asked {c['target_host']}: {q}".rstrip(": "),
            "trace_id": c["trace_id"], "chained": True,
            "detail": None if c["policy_verdict"] == "allowed" else c["policy_verdict"],
            "card_signature": c.get("card_signature"), "key_changed": c.get("key_changed"),
            "latency_ms": c.get("latency_ms"),
        })
    events.sort(key=lambda e: e["ts"], reverse=True)
    return events[:limit]


def daily_counts(gw, days: int = 14) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)
    buckets = {(start + timedelta(days=i)).isoformat(): dict.fromkeys(LANES, 0) for i in range(days)}
    since = start.isoformat()
    for e in activity(gw, limit=5000):
        day = e["ts"][:10]
        if day >= since and day in buckets:
            buckets[day][e["lane"]] += 1
    return [{"day": d, **c} for d, c in buckets.items()]


def routes(gw) -> list:
    s = gw.cfg.owner_secret
    base = f"/{s}"

    async def index(_req: Request) -> Response:
        page = DIST / "index.html"
        if not page.is_file():
            return PlainTextResponse("Dashboard not built. Run: cd services/gateway/dashboard && npm ci && "
                                     "npm run build", status_code=503, headers=SECURITY_HEADERS)
        return FileResponse(page, headers=SECURITY_HEADERS)

    async def to_slash(_req: Request) -> Response:
        return RedirectResponse(f"{base}/", status_code=308, headers={"Referrer-Policy": "no-referrer"})

    async def overview(_req: Request) -> Response:
        st = gw.store
        return _json({
            "owner": gw.cfg.owner_name,
            "pending": st.count_requests("pending"),
            "untriaged": len(st.list_requests("pending", 200, untriaged_only=True)),
            "triage_enabled": gw.triage is not None,
            "outbound_enabled": gw.outbound is not None,
            "signed": gw.identity is not None,
            "audit_chain": st.verify_audit_chain(),
            "outbound_chain_intact": st.verify_outbound_chain(),
            "profile_source": gw.doc_source["profile"]["source"],
            "takes_source": gw.doc_source["takes"]["source"],
            "daily": daily_counts(gw),
        })

    async def list_requests(req: Request) -> Response:
        status = (req.query_params.get("status") or "").strip().lower() or None
        if status and status not in REQUEST_STATUSES:
            return _err("unknown status")
        return _json({"requests": gw.store.list_requests(status, 200)})

    async def resolve(req: Request) -> Response:
        rid = req.path_params["rid"]
        try:
            body = await req.json()
        except Exception:
            return _err("expected JSON")
        decision = str(body.get("decision") or "").strip().lower()
        if decision not in REQUEST_STATUSES or decision == "pending":
            return _err("decision must be approved, declined, or done")
        reply = clean_text(body.get("reply"), 2000) or None
        row = gw.store.resolve_request(rid, decision, reply)
        if row is None:
            return _err(f"no request {rid}", 404)
        gw.store.audit(trace_id="tr_" + uuid.uuid4().hex[:12], tier="owner", tool="resolve_request",
                       client="dashboard", ip_hash=None,
                       args={"request_id": rid, "decision": decision, "reply": reply}, outcome="ok")
        return _json({"ok": True, "request": row})

    async def get_activity(req: Request) -> Response:
        try:
            limit = max(1, min(int(req.query_params.get("limit", 150)), 1000))
        except ValueError:
            limit = 150
        reads = req.query_params.get("reads") == "1"
        return _json({"events": activity(gw, limit, reads)})

    def _doc_view(name: str) -> dict:
        prof = getattr(gw, name)
        src = gw.doc_source[name]
        return {"source": src["source"], "markdown": src["markdown"],
                "sections": [{"title": x["title"], "text": x["text"]} for x in prof.sections],
                "history": gw.store.document_history(name)}

    async def public(req: Request) -> Response:
        from server import TOOL_PERMISSIONS  # late import: server imports this module
        return _json({
            "owner": gw.cfg.owner_name,
            "profile": _doc_view("profile"),
            "takes": _doc_view("takes"),
            "tools": [{"name": k, "permission": v} for k, v in TOOL_PERMISSIONS.items()],
            "availability_connected": gw.freebusy is not None,
            "card": gw.agent_card(gw._base_url(req)),
        })

    async def save_doc(req: Request) -> Response:
        name = req.path_params["name"]
        if name not in DOCUMENT_NAMES:
            return _err("unknown document", 404)
        try:
            body = await req.json()
        except Exception:
            return _err("expected JSON")
        md = body.get("markdown")
        if not isinstance(md, str) or len(md) > 60_000:
            return _err("markdown must be a string under 60k characters")
        row = gw.store.save_document(name, md, "dashboard")
        gw.set_document(name, md, f"dashboard v{row['id']}")
        gw.store.audit(trace_id="tr_" + uuid.uuid4().hex[:12], tier="owner", tool="save_document",
                       client="dashboard", ip_hash=None, args={"name": name, "version": row["id"]}, outcome="ok")
        return _json({"ok": True, **_doc_view(name)})

    async def preview(req: Request) -> Response:
        """What a stranger's agent would get back for a question. Not logged as public traffic."""
        try:
            body = await req.json()
        except Exception:
            return _err("expected JSON")
        q = clean_text(body.get("question"), 500, multiline=False)
        return _json({
            "question": q,
            "ask_about_owner": gw.profile.answer(q) if not gw.profile.empty else [],
            "get_owner_take": gw.takes.answer(q, k=2, fallback=False) if not gw.takes.empty else [],
        })

    return [
        Route(base, to_slash),
        Route(f"{base}/", index),
        Route(f"{base}/api/overview", overview),
        Route(f"{base}/api/requests", list_requests),
        Route(f"{base}/api/requests/{{rid}}", resolve, methods=["POST"]),
        Route(f"{base}/api/activity", get_activity),
        Route(f"{base}/api/public", public),
        Route(f"{base}/api/public/preview", preview, methods=["POST"]),
        Route(f"{base}/api/public/{{name}}", save_doc, methods=["PUT"]),
        Mount(f"{base}/assets", app=StaticFiles(directory=DIST / "assets", check_dir=False)),
    ]


__all__ = ["routes", "activity", "daily_counts"]
