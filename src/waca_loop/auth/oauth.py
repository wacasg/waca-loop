"""Google OAuth (§5).

Follows the common Google installed-app OAuth pattern: installed-app flow, on-disk token
store, and identity (``sub``/``email``) read from the ID token. Client config is the usual
Client ID/Secret, scopes, and token store.

Scopes (§5): BigQuery R/W + ``openid email``. The resolved ``sub`` is mapped to a
``person_id`` via ``oauth_identity_map`` (done in :mod:`waca_loop.auth.identity`).
"""

from __future__ import annotations

import os

os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from waca_loop.config import Settings


@dataclass(frozen=True)
class Identity:
    """The authenticated principal extracted from the OAuth ID token (§5)."""

    subject: str          # OAuth ``sub``
    email: str | None = None
    # Google's `email_verified` claim. False unless the issuer asserted it — anything that
    # binds an account BY EMAIL (auth/as_provider.py の事前登録) must require this, or an
    # address alone would be enough to claim someone else's registration.
    email_verified: bool = False
    # Google Workspace `hd` (hosted domain) claim; None for consumer accounts (gmail.com
    # never carries it). Proves the account is issued by that domain's Workspace, which
    # `email_verified` alone does not — see as_provider._claim_pending.
    hd: str | None = None


@runtime_checkable
class IdentityProvider(Protocol):
    def identity(self) -> Identity:
        ...

    def credentials(self) -> Any:
        ...


class StaticIdentity:
    """A fixed identity for tests / headless runs (no interactive OAuth)."""

    def __init__(self, subject: str, email: str | None = None, credentials: Any = None) -> None:
        self._identity = Identity(subject=subject, email=email)
        self._credentials = credentials

    def identity(self) -> Identity:
        return self._identity

    def credentials(self) -> Any:
        return self._credentials


