"""Self-contained (stateless) signed bearer tokens for the hosted AS.

See docs/SETUP_gcp.md §8.

Why: if the AS kept issued access/refresh tokens in process-memory dicts, every Cloud Run
restart/redeploy would invalidate all connectors. These tokens carry their own claims (type,
subject, client_id, scopes,
expiry) and an HMAC-SHA256 signature, so any instance holding the signing key can verify them
— nothing token-related is stored server-side. §5 note: the subject↔person link stays only in
``oauth_identity_map`` (where the raw ``sub`` lives by design); no *new* table stores a ``sub``.
The ``sub`` claim inside the token is base64url-readable by its holder — that holder is the
user's own MCP client, which already knows the user's Google identity, so nothing new is
disclosed; it just must never be written to server logs.

Pure stdlib on purpose (hmac/hashlib/base64/json): usable offline in tests, no JWT
dependency, no clock skew games — a token is valid iff the signature matches and
``exp`` is in the future.

Format: ``llt1.<b64url(json payload)>.<b64url(hmac_sha256(key, payload_b64))>``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

_PREFIX = "llt1"  # token format prefix, version 1 (bump on payload changes)

ACCESS_TOKEN_TTL = 3600            # seconds
# 30 days: each refresh issues a fresh 30-day token, so an actively-used connector never
# expires; only >30-day idle needs a one-click Google re-login. Kept deliberately shorter
# than convenience would pick because a leaked refresh token cannot be individually revoked
# (stateless — rotating the signing key is the only revocation); this TTL bounds that exposure.
REFRESH_TOKEN_TTL = 30 * 86400
# Signing keys must be real secrets, not placeholders (`openssl rand -base64 48` → 64 chars).
MIN_SIGNING_KEY_LEN = 32


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(payload_b64: str, key: str) -> str:
    mac = hmac.new(key.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256)
    return _b64e(mac.digest())


def mint_token(
    *,
    token_type: str,
    subject: str | None,
    client_id: str,
    scopes: list[str],
    ttl: int,
    key: str,
    issuer: str,
    audience: str | None = None,
    now: float | None = None,
) -> str:
    """Mint a signed token. ``token_type`` is ``"access"`` or ``"refresh"``.

    ``issuer`` binds the token to one deployment (the server's public URL): a staging or
    dev instance that happens to share the signing key still can't mint tokens production
    would accept.

    ``audience`` binds it to one *resource* — the RFC 8707 identifier this AS protects
    (``…/mcp``). Without it, a token a user was tricked into minting "for" some other MCP
    server can be replayed here and works as that user. Optional in the signature only so
    the claim can be enforced gradually; production always passes it. See :func:`verify_token`
    for the two enforcement modes.
    """
    if not key:
        raise ValueError("signing key is required to mint tokens")
    if not issuer:
        raise ValueError("issuer is required to mint tokens")
    iat = int(now if now is not None else time.time())
    payload = {
        "v": 1,
        "typ": token_type,
        "iss": issuer,
        "sub": subject,
        "cid": client_id,
        "scp": list(scopes),
        "iat": iat,
        "exp": iat + ttl,
        "jti": secrets.token_urlsafe(16),
    }
    if audience is not None:
        payload["aud"] = audience
    payload_b64 = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{_PREFIX}.{payload_b64}.{_sign(payload_b64, key)}"


def verify_token(
    token: str,
    *,
    key: str,
    expected_type: str,
    expected_issuer: str,
    expected_audience: str | None = None,
    require_audience: bool = False,
    now: float | None = None,
) -> dict | None:
    """Verify signature + expiry + type + issuer (+ audience); return payload or ``None``.

    Every malformed/tampered/expired/wrong-type/wrong-issuer case returns ``None`` — callers
    translate that into 401/invalid_grant. Never raises on untrusted input (the whole check
    runs under try/except: e.g. a non-ASCII signature part would make ``compare_digest``
    raise ``TypeError``).

    **Audience has two enforcement modes.** Requiring ``aud`` unconditionally would
    invalidate every token minted without it and force every connected person to re-login at
    once. So:

    * **Lenient (default, ``require_audience=False``)** — a token that *carries* ``aud`` must
      match ``expected_audience``; a token *without* the claim still passes. Newly minted
      tokens all carry it, so the wrong-resource replay is refused for every token this server
      mints, while tokens without the claim keep working.
    * **Strict (``require_audience=True``)** — a token with no ``aud`` is refused too.
      Safe to switch on once every token without ``aud`` has expired: access tokens live
      1 hour and refresh tokens 30 days, and each refresh mints an ``aud``-bearing pair,
      so the honest ceiling is the refresh TTL for a connector left idle. Flip it with
      ``MCP_REQUIRE_AUD=1``.

    With ``expected_audience=None`` the claim is not checked at all (stdio/dev).
    """
    if not key or not expected_issuer:
        return None
    try:
        prefix, payload_b64, sig = token.split(".")
        if prefix != _PREFIX:
            return None
        if not hmac.compare_digest(sig, _sign(payload_b64, key)):
            return None
        payload = json.loads(_b64d(payload_b64))
    except (ValueError, AttributeError, TypeError, UnicodeDecodeError, UnicodeEncodeError):
        return None
    if not isinstance(payload, dict) or payload.get("typ") != expected_type:
        return None
    if payload.get("iss") != expected_issuer:
        return None
    if expected_audience is not None:
        aud = payload.get("aud")
        if aud is None:
            # Lenient mode tolerates a token without `aud`; strict mode does not.
            if require_audience:
                return None
        elif aud != expected_audience:
            return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < (now if now is not None else time.time()):
        return None
    return payload
