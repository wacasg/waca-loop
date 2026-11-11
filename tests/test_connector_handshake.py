"""C1–C10 — the OAuth+MCP handshake a third-party connector actually performs.

Failure mode guarded here: a third-party web connector fetches the metadata, then stops — no
Google login, no tools ("このアプリで使用可能なアクションはありません"). The MCP server itself is
fine; the **OAuth negotiation** rejects the client before login, e.g. when the AS only tolerates
``openid``/``email`` and refuses (or bounces) anything else. Tests that drive
:class:`ServerContext` directly cannot see this, because the transport, the metadata documents
and the DCR → authorize → token → initialize → tools/list chain are bypassed.

These tests run that chain over ASGI — offline, no BigQuery, no Google, no network — against the
**production-shaped** app (``build_http_app``: FastMCP + the metadata/access-log middlewares).

"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from waca_loop.auth.as_provider import GoogleBrokeringAuthProvider, build_auth_settings
from waca_loop.auth.asgi import ADVERTISED_SCOPES, AS_METADATA_PATH, PRM_PATH, build_http_app
from waca_loop.auth.client_store import InMemoryOAuthClientStore
from waca_loop.auth.oauth import Identity
from waca_loop.config import Settings
from waca_loop.ledger.usage_log import InMemoryUsageLogStore
from waca_loop.server import ServerContext

PUBLIC = "https://loop-test.example.run.app"
SIGNING_KEY = "connector-handshake-key-0123456789abcdef"
# ChatGPT's connector platform registers this fixed redirect; Claude uses its own. Either way the
# server never gets to choose it — it must accept what a legitimate client presents.
REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
GOOGLE_SUB = "sub-sato"        # in tests/conftest.py's identity_map -> p_sato


@pytest.fixture
def hosted_settings() -> Settings:
    return Settings(
        MCP_PUBLIC_URL=PUBLIC,
        MCP_TOKEN_SIGNING_KEY=SIGNING_KEY,
        OAUTH_CLIENT_ID="google-client-id",
        OAUTH_CLIENT_SECRET="google-client-secret",  # dummy, never leaves the test
    )


@pytest.fixture
def provider(hosted_settings, person_store):
    settings = hosted_settings
    return GoogleBrokeringAuthProvider(
        settings,
        person_store,
        client_store=InMemoryOAuthClientStore(),
        signing_key=SIGNING_KEY,
    )


@pytest.fixture
def mcp_server(provider, hosted_settings, term_store, person_store, embedder, viewer_member):
    from waca_loop.server import build_mcp

    context = ServerContext(
        term_store=term_store,
        usage_store=InMemoryUsageLogStore(),
        embedder=embedder,
        viewer=viewer_member,
        person_store=person_store,
    )
    return build_mcp(
        context,
        auth_provider=provider,
        auth_settings=build_auth_settings(hosted_settings),
        google_callback=provider.handle_google_callback,
        # As on Cloud Run: a localhost host bakes in a DNS-rebinding allowlist and every request
        # dies with 421 (build_mcp docstring). Testing with the production binding is the point.
        host="0.0.0.0",  # noqa: S104
        port=8080,
    )


@pytest.fixture
def client(mcp_server):
    with TestClient(
        build_http_app(mcp_server), base_url=PUBLIC, raise_server_exceptions=False
    ) as test_client:
        yield test_client


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _register(client, **overrides) -> dict:
    body = {
        "client_name": "ChatGPT",
        "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",   # public client + PKCE
    }
    body.update(overrides)
    return client.post("/register", json=body)


def _authorize(client, client_id: str, challenge: str, scope: str | None):
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "state": "client-state",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": PUBLIC + "/mcp",           # RFC 8707
    }
    if scope is not None:
        params["scope"] = scope
    return client.get("/authorize", params=params, follow_redirects=False)


# --- C1: registration ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "scope",
    [
        None,                                   # client omits scope -> server default
        "openid email",
        "openid email profile offline_access",  # ★ extra scopes must not 400
        "connector:read",                       # an unknown label is still only a label
    ],
)
def test_c1_dcr_accepts_any_scope_a_client_declares(client, scope):
    """A DCR is a client describing itself. Refusing it over a scope label ends the connection
    before any human ever sees a login screen — and scope buys no access here (§3-①)."""
    extra = {} if scope is None else {"scope": scope}
    response = _register(client, **extra)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["client_id"]
    # A public client must not be handed a secret it never asked for.
    assert body["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in body


# --- C2: authorization redirects to Google, never back with an error -----------------------


@pytest.mark.parametrize(
    "scope",
    [None, "openid email", "openid email profile offline_access"],
)
def test_c2_authorize_reaches_google(client, scope):
    """/authorize must send the browser to Google, not bounce back to the client with
    ``error=invalid_scope`` (the user would see the login screen never appear)."""
    registered = _register(client, scope=(scope or "openid email")).json()
    _, challenge = _pkce()
    response = _authorize(client, registered["client_id"], challenge, scope)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://accounts.google.com/"), location
    assert "error=" not in location
    # What we ask Google for never follows the client's request (§3-①).
    assert parse_qs(urlparse(location).query)["scope"] == ["openid email"]


def test_c2_authorize_scope_does_not_widen_what_google_is_asked_for(client):
    registered = _register(client, scope="openid email profile offline_access").json()
    _, challenge = _pkce()
    location = _authorize(
        client, registered["client_id"], challenge, "openid email profile offline_access"
    ).headers["location"]
    query = parse_qs(urlparse(location).query)
    assert query["scope"] == ["openid email"]
    assert query["client_id"] == ["google-client-id"]


# --- C3: the whole chain, DCR -> ... -> tools/list -----------------------------------------


def _complete_login(client, provider, monkeypatch, *, scope="openid email profile offline_access"):
    """Drive DCR → /authorize → (Google) → callback → /token and return the access token."""
    registered = _register(client, scope=scope).json()
    verifier, challenge = _pkce()
    google_url = _authorize(client, registered["client_id"], challenge, scope).headers["location"]
    google_state = parse_qs(urlparse(google_url).query)["state"][0]

    async def _fake_exchange(code: str) -> Identity:
        assert code == "google-auth-code"
        return Identity(
            subject=GOOGLE_SUB, email="sato@example.com", email_verified=True, hd=None
        )

    monkeypatch.setattr(provider, "_exchange_google_code", _fake_exchange)
    callback = client.get(
        "/auth/google/callback",
        params={"code": "google-auth-code", "state": google_state},
        follow_redirects=False,
    )
    # The allowlist said WHO logged in; the person now says WHICH client it is for.
    assert callback.status_code == 200, callback.text
    approved = client.post(
        "/auth/approve",
        data={"approval_token": _approval_token(callback.text), "decision": "approve"},
        follow_redirects=False,
    )
    assert approved.status_code == 303, approved.text
    back = parse_qs(urlparse(approved.headers["location"]).query)
    assert back["state"] == ["client-state"]

    token = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": back["code"][0],
            "client_id": registered["client_id"],
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
            "resource": PUBLIC + "/mcp",
        },
    )
    assert token.status_code == 200, token.text
    return token.json()["access_token"]


def _approval_token(page_html: str) -> str:
    found = re.search(r"name='approval_token' value='([^']+)'", page_html)
    assert found, f"no approval token in the page: {page_html[:200]}"
    return found.group(1)


def _rpc(client, access_token: str, payload: dict, session_id: str | None = None):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    return client.post("/mcp", json=payload, headers=headers)


def _sse_result(response) -> dict:
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:]).get("result", {})
    raise AssertionError(f"no SSE data frame in: {response.text[:200]}")


def test_c3_full_handshake_returns_every_tool(client, mcp_server, provider, monkeypatch):
    """DCR → authorize → Google → token → initialize → tools/list, as a real connector does.
    ``tools/list`` over the transport must expose exactly what the server registered."""
    access_token = _complete_login(client, provider, monkeypatch)

    initialize = _rpc(
        client,
        access_token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "connector-test", "version": "1"},
            },
        },
    )
    assert initialize.status_code == 200, initialize.text
    session_id = initialize.headers["mcp-session-id"]
    _rpc(
        client,
        access_token,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        session_id,
    )

    listed = _rpc(client, access_token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                  session_id)
    assert listed.status_code == 200
    names = {tool["name"] for tool in _sse_result(listed)["tools"]}
    # Private SDK API on purpose: the assertion worth making is "the transport exposes exactly
    # what the server registered", which no public surface states. Safe under the `mcp<2` pin.
    assert names == {tool.name for tool in mcp_server._tool_manager.list_tools()}
    # The two the client is instructed to call every turn must be reachable, by name.
    assert {"lookup_terms", "log_usage", "get_applicable_policy"} <= names


def test_c3_unregistered_subject_never_gets_a_token(client, provider, monkeypatch):
    """The allowlist — not the scope — is the gate (SPEC_phase1 §5). An unknown ``sub`` is
    refused at the Google callback, so no code and no token exist to reach a tool with."""
    registered = _register(client).json()
    _, challenge = _pkce()
    google_url = _authorize(client, registered["client_id"], challenge, "openid email").headers[
        "location"
    ]
    state = parse_qs(urlparse(google_url).query)["state"][0]

    async def _stranger(code: str) -> Identity:
        return Identity(
            subject="sub-nobody", email="stranger@example.com", email_verified=True, hd=None
        )

    monkeypatch.setattr(provider, "_exchange_google_code", _stranger)
    response = client.get(
        "/auth/google/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert "location" not in response.headers
    assert provider._awaiting_approval == {}   # nothing parked for a stranger to approve


# --- C4: unauthenticated probe -------------------------------------------------------------


def test_c4_unauthenticated_mcp_returns_401_pointing_at_the_metadata(client):
    """How a connector discovers it must log in. A 401 without ``resource_metadata`` leaves the
    client with nowhere to go — the "ツールをスキャン" step never appears."""
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{PUBLIC}{PRM_PATH}/mcp"' in challenge


def test_c4_forged_bearer_is_refused(client):
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream",
                 "Authorization": "Bearer llt1.forged.token"},
    )
    assert response.status_code == 401


# --- C5/C6: metadata documents -------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        AS_METADATA_PATH,
        AS_METADATA_PATH + "/mcp",   # MCP spec's path-insertion form
        PRM_PATH,                    # bare origin (what the SDK stops serving on its own)
        PRM_PATH + "/mcp",           # RFC 9728 §3.1
    ],
)
def test_c5_metadata_is_served_wherever_a_client_looks(client, path):
    response = client.get(path)
    assert response.status_code == 200, path
    assert response.json()


def test_c6_as_metadata_matches_what_the_server_accepts(client):
    metadata = client.get(AS_METADATA_PATH).json()
    assert metadata["registration_endpoint"] == f"{PUBLIC}/register"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    # Public PKCE clients are accepted by /token, so they must be advertised (§3-④).
    assert "none" in metadata["token_endpoint_auth_methods_supported"]
    # Registration tolerates any label, but we only claim what the AS honestly uses.
    assert metadata["scopes_supported"] == ADVERTISED_SCOPES


def test_c6_alias_and_canonical_as_metadata_agree(client):
    assert client.get(AS_METADATA_PATH).json() == client.get(AS_METADATA_PATH + "/mcp").json()


def test_c6_protected_resource_metadata_names_the_mcp_endpoint(client):
    metadata = client.get(PRM_PATH + "/mcp").json()
    assert metadata["resource"].rstrip("/") == f"{PUBLIC}/mcp"
    assert metadata["authorization_servers"] == [f"{PUBLIC}/"]
    assert client.get(PRM_PATH).json() == metadata


# --- C7: the access log says how far a connector got, and nothing more ---------------------


def test_c7_access_log_records_the_rpc_method_but_never_the_payload(
    client, provider, monkeypatch, caplog
):
    """§3-⑤. ``initialize`` and ``tools/list`` are both ``POST /mcp`` in Cloud Run's request log;
    the method name is what makes "did it reach tools/list?" answerable. Arguments are not:
    Cloud Logging is durable and widely read, so the ledger's summary-only rule applies here."""
    access_token = _complete_login(client, provider, monkeypatch)
    with caplog.at_level("INFO", logger="waca_loop.auth.asgi"):
        initialize = _rpc(
            client,
            access_token,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "connector-test", "version": "1"},
                },
            },
        )
        session_id = initialize.headers["mcp-session-id"]
        _rpc(client, access_token,
             {"jsonrpc": "2.0", "method": "notifications/initialized"}, session_id)
        _rpc(
            client,
            access_token,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "lookup_terms",
                    "arguments": {"query": "会員の田中さんの連絡先", "k": 3},
                },
            },
            session_id,
        )
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "rpc=initialize" in logged
    assert "rpc=tools/call" in logged
    assert "status=200" in logged
    # Nothing a user typed, and no credential, may reach a durable log.
    assert "田中" not in logged
    assert "arguments" not in logged
    assert access_token not in logged


