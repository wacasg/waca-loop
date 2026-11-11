"""Hosted remote-server refactor: one shared runtime, per-request viewer → per-request ACL.

These assert the hosted-mode decoupling (service-account BigQuery + per-request identity) without
any cloud: a single :class:`SharedRuntime` feeds :func:`context_for`, and two different viewers
built from it get correctly different ACL results (§3.3) and ledger attribution (§5).
"""

from __future__ import annotations

import pytest

from waca_loop.ledger.usage_log import InMemoryUsageLogStore
from waca_loop.server import SharedRuntime, context_for, serve_http


def _runtime(term_store, person_store, embedder) -> SharedRuntime:
    # InMemoryTermStore is viewer-independent (ACL is applied in hybrid_search from the
    # viewer on the context), so the factory returns the shared store regardless of viewer.
    return SharedRuntime(
        term_store_factory=lambda _viewer: term_store,
        usage_store=InMemoryUsageLogStore(),
        embedder=embedder,
        persons=person_store,
    )


def test_context_for_applies_each_viewers_acl(
    term_store, person_store, embedder, viewer_member, viewer_sales
):
    """Same runtime, two viewers → the dept_sales sensitive term is visible only to sales."""
    runtime = _runtime(term_store, person_store, embedder)

    sales = context_for(viewer_sales, runtime).lookup_terms("Snowprism")["hits"]
    eng = context_for(viewer_member, runtime).lookup_terms("Snowprism")["hits"]
    sales_hits = {h["term"] for h in sales}
    eng_hits = {h["term"] for h in eng}

    assert "Snowprism" in sales_hits          # owner_unit dept_sales — sales may see it
    assert "Snowprism" not in eng_hits        # dept_eng viewer is gated out (§3.3)


def test_context_for_attributes_ledger_per_viewer(
    term_store, person_store, embedder, viewer_sales, viewer_outsider
):
    """A resolved viewer logs under actor_id; an unresolved one under a hashed actor_raw (§5)."""
    runtime = _runtime(term_store, person_store, embedder)

    ctx_sales = context_for(viewer_sales, runtime)
    ctx_sales.log_usage(use_category="l2", data_kinds=["none"], summary="s", client="t")

    ctx_out = context_for(viewer_outsider, runtime)
    ctx_out.log_usage(use_category="l2", data_kinds=["none"], summary="s", client="t")

    rows = runtime.usage_store.rows  # InMemory store keeps inserted rows (dicts)
    by_actor = {(r["actor_id"], r["actor_raw"]) for r in rows}
    assert (viewer_sales.person_id, None) in by_actor          # resolved → actor_id
    assert (None, viewer_outsider.subject) in by_actor         # unresolved → hashed actor_raw


def test_serve_http_fails_closed_without_auth(monkeypatch):
    """The hosted entrypoint must refuse to start without ``MCP_PUBLIC_URL`` (§3.3, §5).

    Guards against ever exposing the single service-account BigQuery connection to anonymous
    callers — a "loaded gun" — by failing at startup, not just per request.
    Required config keys are stubbed so the run reaches the auth guard itself, not the
    earlier missing-config check in get_settings(). ``MCP_PUBLIC_URL`` is deliberately left
    unset — with no resource identifier there is no audience to bind tokens to.
    """
    from waca_loop.config import get_settings

    for key in ("GCP_PROJECT", "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET"):
        monkeypatch.setenv(key, f"test-{key.lower()}")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="UNAUTHENTICATED"):
            serve_http()
    finally:
        get_settings.cache_clear()


def test_verify_google_token_requires_audience():
    """An empty audience would skip Google's audience check (cross-client token confusion)."""
    from waca_loop.auth.oauth import verify_google_token

    with pytest.raises(ValueError, match="audience is required"):
        verify_google_token("any.jwt.here", audience="")


# --- Hosted mode: server-as-authorization-server (GoogleBrokeringAuthProvider) ------------------

class _StubSettings:
    mcp_public_url = "https://waca-loop.example.run.app"
    oauth_client_id = "test-client-id.apps.googleusercontent.com"
    oauth_client_secret = "test-secret"  # noqa: S105 — dummy for the token-exchange path
    # aud-binding strict-mode switch. Present here on purpose: the provider reads the attribute
    # directly rather than through `getattr(..., default)`, so a stub that drifts from
    # `Settings` fails loudly instead of silently pinning lenient mode.
    mcp_require_aud = False


def test_the_settings_stub_carries_every_field_the_provider_reads():
    """スタブが本物の Settings から乖離していないこと.

    乖離していても `getattr` の既定値があれば黙って動いてしまう — 「効いているつもり」の形。
    既定値を持たせていないので、ここで気づけるようにしておく。
    """
    from waca_loop.config import Settings

    for name in ("mcp_public_url", "oauth_client_id", "oauth_client_secret", "mcp_require_aud"):
        assert hasattr(_StubSettings, name)
        assert name in Settings.model_fields


def _provider(person_store, client_store=None):
    """AS provider with offline stores + a fixed signing key (auth/tokens.py)."""
    from waca_loop.auth.as_provider import GoogleBrokeringAuthProvider
    from waca_loop.auth.client_store import InMemoryOAuthClientStore

    return GoogleBrokeringAuthProvider(
        _StubSettings(),
        person_store,
        client_store=client_store if client_store is not None else InMemoryOAuthClientStore(),
        signing_key="test-signing-key-0123456789abcdef",
    )


