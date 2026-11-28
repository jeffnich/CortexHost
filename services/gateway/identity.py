"""The gateway's public identity: one Ed25519 key, three uses.

1. Signs the A2A Agent Card (JWS, RFC 7515, over a JCS-canonical card).
2. Signs every outbound HTTP request (Web Bot Auth: RFC 9421 HTTP Message
   Signatures with a `Signature-Agent` header naming this gateway's origin).
3. (Later) `private_key_jwt` client auth for MCP servers that want OAuth.

Public keys are published at /.well-known/jwks.json and at the Web Bot Auth
key directory, /.well-known/http-message-signatures-directory. A signature
proves that the holder of a key published at that origin sent the message.
It says nothing about whether the sender is trustworthy.

Generate a key:  python identity.py keygen   (prints GATEWAY_SIGNING_KEY)
"""

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

SIG_LABEL = "sig1"
SIG_TAG = "web-bot-auth"
SIG_TTL_S = 300  # draft recommends <= 24h; short limits replay
DIRECTORY_PATH = "/.well-known/http-message-signatures-directory"
DIRECTORY_MEDIA_TYPE = "application/http-message-signatures-directory+json"
JWKS_PATH = "/.well-known/jwks.json"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical_json(value) -> bytes:
    """JCS (RFC 8785) for the JSON we produce: strings, ints, bools, lists, dicts.
    Floats are rejected rather than risk a canonicalization mismatch."""

    def check(v):
        if isinstance(v, float):
            raise ValueError("floats are not allowed in signed JSON")
        if isinstance(v, dict):
            for x in v.values():
                check(x)
        elif isinstance(v, list):
            for x in v:
                check(x)

    check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def public_jwk(pub: Ed25519PublicKey) -> dict:
    raw = pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return {"kty": "OKP", "crv": "Ed25519", "x": b64url(raw)}


def jwk_thumbprint(jwk: dict) -> str:
    """RFC 7638 thumbprint, used as `kid` / `keyid`."""
    required = {k: jwk[k] for k in ("crv", "kty", "x")}
    return b64url(hashlib.sha256(canonical_json(required)).digest())


def public_key_from_jwk(jwk: dict) -> Ed25519PublicKey:
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise ValueError("only Ed25519 keys are supported")
    return Ed25519PublicKey.from_public_bytes(b64url_decode(jwk["x"]))


class Identity:
    """The gateway's signing key plus the public origin it speaks for."""

    def __init__(self, private_key: Ed25519PrivateKey, public_url: str):
        parts = urlsplit(public_url)
        if parts.scheme not in ("https", "http") or not parts.netloc:
            raise ValueError("GATEWAY_PUBLIC_URL must be an absolute URL like https://example.com")
        self._key = private_key
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.jwk = {**public_jwk(private_key.public_key()), "use": "sig", "alg": "EdDSA"}
        self.kid = jwk_thumbprint(self.jwk)
        self.jwk["kid"] = self.kid

    @classmethod
    def from_seed(cls, seed_b64url: str, public_url: str) -> "Identity":
        seed = b64url_decode(seed_b64url.strip())
        if len(seed) != 32:
            raise ValueError("GATEWAY_SIGNING_KEY must be a base64url 32-byte Ed25519 seed")
        return cls(Ed25519PrivateKey.from_private_bytes(seed), public_url)

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)

    # ---- published key sets --------------------------------------------------

    def jwks(self) -> dict:
        return {"keys": [self.jwk]}

    def directory(self) -> dict:
        """Web Bot Auth key directory: a JWK Set."""
        return {"keys": [self.jwk]}

    # ---- agent card (JWS) ----------------------------------------------------

    def sign_card(self, card: dict) -> dict:
        """Return the card with a `signatures` entry: JWS over JCS(card minus signatures)."""
        unsigned = {k: v for k, v in card.items() if k != "signatures"}
        header = {"alg": "EdDSA", "typ": "JOSE", "kid": self.kid, "jku": self.origin + JWKS_PATH}
        protected = b64url(canonical_json(header))
        payload = b64url(canonical_json(unsigned))
        sig = b64url(self.sign(f"{protected}.{payload}".encode()))
        return {**unsigned, "signatures": [{"protected": protected, "signature": sig}]}

    # ---- RFC 9421 request signing ----------------------------------------------

    def signature_headers(self, method: str, url: str, now: int | None = None) -> dict:
        """Headers that sign one request: Signature-Agent, Signature-Input, Signature."""
        created = int(now if now is not None else time.time())
        agent_header = f'{SIG_LABEL}="{self.origin}"'
        params = (
            f'("@method" "@authority" "@path" "signature-agent";key="{SIG_LABEL}")'
            f';created={created};expires={created + SIG_TTL_S};keyid="{self.kid}"'
            f';alg="ed25519";nonce="{b64url(secrets.token_bytes(16))}";tag="{SIG_TAG}"'
        )
        base = signature_base(method, url, f'"{self.origin}"', params)
        sig = b64url_std(self.sign(base.encode()))
        return {
            "Signature-Agent": agent_header,
            "Signature-Input": f"{SIG_LABEL}={params}",
            "Signature": f"{SIG_LABEL}=:{sig}:",
        }