def test_c7_access_log_records_the_scope_a_client_asked_for(client, caplog):
    """The single fact that decides the §3-① diagnosis, and the reason it took a code read to
    answer "what did ChatGPT send?" the first time."""
    with caplog.at_level("INFO", logger="waca_loop.auth.asgi"):
        registered = _register(client, scope="openid email profile offline_access").json()
        _, challenge = _pkce()
        _authorize(client, registered["client_id"], challenge, "openid email profile")
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "POST /register" in logged
    assert "offline_access" in logged          # from the DCR body
    assert "GET /authorize" in logged
    assert "'openid email profile'" in logged  # from the query string


def test_c7_a_tool_argument_named_scope_is_not_logged(client, provider, monkeypatch, caplog):
    """The DCR ``scope`` sniffer must key on the path: ``set_session_scope`` takes an argument of
    that name, and a tool argument is user-supplied text, not client metadata."""
    access_token = _complete_login(client, provider, monkeypatch)
    initialize = _rpc(
        client,
        access_token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "connector-test", "version": "1"}},
        },
    )
    session_id = initialize.headers["mcp-session-id"]
    _rpc(client, access_token, {"jsonrpc": "2.0", "method": "notifications/initialized"},
         session_id)
    with caplog.at_level("INFO", logger="waca_loop.auth.asgi"):
        _rpc(
            client,
            access_token,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "set_session_scope",
                           "arguments": {"scope": "org", "reason_summary": "田中家の相続の件"}},
            },
            session_id,
        )
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "rpc=tools/call" in logged
    assert "scope_requested" not in logged
    assert "田中" not in logged


