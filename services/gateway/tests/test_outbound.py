"""Outbound tests: identity and signatures, the signed Agent Card, policy, the
hash-chained log, and ask_agent end to end against a fake A2A agent and a
second live gateway over MCP.

The A2A reply fixtures are real responses captured from AgentBin
(https://darrelmiller.github.io/agentbin/) on 2026-11-28.

Run: cd services/gateway && python -m pytest -q
"""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from identity import Identity, keygen, verify_card, verify_request  # noqa: E402
from outbound import Policy, PolicyError, a2a_reply, parse_target  # noqa: E402
from server import Config, Gateway  # noqa: E402
from test_gateway import PROFILE, SECRET, _free_port, call  # noqa: E402

PUBLIC_URL = "https://agent.example.test"
OPEN_POLICY = """
tools: {ask_agent: auto}
targets: {allow: ["127.0.0.1"]}
allow_private_targets: true
"""


def make(tmp_path, name="gw", policy=OPEN_POLICY, key=None, public_url=PUBLIC_URL, **kw) -> Gateway:
    cfg = Config(owner_secret=SECRET, owner_name="Ada", profile_markdown=PROFILE,
                 db_path=str(tmp_path / f"{name}.db"), ip_salt="salt", public_url=public_url,
                 signing_key=key or keygen(), outbound_policy=policy, **kw)
    return Gateway(cfg)


def ask(gw, **args) -> dict:
    content = asyncio.run(gw.owner.call_tool("ask_agent", args))
    blocks = content[0] if isinstance(content, tuple) else content
    return json.loads(blocks[0].text)


def serve(app, port=None):
    port = port or _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return server, th, f"http://127.0.0.1:{port}"


# ---- identity ---------------------------------------------------------------

def test_request_signature_round_trip_and_tamper():
    ident = Identity.from_seed(keygen(), PUBLIC_URL)
    url = "https://other.example/a2a/rpc"
    h = ident.signature_headers("POST", url)
    assert h["Signature-Agent"] == f'sig1="{PUBLIC_URL}"'
    assert 'tag="web-bot-auth"' in h["Signature-Input"] and f'keyid="{ident.kid}"' in h["Signature-Input"]
    assert verify_request("POST", url, h, ident.directory()) == PUBLIC_URL
    with pytest.raises(ValueError, match="bad signature"):
        verify_request("POST", "https://other.example/elsewhere", h, ident.directory())
    with pytest.raises(ValueError, match="bad signature"):
        verify_request("GET", url, h, ident.directory())
    with pytest.raises(ValueError, match="unknown keyid"):
        verify_request("POST", url, h, Identity.from_seed(keygen(), PUBLIC_URL).directory())
    with pytest.raises(ValueError, match="expired"):
        verify_request("POST", url, h, ident.directory(), now=int(time.time()) + 3600)


def test_bad_key_rejected():
    with pytest.raises(ValueError):
        Identity.from_seed("too-short", PUBLIC_URL)


# ---- agent card + well-known routes --------------------------------------------

def test_card_is_signed_and_served(tmp_path):
    gw = make(tmp_path)
    client = TestClient(gw.app())
    card = client.get("/.well-known/agent-card.json").json()
    jwks = client.get("/.well-known/jwks.json").json()
    directory = client.get("/.well-known/http-message-signatures-directory")
    assert directory.headers["content-type"].startswith("application/http-message-signatures-directory+json")
    assert card["supportedInterfaces"][0] == {"url": f"{PUBLIC_URL}/mcp", "protocolBinding": "MCP",
                                              "protocolVersion": card["supportedInterfaces"][0]["protocolVersion"]}
    assert {s["id"] for s in card["skills"]} >= {"ask_about_owner", "request_meeting"}
    assert verify_card(card, jwks) == "valid"
    assert verify_card({**card, "name": "Mallory's agent"}, jwks) == "invalid"
    assert verify_card({k: v for k, v in card.items() if k != "signatures"}, jwks) == "unsigned"
    assert "Ada" in card["name"] and "private" not in json.dumps(card).lower()


def test_unsigned_card_without_key(tmp_path):
    cfg = Config(owner_secret=SECRET, owner_name="Ada", profile_markdown=PROFILE, db_path=str(tmp_path / "x.db"))
    client = TestClient(Gateway(cfg).app())
    card = client.get("/.well-known/agent-card.json").json()
    assert "signatures" not in card
    assert client.get("/.well-known/jwks.json").status_code == 404


