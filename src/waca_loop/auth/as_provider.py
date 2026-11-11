"""Server-as-Authorization-Server that brokers Google login (hosted mode — docs/SETUP_gcp.md §8).

Claude.ai's MCP connector requires an OAuth authorization server that supports **Dynamic Client
Registration** (RFC 7591). Google does not. Pointing the Protected Resource Metadata straight at
``accounts.google.com`` therefore dead-ends at Claude's "auto client registration not supported".

So *this server is the authorization server* the MCP client (Claude) talks to. It:

1. Accepts DCR (``/register``) so Claude auto-registers — no client secret is ever distributed to
   end users; connecting is "add the URL + approve in the browser once".
2. On ``/authorize`` it redirects the browser to Google (our single Web OAuth client), i.e. Google
   is the hidden 3rd-party identity provider (see :meth:`GoogleBrokeringAuthProvider.authorize`).
3. On the Google callback it verifies the returned ID token, applies the ``oauth_identity_map``
   **allowlist gate** (SPEC_phase1 §5: an unregistered ``sub`` is rejected outright, never a
   visitor), then
   mints *its own* authorization code and, later, its own bearer tokens.
4. Every MCP request is authenticated by :meth:`load_access_token` (wired as the FastMCP
   ``ProviderTokenVerifier``); the token's ``subject`` is the Google ``sub`` already resolvable to
   a ``person_id`` (identity → viewer → ACL, §3.3).

**Restart-safe by design.** If registered clients, codes and tokens lived in process dicts,
every Cloud Run restart/redeploy would invalidate all connectors — and "reconnect" would also
fail because the saved ``client_id`` would be gone. Therefore:

- **Access/refresh tokens are self-contained HMAC-signed tokens** (:mod:`waca_loop.auth.tokens`)
  — nothing token-related is stored server-side; any instance holding ``MCP_TOKEN_SIGNING_KEY``
  verifies them (the subject↔person link stays only in ``oauth_identity_map``, §5). Refresh
  re-applies the ``oauth_identity_map`` gate, and every MCP request re-resolves the subject
  (``server.py`` hosted factory), so removing someone from the allowlist cuts their access on
  their next request.
- **Registered DCR clients are persisted** via a client store pair
  (:mod:`waca_loop.auth.client_store`; BigQuery ``oauth_clients`` in production).
- Only *in-flight login state* (``_pending`` /authorize parkings and one-time authorization
  codes) stays in-memory: it spans a single browser redirect measured in seconds, and one-time
  codes MUST be single-use (RFC 6749 §4.1.2) which requires shared state. This is why the
  deployment runs a single Cloud Run instance — and a restart only aborts logins in
  flight at that instant, never established connections.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from waca_loop.auth.oauth import Identity, verify_google_token
from waca_loop.auth.tokens import (
    ACCESS_TOKEN_TTL,
    MIN_SIGNING_KEY_LEN,
    REFRESH_TOKEN_TTL,
    mint_token,
    verify_token,
)

if TYPE_CHECKING:
    from mcp.server.auth.provider import AuthorizationParams
    from starlette.requests import Request
    from starlette.responses import Response

logger = logging.getLogger(__name__)

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 — public endpoint URL
GOOGLE_CALLBACK_PATH = "/auth/google/callback"
# Path FastMCP serves Streamable HTTP on (its default). The RFC 9728 resource identifier is
# this URL, not the bare origin — see build_auth_settings.
MCP_PATH = "/mcp"

#: The only scopes this AS actually uses — what it asks Google for, and therefore the only labels
#: it will WRITE INTO A TOKEN. Registration accepts any label a client declares (see
#: :func:`build_auth_settings`); minting clamps to this set so a self-registered client can never
#: hand itself a token asserting something a future scope check might believe.
GRANTED_SCOPES = ("openid", "email")

_CODE_TTL = 600            # our authorization code lifetime
# /register is unauthenticated by spec (RFC 7591) and writes a BigQuery row per call —
# cap it so an anonymous loop can't amplify cost/row growth. Way above legitimate use:
# claude.ai registers roughly once per connector setup.
_REGISTRATIONS_PER_HOUR = 30
# Largest client metadata document we will persist. /register is unauthenticated and —
# because registration accepts any scope label (build_auth_settings) — nothing else bounds the
# length of `scope`, `client_name` or the URI fields, all of which land verbatim in the BigQuery
# `oauth_clients` row.
# A real DCR document is a few hundred bytes; this is far above legitimate use and turns
# "anonymous, unbounded write" back into "anonymous, bounded write".
_MAX_CLIENT_METADATA_BYTES = 8192
#: How long the "この接続を許可しますか" page stays valid. It is shown immediately after the
#: Google login and answered in seconds; a short life keeps the parked login from outliving the
#: browser tab it belongs to.
_APPROVAL_TTL = 300
GOOGLE_APPROVE_PATH = "/auth/approve"


def resource_identifier(mcp_public_url: str) -> str:
    """The one RFC 8707 / RFC 9728 resource identifier — the MCP endpoint, not the origin.

    Used by the token minter (``aud``) and by the advertised metadata (``resource``). One
    function so the two cannot drift; normalised through ``AnyHttpUrl`` because that is what
    the metadata publishes, and that normalisation is not identity — it lowercases the host
    and drops a default ``:443``.
    """
    from pydantic import AnyHttpUrl

    return str(AnyHttpUrl(mcp_public_url.rstrip("/") + MCP_PATH))


@dataclass
class _Pending:
    """An /authorize request parked while the user completes the Google login."""

    client_id: str
    redirect_uri: str                       # the MCP client's redirect (e.g. claude.ai callback)
    redirect_uri_provided_explicitly: bool
    mcp_state: str | None                   # the MCP client's own `state`, echoed back verbatim
    code_challenge: str                     # PKCE challenge (verified by the SDK TokenHandler)
    scopes: list[str] = field(default_factory=list)
    resource: str | None = None             # RFC 8707 resource indicator


class GoogleBrokeringAuthProvider:
    """OAuth 2.1 authorization server that brokers Google as the identity provider.

    Implements the mcp ``OAuthAuthorizationServerProvider`` protocol. See the module docstring
    for the end-to-end flow.
    """

    def __init__(
        self, settings: Any, persons: Any, *, client_store: Any, signing_key: str
    ) -> None:
        """``client_store`` persists DCR registrations (InMemory in tests, BigQuery hosted);
        ``signing_key`` signs the self-contained tokens. Both are mandatory — an AS without
        them would silently regress to restart-fragile in-memory state.

        ``signing_key`` may hold several comma-separated keys: the FIRST mints, ALL verify.
        That makes key rotation graceful — deploy ``newkey,oldkey``, let the old tokens age
        out (≤ REFRESH_TOKEN_TTL), then drop the old key — instead of a mass logout.
        """
        keys = [k.strip() for k in (signing_key or "").split(",") if k.strip()]
        if not keys:
            raise ValueError(
                "GoogleBrokeringAuthProvider requires a token signing key "
                "(MCP_TOKEN_SIGNING_KEY) — refusing to mint unverifiable tokens"
            )
        short = [k for k in keys if len(k) < MIN_SIGNING_KEY_LEN]
        if short:
            raise ValueError(
                f"MCP_TOKEN_SIGNING_KEY entries must be >= {MIN_SIGNING_KEY_LEN} chars "
                "(generate with `openssl rand -base64 48`) — a short key makes every "
                "minted token forgeable"
            )
        self._settings = settings
        self._persons = persons
        self._clients = client_store
        self._signing_key = keys[0]
        self._verify_keys = keys
        self._register_times: list[float] = []   # sliding-window /register throttle
        # In-flight login state only (seconds-lived; see module docstring). Established
        # connections never depend on these surviving a restart.
        self._pending: dict[str, _Pending] = {}      # google_state -> pending /authorize
        # approval_token -> (identity, pending, expiry): a login that passed the allowlist and is
        # waiting for the person to confirm WHICH client it is for (see handle_google_callback).
        self._awaiting_approval: dict[str, tuple[Identity, _Pending, float]] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        # Best-effort in-process revocation/rotation marks for the stateless tokens:
        # jti -> token exp (pruned once expired, so the warm min-instances=1 process
        # doesn't grow this forever). Trade-off: these marks are lost on restart, so a
        # rotated-away refresh token becomes usable again until its own exp; the only hard
        # revocation lever is rotating MCP_TOKEN_SIGNING_KEY.
        # The identity_map re-gate in exchange_refresh_token covers removed *people*.
        self._revoked_jtis: dict[str, int] = {}

    def _mark_revoked(self, jti: str, exp: int) -> None:
        now = time.time()
        self._revoked_jtis = {j: e for j, e in self._revoked_jtis.items() if e >= now}
        self._revoked_jtis[jti] = exp

    @property
    def _audience(self) -> str:
        """RFC 8707 resource identifier this AS mints for — the MCP endpoint, not the origin.

        Delegates to :func:`resource_identifier`, which is also what the PRM advertises, so
        the two are the same string **including normalisation**. That last word is the whole
        point: ``AnyHttpUrl`` lowercases the host and drops a default ``:443``, so deriving
        the string twice would make an ``MCP_PUBLIC_URL`` written as ``https://Loop.Example:443``
        mint ``aud=https://Loop.Example:443/mcp`` while advertising
        ``resource=https://loop.example/mcp`` — silently voiding the binding for any client
        that compares them.
        """
        return resource_identifier(self._settings.mcp_public_url)

    def _verify(self, token: str, expected_type: str) -> dict | None:
        """Verify against every configured key (rotation window) — first match wins."""
        for key in self._verify_keys:
            payload = verify_token(
                token,
                key=key,
                expected_type=expected_type,
                expected_issuer=self._settings.mcp_public_url,
                expected_audience=self._audience,
                # Read the attribute directly: a getattr default would turn a future
                # rename into a permanent, invisible "aud optional" mode — the "believed
                # enforced, isn't" shape.
                require_audience=bool(self._settings.mcp_require_aud),
            )
            if payload is not None:
                return payload
        return None

    @property
    def _google_redirect_uri(self) -> str:
        return self._settings.mcp_public_url.rstrip("/") + GOOGLE_CALLBACK_PATH

    # --- Dynamic Client Registration -------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if client_info.client_id is None:
            return
        now = time.time()
        self._register_times = [t for t in self._register_times if t > now - 3600]
        if len(self._register_times) >= _REGISTRATIONS_PER_HOUR:
            logger.warning("waca-loop-serve: /register rate limit hit — refusing DCR")
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="registration rate limit exceeded, try again later",
            )
        self._register_times.append(now)
        try:
            size = len(client_info.model_dump_json().encode())
        except Exception:  # noqa: BLE001 — an unserializable registration is not ours to store
            size = _MAX_CLIENT_METADATA_BYTES + 1
        if size > _MAX_CLIENT_METADATA_BYTES:
            logger.warning(
                "waca-loop-serve: refusing an oversized DCR document (%d bytes)", size
            )
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="client metadata is too large",
            )
        self._clients.put(client_info)

    # --- Authorization: hand off to Google -------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the request and return the Google authorization URL to redirect the user to."""
        google_state = secrets.token_urlsafe(32)
        self._pending[google_state] = _Pending(
            client_id=client.client_id or "",
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            mcp_state=params.state,
            code_challenge=params.code_challenge,
            scopes=list(params.scopes or []),
            resource=params.resource,
        )
        query = urlencode(
            {
                "client_id": self._settings.oauth_client_id,
                "redirect_uri": self._google_redirect_uri,
                "response_type": "code",
                "scope": "openid email",
                "state": google_state,
                "access_type": "online",
                "prompt": "select_account",
            }
        )
        return f"{GOOGLE_AUTH_ENDPOINT}?{query}"

    # --- Google callback -------------------------------------------------------------------

    async def handle_google_callback(self, request: Request) -> Response:
        """Custom route (unauthenticated) hit by Google after the user approves.

        Exchanges the Google code for an ID token, verifies it, applies the identity-map gate,
        then shows the approval page (:func:`_approval_page`). The redirect back to the MCP
        client's ``redirect_uri`` with *our* code happens in :meth:`handle_approval`, only if the
        person approves.
        """
        from starlette.responses import HTMLResponse

        code = request.query_params.get("code")
        state = request.query_params.get("state")
        error = request.query_params.get("error")
        if error:
            return HTMLResponse(_error_page(f"Google 認証エラー: {error}"), status_code=400)
        if not code or not state:
            return HTMLResponse(_error_page("不正なコールバック(code/state 欠落)"), status_code=400)

        pending = self._pending.pop(state, None)
        if pending is None:
            return HTMLResponse(
                _error_page("セッションが期限切れ、または不明です。もう一度接続し直してください。"),
                status_code=400,
            )

        try:
            identity = await self._exchange_google_code(code)
        except Exception as exc:  # noqa: BLE001 — surface any exchange/verify failure as 502
            logger.warning("waca-loop-serve: Google code exchange failed: %s", exc)
            return HTMLResponse(_error_page("Google トークン交換に失敗しました。"), status_code=502)

        if self._resolve_or_claim(identity) is False:
            return HTMLResponse(
                _error_page(
                    "このアカウントはまだ許可リスト(identity_map)に登録されていません。"
                    "管理者に連絡してください。"
                ),
                status_code=403,
            )

        # The allowlist proves WHO logged in. It cannot prove WHICH client they meant to connect,
        # and /register is unauthenticated by spec: anyone may register a client whose redirect_uri
        # is their own, then send a staff member an /authorize link. Without this page that person
        # would log in, see nothing unusual, and hand a working token to a stranger.
        token = secrets.token_urlsafe(32)
        self._prune_approvals()
        self._awaiting_approval[token] = (identity, pending, time.time() + _APPROVAL_TTL)
        client = await self.get_client(pending.client_id)
        return HTMLResponse(
            _approval_page(
                token=token,
                client_name=getattr(client, "client_name", None),
                redirect_uri=pending.redirect_uri,
                email=identity.email,
            ),
            headers={"Cache-Control": "no-store"},
        )

    async def handle_approval(self, request: Request) -> Response:
        """Custom route (unauthenticated, one-time-token bound) — the person's answer to the page
        above. Only ``approve`` mints a code; anything else drops the parked login."""
        from starlette.responses import HTMLResponse, RedirectResponse

        form = await request.form()
        token = str(form.get("approval_token") or "")
        decision = str(form.get("decision") or "")
        self._prune_approvals()
        parked = self._awaiting_approval.pop(token, None)
        if parked is None:
            return HTMLResponse(
                _error_page("この確認は期限切れ、または既に使われています。もう一度接続し直してください。"),
                status_code=400,
            )
        identity, pending, _ = parked
        if decision != "approve":
            logger.info("waca-loop-serve: connection declined by the user at the approval page")
            return HTMLResponse(
                _error_page("接続を許可しませんでした。何も渡していません。"), status_code=200
            )

        mcp_code = self._issue_code(identity, pending)
        redirect = construct_redirect_uri(
            pending.redirect_uri, code=mcp_code, state=pending.mcp_state
        )
        return RedirectResponse(
            url=redirect, status_code=303, headers={"Cache-Control": "no-store"}
        )

    def _prune_approvals(self) -> None:
        now = time.time()
        self._awaiting_approval = {
            t: v for t, v in self._awaiting_approval.items() if v[2] > now
        }

    async def _exchange_google_code(self, code: str) -> Identity:
        """POST the Google token endpoint and verify the returned ID token → :class:`Identity`."""
        import httpx

        data = {
            "code": code,
            "client_id": self._settings.oauth_client_id,
            "client_secret": self._settings.oauth_client_secret,
            "redirect_uri": self._google_redirect_uri,
            "grant_type": "authorization_code",
        }
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.post(GOOGLE_TOKEN_ENDPOINT, data=data)
        if resp.status_code != 200:
            raise RuntimeError(f"google token endpoint {resp.status_code}: {resp.text[:200]}")
        id_token = resp.json().get("id_token")
        if not id_token:
            raise RuntimeError("google token response missing id_token")
        # Audience is our Web OAuth client_id (OIDC binds `aud` to the requesting client).
        return verify_google_token(id_token, audience=self._settings.oauth_client_id)

    def _claim_pending(self, identity: Identity) -> bool:
        """Bind a pre-registered person to this ``sub`` on their first login (§5).

        People are registered ahead of time by email (``scripts/register_members.py`` or
        ``scripts/preregister_from_sheet.py``), which removes the "first login always fails, an
        admin digs the sub out of Cloud Logging, try again" step. The conditions live in
        :func:`waca_loop.auth.identity.claim_preregistration` (shared with the local server);
        this wrapper adds the logging.
        """
        from waca_loop.auth.identity import claim_preregistration
        from waca_loop.grounding.persons import normalize_email
        from waca_loop.org import get_org_config

        org_domains = get_org_config().workspace_domains
        if identity.email and identity.email_verified:
            domain = normalize_email(identity.email).rpartition("@")[2]
            if domain in org_domains and identity.hd not in org_domains:
                logger.warning(
                    "waca-loop-serve: refused to claim a %s pre-registration from a "
                    "non-organizational account (hd=%r)",
                    domain,
                    identity.hd,
                )
                return False
        person_id = claim_preregistration(identity, self._persons, org_domains)
        if person_id is None:
            return False
        # Cloud Logging is a durable sink read by more people than oauth_identity_map, so the
        # raw sub is hashed here exactly as the ledger hashes it (§5).
        from waca_loop.grounding.persons import hash_subject

        logger.info(
            "waca-loop-serve: bound pre-registered person_id=%s to %s on first login",
            person_id,
            hash_subject(identity.subject),
        )
        return True

    def _resolve_or_claim(self, identity: Identity) -> bool:
        """The identity-map allowlist gate (SPEC_phase1 §5): is this ``sub`` someone we know?

        Kept separate from minting the code so the approval page can sit between the
        two — the gate says *who*, the page says *to which client*.
        """
        if self._persons.resolve_person_id(identity.subject) is None and not self._claim_pending(
            identity
        ):
            from waca_loop.grounding.persons import hash_subject

            # Hashed like the ledger (§5): Cloud Logging is durable and read by more people
            # than oauth_identity_map. The email stays — an admin registering this person
            # needs to know who was turned away, and it is already in the org sheet.
            logger.warning(
                "waca-loop-serve: rejected unregistered %s email=%s "
                "(not in oauth_identity_map)",
                hash_subject(identity.subject),
                identity.email,
            )
            return False
        return True

    def _issue_code(self, identity: Identity, pending: _Pending) -> str:
        """Store & return our authorization code. Callers must have passed the gate first."""
        mcp_code = secrets.token_urlsafe(32)
        self._codes[mcp_code] = AuthorizationCode(
            code=mcp_code,
            scopes=pending.scopes,
            expires_at=time.time() + _CODE_TTL,
            client_id=pending.client_id,
            code_challenge=pending.code_challenge,
            redirect_uri=pending.redirect_uri,  # type: ignore[arg-type]  # AnyUrl coerces str
            redirect_uri_provided_explicitly=pending.redirect_uri_provided_explicitly,
            resource=pending.resource,
            subject=identity.subject,
        )
        return mcp_code

    # --- Token issuance / verification -----------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        if code.expires_at < time.time():
            self._codes.pop(authorization_code, None)
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)
        return self._mint(
            client.client_id or "", authorization_code.scopes, authorization_code.subject
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        payload = self._verify(refresh_token, "refresh")
        if payload is None or payload.get("cid") != client.client_id:
            return None
        if payload.get("jti") in self._revoked_jtis:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=payload["cid"],
            scopes=list(payload.get("scp") or []),
            expires_at=payload["exp"],
            subject=payload.get("sub"),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Re-apply the identity-map allowlist gate (SPEC_phase1 §5) on every refresh: someone
        # removed from `oauth_identity_map` stops refreshing immediately, so their access ends
        # within one access-token TTL even though the refresh token itself is stateless.
        subject = refresh_token.subject
        if subject is None or self._persons.resolve_person_id(subject) is None:
            logger.warning(
                "waca-loop-serve: refused refresh for a subject no longer in "
                "oauth_identity_map (client_id=%s)",
                client.client_id,
            )
            raise TokenError("invalid_grant", "subject is no longer authorized")
        # Best-effort rotation (in-process): mark the presented token's jti as used.
        payload = self._verify(refresh_token.token, "refresh")
        if payload is not None:
            self._mark_revoked(payload["jti"], payload["exp"])
        use_scopes = scopes or refresh_token.scopes
        return self._mint(client.client_id or "", use_scopes, subject)

    async def load_access_token(self, token: str) -> AccessToken | None:
        payload = self._verify(token, "access")
        if payload is None or payload.get("jti") in self._revoked_jtis:
            return None
        return AccessToken(
            token=token,
            client_id=payload["cid"],
            scopes=list(payload.get("scp") or []),
            expires_at=payload["exp"],
            subject=payload.get("sub"),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        tok = getattr(token, "token", None)
        if tok is None:
            return
        for typ in ("access", "refresh"):
            payload = self._verify(tok, typ)
            if payload is not None:
                self._mark_revoked(payload["jti"], payload["exp"])

    def _mint(self, client_id: str, scopes: list[str], subject: str | None) -> OAuthToken:
        """Mint a self-contained access+refresh token pair — nothing is stored server-side.

        The granted scopes are **clamped to** :data:`GRANTED_SCOPES`, never the label set the
        client registered. DCR is unauthenticated and scope-permissive, so without this an
        anonymous client could mint itself a token reading ``scp=["steward", "admin"]`` — worth
        nothing today (``required_scopes=[]``, the ACL reads the *person*), but it would turn the
        first scope check anyone adds later into self-service. Granting fewer scopes than were
        requested is ordinary OAuth (RFC 6749 §5.1) — hence ``scope`` in the response.
        """
        scopes = [s for s in scopes if s in GRANTED_SCOPES]
        access = mint_token(
            token_type="access",
            subject=subject,
            client_id=client_id,
            scopes=scopes,
            ttl=ACCESS_TOKEN_TTL,
            key=self._signing_key,
            issuer=self._settings.mcp_public_url,
            audience=self._audience,
        )
        refresh = mint_token(
            token_type="refresh",
            subject=subject,
            client_id=client_id,
            scopes=scopes,
            ttl=REFRESH_TOKEN_TTL,
            key=self._signing_key,
            issuer=self._settings.mcp_public_url,
            audience=self._audience,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes) or None,
            refresh_token=refresh,
        )


def build_auth_settings(settings: Any) -> Any:
    """FastMCP ``AuthSettings`` for hosted mode: *this* server is the issuer + resource server,
    with Dynamic Client Registration enabled so the MCP client can self-register.

    **Scope-permissive on purpose.** With ``valid_scopes=["openid", "email"]`` the SDK would
    reject — *before this provider is ever called* — any DCR whose ``scope`` mentioned anything
    else (``profile``, ``offline_access``, …) with 400 ``invalid_client_metadata``; and because
    ``/authorize`` then checks the requested scope against the **registered** scope, a client
    asking for one of those would bounce straight back to its redirect_uri with
    ``error=invalid_scope`` — never reaching the Google login screen (some MCP clients, such as
    ChatGPT's web connector, do ask for such scopes).

    Accepting the label costs nothing, because **in WACA Loop scope is not the authorization
    mechanism**:

    - the gate is the ``oauth_identity_map`` allowlist applied at Google-callback time
      (SPEC_phase1 §5) —
      an unregistered ``sub`` gets no code and no token, whatever it asked for;
    - what a viewer may then see is the 3-axis ACL (:mod:`waca_loop.retrieval.acl`, §3.3),
      which reads the *person*, never the token's ``scp``;
    - ``required_scopes=[]`` — no tool call is scope-checked;
    - what we ask **Google** for is hard-coded ``openid email``
      (:meth:`GoogleBrokeringAuthProvider.authorize`) and does not follow the client's request.

    So a client may register itself with any scope string; it buys no access. What the AS
    honestly *uses* is still advertised as ``openid email`` — see
    :data:`waca_loop.auth.asgi.ADVERTISED_SCOPES` (``scopes_supported`` in the metadata) —
    and **the load-bearing line is the clamp in** :meth:`GoogleBrokeringAuthProvider._mint`:
    a self-declared label never reaches a token's ``scp``. Anyone adding a scope check later
    must read that method first.

    **Audience binding.** Minted tokens carry ``aud`` = the advertised resource, and
    :func:`..tokens.verify_token` refuses a token whose ``aud`` names a *different* resource. A
    token with no ``aud`` at all still passes unless ``MCP_REQUIRE_AUD=1`` (strict mode) — so
    that tokens minted without ``aud`` do not all die at once, which would force everyone to
    re-login in one step.
    Both the ``aud`` here and the ``resource`` advertised below come from
    :func:`resource_identifier`, so they are the same string by construction.

    The residual risk strict mode does not cover: a second service sharing BOTH
    ``MCP_TOKEN_SIGNING_KEY`` and ``iss`` could still mint tokens this server accepts — the
    answer there is not to share the key.

    ``resource_server_url`` is the **MCP endpoint** (``…/mcp``), not the origin: RFC 9728 §3.1
    makes the metadata URL ``/.well-known/oauth-protected-resource/mcp``, which is the first
    place a spec-following client looks (and what the SDK then names in ``WWW-Authenticate``).
    The bare-origin path stays served as an alias by
    :class:`waca_loop.auth.asgi.MetadataCompatMiddleware`, so clients that only try the root
    keep working.
    """
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
    from pydantic import AnyHttpUrl

    base = AnyHttpUrl(settings.mcp_public_url)
    # Strict clients compare the advertised `resource` against the URL they connected to and
    # refuse a mismatch, e.g.:
    #   "Protected resource https://…/ does not match expected https://…/mcp"
    # so the resource is the /mcp endpoint. The *issuer* stays at the origin — this server is
    # its own AS, mounted at the root.
    resource = AnyHttpUrl(resource_identifier(settings.mcp_public_url))
    return AuthSettings(
        issuer_url=base,
        resource_server_url=resource,
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            # None = the SDK does not police the registered scope (see docstring). The default
            # still applies when a client omits `scope` entirely.
            valid_scopes=None,
            default_scopes=["openid", "email"],
        ),
        # The real gate is the identity_map allowlist (applied at Google-callback time), not a
        # scope check, so we don't force scopes on each tool call.
        required_scopes=[],
    )