# --- C8: a self-declared scope never becomes a granted one --------------------------------


def test_c8_token_scopes_are_clamped_to_what_the_as_actually_grants(
    client, provider, monkeypatch, hosted_settings
):
    """DCR is unauthenticated and accepts any label (C1). Minting must not echo it: a client
    that registers ``steward`` must not end up holding a token that *says* ``steward``, or the
    first scope check anyone adds later would be self-service."""
    from waca_loop.auth.as_provider import GRANTED_SCOPES
    from waca_loop.auth.tokens import verify_token

    access_token = _complete_login(
        client, provider, monkeypatch, scope="openid email steward admin offline_access"
    )
    payload = verify_token(
        access_token,
        key=SIGNING_KEY,
        expected_type="access",
        expected_issuer=hosted_settings.mcp_public_url,
    )
    assert payload is not None
    assert set(payload["scp"]) <= set(GRANTED_SCOPES)
    assert "steward" not in payload["scp"]


def test_c8_no_tool_call_is_scope_gated(hosted_settings):
    """The clamp is defence in depth, not the gate. The gate is the identity_map allowlist
    (C3) and the 3-axis ACL — so nothing here may start depending on scopes."""
    assert build_auth_settings(hosted_settings).required_scopes == []


def test_c8_metadata_requests_are_logged_too(client, caplog):
    """A 404 at the first place a client looks for the metadata is the failure the access log
    exists to surface — so the metadata paths have to be watched."""
    with caplog.at_level("INFO", logger="waca_loop.auth.asgi"):
        client.get(PRM_PATH + "/mcp")
        client.get(AS_METADATA_PATH)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert f"GET {PRM_PATH}/mcp status=200" in logged
    assert f"GET {AS_METADATA_PATH} status=200" in logged