# ---- policy -----------------------------------------------------------------

def test_policy_defaults_off_and_validates():
    p = Policy.from_yaml("")
    assert p.mode("ask_agent") == "off" and not p.host_allowed("anything.example")
    with pytest.raises(ValueError):
        Policy.from_yaml("tools: {ask_agent: yolo}")
    with pytest.raises(ValueError):
        Policy.from_yaml("tools: {ask_agent: on}")  # YAML true is not a mode
    assert Policy.from_yaml("tools: {ask_agent: off}").mode("ask_agent") == "off"
    p = Policy.from_yaml("tools: {ask_agent: auto}\ntargets: {allow: ['*.example.com'], deny: ['bad.example.com']}")
    assert p.host_allowed("a.example.com") and not p.host_allowed("bad.example.com")
    assert Policy.from_yaml("kill_switch: true\ntools: {ask_agent: auto}").mode("ask_agent") == "off"


def test_example_policy_parses():
    text = (Path(__file__).resolve().parents[1] / "outbound_policy.example.yaml").read_text()
    p = Policy.from_yaml(text)
    assert p.mode("ask_agent") == "off"  # ships off for Cortex Kit


def test_parse_target():
    assert parse_target("friend.example").card_url == "https://friend.example/.well-known/agent-card.json"
    t = parse_target("https://agentbin.example/echo/")
    assert t.card_url == "https://agentbin.example/echo/.well-known/agent-card.json"
    assert parse_target("https://mcp.deepwiki.com/mcp").kind == "mcp"
    with pytest.raises(PolicyError):
        parse_target("http://plain.example")
    with pytest.raises(PolicyError):
        parse_target("https://user:pw@x.example")


def test_off_and_unconfigured_are_refused_and_logged(tmp_path):
    gw = make(tmp_path, policy="")
    r = ask(gw, target="127.0.0.1", question="hi")
    assert r["ok"] is False and "switched off" in r["detail"]
    cfg = Config(owner_secret=SECRET, owner_name="Ada", profile_markdown=PROFILE, db_path=str(tmp_path / "n.db"))
    r2 = call(Gateway(cfg).owner, "ask_agent", target="127.0.0.1", question="hi")
    assert "not set up" in r2["detail"]
    log = call(gw.owner, "outbound_activity")
    assert log["calls"][0]["status"] == "denied" and log["chain_intact"]


def test_private_addresses_refused_without_dev_flag(tmp_path):
    gw = make(tmp_path, policy="tools: {ask_agent: auto}\ntargets: {allow: ['localhost']}")
    r = ask(gw, target="localhost", question="hi")
    assert r["ok"] is False and "private address" in r["detail"]


def test_not_on_allowlist(tmp_path):
    gw = make(tmp_path, policy="tools: {ask_agent: auto}\ntargets: {allow: ['friend.example']}")
    r = ask(gw, target="stranger.example", question="hi")
    assert "allowlist" in r["detail"]


def test_ask_me_needs_confirmation(tmp_path):
    gw = make(tmp_path, policy=OPEN_POLICY.replace("auto", "ask_me"))
    r = ask(gw, target="127.0.0.1", question="hi")
    assert r["error"] == "needs_confirmation"


def test_question_length_cap(tmp_path):
    gw = make(tmp_path, policy=OPEN_POLICY + "disclosure: {max_question_chars: 10}\n")
    assert "longer than 10" in ask(gw, target="127.0.0.1", question="x" * 50)["detail"]


# ---- A2A reply parsing (real AgentBin responses) --------------------------------

def test_a2a_reply_v1_message_and_task():
    msg = {"message": {"role": "ROLE_AGENT", "parts": [{"text": "Echo: hello from a test client"}],
                       "messageId": "bf9a", "contextId": "2214"}}
    assert a2a_reply(msg) == {"text": "Echo: hello from a test client", "state": "message", "context_id": "2214"}
    task = {"task": {"id": "324c", "contextId": "2bde", "status": {"state": "TASK_STATE_COMPLETED"},
                     "artifacts": [{"artifactId": "7781", "name": "result",
                                    "parts": [{"text": "[task-lifecycle] Processed: task-lifecycle hello"}]}]}}
    r = a2a_reply(task)
    assert r["state"] == "completed" and r["task_id"] == "324c" and "Processed" in r["text"]


