"""Access log: every tool call leaves a hash-chained row, and the dashboard feed serves it.

Run: cd services/mcp && python -m pytest -q
"""

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

SECRET = "mcp-secret-for-tests-0123456789"  # gitleaks:allow (test fixture)


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    for k, v in {"QDRANT_URL": "http://qdrant.invalid", "CORTEX_TENANT_ID": "t", "CORTEX_USER_ID": "u",
                 "OPENAI_API_KEY": "sk-test", "CORTEX_ACCESS_DB": str(tmp_path / "access.db")}.items():
        monkeypatch.setenv(k, v)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.modules.pop("server", None)
    import server  # noqa: PLC0415

    def fake_q(method, path, body=None):
        if path.endswith("/points/search"):
            return {"result": [{"id": "m1", "score": 0.9, "payload": {"text": "a"}},
                               {"id": "m2", "score": 0.8, "payload": {"text": "b"}}]}
        if path.endswith("/points/scroll"):
            return {"result": {"points": [{"id": "m3", "payload": {"text": "c"}}]}}
        return {"result": {}}

    monkeypatch.setattr(server, "_q", fake_q)
    monkeypatch.setattr(server, "_embed", lambda text: [0.0])
    monkeypatch.setattr(server, "_access", None)
    yield server
    sys.path.pop(0)


def call(server, tool, **args):
    out = asyncio.run(server.mcp.call_tool(tool, args))
    blocks = out[0] if isinstance(out, tuple) else out
    return json.loads(blocks[0].text)


def test_reads_and_writes_are_logged_with_ids(srv):
    call(srv, "search_memory", query="what did I decide")
    call(srv, "recent_memories", days=3)
    stored = call(srv, "store_memory", text="new fact", dedupe_key="k1")
    call(srv, "add_todo", text="ship it")
    rows = srv._access.recent()
    assert [r["tool"] for r in rows] == ["add_todo", "store_memory", "recent_memories", "search_memory"]
    assert rows[3]["kind"] == "read" and rows[3]["memory_ids"] == ["m1", "m2"]
    assert rows[3]["detail"] == "query: what did I decide"
    assert rows[1]["kind"] == "write" and rows[1]["memory_ids"] == [stored["memory_id"]]
    assert srv._access.verify()["intact"]


def test_tampering_breaks_the_chain(srv, tmp_path):
    call(srv, "search_memory", query="one")
    call(srv, "search_memory", query="two")
    db = sqlite3.connect(tmp_path / "access.db")
    db.execute("UPDATE access SET client = 'someone-else' WHERE id = 1")
    db.commit()
    assert srv._access.verify() == {"intact": False, "checked": 2, "broken_at": 1}


def test_logging_failure_never_breaks_the_tool(srv, monkeypatch):
    class Broken:
        def record(self, *a, **k):
            raise RuntimeError("disk full")

    monkeypatch.setattr(srv, "_access", Broken())
    assert call(srv, "search_memory", query="still works")["count"] == 2


def test_access_feed_route(srv):
    from starlette.testclient import TestClient  # noqa: PLC0415

    srv.register_access_route(SECRET)
    call(srv, "search_memory", query="q")
    with TestClient(srv.mcp.streamable_http_app()) as c:
        r = c.get(f"/{SECRET}/access.json")
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "*"
        body = r.json()
        assert body["chain"]["intact"] and body["entries"][0]["tool"] == "search_memory"
        assert c.get("/wrong-secret/access.json").status_code == 404
