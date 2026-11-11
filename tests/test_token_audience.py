"""トークンを「宛先(aud)」で束縛する.

メタデータで「このトークンは …/mcp 用」と広告しても、トークン自体に宛先を刻まなければ、
別のMCPサーバに誘導されて発行させられたトークンを WACA Loop に提示されたとき、
WACA Loop 側に断る根拠が無い（＝その人として使える）。

宛先の無いトークンまで一律に断ると、宛先を刻まずに発行されたトークンを持つ全員が一斉に
ログアウトされる。そのため2つのモードがある。ここで固定するのは「緩いモード（既定）は宛先の
無いトークンを通しつつ**間違った宛先だけ**を弾く」「厳格なモード（`MCP_REQUIRE_AUD`）は
宛先の無いトークンも弾く」の両方。
"""

from __future__ import annotations

from waca_loop.auth.tokens import mint_token, verify_token

KEY = "test-signing-key-0123456789abcdefghijklmn"
ISS = "https://loop.example.run.app"
AUD = "https://loop.example.run.app/mcp"


def _mint(**over):
    kw = dict(
        token_type="access", subject="sub-1", client_id="cid-1",
        scopes=["openid", "email"], ttl=3600, key=KEY, issuer=ISS,
    )
    kw.update(over)
    return mint_token(**kw)


def _verify(token, **over):
    kw = dict(key=KEY, expected_type="access", expected_issuer=ISS, expected_audience=AUD)
    kw.update(over)
    return verify_token(token, **kw)


def test_a_token_minted_for_this_resource_carries_the_claim_and_verifies():
    payload = _verify(_mint(audience=AUD))
    assert payload is not None
    assert payload["aud"] == AUD


def test_a_token_minted_for_another_resource_is_refused():
    """これが穴そのもの。別のリソース宛のトークンは、緩いモードでも通らない."""
    assert _verify(_mint(audience="https://evil.example.com/mcp")) is None


def test_lenient_mode_accepts_a_token_without_aud():
    """宛先の無いトークン(aud 無し)は、緩いモードでは通る — 全員の一斉ログアウトを避ける."""
    assert _verify(_mint()) is not None


def test_strict_mode_refuses_a_token_without_aud():
    assert _verify(_mint(), require_audience=True) is None


def test_strict_mode_accepts_a_correctly_bound_token():
    assert _verify(_mint(audience=AUD), require_audience=True) is not None


def test_without_an_expected_audience_the_claim_is_not_checked():
    """stdio/開発では宛先の概念が無いので、素通りさせる(挙動を変えない)."""
    assert verify_token(
        _mint(audience="https://anything.example/mcp"),
        key=KEY, expected_type="access", expected_issuer=ISS,
    ) is not None


def test_the_audience_is_covered_by_the_signature():
    """aud を後から差し替えられないこと(署名の範囲内)."""
    import base64
    import json

    token = _mint(audience=AUD)
    prefix, payload_b64, sig = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
    payload["aud"] = "https://evil.example.com/mcp"
    forged = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=").decode()
    assert _verify(f"{prefix}.{forged}.{sig}") is None


def test_the_provider_binds_both_tokens_to_the_mcp_endpoint():
    """発行側: access も refresh も、広告している …/mcp と同じ宛先を刻む."""
    import base64
    import json

    from waca_loop.auth.as_provider import GoogleBrokeringAuthProvider
    from waca_loop.auth.client_store import InMemoryOAuthClientStore
    from waca_loop.config import Settings
    from waca_loop.grounding.persons import InMemoryPersonStore

    settings = Settings(MCP_PUBLIC_URL=ISS, MCP_TOKEN_SIGNING_KEY=KEY,
                        OAUTH_CLIENT_ID="g", OAUTH_CLIENT_SECRET="s")
    provider = GoogleBrokeringAuthProvider(
        settings, InMemoryPersonStore(persons=[], identity_map={}),
        client_store=InMemoryOAuthClientStore(), signing_key=KEY,
    )
    tok = provider._mint("cid-1", ["openid", "email"], "sub-1")  # noqa: SLF001

    def aud_of(t):
        payload_b64 = t.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))["aud"]

    assert aud_of(tok.access_token) == AUD
    assert aud_of(tok.refresh_token) == AUD


