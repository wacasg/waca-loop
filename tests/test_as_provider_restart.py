"""Restart-safety of the hosted AS (docs/SETUP_gcp.md §8).

If AS state (DCR clients, tokens) lived in process dicts, every Cloud Run restart/redeploy
would invalidate all claude.ai connectors, and "reconnect" would fail because the saved
client_id is gone. Hence self-contained HMAC-signed tokens (``auth/tokens.py``) + a persisted
client registry (``auth/client_store.py``).

A "restart" here is simulated the honest way: build a **fresh provider instance** sharing
only what production shares across restarts — the client store backend and the signing key.
Everything held in the dead instance's memory is gone.

Security posture asserted alongside: issuer binding, minimum
key length, fail-closed verification on hostile input, identity_map re-gate on refresh, and
the hosted per-request viewer refusing subjects removed from the allowlist.
"""

from __future__ import annotations

import pytest

from waca_loop.auth.client_store import InMemoryOAuthClientStore
from waca_loop.auth.tokens import REFRESH_TOKEN_TTL, mint_token, verify_token

KEY = "unit-test-signing-key-0123456789abcdef"  # >= MIN_SIGNING_KEY_LEN
ISS = "https://waca-loop.example.run.app"


def _mint(**overrides) -> str:
    kwargs = dict(
        token_type="access", subject="sub-1", client_id="cid-1",
        scopes=["openid", "email"], ttl=60, key=KEY, issuer=ISS,
    )
    kwargs.update(overrides)
    return mint_token(**kwargs)


# --- auth/tokens.py codec ---------------------------------------------------------------


def test_token_roundtrip_and_claims():
    payload = verify_token(_mint(), key=KEY, expected_type="access", expected_issuer=ISS)
    assert payload is not None
    assert payload["sub"] == "sub-1"
    assert payload["cid"] == "cid-1"
    assert payload["scp"] == ["openid", "email"]
    assert payload["iss"] == ISS


def test_token_verification_fails_closed():
    tok = _mint()
    # Wrong key, wrong type, wrong issuer, tampering, garbage, expiry — all None, no raise.
    assert verify_token(tok, key="x" * 40, expected_type="access", expected_issuer=ISS) is None
    assert verify_token(tok, key=KEY, expected_type="refresh", expected_issuer=ISS) is None
    assert (
        verify_token(
            tok, key=KEY, expected_type="access", expected_issuer="https://staging.example"
        )
        is None
    )
    prefix, payload_b64, sig = tok.split(".")
    tampered = f"{prefix}.{payload_b64[:-2]}AA.{sig}"
    assert verify_token(tampered, key=KEY, expected_type="access", expected_issuer=ISS) is None
    assert verify_token("not-a-token", key=KEY, expected_type="access", expected_issuer=ISS) is None
    assert verify_token("", key=KEY, expected_type="access", expected_issuer=ISS) is None
    expired = _mint(now=1_000_000)
    assert verify_token(expired, key=KEY, expected_type="access", expected_issuer=ISS) is None
    # Hostile non-ASCII signature part must be a rejection, not a TypeError-driven 500
    # (the verifier never raises on untrusted input).
    assert (
        verify_token("llt1.abc.é", key=KEY, expected_type="access", expected_issuer=ISS) is None
    )
    # Empty key / issuer can neither mint nor verify.
    with pytest.raises(ValueError, match="signing key"):
        _mint(key="")
    with pytest.raises(ValueError, match="issuer"):
        _mint(issuer="")
    assert verify_token(tok, key="", expected_type="access", expected_issuer=ISS) is None
    assert verify_token(tok, key=KEY, expected_type="access", expected_issuer="") is None


# --- provider helpers -------------------------------------------------------------------


class _StubSettings:
    mcp_public_url = ISS
    oauth_client_id = "test-client-id.apps.googleusercontent.com"
    oauth_client_secret = "test-secret"  # noqa: S105 — dummy
    mcp_require_aud = False   # aud 束縛の厳格なモードのスイッチ（既定は緩いモード）


def _provider(person_store, client_store, key=KEY):
    from waca_loop.auth.as_provider import GoogleBrokeringAuthProvider

    return GoogleBrokeringAuthProvider(
        _StubSettings(), person_store, client_store=client_store, signing_key=key
    )


