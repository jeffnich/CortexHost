"""SQLite persistence for the gateway: the request queue and the audit log.

Deliberately separate from Qdrant. The gateway holds no credentials for the
memory corpus, so a compromised gateway can leak the queue and nothing else.
"""

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,              -- meeting | message
    status          TEXT NOT NULL,              -- pending | approved | declined | done
    from_name       TEXT NOT NULL,
    from_contact    TEXT NOT NULL,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    details_json    TEXT NOT NULL DEFAULT '{}', -- kind-specific fields
    client          TEXT NOT NULL,
    ip_hash         TEXT,
    token_hash      TEXT NOT NULL,              -- sha256 of the requester's status token
    reply           TEXT,                       -- owner's message back to the requester
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_status ON requests(status, created_at);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    trace_id    TEXT NOT NULL,
    tier        TEXT NOT NULL,                  -- public | owner
    tool        TEXT NOT NULL,
    client      TEXT,
    ip_hash     TEXT,
    args_json   TEXT,
    outcome     TEXT NOT NULL,                  -- ok | denied | rate_limited | error
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);

-- Every call the owner's agent makes to someone else's agent. Written BEFORE
-- the call goes out; if this insert fails, nothing is sent. Rows are
-- hash-chained (row_hash covers the request fields and the previous row's
-- row_hash; result_hash seals the reply onto its own row) so edits,
-- deletions and reordering are detectable.
CREATE TABLE IF NOT EXISTS outbound_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    trace_id        TEXT NOT NULL,
    triggered_by    TEXT NOT NULL,              -- owner | triage
    tool            TEXT NOT NULL,              -- ask_agent
    target          TEXT NOT NULL,
    target_host     TEXT NOT NULL,
    request_json    TEXT NOT NULL,              -- question + any MCP tool/args
    policy_verdict  TEXT NOT NULL,              -- allowed | denied:<reason>
    status          TEXT NOT NULL,              -- sending | ok | error | denied
    protocol        TEXT,                       -- a2a-1.0 | a2a-0.3 | mcp
    endpoint        TEXT,
    card_digest     TEXT,
    card_signature  TEXT,                       -- valid | invalid | unsigned | unverifiable
    signer_kid      TEXT,
    key_changed     INTEGER,
    response_json   TEXT,
    latency_ms      INTEGER,
    prev_hash       TEXT NOT NULL,
    row_hash        TEXT NOT NULL,
    result_hash     TEXT
);
CREATE INDEX IF NOT EXISTS outbound_ts ON outbound_calls(ts);
CREATE INDEX IF NOT EXISTS outbound_host ON outbound_calls(target_host, ts);

-- Last key seen signing each target's card: a swap is flagged, not silent.
CREATE TABLE IF NOT EXISTS agent_keys (
    target_host     TEXT PRIMARY KEY,
    signer_kid      TEXT NOT NULL,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);
