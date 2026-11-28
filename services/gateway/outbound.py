"""Outbound: the owner's agent asking other agents questions.

One tool for now, ask_agent, and it can only ask. It never fills in a form,
leaves a message, books anything, or calls a tool that writes. The policy
(outbound_policy.example.yaml) is enforced here in code, not in a prompt.

Flow for one call: policy check -> log row written -> resolve the target's
Agent Card (or use an MCP URL directly) -> signed request -> reply stored and
returned wrapped as `untrusted`. If the log write fails, nothing is sent.

Protocols:
  A2A v1.0  JSON-RPC `SendMessage` with `A2A-Version: 1.0`
  A2A v0.3  JSON-RPC `message/send` (cards with a top-level `url`)
  MCP       a read-only tool on a remote MCP server (streamable HTTP)
"""

import asyncio
import fnmatch
import hashlib
import ipaddress
import json
import socket
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import httpx
import yaml

from identity import JWKS_PATH, Identity, SigningAuth, b64url_decode, verify_card

TOOL_MODES = ("off", "ask_me", "auto")
MAX_REPLY_CHARS = 8000
CARD_PATH = "/.well-known/agent-card.json"
USER_AGENT = "cortex-gateway-outbound/1"

# MCP tools ask_agent may call. Anything else is refused unless the remote
# server marks it readOnlyHint. Tools that file requests (leave_message,
# request_meeting) are never "asking": they carry the owner's words somewhere.
DEFAULT_MCP_READ_TOOLS = (
    "ask_about_owner", "get_owner_take", "check_availability",
    "ask_question", "read_wiki_structure", "read_wiki_contents",
)
NEVER_MCP_TOOLS = ("leave_message", "request_meeting", "check_request", "resolve_request", "record_triage")


class PolicyError(ValueError):
    """The call is not allowed. The message is safe to show the owner."""


@dataclass
class Policy:
    kill_switch: bool = False
    tools: dict = field(default_factory=lambda: {"ask_agent": "off"})
    allow: list = field(default_factory=list)
    deny: list = field(default_factory=list)
    calls_per_hour: int = 20
    calls_per_target_per_day: int = 10
    max_question_chars: int = 1000
    mcp_read_tools: list = field(default_factory=lambda: list(DEFAULT_MCP_READ_TOOLS))
    allow_private_targets: bool = False  # tests and local dev only

    @classmethod
    def from_yaml(cls, text: str) -> "Policy":
        raw = yaml.safe_load(text or "") or {}
        if not isinstance(raw, dict):
            raise ValueError("outbound policy must be a YAML mapping")
        targets = raw.get("targets") or {}
        limits = raw.get("limits") or {}
        disclosure = raw.get("disclosure") or {}
        tools = {**{"ask_agent": "off"}, **(raw.get("tools") or {})}
        # YAML 1.1 reads a bare `off` as False. Accept that one; refuse a bare `on`/`yes`.
        tools = {k: ("off" if v is False else v) for k, v in tools.items()}
        for name, mode in tools.items():
            if mode not in TOOL_MODES:
                raise ValueError(f"tools.{name} must be one of {TOOL_MODES}")
        return cls(
            kill_switch=bool(raw.get("kill_switch", False)),
            tools=tools,
            allow=[str(x).lower() for x in targets.get("allow") or []],
            deny=[str(x).lower() for x in targets.get("deny") or []],
            calls_per_hour=int(limits.get("calls_per_hour", 20)),
            calls_per_target_per_day=int(limits.get("calls_per_target_per_day", 10)),
            max_question_chars=int(disclosure.get("max_question_chars", 1000)),
            mcp_read_tools=list(raw.get("mcp_read_tools") or DEFAULT_MCP_READ_TOOLS),
            allow_private_targets=bool(raw.get("allow_private_targets", False)),
        )

    def mode(self, tool: str) -> str:
        return "off" if self.kill_switch else self.tools.get(tool, "off")

    def host_allowed(self, host: str) -> bool:
        host = host.lower()
        if any(fnmatch.fnmatch(host, pat) for pat in self.deny):
            return False
        return any(fnmatch.fnmatch(host, pat) for pat in self.allow)

    def summary(self) -> dict:
        return {"kill_switch": self.kill_switch, "tools": self.tools, "allow": self.allow,
                "deny": self.deny, "calls_per_hour": self.calls_per_hour,
                "calls_per_target_per_day": self.calls_per_target_per_day,
                "max_question_chars": self.max_question_chars}


# ---- target parsing and network safety ----------------------------------------

@dataclass
class Target:
    url: str          # normalized URL as given
    host: str
    kind: str         # "mcp" (direct MCP endpoint) or "card" (resolve an Agent Card)
    card_url: str = ""


