"""Instant push to the owner's phone when a request lands in the queue.

Channel: ntfy (https://ntfy.sh, or a self-hosted server). One HTTP POST, a
free phone app, no account needed. The topic URL is the secret: anyone who
knows it can read the notifications, so it lives in an env var and uses an
unguessable topic name. An optional access token covers protected topics.

Visitor text is untrusted. It goes only in the message body, as plain text,
with links neutered and length capped, and never in headers or click actions.
A flood cap keeps spam from turning the owner's phone into a pager.
"""

import re
import sys
import threading
import time
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


class Notifier:
    def __init__(
        self,
        url: str,
        token: str = "",
        per_hour: int = 10,
        post=_post,
        background: bool = True,
        clock=time.monotonic,
    ):
        self.url = url
        self.token = token
        self.per_hour = per_hour
        self._post = post
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
        self._send(title, body, priority="default")

    def _send(self, title: str, body: str, priority: str) -> None:
        if self._limiter.check("push", self.per_hour, 3600):
            # Over the cap: say so once per window, then stay quiet.
            with self._lock:
                now = self._clock()
                if now < self._paused_until:
                    return
                self._paused_until = now + 3600
            title, body, priority = (
                "Gateway: notifications paused",
                f"More than {self.per_hour} requests in the last hour. Pings resume within the hour; "
                "everything is still in the queue.",
                "high",
            )
        headers = {"Title": title, "Priority": priority, "Tags": "inbox_tray", "Markdown": "no"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        payload = body.encode("utf-8")
        if self._background:
            threading.Thread(target=self._deliver, args=(payload, headers), daemon=True).start()
        else:
            self._deliver(payload, headers)

    def _deliver(self, payload: bytes, headers: dict) -> None:
        try:
            self._post(self.url, payload, headers)
        except Exception as exc:  # a failed ping must never fail the request
            print(f"notify failed: {exc!r}", file=sys.stderr, flush=True)