# --- C9: what an anonymous caller can spend --------------


async def test_c9_a_forged_path_cannot_write_its_own_log_line(caplog):
    """uvicorn percent-decodes the request target into ``scope["path"]``, so a path carrying
    ``%0A`` could otherwise write a second entry indistinguishable from a real one. The log
    states the *watched label*, never the caller's text.

    Driven at the ASGI layer on purpose: an HTTP client library refuses to send this, which is
    exactly why it has to be tested where the bytes actually arrive.
    """
    from waca_loop.auth.asgi import ConnectorAccessLogMiddleware

    forged = (
        "/.well-known/oauth-\n2026-08-23 05:00:00 INFO waca-loop-access: "
        "POST /mcp status=200 rpc=tools/list"
    )

    async def _app(scope, receive, send):
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent = []

    async def _send(message):
        sent.append(message)

    middleware = ConnectorAccessLogMiddleware(_app)
    with caplog.at_level("INFO", logger="waca_loop.auth.asgi"):
        await middleware(
            {"type": "http", "method": "GET\nPOST", "path": forged, "query_string": b""},
            _receive,
            _send,
        )
        # And the watched paths themselves log the label, with the verb reduced to letters.
        await middleware(
            {"type": "http", "method": "GET\nPOST", "path": AS_METADATA_PATH,
             "query_string": b""},
            _receive,
            _send,
        )
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "rpc=tools/list" not in logged
    assert "2026-08-23 05:00:00" not in logged     # the forged text never reaches the sink
    assert logged.count("waca-loop-access") == 1
    assert f"GETPOST {AS_METADATA_PATH} status=404" in logged
    assert sent, "the requests must still be served"


