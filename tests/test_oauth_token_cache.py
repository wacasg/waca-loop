"""OAuth token cache: a valid cached token still needs an id_token (§5).

``Credentials.to_json()`` never persists ``id_token``, so a token cached within its
lifetime loads back *valid but id_token-less* — ``identity()`` would then fail until
the token expired. ``credentials()`` must refresh in that case to mint a fresh id_token.
"""

from __future__ import annotations

import google.oauth2.credentials as gcreds

from waca_loop.auth.oauth import OAuthManager


class _FakeCreds:
    def __init__(self) -> None:
        self.valid = True
        self.expired = False
        self.refresh_token = "refresh"
        self.id_token = None
        self.refreshed = 0

    def refresh(self, _request) -> None:
        self.refreshed += 1
        self.id_token = "header.payload.sig"

    def to_json(self) -> str:
        return f'{{"refreshed": {self.refreshed}}}'


class _Settings:
    """Minimal surface used by OAuthManager.credentials()."""

    scopes_list = ["openid", "email"]
    oauth_client_id = "client-id"
    oauth_client_secret = "client-secret"

    def __init__(self, store: str) -> None:
        self.oauth_token_store = store


def test_cached_valid_token_without_id_token_refreshes(tmp_path, monkeypatch):
    store = tmp_path / "tokens"
    store.mkdir()
    (store / "token.json").write_text("{}", encoding="utf-8")

    fake = _FakeCreds()
    monkeypatch.setattr(
        gcreds.Credentials, "from_authorized_user_file",
        staticmethod(lambda _path, _scopes: fake),
    )

    creds = OAuthManager(_Settings(str(store))).credentials()

    assert creds is fake
    assert fake.refreshed == 1          # refreshed despite being valid — to mint the id_token
    assert creds.id_token               # identity() can now decode sub/email


def test_cached_token_with_id_token_is_not_refreshed(tmp_path, monkeypatch):
    store = tmp_path / "tokens"
    store.mkdir()
    (store / "token.json").write_text("{}", encoding="utf-8")

    fake = _FakeCreds()
    fake.id_token = "already.there.sig"
    monkeypatch.setattr(
        gcreds.Credentials, "from_authorized_user_file",
        staticmethod(lambda _path, _scopes: fake),
    )

    OAuthManager(_Settings(str(store))).credentials()

    assert fake.refreshed == 0          # no needless refresh round-trip


def test_refreshed_token_is_written_back(tmp_path, monkeypatch):
    """A refresh must land on disk — otherwise the cache never ages forward (§5)."""
    store = tmp_path / "tokens"
    store.mkdir()
    token = store / "token.json"
    token.write_text("{}", encoding="utf-8")

    fake = _FakeCreds()
    monkeypatch.setattr(
        gcreds.Credentials, "from_authorized_user_file",
        staticmethod(lambda _path, _scopes: fake),
    )

    OAuthManager(_Settings(str(store))).credentials()

    assert fake.refreshed == 1
    assert token.read_text(encoding="utf-8") == '{"refreshed": 1}'
    assert token.stat().st_mode & 0o777 == 0o600     # the store holds a refresh token
    assert not list(store.glob(".token-*"))          # no temp file left behind


def test_revoked_refresh_token_falls_through_to_consent(tmp_path, monkeypatch):
    """A RefreshError must reach the consent flow, not escape and kill the server (§5)."""
    from google.auth.exceptions import RefreshError
    from google_auth_oauthlib.flow import InstalledAppFlow

    store = tmp_path / "tokens"
    store.mkdir()
    (store / "token.json").write_text("{}", encoding="utf-8")

    stale = _FakeCreds()
    stale.valid = False
    stale.expired = True

    def _boom(_request):
        raise RefreshError("invalid_grant")

    stale.refresh = _boom
    monkeypatch.setattr(
        gcreds.Credentials, "from_authorized_user_file",
        staticmethod(lambda _path, _scopes: stale),
    )

    fresh = _FakeCreds()

    class _FakeFlow:
        def run_local_server(self, port=0):
            return fresh

    monkeypatch.setattr(
        InstalledAppFlow, "from_client_config",
        staticmethod(lambda _config, _scopes: _FakeFlow()),
    )

    creds = OAuthManager(_Settings(str(store))).credentials()

    assert creds is fresh                                        # re-consented, did not raise
    assert (store / "token.json").read_text(encoding="utf-8") == '{"refreshed": 0}'


def test_stale_temp_tokens_are_swept(tmp_path, monkeypatch):
    """A SIGKILL mid-write leaves a plaintext token behind; _persist must clear it (§5)."""
    store = tmp_path / "tokens"
    store.mkdir()
    (store / "token.json").write_text("{}", encoding="utf-8")
    orphan = store / ".token-abc123.json"
    orphan.write_text('{"refresh_token": "leaked"}', encoding="utf-8")

    fake = _FakeCreds()
    monkeypatch.setattr(
        gcreds.Credentials, "from_authorized_user_file",
        staticmethod(lambda _path, _scopes: fake),
    )

    OAuthManager(_Settings(str(store))).credentials()

    assert not orphan.exists()
    assert not list(store.glob(".token-*"))