def b64url_std(data: bytes) -> str:
    """RFC 9421 byte sequences use standard base64 with padding."""
    return base64.b64encode(data).decode()


def _authority(url: str) -> str:
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    default = {"https": 443, "http": 80}.get(p.scheme)
    return host if p.port in (None, default) else f"{host}:{p.port}"


def _path(url: str) -> str:
    return urlsplit(url).path or "/"


def signature_base(method: str, url: str, agent_member: str, params: str) -> str:
    """The RFC 9421 signature base for our fixed component list."""
    return (
        f'"@method": {method.upper()}\n'
        f'"@authority": {_authority(url)}\n'
        f'"@path": {_path(url)}\n'
        f'"signature-agent";key="{SIG_LABEL}": {agent_member}\n'
        f'"@signature-params": {params}'
    )


def _parse_params(sig_input: str) -> tuple[str, dict]:
    """Split `sig1=(...);a=b;...` into (raw params string, {name: value})."""
    label, _, raw = sig_input.partition("=")
    if label.strip() != SIG_LABEL or not raw.startswith("("):
        raise ValueError("unexpected Signature-Input")
    inner, _, rest = raw[1:].partition(")")
    out: dict = {"components": inner.split(" ")}
    for item in filter(None, rest.split(";")):
        k, _, v = item.partition("=")
        out[k] = v.strip('"') if v.startswith('"') else int(v)
    return raw, out


def verify_request(method: str, url: str, headers: dict, jwks: dict, now: int | None = None) -> str:
    """Verify a signed request against a JWK Set. Returns the Signature-Agent origin.
    Raises ValueError with a short reason on any failure."""
    h = {k.lower(): v for k, v in headers.items()}
    try:
        raw, params = _parse_params(h["signature-input"])
        agent_member = h["signature-agent"].partition("=")[2]
        sig_field = h["signature"]
    except KeyError as exc:
        raise ValueError(f"missing header {exc}") from None
    if params.get("tag") != SIG_TAG:
        raise ValueError("wrong tag")
    t = int(now if now is not None else time.time())
    if not params.get("created") or params["created"] > t + 60 or params.get("expires", 0) < t:
        raise ValueError("signature expired or not yet valid")
    keys = {k.get("kid") or jwk_thumbprint(k): k for k in jwks.get("keys", [])}
    jwk = keys.get(params.get("keyid"))
    if jwk is None:
        raise ValueError("unknown keyid")
    prefix = f"{SIG_LABEL}=:"
    if not sig_field.startswith(prefix):
        raise ValueError("malformed Signature")
    sig = base64.b64decode(sig_field[len(prefix):].rstrip(":"))
    base = signature_base(method, url, agent_member, raw)
    try:
        public_key_from_jwk(jwk).verify(sig, base.encode())
    except InvalidSignature:
        raise ValueError("bad signature") from None
    return agent_member.strip('"')


def verify_card(card: dict, jwks: dict) -> str:
    """Check a card's JWS signatures against a JWK Set: 'valid', 'invalid', or 'unsigned'."""
    sigs = card.get("signatures") or []
    if not sigs:
        return "unsigned"
    unsigned = {k: v for k, v in card.items() if k != "signatures"}
    try:
        payload = b64url(canonical_json(unsigned))
    except ValueError:
        return "invalid"
    keys = {k.get("kid") or jwk_thumbprint(k): k for k in jwks.get("keys", [])}
    for s in sigs:
        try:
            header = json.loads(b64url_decode(s["protected"]))
            jwk = keys.get(header.get("kid"))
            if header.get("alg") != "EdDSA" or jwk is None:
                continue
            public_key_from_jwk(jwk).verify(b64url_decode(s["signature"]), f"{s['protected']}.{payload}".encode())
            return "valid"
        except (InvalidSignature, KeyError, ValueError, TypeError):
            continue
    return "invalid"


class SigningAuth(httpx.Auth):
    """httpx auth hook: signs every request it sends, including MCP's follow-ups."""

    def __init__(self, identity: Identity):
        self.identity = identity

    def auth_flow(self, request: httpx.Request):
        request.headers.update(self.identity.signature_headers(request.method, str(request.url)))
        yield request


def keygen() -> str:
    seed = secrets.token_bytes(32)
    return b64url(seed)


def load_or_create_key(path: str) -> str:
    """Read the seed at `path`, or create it (mode 0600) on first boot. Keeping the key on
    the service's volume means it never has to be copied anywhere to be set."""
    import os
    from pathlib import Path

    p = Path(path)
    if p.is_file():
        return p.read_text().strip()
    p.parent.mkdir(parents=True, exist_ok=True)
    seed = keygen()
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(seed + "\n")
    return seed


if __name__ == "__main__":  # pragma: no cover
    import sys

    if sys.argv[1:] == ["keygen"]:
        print(keygen())
    else:
        print("usage: python identity.py keygen", file=sys.stderr)
        sys.exit(2)
