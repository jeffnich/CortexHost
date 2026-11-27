"""Input hygiene, rate limiting, and caller identity for public traffic.

Everything a visitor sends is untrusted. These helpers keep it small, plain,
and attributable before it touches the queue or the audit log.
"""

import hashlib
import ipaddress
import re
import threading
import time
import unicodedata
from collections import defaultdict, deque

# Zero-width and bidi-control characters are a favourite way to hide text
# from a human reviewer while an LLM still reads it.
_INVISIBLE = re.compile(
    "[​-‏‪-‮⁠-⁤⁦-⁩﻿]"
)
_LABEL_RE = re.compile(r"[^a-z0-9._-]+")


def clean_text(value: object, max_len: int, *, multiline: bool = True) -> str:
    """Normalize untrusted text: NFKC, strip invisibles and control chars, cap length."""
    s = unicodedata.normalize("NFKC", str(value or ""))
    s = _INVISIBLE.sub("", s)
    keep = "\n\t" if multiline else ""
    s = "".join(ch for ch in s if ch in keep or unicodedata.category(ch)[0] != "C")
    if not multiline:
        s = " ".join(s.split())
    s = s.strip()
    if len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return s


def clean_label(value: str | None) -> str | None:
    label = _LABEL_RE.sub("-", (value or "").strip().lower()).strip("-")[:40]
    return label or None


def hash_ip(ip: str | None, salt: str) -> str | None:
    """Audit by stable pseudonym, never by raw IP."""
    if not ip:
        return None
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:16]


def network_of(ip: str | None) -> str | None:
    """The caller's network: /24 for IPv4, /48 for IPv6. Unparseable -> as given."""
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    prefix = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))


class RateLimiter:
    """Sliding-window counters keyed by arbitrary strings. In-memory, per process.

    Good enough for one small container; a restart forgives everyone, which is
    acceptable for a public front door with a global daily cap behind it.
    """

    def __init__(self, clock=time.monotonic):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()
        self._clock = clock

    def check(self, key: str, limit: int, window_s: float) -> float:
        """Record a hit if under the limit. Returns 0 if allowed, else seconds to wait."""
        now = self._clock()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window_s:
                q.popleft()
            if len(q) >= limit:
                return max(1.0, q[0] + window_s - now)
            q.append(now)
            return 0.0