def _pending(provider, client_id="mcp-client"):
    from waca_loop.auth.as_provider import _Pending

    return _Pending(
        client_id=client_id,
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        redirect_uri_provided_explicitly=True,
        mcp_state="mcp-state-abc",
        code_challenge="challenge",
        scopes=["openid", "email"],
    )


async def test_authorize_parks_pending_and_targets_google(person_store):
    """/authorize returns a Google URL carrying our callback + a state that maps to a parked req."""
    from urllib.parse import parse_qs, urlparse

    from mcp.server.auth.provider import AuthorizationParams
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl

    from waca_loop.auth.as_provider import GOOGLE_CALLBACK_PATH

    provider = _provider(person_store)
    client = OAuthClientInformationFull(
        client_id="mcp-client", redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")]
    )
    params = AuthorizationParams(
        state="mcp-state-abc",
        scopes=["openid", "email"],
        code_challenge="challenge",
        redirect_uri=AnyUrl("https://claude.ai/api/mcp/auth_callback"),
        redirect_uri_provided_explicitly=True,
        resource="https://waca-loop.example.run.app",
    )
    url = await provider.authorize(client, params)
    q = parse_qs(urlparse(url).query)
    assert q["client_id"][0] == _StubSettings.oauth_client_id
    assert q["redirect_uri"][0].endswith(GOOGLE_CALLBACK_PATH)
    google_state = q["state"][0]
    assert google_state in provider._pending  # parked, keyed by the state we sent Google


async def test_gate_rejects_unregistered_sub_and_accepts_registered(person_store):
    """The identity-map allowlist gate (SPEC_phase1 §5) is applied at Google-callback time, then
    a full code→token exchange yields an access token whose subject is the Google sub."""
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl

    from waca_loop.auth.oauth import Identity

    provider = _provider(person_store)

    # Unregistered sub -> the gate refuses (rejected outright, not a visitor).
    ghost = Identity(subject="sub_not_in_map", email="ghost@mail.example.com")
    assert provider._resolve_or_claim(ghost) is False

    # Registered sub -> gate passes; the code is minted after the person approves the client
    # (the approval page sits between the two — see handle_google_callback / handle_approval).
    registered_sub = next(iter(person_store._identity_map))
    ident = Identity(subject=registered_sub, email="member@example.net")
    assert provider._resolve_or_claim(ident) is True
    code = provider._issue_code(ident, _pending(provider))
    assert code is not None

    client = OAuthClientInformationFull(
        client_id="mcp-client", redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")]
    )
    loaded = await provider.load_authorization_code(client, code)
    assert loaded is not None and loaded.subject == registered_sub
    token = await provider.exchange_authorization_code(client, loaded)
    access = await provider.load_access_token(token.access_token)
    assert access is not None and access.subject == registered_sub
    # The one-time code is consumed.
    assert await provider.load_authorization_code(client, code) is None


async def test_refresh_token_rotates_and_preserves_subject(person_store):
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl

    from waca_loop.auth.oauth import Identity

    provider = _provider(person_store)
    registered_sub = next(iter(person_store._identity_map))
    code = provider._issue_code(
        Identity(subject=registered_sub, email="m@example.net"), _pending(provider)
    )
    client = OAuthClientInformationFull(
        client_id="mcp-client", redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")]
    )
    loaded = await provider.load_authorization_code(client, code)
    token = await provider.exchange_authorization_code(client, loaded)

    rt = await provider.load_refresh_token(client, token.refresh_token)
    assert rt is not None
    new_token = await provider.exchange_refresh_token(client, rt, ["openid", "email"])
    access = await provider.load_access_token(new_token.access_token)
    assert access is not None and access.subject == registered_sub
    # Old refresh token is rotated out.
    assert await provider.load_refresh_token(client, token.refresh_token) is None


def test_build_auth_settings_enables_dcr(person_store):
    """FastMCP auth settings: this server is issuer+resource, with DCR enabled."""
    from waca_loop.auth.as_provider import build_auth_settings

    auth = build_auth_settings(_StubSettings())
    assert str(auth.issuer_url).rstrip("/") == _StubSettings.mcp_public_url
    # RESOURCE = the MCP endpoint, ISSUER = the origin (RFC 9728 §3.1). The resource identifier
    # puts the protected-resource metadata at /.well-known/oauth-protected-resource/mcp — the
    # first place a spec-following connector looks — and strict clients (Gemini CLI) compare the
    # advertised value against the URL they connected to. The bare origin stays served as an
    # alias by waca_loop.auth.asgi.MetadataCompatMiddleware (test_connector_handshake C5).
    assert str(auth.resource_server_url).rstrip("/") == _StubSettings.mcp_public_url + "/mcp"
    assert auth.client_registration_options.enabled is True
    # Registration does not police the scope a client declares — the gate is the identity_map
    # allowlist, not a label (C1/C2 in tests/test_connector_handshake.py).
    assert auth.client_registration_options.valid_scopes is None
    assert auth.client_registration_options.default_scopes == ["openid", "email"]