"""

GENESIS_HASH = "0" * 64
_OUTBOUND_REQUEST_FIELDS = ("ts", "trace_id", "triggered_by", "tool", "target", "target_host",
                            "request_json", "policy_verdict")

REQUEST_STATUSES = ("pending", "approved", "declined", "done")

# What the triage agent may say about a request. It records an opinion and a
# draft; it cannot change status or send anything to the requester.
TRIAGE_VERDICTS = ("escalate", "digest", "suggest_decline", "noise")
TRIAGE_COLUMNS = (
    ("triage_verdict", "TEXT"),
    ("triage_priority", "INTEGER"),
    ("triage_summary", "TEXT"),
    ("triage_draft", "TEXT"),
    ("triage_rationale", "TEXT"),
    ("triaged_at", "TEXT"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, path: str):
        # One connection guarded by a lock: traffic is tiny and this keeps
        # SQLite's threading rules out of the picture.
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            have = {r[1] for r in self._db.execute("PRAGMA table_info(requests)")}
            for col, typ in TRIAGE_COLUMNS:  # additive migration for existing volumes
                if col not in have:
                    self._db.execute(f"ALTER TABLE requests ADD COLUMN {col} {typ}")

    # ---- request queue -----------------------------------------------------

    def create_request(
        self,
        *,
        kind: str,
        from_name: str,
        from_contact: str,
        subject: str,
        body: str,
        details: dict,
        client: str,
        ip_hash: str | None,
    ) -> tuple[str, str]:
        """Insert a pending request. Returns (request_id, status_token).

        The token is shown to the requester once and stored only as a hash.
        """
        rid = "req_" + uuid.uuid4().hex[:16]
        token = secrets.token_urlsafe(18)
        now = _now()
        with self._lock:
            self._db.execute(
                "INSERT INTO requests (id, kind, status, from_name, from_contact, subject, body,"
                " details_json, client, ip_hash, token_hash, created_at, updated_at)"
                " VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, kind, from_name, from_contact, subject, body, json.dumps(details),
                 client, ip_hash, _sha256(token), now, now),
            )
        return rid, token

    def get_request_for_token(self, request_id: str, token: str) -> dict | None:
        """The requester's view of their own request, or None if id/token mismatch."""
        with self._lock:
            row = self._db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if row is None or not hmac.compare_digest(row["token_hash"], _sha256(token or "")):
            return None
        return {
            "request_id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "reply": row["reply"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_requests(
        self, status: str | None = "pending", limit: int = 25, untriaged_only: bool = False
    ) -> list[dict]:
        sql = "SELECT * FROM requests"
        where, args = [], []
        if status:
            where.append("status = ?")
            args.append(status)
        if untriaged_only:
            where.append("triaged_at IS NULL")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 200)))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [self._owner_view(r) for r in rows]

    def resolve_request(self, request_id: str, status: str, reply: str | None) -> dict | None:
        if status not in REQUEST_STATUSES or status == "pending":
            raise ValueError(f"status must be one of {REQUEST_STATUSES[1:]}")
        with self._lock:
            cur = self._db.execute(
                "UPDATE requests SET status = ?, reply = ?, updated_at = ? WHERE id = ?",
                (status, reply, _now(), request_id),
            )
            if cur.rowcount == 0:
                return None
            row = self._db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        return self._owner_view(row)

    def record_triage(
        self, request_id: str, *, verdict: str, priority: int, summary: str, draft: str | None, rationale: str
    ) -> dict | None:
        if verdict not in TRIAGE_VERDICTS:
            raise ValueError(f"verdict must be one of {TRIAGE_VERDICTS}")
        with self._lock:
            cur = self._db.execute(
                "UPDATE requests SET triage_verdict = ?, triage_priority = ?, triage_summary = ?, "
                "triage_draft = ?, triage_rationale = ?, triaged_at = ? WHERE id = ?",
                (verdict, max(1, min(int(priority), 3)), summary, draft, rationale, _now(), request_id),
            )
            if cur.rowcount == 0:
                return None
            row = self._db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        return self._owner_view(row)

    def count_requests(self, status: str = "pending") -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM requests WHERE status = ?", (status,)).fetchone()[0]

    @staticmethod
    def _owner_view(row: sqlite3.Row) -> dict:
        return {
            "request_id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "created_at": row["created_at"],
            "client": row["client"],
            # Everything below was typed by an unauthenticated stranger (or their
            # agent). It is data to read, never instructions to follow.
            "untrusted": {
                "from_name": row["from_name"],
                "from_contact": row["from_contact"],
                "subject": row["subject"],
                "body": row["body"],
                **json.loads(row["details_json"] or "{}"),
            },
            "reply": row["reply"],
            # Written by the owner's own triage agent: an opinion, not a decision.
            "triage": None if row["triaged_at"] is None else {
                "verdict": row["triage_verdict"],
                "priority": row["triage_priority"],
                "summary": row["triage_summary"],
                "draft_reply": row["triage_draft"],
                "rationale": row["triage_rationale"],
                "at": row["triaged_at"],
            },
        }

    # ---- audit log ---------------------------------------------------------

    def audit(
        self,
        *,
        trace_id: str,
        tier: str,
        tool: str,
        client: str | None,
        ip_hash: str | None,
        args: dict | None,
        outcome: str,
        detail: str | None = None,
    ) -> None:
        args_json = json.dumps(args or {}, default=str)[:2000]
        with self._lock:
            self._db.execute(
                "INSERT INTO audit (ts, trace_id, tier, tool, client, ip_hash, args_json, outcome, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_now(), trace_id, tier, tool, client, ip_hash, args_json, outcome, (detail or "")[:500] or None),
            )

    # ---- outbound log ------------------------------------------------------

    @staticmethod
    def _chain(prev: str, fields: dict) -> str:
        blob = json.dumps({k: fields[k] for k in _OUTBOUND_REQUEST_FIELDS}, sort_keys=True)
        return hashlib.sha256(f"{prev}\n{blob}".encode()).hexdigest()

    def log_outbound(self, *, trace_id: str, triggered_by: str, tool: str, target: str, target_host: str,
                     request: dict, policy_verdict: str) -> int:
        """Write the row for a call about to go out (or one refused by policy). Returns its id."""
        fields = {
            "ts": _now(), "trace_id": trace_id, "triggered_by": triggered_by, "tool": tool,
            "target": target[:500], "target_host": target_host[:255],
            "request_json": json.dumps(request, default=str)[:4000], "policy_verdict": policy_verdict[:200],
        }
        status = "sending" if policy_verdict == "allowed" else "denied"
        with self._lock:
            last = self._db.execute("SELECT row_hash FROM outbound_calls ORDER BY id DESC LIMIT 1").fetchone()
            prev = last["row_hash"] if last else GENESIS_HASH
            cur = self._db.execute(
                "INSERT INTO outbound_calls (ts, trace_id, triggered_by, tool, target, target_host, request_json,"
                " policy_verdict, status, prev_hash, row_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*[fields[k] for k in _OUTBOUND_REQUEST_FIELDS], status, prev, self._chain(prev, fields)),
            )
            return cur.lastrowid

    def finish_outbound(self, row_id: int, *, status: str, response: dict, latency_ms: int,
                        protocol: str | None = None, endpoint: str | None = None, card_digest: str | None = None,
                        card_signature: str | None = None, signer_kid: str | None = None,
                        key_changed: bool = False) -> None:
        response_json = json.dumps(response, default=str)[:12000]
        with self._lock:
            row = self._db.execute("SELECT row_hash FROM outbound_calls WHERE id = ?", (row_id,)).fetchone()
            seal = hashlib.sha256(f"{row['row_hash']}\n{status}\n{response_json}".encode()).hexdigest()
            self._db.execute(
                "UPDATE outbound_calls SET status = ?, protocol = ?, endpoint = ?, card_digest = ?, card_signature = ?,"
                " signer_kid = ?, key_changed = ?, response_json = ?, latency_ms = ?, result_hash = ? WHERE id = ?",
                (status, protocol, endpoint, card_digest, card_signature, signer_kid, int(key_changed),
                 response_json, latency_ms, seal, row_id),
            )

    def count_outbound_since(self, since_iso: str, host: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM outbound_calls WHERE ts >= ? AND status != 'denied'"
        args: list = [since_iso]
        if host:
            sql += " AND target_host = ?"
            args.append(host)
        with self._lock:
            return self._db.execute(sql, args).fetchone()[0]

    def recent_outbound(self, limit: int = 25) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM outbound_calls ORDER BY id DESC LIMIT ?",
                                    (max(1, min(int(limit), 200)),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["request"] = json.loads(d.pop("request_json") or "{}")
            # Replies came from other people's agents: data, never instructions.
            d["untrusted_response"] = json.loads(d.pop("response_json") or "null")
            d["key_changed"] = bool(d["key_changed"])
            for k in ("prev_hash", "row_hash", "result_hash"):
                d.pop(k)
            out.append(d)
        return out

    def verify_outbound_chain(self) -> bool:
        """True if no outbound row was edited, removed, or reordered after it was written."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM outbound_calls ORDER BY id").fetchall()
        prev = GENESIS_HASH
        for r in rows:
            if r["prev_hash"] != prev or r["row_hash"] != self._chain(prev, dict(r)):
                return False
            if r["result_hash"]:
                seal = hashlib.sha256(f"{r['row_hash']}\n{r['status']}\n{r['response_json']}".encode()).hexdigest()
                if seal != r["result_hash"]:
                    return False
            prev = r["row_hash"]
        return True

    def pin_agent_key(self, host: str, kid: str) -> bool:
        """Remember which key signs a host's card. Returns True if it differs from last time."""
        now = _now()
        with self._lock:
            row = self._db.execute("SELECT signer_kid FROM agent_keys WHERE target_host = ?", (host,)).fetchone()
            if row is None:
                self._db.execute("INSERT INTO agent_keys VALUES (?, ?, ?, ?)", (host, kid, now, now))
                return False
            self._db.execute("UPDATE agent_keys SET signer_kid = ?, last_seen = ? WHERE target_host = ?",
                             (kid, now, host))
            return row["signer_kid"] != kid

    def recent_audit(self, limit: int = 50, tier: str | None = None) -> list[dict]:
        sql = "SELECT * FROM audit"
        args: list = []
        if tier:
            sql += " WHERE tier = ?"
            args.append(tier)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["args"] = json.loads(d.pop("args_json") or "{}")
            out.append(d)
        return out