def test_c9_error_lines_are_capped_per_minute(client, caplog, monkeypatch):
    """Every watched path answers unauthenticated callers, so a flood must not be able to turn a
    durable, billed sink into its own amplifier."""
    from types import SimpleNamespace

    from waca_loop.auth import asgi

    # Pin the limiter's clock inside one minute window, so a run that happens to cross a minute
    # boundary does not reset the count (the cap is per wall-clock minute).
    monkeypatch.setattr(asgi, "time", SimpleNamespace(monotonic=lambda: 600.0))
    with caplog.at_level("INFO", logger="waca_loop.auth.asgi"):
        for _ in range(asgi._ERROR_LINES_PER_MINUTE + 25):
            client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Accept": "application/json, text/event-stream"},
            )
    written = [r for r in caplog.records if "status=401" in r.getMessage()]
    assert len(written) == asgi._ERROR_LINES_PER_MINUTE


def test_c9_oversized_registration_is_refused(client):
    """`/register` is unauthenticated and its document is persisted verbatim; dropping the scope
    check removed the only thing that bounded its size."""
    response = _register(client, scope="x" * 20000)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_client_metadata"
    # A legitimate connector document is unaffected.
    assert _register(client, scope="openid email profile offline_access").status_code == 201


# --- C10: the approval page ---------------------------