def _client(client_id="mcp-client"):
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl

    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="dcr-issued-secret",  # noqa: S106 — dummy registration secret
        redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")],
    )


def _pending(client_id="mcp-client"):
    from waca_loop.auth.as_provider import _Pending

    return _Pending(
        client_id=client_id,
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        redirect_uri_provided_explicitly=True,
        mcp_state="mcp-state-abc",
        code_challenge="challenge",
        scopes=["openid", "email"],
    )


async def _login_and_get_tokens(provider, person_store, client):
    """Full happy path on one provider: gate → (approval) → code → token pair."""
    from waca_loop.auth.oauth import Identity

    registered_sub = next(iter(person_store._identity_map))
    identity = Identity(subject=registered_sub, email="m@example.net")
    assert provider._resolve_or_claim(identity) is True
    code = provider._issue_code(identity, _pending())
    loaded = await provider.load_authorization_code(client, code)
    return registered_sub, await provider.exchange_authorization_code(client, loaded)


# --- Connections survive an instance restart -------------------------------------------


async def test_connection_survives_restart(person_store):
    """Tokens minted and clients registered before a restart keep working after it."""
    shared_client_store = InMemoryOAuthClientStore()
    client = _client()

    old = _provider(person_store, shared_client_store)
    await old.register_client(client)
    sub, tokens = await _login_and_get_tokens(old, person_store, client)

    # Restart: a fresh instance shares only the persisted client store + signing key.
    new = _provider(person_store, shared_client_store)

    # (1) claude.ai's saved client_id still resolves — "reconnect" does not dead-end.
    restored = await new.get_client("mcp-client")
    assert restored is not None
    assert restored.client_secret == "dcr-issued-secret"  # noqa: S105 — /token auth needs it

    # (2) The outstanding access token still verifies, bound to the same subject.
    access = await new.load_access_token(tokens.access_token)
    assert access is not None and access.subject == sub

    # (3) The refresh token still exchanges — the connector silently recovers.
    rt = await new.load_refresh_token(restored, tokens.refresh_token)
    assert rt is not None and rt.expires_at is not None
    refreshed = await new.exchange_refresh_token(restored, rt, ["openid", "email"])
    access2 = await new.load_access_token(refreshed.access_token)
    assert access2 is not None and access2.subject == sub


async def test_key_rotation_window_keeps_old_tokens_valid(person_store):
    """Comma-separated keys: first mints, all verify — rotation without a mass logout."""
    store = InMemoryOAuthClientStore()
    client = _client()
    old = _provider(person_store, store, key=KEY)
    await old.register_client(client)
    sub, tokens = await _login_and_get_tokens(old, person_store, client)

    new_key = "rotated-signing-key-0123456789abcdef"
    rotated = _provider(person_store, store, key=f"{new_key},{KEY}")
    access = await rotated.load_access_token(tokens.access_token)
    assert access is not None and access.subject == sub  # old-key token still accepted
    # And tokens the rotated instance mints use the new (first) key.
    rt = await rotated.load_refresh_token(client, tokens.refresh_token)
    refreshed = await rotated.exchange_refresh_token(client, rt, ["openid"])
    assert (
        verify_token(
            refreshed.access_token, key=new_key, expected_type="access", expected_issuer=ISS
        )
        is not None
    )


async def test_refresh_regates_identity_map(person_store):
    """Refresh re-applies the allowlist gate (SPEC_phase1 §5): a removed sub cannot refresh."""
    from mcp.server.auth.provider import TokenError

    provider = _provider(person_store, InMemoryOAuthClientStore())
    client = _client()
    await provider.register_client(client)
    sub, tokens = await _login_and_get_tokens(provider, person_store, client)

    person_store._identity_map.pop(sub)  # an admin removes the person from the allowlist
    rt = await provider.load_refresh_token(client, tokens.refresh_token)
    assert rt is not None  # the token itself is intact ...
    with pytest.raises(TokenError):  # ... but the exchange is refused, fail closed
        await provider.exchange_refresh_token(client, rt, ["openid", "email"])