def parse_target(target: str, allow_http: bool = False) -> Target:
    t = (target or "").strip()
    if not t:
        raise PolicyError("target is required")
    if "://" not in t:
        t = "https://" + t
    p = urlsplit(t)
    if p.scheme != "https" and not (allow_http and p.scheme == "http"):
        raise PolicyError("target must be https")
    if not p.hostname or p.username or p.password:
        raise PolicyError("target must be a plain host or URL")
    path = p.path.rstrip("/")
    url = urlunsplit((p.scheme, p.netloc.lower(), path, "", ""))
    if path.endswith("/mcp"):
        return Target(url=url, host=p.hostname.lower(), kind="mcp")
    if path.endswith(CARD_PATH):
        card_url = url
    else:
        card_url = urlunsplit((p.scheme, p.netloc.lower(), path + CARD_PATH, "", ""))
    return Target(url=url, host=p.hostname.lower(), kind="card", card_url=card_url)


def _is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return addr.is_global and not addr.is_multicast


async def check_host_is_public(host: str) -> None:
    """Refuse targets that resolve to private, loopback or link-local addresses (SSRF)."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise PolicyError(f"could not resolve {host}") from None
    if not infos or not all(_is_public_ip(i[4][0]) for i in infos):
        raise PolicyError(f"{host} resolves to a private address")


# ---- reply shaping -------------------------------------------------------------

def _clip(text: str) -> str:
    return text if len(text) <= MAX_REPLY_CHARS else text[:MAX_REPLY_CHARS] + " [truncated]"


def _parts_text(parts) -> list[str]:
    out = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str):
            out.append(part["text"])
        elif "data" in part:
            out.append(json.dumps(part["data"], default=str))
        elif "file" in part or "url" in part or "raw" in part:
            out.append("[file part omitted]")
    return out


def a2a_reply(result: dict) -> dict:
    """Flatten a v1.0 or v0.3 SendMessage result into text + ids + state."""
    if not isinstance(result, dict):
        return {"text": "", "state": "unknown"}
    msg = result.get("message")
    task = result.get("task")
    if msg is None and task is None:  # v0.3: the result IS the message or task
        if result.get("kind") == "task" or "status" in result:
            task = result
        else:
            msg = result
    if task is not None:
        status = task.get("status") or {}
        texts = _parts_text((status.get("message") or {}).get("parts"))
        for art in task.get("artifacts") or []:
            texts += _parts_text(art.get("parts"))
        state = str(status.get("state", "unknown")).lower().removeprefix("task_state_").replace("_", "-")
        return {"text": _clip("\n".join(texts)), "state": state,
                "task_id": task.get("id"), "context_id": task.get("contextId")}
    return {"text": _clip("\n".join(_parts_text(msg.get("parts")))), "state": "message",
            "context_id": msg.get("contextId")}


def pick_a2a_interface(card: dict) -> tuple[str, str] | None:
    """(url, version) for the card's JSON-RPC interface, preferring v1.0."""
    for iface in card.get("supportedInterfaces") or []:
        if str(iface.get("protocolBinding", "")).upper() == "JSONRPC" and iface.get("url"):
            return iface["url"], str(iface.get("protocolVersion") or "1.0")
    if card.get("url") and str(card.get("preferredTransport", "JSONRPC")).upper() == "JSONRPC":
        return card["url"], "0.3"
    return None


def pick_mcp_interface(card: dict) -> str | None:
    for iface in card.get("supportedInterfaces") or []:
        if str(iface.get("protocolBinding", "")).upper() == "MCP" and iface.get("url"):
            return iface["url"]
    return None


def a2a_request(question: str, version: str, context_id: str | None, task_id: str | None) -> tuple[dict, dict]:
    mid = "m_" + uuid.uuid4().hex
    if version.startswith("0."):
        message = {"kind": "message", "messageId": mid, "role": "user",
                   "parts": [{"kind": "text", "text": question}]}
        method, headers = "message/send", {}
    else:
        message = {"messageId": mid, "role": "ROLE_USER", "parts": [{"text": question}]}
        method, headers = "SendMessage", {"A2A-Version": "1.0"}
    if context_id:
        message["contextId"] = context_id
    if task_id:
        message["taskId"] = task_id
    body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex[:12], "method": method,
            "params": {"message": message, "configuration": {"blocking": True}}}
    return body, headers


# ---- the client ----------------------------------------------------------------