def test_a2a_reply_v03_task():
    task = {"kind": "task", "id": "6a97", "contextId": "f93a", "status": {"state": "completed"},
            "artifacts": [{"parts": [{"kind": "text", "text": "[task-lifecycle] Processed: hi"}]}]}
    assert a2a_reply(task)["text"] == "[task-lifecycle] Processed: hi"


# ---- ask_agent end to end: fake A2A agent ----------------------------------------

def fake_a2a_agent(version: str, signer_jwks: dict, seen: list):
    async def card(request: Request):
        base = f"{request.url.scheme}://{request.url.netloc}"
        if version == "1.0":
            body = {"name": "Echo Agent", "supportedInterfaces": [
                {"url": f"{base}/echo", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}], "skills": []}
        else:
            body = {"name": "Echo Agent", "url": f"{base}/echo", "preferredTransport": "JSONRPC",
                    "protocolVersion": "0.3.0", "skills": []}
        return JSONResponse(body)

    async def echo(request: Request):
        try:
            agent = verify_request(request.method, str(request.url), dict(request.headers), signer_jwks)
        except ValueError as exc:
            agent = f"invalid: {exc}"
        rpc = await request.json()
        seen.append({"agent": agent, "method": rpc["method"], "version": request.headers.get("a2a-version")})
        text = rpc["params"]["message"]["parts"][0]["text"]
        if rpc["method"] == "SendMessage":
            result = {"message": {"role": "ROLE_AGENT", "parts": [{"text": f"Echo: {text}"}], "contextId": "c1"}}
        else:
            result = {"kind": "message", "role": "agent", "parts": [{"kind": "text", "text": f"Echo: {text}"}]}
        return JSONResponse({"jsonrpc": "2.0", "id": rpc["id"], "result": result})

    return Starlette(routes=[Route("/.well-known/agent-card.json", card), Route("/echo", echo, methods=["POST"])])


@pytest.mark.parametrize("version,protocol", [("1.0", "a2a-1.0"), ("0.3", "a2a-0.3")])
def test_ask_agent_a2a_signed_round_trip(tmp_path, version, protocol):
    gw = make(tmp_path)
    seen: list = []
    server, th, base = serve(fake_a2a_agent(version, gw.identity.directory(), seen))
    try:
        r = ask(gw, target=base, question="what are you working on?")
    finally:
        server.should_exit = True
        th.join(5)
    assert r["ok"] is True, r
    assert r["protocol"] == protocol and r["card_signature"] == "unsigned"
    assert r["untrusted"]["text"] == "Echo: what are you working on?"
    assert seen[0]["agent"] == PUBLIC_URL  # the other side verified our signature
    assert seen[0]["version"] == ("1.0" if version == "1.0" else None)
    log = call(gw.owner, "outbound_activity")
    row = log["calls"][0]
    assert row["status"] == "ok" and row["protocol"] == protocol and row["request"]["question"]
    assert row["untrusted_response"]["text"].startswith("Echo:") and log["chain_intact"]


def test_per_target_daily_cap(tmp_path):
    gw = make(tmp_path, policy=OPEN_POLICY + "limits: {calls_per_target_per_day: 1}\n")
    server, th, base = serve(fake_a2a_agent("1.0", gw.identity.directory(), []))
    try:
        assert ask(gw, target=base, question="one")["ok"] is True
        r = ask(gw, target=base, question="two")
    finally:
        server.should_exit = True
        th.join(5)
    assert r["ok"] is False and "daily cap" in r["detail"]


# ---- ask_agent end to end: another gateway over MCP --------------------------------