def _start_login(client, provider, monkeypatch, *, client_name="ChatGPT", redirect=REDIRECT):
    """DCR → /authorize → Google → callback, stopping at the approval page."""
    registered = _register(client, client_name=client_name, redirect_uris=[redirect]).json()
    _, challenge = _pkce()
    params = {
        "response_type": "code",
        "client_id": registered["client_id"],
        "redirect_uri": redirect,
        "state": "client-state",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    google_url = client.get("/authorize", params=params, follow_redirects=False).headers["location"]

    async def _fake_exchange(code: str) -> Identity:
        return Identity(subject=GOOGLE_SUB, email="sato@example.com", email_verified=True, hd=None)

    monkeypatch.setattr(provider, "_exchange_google_code", _fake_exchange)
    return client.get(
        "/auth/google/callback",
        params={"code": "google-auth-code",
                "state": parse_qs(urlparse(google_url).query)["state"][0]},
        follow_redirects=False,
    )


def test_c10_the_person_is_told_which_client_they_are_approving(client, provider, monkeypatch):
    """DCR is unauthenticated: anyone may register a client pointing at their own host and send a
    staff member an /authorize link. The allowlist proves who logged in, not what they meant to
    connect — so the redirect target and the client's name are put in front of them first."""
    page = _start_login(
        client, provider, monkeypatch,
        client_name="サンプル協会公式コネクタ", redirect="https://evil.example/cb",
    )
    assert page.status_code == 200
    assert "この接続を許可しますか" in page.text
    assert "サンプル協会公式コネクタ" in page.text
    assert "evil.example" in page.text          # the host that would receive the code
    assert "sato@example.com" in page.text      # and which account is being handed over


def test_c10_declining_hands_over_nothing(client, provider, monkeypatch):
    page = _start_login(client, provider, monkeypatch)
    response = client.post(
        "/auth/approve",
        data={"approval_token": _approval_token(page.text), "decision": "deny"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "location" not in response.headers   # the browser never goes back to the client
    assert provider._codes == {}                # and no code exists to exchange


def test_c10_an_approval_token_works_once(client, provider, monkeypatch):
    page = _start_login(client, provider, monkeypatch)
    token = _approval_token(page.text)
    first = client.post(
        "/auth/approve", data={"approval_token": token, "decision": "approve"},
        follow_redirects=False,
    )
    assert first.status_code == 303
    second = client.post(
        "/auth/approve", data={"approval_token": token, "decision": "approve"},
        follow_redirects=False,
    )
    assert second.status_code == 400
    assert "location" not in second.headers


def test_c10_a_forged_or_expired_token_gets_nothing(client):
    response = client.post(
        "/auth/approve", data={"approval_token": "made-up", "decision": "approve"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "location" not in response.headers


def test_c10_a_hostile_client_name_cannot_run_script_on_our_origin(client, provider, monkeypatch):
    """`client_name` is free text from an unauthenticated DCR document, rendered on our own
    origin — the one place where "it is only a label" stops being true."""
    page = _start_login(
        client, provider, monkeypatch,
        client_name="<script>alert(document.domain)</script>",
    )
    assert page.status_code == 200
    assert "<script>alert" not in page.text
    assert "&lt;script&gt;" in page.text


def test_c10_the_approval_step_is_visible_in_the_access_log(client, provider, monkeypatch, caplog):
    """The approval page is part of the connect flow, so "how far did it get" must show it —
    otherwise a connector that stalls at the page looks identical to one that stalled at Google."""
    page = _start_login(client, provider, monkeypatch)
    with caplog.at_level("INFO", logger="waca_loop.auth.asgi"):
        client.post(
            "/auth/approve",
            data={"approval_token": _approval_token(page.text), "decision": "approve"},
            follow_redirects=False,
        )
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "POST /auth/approve status=303" in logged
