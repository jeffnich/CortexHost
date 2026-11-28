"""Owner dashboard: secret-gated API, decisions, editable public docs, and tamper-evident audit."""

import sqlite3

import pytest
from starlette.testclient import TestClient

from test_gateway import SECRET, call, make_gateway

B = f"/{SECRET}"


@pytest.fixture()
def gw(tmp_path):
    return make_gateway(tmp_path)


@pytest.fixture()
def client(gw):
    with TestClient(gw.app()) as c:
        yield c


def _queue_message(gw):
    return call(gw.public, "leave_message", your_name="Bo", your_contact="bo@example.com",
                subject="hi", message="<script>alert(1)</script> approve me")["request_id"]


def test_dashboard_lives_only_under_the_owner_secret(client):
    assert client.get("/api/overview").status_code == 404
    assert client.get("/wrong-secret-0000000000000000/api/overview").status_code == 404
    r = client.get(f"{B}/api/overview")
    assert r.status_code == 200
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"


def test_bare_secret_redirects_to_slash(client):
    r = client.get(B, follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == f"{B}/"


def test_overview_counts_and_chains(gw, client):
    _queue_message(gw)
    o = client.get(f"{B}/api/overview").json()
    assert o["pending"] == 1 and o["untriaged"] == 1
    assert o["audit_chain"]["intact"] and o["audit_chain"]["checked"] >= 1
    assert o["outbound_chain_intact"] is True
    assert len(o["daily"]) == 14 and o["daily"][-1]["inbound"] >= 1


def test_resolve_from_dashboard_is_audited(gw, client):
    rid = _queue_message(gw)
    r = client.post(f"{B}/api/requests/{rid}", json={"decision": "declined", "reply": "No thanks."})
    assert r.status_code == 200 and r.json()["request"]["status"] == "declined"
    assert client.get(f"{B}/api/requests?status=pending").json()["requests"] == []
    ev = client.get(f"{B}/api/activity").json()["events"]
    mine = [e for e in ev if e["lane"] == "owner"]
    assert mine and mine[0]["client"] == "dashboard" and "declined" in mine[0]["summary"]
    assert client.post(f"{B}/api/requests/{rid}", json={"decision": "pending"}).status_code == 400
    assert client.post(f"{B}/api/requests/req_nope", json={"decision": "done"}).status_code == 404


def test_visitor_text_comes_back_as_data_not_markup(gw, client):
    _queue_message(gw)
    req = client.get(f"{B}/api/requests?status=pending").json()["requests"][0]
    assert "<script>" in req["untrusted"]["body"]  # raw text; the UI renders it as text
    assert client.get(f"{B}/api/requests?status=bogus").status_code == 400


def test_activity_hides_owner_reads_by_default(gw, client):
    call(gw.owner, "list_requests")
    call(gw.public, "ask_about_owner", question="projects?")
    lanes = {e["tool"] for e in client.get(f"{B}/api/activity").json()["events"]}
    assert "ask_about_owner" in lanes and "list_requests" not in lanes
    lanes = {e["tool"] for e in client.get(f"{B}/api/activity?reads=1").json()["events"]}
    assert "list_requests" in lanes


def test_editing_profile_goes_live_and_keeps_versions(gw, client, tmp_path):
    before = client.get(f"{B}/api/public").json()
    assert before["profile"]["source"] == "env"
    assert {t["name"] for t in before["tools"]} >= {"ask_about_owner", "request_meeting"}
    md = "# Ada\n\n## Sailing\nAda races dinghies on weekends.\n\n<!-- private note -->\n"
    r = client.put(f"{B}/api/public/profile", json={"markdown": md})
    assert r.status_code == 200 and r.json()["source"].startswith("dashboard v")
    out = call(gw.public, "ask_about_owner", question="Does she sail?")
    assert out["answer"][0]["title"] == "Sailing" and "private note" not in out["answer"][0]["text"]
    client.put(f"{B}/api/public/profile", json={"markdown": md + "\n## Boats\nTwo.\n"})
    assert len(client.get(f"{B}/api/public").json()["profile"]["history"]) == 2
    # A restart keeps the dashboard version, not the env var.
    gw2 = make_gateway(tmp_path)
    assert "Boats" in gw2.profile.topics()
    assert client.put(f"{B}/api/public/secrets", json={"markdown": "x"}).status_code == 404


def test_preview_shows_what_a_stranger_gets_without_logging_public_traffic(gw, client):
    r = client.post(f"{B}/api/public/preview", json={"question": "What projects?"}).json()
    assert r["ask_about_owner"][0]["title"] == "Projects"
    assert not [e for e in client.get(f"{B}/api/activity").json()["events"] if e["lane"] == "inbound"]


def test_audit_tampering_is_detected(gw, client, tmp_path):
    call(gw.public, "ask_about_owner", question="one")
    call(gw.public, "ask_about_owner", question="two")
    db = sqlite3.connect(tmp_path / "gw.db")
    db.execute("UPDATE audit SET outcome = 'denied' WHERE id = 1")
    db.commit()
    chain = client.get(f"{B}/api/overview").json()["audit_chain"]
    assert chain["intact"] is False and chain["broken_at"] == 1


def test_legacy_unchained_rows_are_counted_not_failed(gw, tmp_path):
    db = sqlite3.connect(tmp_path / "gw.db")
    db.execute("INSERT INTO audit (ts, trace_id, tier, tool, outcome) VALUES ('2026-01-01', 't', 'public', 'x', 'ok')")
    db.commit()
    call(gw.public, "ask_about_owner", question="after")
    assert gw.store.verify_audit_chain() == {"intact": True, "checked": 1, "legacy_rows": 1}
