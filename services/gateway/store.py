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
"""

REQUEST_STATUSES = ("pending", "approved", "declined", "done")


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

    def list_requests(self, status: str | None = "pending", limit: int = 25) -> list[dict]:
        sql = "SELECT * FROM requests"
        args: list = []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
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