def test_strict_mode_is_wired_through_the_provider():
    """設定 → provider._verify まで届いているか（フラグが「効いているつもり」でないこと）.

    厳格なモードで、aud 無しのトークンが本当に落ちることを、provider 越しで確かめる。
    """
    from waca_loop.auth.as_provider import GoogleBrokeringAuthProvider
    from waca_loop.auth.client_store import InMemoryOAuthClientStore
    from waca_loop.config import Settings
    from waca_loop.grounding.persons import InMemoryPersonStore

    unbound = _mint()                     # aud を持たないトークン
    bound = _mint(audience=AUD)

    def provider(require: bool):
        s = Settings(MCP_PUBLIC_URL=ISS, MCP_TOKEN_SIGNING_KEY=KEY,
                     OAUTH_CLIENT_ID="g", OAUTH_CLIENT_SECRET="s",
                     MCP_REQUIRE_AUD=require)
        return GoogleBrokeringAuthProvider(
            s, InMemoryPersonStore(persons=[], identity_map={}),
            client_store=InMemoryOAuthClientStore(), signing_key=KEY,
        )

    lenient, strict = provider(False), provider(True)
    assert lenient._verify(unbound, "access") is not None     # noqa: SLF001
    assert strict._verify(unbound, "access") is None         # noqa: SLF001
    assert strict._verify(bound, "access") is not None       # noqa: SLF001


def test_the_strict_mode_switch_is_reachable_from_the_environment(monkeypatch):
    """`MCP_REQUIRE_AUD=1` という**環境変数の綴り**で効くこと.

    コンストラクタ引数はフィールド名でも通ってしまうため、それだけでは別名が生きている
    証明にならない。本番で立てるのは環境変数のほうなので、そちらを固定する。
    """
    from waca_loop.config import Settings

    monkeypatch.setenv("MCP_REQUIRE_AUD", "1")
    assert Settings(MCP_PUBLIC_URL=ISS, MCP_TOKEN_SIGNING_KEY=KEY,
                    OAUTH_CLIENT_ID="g", OAUTH_CLIENT_SECRET="s").mcp_require_aud is True
    monkeypatch.delenv("MCP_REQUIRE_AUD")
    assert Settings(MCP_PUBLIC_URL=ISS, MCP_TOKEN_SIGNING_KEY=KEY,
                    OAUTH_CLIENT_ID="g", OAUTH_CLIENT_SECRET="s").mcp_require_aud is False


def test_the_advertised_resource_and_the_token_audience_are_one_string():
    """正規化が入る側と入らない側で別式になっていないこと."""
    from waca_loop.auth.as_provider import (
        GoogleBrokeringAuthProvider,
        build_auth_settings,
        resource_identifier,
    )
    from waca_loop.auth.client_store import InMemoryOAuthClientStore
    from waca_loop.config import Settings
    from waca_loop.grounding.persons import InMemoryPersonStore

    # 正規化が実際に効く綴り（ホスト大文字＋既定ポート）で比べる。
    s = Settings(MCP_PUBLIC_URL="https://Loop.Example.run.app:443",
                 MCP_TOKEN_SIGNING_KEY=KEY, OAUTH_CLIENT_ID="g", OAUTH_CLIENT_SECRET="s")
    provider = GoogleBrokeringAuthProvider(
        s, InMemoryPersonStore(persons=[], identity_map={}),
        client_store=InMemoryOAuthClientStore(), signing_key=KEY,
    )
    # 発行側(aud)と広告側(resource)を**直接**比べる。両方を同じ関数で作ると同語反復になり、
    # ドリフトが見えない。
    assert provider._audience == str(build_auth_settings(s).resource_server_url)  # noqa: SLF001
    assert provider._audience == resource_identifier(s.mcp_public_url)            # noqa: SLF001
    assert "Loop.Example" not in provider._audience                                 # noqa: SLF001
