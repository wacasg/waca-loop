"""Typed configuration loaded from environment (.env). See .env.example."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    """Raised when required configuration is missing (§4). Message names the exact key(s)."""


def find_env_file() -> str | None:
    """Locate the .env file robustly, independent of the current working directory (§4).

    Order: (1) ``WACA_LOOP_ENV`` if set (returned as-is so a missing path surfaces a
    clear error downstream), (2) upward search for ``.env`` from the package location.
    Returns ``None`` when nothing is found (env vars may still be set in the environment).
    """
    override = os.environ.get("WACA_LOOP_ENV")
    if override:
        return override
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        candidate = parent / ".env"
        if candidate.is_file():
            return str(candidate)
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # BigQuery (datasets are created in the configured project — §0/§2.1)
    gcp_project: str = Field(default="", validation_alias="GCP_PROJECT")
    ds_grounding: str = Field(default="waca_loop_grounding", validation_alias="DS_GROUNDING")
    ds_ledger: str = Field(default="waca_loop_ledger", validation_alias="DS_LEDGER")
    ds_feedback: str = Field(default="waca_loop_feedback", validation_alias="DS_FEEDBACK")
    ds_knowledge: str = Field(default="waca_loop_knowledge", validation_alias="DS_KNOWLEDGE")
    # Source-layer envelope for all ingestion connectors (ARCHITECTURE_knowledge_layers.md).
    ds_integration: str = Field(default="waca_loop_integration", validation_alias="DS_INTEGRATION")
    bq_location: str = Field(default="US", validation_alias="BQ_LOCATION")
    bq_connection: str = Field(default="", validation_alias="BQ_CONNECTION")

    # Google OAuth (§5)
    oauth_client_id: str = Field(default="", validation_alias="OAUTH_CLIENT_ID")
    oauth_client_secret: str = Field(default="", validation_alias="OAUTH_CLIENT_SECRET")
    oauth_scopes: str = Field(
        default="https://www.googleapis.com/auth/bigquery openid email",
        validation_alias="OAUTH_SCOPES",
    )
    oauth_token_store: str = Field(default=".tokens/", validation_alias="OAUTH_TOKEN_STORE")

    # Embeddings (§2.3 — pick one before backfill)
    embedding_provider: str = Field(default="", validation_alias="EMBEDDING_PROVIDER")
    embedding_model: str = Field(default="", validation_alias="EMBEDDING_MODEL")
    embedding_dim: int = Field(default=768, validation_alias="EMBEDDING_DIM")

    # ERN 鮮度減衰の半減期（E9）。**ロジック内リテラル禁止** — engine/signals 側に
    # 数値を書くと、設定を変えても候補が変わらなくなり E9 が形骸化する。
    ern_half_life_days: int = Field(default=180, validation_alias="ERN_HALF_LIFE_DAYS")

    # Retention / monitor
    usage_log_retention_days: int = Field(default=730, validation_alias="USAGE_LOG_RETENTION_DAYS")
    # ERN 監査（誰が誰を探したか）の保持期間。**`usage_log` より短くする** — こちらのほうが
    # 機微だからである。
    # DDL 側の `partition_expiration_days` と揃えること（`sql/ddl/15_ern.sql`）。
    ern_audit_retention_days: int = Field(default=365, validation_alias="ERN_AUDIT_RETENTION_DAYS")
    monitor_window_days: int = Field(default=7, validation_alias="MONITOR_WINDOW_DAYS")

    # Survey / improvement loop
    # pseudonymous (default) | identified | anonymous
    survey_attribution: str = Field(default="pseudonymous", validation_alias="SURVEY_ATTRIBUTION")
    survey_cadence: str = Field(default="monthly", validation_alias="SURVEY_CADENCE")

    # WACA Loop ops guards (SPEC_ops R2). strict=False: log_usage WITHOUT org_context is recorded
    # (R1 acceptance); provided-but-unrelated org_context still goes
    # pending. strict=True: absent org_context also goes pending — flip on after the §6 client
    # instructions are distributed to all users.
    org_guard_strict: bool = Field(default=False, validation_alias="ORG_GUARD_STRICT")
    # セッションスコープ宣言の有効期限 (SPEC_ops R1)。
    # 既定は12時間 — 「朝に宣言して夕方まで効く」を満たしつつ、**夜をまたがない**。戻し忘れは
    # 翌日の問題として現れる（別の会話・別の日に記録が落ちる）ので、そこで切れるのが要点。
    # 短くすると私事の会話の続きが記録され始める危険が増し、長くすると戻し忘れが延命する。
    scope_ttl_hours: int = Field(default=12, validation_alias="SCOPE_TTL_HOURS")
    # 改善要望フォームの流入超過アラート閾値 (SPEC_improvement_requests §3.2)。
    # 「大量のときだけアラート」— 超えたら取り込みを止めて知らせる。捨てないし全件通しもしない。
    form_burst_threshold: int = Field(
        default=30, validation_alias="FORM_BURST_THRESHOLD"
    )
    # 改善要望を映すシート (SPEC_improvement_requests §5・§10)。未設定なら即時反映は無効。
    improvement_sheet_id: str = Field(default="", validation_alias="IMPROVEMENT_SHEET_ID")
    # シート書き込み用のサービスアカウント（シートを編集者で共有しておくこと）。
    # ユーザADCは spreadsheets スコープを取れないので、実運用はこちらを使う。
    sheets_impersonate_sa: str = Field(default="", validation_alias="SHEETS_IMPERSONATE_SA")
    # Lite Policy / Knowledge Store. live Sheets API ではなく、Sheets から export した
    # JSON grid/rows を読む。Google API で直接読む reader は、この backend の外側に置く。
    lite_policy_store_json: str = Field(default="", validation_alias="LITE_POLICY_STORE_JSON")

    # Hosted remote-server mode (service-account BigQuery + per-request identity).
    # stdio (default, local admin/dev) | http (Cloud Run remote MCP, Streamable HTTP).
    server_mode: str = Field(default="stdio", validation_alias="SERVER_MODE")
    # How the BigQuery client authenticates:
    #   adc        -> Application Default Credentials (the Cloud Run service account). The server
    #                 is the SOLE gatekeeper; no user ever holds BigQuery IAM (the ACL invariant
    #                 §3.3 is only meaningful this way). Default for http mode.
    #   user_oauth -> the running admin's interactive OAuth creds (local stdio admin only).
    bq_auth: str = Field(default="user_oauth", validation_alias="BQ_AUTH")
    # Public HTTPS base URL of the deployed server. In hosted mode (docs/SETUP_gcp.md §8) this
    # server IS the OAuth authorization server, so this is the issuer + resource identifier
    # for all OAuth metadata
    # (/.well-known/oauth-authorization-server + /.well-known/oauth-protected-resource) and the
    # base for the Google callback route (/auth/google/callback). See auth/as_provider.py.
    mcp_public_url: str = Field(default="", validation_alias="MCP_PUBLIC_URL")
    # Strict audience mode. False = tokens without an `aud` claim still work
    # (see auth/tokens.verify_token). Flip to 1 only after every refresh token without `aud`
    # has expired, or everyone still holding one is logged out at once.
    mcp_require_aud: bool = Field(default=False, validation_alias="MCP_REQUIRE_AUD")
    # NOTE: login is NOT restricted to a single Workspace domain — a roster may span several
    # Workspace domains plus consumer addresses (gmail.com carries no `hd` claim at all), so a
    # blanket domain gate cannot express it. The gate is the
    # `oauth_identity_map` allowlist applied at Google-callback time in auth/as_provider.py:
    # an unregistered `sub` is rejected outright (no token/code issued), never a visitor.
    # `hd` IS checked, but only where it means something: claiming a pre-registration whose
    # address is at one of `workspace_domains`. See _claim_pending.
    # Listen port for http mode (Cloud Run injects $PORT).
    port: int = Field(default=8080, validation_alias="PORT")
    # HMAC key for the AS's self-contained access/refresh tokens (auth/tokens.py). Required in
    # http mode (serve_http fails closed without it); keep it in Secret Manager, never in logs.
    # Rotating it invalidates all outstanding tokens (users re-login via Google once).
    mcp_token_signing_key: str = Field(default="", validation_alias="MCP_TOKEN_SIGNING_KEY")

    @property
    def scopes_list(self) -> list[str]:
        return [s for s in self.oauth_scopes.split() if s]

    @property
    def workspace_domains_set(self) -> frozenset[str]:
        """The organization's Google Workspace domains (org.toml [org] workspace_domains).

        Pre-registration by email requires the ID token's ``hd`` claim to match one of these, so
        an address at such a domain can only be claimed by an account its Workspace issued.
        Addresses elsewhere (gmail.com etc.) carry no ``hd`` and are not subject to that check.
        """
        from waca_loop.org import get_org_config

        return get_org_config().workspace_domains

    @property
    def export_admin_units_set(self) -> frozenset[str]:
        """FLOOR-02 break-glass holders (org.toml [units] export_admin; empty = nobody).

        Units allowed to exceed the per-tool result cap (server._K_CAPS) by stating a reason.
        The roster (`_K_CAPS["persons"]`, `find_expert`) stays outside break-glass regardless,
        and exceeding still requires a reason AND a live audit sink.
        """
        from waca_loop.org import get_org_config

        return get_org_config().units.export_admin

    def table(self, dataset: str, name: str) -> str:
        """Fully-qualified ``project.dataset.table`` reference."""
        return f"{self.gcp_project}.{dataset}.{name}"

    @property
    def terms_table(self) -> str:
        return self.table(self.ds_grounding, "terms")

    @property
    def persons_table(self) -> str:
        return self.table(self.ds_grounding, "persons")

    @property
    def identity_map_table(self) -> str:
        return self.table(self.ds_grounding, "oauth_identity_map")

    @property
    def oauth_clients_table(self) -> str:
        return self.table(self.ds_grounding, "oauth_clients")

    @property
    def usage_log_table(self) -> str:
        return self.table(self.ds_ledger, "usage_log")

    @property
    def usage_withdrawals_table(self) -> str:
        return self.table(self.ds_ledger, "usage_withdrawals")

    @property
    def tool_events_table(self) -> str:
        return self.table(self.ds_ledger, "tool_events")

    @property
    def session_scopes_table(self) -> str:
        return self.table(self.ds_ledger, "session_scopes")

    @property
    def embedding_model_ref(self) -> str:
        """Fully-qualified embedding model created by sql/ddl/06_embedding_model.sql (bq_ml)."""
        return self.table(self.ds_grounding, "embedding_model")

    @property
    def survey_responses_table(self) -> str:
        return self.table(self.ds_feedback, "survey_responses")

    @property
    def improvement_tasks_table(self) -> str:
        return self.table(self.ds_feedback, "improvement_tasks")

    @property
    def improvement_requests_table(self) -> str:
        return self.table(self.ds_feedback, "improvement_requests")

    @property
    def extraction_candidates_table(self) -> str:
        return self.table(self.ds_feedback, "extraction_candidates")

    # --- ERN (SPEC_phase3 §1) ---
    @property
    def ern_assertions_table(self) -> str:
        return self.table(self.ds_knowledge, "ern_assertions")

    @property
    def ern_audit_table(self) -> str:
        return self.table(self.ds_feedback, "ern_audit")

    @property
    def policy_table(self) -> str:
        return self.table(self.ds_feedback, "policy")

    @property
    def questions_table(self) -> str:
        return self.table(self.ds_knowledge, "questions")

    @property
    def sfaq_feedback_table(self) -> str:
        return self.table(self.ds_knowledge, "feedback")

    @property
    def source_items_table(self) -> str:
        return self.table(self.ds_integration, "source_items")


# Keys with no safe default: the server cannot run without them (§4).
_REQUIRED_KEYS = {
    "GCP_PROJECT": "gcp_project",
    "OAUTH_CLIENT_ID": "oauth_client_id",
    "OAUTH_CLIENT_SECRET": "oauth_client_secret",
}


@lru_cache
def get_settings() -> Settings:
    env_file = find_env_file()
    settings = Settings(_env_file=env_file) if env_file else Settings()
    missing = [name for name, attr in _REQUIRED_KEYS.items() if not getattr(settings, attr)]
    if missing:
        where = env_file or "no .env found (searched upward from the package)"
        raise ConfigError(
            "Missing required config key(s): "
            + ", ".join(missing)
            + f". Set them in {where} (see .env.example), export them, "
            "or point WACA_LOOP_ENV at your .env."
        )
    return settings