class OAuthManager:
    """Interactive Google OAuth with a local token cache (§5).

    Lazy-imports ``google-auth-oauthlib`` so importing this module never requires the
    Google libraries. Token JSON is cached under ``OAUTH_TOKEN_STORE`` and refreshed.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._creds: Any | None = None

    @property
    def _token_path(self) -> Path:
        store = Path(self._settings.oauth_token_store)
        store.mkdir(mode=0o700, parents=True, exist_ok=True)
        return store / "token.json"

    def _client_config(self) -> dict:
        return {
            "installed": {
                "client_id": self._settings.oauth_client_id,
                "client_secret": self._settings.oauth_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }

    def _persist(self, creds: Any) -> None:
        """Write the token back to the store atomically, 0600 (§5).

        The store is shared across every worktree, so a half-written token.json would
        break all checkouts at once. Share it by symlinking the **directory** — a
        file-level symlink on token.json would be replaced by os.replace() below, which
        silently drops that checkout out of the shared store.
        """
        path = self._token_path
        # A SIGKILL between mkstemp and os.replace leaves a plaintext token behind;
        # nothing else cleans these up, so sweep before writing a new one.
        for stale in path.parent.glob(".token-*"):
            stale.unlink(missing_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".token-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(creds.to_json())
                fh.flush()
                os.fsync(fh.fileno())   # rename is atomic, but the bytes must land first
            os.chmod(tmp, 0o600)
            os.replace(tmp, str(path))
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _refresh(self, creds: Any) -> Any | None:
        """Refresh and persist; return None when the refresh token is no longer usable (§5).

        A revoked/expired refresh token raises ``RefreshError``. Letting it escape would
        kill the MCP server at startup instead of falling through to the consent flow.
        """
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request

        try:
            creds.refresh(Request())
        except RefreshError:
            return None
        self._persist(creds)
        return creds

    def credentials(self) -> Any:
        """Return valid credentials, running the consent flow / refresh as needed."""
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds: Credentials | None = None
        if self._token_path.exists():
            creds = Credentials.from_authorized_user_file(
                str(self._token_path), self._settings.scopes_list
            )
        if creds and creds.expired and creds.refresh_token:
            creds = self._refresh(creds)
        elif creds and creds.valid and not creds.id_token and creds.refresh_token:
            # Credentials.to_json() never persists id_token, so a still-valid cached token
            # loads without one and identity() would fail (§5). Refresh mints a fresh id_token.
            creds = self._refresh(creds)
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_config(
                self._client_config(), self._settings.scopes_list
            )
            creds = flow.run_local_server(port=0)
            self._persist(creds)
        self._creds = creds
        return creds

    def identity(self) -> Identity:
        """Extract ``sub``/``email`` from the cached ID token (§5)."""
        creds = self._creds or self.credentials()
        id_token = getattr(creds, "id_token", None)
        if isinstance(id_token, str):
            claims = _decode_id_token_unverified(id_token)
            return _identity_from_claims(claims)
        # Fall back to the cached token file's id_token if present.
        if self._token_path.exists():
            data = json.loads(self._token_path.read_text(encoding="utf-8"))
            if data.get("id_token"):
                claims = _decode_id_token_unverified(data["id_token"])
                return _identity_from_claims(claims)
        raise RuntimeError("No id_token available; ensure 'openid email' scopes are granted (§5).")


def _identity_from_claims(claims: dict) -> Identity:
    """Build an :class:`Identity` from ID-token claims, keeping what pre-registration needs."""
    return Identity(
        subject=claims.get("sub", ""),
        email=claims.get("email"),
        email_verified=claims.get("email_verified") is True,
        hd=claims.get("hd"),
    )


def _decode_id_token_unverified(id_token: str) -> dict:
    """Decode the JWT payload WITHOUT signature verification.

    Safe here only because the token came straight from Google's token endpoint over TLS
    within this process (the local stdio admin self-login). Do not use for tokens received
    from untrusted callers — use :func:`verify_google_token` for those.
    """
    import base64

    parts = id_token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))


def verify_google_token(
    id_token: str,
    *,
    audience: str,
    required_domain: str | None = None,
) -> Identity:
    """Verify a Google-issued ID token (JWT) and return its :class:`Identity` (§5, hosted mode).

    Unlike :func:`_decode_id_token_unverified`, this checks the **signature, issuer and
    expiry** against Google's keys — mandatory for inbound tokens from arbitrary callers on the
    hosted server. When ``required_domain`` is set, the verified ``hd`` (Workspace) claim must
    match, restricting access to the org's domain (e.g. ``example.org``).

    ``audience`` is **required and must be non-empty**: passing ``None`` to Google's verifier
    skips audience checking, which would let a validly-signed token minted for *any other* OAuth
    client verify here (cross-client token confusion). Per OIDC, a Google ID token's ``aud`` is
    always the ``client_id`` of the relying party that requested it, so the caller must pass
    **our registered Google OAuth ``client_id``** here — not the MCP resource server's own URL
    (``mcp_public_url``), which Google never puts in ``aud``.

    This is the building block the hosted server's MCP ``TokenVerifier`` plugs in;
    it raises ``ValueError`` on any verification failure so a bad token never yields a viewer.
    """
    if not audience:
        raise ValueError("audience is required for inbound token verification (no skip)")

    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    claims = google_id_token.verify_oauth2_token(
        id_token, google_requests.Request(), audience
    )
    issuer = claims.get("iss")
    if issuer not in ("accounts.google.com", "https://accounts.google.com"):
        raise ValueError(f"unexpected token issuer: {issuer!r}")
    if required_domain and claims.get("hd") != required_domain:
        raise ValueError("token domain (hd) not permitted")
    subject = claims.get("sub")
    if not subject:
        raise ValueError("verified token has no subject")
    return Identity(
        subject=subject,
        email=claims.get("email"),
        email_verified=bool(claims.get("email_verified")),
        hd=claims.get("hd"),
    )