async def test_foreign_tokens_rejected(person_store):
    """Tokens from another key OR another deployment (issuer) of the same key are refused."""
    provider = _provider(person_store, InMemoryOAuthClientStore())
    other_key = mint_token(
        token_type="access", subject="sub-x", client_id="mcp-client",
        scopes=["openid"], ttl=3600, key="some-other-environments-key-0123456789", issuer=ISS,
    )
    assert await provider.load_access_token(other_key) is None
    same_key_other_iss = mint_token(
        token_type="access", subject="sub-x", client_id="mcp-client",
        scopes=["openid"], ttl=3600, key=KEY, issuer="https://staging.example.run.app",
    )
    assert await provider.load_access_token(same_key_other_iss) is None
    # And a refresh token is never accepted where an access token is expected.
    disguised = mint_token(
        token_type="refresh", subject="sub-x", client_id="mcp-client",
        scopes=["openid"], ttl=REFRESH_TOKEN_TTL, key=KEY, issuer=ISS,
    )
    assert await provider.load_access_token(disguised) is None


def test_provider_requires_signing_key(person_store):
    """No silent fallback to restart-fragile unsigned tokens; short keys refused."""
    with pytest.raises(ValueError, match="signing key"):
        _provider(person_store, InMemoryOAuthClientStore(), key="")
    with pytest.raises(ValueError, match="32"):
        _provider(person_store, InMemoryOAuthClientStore(), key="short-key")


async def test_register_rate_limit(person_store):
    """Unauthenticated /register can't loop-insert BigQuery rows without bound."""
    from mcp.server.auth.provider import RegistrationError

    from waca_loop.auth.as_provider import _REGISTRATIONS_PER_HOUR

    provider = _provider(person_store, InMemoryOAuthClientStore())
    for i in range(_REGISTRATIONS_PER_HOUR):
        await provider.register_client(_client(f"client-{i}"))
    with pytest.raises(RegistrationError):
        await provider.register_client(_client("one-too-many"))
    assert await provider.get_client("one-too-many") is None  # nothing persisted


def test_serve_http_fails_closed_without_signing_key(monkeypatch, tmp_path):
    """Hosted entrypoint refuses to start when MCP_TOKEN_SIGNING_KEY is missing."""
    from waca_loop.config import get_settings
    from waca_loop.server import serve_http

    for key in ("GCP_PROJECT", "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET"):
        monkeypatch.setenv(key, f"test-{key.lower()}")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://waca-loop.example.run.app")
    monkeypatch.delenv("MCP_TOKEN_SIGNING_KEY", raising=False)
    # Pin config away from any real .env a dev/prod checkout might have (find_env_file
    # searches upward from the package) — the key must be genuinely absent here.
    monkeypatch.setenv("WACA_LOOP_ENV", str(tmp_path / "absent.env"))
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="MCP_TOKEN_SIGNING_KEY"):
            serve_http()
    finally:
        get_settings.cache_clear()


def test_hosted_factory_denies_removed_subject(
    term_store, person_store, embedder, monkeypatch
):
    """A verified token whose sub was removed from the allowlist is denied outright —
    never downgraded to an anonymous viewer (SPEC_phase1 §5: "never a visitor")."""
    import waca_loop.server as server_mod
    from waca_loop.ledger.usage_log import InMemoryUsageLogStore
    from waca_loop.server import SharedRuntime, build_request_context_factory

    runtime = SharedRuntime(
        term_store_factory=lambda _viewer: term_store,
        usage_store=InMemoryUsageLogStore(),
        embedder=embedder,
        persons=person_store,
    )
    factory = build_request_context_factory(runtime)

    registered_sub = next(iter(person_store._identity_map))
    monkeypatch.setattr(server_mod, "_current_subject", lambda: registered_sub)
    assert factory().viewer.person_id is not None  # registered member resolves

    person_store._identity_map.pop(registered_sub)
    with pytest.raises(PermissionError, match="oauth_identity_map"):
        factory()


# --- client store serialization ---------------------------------------------------------


def test_client_store_roundtrip_preserves_registration():
    """The BQ row format (client_json) restores the full registration, secret included."""
    from waca_loop.auth.client_store import client_to_row, row_to_client

    store = InMemoryOAuthClientStore()
    info = _client("roundtrip-client")
    store.put(info)
    restored = store.get("roundtrip-client")
    assert restored == info
    assert store.get("unknown-client") is None
    # The exact row dict the BigQuery store streams is restorable too.
    from datetime import UTC, datetime

    row = client_to_row(info, datetime.now(UTC))
    assert set(row) == {"client_id", "client_json", "created_at"}
    assert row_to_client(row) == info