class Outbound:
    """Does the network part of ask_agent. Policy and logging live in the caller."""

    def __init__(self, identity: Identity, policy: Policy, timeout_s: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.identity = identity
        self.policy = policy
        self.timeout = httpx.Timeout(timeout_s)
        self.transport = transport  # tests inject one

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            auth=SigningAuth(self.identity), timeout=self.timeout, follow_redirects=False,
            headers={"User-Agent": USER_AGENT}, transport=self.transport,
        )

    async def guard(self, target: Target) -> None:
        if not self.policy.host_allowed(target.host):
            raise PolicyError(f"{target.host} is not on the outbound allowlist")
        if not self.policy.allow_private_targets:
            await check_host_is_public(target.host)

    async def _guard_url(self, url: str) -> None:
        """Endpoints named inside a card must pass the same checks as the card's host."""
        t = parse_target(url, allow_http=self.policy.allow_private_targets)
        await self.guard(t)

    async def fetch_card(self, target: Target) -> tuple[dict, str]:
        async with self._client() as c:
            r = await c.get(target.card_url, headers={"Accept": "application/json"})
            if r.status_code != 200:
                raise RuntimeError(f"agent card fetch returned HTTP {r.status_code}")
            if len(r.content) > 256_000:
                raise RuntimeError("agent card too large")
            card = r.json()
            if not isinstance(card, dict):
                raise RuntimeError("agent card is not a JSON object")
            return card, await self._card_signature(c, target, card)

    async def _card_signature(self, c: httpx.AsyncClient, target: Target, card: dict) -> str:
        sigs = card.get("signatures") or []
        if not sigs:
            return "unsigned"
        try:
            header = json.loads(b64url_decode(sigs[0]["protected"]))
            jku = header.get("jku") or ""
        except Exception:
            return "invalid"
        # Only trust a key set on the card's own origin; anything else proves nothing.
        jp = urlsplit(jku)
        if jp.hostname != target.host or jp.scheme != urlsplit(target.card_url).scheme:
            return "unverifiable"
        try:
            r = await c.get(jku)
            return verify_card(card, r.json()) if r.status_code == 200 else "unverifiable"
        except Exception:
            return "unverifiable"

    async def send_a2a(self, url: str, version: str, question: str,
                       context_id: str | None = None, task_id: str | None = None) -> dict:
        await self._guard_url(url)
        body, headers = a2a_request(question, version, context_id, task_id)
        async with self._client() as c:
            r = await c.post(url, json=body, headers={**headers, "Content-Type": "application/json"})
        if r.status_code != 200:
            raise RuntimeError(f"A2A endpoint returned HTTP {r.status_code}")
        data = r.json()
        if data.get("error"):
            err = data["error"]
            raise RuntimeError(f"A2A error {err.get('code')}: {str(err.get('message'))[:200]}")
        return a2a_reply(data.get("result"))

    async def call_mcp(self, url: str, tool: str | None, arguments: dict | None) -> dict:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        await self._guard_url(url)
        transport = self.transport

        def factory(headers=None, timeout=None, auth=None):
            return httpx.AsyncClient(headers={**(headers or {}), "User-Agent": USER_AGENT},
                                     timeout=timeout or self.timeout, auth=auth,
                                     follow_redirects=False, transport=transport)

        # Decide inside the session, raise outside it: anyio task groups would
        # otherwise wrap a refusal in an ExceptionGroup.
        refused = None
        async with streamablehttp_client(url, auth=SigningAuth(self.identity),
                                         httpx_client_factory=factory) as (read, write, _):
            async with ClientSession(read, write) as s:
                await s.initialize()
                listed = (await s.list_tools()).tools
                readable = {t.name: t for t in listed if self._mcp_tool_ok(t)}
                if not tool:
                    result = {"state": "tools_listed", "text": "",
                              "tools": [{"name": t.name, "description": (t.description or "")[:300],
                                         "input_schema": t.inputSchema} for t in readable.values()]}
                elif tool not in readable:
                    refused = f"{tool} is not a read-only tool ask_agent may call"
                else:
                    res = await s.call_tool(tool, arguments or {})
                    texts = [b.text for b in res.content if getattr(b, "type", "") == "text"]
                    result = {"state": "error" if res.isError else "completed", "text": _clip("\n".join(texts))}
        if refused:
            raise PolicyError(refused)
        return result

    def _mcp_tool_ok(self, t) -> bool:
        if t.name in NEVER_MCP_TOOLS:
            return False
        ann = getattr(t, "annotations", None)
        return t.name in self.policy.mcp_read_tools or bool(ann and ann.readOnlyHint)


def card_digest(card: dict) -> str:
    return hashlib.sha256(json.dumps(card, sort_keys=True).encode()).hexdigest()[:16]


def card_signer_kid(card: dict) -> str | None:
    try:
        return json.loads(b64url_decode(card["signatures"][0]["protected"])).get("kid")
    except Exception:
        return None


__all__ = ["Outbound", "Policy", "PolicyError", "Target", "parse_target", "a2a_reply",
           "pick_a2a_interface", "pick_mcp_interface", "card_digest", "card_signer_kid", "JWKS_PATH"]
