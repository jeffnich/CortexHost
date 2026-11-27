"""Instant ping to the owner when a request lands in the queue.

Two channels, pick one with env vars:
- email (default recommendation): one Resend API call to the owner's own
  inbox, so the ping shows up in the mail app already on their phone.
  Plain-text only, no HTML, so nothing in it can render as a link.
- ntfy: one POST to a topic the owner subscribes to in the ntfy app. The
  topic URL is the secret.

Visitor text is untrusted. It goes only in the message body, as plain text,
with links neutered and length capped, and never in headers or click actions.
A flood cap keeps spam from turning the owner's phone into a pager.
"""

import re
import sys
import threading
import time
import json
import urllib.request

from guard import RateLimiter, clean_text

_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_DOMAINISH = re.compile(r"(?i)\b([a-z0-9-]+)\.(com|net|org|io|ai|co|app|dev|ly|me|xyz|info|biz)\b")


def defang(text: str, max_len: int) -> str:
    """Plain one-line text with no tappable links."""
    s = clean_text(text, 2000, multiline=False)
    s = _URL.sub("[link]", s)
    s = _DOMAINISH.sub(r"\1[.]\2", s)
    return clean_text(s, max_len, multiline=False)


def _post(url: str, body: bytes, headers: dict) -> None:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - owner-configured URL
        resp.read(1024)


def ntfy_transport(url: str, token: str = "", post=_post):
    def send(title: str, body: str, priority: str, ref: str) -> None:
        headers = {"Title": title, "Priority": priority, "Tags": "inbox_tray", "Markdown": "no"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        post(url, body.encode("utf-8"), headers)
    return send


RESEND_URL = "https://api.resend.com/emails"
DEFAULT_FROM = "Cortex Gateway <onboarding@resend.dev>"


def email_transport(api_key: str, to: str, sender: str = DEFAULT_FROM, post=_post):
    """Resend. The resend.dev sender needs no domain but only delivers to the
    Resend account's own address, which is exactly the owner's inbox."""
    def send(title: str, body: str, priority: str, ref: str) -> None:
        subject = f"{title} ({ref})" if ref else title  # ref keeps Gmail from threading pings together
        payload = {"from": sender, "to": [to], "subject": subject, "text": body}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                   "User-Agent": "cortex-gateway"}
        post(RESEND_URL, json.dumps(payload).encode("utf-8"), headers)
    return send


class Notifier:
    def __init__(self, transport, per_hour: int = 10, background: bool = True, clock=time.monotonic):
        self._transport = transport
        self.per_hour = per_hour
        self._background = background
        self._limiter = RateLimiter(clock=clock)
        self._clock = clock
        self._paused_until = 0.0
        self._lock = threading.Lock()

    def request_received(self, *, kind: str, request_id: str, from_name: str, subject: str, client: str) -> None:
        title = "Gateway: new meeting request" if kind == "meeting" else "Gateway: new message"
        body = (
            f"{defang(from_name, 60)}: {defang(subject, 120)}\n"
            f"via {defang(client, 30)} · {request_id}\n"
            "Review with list_requests on your owner connector."
        )
        self._send(title, body, priority="default", ref=request_id)

    def _send(self, title: str, body: str, priority: str, ref: str) -> None:
        if self._limiter.check("push", self.per_hour, 3600):
            # Over the cap: say so once per window, then stay quiet.
            with self._lock:
                now = self._clock()
                if now < self._paused_until:
                    return
                self._paused_until = now + 3600
            title, body, priority, ref = (
                "Gateway: notifications paused",
                f"More than {self.per_hour} requests in the last hour. Pings resume within the hour; "
                "everything is still in the queue.",
                "high",
                "",
            )
        args = (title, body, priority, ref)
        if self._background:
            threading.Thread(target=self._deliver, args=args, daemon=True).start()
        else:
            self._deliver(*args)

    def _deliver(self, *args) -> None:
        try:
            self._transport(*args)
        except Exception as exc:  # a failed ping must never fail the request
            print(f"notify failed: {exc!r}", file=sys.stderr, flush=True)


def from_config(cfg) -> "Notifier | None":
    if cfg.resend_api_key and cfg.notify_email:
        t = email_transport(cfg.resend_api_key, cfg.notify_email, cfg.notify_from or DEFAULT_FROM)
    elif cfg.ntfy_url:
        t = ntfy_transport(cfg.ntfy_url, cfg.ntfy_token)
    else:
        return None
    return Notifier(t, cfg.pings_per_hour)