def test_ask_agent_mcp_against_another_gateway(tmp_path):
    caller = make(tmp_path, name="caller")
    port = _free_port()
    target = make(tmp_path, name="target", public_url=f"http://127.0.0.1:{port}")
    server, th, base = serve(target.app(), port)
    try:
        listing = ask(caller, target=f"{base}/mcp")
        answer = ask(caller, target=f"{base}/mcp", question="what does Ada work on?", tool="ask_about_owner")
        write = ask(caller, target=f"{base}/mcp", tool="leave_message",
                    arguments={"your_name": "x", "your_contact": "y", "subject": "s", "message": "m"})
        via_card = ask(caller, target=base, question="background", tool="ask_about_owner")
    finally:
        server.should_exit = True
        th.join(5)
    names = {t["name"] for t in listing["untrusted"]["tools"]}
    assert {"ask_about_owner", "get_owner_take", "check_availability"} <= names
    assert not names & {"leave_message", "request_meeting", "check_request"}
    assert answer["ok"] is True and answer["protocol"] == "mcp"
    assert json.loads(answer["untrusted"]["text"])["owner"] == "Ada"
    assert write["ok"] is False and "read-only" in write["untrusted"]["error"]
    assert target.store.count_requests("pending") == 0  # nothing was filed on the other side
    # Resolving the target's signed card, verifying it, then using its MCP interface.
    assert via_card["ok"] is True and via_card["card_signature"] == "valid" and via_card["protocol"] == "mcp"
    assert caller.store.pin_agent_key("127.0.0.1", target.identity.kid) is False  # pinned on first contact


def test_key_change_is_flagged(tmp_path):
    caller = make(tmp_path, name="caller")
    assert caller.store.pin_agent_key("friend.example", "kid-1") is False
    assert caller.store.pin_agent_key("friend.example", "kid-1") is False
    assert caller.store.pin_agent_key("friend.example", "kid-2") is True


# ---- log integrity ---------------------------------------------------------------

def test_outbound_log_hash_chain_detects_edits(tmp_path):
    gw = make(tmp_path)
    for host in ("a.example", "b.example", "c.example"):
        rid = gw.store.log_outbound(trace_id="t", triggered_by="owner", tool="ask_agent", target=host,
                                    target_host=host, request={"question": "q"}, policy_verdict="allowed")
        gw.store.finish_outbound(rid, status="ok", response={"text": "fine"}, latency_ms=5)
    assert gw.store.verify_outbound_chain()
    gw.store._db.execute("UPDATE outbound_calls SET request_json = '{\"question\": \"edited\"}' WHERE id = 2")
    assert not gw.store.verify_outbound_chain()


def test_outbound_log_detects_reply_edits_and_deletes(tmp_path):
    gw = make(tmp_path)
    for host in ("a.example", "b.example"):
        rid = gw.store.log_outbound(trace_id="t", triggered_by="owner", tool="ask_agent", target=host,
                                    target_host=host, request={}, policy_verdict="allowed")
        gw.store.finish_outbound(rid, status="ok", response={"text": "fine"}, latency_ms=5)
    gw.store._db.execute("UPDATE outbound_calls SET response_json = '{\"text\": \"forged\"}' WHERE id = 2")
    assert not gw.store.verify_outbound_chain()
    gw.store._db.execute("DELETE FROM outbound_calls WHERE id = 2")
    assert gw.store.verify_outbound_chain()  # removing the last row is undetectable by a chain alone
    gw.store.log_outbound(trace_id="t", triggered_by="owner", tool="ask_agent", target="c", target_host="c",
                          request={}, policy_verdict="allowed")
    gw.store._db.execute("DELETE FROM outbound_calls WHERE id = 1")
    assert not gw.store.verify_outbound_chain()


def test_key_is_created_on_the_volume_and_reused(tmp_path):
    key_path = tmp_path / "data" / "signing.key"
    cfg = dict(owner_secret=SECRET, owner_name="Ada", profile_markdown=PROFILE, db_path=str(tmp_path / "k.db"),
               public_url=PUBLIC_URL, signing_key_path=str(key_path))
    first = Gateway(Config(**cfg)).identity.kid
    assert key_path.is_file() and (key_path.stat().st_mode & 0o777) == 0o600
    assert Gateway(Config(**cfg)).identity.kid == first  # same key after a restart


def test_no_public_url_means_no_key_file(tmp_path):
    key_path = tmp_path / "signing.key"
    cfg = Config(owner_secret=SECRET, owner_name="Ada", profile_markdown=PROFILE, db_path=str(tmp_path / "k.db"),
                 signing_key_path=str(key_path))
    assert Gateway(cfg).identity is None and not key_path.exists()