def _approval_page(
    *, token: str, client_name: str | None, redirect_uri: str, email: str | None
) -> str:
    """The "which client is this for?" page.

    Everything variable here comes from an **unauthenticated** DCR document or from the parked
    request, so every value is HTML-escaped and length-capped: ``client_name`` is free text a
    stranger chose, and rendering it raw would hand them script execution on our origin.

    The redirect target is shown as its **host**, because that is the part that decides where the
    code goes — a full URL invites the reader to skim it.
    """
    from html import escape
    from urllib.parse import urlparse

    name = (client_name or "(名前を名乗っていないクライアント)")[:80]
    host = urlparse(redirect_uri).netloc[:120] or "(不明)"
    who = (email or "(不明)")[:120]
    return (
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>waca-loop 接続の確認</title></head>"
        "<body style='font-family:sans-serif;max-width:34rem;margin:3rem auto;padding:0 1rem;"
        "line-height:1.7'>"
        "<h1 style='font-size:1.15rem'>この接続を許可しますか？</h1>"
        f"<p>WACA Loop（{escape(_org_name())}の用語・過去の判断・AIポリシー）を、"
        "次のアプリに渡します。</p>"
        "<table style='border-collapse:collapse;margin:1.2rem 0'>"
        f"<tr><td style='padding:.3rem 1rem .3rem 0;color:#555'>アプリ名</td>"
        f"<td><b>{escape(name)}</b></td></tr>"
        f"<tr><td style='padding:.3rem 1rem .3rem 0;color:#555'>戻り先</td>"
        f"<td><b>{escape(host)}</b></td></tr>"
        f"<tr><td style='padding:.3rem 1rem .3rem 0;color:#555'>あなた</td>"
        f"<td>{escape(who)}</td></tr>"
        "</table>"
        "<p style='background:#fff8e1;border-left:3px solid #f0b429;padding:.6rem .9rem'>"
        "<b>アプリ名と戻り先に心当たりがなければ、「許可しない」を押してください。</b><br>"
        "自分で接続の設定をしているとき以外に、この画面が出ることはありません。</p>"
        f"<form method='post' action='{escape(GOOGLE_APPROVE_PATH)}'>"
        f"<input type='hidden' name='approval_token' value='{escape(token)}'>"
        "<button type='submit' name='decision' value='approve' "
        "style='font-size:1rem;padding:.6rem 1.4rem;margin-right:.8rem'>許可する</button>"
        "<button type='submit' name='decision' value='deny' "
        "style='font-size:1rem;padding:.6rem 1.4rem'>許可しない</button>"
        "</form></body></html>"
    )


def _error_page(message: str) -> str:
    return (
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        "<title>waca-loop 接続</title></head>"
        "<body style='font-family:sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
        f"<h1 style='font-size:1.1rem'>接続できませんでした</h1><p>{message}</p></body></html>"
    )


def _org_name() -> str:
    from waca_loop.org import get_org_config

    return get_org_config().name
